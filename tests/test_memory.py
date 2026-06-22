"""Tests for the long-term memory store (app/memory.py)."""

from __future__ import annotations

from app import memory as memory_mod
from app.memory import KIND_FACT, KIND_SESSION, Memory


def _mem(tmp_path):
    return Memory(tmp_path / "m.db")


def test_add_and_search_fact(tmp_path):
    m = _mem(tmp_path)
    rid = m.add_fact("Allergic to shellfish")
    assert rid is not None
    hits = m.search("shellfish allergy")
    assert hits and hits[0].kind == KIND_FACT
    assert "shellfish" in hits[0].content.lower()


def test_fact_dedup(tmp_path):
    m = _mem(tmp_path)
    a = m.add_fact("Prefers metric units")
    b = m.add_fact("Prefers metric units")
    assert a == b
    assert m.count(KIND_FACT) == 1


def test_recent_newest_first(tmp_path):
    m = _mem(tmp_path)
    m.add_session("First session about taxes")
    m.add_session("Second session about vacation")
    recent = m.recent(limit=5)
    assert recent[0].content.startswith("Second")


def test_search_relevance_finds_best_match(tmp_path):
    m = _mem(tmp_path)
    m.add_session("Discussed the kitchen remodel budget and contractor quotes")
    m.add_session("Planned a weekend hiking trip to the mountains")
    hits = m.search("kitchen remodel budget", limit=2)
    assert hits
    assert "kitchen" in hits[0].content.lower()


def test_search_empty_query_returns_recent(tmp_path):
    m = _mem(tmp_path)
    m.add_fact("Fact one")
    m.add_session("Session two")
    assert len(m.search("   ", limit=5)) == 2


def test_kinds_filter(tmp_path):
    m = _mem(tmp_path)
    m.add_fact("Likes jazz music")
    m.add_session("Talked about jazz concerts")
    facts = m.search("jazz", kinds=(KIND_FACT,))
    assert facts and all(h.kind == KIND_FACT for h in facts)


def test_search_handles_punctuation_and_operators(tmp_path):
    m = _mem(tmp_path)
    m.add_fact("Project deadline is Friday")
    # FTS5 operators / punctuation in the query must not raise a syntax error
    hits = m.search('deadline?? "AND" (Friday)')
    assert any("deadline" in h.content.lower() for h in hits)


def test_import_legacy_sessions_idempotent(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "session_2026-01-01_09-00.md").write_text(
        "# JARVIS Session — January 1\n\n- Talked about new-year goals\n",
        encoding="utf-8",
    )
    (notes / "session_2026-01-02_10-00.md").write_text(
        "# JARVIS Session — January 2\n\n- Reviewed the remodel budget\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(memory_mod, "NOTES_DIR", notes)

    m = _mem(tmp_path)
    assert m.import_legacy_sessions() == 2
    assert m.count(KIND_SESSION) == 2
    assert m.import_legacy_sessions() == 0  # second run is a no-op

    hits = m.search("budget")
    assert any("budget" in h.content.lower() for h in hits)
    # the "# JARVIS Session …" header line was stripped on import
    assert all(not h.content.startswith("#") for h in hits)
