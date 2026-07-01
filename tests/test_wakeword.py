"""Tests for the wake-word logic (app/wakeword.py).

wake_score_triggers()/silence_auto_stop_due() are pure and fully covered.
watch_for_silence() only calls methods on whatever `recorder` it's given, so
it's tested here with a small fake — no real sounddevice/microphone needed.
WakeWordListener itself needs sounddevice + openwakeword (a native PortAudio
dependency not present in this environment, same reason the rest of the audio
pipeline has no coverage here); its `available` probe is checked instead,
since that's the one behavior a real run can't skip either.
"""

from __future__ import annotations

from app.wakeword import (
    WakeWordListener,
    silence_auto_stop_due,
    wake_score_triggers,
    watch_for_silence,
)


# ── Pure decision helpers ─────────────────────────────────────────────────────

def test_wake_score_triggers():
    assert wake_score_triggers(0.7, 0.5)
    assert wake_score_triggers(0.5, 0.5)  # threshold is inclusive
    assert not wake_score_triggers(0.3, 0.5)


def test_silence_auto_stop_due_on_enough_quiet_polls():
    assert silence_auto_stop_due(5, 5, elapsed_s=2.0, max_duration_s=12.0)
    assert not silence_auto_stop_due(4, 5, elapsed_s=2.0, max_duration_s=12.0)


def test_silence_auto_stop_due_on_max_duration():
    # Still talking (0 quiet polls) but the hard cap is hit.
    assert silence_auto_stop_due(0, 5, elapsed_s=12.0, max_duration_s=12.0)
    assert not silence_auto_stop_due(0, 5, elapsed_s=11.9, max_duration_s=12.0)


# ── watch_for_silence (duck-typed fake recorder, no real audio) ──────────────

class _FakeRecorder:
    def __init__(self, levels: list[float]):
        # Each current_level() call pops the next scripted value (last value
        # repeats once exhausted, so a short script still stops the loop).
        self._levels = levels
        self.is_recording = True
        self.calls = 0

    def current_level(self) -> float:
        self.calls += 1
        return self._levels.pop(0) if len(self._levels) > 1 else self._levels[0]


def test_watch_for_silence_stops_after_enough_quiet_polls():
    recorder = _FakeRecorder([50, 50, 2, 2, 2])  # loud, loud, then quiet
    fired = []
    thread = watch_for_silence(
        recorder, on_timeout=lambda: fired.append(1),
        poll_interval_s=0.01, silence_rms=15.0, required_quiet_polls=3,
        max_duration_s=5.0,
    )
    thread.join(timeout=2)
    assert fired == [1]


def test_watch_for_silence_hits_max_duration_even_if_never_quiet():
    recorder = _FakeRecorder([50])  # stays loud forever
    fired = []
    thread = watch_for_silence(
        recorder, on_timeout=lambda: fired.append(1),
        poll_interval_s=0.01, silence_rms=15.0, required_quiet_polls=100,
        max_duration_s=0.05,
    )
    thread.join(timeout=2)
    assert fired == [1]


def test_watch_for_silence_ignores_leading_silence_before_speech_starts():
    # 4 quiet polls up front (would already exceed required_quiet_polls=3
    # under the old buggy behavior, stopping before any speech happened),
    # THEN speech, THEN real trailing silence.
    levels = [2, 2, 2, 2, 50, 50, 2, 2, 2]
    recorder = _FakeRecorder(levels)
    fired = []
    thread = watch_for_silence(
        recorder, on_timeout=lambda: fired.append(1),
        poll_interval_s=0.005, silence_rms=15.0, required_quiet_polls=3,
        max_duration_s=5.0,
    )
    thread.join(timeout=2)
    assert fired == [1]
    # Proves the leading silence didn't fire early at poll 3: it must have
    # consumed the loud polls plus the post-speech quiet countdown too.
    assert recorder.calls >= 9


def test_watch_for_silence_before_speech_only_max_duration_applies():
    # Never crosses the loud threshold at all (e.g. a false wake trigger with
    # only ambient noise) — a low required_quiet_polls must NOT stop it early;
    # only the hard duration cap may.
    recorder = _FakeRecorder([2])
    fired = []
    thread = watch_for_silence(
        recorder, on_timeout=lambda: fired.append(1),
        poll_interval_s=0.01, silence_rms=15.0, required_quiet_polls=2,
        max_duration_s=0.05,
    )
    thread.join(timeout=2)
    assert fired == [1]
    assert recorder.calls >= 5  # ~0.05s / 0.01s, not the 2 quiet_polls would allow


def test_watch_for_silence_exits_quietly_if_recording_already_stopped():
    recorder = _FakeRecorder([50])
    recorder.is_recording = False  # ended some other way (e.g. the mic button)
    fired = []
    thread = watch_for_silence(
        recorder, on_timeout=lambda: fired.append(1),
        poll_interval_s=0.01, required_quiet_polls=3, max_duration_s=5.0,
    )
    thread.join(timeout=2)
    assert fired == []


# ── WakeWordListener availability (the one behavior testable everywhere) ─────

def test_listener_unavailable_without_audio_deps():
    # In this environment sounddevice needs a native PortAudio lib that isn't
    # installed, so the probe should fail closed rather than raise.
    listener = WakeWordListener(on_wake=lambda: None)
    assert listener.available is False
    # A no-op start()/pause()/resume()/stop() on an unavailable listener must
    # never raise — main.py calls these unconditionally.
    assert listener.start() is False
    listener.pause()
    listener.resume()
    listener.stop()
