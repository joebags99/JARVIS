"""Wake word — "Hey JARVIS" starts a recording hands-free.

Sits alongside push-to-talk (``app/recorder.py``), not instead of it: an
always-on background listener scores a rolling window of microphone audio
against a small local model (`openWakeWord <https://github.com/dscripka/openWakeWord>`_,
no account/API key, matching the project's local-first choice for STT with
faster-whisper) and calls back once the phrase is heard. Off by default —
set ``JARVIS_WAKE_WORD_ENABLED=true`` to turn it on.

``WakeWordListener`` owns its own ``sounddevice.InputStream``, separate from
``Recorder``'s. The two must never be open at once (most audio backends
tolerate two simultaneous input streams on one device poorly, and there's no
reason to keep scoring for the wake phrase while an utterance triggered by it
is already being captured) — ``pause()``/``resume()`` let a caller suspend
scoring without tearing the stream down and rebuilding it, so resuming is
instant. ``app/overlay.py``'s ``_start_recording``/``_stop_recording`` call
these automatically for *any* recording (push-to-talk included), so the
caller doesn't need to reason about it per trigger source.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("wakeword")

SAMPLE_RATE = 16_000
CHUNK_SAMPLES = 1280  # openWakeWord's expected ~80ms frame at 16kHz
# Once a wake fires, ignore further detections for this long — a single
# utterance of the phrase can otherwise span several high-scoring callbacks
# in a row. The caller (main._start_wake_word) also pauses the listener
# almost immediately, so this is a defensive floor, not the primary guard.
_RETRIGGER_COOLDOWN_S = 3.0

# Silence-timeout defaults for a hands-free recording's auto-stop (there's no
# button release to know the user's done talking). Tuned conservatively —
# these are starting points the user should tune after trying it for real.
DEFAULT_POLL_INTERVAL_S = 0.3
DEFAULT_SILENCE_RMS = 15.0
DEFAULT_REQUIRED_QUIET_POLLS = 5          # ~1.5s of quiet at the default interval
DEFAULT_MAX_DURATION_S = 12.0


# ── Pure decision helpers ─────────────────────────────────────────────────────

def wake_score_triggers(score: float, threshold: float) -> bool:
    """True when a wake-word model's confidence score counts as a detection."""
    return score >= threshold


def silence_auto_stop_due(
    quiet_polls: int, required_quiet_polls: int, elapsed_s: float, max_duration_s: float,
) -> bool:
    """True when a hands-free recording should end itself: either enough
    consecutive quiet polls have passed, or the hard duration cap is hit
    (whichever comes first — a long ramble shouldn't record forever, and a
    quick question shouldn't wait out the full cap)."""
    return quiet_polls >= required_quiet_polls or elapsed_s >= max_duration_s


# ── Listener ────────────────────────────────────────────────────────────────

