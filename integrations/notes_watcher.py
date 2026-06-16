"""Meeting-notes ingestion.

Notes are split into separate streams — Daedabyte, Brightpoint, DnD, and
General — each its own subfolder under ``notes/``, so the work/personal
streams never mix and JARVIS never has to guess which one a note belongs to.
This mirrors the category convention already used for Todoist projects
(``integrations/todoist.py``): a fixed set of named buckets, not a free-form
tag.

Reads ``.txt`` / ``.md`` files from ``notes/<category>/``, newest first, and
(optionally) watches the folder recursively with ``watchdog`` so freshly
dropped files are picked up without a restart. Context assembly is
pull-based, so the watcher is really just for logging/awareness — the source
of truth is ``read_recent_notes``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import NOTES_DIR
from app.logging_setup import get_logger

log = get_logger("notes")

NOTE_EXTENSIONS = (".txt", ".md")
CATEGORIES = ("Daedabyte", "General", "Brightpoint", "DnD")


def _resolve_category(category: str) -> str:
    """Case-insensitively match *category* against CATEGORIES. Raises ValueError if unknown."""
    for c in CATEGORIES:
        if c.lower() == category.lower():
            return c
    raise ValueError(
        f"Unknown notes category '{category}'. Must be one of: {', '.join(CATEGORIES)}."
    )


def _category_dir(category: str) -> Path:
    return NOTES_DIR / category


@dataclass
class Note:
    path: Path
    content: str
    modified: float


def read_recent_notes(category: str, limit: int = 5, max_chars: int = 2000) -> list[Note]:
    """Return up to *limit* most-recently-modified notes in *category*, each truncated.

    Never raises — an unknown category logs a warning and returns an empty
    list rather than raising, so a bad value degrades to "no notes" instead
    of crashing the tool call. Only ever reads from the requested category's
    subfolder, never another one, so the two work streams can't bleed into
    each other.
    """
    try:
        matched = _resolve_category(category)
    except ValueError as exc:
        log.warning(str(exc))
        return []

    folder = _category_dir(matched)
    if not folder.exists():
        return []

    files = [
        p
        for p in folder.iterdir()
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

    log.info("loaded %d recent note(s) from %s", len(notes), matched)
    return notes


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug or "note"


def create_note(
    category: str, content: str, title: str | None = None, date: str | None = None
) -> str:
    """Write a new note file to notes/<category>/, following the YYYY-MM-DD_topic.md convention.

    Never raises — returns a human-readable status string for Claude. If the
    generated filename already exists (e.g. a second note the same day with
    the same title), a numeric suffix is appended rather than overwriting.
    """
    try:
        matched = _resolve_category(category)
    except ValueError as exc:
        return f"Error: {exc}"

    note_date = date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(note_date)
    except ValueError:
        return f"Error: '{date}' is not a valid YYYY-MM-DD date."

    folder = _category_dir(matched)
    folder.mkdir(parents=True, exist_ok=True)

    slug = _slugify(title or "note")
    base_name = f"{note_date}_{slug}"
    path = folder / f"{base_name}.md"
    suffix = 2
    while path.exists():
        path = folder / f"{base_name}_{suffix}.md"
        suffix += 1

    body = f"# {title}\n\n{content}\n" if title else f"{content}\n"
    try:
        path.write_text(body, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("could not write note %s: %s", path.name, exc)
        return f"Error saving note: {exc}"

    log.info("created note %s/%s", matched, path.name)
    return f"Note saved as notes/{matched}/{path.name}."


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
            # recursive=True — notes now live under notes/<category>/ subfolders.
            self._observer.schedule(_Handler(), str(NOTES_DIR), recursive=True)
            self._observer.start()
            log.info("watching notes folder (recursive): %s", NOTES_DIR)
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
