"""Tests for the Obsidian vault engine (integrations/obsidian.py)."""

from __future__ import annotations

import pytest

from app import vault_index
from app.config import CONFIG
from app.memory import Memory
from app.vault_index import VaultIndex
from integrations import obsidian
from integrations.obsidian import VaultError


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A temp vault with the global search index redirected to a temp DB."""
    root = tmp_path / "vault"
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", str(root))
    monkeypatch.setattr(CONFIG, "obsidian_enabled", True)
    # Redirect get_index()'s singleton so writes don't touch the repo's DB.
    monkeypatch.setattr(vault_index, "_INDEX", VaultIndex(tmp_path / "idx.db"))
    return root


# ── Path safety ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["../evil.md", "../../etc/passwd", "Topics/../../escape.md"])
def test_path_traversal_rejected(vault, bad):
    with pytest.raises(VaultError):
        obsidian.write_note(bad, "x")


def test_absolute_path_outside_vault_rejected(vault, tmp_path):
    with pytest.raises(VaultError):
        obsidian.write_note(str(tmp_path / "outside.md"), "x")
    # nothing leaked outside the vault
    assert not (tmp_path / "outside.md").exists()


def test_absolute_path_inside_vault_allowed(vault):
    obsidian.write_note(str(vault / "Topics" / "Inside.md"), "ok", title="Inside")
    assert obsidian.read_note("Topics/Inside.md").title == "Inside"


# ── Read / write / append ──────────────────────────────────────────────────────
def test_write_and_read_roundtrip(vault):
    obsidian.write_note("Topics/Test.md", "Hello world body", title="Test", tags=["x", "y"])
    note = obsidian.read_note("Topics/Test.md")
    assert note.title == "Test"
    assert "Hello world body" in note.body
    assert "x" in note.meta.get("tags", [])
    raw = (vault / "Topics" / "Test.md").read_text(encoding="utf-8")
    assert raw.startswith("---")          # frontmatter stamped
    assert "# Test" in raw                # heading added


def test_write_adds_md_suffix(vault):
    obsidian.write_note("People/Sam", "A colleague", title="Sam")
    assert (vault / "People" / "Sam.md").exists()


def test_move_to_archive_excludes_from_index(vault):
    obsidian.write_note("Imported/old.md", "secret budget figure", title="Old")
    assert any(h.path == "Imported/old.md" for h in obsidian.search("budget"))

    new_rel = obsidian.move_to_archive("Imported/old.md")
    assert new_rel == "Archive/Imported/old.md"
    assert not (vault / "Imported" / "old.md").exists()
    assert (vault / "Archive" / "Imported" / "old.md").exists()

    # Gone from search, and a full reindex keeps Archive/ out of the index.
    assert all(not h.path.startswith("Archive/") for h in obsidian.search("budget"))
    obsidian.reindex()
    assert all(not h.path.startswith("Archive/") for h in obsidian.search("budget"))


def test_move_to_archive_missing_note_raises(vault):
    with pytest.raises(VaultError):
        obsidian.move_to_archive("Imported/nope.md")


# ── People roster / name canonicalization ──────────────────────────────────────
def test_roster_from_people_aliases(vault):
    obsidian.set_aliases("Joe Konkle", ["Joe", "Joe K"])
    roster = obsidian.get_roster()
    assert roster["joe"] == "Joe Konkle"
    assert roster["joe k"] == "Joe Konkle"
    assert roster["joe konkle"] == "Joe Konkle"
    assert obsidian.canonical_people()["Joe Konkle"]  # has aliases


def test_canonicalize_links_only_touches_plain_known_links(vault):
    obsidian.set_aliases("Joe Konkle", ["Joe", "Joe K"])
    out = obsidian.canonicalize_links(
        "Met [[Joe]] and [[Joe K|Joey]] re [[Joe K#Notes]] and [[Sam]]."
    )
    assert "[[Joe Konkle]]" in out          # plain alias rewritten
    assert "[[Joe K|Joey]]" in out          # display-aliased link untouched
    assert "[[Joe K#Notes]]" in out         # heading link untouched
    assert "[[Sam]]" in out                 # unknown name untouched


def test_write_note_canonicalizes_person_links(vault):
    obsidian.set_aliases("Joe Konkle", ["Joe K"])
    obsidian.write_note("Sessions/m.md", "Spoke with [[Joe K]] today.", title="M")
    raw = (vault / "Sessions" / "m.md").read_text(encoding="utf-8")
    assert "[[Joe Konkle]]" in raw and "[[Joe K]]" not in raw


def test_backlinks_are_alias_aware(vault):
    canon_rel = obsidian.set_aliases("Joe Konkle", ["Joe K"])
    # A straggler that still links the alias (canonicalize off to simulate it).
    obsidian.write_note("Sessions/n.md", "ref [[Joe K]]", title="N", canonicalize=False)
    assert "Sessions/n.md" in obsidian.read_note(canon_rel).backlinks


def test_merge_and_recanonicalize(vault):
    obsidian.write_note("People/Joe.md", "Joe is CAO.", title="Joe", canonicalize=False)
    obsidian.write_note("Sessions/s.md", "owner [[Joe]]", title="S", canonicalize=False)

    obsidian.set_aliases("Joe Konkle", ["Joe"])
    assert obsidian.merge_note_into("People/Joe.md", "Joe Konkle") is True
    changed = obsidian.recanonicalize_vault()

    canon = obsidian.read_note("People/joe_konkle.md")
    assert "Joe is CAO" in canon.body                 # duplicate folded in
    assert not (vault / "People" / "Joe.md").exists()  # original archived
    assert (vault / "Archive" / "People" / "Joe.md").exists()
    assert "[[Joe Konkle]]" in (vault / "Sessions" / "s.md").read_text(encoding="utf-8")
    assert changed >= 1


def test_read_missing_raises(vault):
    with pytest.raises(VaultError):
        obsidian.read_note("nope.md")


def test_title_based_write_is_noncolliding(vault):
    p1 = obsidian.path_for_title("Meeting With Sam", "Projects")
    obsidian.write_note(p1, "first", title="Meeting With Sam", overwrite=False)
    obsidian.write_note(p1, "second", title="Meeting With Sam", overwrite=False)
    files = sorted(p.name for p in (vault / "Projects").glob("*.md"))
    assert files == ["meeting_with_sam.md", "meeting_with_sam_2.md"]


def test_overwrite_replaces(vault):
    obsidian.write_note("Topics/T.md", "original", title="T")
    msg = obsidian.write_note("Topics/T.md", "replaced", title="T")
    assert msg.startswith("Updated")
    assert "replaced" in obsidian.read_note("Topics/T.md").body


def test_append_creates_then_appends(vault):
    obsidian.append_note("Daily/2026-06-24.md", "- woke up")
    obsidian.append_note("Daily/2026-06-24.md", "- shipped the vault feature")
    body = obsidian.read_note("Daily/2026-06-24.md").body
    assert "woke up" in body and "shipped the vault feature" in body


def test_append_empty_rejected(vault):
    with pytest.raises(VaultError):
        obsidian.append_note("Daily/x.md", "   ")


# ── Markdown parsing ───────────────────────────────────────────────────────────
def test_frontmatter_roundtrip():
    text = "---\ntitle: Sam\ntags: [meeting, daedabyte]\n---\n\n# Sam\n\nNotes."
    meta, body = obsidian.parse_frontmatter(text)
    assert meta["title"] == "Sam"
    assert meta["tags"] == ["meeting", "daedabyte"]
    assert body.startswith("# Sam")
    rebuilt = obsidian.build_frontmatter(meta)
    assert "title: Sam" in rebuilt
    assert "tags: [meeting, daedabyte]" in rebuilt


def test_no_frontmatter_returns_whole_body():
    meta, body = obsidian.parse_frontmatter("Just a plain note.")
    assert meta == {}
    assert body == "Just a plain note."


def test_extract_tags_frontmatter_and_inline():
    meta = {"tags": ["fromfm"]}
    tags = obsidian.extract_tags(meta, "Body with #inline and #project/alpha tags")
    assert set(tags) == {"fromfm", "inline", "project/alpha"}


def test_extract_wikilinks():
    links = obsidian.extract_wikilinks("Talk to [[Sam]] and [[Project Alpha|Alpha]] today")
    assert links == ["Sam", "Project Alpha"]


# ── Search / backlinks (via the live index) ────────────────────────────────────
def test_search_and_backlinks(vault):
    obsidian.write_note("People/Sam.md", "A colleague at [[Daedabyte]]", title="Sam", tags=["people"])
    obsidian.write_note("Projects/Daedabyte.md", "Working with [[Sam]] on the launch", title="Daedabyte")
    hits = obsidian.search("colleague")
    assert any(h.path == "People/Sam.md" for h in hits)
    sam = obsidian.read_note("People/Sam.md")
    assert "Projects/Daedabyte.md" in sam.backlinks
    assert "Daedabyte" in sam.links


def test_list_notes(vault):
    obsidian.write_note("People/Sam.md", "x", title="Sam")
    obsidian.write_note("Projects/Site.md", "y", title="Site")
    assert obsidian.list_notes() == ["People/Sam.md", "Projects/Site.md"]
    assert obsidian.list_notes(folder="People") == ["People/Sam.md"]


# ── Scaffold + migration ───────────────────────────────────────────────────────
def test_ensure_scaffold(vault):
    obsidian.ensure_scaffold()
    assert (vault / "index.md").exists()
    for folder in ("Sessions", "People", "Projects", "Memory"):
        assert (vault / folder).is_dir()


def test_migrate_legacy_idempotent_and_nondestructive(vault, tmp_path):
    # Legacy notes/ layout: a category note + a root session summary.
    notes = tmp_path / "notes"
    (notes / "Daedabyte").mkdir(parents=True)
    (notes / "Daedabyte" / "2026-01-01_kickoff.md").write_text("# Kickoff\n\nNotes", encoding="utf-8")
    (notes / "session_2026-01-02_10-00.md").write_text("# Session\n\nRecap", encoding="utf-8")
    # Legacy memory.db with a durable fact.
    mem_db = tmp_path / "memory.db"
    Memory(mem_db).add_fact("Allergic to shellfish")

    count = obsidian.migrate_legacy(notes_dir=notes, memory_db=mem_db)
    assert count == 3  # category note + session file + 1 fact

    assert (vault / "Imported" / "Daedabyte" / "2026-01-01_kickoff.md").exists()
    assert (vault / "Sessions" / "session_2026-01-02_10-00.md").exists()
    assert "shellfish" in (vault / "Memory" / "Facts.md").read_text(encoding="utf-8").lower()

    # Re-run is a no-op (marker honored) and originals are untouched.
    assert obsidian.migrate_legacy(notes_dir=notes, memory_db=mem_db) == 0
    assert (notes / "Daedabyte" / "2026-01-01_kickoff.md").exists()
    assert mem_db.exists()


def test_migration_plan_previews_without_writing(vault, tmp_path):
    notes = tmp_path / "notes"
    (notes / "General").mkdir(parents=True)
    (notes / "General" / "a.md").write_text("x", encoding="utf-8")
    (notes / "session_2026-01-01_09-00.md").write_text("y", encoding="utf-8")
    mem_db = tmp_path / "memory.db"
    Memory(mem_db).add_fact("likes tea")

    plan = obsidian.migration_plan(notes_dir=notes, memory_db=mem_db)
    assert [d for _, d in plan["notes"]] == ["Imported/General/a.md"]
    assert [d for _, d in plan["sessions"]] == ["Sessions/session_2026-01-01_09-00.md"]
    assert plan["fact_count"] == 1
    assert plan["already_migrated"] is False
    # A preview writes nothing — no vault folder, marker, or Imported/ created.
    assert not vault.exists()
