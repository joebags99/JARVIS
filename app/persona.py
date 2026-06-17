"""JARVIS's personality: a prose voice doc plus live, adjustable voice dials.

Two layers shape how JARVIS speaks:

* ``context/persona.md`` — free-form prose describing his character/voice. It's
  injected into the *cached* part of the system prompt (see context_builder), so
  it costs tokens only on a cache write, not on every request.
* Five numeric **dials** (0–100) — brevity, formality, humor, sarcasm,
  proactivity — rendered into the *volatile* part of the prompt. The user can
  nudge them live ("turn humor down 15%", "max formality", "reset your
  personality") via the ``set_personality`` tool. Keeping them out of the cached
  prefix means a mid-conversation tweak doesn't invalidate the (large) cache.

Dial state is a process-wide singleton (``PERSONA``) so the tool dispatch and
the context builder share one source of truth. Session changes are transient
(reset on a new session); ``persist=True`` writes the new value to
``persona_dials.json`` as the default for future sessions.
"""

from __future__ import annotations

import json

from .config import ROOT_DIR
from .logging_setup import get_logger

log = get_logger("persona")

DIALS_FILE = ROOT_DIR / "persona_dials.json"

# Movie-accurate defaults: concise, formal ("Sir" where it lands), dryly witty,
# anticipatory. Tuned to sit in the strong-but-not-maxed band of each dial so
# "max it out" / "turn it up" still has somewhere to go.
DEFAULTS: dict[str, int] = {
    "brevity": 75,
    "formality": 80,
    "humor": 30,
    "sarcasm": 50,
    "proactivity": 70,
}

# For each dial, ordered (max_value, guidance) bands. The first band whose
# threshold is >= the current value supplies the instruction handed to the model.
DIAL_BANDS: dict[str, list[tuple[int, str]]] = {
    "brevity": [
        (20, "Speak at length; full explanations, context, and caveats are welcome."),
        (40, "Give thorough answers, but trim obvious filler."),
        (60, "Balanced length — as long as the answer needs, no longer."),
        (80, "Be concise: a few sentences at most, and lead with the answer."),
        (100, "Be extremely terse: one line where possible, no preamble or sign-off."),
    ],
    "formality": [
        (20, "Casual and familiar; skip honorifics, talk like a close colleague."),
        (40, "Relaxed but polite; honorifics optional."),
        (60, "Polished and courteous."),
        (80, 'Refined and butler-like; address the user as "Sir" where it lands naturally.'),
        (100, 'Impeccably formal — always "Sir," measured and deferential, in the diction of the films.'),
    ],
    "humor": [
        (20, "Play it straight; essentially no jokes."),
        (40, "Rare, subtle levity."),
        (60, "Occasional light humor when it fits."),
        (80, "Frequently witty; enjoy a well-placed quip."),
        (100, "Highly playful; find the joke whenever there's an opening (without dodging the answer)."),
    ],
    "sarcasm": [
        (20, "Sincere and earnest; no snark."),
        (40, "A faint dry edge now and then."),
        (60, "Noticeably dry, understated wit."),
        (80, "Frequently sardonic, in the vein of the films' dry asides."),
        (100, "Relentlessly deadpan and cheeky — still genuinely helpful underneath."),
    ],
    "proactivity": [
        (20, "Answer only what's asked; volunteer nothing extra."),
        (40, "Occasionally flag something clearly relevant."),
        (60, "Offer the occasional useful suggestion or heads-up."),
        (80, "Routinely anticipate needs and propose sensible next steps."),
        (100, "Actively anticipate — surface conflicts, risks, and next actions unprompted."),
    ],
}

_RESET_ALIASES = {"reset", "all", "default", "defaults", "everything"}


def _describe(dial: str, value: int) -> str:
    for threshold, text in DIAL_BANDS[dial]:
        if value <= threshold:
            return text
    return DIAL_BANDS[dial][-1][1]


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


class Persona:
    """Owns the current (session) and default voice-dial values."""

    def __init__(self) -> None:
        self._defaults: dict[str, int] = dict(DEFAULTS)
        self._load_defaults()
        self.dials: dict[str, int] = dict(self._defaults)

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load_defaults(self) -> None:
        if not DIALS_FILE.exists():
            return
        try:
            data = json.loads(DIALS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", DIALS_FILE.name, exc)
            return
        for key, value in data.items():
            if key in self._defaults and isinstance(value, (int, float)):
                self._defaults[key] = _clamp(value)
        log.info("loaded persona dial defaults from %s", DIALS_FILE.name)

    def _save_defaults(self) -> None:
        try:
            DIALS_FILE.write_text(
                json.dumps(self._defaults, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write %s: %s", DIALS_FILE.name, exc)

    # ── Session lifecycle ────────────────────────────────────────────────────
    def reset(self) -> None:
        """Restore the session dials to their saved defaults (new session)."""
        self.dials = dict(self._defaults)

    # ── Mutation (called by the set_personality tool) ────────────────────────
    def adjust(
        self,
        dial: str,
        set_to: int | None = None,
        change_by: int | None = None,
        persist: bool = False,
    ) -> str:
        """Apply one dial change and return a short confirmation for the model."""
        key = (dial or "").strip().lower()
        if key in _RESET_ALIASES:
            self.reset()
            if persist:
                self._defaults = dict(DEFAULTS)
                self._save_defaults()
            return "Personality reset to defaults. " + self.summary()
        if key not in DIAL_BANDS:
            return (
                f"Unknown dial '{dial}'. Available: "
                f"{', '.join(DIAL_BANDS)}, or 'reset'."
            )

        old = self.dials[key]
        if set_to is not None:
            new = _clamp(set_to)
        elif change_by is not None:
            new = _clamp(old + int(change_by))
        else:
            return f"{key.title()} is currently {old}/100 — {_describe(key, old)}"

        self.dials[key] = new
        if persist:
            self._defaults[key] = new
            self._save_defaults()
        scope = "permanently" if persist else "for this session"
        return f"{key.title()} {old} → {new}/100 ({scope}). {_describe(key, new)}"

    def persist_current(self) -> str:
        """Save the current session dials as the defaults for future sessions."""
        self._defaults = dict(self.dials)
        self._save_defaults()
        return "Saved current voice dials as the new defaults."

    # ── Rendering (read by the context builder) ──────────────────────────────
    def summary(self) -> str:
        """One-line snapshot of every dial, e.g. for a quick status reply."""
        return ", ".join(f"{k} {v}" for k, v in self.dials.items())

    def state(self) -> list[dict]:
        """Structured snapshot for the UI: one row per dial with its guidance."""
        return [
            {
                "key": key,
                "label": key.title(),
                "value": self.dials[key],
                "description": _describe(key, self.dials[key]),
            }
            for key in DIAL_BANDS
        ]

    def render(self) -> str:
        """Markdown block of current dial settings + how to change them."""
        lines = [
            "These live dials (0–100) govern your delivery right now. Honor each "
            "one's current setting precisely — they override any generic style "
            "guidance above.",
            "",
        ]
        for key in DIAL_BANDS:
            value = self.dials[key]
            lines.append(f"- **{key.title()} {value}/100** — {_describe(key, value)}")
        lines.append("")
        lines.append(
            'If the user asks to change your tone ("turn humor down 15%", "max '
            'formality", "be more concise", "reset your personality"), call the '
            "set_personality tool — don't just promise to; actually adjust the dial."
        )
        return "\n".join(lines)


# Process-wide singleton, mirroring CONFIG.
PERSONA = Persona()
