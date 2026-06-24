"""FTS5 search index over the Obsidian vault.

The vault (a folder of markdown files) is JARVIS's durable knowledge + memory
store; this module makes it *searchable*. It mirrors ``app/memory.py``'s
approach — SQLite's built-in FTS5 with BM25 relevance ranking, a LIKE keyword
fallback when FTS5 isn't compiled in, no embeddings and no external service — but
indexes *files* keyed by their vault-relative path instead of generated memory
rows.

The index is a cache, not the source of truth: the markdown on disk is. It's
kept fresh incrementally by :class:`integrations.obsidian.ObsidianWatcher`
(``upsert``/``remove`` on file events) and fully reconciled against the vault on
startup via :meth:`VaultIndex.sync`. A lost or corrupt index can always be
rebuilt from the files. Everything is best-effort: a failed write/query logs and
returns a safe empty value rather than breaking a conversation. The DB lives at
``vault_index.db`` (gitignored) and is created lazily so importing this module is
side-effect free (tests construct their own ``VaultIndex(tmp_path)``).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT_DIR
from .logging_setup import get_logger

log = get_logger("vault-index")

DB_PATH = ROOT_DIR / "vault_index.db"

# Words for an FTS/LIKE query; single chars and punctuation are dropped so a raw
# user/model phrase can't inject FTS5 operators or blow up the MATCH syntax.
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

# How much body text to scan/return when building a result snippet.
_SNIPPET_RADIUS = 160


@dataclass
class VaultHit:
    """One search hit: a vault note plus a short relevance snippet."""

    path: str
    title: str
    tags: str
    snippet: str
    mtime: float


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if len(w) >= 2]


def _snippet(body: str, query: str) -> str:
    """A short excerpt of *body* around the first query word (best-effort)."""
    body = (body or "").strip()
    if not body:
        return ""
    words = _tokens(query)
    low = body.lower()
    pos = -1
    for w in words:
        pos = low.find(w)
        if pos != -1:
            break
    if pos == -1:  # match was in title/tags, or empty query — show the head
        head = body[: _SNIPPET_RADIUS * 2]
        return head + ("…" if len(body) > len(head) else "")
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(body), pos + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end].strip() + suffix


class VaultIndex:
    """A SQLite/FTS5-backed search index over the vault's markdown files."""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self._path = str(db_path)
        self._fts = True  # verified during schema init; LIKE fallback otherwise
        self._init_db()

    # ── Connection / schema ──────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS notes("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " path TEXT UNIQUE NOT NULL,"
                    " title TEXT,"
                    " tags TEXT,"
                    " body TEXT,"
                    " mtime REAL NOT NULL)"
                )
                self._init_fts(conn)
        except Exception as exc:  # noqa: BLE001
            log.error("vault index init failed: %s", exc)
            self._fts = False

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        """Create the FTS5 index + sync triggers; flip to LIKE mode if unsupported."""
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts "
                "USING fts5(title, tags, body, content='notes', content_rowid='id')"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN"
                " INSERT INTO notes_fts(rowid, title, tags, body)"
                " VALUES (new.id, new.title, new.tags, new.body); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN"
                " INSERT INTO notes_fts(notes_fts, rowid, title, tags, body)"
                " VALUES('delete', old.id, old.title, old.tags, old.body); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN"
                " INSERT INTO notes_fts(notes_fts, rowid, title, tags, body)"
                " VALUES('delete', old.id, old.title, old.tags, old.body);"
                " INSERT INTO notes_fts(rowid, title, tags, body)"
                " VALUES (new.id, new.title, new.tags, new.body); END"
            )
            self._fts = True
        except sqlite3.OperationalError as exc:
            log.warning("FTS5 unavailable; using LIKE search fallback: %s", exc)
            self._fts = False

    # ── Writes ───────────────────────────────────────────────────────────────
    def upsert(self, path: str, title: str, tags: str, body: str, mtime: float) -> None:
        """Insert or replace one note by its vault-relative *path*.

        Implemented as delete-then-insert so the FTS sync triggers fire cleanly
        (a plain INSERT OR REPLACE would reuse the rowid without an UPDATE event).
        """
        path = (path or "").strip()
        if not path:
            return
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM notes WHERE path=?", (path,))
                conn.execute(
                    "INSERT INTO notes(path, title, tags, body, mtime) VALUES(?,?,?,?,?)",
                    (path, title or "", tags or "", body or "", mtime),
                )
        except Exception as exc:  # noqa: BLE001
            log.error("vault index upsert failed for %s: %s", path, exc)

    def remove(self, path: str) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM notes WHERE path=?", ((path or "").strip(),))
        except Exception as exc:  # noqa: BLE001
            log.error("vault index remove failed for %s: %s", path, exc)

    def sync(self, entries: list[tuple[str, str, str, str, float]]) -> int:
        """Reconcile the whole index against *entries* = (path,title,tags,body,mtime).

        Upserts only notes whose mtime changed (cheap re-scan on startup) and
        drops any indexed path no longer present on disk. Returns the number of
        notes (re)indexed.
        """
        try:
            with closing(self._connect()) as conn:
                known = {
                    r["path"]: r["mtime"]
                    for r in conn.execute("SELECT path, mtime FROM notes")
                }
        except Exception as exc:  # noqa: BLE001
            log.error("vault index sync read failed: %s", exc)
            known = {}

        seen: set[str] = set()
        changed = 0
        for path, title, tags, body, mtime in entries:
            seen.add(path)
            if known.get(path) != mtime:
                self.upsert(path, title, tags, body, mtime)
                changed += 1
        for stale in known.keys() - seen:
            self.remove(stale)
        if changed or (known.keys() - seen):
            log.info("vault index synced: %d changed, %d removed",
                     changed, len(known.keys() - seen))
        return changed

    # ── Reads ────────────────────────────────────────────────────────────────
    def search(
        self, query: str, tag: str | None = None, folder: str | None = None, limit: int = 5
    ) -> list[VaultHit]:
        """Relevance-ranked search. Falls back to most-recent when query is empty."""
        if not (query or "").strip():
            return self.recent(tag=tag, folder=folder, limit=limit)
        try:
            with closing(self._connect()) as conn:
                fts_query = " OR ".join(f'"{w}"' for w in _tokens(query)[:20])
                if self._fts and fts_query:
                    rows = self._fts_search(conn, fts_query, tag, folder, limit)
                else:
                    rows = self._like_search(conn, query, tag, folder, limit)
                return [self._hit(r, query) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.error("vault index search failed: %s", exc)
            return []

    def recent(
        self, tag: str | None = None, folder: str | None = None, limit: int = 5
    ) -> list[VaultHit]:
        try:
            with closing(self._connect()) as conn:
                sql = "SELECT path, title, tags, body, mtime FROM notes "
                where, params = self._filters(tag, folder)
                if where:
                    sql += "WHERE " + " AND ".join(where) + " "
                sql += "ORDER BY mtime DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [self._hit(r, "") for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.error("vault index recent failed: %s", exc)
            return []

    def count(self) -> int:
        try:
            with closing(self._connect()) as conn:
                return conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            log.error("vault index count failed: %s", exc)
            return 0

    def linking_to(self, name: str, limit: int = 25) -> list[str]:
        """Paths of notes whose body has a ``[[wikilink]]`` to *name* (best-effort).

        Powers backlinks without a full filesystem scan: the index already holds
        every note's body, so a LIKE over it finds inbound links cheaply.
        """
        name = (name or "").strip()
        if not name:
            return []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT path FROM notes WHERE body LIKE ? OR body LIKE ? LIMIT ?",
                    (f"%[[{name}]]%", f"%[[{name}|%", limit),
                ).fetchall()
                return [r["path"] for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.error("vault index linking_to failed: %s", exc)
            return []

    # ── Query builders ───────────────────────────────────────────────────────
    @staticmethod
    def _filters(tag: str | None, folder: str | None) -> tuple[list[str], list]:
        """Shared tag/folder WHERE clauses for both search and recent."""
        where: list[str] = []
        params: list = []
        if tag:
            where.append("LOWER(notes.tags) LIKE ?")
            params.append(f"%{tag.lstrip('#').lower()}%")
        if folder:
            prefix = folder.strip("/")
            where.append("notes.path LIKE ?")
            params.append(f"{prefix}/%")
        return where, params

    def _fts_search(self, conn, fts_query, tag, folder, limit):
        sql = (
            "SELECT notes.path, notes.title, notes.tags, notes.body, notes.mtime "
            "FROM notes_fts f JOIN notes ON notes.id = f.rowid "
            "WHERE notes_fts MATCH ? "
        )
        params: list = [fts_query]
        where, fparams = self._filters(tag, folder)
        if where:
            sql += "AND " + " AND ".join(where) + " "
            params += fparams
        sql += "ORDER BY bm25(notes_fts) LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    def _like_search(self, conn, query, tag, folder, limit):
        words = _tokens(query)
        if not words:
            return self.recent(tag=tag, folder=folder, limit=limit)
        clauses = [
            "(LOWER(notes.title) LIKE ? OR LOWER(notes.body) LIKE ? OR LOWER(notes.tags) LIKE ?)"
            for _ in words
        ]
        params: list = []
        for w in words:
            params += [f"%{w}%", f"%{w}%", f"%{w}%"]
        sql = (
            "SELECT notes.path, notes.title, notes.tags, notes.body, notes.mtime "
            "FROM notes WHERE (" + " OR ".join(clauses) + ") "
        )
        where, fparams = self._filters(tag, folder)
        if where:
            sql += "AND " + " AND ".join(where) + " "
            params += fparams
        sql += "ORDER BY mtime DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def _hit(r, query: str) -> VaultHit:
        return VaultHit(
            path=r["path"], title=r["title"] or "", tags=r["tags"] or "",
            snippet=_snippet(r["body"], query), mtime=r["mtime"],
        )


# Lazily-constructed process-wide index (mirrors get_memory()), so a bare import
# doesn't create vault_index.db — tests use VaultIndex(tmp_path).
_INDEX: VaultIndex | None = None


def get_index() -> VaultIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = VaultIndex()
    return _INDEX
