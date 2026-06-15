"""Meeting-notes ingestion.

Reads ``.txt`` / ``.md`` files from the ``notes/`` folder, newest first, and
(optionally) watches the folder with ``watchdog`` so freshly dropped files are
picked up without a restart. Context assembly is pull-based, so the watcher is
really just for logging/awareness — the source of truth is ``read_recent_notes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import NOTES_DIR
from app.logging_setup import get_logger

log = get_logger("notes")

NOTE_EXTENSIONS = (".txt", ".md")


@dataclass
class Note:
    path: Path
    content: str
    modified: float


def read_recent_notes(limit: int = 5, max_chars: int = 2000) -> list[Note]:
    """Return up to ``limit`` most-recently-modified notes, each truncated.

    Never raises — unreadable files are skipped and logged.
    """
    if not NOTES_DIR.exists():
        return []

    files = [
        p
        for p in NOTES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in NOTE_EXTENSIONS
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    notes: list[Note] = []
    for path in files[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read note %s: %s", path.name, exc)
            continue
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n…(truncated)"
        notes.append(Note(path=path, content=text, modified=path.stat().st_mtime))

    log.info("loaded %d recent note(s)", len(notes))
    return notes


class NotesWatcher:
    """Optional background watcher that logs when notes change.

    Safe to construct even if ``watchdog`` isn't installed — it simply no-ops.
    """

    def __init__(self, on_change=None):
        self._observer = None
        self._on_change = on_change

    def start(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            log.info("watchdog not installed; notes will still load on each query")
            return

        on_change = self._on_change

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: D401
                if event.is_directory:
                    return
                if Path(event.src_path).suffix.lower() in NOTE_EXTENSIONS:
                    log.info("notes changed: %s", Path(event.src_path).name)
                    if on_change:
                        try:
                            on_change()
                        except Exception:  # noqa: BLE001
                            log.debug("notes on_change callback failed", exc_info=True)

        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), str(NOTES_DIR), recursive=False)
            self._observer.start()
            log.info("watching notes folder: %s", NOTES_DIR)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start notes watcher: %s", exc)
            self._observer = None

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
