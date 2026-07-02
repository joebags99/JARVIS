"""Tests for the shared multi-source calendar fetch (integrations/calendar_sources.py)."""

from __future__ import annotations

import datetime as dt

from integrations import google_calendar, outlook_calendar, outlook_ics
from integrations.google_calendar import CalEvent
from integrations.calendar_sources import get_all_events, next_event


def _event(summary, start, end=None, all_day=False, source="Test") -> CalEvent:
    return CalEvent(summary=summary, start=start, end=end, location=None, all_day=all_day, source=source)


# ── get_all_events ────────────────────────────────────────────────────────────

def test_get_all_events_concatenates_all_sources(monkeypatch):
    g = [_event("G", dt.datetime(2026, 7, 1, 9))]
    o = [_event("O", dt.datetime(2026, 7, 1, 10))]
    i = [_event("I", dt.datetime(2026, 7, 1, 11))]
    monkeypatch.setattr(google_calendar, "get_events", lambda days, max_events: g)
    monkeypatch.setattr(outlook_calendar, "get_events", lambda days, max_events: o)
    monkeypatch.setattr(outlook_ics, "get_events", lambda days, max_events: i)

    events = get_all_events(1, 30)
    assert [e.summary for e in events] == ["G", "O", "I"]


def test_get_all_events_tolerates_one_source_raising(monkeypatch):
    monkeypatch.setattr(google_calendar, "get_events", lambda days, max_events: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(outlook_calendar, "get_events", lambda days, max_events: [_event("O", dt.datetime(2026, 7, 1))])
    monkeypatch.setattr(outlook_ics, "get_events", lambda days, max_events: [_event("I", dt.datetime(2026, 7, 1))])

    events = get_all_events(1, 30)
    assert [e.summary for e in events] == ["O", "I"]


def test_get_all_events_tolerates_all_sources_raising(monkeypatch):
    def _raise(days, max_events):
        raise RuntimeError("boom")

    monkeypatch.setattr(google_calendar, "get_events", _raise)
    monkeypatch.setattr(outlook_calendar, "get_events", _raise)
    monkeypatch.setattr(outlook_ics, "get_events", _raise)

    assert get_all_events(1, 30) == []


def test_get_all_events_calls_sources_positionally(monkeypatch):
    # outlook_ics.get_events names its 2nd param max_results, not max_events —
    # calling positionally must still work for all three sources.
    seen = []
    monkeypatch.setattr(google_calendar, "get_events", lambda d, m: seen.append(("g", d, m)) or [])
    monkeypatch.setattr(outlook_calendar, "get_events", lambda d, m: seen.append(("o", d, m)) or [])
    monkeypatch.setattr(outlook_ics, "get_events", lambda d, m: seen.append(("i", d, m)) or [])
    get_all_events(1, 30)
    assert seen == [("g", 1, 30), ("o", 1, 30), ("i", 1, 30)]


# ── next_event ────────────────────────────────────────────────────────────────

NOW = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_next_event_returns_soonest_future_event():
    events = [
        _event("Later", NOW + dt.timedelta(hours=3)),
        _event("Past", NOW - dt.timedelta(hours=1)),
        _event("Soonest", NOW + dt.timedelta(minutes=15)),
    ]
    assert next_event(events, NOW).summary == "Soonest"


def test_next_event_returns_none_when_all_past():
    events = [_event("Past1", NOW - dt.timedelta(hours=2)), _event("Past2", NOW - dt.timedelta(hours=1))]
    assert next_event(events, NOW) is None


def test_next_event_returns_none_for_empty_list():
    assert next_event([], NOW) is None


def test_next_event_treats_ongoing_timed_event_with_future_end_as_current():
    ongoing = _event("Standup", NOW - dt.timedelta(minutes=10), end=NOW + dt.timedelta(minutes=20))
    assert next_event([ongoing], NOW) is ongoing


def test_next_event_timed_event_with_no_end_is_past_once_started():
    started = _event("Started", NOW - dt.timedelta(minutes=1))
    assert next_event([started], NOW) is None


def test_next_event_all_day_event_today_counts_as_current():
    today_all_day = _event(
        "Holiday", NOW.replace(hour=0, minute=0, second=0, microsecond=0), all_day=True,
    )
    assert next_event([today_all_day], NOW) is today_all_day


def test_next_event_all_day_event_yesterday_excluded():
    yesterday_all_day = _event(
        "Old holiday",
        (NOW - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        all_day=True,
    )
    assert next_event([yesterday_all_day], NOW) is None


def test_next_event_sorts_multiple_future_events_by_start():
    e1 = _event("Third", NOW + dt.timedelta(hours=3))
    e2 = _event("First", NOW + dt.timedelta(minutes=5))
    e3 = _event("Second", NOW + dt.timedelta(hours=1))
    assert next_event([e1, e2, e3], NOW).summary == "First"


def test_next_event_handles_naive_datetimes_without_raising():
    naive_now = dt.datetime(2026, 7, 1, 12, 0)  # no tzinfo
    naive_event = _event("Naive", naive_now + dt.timedelta(minutes=10))
    assert next_event([naive_event], naive_now).summary == "Naive"
