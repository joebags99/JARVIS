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


# US state abbreviations → full names, so a hint like "NC" matches Open-Meteo's
# admin1 field ("North Carolina"). Covers the common "City, ST" form users type.
_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


def _hint_matches(result: dict, hint: str) -> bool:
    """True if a region hint (e.g. 'NC', 'Texas', 'France') matches a geocode hit."""
    hint = hint.strip().lower()
    if not hint:
        return True
    expanded = _US_STATES.get(hint, hint)
    fields = [
        str(result.get(k, "")).lower()
        for k in ("admin1", "admin2", "country", "country_code")
    ]
    for f in fields:
        if f and (f == hint or f == expanded or expanded in f or hint in f):
            return True
    return False


def _geocode(place: str) -> tuple[float, float, str] | str:
    """Resolve a place name to ``(lat, lon, display_label)``, or an error string.

    Open-Meteo geocodes on the bare place name, so "Charlotte, NC" finds
    nothing — split the city from any state/country hints and use the hints
    to disambiguate among the candidates instead.
    """
    parts = [p.strip() for p in place.split(",") if p.strip()]
    name = parts[0] if parts else place
    hints = parts[1:]

    try:
        geo = requests.get(
            _GEOCODE_URL,
            params={"name": name, "count": 10, "language": "en", "format": "json"},
            timeout=15,
        )
        geo.raise_for_status()
        results = geo.json().get("results") or []
        if not results:
            return f"Couldn't find a location matching '{place}'."
        # Prefer the candidate matching the most hints; results are already
        # population-sorted, so ties (and the no-hint case) keep the top hit.
        top = max(results, key=lambda r: sum(_hint_matches(r, h) for h in hints))
        lat, lon = top["latitude"], top["longitude"]
        label = ", ".join(
            part for part in (top.get("name"), top.get("admin1"), top.get("country_code"))
            if part
        )
        return lat, lon, label
    except Exception as exc:  # noqa: BLE001
        log.error("geocode failed for %r: %s", place, exc)
        return f"Error looking up '{place}': {exc}"


def get_current_compact(location: str | None = None) -> str:
    """Terse ``"72°F, clear sky"`` — no location label, forecast, wind, or
    humidity. For space-constrained displays (the ambient HUD's meeting/
    weather card) where get_weather()'s full one-liner ("Fort Wayne,
    Indiana, US: clear sky, 72°F (feels 70°), high 75° / low 60°, ...")
    is too long to fit before it gets truncated. Never raises.
    """
    place = (location or CONFIG.location or "").strip()
    if not place:
        return "No location set"
    geocoded = _geocode(place)
    if isinstance(geocoded, str):
        return "Weather unavailable"
    lat, lon, _label = geocoded
    try:
        fc = requests.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
            },
            timeout=15,
        )
        fc.raise_for_status()
        cur = fc.json().get("current", {})
    except Exception as exc:  # noqa: BLE001
        log.debug("compact weather fetch failed for %r: %s", place, exc)
        return "Weather unavailable"
    desc = _describe(cur.get("weather_code"))
    temp = cur.get("temperature_2m")
    return f"{round(temp)}°F, {desc}" if temp is not None else desc


def get_weather(location: str | None = None, days: int = 1) -> str:
    """Return current conditions + a 1-to-16 day forecast for *location*. Never raises."""
    place = (location or CONFIG.location or "").strip()
    if not place:
        return (
            "No location given and no default set. Ask the user which city, or "
            "set JARVIS_LOCATION in .env."
        )
    days = max(1, min(int(days or 1), 16))

    geocoded = _geocode(place)
    if isinstance(geocoded, str):
        return geocoded
    lat, lon, label = geocoded

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
                "forecast_days": days,
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

    def _day(key, i):
        vals = daily.get(key) or []
        return vals[i] if i < len(vals) else None

    now_desc = _describe(cur.get("weather_code"))
    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    humidity = cur.get("relative_humidity_2m")
    wind = cur.get("wind_speed_10m")

    def _temp_clause() -> str | None:
        if temp is None:
            return None
        feels_note = f" (feels {round(feels)}°)" if feels is not None else ""
        return f"{round(temp)}°F{feels_note}"

    def _wind_humidity() -> list[str]:
        out = []
        if wind is not None:
            out.append(f"wind {round(wind)} mph")
        if humidity is not None:
            out.append(f"humidity {humidity}%")
        return out

    def _day_forecast(i: int) -> str:
        """high/low + precip for forecast day i (no description prefix)."""
        seg = []
        hi, lo = _day("temperature_2m_max", i), _day("temperature_2m_min", i)
        precip = _day("precipitation_probability_max", i)
        if hi is not None and lo is not None:
            seg.append(f"high {round(hi)}° / low {round(lo)}°")
        if precip is not None:
            seg.append(f"{precip}% precip")
        return ", ".join(seg)

    times = daily.get("time") or []
    if days <= 1 or len(times) <= 1:
        # Compact single line (current conditions + today's high/low/precip).
        parts = [f"{label}: {now_desc}"]
        if (t := _temp_clause()):
            parts.append(t)
        if (today := _day_forecast(0)):
            parts.append(today)
        parts.extend(_wind_humidity())
        line = ", ".join(parts) + "."
    else:
        import datetime as dt

        def _label_day(date_str: str, i: int) -> str:
            if i == 0:
                return "Today"
            try:
                return dt.date.fromisoformat(date_str).strftime("%a %b %d")
            except (ValueError, TypeError):
                return date_str

        now_line = ", ".join(
            [f"{label} — now: {now_desc}"]
            + ([t] if (t := _temp_clause()) else [])
            + _wind_humidity()
        )
        lines = [now_line + ".", "Forecast:"]
        for i, date_str in enumerate(times):
            day_desc = _describe(_day("weather_code", i))
            detail = _day_forecast(i)
            lines.append(f"- {_label_day(date_str, i)}: {day_desc}" + (f", {detail}" if detail else ""))
        line = "\n".join(lines)

    # Log the full result on one line (newlines flattened) — truncating it hid
    # the multi-day highs and made a current-vs-high temp read look like a bug.
    log.info("weather for %s (%d day(s)) -> %s", label, days, line.replace("\n", " | "))
    return line
