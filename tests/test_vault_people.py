"""Tests for the people-consolidation pass (app/vault_people.py).

The Claude clustering call is injected as a fake ``clusterer`` so these run
offline and token-free.
"""

from __future__ import annotations

from app import vault_index, vault_people
from app.config import CONFIG
from app.vault_index import VaultIndex
import pytest


@pytest.fixture
def people_vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", str(root))
    monkeypatch.setattr(CONFIG, "obsidian_enabled", True)
    monkeypatch.setattr(vault_index, "_INDEX", VaultIndex(tmp_path / "idx.db"))
    from integrations import obsidian

    obsidian.ensure_scaffold()
    return root, obsidian


def _clusterer(_names):
    return [{"canonical": "Joe Konkle", "aliases": ["Joe", "Joe K"]}]


# ── Parsing ────────────────────────────────────────────────────────────────────
def test_parse_clusters_filters_singletons_and_self_aliases():
    text = (
        '{"clusters":['
        '{"canonical":"Joe Konkle","aliases":["Joe","Joe K","Joe Konkle"]},'
        '{"canonical":"Sam Rivera","aliases":[]}]}'
    )
    clusters = vault_people.parse_clusters(text)
    assert len(clusters) == 1
    assert clusters[0]["canonical"] == "Joe Konkle"
    assert clusters[0]["aliases"] == ["Joe", "Joe K"]   # self-alias dropped


def test_gather_candidates_collects_links_and_people(people_vault):
    _root, obsidian = people_vault
    obsidian.write_note("People/Joe.md", "x", title="Joe", canonicalize=False)
    obsidian.write_note("Sessions/m.md", "with [[Joe K]] and [[Sam]]", title="M",
                        canonicalize=False)
    names = vault_people.gather_candidates()
    assert {"Joe", "Joe K", "Sam"} <= set(names)


# ── Orchestration ──────────────────────────────────────────────────────────────
def test_preview_writes_nothing(people_vault):
    root, obsidian = people_vault
    obsidian.write_note("Sessions/m.md", "[[Joe]] and [[Joe K]]", title="M",
                        canonicalize=False)
    out = vault_people.run(apply=False, clusterer=_clusterer)
    assert out["clusters"] and out["applied"] == []
    assert not (root / "People" / "joe_konkle.md").exists()
    assert "[[Joe]]" in (root / "Sessions" / "m.md").read_text(encoding="utf-8")


def test_apply_consolidates(people_vault):
    root, obsidian = people_vault
    obsidian.write_note("People/Joe.md", "Joe is CAO.", title="Joe", canonicalize=False)
    obsidian.write_note("People/Joe K.md", "second note", title="Joe K", canonicalize=False)
    obsidian.write_note("Sessions/m.md", "owner [[Joe K]], cc [[Joe]]", title="M",
                        canonicalize=False)

    out = vault_people.run(apply=True, clusterer=_clusterer)
    assert len(out["applied"]) == 1
    assert out["links_rewritten"] >= 1

    canon = obsidian.read_note("People/joe_konkle.md")
    assert "Joe Konkle" in (canon.meta.get("title") or canon.title)
    assert {"Joe", "Joe K"} <= set(obsidian.extract_aliases(canon.meta))
    # duplicate person notes archived, links collapsed onto the canonical name
    assert not (root / "People" / "Joe.md").exists()
    assert not (root / "People" / "Joe K.md").exists()
    text = (root / "Sessions" / "m.md").read_text(encoding="utf-8")
    assert "[[Joe Konkle]]" in text
    assert "[[Joe]]" not in text and "[[Joe K]]" not in text
