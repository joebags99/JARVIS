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
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app import vault_taxonomy, vault_templates
from app.config import CONFIG, NOTES_DIR, ROOT_DIR
from app.logging_setup import get_logger

log = get_logger("obsidian")

NOTE_EXT = ".md"
# Folder organization is defined by the config-driven taxonomy (app.vault_taxonomy):
#   * DEFAULT_FOLDERS — seeded in a fresh vault (JARVIS still adapts to an existing one)
#   * INDEX_SKIP_FOLDERS — excluded from the search index + graph (e.g. Archive/)
#   * ENTITY_FOLDERS — notes here are canonical, alias-de-duplicated identities
# Computed at import; add a category by editing vault_config.json, not this file.
DEFAULT_FOLDERS = vault_taxonomy.folders()
INDEX_SKIP_FOLDERS = vault_taxonomy.skip_folders()
ENTITY_FOLDERS = vault_taxonomy.entity_folders()
MEMORY_DB_PATH = ROOT_DIR / "memory.db"
_MIGRATION_MARKER = ".jarvis_migrated"

# Inline ``#tag`` (allows nested tags like #project/alpha), and ``[[wikilink]]``
# with an optional ``|alias`` and ``#heading``/``^block`` suffix.
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_/-]*)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
# Plain ``[[target]]`` only (no ``|alias`` / ``#heading``) — the safe surface to
# rewrite when canonicalizing person links, so display text is never clobbered.
_PLAIN_WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)\]\]")


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
    """Render a frontmatter block. ``title/type/created/updated/tags`` come first."""
    ordered = ["title", "type", "created", "updated", "tags"]
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


def extract_aliases(meta: dict) -> list[str]:
    """A note's frontmatter ``aliases`` as a clean list (handles str or list)."""
    a = meta.get("aliases") or []
    if isinstance(a, str):
        a = [x.strip() for x in a.replace(",", " ").split()]
    return _dedupe([str(x).lstrip("#").strip() for x in a if str(x).strip()])


_OPEN_ITEM_HEADERS = {"action items", "open questions"}


def extract_open_items(body: str) -> list[str]:
    """Bullet lines under a ``## Action Items``/``## Open Questions`` heading.

    Matches the "session" note template (``app/vault_templates.py``). Stops
    collecting at the next ``##`` heading; an empty/placeholder section (the
    unfilled template stub) yields nothing.
    """
    items: list[str] = []
    in_section = False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip().lower() in _OPEN_ITEM_HEADERS
            continue
        if in_section and stripped.startswith(("- ", "* ")):
            text = stripped[2:].strip()
            if text:
                items.append(text)
    return items


# ── People roster / name canonicalization ────────────────────────────────────
# Source of truth = the People/ notes' titles + ``aliases`` frontmatter. The
# roster maps every lowercased alias (and the canonical name itself) to the one
# canonical spelling, so "Joe"/"Joe K" both resolve to "Joe Konkle". Cached and
# keyed by vault root so a different vault (e.g. in tests) never sees stale data.
_roster_cache: tuple[str, dict[str, str]] | None = None


def reload_roster() -> dict[str, str]:
    """Rebuild the alias→canonical map from every entity folder. Best-effort."""
    global _roster_cache
    mapping: dict[str, str] = {}
    try:
        root = vault_root()
    except VaultError:
        _roster_cache = None
        return {}
    for folder in ENTITY_FOLDERS:
        base = root / folder
        if not base.exists():
            continue
        for p in sorted(base.glob(f"*{NOTE_EXT}")):
            try:
                meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001
                log.debug("roster: could not read %s: %s", p.name, exc)
                continue
            canonical = title_for(_rel(p), meta, body) or _title_from_stem(p.stem)
            mapping[canonical.lower()] = canonical
            mapping[p.stem.lower()] = canonical
            for alias in extract_aliases(meta):
                mapping[alias.lower()] = canonical
    _roster_cache = (str(root), mapping)
    return mapping


def get_roster() -> dict[str, str]:
    """Cached alias→canonical map, rebuilt when the vault root changes."""
    try:
        root = str(vault_root())
    except VaultError:
        return {}
    if _roster_cache and _roster_cache[0] == root:
        return _roster_cache[1]
    return reload_roster()


def canonical_entities(folder: str) -> dict[str, list[str]]:
    """Canonical name → aliases for notes in *folder* (e.g. People, Projects).

    Only entities that actually have aliases are returned — they're the ones worth
    naming in the prompt/report.
    """
    out: dict[str, list[str]] = {}
    try:
        base = vault_root() / folder
    except VaultError:
        return out
    if not base.exists():
        return out
    for p in sorted(base.glob(f"*{NOTE_EXT}")):
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        aliases = extract_aliases(meta)
        if aliases:
            out[title_for(_rel(p), meta, body) or _title_from_stem(p.stem)] = aliases
    return out


def canonical_people() -> dict[str, list[str]]:
    """Back-compat: canonical person → aliases (see :func:`canonical_entities`)."""
    return canonical_entities("People")


def canonicalize_links(text: str, roster: dict[str, str] | None = None) -> str:
    """Rewrite plain ``[[alias]]`` links to ``[[Canonical]]`` using the roster.

    Only plain links are touched (never ``[[x|display]]`` or ``[[x#heading]]``),
    and only when the target is a known alias, so this is a safe, bounded rewrite
    — no free-text substitution that could mangle names inside prose.
    """
    roster = roster if roster is not None else get_roster()
    if not roster or not text:
        return text

    def _sub(m: re.Match) -> str:
        target = m.group(1).strip()
        canon = roster.get(target.lower())
        return f"[[{canon}]]" if canon and canon != target else m.group(0)

    return _PLAIN_WIKILINK_RE.sub(_sub, text)


