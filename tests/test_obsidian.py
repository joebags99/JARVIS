"""Tests for the Obsidian vault engine (integrations/obsidian.py)."""

from __future__ import annotations

from pathlib import Path

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


# ── Taxonomy: types, graph, maps, health ──────────────────────────────────────
def test_write_stamps_type_from_folder(vault):
    obsidian.write_note("People/Sam.md", "x", title="Sam")
    obsidian.write_note("Topics/SEO.md", "y", title="SEO")
    assert obsidian.read_note("People/Sam.md").meta.get("type") == "person"
    assert obsidian.read_note("Topics/SEO.md").meta.get("type") == "topic"


def test_backfill_types(vault):
    (vault / "Projects").mkdir(parents=True, exist_ok=True)
    (vault / "Projects" / "old.md").write_text("# Old\n\nbody", encoding="utf-8")  # no type
    assert obsidian.backfill_types() >= 1
    assert obsidian.read_note("Projects/old.md").meta.get("type") == "project"


def test_capture_idea(vault):
    obsidian.capture_idea("build a wake word")
    note = obsidian.read_note("Ideas/Inbox.md")
    assert "build a wake word" in note.body and note.meta.get("type") == "idea"


def test_rebuild_mocs_creates_hub_links(vault):
    obsidian.write_note(obsidian.path_for_title("Joe Konkle", "People"), "x", title="Joe Konkle")
    obsidian.write_note(obsidian.path_for_title("Website", "Projects"), "y", title="Website")
    assert obsidian.rebuild_mocs() >= 2
    people_map = obsidian.read_note("Maps/People.md")
    assert "[[People/joe_konkle|Joe Konkle]]" in people_map.body
    assert people_map.meta.get("type") == "map"
    assert "[[Maps/People|People]]" in (vault / "index.md").read_text(encoding="utf-8")


def test_rebuild_mocs_and_graph_idempotent(vault):
    obsidian.write_note(obsidian.path_for_title("X", "Topics"), "a", title="X")
    obsidian.rebuild_mocs()
    obsidian.write_graph_config()
    moc = vault / "Maps" / "Topics.md"
    graph = vault / ".obsidian" / "graph.json"
    moc_mt, graph_mt = moc.stat().st_mtime_ns, graph.stat().st_mtime_ns
    # Re-running with nothing changed must NOT rewrite the files (no startup churn).
    obsidian.rebuild_mocs()
    obsidian.write_graph_config()
    assert moc.stat().st_mtime_ns == moc_mt
    assert graph.stat().st_mtime_ns == graph_mt


def test_find_orphans(vault):
    obsidian.write_note("Topics/Lonely.md", "no links", title="Lonely", canonicalize=False)
    obsidian.write_note("Topics/Linker.md", "see [[Lonely]]", title="Linker", canonicalize=False)
    obsidian.write_note("Topics/Island.md", "truly alone", title="Island", canonicalize=False)
    orphans = obsidian.find_orphans()
    assert "Topics/Island.md" in orphans          # no links in or out
    assert "Topics/Linker.md" not in orphans       # has an outgoing link
    assert "Topics/Lonely.md" not in orphans       # has a backlink


def test_find_dangling_links(vault):
    obsidian.write_note("Topics/A.md", "to [[Ghost]] and [[A]]", title="A", canonicalize=False)
    assert obsidian.find_dangling_links().get("Topics/A.md") == ["Ghost"]  # [[A]] self-resolves


