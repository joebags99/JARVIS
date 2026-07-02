"""Tests for the compact weather formatter (integrations/weather.py).

get_current_compact() feeds the ambient HUD's weather row — get_weather()'s
existing one-liner ("Fort Wayne, Indiana, US: clear sky, 72°F (feels 70°),
high 75° / low 60°, ...") is too long to fit the HUD's small card before the
temperature itself gets truncated off-screen, so this is a terser formatter
sharing _geocode() but skipping the forecast/wind/humidity/location-label
detail. _geocode() itself isn't retested here (get_weather()'s existing
behavior on a bad location is unchanged — same error strings, just
factored out) — these mock it directly to isolate get_current_compact()'s
own formatting logic.
"""

from __future__ import annotations

from app.config import CONFIG
from integrations import weather


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_get_current_compact_no_location_configured(monkeypatch):
    monkeypatch.setattr(CONFIG, "location", "")
    assert weather.get_current_compact(None) == "No location set"


def test_get_current_compact_geocode_failure_returns_fallback(monkeypatch):
    monkeypatch.setattr(weather, "_geocode", lambda place: "Couldn't find a location matching 'Nowhere'.")
    assert weather.get_current_compact("Nowhere") == "Weather unavailable"


def test_get_current_compact_formats_temp_and_condition(monkeypatch):
    monkeypatch.setattr(weather, "_geocode", lambda place: (41.08, -85.13, "Fort Wayne, Indiana, US"))
    monkeypatch.setattr(
        weather.requests, "get",
        lambda *a, **k: _FakeResponse({"current": {"temperature_2m": 71.6, "weather_code": 0}}),
    )
    assert weather.get_current_compact("Fort Wayne, IN") == "72°F, clear sky"


def test_get_current_compact_missing_temp_falls_back_to_condition_only(monkeypatch):
    monkeypatch.setattr(weather, "_geocode", lambda place: (0.0, 0.0, "X"))
    monkeypatch.setattr(
        weather.requests, "get", lambda *a, **k: _FakeResponse({"current": {"weather_code": 3}}),
    )
    assert weather.get_current_compact("X") == "overcast"


def test_get_current_compact_forecast_fetch_raises_returns_fallback(monkeypatch):
    monkeypatch.setattr(weather, "_geocode", lambda place: (0.0, 0.0, "X"))

    def _raise(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(weather.requests, "get", _raise)
    assert weather.get_current_compact("X") == "Weather unavailable"
