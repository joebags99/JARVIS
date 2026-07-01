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

Two ways to speak: ``speak(text)`` synthesizes and plays a complete text in one
shot (used for short one-off notifications); ``start_utterance()`` /
``feed(delta)`` / ``finish()`` speak a reply sentence-by-sentence as it streams
in from Claude, so playback starts almost immediately instead of waiting for
the whole answer — see the "Streamed utterance" section of ``Speaker`` below.
"""

from __future__ import annotations

import queue
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


def _normalize_for_speech(text: str) -> str:
    """Spell out symbols that some engines (ElevenLabs especially) mispronounce
    or glitch on, instead of leaving the raw glyph for the engine to guess at.

    - The degree symbol has no natural spoken form, so "72°F"/"75°" become
      "72 degrees Fahrenheit"/"75 degrees".
    - An em/en dash used as a clause break ("done — for now") reads as an
      audible "dash" on some engines; a comma is the natural spoken pause.
    - A hyphen directly between two digits ("70-75") is a range, not a
      hyphenated word, so it's read as "70 to 75".
    """
    if not text:
        return text
    text = re.sub(r"°\s*F\b", " degrees Fahrenheit", text, flags=re.I)
    text = re.sub(r"°\s*C\b", " degrees Celsius", text, flags=re.I)
    text = text.replace("°", " degrees")
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r"\s+--\s+", ", ", text)
    text = re.sub(r"(?<=\d)-(?=\d)", " to ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Sentence splitting (for streamed speech) ─────────────────────────────────

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "st", "approx",
    "etc", "eg", "ie", "no", "vol",
}
# A sentence boundary: one or more of .!?  (optionally followed by a closing
# quote/paren) then whitespace. Captured so the match's end marks exactly
# where the next sentence (or the still-incomplete tail) begins.
_BOUNDARY_RE = re.compile(r"[.!?]+[)\]\"']*(\s+)")


def split_ready_sentences(buffer: str) -> tuple[list[str], str]:
    """Split *buffer* into complete sentences plus a still-incomplete tail.

    Meant to be called repeatedly as more text streams in: keep the returned
    tail and concatenate the next chunk onto it before calling again.
    Deliberately conservative, not real NLP — a decimal like "3.5" is never
    mistaken for a boundary since the period there has no whitespace after it
    (the regex requires whitespace to even match), and a short list of common
    abbreviations ("Mr.", "Dr.", ...) is checked so those don't fire an early,
    awkward pause. A blank line is always treated as a boundary too (e.g.
    between list items/paragraphs).
    """
    if not buffer:
        return [], buffer

    sentences: list[str] = []
    pos = 0
    for m in _BOUNDARY_RE.finditer(buffer):
        punct_idx = m.start()
        candidate = buffer[pos:m.end()]
        words = re.findall(r"[A-Za-z]+", candidate)
        if buffer[punct_idx] == "." and words and words[-1].lower() in _ABBREVIATIONS:
            continue
        stripped = candidate.strip()
        if stripped:
            sentences.append(stripped)
        pos = m.end()

    # A blank line inside the not-yet-emitted tail is a hard boundary too,
    # independent of punctuation (only the first one — later ones are caught
    # on the next call once more text has streamed in).
    tail = buffer[pos:]
    para_split = re.split(r"\n\s*\n", tail, maxsplit=1)
    if len(para_split) == 2:
        piece = para_split[0].strip()
        if piece:
            sentences.append(piece)
        tail = para_split[1]

    return sentences, tail


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
        # Streamed-utterance state (start_utterance/feed/finish) — see below.
        self._buffer = ""
        self._sentence_queue: queue.Queue | None = None

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
        if self._sentence_queue is not None:
            # Wake the streamed-utterance consumer immediately (it also checks
            # stop_event on its own, but the sentinel avoids the poll delay).
            try:
                self._sentence_queue.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    def _run(self, text: str, stop_event: threading.Event) -> None:
        # Spell out degree signs/dashes before markdown-stripping, which would
        # otherwise drop the em/en dash characters this depends on matching.
        clean = _strip_markdown(_normalize_for_speech(text))
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

    # ── Streamed utterance (speak-as-you-go) ──────────────────────────────────
    # start_utterance() / feed() / finish() let the caller speak a reply as it
    # streams in from Claude, sentence by sentence, instead of waiting for the
    # full text (which speak() above still does — unchanged, still used for
    # one-shot texts like proactive notifications). feed() only *extracts*
    # ready sentences and queues their text; a dedicated consumer thread does
    # the actual (blocking) synth + playback, so feed() — called once per
    # on_delta chunk — never blocks the text-streaming loop that's calling it.

    def start_utterance(self) -> None:
        """Begin a new streamed utterance: cancels any current speech and
        arms the sentence-by-sentence pipeline. Call feed() as text streams
        in, then finish() once the full reply is known."""
        if not self._available:
            return
        self.stop()
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._buffer = ""
        self._sentence_queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._consume_sentences, args=(stop_event, self._sentence_queue),
            daemon=True,
        )
        self._thread.start()

    def feed(self, delta: str) -> None:
        """Feed one streamed text chunk; queues any sentence it completes."""
        if (
            not self._available or not delta
            or self._stop_event is None or self._stop_event.is_set()
            or self._sentence_queue is None
        ):
            return
        self._buffer += delta
        sentences, self._buffer = split_ready_sentences(self._buffer)
        for sentence in sentences:
            self._sentence_queue.put(sentence)

    def finish(self) -> None:
        """Flush any remaining buffered text as the final sentence."""
        if (
            not self._available
            or self._stop_event is None or self._stop_event.is_set()
            or self._sentence_queue is None
        ):
            return
        if self._buffer.strip():
            self._sentence_queue.put(self._buffer)
        self._buffer = ""
        self._sentence_queue.put(None)  # sentinel: no more sentences coming

    def _consume_sentences(self, stop_event: threading.Event, q: "queue.Queue") -> None:
        while not stop_event.is_set():
            try:
                sentence = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if sentence is None:
                return
            clean = _strip_markdown(_normalize_for_speech(sentence))
            if not clean or stop_event.is_set():
                continue
            try:
                samples, samplerate = self._backend.synth(clean, stop_event)
            except Exception as exc:  # noqa: BLE001
                log.error("TTS streamed synthesis failed: %s", exc)
                continue
            if samples is None or stop_event.is_set():
                continue
            _play(samples, samplerate, stop_event)
