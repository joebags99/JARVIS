"""Obsidian vault engine — JARVIS's "second brain".

An Obsidian vault is just a folder of markdown files using a few conventions:
YAML frontmatter, ``[[wikilinks]]``, ``#tags`` and folders. JARVIS reads and
writes it directly on disk (no plugin, no running Obsidian required), making the
vault a single, human-readable, inter-linked home for notes *and* long-term
memory — something the user can also open and edit in Obsidian.

This module owns:

* **Path-safe IO** — every read/write resolves *inside* the configured vault
  root; anything escaping it (``../``, an absolute path elsewhere) is rejected.
  "Write everywhere" means anywhere within the vault, never the wider filesystem.
* **Markdown conventions** — minimal, dependency-free frontmatter + wikilink
  parsing, tag extraction, and frontmatter stamping on write.
* **CRUD** — :func:`read_note`, :func:`write_note`, :func:`append_note`,
  :func:`list_notes`, plus :func:`search` (delegated to the FTS5
  :mod:`app.vault_index`).
* **Indexing glue** — :func:`reindex` (full reconcile on startup) and
  :class:`ObsidianWatcher` (incremental updates on file events), mirroring the
  old ``integrations.notes_watcher`` watcher.
* **Migration** — :func:`migrate_legacy`, a one-time, idempotent, non-destructive
  copy of the legacy ``notes/<category>/`` files and ``memory.db`` facts into the
  vault (originals left intact as a safety net).

Everything is best-effort and never raises across the tool boundary except via
:class:`VaultError`, which the tool handlers turn into a friendly message.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.config import CONFIG, NOTES_DIR, ROOT_DIR
from app.logging_setup import get_logger

log = get_logger("obsidian")

NOTE_EXT = ".md"
# Folders seeded in a fresh vault; JARVIS organizes its writes into these but also
# adapts to whatever structure it finds in an existing vault.
DEFAULT_FOLDERS = ["Sessions", "Daily", "People", "Projects", "Topics", "Memory", "Imported"]
MEMORY_DB_PATH = ROOT_DIR / "memory.db"
_MIGRATION_MARKER = ".jarvis_migrated"

# Inline ``#tag`` (allows nested tags like #project/alpha), and ``[[wikilink]]``
# with an optional ``|alias`` and ``#heading``/``^block`` suffix.
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_/-]*)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")


class VaultError(Exception):
    """A user-facing vault problem (bad path, missing note). Caught by handlers."""


@dataclass
class NoteRead:
    """The result of reading a note: parsed metadata, body, and its link graph."""

    path: str
    title: str
    meta: dict
    body: str
    links: list[str] = field(default_factory=list)
    backlinks: list[str] = field(default_factory=list)


# ── Paths ───────────────────────────────────────────────────────────────────
def vault_root() -> Path:
    """The configured vault folder, created if missing. Raises if unconfigured."""
    vault = CONFIG.obsidian_vault
    if vault is None:
        raise VaultError("no Obsidian vault configured (set OBSIDIAN_VAULT_PATH).")
    root = vault.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_path(rel: str) -> Path:
    """Resolve *rel* to a markdown file that is guaranteed to live in the vault.

    The single chokepoint confining every write to the vault. A relative path is
    joined under the root; an absolute one is taken as-is — but either way the
    *resolved* path (``..`` collapsed) must stay within the vault, so traversal
    (``../evil``) and absolute paths pointing elsewhere are refused. A ``.md``
    suffix is added when absent.
    """
    root = vault_root()
    raw = (rel or "").strip().replace("\\", "/")
    if not raw:
        raise VaultError("a note path is required.")
    p = Path(raw)
    candidate = (p if p.is_absolute() else root / raw).resolve()
    if not candidate.is_relative_to(root):
        raise VaultError(f"path '{raw}' is outside the vault and was refused.")
    if candidate.suffix.lower() != NOTE_EXT:
        candidate = candidate.with_suffix(NOTE_EXT)
    return candidate


def _rel(p: Path) -> str:
    """Vault-relative POSIX path used as a note's stable id everywhere."""
    return p.resolve().relative_to(vault_root()).as_posix()


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
    return slug or "note"