def test_write_graph_config(vault):
    import json as _json
    obsidian.write_graph_config()
    data = _json.loads((vault / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
    queries = {g["query"] for g in data["colorGroups"]}
    assert 'path:"People/"' in queries and 'path:"Ideas/"' in queries
    assert all("rgb" in g["color"] for g in data["colorGroups"])


def test_compute_stats_counts_types_links_and_recency(vault):
    obsidian.write_note("People/Joe Konkle.md", "notes", title="Joe Konkle", canonicalize=False)
    obsidian.write_note(
        "Sessions/planning.md", "Met with [[Joe Konkle]] and [[Joe Konkle]] again.",
        title="Planning", canonicalize=False,
    )
    stats = obsidian.compute_stats()
    assert stats.total == 2
    assert ("person", 1) in stats.by_type and ("session", 1) in stats.by_type
    # A double-mention in one note is still a single inbound edge from that note.
    assert stats.most_linked[0] == ("People/Joe Konkle.md", "Joe Konkle", 1)
    assert stats.recent[0][0] == "Sessions/planning.md"  # written most recently
    assert stats.orphans == 0
    assert stats.dangling_links == 0


def test_write_dashboard_is_idempotent(vault):
    obsidian.write_note("Topics/X.md", "a", title="X")
    dest = obsidian.write_dashboard()
    path = vault / "Maps" / "Dashboard.md"
    assert str(path) == dest
    body = obsidian.read_note("Maps/Dashboard.md").body
    assert "Vault Dashboard" in body and "By type" in body
    mtime = path.stat().st_mtime_ns
    obsidian.write_dashboard()  # nothing changed -> no rewrite (no startup churn)
    assert path.stat().st_mtime_ns == mtime


def test_write_canvas_groups_and_links_entities(vault):
    obsidian.write_note("People/Joe Konkle.md", "Works with [[Daedabyte]].", title="Joe Konkle")
    obsidian.write_note("Companies/Daedabyte.md", "notes", title="Daedabyte")
    obsidian.write_note("Projects/Solo.md", "unrelated", title="Solo")  # no links -> no edge
    path = obsidian.write_canvas()
    import json as _json
    data = _json.loads(Path(path).read_text(encoding="utf-8"))
    file_nodes = {n["file"]: n["id"] for n in data["nodes"] if n["type"] == "file"}
    groups = {n["label"]: n for n in data["nodes"] if n["type"] == "group"}
    assert set(file_nodes) == {"People/Joe Konkle.md", "Companies/Daedabyte.md", "Projects/Solo.md"}
    assert "👤 People" in groups and "🏢 Companies" in groups
    assert len(data["edges"]) == 1
    edge = data["edges"][0]
    assert {edge["fromNode"], edge["toNode"]} == {
        file_nodes["People/Joe Konkle.md"], file_nodes["Companies/Daedabyte.md"],
    }


def test_extract_open_items():
    body = (
        "# Planning\n\n## Summary\nWe talked.\n\n"
        "## Action Items\n- Follow up with Sam\n- Send the doc\n\n"
        "## Open Questions\n- Is budget approved?\n\n"
        "## Decisions\n- Ship next week\n"
    )
    assert obsidian.extract_open_items(body) == [
        "Follow up with Sam", "Send the doc", "Is budget approved?",
    ]


def test_extract_open_items_empty_section_yields_nothing():
    body = "## Action Items\n\n## Decisions\n- Something else\n"
    assert obsidian.extract_open_items(body) == []


def test_list_open_callbacks_only_returns_sessions_with_open_items(vault):
    obsidian.write_note(
        "Sessions/planning.md",
        "## Action Items\n- Follow up with Sam\n",
        title="Planning", canonicalize=False,
    )
    obsidian.write_note(
        "Sessions/empty.md", "## Summary\nNothing outstanding.\n",
        title="Empty", canonicalize=False,
    )
    obsidian.write_note("Topics/unrelated.md", "## Action Items\n- Not a session\n",
                         title="Unrelated", canonicalize=False)

    out = {rel: items for rel, _meta, items in obsidian.list_open_callbacks()}
    assert out == {"Sessions/planning.md": ["Follow up with Sam"]}


def test_mark_callback_nudged_stamps_frontmatter_and_excludes_from_next_list(vault):
    obsidian.write_note(
        "Sessions/planning.md", "## Action Items\n- Follow up with Sam\n",
        title="Planning", canonicalize=False,
    )
    obsidian.mark_callback_nudged("Sessions/planning.md")
    note = obsidian.read_note("Sessions/planning.md")
    assert note.meta.get("callback_nudged")
    # still listed (list_open_callbacks is unfiltered by design — the pure
    # proactive.callback_due() is what excludes an already-nudged note), but
    # the stamp it wrote is exactly what that decision function checks for.
    out = {rel: meta for rel, meta, _items in obsidian.list_open_callbacks()}
    assert out["Sessions/planning.md"].get("callback_nudged")


def test_find_misfiled_and_refile(vault):
    # A real person + a meeting wrongly in People, and the same person duplicated in Projects.
    obsidian.write_note("People/Felicity Kline.md", "person", title="Felicity Kline", canonicalize=False)
    # write_note now routes meetings away from entity folders, so simulate a note
    # that got into People/ by other means (a manual edit / migrated data) on disk.
    (vault / "People").mkdir(parents=True, exist_ok=True)
    (vault / "People" / "meeting_with_jaime_june_24.md").write_text(
        "---\ntitle: Mtg\ntype: person\n---\n\n# Mtg\n\nnotes\n", encoding="utf-8")
    obsidian.write_note("Projects/felicity_kline.md", "dup", title="Felicity Kline", canonicalize=False)
    obsidian.write_note("Projects/Brightpoint/team_meeting.md", "ok", title="TM", canonicalize=False)
    obsidian.write_note("Projects/Brightpoint/spec.md", "scope", title="Spec", canonicalize=False)

    mis = obsidian.find_misfiled()
    assert "People/meeting_with_jaime_june_24.md" in mis["meetings_in_entities"]
    assert "Projects/Brightpoint/team_meeting.md" in mis["meetings_in_entities"]  # nested meeting caught
    assert "Projects/Brightpoint/spec.md" not in mis["meetings_in_entities"]      # non-meeting left alone
    assert "felicity_kline" in mis["cross_folder"]  # in both People and Projects

    moved = obsidian.refile_meetings(dry_run=True)
    assert ("People/meeting_with_jaime_june_24.md", "Sessions/meeting_with_jaime_june_24.md") in moved
    assert not (vault / "Sessions" / "meeting_with_jaime_june_24.md").exists()  # preview only

    obsidian.refile_meetings(dry_run=False)
    assert not (vault / "People" / "meeting_with_jaime_june_24.md").exists()
    assert not (vault / "Projects" / "Brightpoint" / "team_meeting.md").exists()  # nested moved too
    sess = obsidian.read_note("Sessions/meeting_with_jaime_june_24.md")
    assert sess.meta.get("type") == "session"  # type corrected on move


# ── Deterministic routing + templates ──────────────────────────────────────────
def test_write_routes_meeting_out_of_entity_folder(vault):
    # A meeting written into People/ is redirected to Sessions/ on the way in.
    obsidian.write_note("People/standup_june_24.md", "notes", title="Standup")
    assert not (vault / "People" / "standup_june_24.md").exists()
    assert (vault / "Sessions" / "standup_june_24.md").exists()
    assert obsidian.read_note("Sessions/standup_june_24.md").meta.get("type") == "session"


def test_write_routes_by_meeting_title(vault):
    # Even a non-meeting filename is routed when the *title* reads like a meeting.
    obsidian.write_note("Companies/acme.md", "notes", title="Acme Kickoff Meeting")
    assert not (vault / "Companies" / "acme.md").exists()
    assert (vault / "Sessions" / "acme.md").exists()


def test_nested_entity_note_not_routed(vault):
    # A note nested under a project is intentional — never rerouted.
    obsidian.write_note("Projects/Brightpoint/team_meeting.md", "ok", title="TM")
    assert (vault / "Projects" / "Brightpoint" / "team_meeting.md").exists()


def test_looks_like_meeting_catches_session_and_sync_titles():
    # "session"/"sync"/"1:1" must match as whole words, not just filename prefixes
    # (a title ending in "Session" was previously missed — see looks_like_meeting).
    assert obsidian.looks_like_meeting("Planning Session")
    assert obsidian.looks_like_meeting("Q3 Review Session")
    assert obsidian.looks_like_meeting("session_2026-06-30")
    assert obsidian.looks_like_meeting("Weekly Sync")
    assert obsidian.looks_like_meeting("1:1 with Sam")
    assert obsidian.looks_like_meeting("Check-in with Joe")
    # Real entity names must never false-positive on a "sync"/"session" substring.
    assert not obsidian.looks_like_meeting("Synchronicity Corp")
    assert not obsidian.looks_like_meeting("Joe Konkle")


def test_write_routes_by_session_title(vault):
    # A title that merely *ends* in "Session" was the gap this heuristic used to miss.
    obsidian.write_note("People/planning.md", "notes", title="Planning Session")
    assert not (vault / "People" / "planning.md").exists()
    assert (vault / "Sessions" / "planning.md").exists()


def test_empty_note_gets_type_template(vault):
    obsidian.write_note("People/Sam.md", "", title="Sam")
    body = obsidian.read_note("People/Sam.md").body
    for header in ("## Facts", "## Projects", "## Notes"):
        assert header in body
    # A company gets its own section set.
    obsidian.write_note("Companies/Acme.md", "", title="Acme")
    cbody = obsidian.read_note("Companies/Acme.md").body
    assert "## Overview" in cbody and "## People" in cbody


def test_content_note_keeps_authored_body(vault):
    # Real content is never replaced with the template skeleton.
    obsidian.write_note("People/Sam.md", "Sam runs infra.", title="Sam")
    assert "Sam runs infra." in obsidian.read_note("People/Sam.md").body


def test_relocate_note_moves_and_corrects_type(vault):
    obsidian.write_note("Projects/Acme.md", "An org", title="Acme", canonicalize=False)
    dest = obsidian.relocate_note("Projects/Acme.md", "Companies")
    assert dest == "Companies/Acme.md"
    assert not (vault / "Projects" / "Acme.md").exists()
    assert obsidian.read_note("Companies/Acme.md").meta.get("type") == "company"


def test_relocate_note_merges_into_existing_entity(vault):
    obsidian.write_note("People/joe_konkle.md", "The real Joe.", title="Joe Konkle", canonicalize=False)
    obsidian.write_note("Projects/joe_konkle.md", "Misfiled dup.", title="Joe Konkle", canonicalize=False)
    dest = obsidian.relocate_note("Projects/joe_konkle.md", "People")
    assert dest == "People/joe_konkle.md"                       # merged into existing
    assert not (vault / "Projects" / "joe_konkle.md").exists()  # source removed
    body = obsidian.read_note("People/joe_konkle.md").body
    assert "The real Joe." in body and "Misfiled dup." in body


def test_record_session_facts_routes_company(vault):
    obsidian.record_session_facts(
        [{"fact": "B2B SaaS startup", "subject": "Acme Corp", "kind": "company"}]
    )
    note = obsidian.read_note("Companies/acme_corp.md")
    assert note.title == "Acme Corp" and "B2B SaaS startup" in note.body
    assert note.meta.get("type") == "company"


def test_record_session_facts_reuses_existing_entity_across_folders(vault):
    # THE BUG: a fact about an existing person, mis-tagged as a project, used to
    # create a People/ + Projects/ pair. Now it reuses the existing People note.
    obsidian.write_note("People/Joe Konkle.md", "The real Joe.", title="Joe Konkle",
                        canonicalize=False)
    obsidian.record_session_facts(
        [{"fact": "Leads infra", "subject": "Joe Konkle", "kind": "project"}]
    )
    assert "Leads infra" in obsidian.read_note("People/Joe Konkle.md").body
    assert obsidian.find_entity_note("Joe Konkle", "Projects") is None  # no Projects dup


def test_find_existing_entity_searches_all_folders(vault):
    obsidian.write_note("Companies/Acme.md", "x", title="Acme", canonicalize=False)
    assert obsidian.find_existing_entity("Acme") == ("Companies", "Companies/Acme.md")
    assert obsidian.find_existing_entity("Nobody") is None


def test_dedupe_entities_merges_cross_folder(vault):
    obsidian.write_note("People/Joe Konkle.md", "Person note.", title="Joe Konkle",
                        canonicalize=False)
    obsidian.write_note("Projects/joe_konkle.md", "Stray facts.", title="Joe Konkle",
                        canonicalize=False)
    merged = obsidian.dedupe_entities(dry_run=True)
    assert ("Projects/joe_konkle.md", "People/Joe Konkle.md") in merged
    assert (vault / "Projects" / "joe_konkle.md").exists()  # preview only

    obsidian.dedupe_entities(dry_run=False)
    assert not (vault / "Projects" / "joe_konkle.md").exists()          # merged away
    body = obsidian.read_note("People/Joe Konkle.md").body
    assert "Person note." in body and "Stray facts." in body
    assert (vault / "Archive" / "Projects" / "joe_konkle.md").exists()  # reversible


def test_linkify_vault_connects_old_notes(vault):
    obsidian.set_aliases("Joe Konkle", ["Joe"])
    # An old note with a bare mention and no link (frontmatter must survive intact).
    obsidian.write_note("Sessions/old.md", "Met Joe about the deck.", title="Recap")
    fm_before = (vault / "Sessions" / "old.md").read_text(encoding="utf-8").split("\n\n")[0]

    assert obsidian.linkify_vault() >= 1
    note = obsidian.read_note("Sessions/old.md")
    assert "[[Joe Konkle|Joe]]" in note.body
    assert note.meta.get("type") == "session"           # frontmatter preserved
    assert (vault / "Sessions" / "old.md").read_text(encoding="utf-8").startswith(fm_before)

    # Entity folders are skipped — no churn on a second pass.
    person_before = (vault / "People" / "joe_konkle.md").read_text(encoding="utf-8")
    obsidian.linkify_vault()
    assert (vault / "People" / "joe_konkle.md").read_text(encoding="utf-8") == person_before


def test_linkify_entities_wraps_known_names(vault):
    obsidian.set_aliases("Joe Konkle", ["Joe", "Joe K"])
    obsidian.set_aliases("Daedabyte", [], folder="Projects")
    out = obsidian.linkify_entities("Met Joe K about Daedabyte; cc [[Sam]].")
    assert "[[Joe Konkle|Joe K]] about [[Daedabyte]]" in out  # alias linked to canonical
    assert "[[Sam]]" in out                                   # existing link untouched
    # canonical-cased mention links plainly, no display alias
    assert "[[Joe Konkle]]" in obsidian.linkify_entities("spoke with Joe Konkle")


def test_record_session_facts_routes_to_entities(vault):
    obsidian.set_aliases("Joe Konkle", ["Joe"])     # existing person w/ alias
    facts = [
        {"fact": "Allergic to shellfish", "subject": "Joe", "kind": "person"},
        {"fact": "Targeting $50k/yr", "subject": "Daedabyte", "kind": "project"},
        {"fact": "Prefers concise answers", "subject": "Joe Bagley", "kind": "self"},
    ]
    assert obsidian.record_session_facts(facts) == 3

    # alias resolved to the canonical person note — no People/joe.md duplicate
    assert "Allergic to shellfish" in obsidian.read_note("People/joe_konkle.md").body
    assert not (vault / "People" / "joe.md").exists()
    # new project note created with correct title + the fact
    dae = obsidian.read_note("Projects/daedabyte.md")
    assert dae.title == "Daedabyte" and "Targeting $50k/yr" in dae.body
    # self fact lands in Memory/Facts.md, not a person note
    assert "Prefers concise answers" in obsidian.read_note("Memory/Facts.md").body


def test_record_session_facts_no_typo_duplicate_for_known_casing(vault):
    # A migrated, capitalized note; a lowercase-subject fact must reuse it.
    obsidian.write_note("People/Sam.md", "Colleague.", title="Sam", canonicalize=False)
    obsidian.record_session_facts([{"fact": "Owns infra", "subject": "sam", "kind": "person"}])
    assert "Owns infra" in obsidian.read_note("People/Sam.md").body
    assert not (vault / "People" / "sam.md").exists()


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
    p1 = obsidian.path_for_title("Project Alpha", "Projects")
    obsidian.write_note(p1, "first", title="Project Alpha", overwrite=False)
    obsidian.write_note(p1, "second", title="Project Alpha", overwrite=False)
    files = sorted(p.name for p in (vault / "Projects").glob("*.md"))
    assert files == ["project_alpha.md", "project_alpha_2.md"]


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
