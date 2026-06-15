"""Anthropic Claude API client with streaming, context injection, and tool use.

Maintains the per-session conversation history and streams responses token by
token via a callback so the overlay can update the UI in real time. All network
work happens on a background thread (the caller is responsible for that); this
module is intentionally synchronous and stream-oriented.

Tool use flow:
  1. First stream pass — Claude may emit text AND a tool_use block.
  2. Tools are executed locally; results fed back as tool_result messages.
  3. Second stream pass — Claude emits the final confirmation (streamed).
"""

from __future__ import annotations

from typing import Callable

from .config import CONFIG
from .context_builder import ContextBuilder
from .logging_setup import get_logger

log = get_logger("claude")

MAX_TOKENS = 1024

_MONARCH_MCP_URL = "https://api.monarch.com/mcp"
_MCP_BETA = "mcp-client-2025-04-04"

# ── Tool definitions ──────────────────────────────────────────────────────────

CALENDAR_TOOLS = [
    {
        "name": "update_calendar_event",
        "description": (
            "Update (edit) an existing Google Calendar event. "
            "Finds the event by its current title; supply only the fields that "
            "should change — everything else is left untouched. "
            "Use this instead of create_calendar_event whenever the user asks to "
            "edit, reschedule, rename, or modify an existing event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {
                    "type": "string",
                    "description": "Google account name from the source tags, e.g. 'personal'.",
                },
                "calendar_name": {
                    "type": "string",
                    "description": "Calendar name from the source tags, e.g. 'Family', 'Bills'.",
                },
                "event_summary": {
                    "type": "string",
                    "description": "Current title of the event to find and update.",
                },
                "start_hint": {
                    "type": "string",
                    "description": (
                        "ISO 8601 date or datetime to narrow down which occurrence "
                        "to update, e.g. '2026-06-16'. Required when multiple events "
                        "share the same title."
                    ),
                },
                "new_summary": {
                    "type": "string",
                    "description": "New title (omit to keep current).",
                },
                "new_start": {
                    "type": "string",
                    "description": "New start as ISO 8601 with timezone offset (omit to keep current).",
                },
                "new_end": {
                    "type": "string",
                    "description": "New end as ISO 8601 with timezone offset (omit to keep current).",
                },
                "new_description": {
                    "type": "string",
                    "description": "New description (omit to keep current).",
                },
                "new_location": {
                    "type": "string",
                    "description": "New location (omit to keep current).",
                },
            },
            "required": ["account_name", "calendar_name", "event_summary"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create an event on the user's Google Calendar. "
            "The calendar events already in the system prompt show the account name "
            "and calendar name in their source tag, e.g. '[Google/personal/Bills]' "
            "means account_name='personal', calendar_name='Bills'. "
            "Use existing events to infer appropriate timing when the user doesn't "
            "give exact times. Always include timezone offset in start/end."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {
                    "type": "string",
                    "description": (
                        "Google account name from the source tags, e.g. 'personal', "
                        "'work', 'default'."
                    ),
                },
                "calendar_name": {
                    "type": "string",
                    "description": (
                        "Calendar name from the source tags, e.g. 'Joseph Konkle', "
                        "'Bills', 'Family', 'Tasks'."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Event title.",
                },
                "start": {
                    "type": "string",
                    "description": (
                        "Start as ISO 8601 with timezone offset, "
                        "e.g. '2026-06-15T14:00:00-05:00'. "
                        "For all-day events use 'YYYY-MM-DD'."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "End as ISO 8601 with timezone offset. "
                        "Default to 1 hour after start for unspecified durations."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional event notes.",
                },
                "location": {
                    "type": "string",
                    "description": "Optional location.",
                },
            },
            "required": ["account_name", "calendar_name", "summary", "start", "end"],
        },
    },
    {
        "name": "load_knowledge_pool",
        "description": (
            "Load content from a named Google Docs knowledge pool to help answer the "
            "user's question. Call this when a question relates to a topic listed in "
            "the Available Knowledge Pools section of your system prompt (e.g. 'work', "
            "'finances', 'personal'). Do NOT call it for general questions unrelated "
            "to those topics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pool_name": {
                    "type": "string",
                    "description": (
                        "The exact pool name to load, e.g. 'work', 'finances', 'personal'."
                    ),
                },
            },
            "required": ["pool_name"],
        },
    },
]


