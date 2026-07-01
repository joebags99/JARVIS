"""Tests for the TTS text-cleanup helpers (app/tts.py).

Only the pure string functions are covered here — no sounddevice/numpy/network,
so these run in the light CI environment like the rest of the suite.
"""

from __future__ import annotations

from app.tts import _normalize_for_speech, _strip_markdown


# ── _normalize_for_speech ────────────────────────────────────────────────────
def test_degree_fahrenheit_and_celsius():
    assert _normalize_for_speech("72°F") == "72 degrees Fahrenheit"
    assert _normalize_for_speech("22°C") == "22 degrees Celsius"


def test_bare_degree_symbol():
    assert _normalize_for_speech("high 75° / low 60°") == "high 75 degrees / low 60 degrees"


def test_em_and_en_dash_become_a_pause():
    assert _normalize_for_speech("done — for now") == "done, for now"
    assert _normalize_for_speech("a–b") == "a, b"


def test_spaced_double_hyphen_becomes_a_pause():
    assert _normalize_for_speech("done -- for now") == "done, for now"


def test_numeric_range_hyphen_becomes_to():
    assert _normalize_for_speech("70-75°F") == "70 to 75 degrees Fahrenheit"


def test_hyphenated_word_is_untouched():
    # A genuine compound word's hyphen isn't a range or a clause break.
    assert _normalize_for_speech("well-being") == "well-being"


def test_empty_string():
    assert _normalize_for_speech("") == ""


# ── _strip_markdown (existing behavior, still covered) ───────────────────────
def test_strip_markdown_removes_formatting():
    text = "**bold** and *italic* and `code` and [link](http://x.com)"
    assert _strip_markdown(text) == "bold and italic and code and link"


def test_strip_markdown_keeps_degree_and_hyphen_for_normalize_to_handle_first():
    # _strip_markdown alone still preserves ° and - (Speaker._run normalizes
    # them first); this pins that contract so the call order stays correct.
    assert "°" in _strip_markdown("72°F")
    assert "-" in _strip_markdown("well-being")


def test_strip_markdown_drops_emoji():
    assert "🎉" not in _strip_markdown("Great work! 🎉")
