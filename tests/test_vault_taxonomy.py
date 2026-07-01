"""Tests for the config-driven vault taxonomy (app/vault_taxonomy.py)."""

from __future__ import annotations

import json

import pytest

from app import vault_taxonomy as vt


@pytest.fixture(autouse=True)
def _fresh_taxonomy():
    vt.reload()
    yield
    vt.reload()  # runs after monkeypatch restores _CONFIG_FILE → back to defaults


def test_defaults():
    assert {"People", "Companies", "Projects", "Ideas", "Maps"} <= set(vt.folders())
    assert set(vt.entity_folders()) == {"People", "Companies", "Projects"}
    assert vt.type_for_folder("People") == "person"
    assert vt.type_for_folder("Companies") == "company"
    assert vt.type_for_folder("Ideas") == "idea"
    assert vt.type_for_folder("Nonexistent") == "note"
    assert ("People", "#4caf79") in vt.color_groups()
    assert "Archive" in vt.skip_folders()


def test_config_extends_and_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "vault_config.json"
    cfg.write_text(json.dumps({
        "folders": [
            {"folder": "People", "color": "#111111"},                       # override
            {"folder": "Books", "type": "book", "entity": True, "color": "#222222"},  # new
        ],
        "skip": ["Archive", "Scratch"],
    }), encoding="utf-8")
    monkeypatch.setattr(vt, "_CONFIG_FILE", cfg)
    vt.reload()

    assert "Books" in vt.folders()                       # new category, no code change
    assert "Books" in vt.entity_folders()
    assert vt.type_for_folder("Books") == "book"
    assert vt.color_for_folder("People") == "#111111"    # override applied
    assert vt.skip_folders() == {"Archive", "Scratch"}


def test_icons():
    assert vt.icon_for_folder("People") == "👤"
    assert vt.icon_for_type("project") == "🚀"
    assert vt.icon_for_folder("Nonexistent") == "📄"      # default fallback
    assert vt.icon_for_type("nonexistent") == "📄"


def test_icon_falls_back_when_config_omits_it(tmp_path, monkeypatch):
    cfg = tmp_path / "vault_config.json"
    cfg.write_text(json.dumps({"folders": [{"folder": "Books", "type": "book"}]}),
                    encoding="utf-8")
    monkeypatch.setattr(vt, "_CONFIG_FILE", cfg)
    vt.reload()
    assert vt.icon_for_folder("Books") == "📄"
