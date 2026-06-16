"""Anthropic Claude API client with streaming, context injection, and tool use.

Maintains the per-session conversation history and streams responses token by
token via a callback so the overlay can update the UI in real time. All network
work happens on a background thread (the caller is responsible for that); this
module is intentionally synchronous and stream-oriented.

Tool use flow:
  Bounded loop (see MAX_TOOL_ROUNDS): each round streams a response, and if
  Claude emits a local tool_use block, the tool runs and its result is fed
  back for another round. Parallel tool use is disabled so a local tool_use
  and the remote Monarch MCP tool are never requested in the same turn —
  that combination left a dangling, unpaired mcp_tool_use block in history.
"""

from __future__ import annotations

from typing import Callable

from .config import CONFIG
from .context_builder import ContextBuilder
from .logging_setup import get_logger

log = get_logger("claude")

MAX_TOKENS = 1024
MAX_TOOL_ROUNDS = 8  # safety cap on local tool_use <-> tool_result round trips

_MONARCH_MCP_URL = "https://api.monarch.com/mcp"
_MCP_BETA = "mcp-client-2025-04-04"

# Finance-related keywords used to decide whether a message warrants attaching
# the remote Monarch MCP server. Attaching it on every request — regardless of
# topic — burns tokens on tool definitions the model never uses.
_FINANCIAL_KEYWORDS = (
    "spend", "spent", "spending", "budget", "transaction", "expense",
    "balance", "money", "financ", "income", "invest", "bill", "savings",
    "credit", "debit", "bank", "monarch", "net worth", "cash flow",
    "subscription", "paycheck", "afford", "owe", "debt",
)

# History compaction: once a session accumulates more than this many user
# turns, older turns are summarized away to bound per-request token cost.
HISTORY_MAX_TURNS = 8
HISTORY_KEEP_TURNS = 3