def linkify_entities(text: str, roster: dict[str, str] | None = None) -> str:
    """Wrap bare mentions of known people/projects in ``[[wikilinks]]``.

    Scans *text* for any roster name (a canonical name or an alias) and links it to
    the **canonical** note, preserving the original wording as a display alias when
    it differs (``[[Joe Konkle|Joe]]``). Existing ``[[wikilinks]]`` are left intact,
    longer names match first ("Joe Konkle" before "Joe"), and slug-style keys are
    ignored so only real names in prose are linked. Used to wire session recaps
    into the graph so each recap connects to the people/projects it mentions.
    """
    roster = roster if roster is not None else get_roster()
    if not roster or not text:
        return text
    names = sorted((k for k in roster if k and "_" not in k), key=len, reverse=True)
    if not names:
        return text
    pattern = re.compile(
        r"(?<![\w\[])(" + "|".join(re.escape(n) for n in names) + r")(?![\w\]])",
        re.IGNORECASE,
    )

    def _sub(m: re.Match) -> str:
        matched = m.group(1)
        canon = roster.get(matched.lower())
        if not canon:
            return matched
        return f"[[{canon}]]" if matched == canon else f"[[{canon}|{matched}]]"

    # Linkify only the segments *outside* existing [[wikilinks]].
    parts = re.split(r"(\[\[[^\]]*\]\])", text)
    return "".join(
        seg if seg.startswith("[[") else pattern.sub(_sub, seg) for seg in parts
    )


def _split_frontmatter_raw(text: str) -> tuple[str, str]:
    """Split off the frontmatter block *verbatim* (incl. ``---`` fences) from the body.

    Unlike :func:`parse_frontmatter` this preserves the original frontmatter text,
    so a body-only rewrite (e.g. retro-linkifying) never reformats the metadata.
    """
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[: i + 1]), "".join(lines[i + 1:])
    return "", text


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


def _type_for_path(rel: str) -> str:
    """The taxonomy ``type`` for a note, from its top-level folder."""
    return vault_taxonomy.type_for_folder(rel.split("/", 1)[0])


def _stamp(content: str, title: str | None, tags: list[str] | None,
           note_type: str | None = None) -> str:
    """Return *content* with frontmatter (type/created/updated/tags) and a heading.

    Respects frontmatter the model already wrote (merging in tags/updated/type)
    and only adds an ``# H1`` when a title is supplied and the body lacks one.
    """
    meta, body = parse_frontmatter(content or "")
    if title and "title" not in meta:
        meta["title"] = title
    if note_type and "type" not in meta:
        meta["type"] = note_type
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
    """Yield every ``.md`` file in the vault, skipping dot-folders/dot-files.

    When walking from the vault root, top-level ``Archive/`` is also skipped so
    superseded originals (parked there by the cleanup pass) never re-enter the
    search index. Walking an explicit folder (``list_notes('Archive')``) still
    lists them, so they remain browsable on request.
    """
    base = root or vault_root()
    for p in base.rglob(f"*{NOTE_EXT}"):
        parts = p.relative_to(base).parts
        if any(part.startswith(".") for part in parts):
            continue
        if parts and parts[0] in INDEX_SKIP_FOLDERS:
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
        links=extract_wikilinks(body), backlinks=_backlinks(p, title, extract_aliases(meta)),
    )


def _backlinks(p: Path, title: str, aliases: list[str] | None = None) -> list[str]:
    """Notes linking to this one, by filename stem, title, and any aliases.

    Including the note's own ``aliases`` means a ``[[Joe K]]`` reference still
    counts as a backlink to ``People/Joe Konkle.md`` even before links are
    canonicalized — so consolidated identities stay connected.
    """
    try:
        from app.vault_index import get_index
        idx = get_index()
        rel = _rel(p)
        names = _dedupe([Path(rel).stem, title, rel[:-len(NOTE_EXT)], *(aliases or [])])
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
def _invalidate_roster_if_entity(p: Path) -> None:
    """Drop the cached roster when an entity-folder note changed, so it's rebuilt."""
    global _roster_cache
    try:
        if _rel(p).split("/", 1)[0] in ENTITY_FOLDERS:
            _roster_cache = None
    except Exception:  # noqa: BLE001
        _roster_cache = None


def _route(rel: str, title: str | None = None) -> str:
    """Deterministically correct a note's folder *before* it is written.

    The single guard that keeps placement consistent no matter how a note is
    created — a tool call, a paste, a fact write: a meeting/session note can
    never land in an entity folder (People/Companies/Projects); it is redirected
    to ``Sessions/``. Only direct children of an entity folder are rerouted
    (a note nested in a project subfolder is intentional and left alone), and
    everything that is already correctly placed is returned unchanged.
    """
    parts = rel.split("/")
    if len(parts) != 2:
        return rel
    folder, name = parts
    if folder in ENTITY_FOLDERS and (
        looks_like_meeting(Path(name).stem) or looks_like_meeting(title or "")
    ):
        return f"Sessions/{name}"
    return rel


