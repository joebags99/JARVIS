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

MAX_TOKENS = 2048
# Meal-plan tool calls carry 14 dinners with recipe text plus a shopping
# list — easily several times MAX_TOKENS — so they get a bigger budget for
# that turn instead of risking a truncated, invalid tool_use JSON payload.
_MEAL_MAX_TOKENS = 4096
# If a round still hits the output cap *while emitting a tool_use*, its JSON
# arguments are truncated and would fail to execute (e.g. a half-written
# create_note loses its 'content'). We re-run that round once with this larger
# budget rather than running a malformed call. Output is billed per token
# actually generated, so a high ceiling costs nothing on normal short replies.
_MAX_TOKENS_CEILING = 8192
MAX_TOOL_ROUNDS = 16  # safety cap on local tool_use <-> tool_result round trips

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

# Meal-related keywords used to decide whether to attach the native web
# search tool — same reasoning as _FINANCIAL_KEYWORDS: only pay for it when
# the conversation is actually about food.
_MEAL_KEYWORDS = (
    "meal", "dinner", "recipe", "cook", "cooking", "groceries", "grocery",
    "meal prep", "meal plan", "what's for dinner", "leftovers",
)

# Native server-side web search tool (no separate API key — billed through
# the Anthropic account). Dated tool versions follow the same convention as
# other Claude tool types; bump this if Anthropic ships a newer one.
_WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}

# History compaction: once a session accumulates more than this many user
# turns, older turns are summarized away to bound per-request token cost.
HISTORY_MAX_TURNS = 8
HISTORY_KEEP_TURNS = 3
# When compacting, fat tool_result payloads (calendar dumps, note bodies, email
# lists) from older turns are truncated to this many chars — the model already
# acted on them, so the full text no longer needs to ride along every request.
TOOL_RESULT_KEEP_CHARS = 600


def _looks_financial(message: str) -> bool:
    lowered = message.lower()
    return any(kw in lowered for kw in _FINANCIAL_KEYWORDS)


