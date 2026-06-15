"""Microphone capture for push-to-talk voice input.

Records mono 16 kHz audio (what Whisper expects) into memory while the record
button is held, then writes a temporary WAV on stop. If ``sounddevice`` isn't
installed or no input device exists, ``available`` is False and the overlay
disables the voice button.

Set ``AUDIO_INPUT_DEVICE`` in .env to override the system default:
  AUDIO_INPUT_DEVICE=1               # device index
  AUDIO_INPUT_DEVICE=Blue Yeti       # partial name match (case-insensitive)
Leave it empty to use whatever the OS default input device is.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("recorder")

SAMPLE_RATE = 16_000
CHANNELS = 1


def _resolve_device(sd, preference: str) -> int | str | None:
    """Return a sounddevice-compatible device specifier.

    Returns the preference as-is if it looks like a device index (int string),
    the matching device index if it's a partial name, or None (system default)
    if the preference is empty or unresolvable.
    """
    if not preference:
        return None

    # Integer index — pass through directly.
    try:
        return int(preference)
    except ValueError:
        pass

    # Partial name — search case-insensitively.
    pref_lower = preference.lower()
    try:
        devices = sd.query_devices()
        for idx, d in enumerate(devices):
            if (
                d.get("max_input_channels", 0) > 0
                and pref_lower in d.get("name", "").lower()
            ):
                log.info("matched audio device %d: %s", idx, d["name"])
                return idx
        log.warning(
            "AUDIO_INPUT_DEVICE=%r not found; falling back to system default",
            preference,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not search audio devices: %s", exc)

    return None


class Recorder:
    def __init__(self) -> None:
        self._sd = None
        self._np = None
        self._stream = None
        self._frames: list = []
        self._available = False
        self._device: int | str | None = None
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
            input_devices = [
                (i, d) for i, d in enumerate(devices)
                if d.get("max_input_channels", 0) > 0
            ]
            if not input_devices:
                log.warning("no input audio device found; voice disabled")
                return

            log.info(
                "available input devices: %s",
                ", ".join(f"{i}:{d['name']}" for i, d in input_devices),
            )

            self._device = _resolve_device(sd, CONFIG.audio_input_device)
            if self._device is not None:
                log.info("using configured audio device: %r", self._device)
            else:
                default = sd.query_devices(kind="input")
                log.info("using system default input: %s", default.get("name", "?"))

            self._available = True
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
            kwargs: dict = dict(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                callback=_callback,
                dtype="int16",
            )
            if self._device is not None:
                kwargs["device"] = self._device

            self._stream = self._sd.InputStream(**kwargs)
            self._stream.start()
            log.info("recording started (device=%r)", self._device)
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
            log.warning("no audio frames captured — button events may not be firing")
            return None

        try:
            from scipy.io import wavfile

            audio = self._np.concatenate(self._frames, axis=0)
            duration_s = len(audio) / SAMPLE_RATE
            rms = float(self._np.sqrt(self._np.mean(audio.astype("float32") ** 2)))
            log.info(
                "recording saved: %d samples (%.2fs), RMS=%.1f",
                len(audio), duration_s, rms,
            )
            if rms < 10:
                log.warning(
                    "RMS=%.1f — audio appears to be silence; "
                    "check AUDIO_INPUT_DEVICE or microphone permissions",
                    rms,
                )
            if duration_s < 0.3:
                log.warning(
                    "recording only %.2fs — hold the button longer while speaking",
                    duration_s,
                )

            tmp = Path(tempfile.gettempdir()) / "jarvis_recording.wav"
            wavfile.write(str(tmp), SAMPLE_RATE, audio)
            return tmp
        except Exception as exc:  # noqa: BLE001
            log.error("could not save recording: %s", exc)
            return None
