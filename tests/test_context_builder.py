"""Tests for system-prompt assembly (app/context_builder.py)."""

from __future__ import annotations

from app.config import CONFIG
from app.context_builder import DEFAULT_PERSONA, ContextBuilder


def _builder(cache):
    cb = ContextBuilder()
    cb._static_cache = cache
    return cb


def test_two_blocks_stable_and_volatile():
    cb = _builder({
        "profile.md": "I am Joe.",
        "persona.md": "Be terse.",
        "dnd.md": "Campaign notes.",
    })
    blocks = cb.build_system_prompt()
    assert len(blocks) == 2

    stable, volatile = blocks
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "Be terse." in stable["text"]
    assert "I am Joe." in stable["text"]
    assert "Dnd" in stable["text"]  # extra context rendered with a title heading

    assert "cache_control" not in volatile
    assert "Today's Date & Time" in volatile["text"]
    assert "Voice Dials" in volatile["text"]


def test_persona_falls_back_to_default():
    cb = _builder({})
    blocks = cb.build_system_prompt()
    assert DEFAULT_PERSONA in blocks[0]["text"]


def test_truncate_caps_length(monkeypatch):
    cb = ContextBuilder()
    monkeypatch.setattr(CONFIG, "max_context_chars", 10)
    out = cb._truncate("x" * 100)
    assert out.startswith("x" * 10)
    assert "context truncated" in out


def test_truncate_keeps_short_prompt(monkeypatch):
    cb = ContextBuilder()
    monkeypatch.setattr(CONFIG, "max_context_chars", 1000)
    assert cb._truncate("short") == "short"
