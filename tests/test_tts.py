"""Tests for the TTS text-cleanup helpers (app/tts.py).

Most of this file covers the pure string functions — no sounddevice/numpy/
network, so these run in the light CI environment like the rest of the suite.

Speaker.wait_idle()'s clear/set bookkeeping is also covered: constructing a
Speaker via __new__ (skipping __init__'s hardware probe entirely) and giving
it a fake backend. _play() itself already degrades gracefully when
sounddevice is missing (logs and returns), so this exercises the real
speak()/start_utterance()/feed()/finish() code paths end to end without
needing real audio hardware.
"""

from __future__ import annotations

import threading
import time

from app.tts import Speaker, _normalize_for_speech, _strip_markdown, split_ready_sentences


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


# ── Speaker.wait_idle() ───────────────────────────────────────────────────────

class _FakeBackend:
    """synth() takes ~delay seconds (so wait_idle()'s timing is observable),
    polling stop_event like the real backends do so barge-in interrupts
    promptly instead of waiting out the full delay."""

    def __init__(self, delay: float = 0.05):
        self.delay = delay

    def synth(self, text, stop_event):
        waited, step = 0.0, 0.01
        while waited < self.delay:
            if stop_event.is_set():
                return None, 0
            time.sleep(step)
            waited += step
        return [0, 0, 0], 16000


def _bare_speaker(delay: float = 0.05) -> Speaker:
    """A Speaker with __init__'s hardware probe skipped entirely, so this
    works regardless of whether sounddevice/numpy are installed."""
    speaker = Speaker.__new__(Speaker)
    speaker._stop_event = None
    speaker._thread = None
    speaker._backend = _FakeBackend(delay)
    speaker._available = True
    speaker._buffer = ""
    speaker._sentence_queue = None
    speaker._done = threading.Event()
    speaker._done.set()
    speaker._speech_generation = 0
    return speaker


def test_wait_idle_blocks_until_one_shot_speech_finishes():
    speaker = _bare_speaker(delay=0.1)
    speaker.speak("hello there")
    assert not speaker._done.is_set()  # synthesis is still "in flight"
    speaker.wait_idle(timeout=2)
    assert speaker._done.is_set()


def test_wait_idle_blocks_until_streamed_utterance_finishes():
    speaker = _bare_speaker(delay=0.1)
    speaker.start_utterance()
    speaker.feed("Hello there. ")
    assert not speaker._done.is_set()
    speaker.finish()
    speaker.wait_idle(timeout=2)
    assert speaker._done.is_set()


def test_wait_idle_returns_immediately_when_nothing_speaking():
    speaker = _bare_speaker()
    start = time.monotonic()
    speaker.wait_idle(timeout=2)
    assert time.monotonic() - start < 0.5


def test_stop_marks_done_even_when_interrupted_mid_utterance():
    speaker = _bare_speaker(delay=5.0)  # long enough to still be "speaking"
    speaker.start_utterance()
    speaker.feed("This will be interrupted. ")
    assert not speaker._done.is_set()
    speaker.stop()
    speaker.wait_idle(timeout=2)  # interrupts promptly, not after the full 5s
    assert speaker._done.is_set()


# ── Speech-generation race (the wake-word "never stops responding" bug) ─────
# overlay.py's on_reset restarts speech mid-turn (Claude discarding pre-tool
# narration once it decides to call a tool — common on wake-word-triggered
# questions like "what's on my calendar", which is exactly why this hit hard
# via voice): start_utterance() gets called twice in one turn. The first
# call's background thread is stopped but isn't guaranteed to notice
# promptly; if its `finally` block reached `_done.set()` unconditionally, it
# could mark speech "done" while the *second, real* utterance was still
# actively playing, letting wait_idle() (and therefore wake-word resume)
# fire early and reopen the mic mid-speech -> JARVIS's own voice re-triggers
# "Hey JARVIS" -> the loop the user reported never stopping.

class _SlowUnstoppableBackend:
    """Ignores stop_event entirely and always sleeps the full delay — stands
    in for a stopped, superseded utterance's synth call that's slow to
    notice it was cancelled, so its thread's `finally` can land well after a
    newer utterance has already started speaking. Sets `started` the moment
    synth() is actually entered, so a test can wait on that instead of
    racing a fixed sleep against thread scheduling."""

    def __init__(self, delay: float):
        self.delay = delay
        self.started = threading.Event()

    def synth(self, text, stop_event):
        self.started.set()
        time.sleep(self.delay)
        return [0, 0, 0], 16000


def test_superseded_streamed_utterance_cannot_mark_a_newer_one_done_early():
    speaker = _bare_speaker()
    stale_backend = _SlowUnstoppableBackend(delay=0.05)
    speaker._backend = stale_backend
    speaker.start_utterance()  # generation 1
    speaker.feed("First. ")
    assert stale_backend.started.wait(timeout=2)  # gen 1 has committed to its slow synth

    speaker._backend = _FakeBackend(delay=0.3)
    speaker.start_utterance()  # generation 2 — stops gen 1, which ignores it
    speaker.feed("Second. ")
    speaker.finish()

    time.sleep(0.15)  # gen 1's 0.05s synth has long since returned; gen 2 (0.3s) still speaking
    assert not speaker._done.is_set(), "a superseded utterance marked speech done early"

    speaker.wait_idle(timeout=2)
    assert speaker._done.is_set()


def test_superseded_one_shot_speak_cannot_mark_a_newer_one_done_early():
    speaker = _bare_speaker()
    stale_backend = _SlowUnstoppableBackend(delay=0.05)
    speaker._backend = stale_backend
    speaker.speak("First")  # generation 1
    assert stale_backend.started.wait(timeout=2)

    speaker._backend = _FakeBackend(delay=0.3)
    speaker.speak("Second")  # generation 2 — stops gen 1, which ignores it

    time.sleep(0.15)
    assert not speaker._done.is_set(), "a superseded speak() call marked speech done early"

    speaker.wait_idle(timeout=2)
    assert speaker._done.is_set()