def write_note(
    path: str, content: str, title: str | None = None,
    tags: list[str] | None = None, overwrite: bool = True, canonicalize: bool = True,
) -> str:
    """Create or overwrite a note (frontmatter stamped, ``# H1`` added if titled).

    Placement is corrected first (:func:`_route` — a meeting never lands in an
    entity folder), and a fresh, body-less note is seeded with its type's
    template (:mod:`app.vault_templates`) so every note of a kind shares one
    structure. With ``overwrite=False`` an existing path is given a numeric
    suffix instead of being replaced — used when creating a note from a title so
    JARVIS never silently clobbers an existing note. Plain person ``[[links]]``
    are canonicalized against the roster on the way in (``canonicalize=False`` to
    skip) so the same person never splits across name variants.
    """
    p = _safe_path(path)
    routed = _route(_rel(p), title)
    if routed != _rel(p):
        log.info("routed note %s -> %s (meeting/session kept out of entity folder)",
                 _rel(p), routed)
        p = _safe_path(routed)
    existed = p.exists()
    if existed and not overwrite:
        p = _noncolliding(p)
        existed = False
    p.parent.mkdir(parents=True, exist_ok=True)
    if canonicalize:
        content = canonicalize_links(content or "")
    note_type = _type_for_path(_rel(p))
    body = content or ""
    if not body.strip() and vault_templates.has_template(note_type):
        # A brand-new, empty note gets its type's section skeleton.
        body = vault_templates.scaffold(note_type, title=title)
    try:
        p.write_text(_stamp(body, title, tags, note_type), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("could not write note %s: %s", p, exc)
        raise VaultError(f"could not save note: {exc}") from exc
    _reindex_path(p)
    _invalidate_roster_if_entity(p)
    rel = _rel(p)
    log.info("%s vault note %s", "updated" if existed else "created", rel)
    return f"{'Updated' if existed else 'Saved'} note {rel}."


def append_note(path: str, content: str) -> str:
    """Append to a note (creating it with frontmatter if it doesn't exist yet).

    Ideal for journals/logs (daily notes, running session capture) where adding
    to a note is safer than rewriting it.
    """
    p = _safe_path(path)
    addition = canonicalize_links((content or "").strip())
    if not addition:
        raise VaultError("append_note needs non-empty content.")
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            _stamp(addition, _title_from_stem(p.stem), None, _type_for_path(_rel(p))),
            encoding="utf-8",
        )
        _reindex_path(p)
        _invalidate_roster_if_entity(p)
        log.info("created vault note %s (via append)", _rel(p))
        return f"Created note {_rel(p)}."
    existing = p.read_text(encoding="utf-8", errors="replace").rstrip()
    p.write_text(f"{existing}\n\n{addition}\n", encoding="utf-8")
    _reindex_path(p)
    _invalidate_roster_if_entity(p)
    log.info("appended to vault note %s", _rel(p))
    return f"Appended to note {_rel(p)}."


def move_to_archive(path: str) -> str:
    """Move a note under ``Archive/`` (non-colliding) and drop it from the index.

    Used by the cleanup pass to retire an original once its tidied replacement is
    written: the file is preserved (reversible) but ``Archive/`` is excluded from
    search, so it no longer competes with the clean version. The original's
    vault-relative path is mirrored under ``Archive/`` (e.g. ``Imported/x.md`` →
    ``Archive/Imported/x.md``). Returns the new vault-relative path.
    """
    src = _safe_path(path)
    if not src.exists():
        raise VaultError(f"note '{path}' not found.")
    rel = _rel(src)
    dest = _noncolliding(vault_root() / "Archive" / rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
    try:
        from app.vault_index import get_index
        get_index().remove(rel)
    except Exception as exc:  # noqa: BLE001
        log.debug("index remove after archive failed for %s: %s", rel, exc)
    if rel.split("/", 1)[0] == "People":
        global _roster_cache
        _roster_cache = None
    log.info("archived vault note %s -> %s", rel, _rel(dest))
    return _rel(dest)


# ── Identity merge primitives (used by the people-consolidation pass) ─────────
def find_entity_note(name: str, folder: str = "People") -> str | None:
    """Locate an existing note for *name* in *folder*, matching on slug.

    Slug comparison makes the lookup case- and punctuation-insensitive, so a
    migrated ``People/Joe.md`` and a generated ``People/joe.md`` both resolve.
    Returns the note's vault-relative path, or None.
    """
    try:
        base = vault_root() / folder
    except VaultError:
        return None
    if not base.exists():
        return None
    target = _slugify(name)
    for p in sorted(base.glob(f"*{NOTE_EXT}")):
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if _slugify(p.stem) == target or _slugify(title_for(_rel(p), meta, body)) == target:
            return _rel(p)
    return None


def find_person_note(name: str) -> str | None:
    """Back-compat wrapper for :func:`find_entity_note` in People/."""
    return find_entity_note(name, "People")


def find_existing_entity(name: str) -> tuple[str, str] | None:
    """Find *name*'s entity note in **any** entity folder. ``(folder, rel)`` or None.

    Searches People → Companies → Projects (``ENTITY_FOLDERS`` order) and returns
    the first hit, so a writer can reuse an entity wherever it already lives instead
    of spawning a cross-folder duplicate (a person filed in People stays one note
    even when a fact about them is mis-tagged as a project).
    """
    for folder in ENTITY_FOLDERS:
        rel = find_entity_note(name, folder)
        if rel:
            return folder, rel
    return None


def set_aliases(canonical: str, aliases: list[str], folder: str = "People") -> str:
    """Create/update the canonical note in *folder* with a merged ``aliases`` list.

    Preserves any existing body and aliases; the canonical name is never listed
    as its own alias. Reuses an existing note for the name if one is found (rather
    than creating a slug duplicate). Returns the note's vault-relative path.
    """
    rel = find_entity_note(canonical, folder) or path_for_title(canonical, folder)
    p = _safe_path(rel)
    if p.exists():
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    else:
        meta, body = {}, ""
    meta.setdefault("title", canonical)
    meta.setdefault("type", vault_taxonomy.type_for_folder(folder))
    merged = _dedupe(extract_aliases(meta) + [str(a).strip() for a in aliases])
    meta["aliases"] = [a for a in merged if a and a.lower() != canonical.lower()]
    now = _today()
    meta.setdefault("created", now)
    meta["updated"] = now
    heading = "" if _has_heading(body) else f"# {canonical}\n\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{build_frontmatter(meta)}{heading}{body.strip()}\n", encoding="utf-8")
    _reindex_path(p)
    global _roster_cache
    _roster_cache = None
    log.info("set aliases on %s (%d)", _rel(p), len(meta["aliases"]))
    return _rel(p)


def merge_note_into(src_rel: str, canonical: str, folder: str = "People") -> bool:
    """Fold a duplicate entity note's body into the canonical note, then archive it.

    No-op (returns False) if *src_rel* is missing or is already the canonical note.
    """
    canonical_rel = find_entity_note(canonical, folder) or path_for_title(canonical, folder)
    src = _safe_path(src_rel)
    if not src.exists() or _rel(src) == canonical_rel:
        return False
    _meta, body = parse_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
    if body.strip():
        append_note(canonical_rel, f"## Merged from {Path(src_rel).stem}\n\n{body.strip()}")
    move_to_archive(_rel(src))
    return True


