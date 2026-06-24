"""Long-term memory: a local SQLite store for durable facts + session summaries.

Replaces the old "last 3 session files" recall with relevance-ranked retrieval
over everything JARVIS has chosen to remember. Two kinds of memory share one
table:

  * ``session`` — auto-saved summaries of past conversations (cross-session
    memory), written when a substantive session closes.
  * ``fact`` — durable facts/preferences about the user, stored on demand via
    the ``remember`` tool or auto-extracted at session close.

Search uses SQLite's built-in FTS5 (BM25 relevance ranking) — no embeddings, no
external service, fully local, true to JARVIS's privacy posture. ``search`` is
the single retrieval entry point, so a semantic/embedding backend can be slotted
in later without changing callers. On a SQLite build without FTS5 it degrades to
a LIKE keyword scan.

Everything is best-effort: a failed write or query logs and returns a safe empty
value rather than breaking a conversation. The DB lives at memory.db (gitignored)
and is created lazily via ``get_memory()`` so importing this module is side-effect
free (handy for tests, which construct their own ``Memory(tmp_path)``).
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .config import NOTES_DIR, ROOT_DIR
from .logging_setup import get_logger

log = get_logger("memory")

DB_PATH = ROOT_DIR / "memory.db"

KIND_SESSION = "session"
KIND_FACT = "fact"
_KINDS = (KIND_SESSION, KIND_FACT)

# Words for an FTS/LIKE query; single chars and punctuation are dropped so a raw
# user/model phrase can't inject FTS5 operators or blow up the MATCH syntax.
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass
class MemoryItem:
    id: int
    kind: str
    content: str
    source: str | None
    created_at: str


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if len(w) >= 2]


class Memory:
    """A SQLite-backed long-term memory store (facts + session summaries)."""

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
                    "CREATE TABLE IF NOT EXISTS memories("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " kind TEXT NOT NULL,"
                    " content TEXT NOT NULL,"
                    " source TEXT,"
                    " created_at TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)"
                )
                self._init_fts(conn)
        except Exception as exc:  # noqa: BLE001
            log.error("memory db init failed: %s", exc)
            self._fts = False

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        """Create the FTS5 index + sync triggers; flip to LIKE mode if unsupported."""
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(content, content='memories', content_rowid='id')"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN"
                " INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);"
                " END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN"
                " INSERT INTO memories_fts(memories_fts, rowid, content)"
                " VALUES('delete', old.id, old.content); END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN"
                " INSERT INTO memories_fts(memories_fts, rowid, content)"
                " VALUES('delete', old.id, old.content);"
                " INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);"
                " END"
            )
            self._fts = True
        except sqlite3.OperationalError as exc:
            log.warning("FTS5 unavailable; using LIKE search fallback: %s", exc)
            self._fts = False

    # ── Writes ───────────────────────────────────────────────────────────────
    def add(
        self, content: str, kind: str, source: str | None = None, dedup: bool = False
    ) -> int | None:
        """Store one memory; returns its row id (or an existing id when deduped)."""
        content = (content or "").strip()
        if not content or kind not in _KINDS:
            return None
        try:
            with closing(self._connect()) as conn, conn:
                if dedup:
                    row = conn.execute(
                        "SELECT id FROM memories WHERE kind=? AND content=? LIMIT 1",
                        (kind, content),
                    ).fetchone()
                    if row:
                        return row["id"]
                cur = conn.execute(
                    "INSERT INTO memories(kind, content, source, created_at)"
                    " VALUES(?,?,?,?)",
                    (kind, content, source, dt.datetime.now().isoformat(timespec="seconds")),
                )
                return cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            log.error("memory add failed: %s", exc)
            return None

    def add_session(self, summary: str, source: str = "session-close") -> int | None:
        return self.add(summary, KIND_SESSION, source=source)

    def add_fact(self, content: str, source: str = "remember") -> int | None:
        # Facts dedupe on exact content so repeating "remember X" doesn't pile up.
        return self.add(content, KIND_FACT, source=source, dedup=True)

    # ── Reads ────────────────────────────────────────────────────────────────
    def search(
        self, query: str, kinds: tuple[str, ...] | None = None, limit: int = 5
    ) -> list[MemoryItem]:
        """Relevance-ranked search. Falls back to most-recent when query is empty."""
        if not (query or "").strip():
            return self.recent(kinds=kinds, limit=limit)
        try:
            with closing(self._connect()) as conn:
                fts_query = " OR ".join(f'"{w}"' for w in _tokens(query)[:20])
                if self._fts and fts_query:
                    rows = self._fts_search(conn, fts_query, kinds, limit)
                else:
                    rows = self._like_search(conn, query, kinds, limit)
                return [self._row(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.error("memory search failed: %s", exc)
            return []

    def recent(
        self, kinds: tuple[str, ...] | None = None, limit: int = 5
    ) -> list[MemoryItem]:
        try:
            with closing(self._connect()) as conn:
                return [self._row(r) for r in self._recent_rows(conn, kinds, limit)]
        except Exception as exc:  # noqa: BLE001
            log.error("memory recent failed: %s", exc)
            return []

    def count(self, kind: str | None = None) -> int:
        try:
            with closing(self._connect()) as conn:
                if kind:
                    return conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE kind=?", (kind,)
                    ).fetchone()[0]
                return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            log.error("memory count failed: %s", exc)
            return 0

    # ── Query builders ───────────────────────────────────────────────────────
    def _fts_search(self, conn, fts_query, kinds, limit):
        sql = (
            "SELECT m.id, m.kind, m.content, m.source, m.created_at "
            "FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? "
        )
        params: list = [fts_query]
        if kinds:
            sql += "AND m.kind IN (%s) " % ",".join("?" * len(kinds))
            params += list(kinds)
        sql += "ORDER BY bm25(memories_fts) LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    def _like_search(self, conn, query, kinds, limit):
        words = _tokens(query)
        if not words:
            return self._recent_rows(conn, kinds, limit)
        clause = " OR ".join("LOWER(content) LIKE ?" for _ in words)
        params: list = [f"%{w}%" for w in words]
        sql = (
            "SELECT id, kind, content, source, created_at FROM memories "
            f"WHERE ({clause}) "
        )
        if kinds:
            sql += "AND kind IN (%s) " % ",".join("?" * len(kinds))
            params += list(kinds)
        sql += "ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    def _recent_rows(self, conn, kinds, limit):
        sql = "SELECT id, kind, content, source, created_at FROM memories "
        params: list = []
        if kinds:
            sql += "WHERE kind IN (%s) " % ",".join("?" * len(kinds))
            params += list(kinds)
        sql += "ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def _row(r) -> MemoryItem:
        return MemoryItem(
            id=r["id"], kind=r["kind"], content=r["content"],
            source=r["source"], created_at=r["created_at"],
        )

    # ── Legacy import ────────────────────────────────────────────────────────
    def import_legacy_sessions(self) -> int:
        """One-time import of existing notes/session_*.md summaries into the store.

        Idempotent: a meta flag records that it ran, so it imports each legacy
        file once and no-ops on later startups. Returns the number imported.
        """
        try:
            with closing(self._connect()) as conn:
                done = conn.execute(
                    "SELECT value FROM meta WHERE key='legacy_sessions_imported'"
                ).fetchone()
            if done:
                return 0

            imported = 0
            if NOTES_DIR.exists():
                for path in sorted(NOTES_DIR.glob("session_*.md")):
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace").strip()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not read %s: %s", path.name, exc)
                        continue
                    if not text:
                        continue
                    lines = text.splitlines()
                    body = "\n".join(lines[1:]).strip() if lines and lines[0].startswith("#") else text
                    if self.add(body or text, KIND_SESSION, source="imported", dedup=True):
                        imported += 1

            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value)"
                    " VALUES('legacy_sessions_imported', '1')"
                )
            if imported:
                log.info("imported %d legacy session summary(ies) into memory", imported)
            return imported
        except Exception as exc:  # noqa: BLE001
            log.error("legacy session import failed: %s", exc)
            return 0


# Lazily-constructed process-wide store (mirrors CONFIG/PERSONA, but lazy so a
# bare ``import app.memory`` doesn't create memory.db — tests use Memory(tmp)).
_MEMORY: Memory | None = None


def get_memory() -> Memory:
    global _MEMORY
    if _MEMORY is None:
        _MEMORY = Memory()
    return _MEMORY