def _title_from_stem(stem: str) -> str:
    return re.sub(r"[_-]+", " ", stem).strip().title() or stem


def path_for_title(title: str, folder: str | None = None) -> str:
    """A vault-relative ``folder/slug.md`` path for a note created from a title."""
    slug = _slugify(title)
    folder = (folder or "").strip().strip("/")
    return f"{folder}/{slug}{NOTE_EXT}" if folder else f"{slug}{NOTE_EXT}"


def _noncolliding(p: Path) -> Path:
    """Append a numeric suffix until the path is free (mirrors create_note)."""
    if not p.exists():
        return p
    suffix = 2
    while True:
        candidate = p.with_name(f"{p.stem}_{suffix}{p.suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


# ── Frontmatter / markdown parsing (minimal, dependency-free) ────────────────
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---`` YAML-ish frontmatter block from the body.

    Deliberately tiny — handles ``key: value``, inline ``[a, b]`` lists, and
    ``- item`` list blocks (enough for ``title``/``tags``/dates). Unknown shapes
    fall through as plain strings. Returns ``({}, text)`` when there's no
    frontmatter.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    meta: dict = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if raw.lstrip().startswith("- ") and key:  # continuation of a list value
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(raw.lstrip()[2:].strip().strip("'\""))
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif value:
            meta[key] = value.strip("'\"")
        else:
            meta[key] = []  # may be filled by following "- item" lines
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def build_frontmatter(meta: dict) -> str:
    """Render a frontmatter block. ``title/created/updated/tags`` come first."""
    ordered = ["title", "created", "updated", "tags"]
    keys = [k for k in ordered if k in meta] + [k for k in meta if k not in ordered]
    out = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, (list, tuple)):
            out.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            out.append(f"{k}: {v}")
    out.append("---")
    return "\n".join(out) + "\n\n"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.lower()
        if it and key not in seen:
            out.append(it)
            seen.add(key)
    return out


def extract_tags(meta: dict, body: str) -> list[str]:
    """All tags for a note: frontmatter ``tags`` + inline ``#tags`` (no ``#``)."""
    fm = meta.get("tags") or []
    if isinstance(fm, str):
        fm = [t.strip() for t in fm.replace(",", " ").split()]
    tags = [str(t).lstrip("#") for t in fm]
    tags += _TAG_RE.findall(body or "")
    return _dedupe([t for t in tags if t])


def extract_wikilinks(body: str) -> list[str]:
    """Distinct ``[[targets]]`` referenced in *body* (alias/heading stripped)."""
    return _dedupe([m.strip() for m in _WIKILINK_RE.findall(body or "")])


def _has_heading(body: str) -> bool:
    for line in body.splitlines():
        if line.strip():
            return line.lstrip().startswith("#")
    return False


def title_for(rel: str, meta: dict, body: str) -> str:
    if meta.get("title"):
        return str(meta["title"])
    for line in body.splitlines():
        if line.strip().startswith("# "):
            return line.strip()[2:].strip()
    return _title_from_stem(Path(rel).stem)


def _today() -> str:
    return dt.date.today().isoformat()


def _stamp(content: str, title: str | None, tags: list[str] | None) -> str:
    """Return *content* with frontmatter (created/updated/tags) and a heading.

    Respects frontmatter the model already wrote (merging in tags/updated) and
    only adds an ``# H1`` when a title is supplied and the body lacks one.
    """
    meta, body = parse_frontmatter(content or "")
    if title and "title" not in meta:
        meta["title"] = title
    existing = meta.get("tags") or []
    if isinstance(existing, str):
        existing = [existing]
    merged = _dedupe([str(t).lstrip("#") for t in existing]
                     + [t.lstrip("#") for t in (tags or [])])
    if merged:
        meta["tags"] = merged
    now = _today()
    meta.setdefault("created", now)
    meta["updated"] = now
    heading = f"# {title}\n\n" if (title and not _has_heading(body)) else ""
    return f"{build_frontmatter(meta)}{heading}{body.strip()}\n"


