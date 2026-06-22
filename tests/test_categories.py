"""Tests for user-configurable note/task categories (Phase 2)."""

from __future__ import annotations

import pytest

from app.config import CONFIG, DEFAULT_CATEGORIES, Config
from integrations import notes_watcher


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