def _looks_meal_related(message: str) -> bool:
    lowered = message.lower()
    return any(kw in lowered for kw in _MEAL_KEYWORDS)

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
            "Load recent meeting notes from one category of the user's notes folder "
            "— Daedabyte, Brightpoint, DnD, and General are kept in fully separate "
            "subfolders and must never be mixed or merged together in a single "
            "answer. Call this when the user asks about recent meetings, wants to "
            "reference notes, asks what was discussed or decided, or asks about "
            "action items. The user will often say which company/category they "
            "mean; if they don't and it's genuinely unclear which one, ask before "
            "calling this tool rather than guessing or fetching multiple categories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["Daedabyte", "General", "Brightpoint", "DnD"],
                    "description": "Which notes stream to read. Ask the user if unclear — never guess.",
                },
            },
            "required": ["category"],
        },
    },
    {
        "name": "create_note",
        "description": (
            "Save a new meeting/conversation note to one category of the user's "
            "notes folder — Daedabyte, Brightpoint, DnD, and General are kept in "
            "fully separate subfolders and must never be mixed. Use this when the "
            "user asks you to log, save, or write down a note about something (e.g. "
            "'make a note about my meeting with Sam on the 16th') instead of just "
            "summarizing in chat — capture what they actually told you about it "
            "(who, what was discussed, decisions, action items) rather than "
            "inventing detail they didn't give you. The user will often say which "
            "company/category the note belongs to; if they don't and it's genuinely "
            "unclear which one, ask before calling this tool rather than guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["Daedabyte", "General", "Brightpoint", "DnD"],
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
    },
    {
        "name": "get_weather",
        "description": (
            "Get current weather plus a daily forecast for a location. Call this "
            "when the user asks about the weather, whether to bring a jacket, "
            "outdoor plans, or as part of a daily briefing. If the user names a "
            "city use it; otherwise omit location to use their default. For "
            "tomorrow, the weekend, or any future day, set days to cover from "
            "today through that day (use today's date in your system prompt to "
            "count — e.g. if today is Wednesday, the weekend needs days≈4)."
        ),
        "input_schema": {
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
    },
    {
        "name": "recall_session_history",
        "description": (
            "Recall summaries of recent past JARVIS conversations. JARVIS auto-saves "
            "a short summary whenever a session with several exchanges is closed, so "
            "this is your cross-session memory. Call it when the user refers back to "
            "an earlier conversation — 'what did we decide last time', 'pick up where "
            "we left off', 'what were we talking about yesterday' — or when earlier "
            "context would clearly help. This is separate from meeting notes; use "
            "get_recent_notes for those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent session summaries to load. Default 3.",
                },
            },
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
            "which one fits. If the task is really a multi-step goal or a nested "
            "set of objectives (e.g. 'plan the team offsite' with steps like "
            "booking a venue, sending invites, ordering catering), pass each step "
            "as a string in subtasks instead of creating separate flat tasks or "
            "cramming them into one task's text — Todoist will nest them under "
            "the parent task as a checklist."
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
                    "enum": ["Daedabyte", "General", "Brightpoint", "DnD"],
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
                    "enum": ["Daedabyte", "General", "Brightpoint", "DnD"],
                    "description": "New category/project (omit to keep current).",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_meal_history",
        "description": (
            "Look up recent 2-week dinner-plan cycles, including the active one if "
            "any. Call this before proposing a new meal plan so you can avoid "
            "repeating recent dinners, and whenever the user asks what's for "
            "dinner or what they ate recently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cycles_back": {
                    "type": "integer",
                    "description": "How many past cycles to include. Default 3.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "create_meal_plan",
        "description": (
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
        "input_schema": {
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
    },
    {
        "name": "set_personality",
        "description": (
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
        "input_schema": {
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


# Email tools are only surfaced when Gmail is configured (GMAIL_ENABLED=true +
# Google credentials present). Appended to TOOLS at import time so the cached
# tool prefix stays constant for a given install.
EMAIL_TOOLS = [
    {
        "name": "get_emails",
        "description": (
            "List the user's recent emails (sender, subject, date, snippet). Call "
            "this when the user asks about their inbox, recent mail, unread "
            "messages, or wants you to find or summarize an email. Searches all "
            "configured Gmail accounts by default; each result line is tagged with "
            "its source account, e.g. '[work]'. Use the query to narrow results "
            "with Gmail search syntax, e.g. 'is:unread', 'from:sam', "
            "'newer_than:7d', 'subject:invoice'. Defaults to the inbox."
        ),
        "input_schema": {
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
    },
    {
        "name": "create_email_draft",
        "description": (
            "Create a Gmail draft for the user to review and send themselves. "
            "JARVIS never sends mail automatically — this only saves a draft. Use "
            "it when the user asks you to write, draft, or reply to an email. "
            "Confirm the recipient and the gist with the user if either is unclear "
            "rather than inventing an address or details. When several Gmail "
            "accounts are configured you MUST set account; if it's unclear which "
            "one the draft should come from, ask the user before drafting."
        ),
        "input_schema": {
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
    },
]

if CONFIG.gmail_available:
    TOOLS = TOOLS + EMAIL_TOOLS


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
        category = input_data["category"]
        notes = notes_watcher.read_recent_notes(category, 5, 2000)
        if not notes:
            return f"(No {category} notes yet.)"
        blocks = []
        for note in notes:
            modified = dt.datetime.fromtimestamp(note.modified).strftime("%Y-%m-%d")
            blocks.append(f"### {note.path.name} (modified {modified})\n{note.content}")
        return "\n\n".join(blocks)
    if name == "get_weather":
        from integrations import weather
        return weather.get_weather(
            input_data.get("location"), days=int(input_data.get("days") or 1)
        )
    if name == "recall_session_history":
        import datetime as dt
        from integrations import notes_watcher
        summaries = notes_watcher.read_recent_session_summaries(
            int(input_data.get("limit") or 3)
        )
        if not summaries:
            return "(No past session summaries yet.)"
        blocks = []
        for note in summaries:
            modified = dt.datetime.fromtimestamp(note.modified).strftime("%Y-%m-%d %H:%M")
            blocks.append(f"### {note.path.name} (saved {modified})\n{note.content}")
        return "\n\n".join(blocks)
    if name == "create_note":
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
            recurrence_interval=input_data.get("recurrence_interval"),
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
            subtasks=input_data.get("subtasks"),
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
    if name == "get_meal_history":
        from integrations import meal_prep
        return meal_prep.get_history(int(input_data.get("cycles_back") or 3))
    if name == "create_meal_plan":
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
    if name == "set_personality":
        from .persona import PERSONA
        return PERSONA.adjust(
            dial=input_data["dial"],
            set_to=input_data.get("set_to"),
            change_by=input_data.get("change_by"),
            persist=bool(input_data.get("persist", False)),
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
    if name == "get_emails":
        from integrations import gmail
        return gmail.list_emails(
            query=input_data.get("query"),
            max_results=int(input_data.get("max_results") or 10),
            account=input_data.get("account"),
        )
    if name == "create_email_draft":
        from integrations import gmail
        return gmail.create_draft(
            to=input_data["to"],
            subject=input_data["subject"],
            body=input_data["body"],
            cc=input_data.get("cc"),
            account=input_data.get("account"),
        )
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


def _with_message_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Return ``messages`` with an ephemeral cache breakpoint on the final block.

    Prompt caching is a prefix match over ``tools → system → messages``. The
    tools+system prefix already carries its own breakpoint, but the *messages*
    array is otherwise re-billed as fresh input on every request. A single
    ``send()`` can fire up to MAX_TOOL_ROUNDS API calls, each re-sending the
    whole (and growing) history; follow-up turns in a session re-send it again.

    Marking the last block moves the breakpoint to the end of the conversation
    each round. The previous request's breakpoint is now a prefix of this one,
    so the API serves that span from cache (a ~10x cheaper read) and only the
    newest turn is written fresh — the standard incremental multi-turn pattern.

    The stored history is never mutated: only a shallow copy down to the last
    block is made, so cache_control markers never accumulate or persist.
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    breakpoint = {"type": "ephemeral"}
    if isinstance(content, str):
        if not content:
            return messages  # empty text block would be rejected by the API
        last["content"] = [
            {"type": "text", "text": content, "cache_control": breakpoint}
        ]
    elif isinstance(content, list) and content:
        new_content = list(content)
        block = dict(new_content[-1])
        block["cache_control"] = breakpoint
        new_content[-1] = block
        last["content"] = new_content
    else:
        return messages  # nothing markable (e.g. empty content list)
    return messages[:-1] + [last]


class _MonarchUnavailable(Exception):
    """Internal signal that the Monarch MCP server couldn't be reached this turn.

    Raised from the per-turn helper so ``send`` can retry once with the MCP
    server detached instead of failing the whole answer."""


def _is_mcp_connection_error(exc: Exception) -> bool:
    """True for the transient 400 the API raises when it can't reach an MCP server.

    Monarch's hosted MCP server occasionally goes unavailable/unresponsive; the
    API surfaces that as ``invalid_request_error`` with a "Connection error
    while communicating with MCP server" message. That's not a problem with the
    request itself, so it's worth retrying the turn without the MCP server
    attached rather than failing the whole answer.
    """
    msg = str(exc).lower()
    return "mcp server" in msg and (
        "connection error" in msg or "unavailable" in msg or "unresponsive" in msg
    )


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
        # Same stickiness for meal-prep conversations and the web search tool.
        self._meal_active = False
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
        self._meal_active = False
        # Voice-dial tweaks are scoped to a conversation ("...for this convo"),
        # so a fresh session restores the saved defaults.
        from .persona import PERSONA
        PERSONA.reset()
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
                model=CONFIG.summary_model,
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
        self._trim_old_tool_results()
        log.info("compacted %d older turn(s) into a summary", len(boundaries) - HISTORY_KEEP_TURNS)

    def _trim_old_tool_results(self) -> None:
        """Truncate large tool_result payloads from all but the most recent turn.

        Only touches the ``tool_result`` blocks JARVIS itself appends (plain
        string content in user-role messages) and leaves the latest turn's
        results full, so the current working context is untouched while stale
        calendar/notes/email dumps stop riding along on every request.
        """
        boundaries = [
            i for i, m in enumerate(self.history)
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        cutoff = boundaries[-1] if boundaries else len(self.history)
        trimmed = 0
        for m in self.history[:cutoff]:
            if m.get("role") != "user" or not isinstance(m.get("content"), list):
                continue
            for block in m["content"]:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                content = block.get("content")
                if isinstance(content, str) and len(content) > TOOL_RESULT_KEEP_CHARS:
                    block["content"] = (
                        content[:TOOL_RESULT_KEEP_CHARS].rstrip()
                        + "\n…(older tool output trimmed to save context)"
                    )
                    trimmed += 1
        if trimmed:
            log.info("trimmed %d older tool-result block(s) during compaction", trimmed)

    def send(
        self,
        user_message: str,
        on_delta: Callable[[str], None] | None = None,
        on_reset: Callable[[], None] | None = None,
    ) -> str:
        """Send a message with full context; stream deltas via ``on_delta``.

        Handles tool use transparently: if Claude calls a tool (e.g.
        create_calendar_event), the tool executes locally and Claude streams a
        confirmation response. Returns the complete assistant reply. Never
        raises — failures return a readable error string.

        ``on_delta`` receives each streamed text chunk. ``on_reset`` is called
        whenever a round's pre-tool text is discarded (the model spoke, then
        decided to call a tool) so the UI can roll its live stream back to a
        "thinking" state instead of showing text that's about to be replaced.
        """
        if not self.ready:
            return self._init_error or "Claude client is not available."

        self._compact_history()
        history_snapshot = len(self.history)
        self.history.append({"role": "user", "content": user_message})
        system_prompt = self.context.build_system_prompt()

        # The Monarch MCP path needs parallel tool use DISABLED (so a local
        # tool_use and an mcp_tool_use never land in the same turn and leave
        # a dangling block); the web-search path needs it ENABLED (the API
        # rejects disable_parallel_tool_use alongside that programmatic
        # server tool). Those requirements are mutually exclusive, so at
        # most one may attach per turn. Both flags stay sticky for the
        # session, so we pick per-turn by the *current* message's topic:
        # finance wins a finance message, meal wins a meal message, and a
        # neutral follow-up favors Monarch's parallel-safe path.
        fin_now = _looks_financial(user_message)
        meal_now = _looks_meal_related(user_message)
        if fin_now:
            self._monarch_active = True
        if meal_now:
            self._meal_active = True

        want_monarch = CONFIG.monarch_enabled and (
            fin_now or (self._monarch_active and not meal_now)
        )
        want_web = meal_now or (self._meal_active and not fin_now)
        if want_monarch and want_web:
            want_web = False  # can't share a turn — keep the MCP-safe path

        # If Monarch's hosted MCP server is unreachable mid-turn the API rejects
        # the whole request; rather than fail the answer we retry once with the
        # MCP server detached, so the user still gets a (finance-less) reply.
        allow_monarch = True
        while True:
            base_kwargs: dict = dict(
                model=CONFIG.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=TOOLS,
                # Allow parallel tool use so the model can batch several actions
                # (e.g. one note + six to-dos) into a single round instead of
                # burning one round each and exhausting MAX_TOOL_ROUNDS. Only the
                # Monarch MCP path re-disables this (in _run_turn), where a local
                # tool_use sharing a turn with an mcp_tool_use leaves a dangling,
                # unpaired block.
                tool_choice={"type": "auto"},
            )
            try:
                return self._run_turn(
                    base_kwargs, want_monarch, want_web, allow_monarch,
                    on_delta, on_reset,
                )
            except _MonarchUnavailable as exc:
                log.warning(
                    "Monarch MCP server unreachable; retrying without finance data: %s",
                    exc.__cause__,
                )
                del self.history[history_snapshot + 1:]  # keep the user message
                allow_monarch = False
                continue
            except Exception as exc:  # noqa: BLE001
                log.error("Claude API call failed: %s", exc)
                # Roll back the entire failed turn (the user message plus any
                # partial assistant/tool_result exchanges already appended this
                # round) so a dangling tool_use can never linger into the next
                # request — truncating to the pre-turn length handles that
                # regardless of which round the failure happened in.
                del self.history[history_snapshot:]
                return f"⚠️ Error contacting Claude: {exc}"

    def _run_turn(
        self,
        base_kwargs: dict,
        want_monarch: bool,
        want_web: bool,
        allow_monarch: bool,
        on_delta: Callable[[str], None] | None,
        on_reset: Callable[[], None] | None,
    ) -> str:
        """Run one full tool-use loop and return the assistant's reply.

        Attaches the Monarch MCP server (or web search) per the want_* flags.
        Raises ``_MonarchUnavailable`` if the MCP server can't be reached so the
        caller can retry without it; any other failure propagates for the caller
        to roll back and surface.
        """
        full_text = ""
        last_pretext = ""  # most recent pre-tool narration, kept as a fallback
        stream_fn = self._client.messages.stream
        monarch_attached = False
        if want_monarch and allow_monarch:
            try:
                from integrations.monarch_oauth import get_monarch_token
                base_kwargs["mcp_servers"] = [{
                    "type": "url",
                    "name": "monarch",
                    "url": _MONARCH_MCP_URL,
                    "authorization_token": get_monarch_token(),
                }]
                base_kwargs["betas"] = [_MCP_BETA]
                # Keep local and MCP tool calls on separate rounds so a local
                # tool_use never shares a turn with an mcp_tool_use (which would
                # leave a dangling, unpaired block).
                base_kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
                stream_fn = self._client.beta.messages.stream
                monarch_attached = True
                log.info("using beta endpoint with monarch mcp_servers attached")
            except Exception as exc:
                log.error("Monarch token error, falling back to plain endpoint: %s", exc, exc_info=True)
        elif want_web:
            base_kwargs["tools"] = TOOLS + [_WEB_SEARCH_TOOL]
            base_kwargs["tool_choice"] = {"type": "auto"}
            base_kwargs["max_tokens"] = _MEAL_MAX_TOKENS
            log.info("web search tool attached (meal-related conversation)")

        try:
            # ── Bounded tool-use loop: keep tools/mcp_servers attached on every
            # round so local tools (e.g. load_knowledge_pool) and the Monarch
            # MCP tool can both be used for one question, just sequentially.
            for round_num in range(1, MAX_TOOL_ROUNDS + 1):
                messages = _with_message_cache_breakpoint(self.history)
                with stream_fn(messages=messages, **base_kwargs) as stream:
                    for text in stream.text_stream:
                        full_text += text
                        if on_delta:
                            on_delta(text)
                    final_msg = stream.get_final_message()

                log.info(
                    "round %d content block types: %s",
                    round_num, [b.type for b in final_msg.content],
                )

                tool_uses = [b for b in final_msg.content if b.type == "tool_use"]

                # If the output cap was hit *while emitting a tool_use*, the
                # tool's JSON arguments are truncated and would fail to execute
                # (e.g. a long create_note loses its 'content'). Discard this
                # half-formed attempt and re-run the round with a bigger budget,
                # rather than running a broken call and looping on the error.
                if (
                    tool_uses
                    and final_msg.stop_reason == "max_tokens"
                    and base_kwargs.get("max_tokens", MAX_TOKENS) < _MAX_TOKENS_CEILING
                ):
                    log.warning(
                        "round %d truncated mid tool_use (max_tokens); retrying with %d-token budget",
                        round_num, _MAX_TOKENS_CEILING,
                    )
                    base_kwargs["max_tokens"] = _MAX_TOKENS_CEILING
                    if full_text and on_reset:
                        on_reset()
                    full_text = ""
                    continue

                self.history.append({
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in final_msg.content],
                })

                if not tool_uses:
                    break

                # Discard any pre-tool text streamed this round — the real
                # answer comes after the tool result, not before it. Tell the UI
                # to roll its live stream back to "thinking" so the discarded
                # text doesn't linger on screen until the next round renders.
                # Keep the most recent narration, though: if we later exhaust the
                # round budget and the forced wrap-up comes back empty, it's the
                # best summary we have of what just happened.
                if full_text.strip():
                    last_pretext = full_text
                if full_text and on_reset:
                    on_reset()
                full_text = ""

                results = []
                for tu in tool_uses:
                    is_error = False
                    try:
                        outcome = _execute_tool(tu.name, tu.input)
                        # Integrations report handled failures as "Error: ..."
                        # strings — flag those so the model treats them as
                        # failures to recover from, not as data.
                        is_error = outcome.lstrip().startswith("Error")
                    except Exception as exc:  # noqa: BLE001
                        # A tool_use block always needs a paired tool_result —
                        # if execution raises (e.g. a required field was missing
                        # from a truncated/malformed call), report it as the
                        # result instead of letting it escape and leave a
                        # dangling tool_use in history for the next request.
                        log.error("tool %s raised: %s", tu.name, exc)
                        outcome = (
                            f"Error: tool '{tu.name}' failed ({exc}). "
                            "Check that all required fields were provided and try again."
                        )
                        is_error = True
                    log.info("tool %s -> %r", tu.name, outcome[:120])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": outcome,
                        "is_error": is_error,
                    })
                self.history.append({"role": "user", "content": results})
            else:
                log.warning("hit max tool rounds (%d); forcing a final answer", MAX_TOOL_ROUNDS)
                try:
                    forced_kwargs = dict(base_kwargs)
                    forced_kwargs["tool_choice"] = {"type": "none"}
                    full_text = ""
                    messages = _with_message_cache_breakpoint(self.history)
                    with stream_fn(messages=messages, **forced_kwargs) as stream:
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
                    full_text = ""
                # The model often goes quiet after a long run of actions (it
                # already "narrated" in pre-tool text we discarded each round).
                # Fall back to that last narration, or a generic confirmation,
                # so the user never gets an empty reply.
                if not full_text.strip():
                    full_text = last_pretext.strip() or (
                        "Done — I've completed those actions. Check your "
                        "calendar, notes, and to-dos to confirm."
                    )
                    if on_delta:
                        on_delta(full_text)

            log.info("response received (%d chars)", len(full_text))
            return full_text

        except Exception as exc:  # noqa: BLE001
            # A transient Monarch MCP outage is recoverable — signal the caller
            # to retry without it. Everything else propagates for the caller to
            # roll back the turn and surface a readable error.
            if monarch_attached and _is_mcp_connection_error(exc):
                raise _MonarchUnavailable from exc
            raise
