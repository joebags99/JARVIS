"""Microphone capture for push-to-talk voice input.

Records mono 16 kHz audio (what Whisper expects) into memory while the record
button is held, then writes a temporary WAV on stop. If ``sounddevice`` isn't
installed or no input device exists, ``available`` is False and the overlay
disables the voice button.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("recorder")

SAMPLE_RATE = 16_000
CHANNELS = 1


class Recorder:
    def __init__(self) -> None:
        self._sd = None
        self._np = None
        self._stream = None
        self._frames: list = []
        self._available = False
        self._probe()

    def _probe(self) -> None:
        try:
            import sounddevice as sd
            import numpy as np
        except Exception as exc:  # noqa: BLE001
            log.warning("voice deps missing (%s); voice disabled", exc)
            return

        self._sd = sd
        self._np = np
        try:
            devices = sd.query_devices()
            has_input = any(d.get("max_input_channels", 0) > 0 for d in devices)
            if not has_input:
                log.warning("no input audio device found; voice disabled")
                return
            self._available = True
            log.info("microphone available")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not query audio devices (%s); voice disabled", exc)

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> bool:
        """Begin capturing audio. Returns False if recording can't start."""
        if not self._available:
            return False
        self._frames = []

        def _callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("audio status: %s", status)
            self._frames.append(indata.copy())

        try:
            self._stream = self._sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                callback=_callback,
                dtype="int16",
            )
            self._stream.start()
            log.info("recording started")
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("could not start recording: %s", exc)
            self._stream = None
            return False

    def stop(self) -> Path | None:
        """Stop recording and write a temp WAV. Returns its path, or None."""
        if self._stream is None:
            return None
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            log.error("error stopping stream: %s", exc)
        finally:
            self._stream = None

        if not self._frames:
            log.info("no audio captured")
            return None

        try:
            from scipy.io import wavfile

            audio = self._np.concatenate(self._frames, axis=0)
            tmp = Path(tempfile.gettempdir()) / "jarvis_recording.wav"
            wavfile.write(str(tmp), SAMPLE_RATE, audio)
            log.info("recording saved: %s (%d samples)", tmp.name, len(audio))
            return tmp
        except Exception as exc:  # noqa: BLE001
            log.error("could not save recording: %s", exc)
            return None
