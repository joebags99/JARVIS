"""Assembles the dynamic system prompt for JARVIS.

The system prompt is intentionally lean: only the user profile and current
date/time are included on every request. Everything else (calendar, notes,
finances, knowledge docs) is fetched on-demand via Claude tool calls so
tokens are only spent when data is actually relevant to the question.

Static (cached, reloaded on "Reload Context"):
    * every ``.md`` file in ``context/`` (profile first, then the rest)
Dynamic (always included — tiny):
    * current date & time
On-demand via tools:
    * calendar events (get_calendar_events)
    * knowledge vault (search_vault / read_note / write_note / append_note / list_notes)
    * finances        (get_financial_summary)
    * todos           (get_todos / create_todo / update_todo / complete_todo)
    * meal plans      (get_meal_history / create_meal_plan)
    * knowledge docs  (load_knowledge_pool)
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import CONFIG, CONTEXT_DIR, ROOT_DIR
from .logging_setup import get_logger
from .persona import PERSONA

log = get_logger("context")

PROFILE_FILENAME = "profile.md"
PERSONA_FILENAME = "persona.md"

# Fallback voice when the user hasn't written their own context/persona.md.
DEFAULT_PERSONA = (
    "You are JARVIS from the Iron Man films: an unflappable, hyper-competent "
    "British AI butler. You are precise and efficient, address the user as "
    '"Sir" where it fits, and carry a dry, understated wit. You never ramble, '
    "never grovel, and never invent facts; when you don't know something you say "
    "so plainly. The voice dials below fine-tune this baseline."
)


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

    def _persona_section(self) -> str:
        return self.static_context.get(PERSONA_FILENAME, "").strip() or DEFAULT_PERSONA

    def _other_context_section(self) -> str:
        parts = []
        for name, body in self.static_context.items():
            if name in (PROFILE_FILENAME, PERSONA_FILENAME) or not body:
                continue
            title = Path(name).stem.replace("_", " ").replace("-", " ").title()
            parts.append(f"### {title}\n{body}")
        return "\n\n".join(parts).strip()

    def _datetime_section(self) -> str:
        now = dt.datetime.now().astimezone()
        return now.strftime("%A, %B %d, %Y — %I:%M %p %Z (UTC%z)")

    def _vault_section(self) -> str:
        """A tiny, cached pointer to the Obsidian vault — never its contents.

        Tells Claude the vault exists, shows its top-level folders so it knows
        where to look/write, and sets the working habits (search before
        answering, record durable knowledge as linked notes). Note *bodies* are
        always fetched on demand via the vault tools, never preloaded here.
        """
        if not CONFIG.obsidian_available:
            return ""
        try:
            from integrations import obsidian
            root = obsidian.vault_root()
            folders = sorted(
                p.name for p in root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not inspect vault for context: %s", exc)
            folders = []
        folder_line = (
            f"Top-level folders: {', '.join(folders)}.\n" if folders else ""
        )
        return (
            "You keep a single Obsidian knowledge vault — the user's 'second brain' "
            "— that is BOTH your long-term memory and your notes on people, "
            "projects, meetings, and topics. It replaces any older notes/recall "
            "tools.\n"
            f"{folder_line}"
            "Working habits:\n"
            "- Before answering anything that might be recorded (past decisions, "
            "people, projects, 'what did we discuss'), `search_vault` first, then "
            "`read_note` the most relevant hit.\n"
            "- When the user tells you something durable (a decision, a fact, "
            "meeting notes, a preference), capture it with `write_note` (new) or "
            "`append_note` (adding to a daily/running note) instead of only "
            "replying. Record what they actually said — don't invent detail.\n"
            "- Connect notes with `[[wikilinks]]` and `#tags` so the brain stays "
            "interconnected; read a note before overwriting it."
        )

    def _knowledge_pools_section(self) -> str:
        pools_file = ROOT_DIR / CONFIG.knowledge_pools_file
        if not pools_file.exists():
            return ""
        try:
            data = json.loads(pools_file.read_text(encoding="utf-8"))
            pools = data.get("pools", {})
            if not pools:
                return ""
            lines = [
                "Use the `load_knowledge_pool` tool when answering questions about these topics:"
            ]
            for name, pool in pools.items():
                desc = pool.get("description", "")
                lines.append(f"- **{name}**: {desc}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read knowledge pools file: %s", exc)
            return ""

    # ── Assembly ──────────────────────────────────────────────────────────────
    def build_system_prompt(self) -> list[dict]:
        """Build the system prompt as cache-friendly content blocks.

        Returns a list of two blocks: a large *stable* block (profile, knowledge
        pools, extra context, instructions) carrying a ``cache_control``
        breakpoint, followed by a tiny *volatile* block holding the current
        date/time. Prompt caching is a prefix match across ``tools → system →
        messages``, so keeping the minute-level timestamp out of the cached
        prefix lets the (large) TOOLS array and profile be served from cache on
        every follow-up round and turn — instead of being re-billed in full on
        each of the up-to-8 API calls a single message can trigger.
        """
        name = CONFIG.user_name
        stable_sections = [
            (
                f"You are JARVIS, a personal AI assistant for {name}. You have full "
                "awareness of the user's schedule, roles, and notes — but you fetch "
                "data on demand using tools rather than loading everything upfront. "
                "Only call a tool when the question actually needs that data."
            ),
            f"## Your Persona & Voice\n{self._persona_section()}",
            f"## Who You Are Assisting\n{self._profile_section()}",
        ]

        vault = self._vault_section()
        if vault:
            stable_sections.append(f"## Your Knowledge Vault\n{vault}")

        pools = self._knowledge_pools_section()
        if pools:
            stable_sections.append(f"## Available Knowledge Pools\n{pools}")

        other = self._other_context_section()
        if other:
            stable_sections.append(f"## Additional Context\n{other}")

        stable_sections.append(
            "Keep responses focused and actionable. Format for readability in a "
            "small overlay window — use short paragraphs or bullet points where "
            "helpful. Never be verbose when concise serves better."
        )

        stable_text = self._truncate("\n\n".join(stable_sections))
        # Volatile: both the date/time (changes every minute) and the voice dials
        # (the user can nudge them mid-conversation) must sit *after* the cache
        # breakpoint, or they'd invalidate the large cached prefix on every tick
        # or tweak.
        volatile_text = (
            f"## Today's Date & Time\n{self._datetime_section()}\n\n"
            f"## Voice Dials (current)\n{PERSONA.render()}"
        )

        log.info(
            "assembled system prompt (%d stable + %d volatile chars)",
            len(stable_text), len(volatile_text),
        )
        return [
            {
                "type": "text",
                "text": stable_text,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": volatile_text},
        ]

    def _truncate(self, prompt: str) -> str:
        """Hard cap on prompt size; truncates the tail (oldest notes / extras)."""
        limit = CONFIG.max_context_chars
        if len(prompt) <= limit:
            return prompt
        log.warning(
            "system prompt %d chars exceeds cap %d; truncating", len(prompt), limit
        )
        return prompt[:limit].rstrip() + "\n\n…(context truncated to fit budget)"