def recanonicalize_vault() -> int:
    """Rewrite plain person ``[[links]]`` to canonical across every note.

    Returns the number of notes changed. Used after aliases are set so existing
    references (``[[Joe K]]`` …) collapse onto the canonical name everywhere.
    """
    roster = reload_roster()
    if not roster:
        return 0
    changed = 0
    for p in iter_markdown():
        raw = p.read_text(encoding="utf-8", errors="replace")
        new = canonicalize_links(raw, roster)
        if new != raw:
            p.write_text(new, encoding="utf-8")
            _reindex_path(p)
            changed += 1
    return changed


# ── Memory: routing extracted facts to the entity they're about ───────────────
_GENERIC_SUBJECTS = {"user", "me", "i", "myself", "the user", ""}


def record_session_facts(facts: list[dict]) -> int:
    """File each extracted fact under the entity note it's about. Returns the count.

    Each fact is ``{"fact","subject","kind"}`` (see ``claude_client.extract_facts``).
    Facts about a person/company/project are appended — dated, grouped — to that
    entity's note; the target is resolved through the roster (alias→canonical) then
    an **any-folder** slug match (:func:`find_existing_entity`) so a typo, nickname,
    *or mis-tagged kind* reuses the existing note instead of spawning a cross-folder
    duplicate. A new note is created (in the folder its ``kind`` implies) only when
    the entity is genuinely unknown. Facts about the user (``kind == "self"``) or
    with no subject go to ``Memory/Facts.md``.
    """
    if not facts:
        return 0
    user = (CONFIG.user_name or "").strip().lower()
    groups: dict[str, list[str]] = {}          # canonical → facts
    default_folder: dict[str, str] = {}        # canonical → folder for a *new* entity
    general: list[str] = []
    for f in facts:
        text = str((f or {}).get("fact") or "").strip()
        if not text:
            continue
        subject = str(f.get("subject") or "").strip()
        kind = str(f.get("kind") or "").strip().lower()
        if kind == "self" or subject.lower() in _GENERIC_SUBJECTS or subject.lower() == user:
            general.append(text)
            continue
        canonical = get_roster().get(subject.lower(), subject)
        groups.setdefault(canonical, []).append(text)
        default_folder.setdefault(
            canonical, {"project": "Projects", "company": "Companies"}.get(kind, "People")
        )

    today = _today()
    count = 0
    for canonical, texts in groups.items():
        # Reuse the entity wherever it already lives so a person filed in People
        # stays ONE note even if this fact was mis-tagged 'project' — the bug that
        # split every person into a People/ + Projects/ pair. Only a genuinely new
        # entity is created, in the folder its kind implies.
        existing = find_existing_entity(canonical)
        rel = existing[1] if existing else path_for_title(canonical, default_folder[canonical])
        block = f"## Facts ({today})\n" + "\n".join(f"- {t}" for t in texts)
        if not _safe_path(rel).exists():
            # Create with the first facts block as content (preserves casing, e.g.
            # "CCC Legacy" not "Ccc Legacy") rather than an empty stub + append.
            write_note(rel, block, title=canonical, overwrite=False, canonicalize=False)
        else:
            append_note(rel, block)
        count += len(texts)
    if general:
        append_note("Memory/Facts.md", f"## {today}\n" + "\n".join(f"- {t}" for t in general))
        count += len(general)
    log.info("recorded %d session fact(s) across %d entity note(s)", count, len(groups))
    return count


# ── Organization: types, graph coloring, maps, health ────────────────────────
def _write_if_changed(path: Path, content: str) -> bool:
    """Write *content* to *path* only if it differs — avoids needless churn/sync.

    Auto-organization runs on every startup, so rewriting byte-identical maps each
    launch would thrash mtimes (and any Obsidian/Git sync). Returns True if written.
    """
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
    except Exception:  # noqa: BLE001
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def capture_idea(text: str) -> str:
    """Quick-capture an idea into ``Ideas/Inbox.md`` (timestamped). Returns status."""
    text = (text or "").strip()
    if not text:
        raise VaultError("capture_idea needs text.")
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    append_note("Ideas/Inbox.md", f"- {stamp} — {text}")
    return f"Captured to Ideas/Inbox.md: {text}"


def list_open_callbacks() -> list[tuple[str, dict, list[str]]]:
    """Every ``Sessions/`` note with at least one open Action Item/Open Question.

    Raw and unfiltered — includes notes already nudged and notes too fresh to
    nudge about. ``app.proactive.callback_due`` decides which are actually due,
    so this stays a plain "what's in the vault" read with no scheduling policy
    baked in, matching how ``fetch_events`` returns every calendar event and
    lets a pure decision function pick which are "due soon."
    """
    out: list[tuple[str, dict, list[str]]] = []
    for rel in list_notes(folder="Sessions"):
        try:
            raw = (vault_root() / rel).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        meta, body = parse_frontmatter(raw)
        items = extract_open_items(body)
        if items:
            out.append((rel, meta, items))
    return out


def mark_callback_nudged(rel: str) -> None:
    """Stamp a note so its open items are never nudged about again.

    Frontmatter-only rewrite (same pattern as :func:`backfill_types`) — the
    dedup state lives in the vault itself rather than a new side file, so it
    survives a JARVIS restart without any extra persisted state.
    """
    p = _safe_path(rel)
    if not p.exists():
        return
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    meta["callback_nudged"] = _today()
    p.write_text(f"{build_frontmatter(meta)}{body.strip()}\n", encoding="utf-8")
    _reindex_path(p)
    log.info("marked vault callback nudged: %s", rel)


_CALLBACKS_REL = "Maps/Callbacks.md"


