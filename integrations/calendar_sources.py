"""Shared best-effort multi-source calendar fetch.

Both main.py's proactive-scheduler meeting-alert fetch and the ambient HUD
(app/hud.py) need "every upcoming event across whichever calendar sources are
configured, tolerating any one source's failure" — this is that shared
helper, pulled out once a third call site needed the exact same iterate/
try-except/concat logic already duplicated between main.py and
app/tool_registry.py's calendar tool handler. tool_registry.py's copy is left
as-is (a slightly different call shape driven by tool input, not worth
touching for this).
"""

from __future__ import annotations

import datetime as dt

from .google_calendar import CalEvent
from app.logging_setup import get_logger

log = get_logger("calendar_sources")


def get_all_events(days: int, max_events: int = 20) -> list[CalEvent]:
    """Best-effort events from google_calendar + outlook_calendar + outlook_ics.

    Each source is independently try/excepted (missing credentials, a
    network error, an expired token in one source never blocks the others).
    Returns the plain concatenation, NOT re-sorted/re-capped across sources —
    callers that need one combined, capped view (like the HUD's "next
    event") sort/filter afterward themselves via next_event().

    Called positionally: outlook_ics.get_events names its second parameter
    max_results rather than max_events, so keyword args would break it.
    """
    from . import google_calendar, outlook_calendar, outlook_ics
    events: list[CalEvent] = []
    for src in (google_calendar, outlook_calendar, outlook_ics):
        try:
            events += src.get_events(days, max_events)
        except Exception as exc:  # noqa: BLE001
            log.debug("event fetch failed (%s): %s", src.__name__, exc)
    return events


def _sort_key(event: CalEvent) -> dt.datetime:
    """Normalize naive datetimes to UTC so mixed aware/naive events still
    compare — same trick google_calendar.get_events already uses internally."""
    if event.start.tzinfo is None:
        return event.start.replace(tzinfo=dt.timezone.utc)
    return event.start


def _is_past(event: CalEvent, now: dt.datetime) -> bool:
    """Whether *event* is effectively over as of *now*.

    A timed event with a known end stays "current" until it actually ends,
    not just until it starts (an ongoing meeting shouldn't vanish from a
    countdown the moment it begins). An all-day event with no end stays
    current through the rest of its calendar day, not just until its
    midnight `.start` — there's no meaningful "starts in N minutes"
    countdown for one, so treating it as past the instant the day begins
    would make it disappear before it's actually done. A timed event with no
    end is past once its start has passed.
    """
    start = event.start if event.start.tzinfo else event.start.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    if event.end is not None:
        end = event.end if event.end.tzinfo else event.end.replace(tzinfo=dt.timezone.utc)
        return end <= now
    if event.all_day:
        return start.date() < now.date()
    return start <= now


def next_event(events: list[CalEvent], now: dt.datetime) -> CalEvent | None:
    """Soonest future (or currently ongoing) event from *events*, or None."""
    upcoming = [e for e in events if not _is_past(e, now)]
    if not upcoming:
        return None
    return min(upcoming, key=_sort_key)
