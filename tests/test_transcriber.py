"""Tests for the transcription hallucination guard (app/transcriber.py).

Only _filter_hallucinated_segments() is covered — it's pure and needs no
faster-whisper model, just objects with a `no_speech_prob` attribute (a
real Segment's actual shape). faster-whisper itself is a heavy ML
dependency not installed in this environment, same reason the rest of the
audio pipeline has no coverage here.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.transcriber import _filter_hallucinated_segments


def _seg(text: str, no_speech_prob: float) -> SimpleNamespace:
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob)


def test_keeps_confident_speech_segments():
    segments = [_seg("hello there", 0.05), _seg("how are you", 0.1)]
    assert _filter_hallucinated_segments(segments, cutoff=0.6) == segments


def test_drops_high_no_speech_probability_segments():
    segments = [
        _seg("real speech", 0.1),
        _seg("Ederia Raynere Zephoros Marik", 0.95),  # hallucinated on silence
    ]
    kept = _filter_hallucinated_segments(segments, cutoff=0.6)
    assert [s.text for s in kept] == ["real speech"]


def test_drops_all_when_entirely_silence():
    segments = [_seg("Thanks for watching!", 0.99)]
    assert _filter_hallucinated_segments(segments, cutoff=0.6) == []


def test_cutoff_is_exclusive_boundary():
    segments = [_seg("borderline", 0.6)]
    assert _filter_hallucinated_segments(segments, cutoff=0.6) == []
    assert _filter_hallucinated_segments(segments, cutoff=0.61) == segments


def test_missing_attribute_defaults_to_kept():
    # A future faster-whisper field rename shouldn't silently drop everything.
    segments = [SimpleNamespace(text="no no_speech_prob field")]
    assert _filter_hallucinated_segments(segments, cutoff=0.6) == segments


def test_empty_list():
    assert _filter_hallucinated_segments([], cutoff=0.6) == []