def render_callbacks(items: list[tuple[str, dict, list[str]]]) -> str:
    """Render every open-item note as a durable, browsable checklist.

    A tray balloon or spoken nudge disappears the moment it's dismissed; this
    note is the persistent record — every ``Sessions/`` note with unchecked
    Action Items/Open Questions, oldest-touched first so the stalest work
    surfaces at the top. Whether a note has already been nudged is shown but
    doesn't filter it out, so this is a complete "what's still open" view,
    not just a log of past nudges.
    """
    lines = [
        "---", "title: Callbacks", "type: map", "tags: [moc, callbacks]", "---", "",
        "# 🔁 Open Callbacks", "",
        "_Auto-generated — every Sessions/ note with unchecked Action Items or "
        "Open Questions, refreshed at startup and roughly every minute while "
        "JARVIS runs. Resolve or clear the section in the source note and it "
        "drops off here on the next refresh._", "",
    ]
    if not items:
        lines.append("_Nothing open right now._")
        return "\n".join(lines) + "\n"

    def _updated_date(entry: tuple[str, dict, list[str]]) -> dt.date:
        raw = entry[1].get("updated") or entry[1].get("created") or ""
        try:
            return dt.date.fromisoformat(str(raw))
        except ValueError:
            return dt.date.min

    for rel, meta, open_items in sorted(items, key=_updated_date):
        title = meta.get("title") or Path(rel).stem
        updated = meta.get("updated") or meta.get("created") or "?"
        nudged = meta.get("callback_nudged")
        status = f"nudged {nudged}" if nudged else "not yet nudged"
        lines.append(f"## [[{rel[:-len(NOTE_EXT)]}|{title}]]")
        lines.append(f"_Last touched {updated} — {status}._")
        lines.append("")
        lines.extend(f"- {item}" for item in open_items)
        lines.append("")
    return "\n".join(lines) + "\n"


def write_callbacks() -> str:
    """Refresh ``Maps/Callbacks.md`` from the vault's current open items.

    Cheap to call often (a plain scan of ``Sessions/`` plus a no-op write if
    nothing changed) so it can run at startup and on every proactive tick,
    keeping the note current whether an item newly went stale or the user
    just checked one off directly in Obsidian. Returns the note's path.
    """
    items = list_open_callbacks()
    dest = vault_root() / _CALLBACKS_REL
    if _write_if_changed(dest, render_callbacks(items)):
        _reindex_path(dest)
        log.info("refreshed vault callbacks dashboard (%d note(s) with open items)", len(items))
    return str(dest)


def backfill_types() -> int:
    """Stamp a ``type:`` (from the taxonomy) onto any note that lacks one.

    Lets the graph color *existing* notes by kind. Returns the number updated.
    """
    changed = 0
    for p in iter_markdown():
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if meta.get("type"):
            continue
        meta["type"] = _type_for_path(_rel(p))
        p.write_text(f"{build_frontmatter(meta)}{body.strip()}\n", encoding="utf-8")
        _reindex_path(p)
        changed += 1
    return changed