# ── Index glue ───────────────────────────────────────────────────────────────
def _entry_for(p: Path) -> tuple[str, str, str, str, float] | None:
    """Build an index row ``(path,title,tags,body,mtime)`` for a markdown file."""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s for indexing: %s", p.name, exc)
        return None
    meta, body = parse_frontmatter(raw)
    rel = _rel(p)
    return (
        rel,
        title_for(rel, meta, body),
        " ".join(extract_tags(meta, body)),
        body,
        p.stat().st_mtime,
    )


def _reindex_path(p: Path) -> None:
    """Best-effort incremental index update after a write (keeps search current)."""
    try:
        from app.vault_index import get_index
        entry = _entry_for(p)
        if entry:
            get_index().upsert(*entry)
    except Exception as exc:  # noqa: BLE001
        log.debug("incremental reindex failed for %s: %s", p, exc)


def iter_markdown(root: Path | None = None):
    """Yield every ``.md`` file in the vault, skipping dot-folders/dot-files."""
    base = root or vault_root()
    for p in base.rglob(f"*{NOTE_EXT}"):
        if any(part.startswith(".") for part in p.relative_to(base).parts):
            continue
        if p.is_file():
            yield p


def reindex() -> int:
    """Full reconcile of the search index against the vault on disk."""
    from app.vault_index import get_index
    entries = [e for e in (_entry_for(p) for p in iter_markdown()) if e]
    get_index().sync(entries)
    return len(entries)


# ── Reads ────────────────────────────────────────────────────────────────────
def read_note(path: str) -> NoteRead:
    """Read a note: parsed frontmatter, body, outgoing links, and backlinks."""
    p = _safe_path(path)
    if not p.exists():
        raise VaultError(f"note '{_rel(p)}' not found.")
    raw = p.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(raw)
    rel = _rel(p)
    title = title_for(rel, meta, body)
    return NoteRead(
        path=rel, title=title, meta=meta, body=body,
        links=extract_wikilinks(body), backlinks=_backlinks(p, title),
    )


def _backlinks(p: Path, title: str) -> list[str]:
    """Notes linking to this one, via the index (by filename stem and title)."""
    try:
        from app.vault_index import get_index
        idx = get_index()
        rel = _rel(p)
        names = _dedupe([Path(rel).stem, title, rel[:-len(NOTE_EXT)]])
        found: list[str] = []
        for n in names:
            found += idx.linking_to(n)
        return _dedupe([f for f in found if f != rel])
    except Exception as exc:  # noqa: BLE001
        log.debug("backlink lookup failed for %s: %s", p, exc)
        return []


def search(query: str, tag: str | None = None, folder: str | None = None, limit: int = 5):
    """Relevance-ranked vault search (delegates to the FTS5 index)."""
    from app.vault_index import get_index
    return get_index().search(query, tag=tag, folder=folder, limit=limit)


def list_notes(folder: str | None = None, limit: int = 200) -> list[str]:
    """Vault-relative paths of notes, optionally scoped to *folder*, sorted."""
    root = vault_root()
    base = root
    if folder:
        base = (root / folder.strip("/")).resolve()
        if not base.is_relative_to(root) or not base.exists():
            return []
    paths = sorted(_rel(p) for p in iter_markdown(base))
    return paths[:limit]


