"""Central registry of JARVIS's local tools.

Each tool is declared once as a :class:`Tool` — its name, the description and
input schema the model sees, the handler that runs it, and an ``available``
predicate that gates whether it's advertised (e.g. email tools only when Gmail
is configured). ``claude_client`` consumes two functions:

* :func:`api_tools` — the Anthropic ``tools`` array, filtered to what's available
  for this install (in registration order, so the cached tool prefix is stable).
* :func:`execute_tool` — dispatch a tool call by name to its handler.

Adding a capability is now a single edit here (write a handler, append a
``Tool``) instead of touching a definitions list, a dispatch chain, and the
conditional gating in three different places. Handlers import their integration
lazily so importing this module stays cheap and free of heavy/optional deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("tools")

# Note/task categories come from the user's config so the note + todo tools
# aren't hardcoded to one person's set. Built at import — the tool specs are
# registered once — so changing JARVIS_CATEGORIES takes effect on restart.
_CATEGORIES = list(CONFIG.categories)
_CAT_EXAMPLE = _CATEGORIES[0] if _CATEGORIES else "Work"


def _category_phrase() -> str:
    """Render the category list for prose, e.g. 'A, B, and C'."""
    cats = _CATEGORIES
    if not cats:
        return ""
    if len(cats) == 1:
        return cats[0]
    if len(cats) == 2:
        return f"{cats[0]} and {cats[1]}"
    return ", ".join(cats[:-1]) + ", and " + cats[-1]


_CAT_PHRASE = _category_phrase()


@dataclass(frozen=True)
class Tool:
    """A single local tool: its model-facing spec plus how to run it."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], str]
    available: Callable[[], bool] = field(default=lambda: True)

    def spec(self) -> dict:
        """The Anthropic tool definition (name/description/input_schema)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    """Add a tool to the registry (later registration wins on a name clash)."""
    _REGISTRY[tool.name] = tool


def api_tools() -> list[dict]:
    """Specs for every currently-available tool, in registration order."""
    return [t.spec() for t in _REGISTRY.values() if t.available()]


def execute_tool(name: str, input_data: dict) -> str:
    """Dispatch a tool call to its handler and return a result string."""
    tool = _REGISTRY.get(name)
    if tool is None:
        return f"Unknown tool: {name}"
    return tool.handler(input_data)


# ── Handlers ──────────────────────────────────────────────────────────────────
# Each takes the model's tool input dict and returns a result string. Integration
# imports are deferred to call time so this module imports without optional deps.

def _get_calendar_events(input_data: dict) -> str:
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


def _get_recent_notes(input_data: dict) -> str:
    import datetime as dt

    from integrations import notes_watcher
    category = input_data["category"]
    notes = notes_watcher.read_recent_notes(category, 5, 2000)
    if not notes:
        return f"(No {category} notes yet.)"
    blocks = []
    for note in notes:
        modified = dt.datetime.fromtimestamp(note.modified).strftime("%Y-%m-%d")
        blocks.append(f"### {note.path.name} (modified {modified})\n{note.content}")
    return "\n\n".join(blocks)


def _get_weather(input_data: dict) -> str:
    from integrations import weather
    return weather.get_weather(
        input_data.get("location"), days=int(input_data.get("days") or 1)
    )


def _play_music(input_data: dict) -> str:
    from integrations import spotify
    return spotify.play_music(
        input_data["query"], kind=input_data.get("kind") or "track"
    )


def _control_playback(input_data: dict) -> str:
    from integrations import spotify
    return spotify.control_playback(
        input_data["action"], volume_percent=input_data.get("volume_percent")
    )


def _get_now_playing(input_data: dict) -> str:
    from integrations import spotify
    return spotify.now_playing()


def _recall(input_data: dict) -> str:
    from .memory import get_memory
    query = (input_data.get("query") or "").strip()
    limit = int(input_data.get("limit") or 5)
    items = get_memory().search(query, limit=limit)
    if not items:
        return (
            "(No relevant long-term memories found.)" if query
            else "(No long-term memories yet.)"
        )
    blocks = []
    for it in items:
        tag = "Fact" if it.kind == "fact" else "Session"
        when = (it.created_at or "")[:10]
        blocks.append(f"[{tag} · {when}] {it.content}")
    return "\n\n".join(blocks)


def _remember(input_data: dict) -> str:
    from .memory import get_memory
    content = (input_data.get("content") or "").strip()
    if not content:
        return "Error: remember needs a non-empty 'content' string."
    rid = get_memory().add_fact(content, source="remember-tool")
    if rid is None:
        return "Sorry, I couldn't save that to memory."
    return f"Noted — I'll remember that: {content}"


def _create_note(input_data: dict) -> str:
    from integrations import notes_watcher
    category = (input_data.get("category") or "").strip()
    content = (input_data.get("content") or "").strip()
    missing = [
        f for f, v in (("category", category), ("content", content)) if not v
    ]
    if missing:
        return (
            f"Error: create_note is missing required field(s): "
            f"{', '.join(missing)}. Provide them and call it once more."
        )
    return notes_watcher.create_note(
        category=category,
        content=content,
        title=input_data.get("title"),
        date=input_data.get("date"),
    )


def _create_calendar_event(input_data: dict) -> str:
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
        recurrence_interval=input_data.get("recurrence_interval"),
    )


def _update_calendar_event(input_data: dict) -> str:
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


def _get_todos(input_data: dict) -> str:
    from integrations import todoist
    return todoist.list_tasks(input_data.get("filter"))


def _create_todo(input_data: dict) -> str:
    from integrations import todoist
    return todoist.create_task(
        content=input_data["content"],
        category=input_data["category"],
        due_string=input_data.get("due_string"),
        description=input_data.get("description"),
        subtasks=input_data.get("subtasks"),
    )


def _complete_todo(input_data: dict) -> str:
    from integrations import todoist
    return todoist.complete_task(
        content=input_data["content"],
        due_hint=input_data.get("due_hint"),
    )


def _update_todo(input_data: dict) -> str:
    from integrations import todoist
    return todoist.update_task(
        content=input_data["content"],
        due_hint=input_data.get("due_hint"),
        new_content=input_data.get("new_content"),
        new_due_string=input_data.get("new_due_string"),
        new_description=input_data.get("new_description"),
        new_category=input_data.get("new_category"),
    )


def _get_meal_history(input_data: dict) -> str:
    from integrations import meal_prep
    return meal_prep.get_history(int(input_data.get("cycles_back") or 3))


def _create_meal_plan(input_data: dict) -> str:
    from integrations import meal_prep
    return meal_prep.create_cycle(
        start_date=input_data["start_date"],
        account_name=input_data["account_name"],
        calendar_name=input_data["calendar_name"],
        meals=input_data["meals"],
        shopping_list=input_data["shopping_list"],
        dinner_time=input_data.get("dinner_time") or meal_prep.DEFAULT_DINNER_TIME,
        lunch_time=input_data.get("lunch_time") or meal_prep.DEFAULT_LUNCH_TIME,
    )


def _set_personality(input_data: dict) -> str:
    from .persona import PERSONA
    return PERSONA.adjust(
        dial=input_data["dial"],
        set_to=input_data.get("set_to"),
        change_by=input_data.get("change_by"),
        persist=bool(input_data.get("persist", False)),
    )


def _load_knowledge_pool(input_data: dict) -> str:
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


def _get_emails(input_data: dict) -> str:
    from integrations import gmail
    return gmail.list_emails(
        query=input_data.get("query"),
        max_results=int(input_data.get("max_results") or 10),
        account=input_data.get("account"),
    )


def _create_email_draft(input_data: dict) -> str:
    from integrations import gmail
    return gmail.create_draft(
        to=input_data["to"],
        subject=input_data["subject"],
        body=input_data["body"],
        cc=input_data.get("cc"),
        account=input_data.get("account"),
    )


# ── Registrations ─────────────────────────────────────────────────────────────
# Order here is the order tools are advertised to the model. Base tools first,
# then conditionally-available email and Spotify tools, matching the original
# cached-prefix ordering.

register(Tool(
    name="get_calendar_events",
    description=(
        "Fetch the user's upcoming calendar events. Call this whenever the user "
        "asks about their schedule, appointments, meetings, what they have coming "
        "up, or what they're doing on a specific day. Also call it before "
        "creating or editing an event so you know the correct account_name and "
        "calendar_name to use. Entries tagged [Outlook-ICS] are free/busy only — "
        "no title or location data exists for them, so treat their 'Busy' summary "
        "as opaque and never invent a real title or location for one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "How many days ahead to look. Default 7.",
            },
        },
        "required": [],
    },
    handler=_get_calendar_events,
))

register(Tool(
    name="get_recent_notes",
    description=(
        "Load recent meeting notes from one category of the user's notes folder "
        f"— {_CAT_PHRASE} are kept in fully separate "
        "subfolders and must never be mixed or merged together in a single "
        "answer. Call this when the user asks about recent meetings, wants to "
        "reference notes, asks what was discussed or decided, or asks about "
        "action items. The user will often say which company/category they "
        "mean; if they don't and it's genuinely unclear which one, ask before "
        "calling this tool rather than guessing or fetching multiple categories."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(_CATEGORIES),
                "description": "Which notes stream to read. Ask the user if unclear — never guess.",
            },
        },
        "required": ["category"],
    },
    handler=_get_recent_notes,
))

register(Tool(
    name="create_note",
    description=(
        "Save a new meeting/conversation note to one category of the user's "
        f"notes folder — {_CAT_PHRASE} are kept in "
        "fully separate subfolders and must never be mixed. Use this when the "
        "user asks you to log, save, or write down a note about something (e.g. "
        "'make a note about my meeting with Sam on the 16th') instead of just "
        "summarizing in chat — capture what they actually told you about it "
        "(who, what was discussed, decisions, action items) rather than "
        "inventing detail they didn't give you. The user will often say which "
        "company/category the note belongs to; if they don't and it's genuinely "
        "unclear which one, ask before calling this tool rather than guessing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(_CATEGORIES),
                "description": "Which notes stream this belongs to. Ask the user if unclear — never guess.",
            },
            "content": {
                "type": "string",
                "description": "The note body — what was discussed, decided, follow-ups, etc.",
            },
            "title": {
                "type": "string",
                "description": "Short topic/title, e.g. 'Meeting with Sam'. Used in the filename and as a heading.",
            },
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD the note is about. Defaults to today if omitted.",
            },
        },
        "required": ["category", "content"],
    },
    handler=_create_note,
))

register(Tool(
    name="get_weather",
    description=(
        "Get current weather plus a daily forecast for a location. Call this "
        "when the user asks about the weather, whether to bring a jacket, "
        "outdoor plans, or as part of a daily briefing. If the user names a "
        "city use it; otherwise omit location to use their default. For "
        "tomorrow, the weekend, or any future day, set days to cover from "
        "today through that day (use today's date in your system prompt to "
        "count — e.g. if today is Wednesday, the weekend needs days≈4)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": (
                    "City/place to look up, e.g. 'Chicago, IL'. Omit to use "
                    "the user's configured default location."
                ),
            },
            "days": {
                "type": "integer",
                "description": (
                    "Number of forecast days to return, today through that many "
                    "days ahead (1-16). Default 1 (today only). Increase it when "
                    "the user asks about tomorrow or an extended/weekend outlook."
                ),
            },
        },
        "required": [],
    },
    handler=_get_weather,
))

register(Tool(
    name="recall",
    description=(
        "Search JARVIS's long-term memory — durable facts about the user plus "
        "summaries of past conversations — for anything relevant to the current "
        "question. Call it when the user refers back to an earlier conversation "
        "('what did we decide last time', 'pick up where we left off', 'what were "
        "we talking about yesterday'), asks what you know or remember about "
        "something, or when past context would clearly help. Pass a query of key "
        "terms to rank results by relevance; omit it to get the most recent "
        "memories. This is cross-session memory, separate from meeting notes (use "
        "get_recent_notes for those)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Key terms to search memory for, e.g. 'kitchen remodel budget'. "
                    "Omit to return the most recent memories instead."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max memories to return. Default 5.",
            },
        },
        "required": [],
    },
    handler=_recall,
))

register(Tool(
    name="remember",
    description=(
        "Save a durable fact or stable preference about the user to long-term "
        "memory so it's available in future conversations — e.g. the user says "
        "'remember that I'm allergic to shellfish', 'my anniversary is June 3', "
        "'I prefer short answers'. Use it for lasting facts, NOT transient details "
        "like today's schedule or this week's tasks. Keep each fact short and "
        "self-contained."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The fact or preference to remember, as a short self-contained "
                    "statement, e.g. 'Allergic to shellfish'."
                ),
            },
        },
        "required": ["content"],
    },
    handler=_remember,
))

register(Tool(
    name="update_calendar_event",
    description=(
        "Update (edit) an existing Google Calendar event. "
        "Finds the event by its current title; supply only the fields that "
        "should change — everything else is left untouched. "
        "Use this instead of create_calendar_event whenever the user asks to "
        "edit, reschedule, rename, or modify an existing event. "
        "Call get_calendar_events first if you need to confirm the account or calendar name."
    ),
    input_schema={
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
                "description": (
                    "New start as the user's LOCAL wall-clock time in ISO 8601 "
                    "with no timezone offset and no 'Z' (e.g. '2026-06-15T22:00:00'). "
                    "Don't convert to UTC. Omit to keep current."
                ),
            },
            "new_end": {
                "type": "string",
                "description": (
                    "New end as the user's LOCAL wall-clock time in ISO 8601 with "
                    "no offset. Omit to keep current."
                ),
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
    handler=_update_calendar_event,
))

register(Tool(
    name="create_calendar_event",
    description=(
        "Create an event on the user's Google Calendar. "
        "Call get_calendar_events first to see existing events — their source "
        "tags show the account and calendar name, e.g. '[Google/personal/Bills]' "
        "means account_name='personal', calendar_name='Bills'. "
        "Use existing events to infer appropriate timing when the user doesn't "
        "give exact times. Pass times as the user's LOCAL wall-clock time and "
        "do NOT convert to UTC or append 'Z' — JARVIS attaches the user's "
        "timezone itself."
    ),
    input_schema={
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
                    "Start as the user's LOCAL wall-clock time in ISO 8601 with "
                    "NO timezone offset and no 'Z' — e.g. 10pm is "
                    "'2026-06-15T22:00:00'. Don't convert to UTC. Only add an "
                    "offset if the user explicitly names a different timezone. "
                    "For all-day events use 'YYYY-MM-DD'."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "End as the user's LOCAL wall-clock time in ISO 8601 with no "
                    "offset (e.g. '2026-06-15T23:00:00'). Default to 1 hour "
                    "after start for unspecified durations."
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
                    "'every week' -> WEEKLY, 'every other week'/'biweekly' -> WEEKLY "
                    "with recurrence_interval=2. Omit entirely for a one-time event. "
                    "If set, you MUST also set exactly one of recurrence_count or "
                    "recurrence_until — never create an open-ended recurring event."
                ),
            },
            "recurrence_interval": {
                "type": "integer",
                "description": (
                    "Number of recurrence_freq units between occurrences. Omit or "
                    "use 1 for 'every week'/'every day'. Use 2 for 'every other "
                    "week'/'biweekly'/'fortnightly', 3 for 'every 3 months', etc."
                ),
            },
            "recurrence_count": {
                "type": "integer",
                "description": (
                    "Total occurrences including the first, e.g. 'for 5 weeks' -> 5, "
                    "'5 biweekly sessions' -> 5 (count occurrences, not calendar "
                    "weeks — recurrence_interval already covers the spacing). "
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
    handler=_create_calendar_event,
))

register(Tool(
    name="get_todos",
    description=(
        "Fetch the user's Todoist tasks. Call this whenever the user asks "
        "what's on their to-do list, what they need to do today, or about "
        "tasks in a specific category or date range."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": (
                    "Todoist filter query, e.g. 'today', 'overdue | today', "
                    "'tomorrow', a specific date like 'June 20', or a category "
                    f"like '#{_CAT_EXAMPLE}'. Defaults to overdue + today's tasks."
                ),
            },
        },
        "required": [],
    },
    handler=_get_todos,
))

register(Tool(
    name="create_todo",
    description=(
        "Add a new task to the user's Todoist. Always classify the task into "
        "one of the user's categories — ask the user if it's genuinely unclear "
        "which one fits. If the task is really a multi-step goal or a nested "
        "set of objectives (e.g. 'plan the team offsite' with steps like "
        "booking a venue, sending invites, ordering catering), pass each step "
        "as a string in subtasks instead of creating separate flat tasks or "
        "cramming them into one task's text — Todoist will nest them under "
        "the parent task as a checklist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The task text, e.g. 'Renew car registration'.",
            },
            "category": {
                "type": "string",
                "enum": list(_CATEGORIES),
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
            "subtasks": {
                "type": "array",
                "description": (
                    "Optional ordered list of step/objective strings to nest "
                    "under this task as Todoist subtasks, e.g. ['Book venue', "
                    "'Send invites', 'Order catering']. Omit for a plain "
                    "single-step task."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["content", "category"],
    },
    handler=_create_todo,
))

register(Tool(
    name="complete_todo",
    description=(
        "Mark an existing Todoist task as done. Finds the task by matching its "
        "text, so you don't need its exact ID."
    ),
    input_schema={
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
    handler=_complete_todo,
))

register(Tool(
    name="update_todo",
    description=(
        "Edit an existing Todoist task — rename it, reschedule it, change its "
        "notes, or move it to a different category. Finds the task by matching "
        "its current text; supply only the fields that should change. Use this "
        "instead of create_todo whenever the user asks to edit, reschedule, or "
        "recategorize an existing task."
    ),
    input_schema={
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
                "enum": list(_CATEGORIES),
                "description": "New category/project (omit to keep current).",
            },
        },
        "required": ["content"],
    },
    handler=_update_todo,
))

register(Tool(
    name="get_meal_history",
    description=(
        "Look up recent 2-week dinner-plan cycles, including the active one if "
        "any. Call this before proposing a new meal plan so you can avoid "
        "repeating recent dinners, and whenever the user asks what's for "
        "dinner or what they ate recently."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "cycles_back": {
                "type": "integer",
                "description": "How many past cycles to include. Default 3.",
            },
        },
        "required": [],
    },
    handler=_get_meal_history,
))

register(Tool(
    name="create_meal_plan",
    description=(
        "Save an approved 2-week meal plan: creates one calendar event per "
        "meal and one Todoist Groceries task per shopping-list item, then "
        "records the cycle. Dinners default to 5:30 PM Eastern Time and "
        "lunches (only if the user wants lunches planned too) default to "
        "12:30 PM Eastern — don't change these unless the user asks for a "
        "different time. Before calling this tool: "
        "(1) call get_calendar_events with days set high enough to cover every "
        "date from today through the last day of the planning window, and check "
        "it for anything that overlaps a dinner (or lunch) date — trips, flights, "
        "multi-day or all-day events, evenings already booked, etc. For each "
        "affected date, ask the user directly what they want to do (e.g. 'It "
        "looks like you'll be on a trip from the 19th to the 21st — want "
        "travel-friendly snacks/food for those days, or should I just skip "
        "planned dinners then?') and use their answer instead of guessing; "
        "(2) ask the user what meat and produce they currently have on hand "
        "and roughly how long each item has been stored, and schedule the "
        "most perishable items earliest in the cycle; "
        "(3) call get_meal_history to avoid repeating recent dinners; "
        "(4) use web search for recipe ideas that fit the user's preferences, "
        "what they already have, and any travel days flagged in step 1; "
        "(5) present the full overview — date, dish, and a recipe link "
        "or short recipe summary for each (noting any day intentionally left "
        "without a planned dinner) — plus the shopping list, and wait "
        "for explicit approval. Never call this tool without that approval. "
        "Default to account_name='personal' and calendar_name='Family' for "
        "meal events unless the user says otherwise; get_calendar_events also "
        "tells you if those names need confirming."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "account_name": {
                "type": "string",
                "description": "Google account name from the source tags, e.g. 'personal'.",
            },
            "calendar_name": {
                "type": "string",
                "description": "Calendar name from the source tags, e.g. 'Family'.",
            },
            "start_date": {
                "type": "string",
                "description": "First day of the 2-week stretch, YYYY-MM-DD.",
            },
            "dinner_time": {
                "type": "string",
                "description": (
                    "24-hour HH:MM Eastern Time start for each dinner event. "
                    "Default '17:30' (5:30 PM ET) — only override if the user "
                    "asks for a different dinner time."
                ),
            },
            "lunch_time": {
                "type": "string",
                "description": (
                    "24-hour HH:MM Eastern Time start for each lunch event "
                    "(meals with meal_type='lunch'). Default '12:30' (12:30 PM ET)."
                ),
            },
            "meals": {
                "type": "array",
                "description": "One entry per meal date in the cycle (dinners, plus lunches if the user wants those too).",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD."},
                        "dish": {"type": "string", "description": "Meal name, e.g. 'Sheet-pan chicken fajitas'."},
                        "meal_type": {
                            "type": "string",
                            "enum": ["dinner", "lunch"],
                            "description": "Defaults to 'dinner' if omitted. Use 'lunch' for a midday meal.",
                        },
                        "notes": {
                            "type": "string",
                            "description": (
                                "Recipe link or brief recipe text for this dish — "
                                "becomes the calendar event's description, so include "
                                "it for every meal unless the user explicitly says "
                                "they don't want one."
                            ),
                        },
                    },
                    "required": ["date", "dish"],
                },
            },
            "shopping_list": {
                "type": "array",
                "description": "Consolidated grocery items for the whole cycle.",
                "items": {"type": "string"},
            },
        },
        "required": ["account_name", "calendar_name", "start_date", "meals", "shopping_list"],
    },
    handler=_create_meal_plan,
))

register(Tool(
    name="set_personality",
    description=(
        "Adjust how you (JARVIS) speak by changing one of your voice dials, "
        "each 0-100. Call this whenever the user asks you to change your tone or "
        "personality — e.g. 'turn up the sarcasm', 'humor down 15%', 'max "
        "formality', 'be more concise', 'stop calling me Sir', 'reset your "
        "personality'. Dials: brevity (terseness), formality (how refined and "
        "butler-like, e.g. saying 'Sir'), humor (jokes/levity), sarcasm (dry "
        "wit), proactivity (anticipating needs / volunteering suggestions). The "
        "current value of each dial is shown in the Voice Dials section of your "
        "system prompt — use it to resolve relative requests: for 'down 15%' "
        "pass change_by:-15, for 'a bit more' pass a small positive change_by, "
        "for an explicit level pass set_to. Changes apply to this session only "
        "unless persist is true. To restore every dial to its default, pass "
        "dial:'reset'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "dial": {
                "type": "string",
                "enum": [
                    "brevity", "formality", "humor", "sarcasm",
                    "proactivity", "reset",
                ],
                "description": "Which dial to change, or 'reset' to restore all defaults.",
            },
            "set_to": {
                "type": "integer",
                "description": (
                    "Absolute new value 0-100. Use for explicit levels: 'max "
                    "formality' -> 100, 'no sarcasm' -> 0, 'set humor to 10' -> 10."
                ),
            },
            "change_by": {
                "type": "integer",
                "description": (
                    "Relative change in points, e.g. -15 for 'humor down 15%', "
                    "+20 for 'a lot more sarcasm'. Ignored when set_to is given."
                ),
            },
            "persist": {
                "type": "boolean",
                "description": (
                    "If true, save as the new default for future sessions too. "
                    "Default false — applies to the current session only."
                ),
            },
        },
        "required": ["dial"],
    },
    handler=_set_personality,
))

register(Tool(
    name="load_knowledge_pool",
    description=(
        "Load content from a named Google Docs knowledge pool to help answer the "
        "user's question. Call this when a question relates to a topic listed in "
        "the Available Knowledge Pools section of your system prompt (e.g. 'work', "
        "'finances', 'personal'). Do NOT call it for general questions unrelated "
        "to those topics."
    ),
    input_schema={
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
    handler=_load_knowledge_pool,
))

# Email tools are only surfaced when Gmail is configured (GMAIL_ENABLED=true +
# Google credentials present), preserving the original conditional ordering.
register(Tool(
    name="get_emails",
    description=(
        "List the user's recent emails (sender, subject, date, snippet). Call "
        "this when the user asks about their inbox, recent mail, unread "
        "messages, or wants you to find or summarize an email. Searches all "
        "configured Gmail accounts by default; each result line is tagged with "
        "its source account, e.g. '[work]'. Use the query to narrow results "
        "with Gmail search syntax, e.g. 'is:unread', 'from:sam', "
        "'newer_than:7d', 'subject:invoice'. Defaults to the inbox."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Gmail search query, e.g. 'is:unread', 'from:boss "
                    "newer_than:3d'. Omit for the most recent inbox messages."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "How many emails to return per account (1-25). Default 10.",
            },
            "account": {
                "type": "string",
                "description": (
                    "Restrict the search to one configured Gmail account by its "
                    "name (the tag shown in earlier results, e.g. 'work'). Omit "
                    "to search every account at once."
                ),
            },
        },
        "required": [],
    },
    handler=_get_emails,
    available=lambda: CONFIG.gmail_available,
))

register(Tool(
    name="create_email_draft",
    description=(
        "Create a Gmail draft for the user to review and send themselves. "
        "JARVIS never sends mail automatically — this only saves a draft. Use "
        "it when the user asks you to write, draft, or reply to an email. "
        "Confirm the recipient and the gist with the user if either is unclear "
        "rather than inventing an address or details. When several Gmail "
        "accounts are configured you MUST set account; if it's unclear which "
        "one the draft should come from, ask the user before drafting."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Email subject line."},
            "body": {
                "type": "string",
                "description": "The full body text of the email.",
            },
            "cc": {
                "type": "string",
                "description": "Optional CC recipient(s), comma-separated.",
            },
            "account": {
                "type": "string",
                "description": (
                    "Which configured Gmail account to save the draft under "
                    "(the account name, e.g. 'work'). Required when more than "
                    "one account is configured."
                ),
            },
        },
        "required": ["to", "subject", "body"],
    },
    handler=_create_email_draft,
    available=lambda: CONFIG.gmail_available,
))

# Spotify tools are only surfaced when Spotify is configured (SPOTIFY_ENABLED +
# a client id), preserving the original conditional ordering.
register(Tool(
    name="play_music",
    description=(
        "Start playing music on the user's Spotify. Use this when they ask to "
        "play a song, artist, album, or playlist by name (e.g. 'play Back in "
        "Black', 'play some Daft Punk', 'put on my Focus playlist'). You can "
        "also pass a Spotify link or URI the user pasted to play that exact "
        "item. Plays the best match — exact song titles are preferred over an "
        "artist's most-streamed track. If the Spotify app isn't open yet, "
        "this automatically launches it on the user's computer and starts "
        "playback — so just call it; do NOT tell the user you can't reach a "
        "device or ask them to open Spotify first. Requires Spotify Premium. "
        "Use control_playback for pause/skip/volume and get_now_playing to see "
        "what's on."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to play, e.g. 'Back in Black', 'Daft Punk', 'Focus'.",
            },
            "kind": {
                "type": "string",
                "enum": ["track", "album", "artist", "playlist"],
                "description": (
                    "What to search for. Default 'track'. Use 'artist' for "
                    "'play some <artist>', 'playlist' for a named playlist, "
                    "'album' for a full album."
                ),
            },
        },
        "required": ["query"],
    },
    handler=_play_music,
    available=lambda: CONFIG.spotify_available,
))

register(Tool(
    name="control_playback",
    description=(
        "Control current Spotify playback: pause, resume, skip to the next or "
        "previous track, toggle shuffle, or set the volume. Use this for "
        "transport and volume requests rather than play_music (which starts "
        "something new)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "pause", "resume", "next", "previous",
                    "shuffle_on", "shuffle_off", "volume",
                ],
                "description": "The playback action to perform.",
            },
            "volume_percent": {
                "type": "integer",
                "description": "0-100 volume level. Required only when action is 'volume'.",
            },
        },
        "required": ["action"],
    },
    handler=_control_playback,
    available=lambda: CONFIG.spotify_available,
))

register(Tool(
    name="get_now_playing",
    description=(
        "Report what's currently playing on the user's Spotify (track and "
        "artist), or that nothing is playing."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    handler=_get_now_playing,
    available=lambda: CONFIG.spotify_available,
))
