"""Weather integration via Open-Meteo.

No API key required — Open-Meteo's geocoding and forecast endpoints are free
and keyless. We resolve a place name to coordinates, then fetch current
conditions plus today's high/low and precipitation odds, and format a compact
line for Claude. Never raises — returns a readable string on any failure.
"""

from __future__ import annotations

import requests

from app.config import CONFIG
from app.logging_setup import get_logger

log = get_logger("weather")

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes → short descriptions.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def _describe(code: int | None) -> str:
    if code is None:
        return "unknown conditions"
    return _WMO.get(int(code), "unknown conditions")


def get_weather(location: str | None = None) -> str:
    """Return current conditions + today's forecast for *location*. Never raises."""
    place = (location or CONFIG.location or "").strip()
    if not place:
        return (
            "No location given and no default set. Ask the user which city, or "
            "set JARVIS_LOCATION in .env."
        )

    try:
        geo = requests.get(
            _GEOCODE_URL,
            params={"name": place, "count": 1, "language": "en", "format": "json"},
            timeout=15,
        )
        geo.raise_for_status()
        results = geo.json().get("results") or []
        if not results:
            return f"Couldn't find a location matching '{place}'."
        top = results[0]
        lat, lon = top["latitude"], top["longitude"]
        label = ", ".join(
            part for part in (top.get("name"), top.get("admin1"), top.get("country_code"))
            if part
        )
    except Exception as exc:  # noqa: BLE001
        log.error("geocode failed for %r: %s", place, exc)
        return f"Error looking up '{place}': {exc}"

    try:
        fc = requests.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                           "weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=15,
        )
        fc.raise_for_status()
        data = fc.json()
    except Exception as exc:  # noqa: BLE001
        log.error("forecast failed for %r: %s", place, exc)
        return f"Error fetching weather for '{label}': {exc}"

    cur = data.get("current", {})
    daily = data.get("daily", {})

    def _first(key):
        vals = daily.get(key) or []
        return vals[0] if vals else None

    now_desc = _describe(cur.get("weather_code"))
    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    humidity = cur.get("relative_humidity_2m")
    wind = cur.get("wind_speed_10m")
    hi = _first("temperature_2m_max")
    lo = _first("temperature_2m_min")
    precip = _first("precipitation_probability_max")

    parts = [f"{label}: {now_desc}"]
    if temp is not None:
        feels_note = f" (feels {round(feels)}°)" if feels is not None else ""
        parts.append(f"{round(temp)}°F{feels_note}")
    if hi is not None and lo is not None:
        parts.append(f"high {round(hi)}° / low {round(lo)}°")
    if precip is not None:
        parts.append(f"{precip}% precip")
    if wind is not None:
        parts.append(f"wind {round(wind)} mph")
    if humidity is not None:
        parts.append(f"humidity {humidity}%")

    line = ", ".join(parts) + "."
    log.info("weather for %s -> %r", label, line)
    return line
