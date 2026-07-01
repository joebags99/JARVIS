"""Tests for the TTS text-cleanup helpers (app/tts.py).

Only the pure string functions are covered here — no sounddevice/numpy/network,
so these run in the light CI environment like the rest of the suite.
"""

from __future__ import annotations

from app.tts import _normalize_for_speech, _strip_markdown, split_ready_sentences


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


# ── split_ready_sentences (streamed speech) ──────────────────────────────────
def test_splits_multiple_complete_sentences():
    sentences, tail = split_ready_sentences("Hello there. How are you? I'm fine!")
    assert sentences == ["Hello there.", "How are you?"]
    assert tail == "I'm fine!"  # no trailing whitespace yet -> stays buffered


def test_incomplete_sentence_stays_in_tail():
    sentences, tail = split_ready_sentences("Just getting star")
    assert sentences == []
    assert tail == "Just getting star"


def test_feeding_more_text_completes_the_tail():
    _, tail = split_ready_sentences("The weather today is ")
    sentences, tail = split_ready_sentences(tail + "sunny. Enjoy!")
    assert sentences == ["The weather today is sunny."]
    assert tail == "Enjoy!"


def test_abbreviation_does_not_split():
    sentences, tail = split_ready_sentences("I met Dr. Smith today. He was nice.")
    assert sentences == ["I met Dr. Smith today."]
    assert tail == "He was nice."


def test_decimal_number_never_splits():
    # No whitespace between '.' and the next digit, so the boundary regex
    # can't match there regardless of what streamed chunk it arrived in.
    sentences, tail = split_ready_sentences("The value is 3.5 today. Next line.")
    assert sentences == ["The value is 3.5 today."]
    assert tail == "Next line."


def test_digit_then_space_then_digit_still_splits():
    # Not a decimal (there's a real space) -> a genuine sentence boundary. The
    # second sentence has no trailing whitespace yet, so it stays in the tail
    # (as any not-yet-terminated sentence does) rather than being returned.
    sentences, tail = split_ready_sentences("The total is 12. 5 items remain.")
    assert sentences == ["The total is 12."]
    assert tail == "5 items remain."


def test_blank_line_is_a_boundary():
    sentences, tail = split_ready_sentences("First point\n\nSecond point starts")
    assert sentences == ["First point"]
    assert tail == "Second point starts"


def test_question_and_exclamation_marks_split():
    sentences, tail = split_ready_sentences("Really? Yes! Are you sure? ")
    assert sentences == ["Really?", "Yes!", "Are you sure?"]
    assert tail == ""


def test_empty_buffer():
    assert split_ready_sentences("") == ([], "")
