"""Local speech-to-text via faster-whisper.

The model is loaded lazily on first use (and downloaded automatically by
faster-whisper if not cached). If the package isn't installed, ``available`` is
False and the overlay treats voice as unavailable — never a hard crash.
"""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("transcriber")


def _filter_hallucinated_segments(segments: list, cutoff: float) -> list:
    """Drop segments Whisper itself flags as likely not real speech.

    faster-whisper's ``Segment`` carries ``no_speech_prob`` (0-1) per segment;
    a high value means the model thinks that stretch of audio was silence or
    noise, yet still emitted *some* text for it — a well-known Whisper
    failure mode on silence/near-silence audio, and one that gets worse when
    ``hotwords`` nudges the model toward specific vocabulary (this project's
    name_corrections glossary) that was never actually said. ``getattr`` with
    a safe default in case a future faster-whisper version renames the field.
    """
    return [s for s in segments if getattr(s, "no_speech_prob", 0.0) < cutoff]


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._checked = False
        self._available = self._probe()

    def _probe(self) -> bool:
        try:
            import faster_whisper  # noqa: F401

            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("faster-whisper not available (%s); voice disabled", exc)
            return False

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_model(self, on_status=None) -> bool:
        if self._model is not None:
            return True
        if not self._available:
            return False
        try:
            from faster_whisper import WhisperModel

            if on_status:
                on_status(f"Loading Whisper '{CONFIG.whisper_model}' model…")
            log.info("loading whisper model: %s", CONFIG.whisper_model)
            # int8 on CPU keeps memory/compute reasonable on a laptop.
            self._model = WhisperModel(
                CONFIG.whisper_model, device="cpu", compute_type="int8"
            )
            log.info("whisper model loaded")
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("failed to load whisper model: %s", exc)
            self._available = False
            return False

    def transcribe(self, wav_path: Path, on_status=None) -> str:
        """Transcribe a WAV file to text. Returns "" on any failure."""
        if not self._ensure_model(on_status):
            return ""
        if not wav_path or not Path(wav_path).exists():
            return ""
        try:
            if on_status:
                on_status("Transcribing…")
            # Bias toward the user's canonical names so proper nouns transcribe
            # right at the source ("" when no glossary → no effect).
            from . import name_corrector
            hotwords = name_corrector.hotwords()
            # vad_filter=False: don't let Whisper's silence detector discard real audio.
            segments, info = self._model.transcribe(
                str(wav_path),
                beam_size=5,
                vad_filter=False,
                hotwords=hotwords or None,
            )
            all_segments = list(segments)
            kept = _filter_hallucinated_segments(all_segments, CONFIG.whisper_no_speech_cutoff)
            if len(kept) < len(all_segments):
                log.info(
                    "dropped %d/%d segment(s) with high no-speech probability "
                    "(likely silence/noise, not real speech)",
                    len(all_segments) - len(kept), len(all_segments),
                )
            text = " ".join(seg.text.strip() for seg in kept).strip()
            log.info(
                "transcription: %d chars (lang=%s prob=%.2f)",
                len(text), info.language, info.language_probability,
            )
            if not text:
                log.warning("Whisper returned empty — audio may be silence or too short")
            return text
        except Exception as exc:  # noqa: BLE001
            log.error("transcription failed: %s", exc)
            return ""
