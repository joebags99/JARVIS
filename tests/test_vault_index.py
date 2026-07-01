"""Tests for the FTS5 vault search index (app/vault_index.py)."""

from __future__ import annotations

from app.vault_index import VaultIndex


def _idx(tmp_path):
    return VaultIndex(tmp_path / "idx.db")


def test_upsert_and_search(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("Topics/cooking.md", "Cooking", "food recipe", "Pasta and tomato sauce", 100.0)
    hits = ix.search("tomato sauce")
    assert hits and hits[0].path == "Topics/cooking.md"
    assert "tomato" in hits[0].snippet.lower()


def test_search_relevance(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "Remodel", "", "Discussed the kitchen remodel budget and quotes", 1.0)
    ix.upsert("b.md", "Hike", "", "Planned a weekend hiking trip to the mountains", 2.0)
    hits = ix.search("kitchen remodel budget", limit=2)
    assert hits[0].path == "a.md"


def test_recent_newest_first(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("old.md", "Old", "", "first", 1.0)
    ix.upsert("new.md", "New", "", "second", 2.0)
    assert ix.recent(limit=5)[0].path == "new.md"


def test_empty_query_returns_recent(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "A", "", "x", 1.0)
    ix.upsert("b.md", "B", "", "y", 2.0)
    assert len(ix.search("   ")) == 2


def test_tag_filter(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "A", "meeting daedabyte", "notes about jazz", 1.0)
    ix.upsert("b.md", "B", "personal", "notes about jazz", 2.0)
    hits = ix.search("jazz", tag="meeting")
    assert [h.path for h in hits] == ["a.md"]
    # tag filter also tolerates a leading '#'
    assert [h.path for h in ix.search("jazz", tag="#personal")] == ["b.md"]


def test_folder_filter(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("People/sam.md", "Sam", "", "jazz lover", 1.0)
    ix.upsert("Topics/jazz.md", "Jazz", "", "jazz history", 2.0)
    hits = ix.search("jazz", folder="People")
    assert [h.path for h in hits] == ["People/sam.md"]


def test_remove(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "A", "", "removable content", 1.0)
    assert ix.count() == 1
    ix.remove("a.md")
    assert ix.count() == 0
    assert ix.search("removable") == []


def test_upsert_replaces_not_duplicates(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "A", "", "original text", 1.0)
    ix.upsert("a.md", "A", "", "updated text", 2.0)
    assert ix.count() == 1
    hits = ix.search("updated")
    assert hits and "updated" in hits[0].snippet.lower()
    assert ix.search("original") == []  # stale FTS row was cleared


def test_sync_reconciles(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("keep.md", "K", "", "keep me", 1.0)
    ix.upsert("drop.md", "D", "", "drop me", 1.0)
    # keep unchanged (same mtime), new added, drop no longer present → removed.
    changed = ix.sync([
        ("keep.md", "K", "", "keep me", 1.0),
        ("new.md", "N", "", "new note", 3.0),
    ])
    assert changed == 1
    assert {h.path for h in ix.recent(limit=10)} == {"keep.md", "new.md"}


def test_search_handles_punctuation_and_operators(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "Deadline", "", "Project deadline is Friday", 1.0)
    # FTS5 operators / punctuation in the raw query must not raise.
    hits = ix.search('deadline?? "AND" (Friday)')
    assert any("deadline" in h.snippet.lower() for h in hits)


def test_linking_to_finds_backlinks(tmp_path):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "A", "", "see [[Sam]] for details", 1.0)
    ix.upsert("c.md", "C", "", "meeting with [[Sam|Samantha]]", 2.0)
    ix.upsert("b.md", "B", "", "no links here", 3.0)
    assert set(ix.linking_to("Sam")) == {"a.md", "c.md"}


def test_like_fallback_when_no_fts(tmp_path, monkeypatch):
    ix = _idx(tmp_path)
    ix.upsert("a.md", "A", "tagword", "fallback search works", 1.0)
    monkeypatch.setattr(ix, "_fts", False)
    hits = ix.search("fallback")
    assert hits and hits[0].path == "a.md"
    # filters still apply on the LIKE path
    assert ix.search("fallback", tag="tagword")
    assert ix.search("fallback", folder="People") == []
