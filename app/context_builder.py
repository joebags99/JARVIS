"""Assembles the dynamic system prompt for JARVIS.

This is the brain of JARVIS: the quality of every answer depends on what gets
assembled here. It is deliberately small, readable, and easy to extend — add a
new section by writing one method and appending it in ``build_system_prompt``.

Sources
-------
Static (cached, reloaded on "Reload Context"):
    * every ``.md`` file in ``context/`` (profile first, then the rest)
Dynamic (fetched fresh per query):
    * current date & time
    * Google + Outlook calendar events (next 7 days)
    * the 5 most recent files in ``notes/``
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .config import CONFIG, CONTEXT_DIR
from .logging_setup import get_logger
from integrations import google_calendar, outlook_calendar, notes_watcher

log = get_logger("context")

# Caps from the spec to keep the prompt within token budget.
MAX_NOTES = 5
MAX_NOTE_CHARS = 2000
MAX_EVENTS = 20
CALENDAR_DAYS = 7
PROFILE_FILENAME = "profile.md"


class ContextBuilder:
    """Builds the system prompt; caches static context between reloads."""

    def __init__(self) -> None:
        self._static_cache: dict[str, str] | None = None

    # ── Static context (context/*.md) ────────────────────────────────────────
    def reload_static(self) -> None:
        """Re-read all ``.md`` files in context/ into the cache."""
        cache: dict[str, str] = {}
        if CONTEXT_DIR.exists():
            for path in sorted(CONTEXT_DIR.glob("*.md")):
                # Skip committed example templates.
                if path.name.endswith(".example.md"):
                    continue
                try:
                    cache[path.name] = path.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not read context file %s: %s", path.name, exc)
        self._static_cache = cache
        log.info("reloaded %d static context file(s)", len(cache))

    @property
    def static_context(self) -> dict[str, str]:
        if self._static_cache is None:
            self.reload_static()
        return self._static_cache or {}

    # ── Section renderers ─────────────────────────────────────────────────────
    def _profile_section(self) -> str:
        profile = self.static_context.get(PROFILE_FILENAME, "").strip()
        if not profile:
            return "(No profile.md found in /context. Add one to personalize JARVIS.)"
        return profile

    def _other_context_section(self) -> str:
        parts = []
        for name, body in self.static_context.items():
            if name == PROFILE_FILENAME or not body:
                continue
            title = Path(name).stem.replace("_", " ").replace("-", " ").title()
            parts.append(f"### {title}\n{body}")
        return "\n\n".join(parts).strip()

    def _datetime_section(self) -> str:
        now = dt.datetime.now()
        return now.strftime("%A, %B %d, %Y — %I:%M %p")

    def _calendar_section(self) -> str:
        events = []
        try:
            events += google_calendar.get_events(CALENDAR_DAYS, MAX_EVENTS)
        except Exception as exc:  # noqa: BLE001
            log.error("google calendar error: %s", exc)
        try:
            events += outlook_calendar.get_events(CALENDAR_DAYS, MAX_EVENTS)
        except Exception as exc:  # noqa: BLE001
            log.error("outlook calendar error: %s", exc)

        if not events:
            return "(No calendar connected, or no upcoming events.)"

        # Sort merged events chronologically, cap to MAX_EVENTS.
        events.sort(key=lambda e: e.start.replace(tzinfo=None))
        events = events[:MAX_EVENTS]
        return "\n".join(e.format_line() for e in events)

    def _notes_section(self) -> str:
        notes = notes_watcher.read_recent_notes(MAX_NOTES, MAX_NOTE_CHARS)
        if not notes:
            return "(No meeting notes in /notes yet.)"
        blocks = []
        for note in notes:
            modified = dt.datetime.fromtimestamp(note.modified).strftime("%Y-%m-%d")
            blocks.append(f"### {note.path.name} (modified {modified})\n{note.content}")
        return "\n\n".join(blocks)

    # ── Assembly ──────────────────────────────────────────────────────────────
    def build_system_prompt(self) -> str:
        name = CONFIG.user_name
        sections = [
            (
                f"You are JARVIS, a personal AI assistant for {name}. You are smart, "
                "concise, and proactive. You have full awareness of the user's "
                "schedule, roles, and notes."
            ),
            f"## Who You Are Assisting\n{self._profile_section()}",
            f"## Today's Date & Time\n{self._datetime_section()}",
            f"## Upcoming Calendar Events (next {CALENDAR_DAYS} days)\n{self._calendar_section()}",
            f"## Recent Meeting Notes\n{self._notes_section()}",
        ]

        other = self._other_context_section()
        if other:
            sections.append(f"## Additional Context\n{other}")

        sections.append(
            "Keep responses focused and actionable. Format for readability in a "
            "small overlay window — use short paragraphs or bullet points where "
            "helpful. Never be verbose when concise serves better."
        )

        prompt = "\n\n".join(sections)
        prompt = self._truncate(prompt)
        log.info("assembled system prompt (%d chars)", len(prompt))
        return prompt

    def _truncate(self, prompt: str) -> str:
        """Hard cap on prompt size; truncates the tail (oldest notes / extras)."""
        limit = CONFIG.max_context_chars
        if len(prompt) <= limit:
            return prompt
        log.warning(
            "system prompt %d chars exceeds cap %d; truncating", len(prompt), limit
        )
        return prompt[:limit].rstrip() + "\n\n…(context truncated to fit budget)"
