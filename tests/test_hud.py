"""Tests for the ambient HUD's pure logic + hook bookkeeping (app/hud.py).

Hud.__init__ is side-effect-free (no webview import, no window/thread), so a
bare instance can be built directly without pywebview installed — no __new__
bypass needed here, unlike Overlay/WakeWordListener/Speaker whose __init__
does real hardware probing.
"""

from __future__ import annotations

import datetime as dt

from app.hud import Hud, _format_countdown, _weather_due
from integrations.google_calendar import CalEvent


def _event(summary, start, tzinfo=dt.timezone.utc) -> CalEvent:
    return CalEvent(
        summary=summary, start=start.replace(tzinfo=tzinfo) if tzinfo else start,
        end=None, location=None, all_day=False, source="Test",
    )


class _FakeWindow:
    def __init__(self):
        self.hidden = False
        self.shown_calls = 0
        self.hide_calls = 0

    def hide(self):
        self.hidden = True
        self.hide_calls += 1

    def show(self):
        self.hidden = False
        self.shown_calls += 1


def _hud() -> Hud:
    return Hud(on_click=lambda: None)


# ── _format_countdown ─────────────────────────────────────────────────────────

def test_format_countdown_no_event():
    assert _format_countdown(None) == "No upcoming meetings"


def test_format_countdown_minutes_only():
    # +30s padding: _format_countdown computes its own dt.datetime.now()
    # internally, a moment after this one, so a boundary value like exactly
    # +12min would floor to 11m depending on test overhead.
    now = dt.datetime.now(dt.timezone.utc)
    event = _event("Standup", now + dt.timedelta(minutes=12, seconds=30))
    assert _format_countdown(event) == "Standup in 12m"


def test_format_countdown_hours_and_minutes():
    now = dt.datetime.now(dt.timezone.utc)
    event = _event("Planning", now + dt.timedelta(hours=2, minutes=5, seconds=30))
    assert _format_countdown(event) == "Planning in 2h 5m"


def test_format_countdown_event_now_or_past():
    now = dt.datetime.now(dt.timezone.utc)
    event = _event("Standup", now - dt.timedelta(minutes=1))
    assert _format_countdown(event) == "Standup — now"


# ── _weather_due ──────────────────────────────────────────────────────────────

def test_weather_due_when_past_next_fetch():
    assert _weather_due(now_mono=100.0, next_fetch=100.0)
    assert _weather_due(now_mono=101.0, next_fetch=100.0)


def test_weather_not_due_before_next_fetch():
    assert not _weather_due(now_mono=99.0, next_fetch=100.0)


# ── Hud hook bookkeeping ───────────────────────────────────────────────────────

def test_set_unread_count_calls_eval():
    hud = _hud()
    calls = []
    hud._eval = lambda fn, *args: calls.append((fn, args))
    hud.set_unread_count(3)
    assert calls == [("setUnread", (3,))]


def test_on_overlay_visibility_changed_hides_when_overlay_shown():
    hud = _hud()
    hud.window = _FakeWindow()
    hud._visible = True
    hud.on_overlay_visibility_changed(True)
    assert hud.window.hidden is True
    assert hud._visible is False


def test_on_overlay_visibility_changed_shows_when_overlay_hidden_again():
    hud = _hud()
    hud.window = _FakeWindow()
    hud.window.hidden = True
    hud._visible = False
    hud.on_overlay_visibility_changed(False)
    assert hud.window.hidden is False
    assert hud._visible is True


def test_on_overlay_visibility_changed_noop_when_window_none():
    hud = _hud()  # HUD never started (disabled) -> window is None
    hud.on_overlay_visibility_changed(True)  # must not raise
    assert hud.window is None


def test_on_overlay_visibility_changed_idempotent():
    hud = _hud()
    hud.window = _FakeWindow()
    hud._visible = True
    hud.on_overlay_visibility_changed(True)
    hud.on_overlay_visibility_changed(True)  # already hidden -> no second hide() call
    assert hud.window.hide_calls == 1


# ── _tick ────────────────────────────────────────────────────────────────────

def test_tick_calls_eval_with_meeting_and_weather(monkeypatch):
    hud = _hud()
    calls = []
    hud._eval = lambda fn, *args: calls.append((fn, args))

    import integrations.calendar_sources as calendar_sources
    import integrations.weather as weather
    monkeypatch.setattr(calendar_sources, "get_all_events", lambda days, max_events: [])
    monkeypatch.setattr(calendar_sources, "next_event", lambda events, now: None)
    monkeypatch.setattr(weather, "get_weather", lambda: "72°F, sunny")

    hud._tick()

    fn_names = [c[0] for c in calls]
    assert "setMeeting" in fn_names
    assert ("setWeather", ("72°F, sunny",)) in calls


def test_tick_respects_weather_refresh_cadence(monkeypatch):
    hud = _hud()
    hud._eval = lambda fn, *args: None

    import integrations.calendar_sources as calendar_sources
    import integrations.weather as weather
    monkeypatch.setattr(calendar_sources, "get_all_events", lambda days, max_events: [])
    monkeypatch.setattr(calendar_sources, "next_event", lambda events, now: None)
    weather_calls = []
    monkeypatch.setattr(weather, "get_weather", lambda: weather_calls.append(1) or "clear")

    fixed_time = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: fixed_time[0])

    hud._tick()
    hud._tick()  # same monotonic time -> weather must not be re-fetched

    assert len(weather_calls) == 1