def write_graph_config() -> str:
    """Write/refresh ``.obsidian/graph.json`` so the graph colors notes by folder.

    Merges into any existing graph settings (only ``colorGroups`` is replaced),
    so it never clobbers the user's other graph preferences. Returns the path.
    """
    root = vault_root()
    obs = root / ".obsidian"
    obs.mkdir(parents=True, exist_ok=True)
    gpath = obs / "graph.json"
    data: dict = {}
    if gpath.exists():
        try:
            data = json.loads(gpath.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("existing graph.json unreadable, rewriting: %s", exc)
            data = {}
    data["colorGroups"] = [
        {"query": f'path:"{folder}/"', "color": {"a": 1, "rgb": int(color.lstrip("#"), 16)}}
        for folder, color in vault_taxonomy.color_groups()
    ]
    data.setdefault("collapse-color-groups", False)
    if _write_if_changed(gpath, json.dumps(data, indent=2)):
        log.info("wrote graph color config (%d groups) to %s", len(data["colorGroups"]), gpath)
    return str(gpath)


def rebuild_mocs() -> int:
    """Regenerate hub *Maps of Content* — one ``Maps/<Folder>.md`` per populated folder.

    Each map links to every note in its folder, so in the graph it becomes a bright
    hub the cluster orbits. ``index.md`` is refreshed to link to every map. These
    notes are auto-generated (overwritten on each run). Returns the map count.
    """
    root = vault_root()
    skip = {"Maps", "Imported"} | INDEX_SKIP_FOLDERS
    maps: list[str] = []
    for folder in vault_taxonomy.folders():
        if folder in skip:
            continue
        rels = [r for r in list_notes(folder=folder) if not r.startswith(f"{folder}/.")]
        if not rels:
            continue
        icon = vault_taxonomy.icon_for_folder(folder)
        lines = [
            "---", "title: " + f"{folder} Map", "type: map", "tags: [moc]", "---", "",
            f"# {icon} {folder} Map", "",
            f"_Auto-generated hub linking every note in {folder}/._", "",
        ]
        for rel in sorted(rels):
            try:
                meta, body = parse_frontmatter((root / rel).read_text(encoding="utf-8", errors="replace"))
                title = title_for(rel, meta, body)
            except Exception:  # noqa: BLE001
                title = _title_from_stem(Path(rel).stem)
            lines.append(f"- [[{rel[:-len(NOTE_EXT)]}|{title}]]")
        dest = root / "Maps" / f"{folder}.md"
        if _write_if_changed(dest, "\n".join(lines) + "\n"):
            _reindex_path(dest)
        maps.append(folder)

    idx = [
        "---", "title: Index", "type: map", "tags: [moc]", "---", "",
        "# Index — Map of Content", "",
        "JARVIS's knowledge vault. Each map below is a hub linking every note in that area.", "",
    ]
    idx += [
        f"- {vault_taxonomy.icon_for_folder(folder)} [[Maps/{folder}|{folder}]]"
        for folder in maps
    ]
    if _write_if_changed(root / "index.md", "\n".join(idx) + "\n"):
        _reindex_path(root / "index.md")
    log.info("rebuilt %d map(s) of content", len(maps))
    return len(maps)


_DASHBOARD_REL = "Maps/Dashboard.md"


@dataclass
class VaultStats:
    """A snapshot of the vault's shape, for the ``stats``/dashboard report."""

    total: int
    by_type: list[tuple[str, int]]                 # (type, count), most common first
    most_linked: list[tuple[str, str, int]]         # (rel, title, inbound link count)
    recent: list[tuple[str, str, str]]               # (rel, title, YYYY-MM-DD)
    orphans: int
    dangling_links: int


def compute_stats(top: int = 8) -> VaultStats:
    """Compute vault-wide stats in one pass: type counts, link fan-in, recency.

    ``most_linked``/``orphans``/``dangling_links`` all resolve ``[[wikilinks]]``
    the same way as :func:`find_dangling_links` (title/filename-stem/aliases,
    best-effort) in one pass, rather than calling the separate detectors and
    re-scanning the vault twice more.

    The dashboard note itself (``_DASHBOARD_REL``) is excluded from the scan —
    otherwise a stat like "total notes" would count the dashboard describing it,
    and its own "recently active"/backlink-adding links would shift the very
    orphan/dangling counts it reports, so the file would never settle: each
    write would make the next write's numbers stale before the file was even
    closed.
    """
    entries: list[tuple[str, str, str, float, list[str]]] = []  # rel,title,type,mtime,links
    resolve: dict[str, str] = {}
    for p in iter_markdown():
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        rel = _rel(p)
        if rel == _DASHBOARD_REL:
            continue
        title = title_for(rel, meta, body)
        note_type = str(meta.get("type") or _type_for_path(rel))
        entries.append((rel, title, note_type, p.stat().st_mtime, extract_wikilinks(body)))
        for key in _dedupe([title, Path(rel).stem, rel[:-len(NOTE_EXT)], *extract_aliases(meta)]):
            resolve[key.lower()] = rel

    inbound: Counter[str] = Counter()
    titles: dict[str, str] = {}
    dangling_links = 0
    for rel, title, _type, _mtime, links in entries:
        titles[rel] = title
        for target in links:
            hit = resolve.get(target.strip().lower())
            if not hit:
                dangling_links += 1
            elif hit != rel:  # a self-link isn't a real backlink
                inbound[hit] += 1

    orphans = sum(1 for rel, _t, _ty, _m, links in entries if not links and not inbound.get(rel))
    by_type = Counter(t for _, _, t, _, _ in entries)
    most_linked = [
        (rel, titles[rel], n) for rel, n in sorted(inbound.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    ]
    recent = [
        (rel, title, dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"))
        for rel, title, _type, mtime, _links in sorted(entries, key=lambda e: e[3], reverse=True)[:top]
    ]
    return VaultStats(
        total=len(entries),
        by_type=by_type.most_common(),
        most_linked=most_linked,
        recent=recent,
        orphans=orphans,
        dangling_links=dangling_links,
    )


def render_dashboard(stats: VaultStats) -> str:
    """Render a :class:`VaultStats` snapshot as a Maps/Dashboard.md note body."""
    lines = [
        "---", "title: Dashboard", "type: map", "tags: [moc, dashboard]", "---", "",
        "# 📊 Vault Dashboard", "",
        "_Auto-generated snapshot — refreshed by `vault_cli stats` and on every "
        "organize pass._", "",
        f"**{stats.total}** note(s) across **{len(stats.by_type)}** type(s).", "",
    ]
    lines.append("## By type")
    for note_type, count in stats.by_type:
        lines.append(f"- {vault_taxonomy.icon_for_type(note_type)} {note_type}: {count}")
    lines.append("")

    lines.append("## Most-linked notes")
    if stats.most_linked:
        for i, (rel, title, count) in enumerate(stats.most_linked, start=1):
            plural = "backlink" if count == 1 else "backlinks"
            lines.append(f"{i}. [[{rel[:-len(NOTE_EXT)]}|{title}]] — {count} {plural}")
    else:
        lines.append("_(nothing linked yet)_")
    lines.append("")

    lines.append("## Recently active")
    for rel, title, day in stats.recent:
        lines.append(f"- {day} — [[{rel[:-len(NOTE_EXT)]}|{title}]]")
    lines.append("")

    lines.append("## Health")
    lines.append(f"- Orphans: {stats.orphans} note(s) with no links in or out")
    lines.append(f"- Dangling links: {stats.dangling_links} reference(s) to a missing note")
    lines.append("")
    lines.append("Run `vault_cli doctor` for the full health report.")
    return "\n".join(lines) + "\n"


def write_dashboard() -> str:
    """Compute vault stats and refresh ``Maps/Dashboard.md``. Returns its path."""
    stats = compute_stats()
    dest = vault_root() / _DASHBOARD_REL
    if _write_if_changed(dest, render_dashboard(stats)):
        _reindex_path(dest)
        log.info("refreshed vault dashboard (%d notes)", stats.total)
    return str(dest)


# ── Canvas: a spatial map of the entity graph ─────────────────────────────────
_CANVAS_NODE_W, _CANVAS_NODE_H = 260, 60
_CANVAS_COL_GAP, _CANVAS_ROW_GAP, _CANVAS_BAND_GAP = 30, 20, 100
_CANVAS_COLS = 4


def write_canvas() -> str:
    """Regenerate ``Vault Overview.canvas`` — a spatial map of the entity graph.

    Obsidian's built-in graph view shows *every* note; this Canvas is a curated,
    always-fresh alternative scoped to what usually matters most: every
    People/Companies/Projects entity, grouped and colored by folder (matching
    :func:`write_graph_config`'s palette), with an edge drawn wherever one
    entity's note links to another's. Regenerated fresh each run — token-free,
    and safe to move/resize by hand in Obsidian since it's fully overwritten
    next time (position edits won't be preserved across a rerun). Returns the
    file's path. Uses the `JSON Canvas <https://jsoncanvas.org/>`_ format that
    Obsidian's Canvas core plugin reads directly — no plugin required.
    """
    by_folder: dict[str, list[tuple[str, str]]] = {}
    resolve: dict[str, str] = {}          # lowercased title/stem/alias -> rel
    body_by_rel: dict[str, str] = {}
    for folder in ENTITY_FOLDERS:
        items: list[tuple[str, str]] = []
        for rel in list_notes(folder=folder):
            if len(rel.split("/")) != 2:
                continue  # only the entity note itself, not notes nested under it
            try:
                raw = (vault_root() / rel).read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            meta, body = parse_frontmatter(raw)
            title = title_for(rel, meta, body)
            items.append((rel, title))
            body_by_rel[rel] = body
            for key in _dedupe([title, Path(rel).stem, *extract_aliases(meta)]):
                resolve[key.lower()] = rel
        by_folder[folder] = sorted(items, key=lambda x: x[1].lower())

    nodes: list[dict] = []
    node_id: dict[str, str] = {}
    y = 0
    for folder in ENTITY_FOLDERS:
        items = by_folder[folder]
        if not items:
            continue
        color = vault_taxonomy.color_for_folder(folder) or "#607d8b"
        rows = -(-len(items) // _CANVAS_COLS)  # ceil division
        band_h = rows * (_CANVAS_NODE_H + _CANVAS_ROW_GAP) + 70
        nodes.append({
            "id": f"group-{folder}", "type": "group",
            "label": f"{vault_taxonomy.icon_for_folder(folder)} {folder}",
            "x": -20, "y": y - 50,
            "width": _CANVAS_COLS * (_CANVAS_NODE_W + _CANVAS_COL_GAP) + 20,
            "height": band_h, "color": color,
        })
        for i, (rel, _title) in enumerate(items):
            col, row = i % _CANVAS_COLS, i // _CANVAS_COLS
            nid = f"n{len(node_id)}"
            node_id[rel] = nid
            nodes.append({
                "id": nid, "type": "file", "file": rel,
                "x": col * (_CANVAS_NODE_W + _CANVAS_COL_GAP),
                "y": y + row * (_CANVAS_NODE_H + _CANVAS_ROW_GAP),
                "width": _CANVAS_NODE_W, "height": _CANVAS_NODE_H, "color": color,
            })
        y += band_h + _CANVAS_BAND_GAP

    edges: list[dict] = []
    drawn: set[frozenset] = set()
    for rel, body in body_by_rel.items():
        for target in extract_wikilinks(body):
            other = resolve.get(target.strip().lower())
            if not other or other == rel or other not in node_id:
                continue
            pair = frozenset((rel, other))
            if pair in drawn:
                continue
            drawn.add(pair)
            edges.append({"id": f"e{len(edges)}", "fromNode": node_id[rel], "toNode": node_id[other]})

    dest = vault_root() / "Vault Overview.canvas"
    if _write_if_changed(dest, json.dumps({"nodes": nodes, "edges": edges}, indent=2)):
        log.info("refreshed entity canvas (%d nodes, %d edges)", len(nodes), len(edges))
    return str(dest)


def linkify_vault(roster: dict[str, str] | None = None) -> int:
    """Retro-wrap bare mentions of known entities in ``[[links]]`` across old notes.

    Connects pre-existing notes (sessions, imported, topics, daily) into the graph
    deterministically — no API calls. Frontmatter is preserved verbatim; entity
    folders (People/Companies/Projects) and Maps/ are skipped to avoid self-link
    noise. Returns the number of notes changed.
    """
    roster = roster if roster is not None else get_roster()
    if not roster:
        return 0
    skip = set(ENTITY_FOLDERS) | {"Maps"} | INDEX_SKIP_FOLDERS
    changed = 0
    for p in iter_markdown():
        if _rel(p).split("/", 1)[0] in skip:
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        fm, body = _split_frontmatter_raw(raw)
        new_body = linkify_entities(body, roster)
        if new_body != body:
            p.write_text(fm + new_body, encoding="utf-8")
            _reindex_path(p)
            changed += 1
    return changed


# Meeting/session-style note names that should never be a People/Projects *entity*.
# "session" is a whole word here (not just a prefix) so a title like "Planning
# Session" or "Q3 Review Session" is caught too, not just filenames that start
# with it (e.g. "session_2026-06-30").
_MEETING_RE = re.compile(
    r"(?:^|[\s_-])(meeting|standup|stand-up|huddle|retro|retrospective|"
    r"1on1|1-on-1|1:1|1-1|kickoff|kick-off|sync|check-in|checkin|session)"
    r"(?:$|[\s_-])", re.I)


def looks_like_meeting(name: str) -> bool:
    """True if *name* reads like a meeting/session note (not a person/project)."""
    return bool(_MEETING_RE.search(name or ""))


def find_misfiled() -> dict:
    """Detect notes likely in the wrong folder.

    Returns ``{"meetings_in_entities": [rel…], "cross_folder": {slug: [rel…]}}``:
      * meeting/session notes anywhere under an entity folder — a meeting is never
        an entity, so one sitting in ``Projects/`` *or* nested in
        ``Projects/Brightpoint/`` is flagged (other nested notes are left alone),
        and
      * the same entity name present in more than one entity folder (a duplicate,
        e.g. ``People/Felicity Kline.md`` and ``Projects/felicity_kline.md``).
    """
    meetings: list[str] = []
    by_slug: dict[str, list[str]] = {}
    for folder in ENTITY_FOLDERS:
        for rel in list_notes(folder=folder):
            stem = Path(rel).stem
            if looks_like_meeting(stem):
                meetings.append(rel)              # a meeting at any depth is misfiled
                continue
            if len(rel.split("/")) != 2:          # other nested notes are scoped
                continue
            by_slug.setdefault(_slugify(stem), []).append(rel)
    cross = {
        slug: sorted(rels) for slug, rels in by_slug.items()
        if len({r.split("/")[0] for r in rels}) > 1
    }
    return {"meetings_in_entities": sorted(meetings), "cross_folder": cross}


def refile_meetings(dry_run: bool = True) -> list[tuple[str, str]]:
    """Move meeting/session notes out of any entity folder into ``Sessions/``.

    Returns ``(src, dest)`` pairs. With ``dry_run=False`` it performs each move
    (non-colliding), corrects the note's ``type`` to ``session``, and reindexes.
    Catches meetings nested under a project (``Projects/Brightpoint/standup.md``)
    too, since a meeting is never part of an entity.
    """
    moved: list[tuple[str, str]] = []
    for rel in find_misfiled()["meetings_in_entities"]:
        name = Path(rel).name
        if dry_run:
            moved.append((rel, f"Sessions/{name}"))
            continue
        src = _safe_path(rel)
        meta, body = parse_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
        meta["type"] = vault_taxonomy.type_for_folder("Sessions")
        dest = _noncolliding(vault_root() / "Sessions" / name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"{build_frontmatter(meta)}{body.strip()}\n", encoding="utf-8")
        src.unlink()
        try:
            from app.vault_index import get_index
            get_index().remove(rel)
        except Exception as exc:  # noqa: BLE001
            log.debug("index remove after refile failed for %s: %s", rel, exc)
        _reindex_path(dest)
        moved.append((rel, _rel(dest)))
    if moved and not dry_run:
        log.info("refiled %d meeting note(s) out of entity folders", len(moved))
    return moved


def dedupe_entities(dry_run: bool = True) -> list[tuple[str, str]]:
    """Merge cross-folder entity duplicates (same name in 2+ entity folders). Token-free.

    For each duplicated name the copy in the **highest-priority** entity folder
    (``ENTITY_FOLDERS`` order: People → Companies → Projects) is kept and the others
    are folded into it (bodies appended, originals archived), collapsing the
    ``People/Joe Konkle.md`` + ``Projects/joe_konkle.md`` pairs the old fact-router
    created. Returns ``(merged_away, kept)`` pairs.

    This only resolves *exact-name* duplicates; it does not move an entity that's
    simply in the wrong folder (e.g. a campaign filed under People). For that
    semantic placement, use the API-backed ``vault_entities --reclassify``. Safe and
    reversible — losers go to ``Archive/``, nothing is deleted.
    """
    merged: list[tuple[str, str]] = []
    priority = {f: i for i, f in enumerate(ENTITY_FOLDERS)}
    for rels in find_misfiled()["cross_folder"].values():
        keep = min(rels, key=lambda r: priority.get(r.split("/", 1)[0], 99))
        keep_folder = keep.split("/", 1)[0]
        canonical = read_note(keep).title
        for rel in rels:
            if rel == keep:
                continue
            if dry_run:
                merged.append((rel, keep))
            elif merge_note_into(rel, canonical, folder=keep_folder):
                merged.append((rel, keep))
    if merged and not dry_run:
        log.info("deduped %d cross-folder entity duplicate(s)", len(merged))
    return merged


def relocate_note(rel: str, dest_folder: str) -> str:
    """Move a note into *dest_folder*, merging into an existing entity of the same name.

    The mover behind the reclassify pass (a meeting in People/ → Sessions/, a
    person in Projects/ → People/). When the destination is an entity folder and
    a note for the same name already lives there, the bodies are merged and the
    source archived instead of creating a cross-folder duplicate. Otherwise the
    file is moved (non-colliding), its ``type`` corrected to the destination's,
    and the index updated. Returns the note's new vault-relative path.
    """
    src = _safe_path(rel)
    if not src.exists():
        raise VaultError(f"note '{rel}' not found.")
    cur_rel = _rel(src)
    dest_folder = (dest_folder or "").strip().strip("/")
    if not dest_folder:
        raise VaultError("relocate_note needs a destination folder.")
    if cur_rel.split("/", 1)[0] == dest_folder:
        return cur_rel  # already there
    meta, body = parse_frontmatter(src.read_text(encoding="utf-8", errors="replace"))
    title = title_for(cur_rel, meta, body)
    if dest_folder in ENTITY_FOLDERS:
        existing = find_entity_note(title, dest_folder)
        if existing and existing != cur_rel:
            merge_note_into(cur_rel, title, dest_folder)
            log.info("relocated %s by merging into existing %s", cur_rel, existing)
            return existing
    dest = _noncolliding(vault_root() / dest_folder / Path(cur_rel).name)
    meta["type"] = vault_taxonomy.type_for_folder(dest_folder)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"{build_frontmatter(meta)}{body.strip()}\n", encoding="utf-8")
    src.unlink()
    try:
        from app.vault_index import get_index
        get_index().remove(cur_rel)
    except Exception as exc:  # noqa: BLE001
        log.debug("index remove after relocate failed for %s: %s", cur_rel, exc)
    _reindex_path(dest)
    _invalidate_roster_if_entity(src)
    _invalidate_roster_if_entity(dest)
    log.info("relocated vault note %s -> %s", cur_rel, _rel(dest))
    return _rel(dest)


def find_orphans() -> list[str]:
    """Notes with no outgoing ``[[links]]`` and no backlinks — graph islands."""
    orphans: list[str] = []
    for p in iter_markdown():
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if extract_wikilinks(body):
            continue
        rel = _rel(p)
        if not _backlinks(p, title_for(rel, meta, body), extract_aliases(meta)):
            orphans.append(rel)
    return sorted(orphans)


def find_dangling_links() -> dict[str, list[str]]:
    """``{source: [missing targets]}`` for ``[[links]]`` pointing at no existing note."""
    out_links: dict[str, list[str]] = {}
    resolvable: set[str] = set()
    for p in iter_markdown():
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        rel = _rel(p)
        resolvable |= {
            title_for(rel, meta, body).lower(),
            Path(rel).stem.lower(),
            rel[:-len(NOTE_EXT)].lower(),
            *(a.lower() for a in extract_aliases(meta)),
        }
        out_links[rel] = extract_wikilinks(body)
    dangling: dict[str, list[str]] = {}
    for rel, links in out_links.items():
        missing = [t for t in links if t.lower() not in resolvable]
        if missing:
            dangling[rel] = missing
    return dangling


# ── Scaffold + migration ─────────────────────────────────────────────────────
_INDEX_TEMPLATE = (
    "---\ntitle: Index\ntags: [moc]\n---\n\n"
    "# Index — Map of Content\n\n"
    "JARVIS's knowledge vault. Notes are organized into folders and linked with "
    "`[[wikilinks]]`.\n\n"
    "- [[Sessions]] — recaps of past conversations\n"
    "- [[Daily]] — daily logs\n"
    "- [[People]] — notes about individual people\n"
    "- [[Companies]] — notes about companies/organizations\n"
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
