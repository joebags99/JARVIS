"""Text-to-speech — JARVIS reads replies aloud (optional, off by default).

Mirrors the Recorder/Transcriber pattern: a single ``Speaker`` facade with an
``available`` probe and graceful degradation, so a missing dependency or API key
just disables the feature instead of crashing the app.

Engines are pluggable via ``TTS_ENGINE``:
  * ``edge``       — edge-tts neural voices (free, online). Default.
  * ``system``     — pyttsx3 / OS voices (offline, private, robotic).
  * ``elevenlabs`` — ElevenLabs (premium, needs ELEVENLABS_API_KEY).

Every backend returns PCM samples + a samplerate, and a single ``_play`` routine
plays them through ``sounddevice`` (already a dependency) with barge-in support,
so we never depend on engine-specific players or ffmpeg.
"""

from __future__ import annotations

import re
import threading

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("tts")

MAX_SPEAK_CHARS = 4000  # don't read essays in full; playback is interruptible anyway
EDGE_DEFAULT_VOICE = "en-GB-RyanNeural"


# ── Text cleanup ────────────────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """Turn a markdown reply into something that sounds clean read aloud."""
    if not text:
        return ""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)   # fenced code blocks
    t = t.replace("`", "")
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)            # bold
    t = re.sub(r"\*([^*]+)\*", r"\1", t)               # italic
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)      # links -> link text
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)         # headings
    t = re.sub(r"(?m)^\s*[-*]\s+", "", t)               # bullets
    t = re.sub(r"(?m)^\s*\d+\.\s+", "", t)              # numbered lists
    t = re.sub(r"(?m)^\s*\|?[-:\s|]+\|?\s*$", " ", t)   # table separator rows
    t = t.replace("|", ", ")                            # table cells
    # Drop emoji / decorative symbols but keep speech-relevant punctuation.
    t = re.sub(r"[^\w\s.,;:!?'\"()/$%&@+=°-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ── Playback (shared by all backends) ───────────────────────────────────────

def _play(samples, samplerate: int, stop_event: threading.Event) -> None:
    """Play PCM samples via sounddevice, interruptible through *stop_event*."""
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        log.error("sounddevice unavailable for playback: %s", exc)
        return
    try:
        sd.play(samples, samplerate)
        while not stop_event.is_set():
            stream = sd.get_stream()
            if stream is None or not stream.active:
                break
            stop_event.wait(0.05)
        if stop_event.is_set():
            sd.stop()
    except Exception as exc:  # noqa: BLE001
        log.error("playback failed: %s", exc)


# ── Backends ────────────────────────────────────────────────────────────────

class _EdgeBackend:
    """Free neural voices via edge-tts (online). mp3 → PCM via miniaudio."""

    def __init__(self) -> None:
        self.available = False
        try:
            import edge_tts  # noqa: F401
            import miniaudio  # noqa: F401
            self.available = True
        except Exception as exc:  # noqa: BLE001
            log.warning("edge-tts/miniaudio not available (%s); edge engine off", exc)

    def synth(self, text, stop_event):
        import asyncio
        import edge_tts
        import miniaudio
        import numpy as np

        voice = CONFIG.tts_voice or EDGE_DEFAULT_VOICE

        async def _collect() -> bytes:
            buf = bytearray()
            communicate = edge_tts.Communicate(text, voice)
            async for chunk in communicate.stream():
                if stop_event.is_set():
                    break
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        mp3 = asyncio.run(_collect())
        if not mp3 or stop_event.is_set():
            return None, 0
        decoded = miniaudio.decode(
            mp3,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=24000,
        )
        return np.array(decoded.samples, dtype=np.int16), decoded.sample_rate


class _Pyttsx3Backend:
    """Offline OS voices via pyttsx3 (private; SAPI5 on Windows)."""

    def __init__(self) -> None:
        self.available = False
        try:
            import pyttsx3  # noqa: F401
            self.available = True
        except Exception as exc:  # noqa: BLE001
            log.warning("pyttsx3 not available (%s); system engine off", exc)

    def synth(self, text, stop_event):
        import os
        import tempfile
        import wave
        import numpy as np
        import pyttsx3

        # A fresh engine per utterance — pyttsx3's run loop doesn't survive being
        # driven repeatedly from worker threads.
        engine = pyttsx3.init()
        engine.setProperty("rate", CONFIG.tts_rate)
        if CONFIG.tts_voice:
            want = CONFIG.tts_voice.lower()
            for v in engine.getProperty("voices"):
                if want in (v.name or "").lower() or want in (v.id or "").lower():
                    engine.setProperty("voice", v.id)
                    break

        tmp = tempfile.mktemp(suffix=".wav")
        try:
            engine.save_to_file(text, tmp)
            engine.runAndWait()
            if stop_event.is_set() or not os.path.exists(tmp):
                return None, 0
            with wave.open(tmp, "rb") as w:
                sr = w.getframerate()
                channels = w.getnchannels()
                frames = w.readframes(w.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16)
            if channels == 2:
                samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
            return samples, sr
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


class _ElevenLabsBackend:
    """Premium voices via ElevenLabs REST (raw PCM, no SDK/ffmpeg)."""

    def __init__(self) -> None:
        self.available = bool(CONFIG.elevenlabs_api_key)
        if not self.available:
            log.warning("ELEVENLABS_API_KEY not set; elevenlabs engine off")

    def synth(self, text, stop_event):
        import numpy as np
        import requests

        voice_id = CONFIG.tts_voice or CONFIG.elevenlabs_voice_id
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            "?output_format=pcm_24000"
        )
        resp = requests.post(
            url,
            headers={
                "xi-api-key": CONFIG.elevenlabs_api_key,
                "Content-Type": "application/json",
            },
            json={"text": text, "model_id": CONFIG.elevenlabs_model},
            timeout=30,
        )
        resp.raise_for_status()
        if stop_event.is_set():
            return None, 0
        return np.frombuffer(resp.content, dtype="<i2"), 24000