def _looks_financial(message: str) -> bool:
    lowered = message.lower()
    return any(kw in lowered for kw in _FINANCIAL_KEYWORDS)

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_calendar_events",
        "description": (
            "Fetch the user's upcoming calendar events. Call this whenever the user "
            "asks about their schedule, appointments, meetings, what they have coming "
            "up, or what they're doing on a specific day. Also call it before "
            "creating or editing an event so you know the correct account_name and "
            "calendar_name to use. Entries tagged [Outlook-ICS] are free/busy only — "
            "no title or location data exists for them, so treat their 'Busy' summary "
            "as opaque and never invent a real title or location for one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days ahead to look. Default 7.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_notes",
        "description": (
            "Load recent meeting notes from the user's notes folder. Call this when "
            "the user asks about recent meetings, wants to reference notes, asks "
            "what was discussed or decided, or asks about action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "update_calendar_event",
        "description": (
            "Update (edit) an existing Google Calendar event. "
            "Finds the event by its current title; supply only the fields that "
            "should change — everything else is left untouched. "
            "Use this instead of create_calendar_event whenever the user asks to "
            "edit, reschedule, rename, or modify an existing event. "
            "Call get_calendar_events first if you need to confirm the account or calendar name."
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
            "Call get_calendar_events first to see existing events — their source "
            "tags show the account and calendar name, e.g. '[Google/personal/Bills]' "
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
                "recurrence_freq": {
                    "type": "string",
                    "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"],
                    "description": (
                        "Set only if the user wants this event to repeat, e.g. "
                        "'every week' -> WEEKLY. Omit entirely for a one-time event. "
                        "If set, you MUST also set exactly one of recurrence_count or "
                        "recurrence_until — never create an open-ended recurring event."
                    ),
                },
                "recurrence_count": {
                    "type": "integer",
                    "description": (
                        "Total occurrences including the first, e.g. 'for 5 weeks' -> 5. "
                        "Use this OR recurrence_until, not both."
                    ),
                },
                "recurrence_until": {
                    "type": "string",
                    "description": (
                        "Last possible date (YYYY-MM-DD, inclusive) the recurrence may "
                        "extend to, e.g. 'every Monday until August 1st' -> '2026-08-01'. "
                        "Use this OR recurrence_count, not both."
                    ),
                },
            },
            "required": ["account_name", "calendar_name", "summary", "start", "end"],
        },
    },
    {
        "name": "get_todos",
        "description": (
            "Fetch the user's Todoist tasks. Call this whenever the user asks "
            "what's on their to-do list, what they need to do today, or about "
            "tasks in a specific category or date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": (
                        "Todoist filter query, e.g. 'today', 'overdue | today', "
                        "'tomorrow', a specific date like 'June 20', or a category "
                        "like '#Daedabyte'. Defaults to overdue + today's tasks."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "create_todo",
        "description": (
            "Add a new task to the user's Todoist. Always classify the task into "
            "one of the user's categories — ask the user if it's genuinely unclear "
            "which one fits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The task text, e.g. 'Renew car registration'.",
                },
                "category": {
                    "type": "string",
                    "enum": ["Daedabyte", "General", "Brightpoint"],
                    "description": "Which category/project this task belongs to.",
                },
                "due_string": {
                    "type": "string",
                    "description": (
                        "The due date AS THE USER SAID IT, e.g. 'June 20', 'tomorrow', "
                        "'next Friday', 'every Monday'. Pass their words through "
                        "unmodified — do NOT compute or convert this to a specific "
                        "date yourself; JARVIS resolves it locally against the real "
                        "current date. Omit for no due date."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional extra notes for the task.",
                },
            },
            "required": ["content", "category"],
        },
    },
    {
        "name": "complete_todo",
        "description": (
            "Mark an existing Todoist task as done. Finds the task by matching its "
            "text, so you don't need its exact ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Text to search for among the user's open tasks.",
                },
                "due_hint": {
                    "type": "string",
                    "description": (
                        "Due-date text to disambiguate when multiple tasks match, "
                        "e.g. 'today' or 'June 20'."
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "update_todo",
        "description": (
            "Edit an existing Todoist task — rename it, reschedule it, change its "
            "notes, or move it to a different category. Finds the task by matching "
            "its current text; supply only the fields that should change. Use this "
            "instead of create_todo whenever the user asks to edit, reschedule, or "
            "recategorize an existing task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Text to search for among the user's open tasks.",
                },
                "due_hint": {
                    "type": "string",
                    "description": (
                        "Due-date text to disambiguate when multiple tasks match, "
                        "e.g. 'today' or 'June 20'."
                    ),
                },
                "new_content": {
                    "type": "string",
                    "description": "New task text (omit to keep current).",
                },
                "new_due_string": {
                    "type": "string",
                    "description": (
                        "The new due date AS THE USER SAID IT, e.g. 'June 25', "
                        "'tomorrow', 'next Friday', 'no date'. Pass their words "
                        "through unmodified — do NOT compute or convert this to a "
                        "specific date yourself; JARVIS resolves it locally against "
                        "the real current date. Omit to keep the current due date."
                    ),
                },
                "new_description": {
                    "type": "string",
                    "description": "New notes for the task (omit to keep current).",
                },
                "new_category": {
                    "type": "string",
                    "enum": ["Daedabyte", "General", "Brightpoint"],
                    "description": "New category/project (omit to keep current).",
                },
            },
            "required": ["content"],
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
    """Dispatch a tool call and return a result string."""
    if name == "get_calendar_events":
        from integrations import google_calendar, outlook_calendar, outlook_ics
        days = int(input_data.get("days") or 7)
        events = []
        try:
            events += google_calendar.get_events(days, 20)
        except Exception as exc:  # noqa: BLE001
            log.warning("google calendar fetch failed: %s", exc)
        try:
            events += outlook_calendar.get_events(days, 20)
        except Exception as exc:  # noqa: BLE001
            log.warning("outlook calendar fetch failed: %s", exc)
        try:
            events += outlook_ics.get_events(days, 20)
        except Exception as exc:  # noqa: BLE001
            log.warning("outlook ics fetch failed: %s", exc)
        if not events:
            return "(No upcoming events found.)"
        events.sort(key=lambda e: e.start.replace(tzinfo=None))
        return "\n".join(e.format_line() for e in events[:20])
    if name == "get_recent_notes":
        import datetime as dt
        from integrations import notes_watcher
        notes = notes_watcher.read_recent_notes(5, 2000)
        if not notes:
            return "(No meeting notes in /notes yet.)"
        blocks = []
        for note in notes:
            modified = dt.datetime.fromtimestamp(note.modified).strftime("%Y-%m-%d")
            blocks.append(f"### {note.path.name} (modified {modified})\n{note.content}")
        return "\n\n".join(blocks)
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
            recurrence_freq=input_data.get("recurrence_freq"),
            recurrence_count=input_data.get("recurrence_count"),
            recurrence_until=input_data.get("recurrence_until"),
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
    if name == "get_todos":
        from integrations import todoist
        return todoist.list_tasks(input_data.get("filter"))
    if name == "create_todo":
        from integrations import todoist
        return todoist.create_task(
            content=input_data["content"],
            category=input_data["category"],
            due_string=input_data.get("due_string"),
            description=input_data.get("description"),
        )
    if name == "complete_todo":
        from integrations import todoist
        return todoist.complete_task(
            content=input_data["content"],
            due_hint=input_data.get("due_hint"),
        )
    if name == "update_todo":
        from integrations import todoist
        return todoist.update_task(
            content=input_data["content"],
            due_hint=input_data.get("due_hint"),
            new_content=input_data.get("new_content"),
            new_due_string=input_data.get("new_due_string"),
            new_description=input_data.get("new_description"),
            new_category=input_data.get("new_category"),
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
        # Per-pool "account" overrides the top-level default — lets a pool
        # (e.g. a work account's docs) pull from a different Google account
        # than the rest of the user's knowledge pools.
        account = pool.get("account") or data.get("account", CONFIG.google_accounts[0])
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
    # MCP-related block types (mcp_tool_use, mcp_tool_result, server_tool_use, ...)
    # carry fields we don't model explicitly — keep them intact so replaying this
    # history back to the API doesn't drop data the API itself put there.
    return block.model_dump()


# ── Client ────────────────────────────────────────────────────────────────────

class ClaudeClient:
    """Wraps the Anthropic SDK and owns session conversation history."""

    def __init__(self, context_builder: ContextBuilder | None = None) -> None:
        self._client = None
        self._init_error: str | None = None
        self.context = context_builder or ContextBuilder()
        # Session memory: list of {"role": ..., "content": ...} dicts.
        self.history: list[dict] = []
        # Sticky for the session: once a question triggers Monarch, keep it
        # attached for follow-ups (e.g. "what about last month?") even if
        # they don't repeat a financial keyword.
        self._monarch_active = False
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
        self._monarch_active = False
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

    def _compact_history(self) -> None:
        """Summarize older turns once history grows long, to bound token cost.

        Only cuts at "turn boundaries" — history entries that are a plain
        string user message, never a tool_result list — so the cut point
        always falls between complete assistant turns and can't orphan a
        tool_use/tool_result pairing.
        """
        boundaries = [
            i for i, m in enumerate(self.history)
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        if len(boundaries) <= HISTORY_MAX_TURNS:
            return

        cut = boundaries[-HISTORY_KEEP_TURNS]
        older, recent = self.history[:cut], self.history[cut:]
        summary = self.summarize_session(older)
        if not summary:
            return

        self.history = [
            {"role": "user", "content": f"[Earlier in this session:]\n{summary}"},
            {"role": "assistant", "content": "Got it, I'll keep that in mind."},
        ] + recent
        log.info("compacted %d older turn(s) into a summary", len(boundaries) - HISTORY_KEEP_TURNS)

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

        self._compact_history()
        self.history.append({"role": "user", "content": user_message})
        system_prompt = self.context.build_system_prompt()

        full_text = ""
        try:
            # ── Build API kwargs; add Monarch MCP server when enabled. These are
            # reused on every round of the loop below, so local tools and the
            # Monarch MCP tool stay available across sequential tool rounds.
            base_kwargs: dict = dict(
                model=CONFIG.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=TOOLS,
                # A local tool_use always forces the API to stop the turn (it
                # needs a client-supplied tool_result before continuing). If the
                # model also requested the remote Monarch MCP tool in that same
                # turn, the server never gets to resolve/pair it before the stop,
                # leaving a dangling mcp_tool_use block that breaks the next
                # call. Disabling parallel tool use keeps local and MCP tool
                # calls on separate rounds instead.
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
            stream_fn = self._client.messages.stream
            if CONFIG.monarch_enabled and (self._monarch_active or _looks_financial(user_message)):
                self._monarch_active = True
                try:
                    from integrations.monarch_oauth import get_monarch_token
                    base_kwargs["mcp_servers"] = [{
                        "type": "url",
                        "name": "monarch",
                        "url": _MONARCH_MCP_URL,
                        "authorization_token": get_monarch_token(),
                    }]
                    base_kwargs["betas"] = [_MCP_BETA]
                    stream_fn = self._client.beta.messages.stream
                    log.info("using beta endpoint with monarch mcp_servers attached")
                except Exception as exc:
                    log.error("Monarch token error, falling back to plain endpoint: %s", exc, exc_info=True)

            # ── Bounded tool-use loop: keep tools/mcp_servers attached on every
            # round so local tools (e.g. load_knowledge_pool) and the Monarch
            # MCP tool can both be used for one question, just sequentially.
            for round_num in range(1, MAX_TOOL_ROUNDS + 1):
                with stream_fn(messages=self.history, **base_kwargs) as stream:
                    for text in stream.text_stream:
                        full_text += text
                        if on_delta:
                            on_delta(text)
                    final_msg = stream.get_final_message()

                log.info(
                    "round %d content block types: %s",
                    round_num, [b.type for b in final_msg.content],
                )

                self.history.append({
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in final_msg.content],
                })

                tool_uses = [b for b in final_msg.content if b.type == "tool_use"]
                if not tool_uses:
                    break

                # Discard any pre-tool text streamed this round — the real
                # answer comes after the tool result, not before it.
                full_text = ""

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
            else:
                log.warning("hit max tool rounds (%d); forcing a final answer", MAX_TOOL_ROUNDS)
                try:
                    forced_kwargs = dict(base_kwargs)
                    forced_kwargs["tool_choice"] = {"type": "none"}
                    full_text = ""
                    with stream_fn(messages=self.history, **forced_kwargs) as stream:
                        for text in stream.text_stream:
                            full_text += text
                            if on_delta:
                                on_delta(text)
                        final_msg = stream.get_final_message()
                    self.history.append({
                        "role": "assistant",
                        "content": [_block_to_dict(b) for b in final_msg.content],
                    })
                    log.info("forced final response after exhausting tool rounds (%d chars)", len(full_text))
                except Exception as exc:  # noqa: BLE001
                    log.error("forced final response failed: %s", exc)
                    return "Done — but I couldn't generate a summary. Check your calendar/tasks to confirm."

            log.info("response received (%d chars)", len(full_text))
            return full_text

        except Exception as exc:  # noqa: BLE001
            log.error("Claude API call failed: %s", exc)
            # Roll back to keep history consistent — remove any partial turns.
            while self.history and self.history[-1]["role"] == "user":
                self.history.pop()
            return f"⚠️ Error contacting Claude: {exc}"
