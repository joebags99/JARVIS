"""Meal-prep integration: 2-week dinner planning.

Persists each planning cycle to ``meal_plans.json`` (gitignored — personal
data, same convention as ``knowledge_pools.json``) and fans out to the
existing Google Calendar and Todoist integrations: one calendar event per
meal, one Todoist task per shopping-list item (filed under a "Groceries"
project, created on first use the same way Todoist categories already are).

Recipe discovery itself happens in conversation via Claude's native web
search tool (gated in ``app/claude_client.py``) — this module only persists
the plan once the user has approved it and fans it out to the calendars/
task list.

Meal times are anchored to US Eastern Time regardless of the machine's
local timezone or the JARVIS_TIMEZONE override — the user wants dinners at
5:30 PM and lunches at 12:30 PM Eastern specifically, not "whatever the
system clock says." Built with zoneinfo (stdlib) rather than trusting an
LLM-supplied UTC offset, same reasoning as the recurrence-rule handling in
google_calendar.py.
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from app.config import ROOT_DIR
from app.logging_setup import get_logger

log = get_logger("meal_prep")

PLANS_FILE = ROOT_DIR / "meal_plans.json"
GROCERIES_CATEGORY = "Groceries"
CYCLE_DAYS = 14

_EASTERN = ZoneInfo("America/New_York")
DEFAULT_DINNER_TIME = "17:30"  # 5:30 PM ET
DEFAULT_LUNCH_TIME = "12:30"  # 12:30 PM ET


def _load() -> dict:
    if not PLANS_FILE.exists():
        return {"cycles": []}
    try:
        return json.loads(PLANS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", PLANS_FILE.name, exc)
        return {"cycles": []}


def _save(data: dict) -> None:
    PLANS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_history(cycles_back: int = 3) -> str:
    """Format the most recent meal-plan cycles for Claude. Never raises."""
    data = _load()
    cycles = data.get("cycles", [])
    if not cycles:
        return "(No meal plans yet.)"

    today = dt.date.today().isoformat()
    blocks = []
    for cycle in cycles[-cycles_back:]:
        active = " (active)" if cycle["start_date"] <= today <= cycle["end_date"] else ""
        lines = [
            f"### {cycle['start_date']} to {cycle['end_date']}{active}",
        ]
        for meal in cycle.get("meals", []):
            note = f" — {meal['notes']}" if meal.get("notes") else ""
            kind = " (lunch)" if (meal.get("meal_type") or "dinner") == "lunch" else ""
            lines.append(f"- {meal['date']}: {meal['dish']}{kind}{note}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def create_cycle(
    start_date: str,
    account_name: str,
    calendar_name: str,
    meals: list[dict],
    shopping_list: list[str],
    dinner_time: str = DEFAULT_DINNER_TIME,
    lunch_time: str = DEFAULT_LUNCH_TIME,
) -> str:
    """Create calendar events + Groceries tasks for a 2-week meal plan, then persist it.

    Each meal's optional ``meal_type`` ("dinner", the default, or "lunch")
    picks its event time — ``dinner_time``/``lunch_time``, both Eastern Time.

    Never raises — partial failures are collected into the returned summary
    rather than aborting the whole cycle.
    """
    if not meals:
        return "Error: no meals provided."

    from integrations import google_calendar, todoist

    errors: list[str] = []
    created_events = 0

    for meal in meals:
        date_str = meal["date"]
        meal_type = (meal.get("meal_type") or "dinner").lower()
        time_str = lunch_time if meal_type == "lunch" else dinner_time
        hour, minute = (int(p) for p in time_str.split(":"))
        start_dt = dt.datetime.combine(
            dt.date.fromisoformat(date_str), dt.time(hour, minute, tzinfo=_EASTERN)
        )
        end_dt = start_dt + dt.timedelta(hours=1)
        label = "Lunch" if meal_type == "lunch" else "Dinner"
        result = google_calendar.create_event(
            account_name=account_name,
            calendar_name=calendar_name,
            summary=f"{label}: {meal['dish']}",
            start_iso=start_dt.isoformat(),
            end_iso=end_dt.isoformat(),
            description=meal.get("notes"),
        )
        if result.startswith("Error"):
            errors.append(f"{date_str} ({meal['dish']}): {result}")
        else:
            created_events += 1

    created_tasks = 0
    for item in shopping_list:
        result = todoist.create_task(content=item, category=GROCERIES_CATEGORY)
        if result.startswith("Error"):
            errors.append(f"shopping item '{item}': {result}")
        else:
            created_tasks += 1

    end_date = (
        dt.date.fromisoformat(start_date) + dt.timedelta(days=len(meals) - 1)
    ).isoformat()
    data = _load()
    data.setdefault("cycles", []).append({
        "start_date": start_date,
        "end_date": end_date,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "account_name": account_name,
        "calendar_name": calendar_name,
        "meals": meals,
        "shopping_list": shopping_list,
    })
    _save(data)
    log.info(
        "meal plan cycle %s..%s saved (%d/%d events, %d/%d groceries)",
        start_date, end_date, created_events, len(meals), created_tasks, len(shopping_list),
    )

    summary = (
        f"Added {created_events}/{len(meals)} meals to the '{calendar_name}' calendar "
        f"and {created_tasks}/{len(shopping_list)} items to Groceries."
    )
    if errors:
        summary += "\n\nSome items failed:\n" + "\n".join(f"- {e}" for e in errors)
    return summary
