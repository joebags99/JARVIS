"""Tests for the entity-consolidation pass (app/vault_entities.py).

Covers both kinds (people + companies/projects). The Claude clustering call is
injected as a fake ``clusterer`` so these run offline and token-free.
"""

from __future__ import annotations

import pytest

from app import vault_entities, vault_index, vault_people
from app.config import CONFIG
from app.vault_index import VaultIndex


@pytest.fixture
def ent_vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", str(root))
    monkeypatch.setattr(CONFIG, "obsidian_enabled", True)
    monkeypatch.setattr(vault_index, "_INDEX", VaultIndex(tmp_path / "idx.db"))
    from integrations import obsidian

    obsidian.ensure_scaffold()
    return root, obsidian


def _clusterer(kind, _names):
    if kind == "people":
        return [{"canonical": "Joe Konkle", "aliases": ["Joe", "Joe K"]}]
    if kind == "projects":
        return [{"canonical": "Daedabyte", "aliases": ["Databyte"]}]
    return []


# ── Parsing / gathering ─────────────────────────────────────────────────────────
def test_parse_clusters_drops_singletons_and_self_aliases():
    text = (
        '{"clusters":['
        '{"canonical":"Daedabyte","aliases":["Databyte","Daedabyte"]},'
        '{"canonical":"Solo","aliases":[]}]}'
    )
    clusters = vault_entities.parse_clusters(text)
    assert clusters == [{"canonical": "Daedabyte", "aliases": ["Databyte"]}]


def test_gather_candidates_spans_folders_and_links(ent_vault):
    _root, obsidian = ent_vault
    obsidian.write_note("People/Joe.md", "x", title="Joe", canonicalize=False)
    obsidian.write_note("Projects/Databyte.md", "co", title="Databyte", canonicalize=False)
    obsidian.write_note("Sessions/m.md", "[[Joe K]] at [[Daedabyte]]", title="M",
                        canonicalize=False)
    names = set(vault_entities.gather_candidates())
    assert {"Joe", "Databyte", "Joe K", "Daedabyte"} <= names


# ── Orchestration ──────────────────────────────────────────────────────────────
def test_apply_consolidates_people_and_projects(ent_vault):
    root, obsidian = ent_vault
    obsidian.write_note("People/Joe.md", "Joe is CAO.", title="Joe", canonicalize=False)
    obsidian.write_note("Projects/Databyte.md", "the company", title="Databyte",
                        canonicalize=False)
    obsidian.write_note("Sessions/m.md", "[[Joe K]] reviewed [[Databyte]] with [[Joe]]",
                        title="M", canonicalize=False)

    out = vault_entities.run(apply=True, clusterer=_clusterer)
    assert out["links_rewritten"] >= 1

    # Canonical notes carry the aliases.
    assert {"Joe", "Joe K"} <= set(
        obsidian.extract_aliases(obsidian.read_note("People/joe_konkle.md").meta)
    )
    assert "Databyte" in obsidian.extract_aliases(
        obsidian.read_note("Projects/daedabyte.md").meta
    )
    # Duplicates archived; links collapsed to canonical names across kinds.
    assert (root / "Archive" / "People" / "Joe.md").exists()
    assert (root / "Archive" / "Projects" / "Databyte.md").exists()
    text = (root / "Sessions" / "m.md").read_text(encoding="utf-8")
    assert "[[Joe Konkle]]" in text and "[[Daedabyte]]" in text
    assert "[[Databyte]]" not in text and "[[Joe K]]" not in text


def test_kind_filter_only_runs_requested(ent_vault):
    _root, obsidian = ent_vault
    obsidian.write_note("Sessions/m.md", "[[Databyte]] and [[Joe]]", title="M",
                        canonicalize=False)
    out = vault_entities.run(kinds=("projects",), apply=True, clusterer=_clusterer)
    assert "projects" in out["kinds"] and "people" not in out["kinds"]
    assert obsidian.find_entity_note("Daedabyte", "Projects")
    assert obsidian.find_entity_note("Joe Konkle", "People") is None


def test_preview_writes_nothing(ent_vault):
    root, obsidian = ent_vault
    obsidian.write_note("Sessions/m.md", "[[Databyte]]", title="M", canonicalize=False)
    out = vault_entities.run(apply=False, clusterer=_clusterer)
    assert out["kinds"]["projects"]["applied"] == []
    assert not (root / "Projects" / "daedabyte.md").exists()
    assert "[[Databyte]]" in (root / "Sessions" / "m.md").read_text(encoding="utf-8")


def test_vault_people_shim_forces_people_kind(ent_vault, monkeypatch, capsys):
    # The shim delegates to vault_entities with --kind people (preview, no API path).
    monkeypatch.setattr(CONFIG, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(
        vault_entities, "_default_clusterer", lambda model: _clusterer
    )
    assert vault_people.main([]) == 0
    out = capsys.readouterr().out
    assert "people" in out and "companies & projects" not in out