# ── Writes ───────────────────────────────────────────────────────────────────
def write_note(
    path: str, content: str, title: str | None = None,
    tags: list[str] | None = None, overwrite: bool = True,
) -> str:
    """Create or overwrite a note (frontmatter stamped, ``# H1`` added if titled).

    With ``overwrite=False`` an existing path is given a numeric suffix instead
    of being replaced — used when creating a note from a title so JARVIS never
    silently clobbers an existing note.
    """
    p = _safe_path(path)
    existed = p.exists()
    if existed and not overwrite:
        p = _noncolliding(p)
        existed = False
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(_stamp(content, title, tags), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("could not write note %s: %s", p, exc)
        raise VaultError(f"could not save note: {exc}") from exc
    _reindex_path(p)
    rel = _rel(p)
    log.info("%s vault note %s", "updated" if existed else "created", rel)
    return f"{'Updated' if existed else 'Saved'} note {rel}."


def append_note(path: str, content: str) -> str:
    """Append to a note (creating it with frontmatter if it doesn't exist yet).

    Ideal for journals/logs (daily notes, running session capture) where adding
    to a note is safer than rewriting it.
    """
    p = _safe_path(path)
    addition = (content or "").strip()
    if not addition:
        raise VaultError("append_note needs non-empty content.")
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_stamp(addition, _title_from_stem(p.stem), None), encoding="utf-8")
        _reindex_path(p)
        log.info("created vault note %s (via append)", _rel(p))
        return f"Created note {_rel(p)}."
    existing = p.read_text(encoding="utf-8", errors="replace").rstrip()
    p.write_text(f"{existing}\n\n{addition}\n", encoding="utf-8")
    _reindex_path(p)
    log.info("appended to vault note %s", _rel(p))
    return f"Appended to note {_rel(p)}."


# ── Scaffold + migration ─────────────────────────────────────────────────────
_INDEX_TEMPLATE = (
    "---\ntitle: Index\ntags: [moc]\n---\n\n"
    "# Index — Map of Content\n\n"
    "JARVIS's knowledge vault. Notes are organized into folders and linked with "
    "`[[wikilinks]]`.\n\n"
    "- [[Sessions]] — recaps of past conversations\n"
    "- [[Daily]] — daily logs\n"
    "- [[People]] — notes about people\n"
    "- [[Projects]] — project notes\n"
    "- [[Topics]] — topic/reference notes\n"
    "- [[Memory]] — durable facts JARVIS remembers\n"
)


def ensure_scaffold() -> None:
    """Seed the default folders + an ``index.md`` Map of Content in a new vault."""
    root = vault_root()
    for f in DEFAULT_FOLDERS:
        (root / f).mkdir(parents=True, exist_ok=True)
    index = root / "index.md"
    if not index.exists():
        index.write_text(_INDEX_TEMPLATE, encoding="utf-8")


