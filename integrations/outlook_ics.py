"""Outlook calendar via a published ICS feed — no Azure access required.

``outlook_calendar.py`` needs an Azure App Registration (MSAL device-code
OAuth against Microsoft Graph). When a work tenant won't grant that, the
fallback is Outlook-on-the-web's "Publish a Calendar" feature — a personal
account setting that yields a public ``.ics`` URL. Many orgs restrict that
publish mode to free/busy only: no event titles, locations, or descriptions
come through, just "busy from X to Y". This module fetches and parses that
feed, normalizing every occurrence to a generic "Busy" block.

Set ``OUTLOOK_ICS_URL`` in ``.env``. Treat that URL as a secret — anyone who
has it can see your busy/free times.
"""

from __future__ import annotations

import datetime as dt

import icalendar
import recurring_ical_events
import requests

from app.config import CONFIG
from app.logging_setup import get_logger
from integrations.google_calendar import CalEvent

log = get_logger("outlook_ics")

SOURCE = "Outlook-ICS"


def _to_event(occurrence: dict) -> CalEvent:
    start = occurrence["DTSTART"].dt
    end_field = occurrence.get("DTEND")
    end = end_field.dt if end_field else None
    all_day = not isinstance(start, dt.datetime)

    if all_day:
        start = dt.datetime.combine(start, dt.time.min)
        if end is not None and not isinstance(end, dt.datetime):
            end = dt.datetime.combine(end, dt.time.min)

    summary = str(occurrence.get("SUMMARY") or "").strip() or "Busy"
    return CalEvent(
        summary=summary,
        start=start,
        end=end,
        location=None,
        all_day=all_day,
        source=SOURCE,
    )


def get_events(days: int = 7, max_results: int = 20) -> list[CalEvent]:
    """Fetch and expand the published free/busy ICS feed. Never raises."""
    if not CONFIG.outlook_ics_enabled:
        return []

    try:
        resp = requests.get(CONFIG.outlook_ics_url, timeout=15)
        resp.raise_for_status()
        cal = icalendar.Calendar.from_ical(resp.text)

        now = dt.datetime.now()
        end = now + dt.timedelta(days=days)
        occurrences = recurring_ical_events.of(cal).between(now, end)

        events = [_to_event(occ) for occ in occurrences]
        events.sort(key=lambda e: e.start.replace(tzinfo=None))
        return events[:max_results]
    except Exception as exc:  # noqa: BLE001
        log.warning("outlook ics fetch/parse failed: %s", exc)
        return []
