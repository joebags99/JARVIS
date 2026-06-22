"""Tests for the proactive scheduler (app/proactive.py)."""

from __future__ import annotations

import datetime as dt

from app.config import CONFIG
from app.proactive import (
    ProactiveScheduler,
    briefing_due,
    in_quiet_hours,
    meeting_alerts_due,
    parse_hhmm,
    parse_quiet_window,
    short_sender,
)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_parse_hhmm():
    assert parse_hhmm("07:30") == dt.time(7, 30)
    assert parse_hhmm("") is None
    assert parse_hhmm("nope") is None
    assert parse_hhmm("25:00") is None  # out of range


def test_parse_quiet_window():
    assert parse_quiet_window("22:00-07:00") == (dt.time(22), dt.time(7))
    assert parse_quiet_window("") is None
    assert parse_quiet_window("22:00") is None


def test_in_quiet_hours_wraps_midnight():
    w = (dt.time(22), dt.time(7))
    assert in_quiet_hours(dt.time(23), w)
    assert in_quiet_hours(dt.time(2), w)
    assert not in_quiet_hours(dt.time(12), w)


def test_in_quiet_hours_same_day():
    w = (dt.time(9), dt.time(17))
    assert in_quiet_hours(dt.time(12), w)
    assert not in_quiet_hours(dt.time(8), w)
    assert not in_quiet_hours(dt.time(17), w)  # end is exclusive


def test_in_quiet_hours_none_window():
    assert not in_quiet_hours(dt.time(3), None)


def test_briefing_due():
    target = dt.time(7, 30)
    now = dt.datetime(2026, 6, 22, 7, 35)
    assert briefing_due(now, target, None)
    assert not briefing_due(now, target, now.date())          # already fired today
    assert not briefing_due(dt.datetime(2026, 6, 22, 7, 0), target, None)   # before target
    assert not briefing_due(dt.datetime(2026, 6, 22, 9, 0), target, None)   # past the window
    assert not briefing_due(now, None, None)                  # no target set


class _Event:
    def __init__(self, summary, start, all_day=False):
        self.summary = summary
        self.start = start
        self.all_day = all_day


def test_meeting_alerts_due_filters_and_dedups():
    now = dt.datetime(2026, 6, 22, 9, 0)
    events = [
        _Event("Standup", dt.datetime(2026, 6, 22, 9, 10)),   # in 10 min → due
        _Event("Lunch", dt.datetime(2026, 6, 22, 12, 0)),     # too far → not due
        _Event("Past", dt.datetime(2026, 6, 22, 8, 55)),      # already started → not due
        _Event("Allday", dt.datetime(2026, 6, 22, 9, 5), all_day=True),  # all-day → skip
    ]
    due = meeting_alerts_due(events, now, 15, set())
    assert [a.summary for a in due] == ["Standup"]
    assert due[0].minutes == 10
    # once it's in the already-alerted set, it won't fire again
    assert meeting_alerts_due(events, now, 15, {due[0].key}) == []


def test_meeting_alerts_handles_tz_aware_start():
    now = dt.datetime(2026, 6, 22, 9, 0)
    aware_start = (now + dt.timedelta(minutes=5)).astimezone()  # local-aware
    due = meeting_alerts_due([_Event("Mtg", aware_start)], now, 15, set())
    assert len(due) == 1 and due[0].minutes == 5


def test_short_sender():
    assert short_sender("Sam Smith <sam@x.com>") == "Sam Smith"
    assert short_sender("sam@x.com") == "sam@x.com"
    assert short_sender('"Sam" <sam@x.com>') == "Sam"
    assert short_sender("") == "(unknown sender)"


# ── Scheduler tick behavior ───────────────────────────────────────────────────

def _sched(**fakes):
    return ProactiveScheduler(
        notify=fakes.get("notify", lambda *a: None),
        briefing=fakes.get("briefing", lambda: None),
        fetch_events=fakes.get("fetch_events", lambda: []),
        fetch_email=fakes.get("fetch_email", lambda: []),
    )


def _enable(monkeypatch, **over):
    monkeypatch.setattr(CONFIG, "proactive_enabled", True)
    monkeypatch.setattr(CONFIG, "meeting_alerts", over.get("meeting_alerts", False))
    monkeypatch.setattr(CONFIG, "email_alerts", over.get("email_alerts", False))
    monkeypatch.setattr(CONFIG, "meeting_lead_min", 15)
    monkeypatch.setattr(CONFIG, "briefing_time", over.get("briefing_time", ""))
    monkeypatch.setattr(CONFIG, "quiet_hours", over.get("quiet_hours", ""))


def test_tick_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(CONFIG, "proactive_enabled", False)
    calls = []
    _sched(notify=lambda *a: calls.append(a)).tick(dt.datetime(2026, 6, 22, 9, 0))
    assert calls == []


def test_meeting_alert_fires_once(monkeypatch):
    _enable(monkeypatch, meeting_alerts=True)
    now = dt.datetime(2026, 6, 22, 9, 0)
    event = _Event("Standup", dt.datetime(2026, 6, 22, 9, 10))
    notes = []
    s = _sched(notify=lambda t, m: notes.append(m), fetch_events=lambda: [event])
    s.tick(now)
    s.tick(now)  # second tick must not re-alert the same meeting
    assert len(notes) == 1 and "Standup" in notes[0]


def test_quiet_hours_suppress_alerts_but_not_briefing(monkeypatch):
    _enable(monkeypatch, meeting_alerts=True, briefing_time="23:00", quiet_hours="22:00-07:00")
    now = dt.datetime(2026, 6, 22, 23, 0)  # inside quiet hours AND at briefing time
    event = _Event("Late mtg", dt.datetime(2026, 6, 22, 23, 10))
    briefed, notes = [], []
    s = _sched(
        notify=lambda t, m: notes.append(m),
        briefing=lambda: briefed.append(1),
        fetch_events=lambda: [event],
    )
    s.tick(now)
    assert briefed == [1]  # scheduled briefing still fires
    assert notes == []     # meeting alert suppressed during quiet hours


def test_email_first_poll_seeds_then_pings_new(monkeypatch):
    _enable(monkeypatch, email_alerts=True)
    monkeypatch.setattr(type(CONFIG), "gmail_available", property(lambda self: True))
    now = dt.datetime(2026, 6, 22, 9, 0)
    inbox = [{"id": "a", "sender": "Boss <b@x>", "subject": "Hi"}]
    notes = []
    s = _sched(notify=lambda t, m: notes.append(m), fetch_email=lambda: list(inbox))
    s.tick(now)                 # first poll seeds the seen-set, no ping
    assert notes == []
    inbox.append({"id": "b", "sender": "Sam <s@x>", "subject": "New deal"})
    s.tick(now)                 # only the newly-arrived message pings
    assert len(notes) == 1 and "Sam" in notes[0]
