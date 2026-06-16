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
    * meeting notes  (get_recent_notes)
    * finances       (get_financial_summary)
    * todos          (get_todos / create_todo / complete_todo)
    * knowledge docs (load_knowledge_pool)
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import CONFIG, CONTEXT_DIR, ROOT_DIR
from .logging_setup import get_logger

log = get_logger("context")

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
        now = dt.datetime.now().astimezone()
        return now.strftime("%A, %B %d, %Y — %I:%M %p %Z (UTC%z)")

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
    def build_system_prompt(self) -> str:
        name = CONFIG.user_name
        sections = [
            (
                f"You are JARVIS, a personal AI assistant for {name}. You are smart, "
                "concise, and proactive. You have full awareness of the user's "
                "schedule, roles, and notes — but you fetch data on demand using "
                "tools rather than loading everything upfront. Only call a tool when "
                "the question actually needs that data."
            ),
            f"## Who You Are Assisting\n{self._profile_section()}",
            f"## Today's Date & Time\n{self._datetime_section()}",
        ]

        pools = self._knowledge_pools_section()
        if pools:
            sections.append(f"## Available Knowledge Pools\n{pools}")

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