def _copy_into(src: Path, dest: Path) -> bool:
    """Copy a file's text to *dest* (non-colliding). Returns True on success."""
    try:
        dest = _noncolliding(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("migration copy failed (%s → %s): %s", src.name, dest, exc)
        return False


def _legacy_note_targets(notes_dir: Path):
    """Yield ``(src, dest_relative)`` for each legacy ``notes/`` markdown file.

    Shared by the live migration and its dry-run preview so the two can never
    drift. Root-level ``session_*.md`` summaries land in ``Sessions/``; files in a
    ``notes/<category>/`` subfolder go to ``Imported/<category>/``; anything else
    at the notes root goes to ``Imported/``.
    """
    notes_dir = Path(notes_dir)
    if not notes_dir.exists():
        return
    for md in sorted(notes_dir.rglob(f"*{NOTE_EXT}")):
        relparts = md.relative_to(notes_dir).parts
        if md.parent == notes_dir and md.name.startswith("session_"):
            yield md, f"Sessions/{md.name}"
        elif len(relparts) > 1:  # notes/<category>/<file>
            yield md, f"Imported/{relparts[0]}/{md.name}"
        else:
            yield md, f"Imported/{md.name}"


def _count_legacy_facts(db_path: Path) -> int:
    """Number of durable facts in a legacy memory.db (0 if absent/unreadable)."""
    if not Path(db_path).exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memories WHERE kind='fact'"
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not count legacy facts in %s: %s", db_path, exc)
        return 0


def migration_plan(notes_dir: Path | None = None, memory_db: Path | None = None) -> dict:
    """Preview what :func:`migrate_legacy` would import — **writes nothing**.

    Powers the token-free ``vault_cli migrate --dry-run`` "look before you leap"
    check. Does not create the vault folder or the migration marker; it only
    inspects the legacy sources. Returns a dict with the resolved vault root,
    whether a migration already ran, the planned note/session copies as
    ``(src, dest)`` pairs, and the durable-fact count.
    """
    vault = CONFIG.obsidian_vault
    root = vault.expanduser().resolve() if vault else None
    notes: list[tuple[str, str]] = []
    sessions: list[tuple[str, str]] = []
    for src, dest_rel in _legacy_note_targets(notes_dir or NOTES_DIR):
        bucket = sessions if dest_rel.startswith("Sessions/") else notes
        bucket.append((str(src), dest_rel))
    return {
        "vault_root": str(root) if root else None,
        "already_migrated": bool(root and (root / _MIGRATION_MARKER).exists()),
        "notes": notes,
        "sessions": sessions,
        "fact_count": _count_legacy_facts(memory_db or MEMORY_DB_PATH),
    }


def migrate_legacy(notes_dir: Path | None = None, memory_db: Path | None = None) -> int:
    """One-time, idempotent, non-destructive import of legacy notes + facts.

    Copies (never moves) ``notes/<category>/*.md`` → ``Imported/<category>/`` and
    ``notes/session_*.md`` → ``Sessions/``, and migrates durable *facts* from
    ``memory.db`` into ``Memory/Facts.md``. Legacy session summaries are skipped
    here because they're already captured as the copied ``session_*.md`` files —
    importing the DB rows too would duplicate them. A marker file in the vault
    makes reruns no-ops; the originals are left untouched as a safety net.
    """
    root = vault_root()
    marker = root / _MIGRATION_MARKER
    if marker.exists():
        return 0
    ensure_scaffold()

    migrated = 0
    for src, dest_rel in _legacy_note_targets(notes_dir or NOTES_DIR):
        if _copy_into(src, root / dest_rel):
            migrated += 1

    migrated += _migrate_facts(memory_db or MEMORY_DB_PATH, root)

    marker.write_text(dt.datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    log.info("vault migration imported %d legacy item(s)", migrated)
    return migrated


def _migrate_facts(db_path: Path, root: Path) -> int:
    """Append durable facts from a legacy memory.db into Memory/Facts.md."""
    if not Path(db_path).exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute(
                "SELECT content FROM memories WHERE kind='fact' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read legacy facts from %s: %s", db_path, exc)
        return 0
    facts = [r[0] for r in rows if r and r[0]]
    if not facts:
        return 0
    bullets = "\n".join(f"- {f}" for f in facts)
    append_note("Memory/Facts.md", f"## Imported facts\n{bullets}")
    return len(facts)


# ── Watcher ──────────────────────────────────────────────────────────────────
class ObsidianWatcher:
    """Background watcher that keeps the search index in step with the vault.

    Same ``watchdog`` pattern as the retired ``NotesWatcher`` — safe to construct
    when ``watchdog`` isn't installed (it no-ops) — but on a markdown change it
    upserts/removes the affected note in the FTS5 index instead of just logging.
    """

    def __init__(self) -> None:
        self._observer = None

    def start(self) -> None:
        if not CONFIG.obsidian_available:
            return
        try:
            root = vault_root()
        except VaultError as exc:
            log.warning("cannot watch vault: %s", exc)
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            log.info("watchdog not installed; vault index updates on startup only")
            return

        def update(path_str: str, removed: bool) -> None:
            path = Path(path_str)
            if path.suffix.lower() != NOTE_EXT:
                return
            try:
                from app.vault_index import get_index
                if removed:
                    get_index().remove(_rel(path))
                else:
                    _reindex_path(path)
            except Exception as exc:  # noqa: BLE001
                log.debug("vault watch update failed for %s: %s", path, exc)

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    update(event.src_path, removed=False)

            def on_modified(self, event):
                if not event.is_directory:
                    update(event.src_path, removed=False)

            def on_deleted(self, event):
                if not event.is_directory:
                    update(event.src_path, removed=True)

            def on_moved(self, event):
                if not event.is_directory:
                    update(event.src_path, removed=True)
                    update(event.dest_path, removed=False)

        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), str(root), recursive=True)
            self._observer.start()
            log.info("watching Obsidian vault (recursive): %s", root)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start vault watcher: %s", exc)
            self._observer = None

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
