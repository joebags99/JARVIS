"""Tests for user-configurable note/task categories (Phase 2)."""

from __future__ import annotations

import json

import pytest

from app import config as cfg
from app.config import CONFIG, DEFAULT_CATEGORIES, Config
from integrations import notes_watcher


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    """Point the user-config file + notes dir at a throwaway location."""
    monkeypatch.setattr(cfg, "USER_CONFIG_FILE", tmp_path / "jarvis_config.json")
    monkeypatch.setattr(cfg, "NOTES_DIR", tmp_path / "notes")
    return tmp_path


def test_default_categories_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_CATEGORIES", raising=False)
    assert Config().categories == DEFAULT_CATEGORIES


def test_categories_parsed_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_CATEGORIES", "Work, Personal ,Side Project")
    assert Config().categories == ["Work", "Personal", "Side Project"]


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_CATEGORIES", "   ")
    assert Config().categories == DEFAULT_CATEGORIES


def test_resolve_category_reads_config_at_call_time(monkeypatch):
    monkeypatch.setattr(CONFIG, "categories", ["Work", "Home"])
    assert notes_watcher._resolve_category("work") == "Work"
    assert notes_watcher._resolve_category("HOME") == "Home"


def test_resolve_unknown_category_raises(monkeypatch):
    monkeypatch.setattr(CONFIG, "categories", ["Work", "Home"])
    with pytest.raises(ValueError, match="Unknown notes category"):
        notes_watcher._resolve_category("Daedabyte")


# ── Settings-panel persistence (jarvis_config.json) ──────────────────────────

def test_save_categories_cleans_and_persists(isolated_store):
    c = Config()
    out = c.save_categories(["Work", " work ", "Home", "", "music", "Music"])
    # blanks dropped; case-insensitive dupes removed; order + first-seen casing kept
    assert out == ["Work", "Home", "music"]
    assert c.categories == ["Work", "Home", "music"]
    saved = json.loads(cfg.USER_CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["categories"] == ["Work", "Home", "music"]
    # new category folders created
    assert (cfg.NOTES_DIR / "Work").is_dir()


def test_save_categories_rejects_empty(isolated_store):
    c = Config()
    with pytest.raises(ValueError, match="At least one category"):
        c.save_categories(["  ", ""])


def test_resolve_prefers_user_config_over_env(isolated_store, monkeypatch):
    monkeypatch.setenv("JARVIS_CATEGORIES", "EnvA,EnvB")
    cfg.USER_CONFIG_FILE.write_text(
        json.dumps({"categories": ["FileA", "FileB"]}), encoding="utf-8"
    )
    assert cfg._resolve_categories() == ["FileA", "FileB"]


def test_resolve_falls_back_to_env_when_no_file(isolated_store, monkeypatch):
    monkeypatch.setenv("JARVIS_CATEGORIES", "EnvA,EnvB")
    assert cfg._resolve_categories() == ["EnvA", "EnvB"]


def test_read_user_config_missing_returns_empty(isolated_store):
    assert cfg._read_user_config() == {}