def _execute_tool(name: str, input_data: dict) -> str:
    """Dispatch a local tool call and return a result string."""
    if name == "create_calendar_event":
        from integrations.google_calendar import create_event
        return create_event(
            account_name=input_data["account_name"],
            calendar_name=input_data["calendar_name"],
            summary=input_data["summary"],
            start_iso=input_data["start"],
            end_iso=input_data["end"],
            description=input_data.get("description"),
            location=input_data.get("location"),
        )
    if name == "update_calendar_event":
        from integrations.google_calendar import update_event
        return update_event(
            account_name=input_data["account_name"],
            calendar_name=input_data["calendar_name"],
            event_summary=input_data["event_summary"],
            start_hint=input_data.get("start_hint"),
            new_summary=input_data.get("new_summary"),
            new_start_iso=input_data.get("new_start"),
            new_end_iso=input_data.get("new_end"),
            new_description=input_data.get("new_description"),
            new_location=input_data.get("new_location"),
        )
    if name == "load_knowledge_pool":
        import json
        from .config import ROOT_DIR
        pools_path = ROOT_DIR / CONFIG.knowledge_pools_file
        if not pools_path.exists():
            return (
                "No knowledge_pools.json found. "
                "Copy knowledge_pools.json.example and fill in your Google Doc IDs."
            )
        try:
            data = json.loads(pools_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return f"Error reading knowledge_pools.json: {exc}"
        pool_name = input_data.get("pool_name", "")
        pools = data.get("pools", {})
        pool = pools.get(pool_name)
        if pool is None:
            available = ", ".join(pools.keys()) or "(none configured)"
            return (
                f"Pool '{pool_name}' not found. Available pools: {available}"
            )
        account = data.get("account", CONFIG.google_accounts[0])
        from integrations.google_docs import load_pool
        content = load_pool(pool, account)
        header = f"## Knowledge Pool: {pool_name}\n_{pool.get('description', '')}_"
        return f"{header}\n\n{content}"
    return f"Unknown tool: {name}"


def _block_to_dict(block) -> dict:
    """Serialize an Anthropic content block to a plain dict for history storage."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    return {"type": block.type}


# ── Client ────────────────────────────────────────────────────────────────────

class ClaudeClient:
    """Wraps the Anthropic SDK and owns session conversation history."""

    def __init__(self, context_builder: ContextBuilder | None = None) -> None:
        self._client = None
        self._init_error: str | None = None
        self.context = context_builder or ContextBuilder()
        # Session memory: list of {"role": ..., "content": ...} dicts.
        self.history: list[dict] = []
        self._init_client()

    def _init_client(self) -> None:
        if not CONFIG.has_anthropic_key:
            self._init_error = (
                "No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file."
            )
            log.error(self._init_error)
            return
        try:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=CONFIG.anthropic_api_key)
            log.info("Anthropic client ready (model=%s)", CONFIG.anthropic_model)
        except ImportError:
            self._init_error = "The 'anthropic' package is not installed."
            log.error(self._init_error)
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"Could not initialize Anthropic client: {exc}"
            log.error(self._init_error)

    @property
    def ready(self) -> bool:
        return self._client is not None

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def reset_session(self) -> None:
        """Clear conversation history (called when overlay reopens)."""
        self.history.clear()
        log.info("session history cleared")

    def reload_context(self) -> None:
        self.context.reload_static()

    def summarize_session(self, history: list[dict]) -> str:
        """One-shot summary of a conversation history. Returns '' on failure."""
        if not self.ready or not history:
            return ""

        lines = []
        for m in history:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                lines.append(f"{role.upper()}: {content.strip()}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text", "").strip()
                        if t:
                            lines.append(f"{role.upper()}: {t}")

        if not lines:
            return ""

        try:
            response = self._client.messages.create(
                model=CONFIG.anthropic_model,
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize this JARVIS assistant conversation in 3-5 concise "
                        "bullet points. Focus on topics discussed, decisions made, and "
                        "any calendar events or actions taken.\n\n"
                        + "\n".join(lines)
                    ),
                }],
            )
            return response.content[0].text if response.content else ""
        except Exception as exc:  # noqa: BLE001
            log.error("summarize_session failed: %s", exc)
            return ""

    def send(
        self,
        user_message: str,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """Send a message with full context; stream deltas via ``on_delta``.

        Handles a single round of tool use transparently: if Claude calls a
        tool (e.g. create_calendar_event), the tool executes locally and Claude
        streams a confirmation response. Returns the complete assistant reply.
        Never raises — failures return a readable error string.
        """
        if not self.ready:
            return self._init_error or "Claude client is not available."

        self.history.append({"role": "user", "content": user_message})
        system_prompt = self.context.build_system_prompt()

        full_text = ""
        try:
            # ── First pass: stream with tools enabled ─────────────────────────
            first_kwargs: dict = dict(
                model=CONFIG.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=self.history,
                tools=CALENDAR_TOOLS,
            )
            if CONFIG.monarch_enabled:
                first_kwargs["mcp_servers"] = [{
                    "type": "url",
                    "name": "monarch",
                    "url": _MONARCH_MCP_URL,
                    "authorization_token": CONFIG.monarch_api_token,
                }]
                first_kwargs["betas"] = [_MCP_BETA]
                _stream_fn = self._client.beta.messages.stream
            else:
                _stream_fn = self._client.messages.stream

            with _stream_fn(**first_kwargs) as stream:
                for text in stream.text_stream:
                    full_text += text
                    if on_delta:
                        on_delta(text)
                final_msg = stream.get_final_message()

            tool_uses = [b for b in final_msg.content if b.type == "tool_use"]

            if tool_uses:
                # Commit assistant turn (text + tool_use blocks) to history.
                self.history.append({
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in final_msg.content],
                })

                # Execute each tool, collect results.
                results = []
                for tu in tool_uses:
                    outcome = _execute_tool(tu.name, tu.input)
                    log.info("tool %s -> %r", tu.name, outcome[:120])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": outcome,
                    })
                self.history.append({"role": "user", "content": results})

                # ── Second pass: stream Claude's follow-up confirmation ────────
                followup = ""
                with self._client.messages.stream(
                    model=CONFIG.anthropic_model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=self.history,
                ) as stream:  # no tools/MCP needed for follow-up text
                    for text in stream.text_stream:
                        followup += text
                        full_text += text
                        if on_delta:
                            on_delta(text)
                self.history.append({"role": "assistant", "content": followup})
            else:
                self.history.append({"role": "assistant", "content": full_text})

            log.info("response received (%d chars)", len(full_text))
            return full_text

        except Exception as exc:  # noqa: BLE001
            log.error("Claude API call failed: %s", exc)
            # Roll back to keep history consistent — remove any partial turns.
            while self.history and self.history[-1]["role"] == "user":
                self.history.pop()
            return f"⚠️ Error contacting Claude: {exc}"