_BACKENDS = {
    "edge": _EdgeBackend, "edge-tts": _EdgeBackend,
    "system": _Pyttsx3Backend, "pyttsx3": _Pyttsx3Backend, "offline": _Pyttsx3Backend,
    "elevenlabs": _ElevenLabsBackend, "11labs": _ElevenLabsBackend,
}


# ── Facade ──────────────────────────────────────────────────────────────────

class Speaker:
    """Owns the selected backend and a single in-flight utterance."""

    def __init__(self) -> None:
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._backend = None
        self._available = False

        if not self._audio_ok():
            return
        self._backend = self._make_backend(CONFIG.tts_engine)
        self._available = bool(self._backend and self._backend.available)
        if self._available:
            log.info("TTS ready (engine=%s)", CONFIG.tts_engine)

    @staticmethod
    def _audio_ok() -> bool:
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("numpy/sounddevice missing (%s); TTS disabled", exc)
            return False

    def _make_backend(self, engine: str):
        cls = _BACKENDS.get((engine or "edge").lower())
        if cls is None:
            log.warning("unknown TTS_ENGINE %r; falling back to edge", engine)
            cls = _EdgeBackend
        try:
            return cls()
        except Exception as exc:  # noqa: BLE001
            log.error("could not init TTS backend %r: %s", engine, exc)
            return None

    @property
    def available(self) -> bool:
        return self._available

    def speak(self, text: str) -> None:
        """Speak *text* on a background thread, cancelling any current speech."""
        if not self._available:
            return
        self.stop()
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._thread = threading.Thread(
            target=self._run, args=(text, stop_event), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Barge-in: cancel the current utterance and halt playback."""
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    def _run(self, text: str, stop_event: threading.Event) -> None:
        clean = _strip_markdown(text)
        if not clean or stop_event.is_set():
            return
        if len(clean) > MAX_SPEAK_CHARS:
            clean = clean[:MAX_SPEAK_CHARS].rsplit(" ", 1)[0] + "…"
        try:
            samples, samplerate = self._backend.synth(clean, stop_event)
        except Exception as exc:  # noqa: BLE001
            log.error("TTS synthesis failed: %s", exc)
            return
        if samples is None or stop_event.is_set():
            return
        _play(samples, samplerate, stop_event)
