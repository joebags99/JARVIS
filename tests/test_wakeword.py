"""Tests for the wake-word logic (app/wakeword.py).

wake_score_triggers()/silence_auto_stop_due() are pure and fully covered.
watch_for_silence() only calls methods on whatever `recorder` it's given, so
it's tested here with a small fake — no real sounddevice/microphone needed.

WakeWordListener's real `_probe()` needs sounddevice + openwakeword (a
native PortAudio dependency not present in this environment); its
`available` probe is checked for that path. Its pause()/resume()/callback
plumbing, however, is pure enough to test by constructing a listener and
swapping in fake sounddevice/model objects post-probe — exactly the
threading behavior a real bug (two InputStreams briefly open on one device,
and pause() being called unsafely from within its own stream's audio
callback) was found and fixed in.
"""

from __future__ import annotations

import threading
import time

from app.wakeword import (
    WakeWordListener,
    silence_auto_stop_due,
    wake_score_triggers,
    watch_for_silence,
)


class _FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs.get("callback")
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class _FakeSd:
    def __init__(self):
        self.streams: list[_FakeStream] = []

    def InputStream(self, **kwargs):
        s = _FakeStream(**kwargs)
        self.streams.append(s)
        return s


class _FakeModel:
    def __init__(self, score=0.0):
        self.score = score

    def predict(self, x):
        return {"hey_jarvis": self.score}


class _FakeIndata:
    """Stands in for the numpy array sounddevice passes to the callback —
    _callback only calls .reshape(-1) on it, so a real numpy array (a heavy
    dependency this test suite otherwise avoids) isn't needed."""

    def reshape(self, *args):
        return self


def _fake_listener(on_wake=lambda: None, score=0.0) -> WakeWordListener:
    """A listener with a fake (post-probe) sounddevice + model, no real audio."""
    listener = WakeWordListener(on_wake=on_wake)
    listener._available = True
    listener._sd = _FakeSd()
    listener._model = _FakeModel(score)
    listener._model_name = "hey_jarvis"
    return listener


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


# ── pause()/resume() never leave two streams open (fake sounddevice) ────────

def test_start_opens_exactly_one_stream():
    listener = _fake_listener()
    assert listener.start() is True
    assert len(listener._sd.streams) == 1
    assert listener._sd.streams[0].started is True


def test_pause_fully_closes_the_stream_not_just_flags_it():
    listener = _fake_listener()
    listener.start()
    stream = listener._sd.streams[0]
    listener.pause()
    assert stream.closed is True
    assert listener._stream is None  # nothing left "open" to race with Recorder's


def test_resume_after_pause_opens_a_new_stream():
    listener = _fake_listener()
    listener.start()
    listener.pause()
    listener.resume()
    assert len(listener._sd.streams) == 2  # the old one, plus a fresh one
    assert listener._sd.streams[1].started is True


def test_pause_when_not_paused_is_idempotent():
    listener = _fake_listener()
    listener.start()
    listener.pause()
    listener.pause()  # must not double-close or raise
    assert listener._sd.streams[0].closed is True


def test_resume_without_a_prior_pause_is_a_noop():
    listener = _fake_listener()
    listener.start()
    listener.resume()  # never paused -> must not open a second stream
    assert len(listener._sd.streams) == 1


def test_resume_before_start_is_a_noop():
    listener = _fake_listener()
    listener.resume()
    assert listener._sd.streams == []


# ── on_wake dispatches off the audio callback thread (deadlock avoidance) ───

def test_callback_never_calls_on_wake_on_the_calling_thread():
    # Regression test: pause() (called from on_wake -> _start_recording) now
    # stops/closes this very stream, which sounddevice/PortAudio warns is
    # unsafe to do from within the stream's own callback thread. The callback
    # must hand off to a fresh thread instead of calling on_wake() inline.
    caller_thread = threading.current_thread()
    seen_threads = []
    done = threading.Event()

    def on_wake():
        seen_threads.append(threading.current_thread())
        done.set()

    listener = _fake_listener(on_wake=on_wake, score=1.0)
    listener.start()

    listener._callback(_FakeIndata(), 1280, None, None)

    assert done.wait(timeout=2)
    assert seen_threads[0] is not caller_thread


def test_callback_respects_retrigger_cooldown():
    calls = []
    listener = _fake_listener(on_wake=lambda: calls.append(1), score=1.0)
    listener.start()
    listener._last_trigger = time.monotonic()  # just triggered

    listener._callback(_FakeIndata(), 1280, None, None)
    time.sleep(0.05)
    assert calls == []  # still inside the cooldown window


def test_callback_does_not_trigger_below_threshold():
    calls = []
    listener = _fake_listener(on_wake=lambda: calls.append(1), score=0.1)

    listener._callback(_FakeIndata(), 1280, None, None)
    time.sleep(0.05)
    assert calls == []
