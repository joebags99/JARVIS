"""Tests for the LLM-driven vault cleanup (app/vault_organize.py).

The Claude call is injected as a fake ``proposer`` so these run offline and
token-free; only the orchestration (preview vs apply, archiving, indexing) and the
JSON parsing are exercised.
"""

from __future__ import annotations

import pytest

from app import vault_index, vault_organize
from app.config import CONFIG
from app.vault_index import VaultIndex


@pytest.fixture
def org_vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", str(root))
    monkeypatch.setattr(CONFIG, "obsidian_enabled", True)
    monkeypatch.setattr(vault_index, "_INDEX", VaultIndex(tmp_path / "idx.db"))
    from integrations import obsidian

    obsidian.ensure_scaffold()
    (root / "Imported" / "Daedabyte").mkdir(parents=True, exist_ok=True)
    (root / "Imported" / "Daedabyte" / "raw.md").write_text(
        "messy notes: talked budget with Sam", encoding="utf-8"
    )
    obsidian.reindex()
    return root, obsidian


def _fake(rel, raw):
    return vault_organize.Proposal(
        source=rel,
        target="Projects/Daedabyte/q3-sync.md",
        title="Q3 Sync",
        body="## Summary\nTalked budget with [[Sam Rivera]].",
        tags=["meeting", "daedabyte"],
    )


# ── JSON parsing ───────────────────────────────────────────────────────────────
def test_parse_proposal_plain_json():
    p = vault_organize.parse_proposal(
        "Imported/x.md",
        '{"folder":"Topics","title":"SEO Basics","tags":["seo"],"body":"## A\\ntext"}',
    )
    assert p.target == "Topics/seo_basics.md"
    assert p.tags == ["seo"]
    assert p.title == "SEO Basics"


def test_parse_proposal_fenced_json():
    text = '```json\n{"folder":"People","title":"Sam","body":"hi"}\n```'
    assert vault_organize.parse_proposal("Imported/y.md", text).target == "People/sam.md"


def test_parse_proposal_empty_body_raises():
    with pytest.raises(ValueError):
        vault_organize.parse_proposal(
            "Imported/z.md", '{"folder":"Topics","title":"T","body":""}'
        )


# ── Orchestration ──────────────────────────────────────────────────────────────
def test_preview_writes_nothing(org_vault):
    root, _ = org_vault
    out = vault_organize.run(folder="Imported", apply=False, proposer=_fake)
    assert len(out["planned"]) == 1
    assert out["applied"] == []
    assert (root / "Imported" / "Daedabyte" / "raw.md").exists()
    assert not (root / "Projects" / "Daedabyte" / "q3-sync.md").exists()
    assert not (root / "Archive").exists()


def test_apply_refiles_and_archives(org_vault):
    root, obsidian = org_vault
    out = vault_organize.run(folder="Imported", apply=True, proposer=_fake)
    assert len(out["applied"]) == 1

    tidied = root / "Projects" / "Daedabyte" / "q3-sync.md"
    assert tidied.exists()
    raw = tidied.read_text(encoding="utf-8")
    assert raw.startswith("---")            # frontmatter stamped
    assert "[[Sam Rivera]]" in raw          # wikilink preserved

    # Original archived (not deleted), gone from Imported/.
    assert not (root / "Imported" / "Daedabyte" / "raw.md").exists()
    assert (root / "Archive" / "Imported" / "Daedabyte" / "raw.md").exists()

    # Search finds the tidied note, never the archived original.
    hits = obsidian.search("budget")
    assert any("Projects/Daedabyte" in h.path for h in hits)
    assert all(not h.path.startswith("Archive/") for h in hits)


def test_limit(org_vault):
    root, obsidian = org_vault
    (root / "Imported" / "Daedabyte" / "raw2.md").write_text("more", encoding="utf-8")
    out = vault_organize.run(folder="Imported", apply=False, limit=1, proposer=_fake)
    assert len(out["planned"]) == 1


def test_proposer_error_is_collected_not_raised(org_vault):
    def boom(rel, raw):
        raise ValueError("model said no")

    out = vault_organize.run(folder="Imported", apply=True, proposer=boom)
    assert out["planned"] == []
    assert out["applied"] == []
    assert len(out["errors"]) == 1