class WakeWordListener:
    """Continuous background listener for a wake phrase. Mirrors Recorder's
    ``available``/``start``/``stop`` shape."""

    def __init__(self, on_wake: Callable[[], None]) -> None:
        self._on_wake = on_wake
        self._sd = None
        self._model = None
        self._model_name = ""
        self._stream = None
        self._available = False
        self._paused = threading.Event()
        self._last_trigger = 0.0
        self._probe()

    def _probe(self) -> None:
        try:
            import sounddevice as sd
            import openwakeword.utils
            from openwakeword.model import Model
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "wake-word deps missing (%s); install openwakeword + onnxruntime "
                "(see requirements.txt); wake word disabled", exc,
            )
            return
        self._sd = sd

        # openWakeWord's pretrained models (e.g. "hey_jarvis") ship as metadata
        # only — the actual weights aren't bundled in the pip package and
        # Model() does NOT fetch them itself, unlike faster-whisper's
        # auto-download. This downloads them on first use (idempotent — skips
        # files that already exist, so it's a fast no-op on every later
        # launch) so wake word "just works" the same way voice input does. A
        # custom absolute path in JARVIS_WAKE_WORD_PHRASE isn't one of the
        # bundled names, so this is a harmless no-op for that case.
        try:
            openwakeword.utils.download_models([CONFIG.wake_word_phrase])
        except Exception as exc:  # noqa: BLE001
            log.error(
                "could not download wake-word model %r (needs internet the "
                "first time): %s", CONFIG.wake_word_phrase, exc,
            )
            return

        try:
            # onnx (not tflite): matches the project's numpy 2.x-compatible
            # pin (requirements.txt caps at <3, not <2) without pulling in a
            # second, less broadly-compatible inference runtime.
            self._model = Model(
                wakeword_models=[CONFIG.wake_word_phrase], inference_framework="onnx",
            )
            # The dict key predict() returns is the model's registered name —
            # "hey_jarvis" for a bundled phrase, but a custom path's file stem
            # (e.g. "my_model_v1") for a custom model, never the path string
            # itself. Read it back rather than assuming it matches the config
            # value, or a custom-path model would never trigger.
            self._model_name = next(iter(self._model.models.keys()))
            self._available = True
        except Exception as exc:  # noqa: BLE001
            log.error(
                "could not load wake-word model %r: %s", CONFIG.wake_word_phrase, exc
            )

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> bool:
        """Begin listening. Returns False if the stream couldn't start."""
        if not self._available or self._stream is not None:
            return False

        def _callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("wake-word audio status: %s", status)
            if self._paused.is_set():
                return
            try:
                scores = self._model.predict(indata.reshape(-1))
            except Exception as exc:  # noqa: BLE001
                log.debug("wake-word scoring failed: %s", exc)
                return
            score = scores.get(self._model_name, 0.0)
            if not wake_score_triggers(score, CONFIG.wake_word_threshold):
                return
            now = time.monotonic()
            if now - self._last_trigger < _RETRIGGER_COOLDOWN_S:
                return
            self._last_trigger = now
            log.info("wake word detected (score=%.2f)", score)
            try:
                self._on_wake()
            except Exception as exc:  # noqa: BLE001
                log.error("on_wake callback failed: %s", exc)

        try:
            self._stream = self._sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=CHUNK_SAMPLES, callback=_callback,
            )
            self._stream.start()
            log.info(
                "wake-word listener started (phrase=%s, threshold=%.2f)",
                CONFIG.wake_word_phrase, CONFIG.wake_word_threshold,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("could not start wake-word listener: %s", exc)
            self._stream = None
            return False

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            log.error("error stopping wake-word listener: %s", exc)
        finally:
            self._stream = None

    def pause(self) -> None:
        """Suspend scoring (stream stays open — resume() is instant)."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()


# ── Hands-free auto-stop ──────────────────────────────────────────────────────

def watch_for_silence(
    recorder,
    on_timeout: Callable[[], None],
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    silence_rms: float = DEFAULT_SILENCE_RMS,
    required_quiet_polls: int = DEFAULT_REQUIRED_QUIET_POLLS,
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
) -> threading.Thread:
    """Poll ``recorder.current_level()`` on a background thread and call
    ``on_timeout()`` once the recording should end itself (see
    :func:`silence_auto_stop_due`) — so a wake-word-triggered recording
    doesn't need a button release. Exits quietly without calling
    ``on_timeout`` if the recording already ended some other way (e.g. the
    mic button). Returns the (already-started) watchdog thread.
    """
    start = time.monotonic()
    quiet_polls = 0

    def _loop() -> None:
        nonlocal quiet_polls
        while True:
            time.sleep(poll_interval_s)
            if not recorder.is_recording:
                return  # ended some other way — nothing to do
            elapsed = time.monotonic() - start
            level = recorder.current_level()
            quiet_polls = quiet_polls + 1 if level < silence_rms else 0
            if silence_auto_stop_due(quiet_polls, required_quiet_polls, elapsed, max_duration_s):
                try:
                    on_timeout()
                except Exception as exc:  # noqa: BLE001
                    log.error("auto-stop callback failed: %s", exc)
                return

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread
