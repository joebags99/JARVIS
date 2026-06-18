"""Spotify integration — play/control music via the Web API.

Search for and play tracks/albums/artists/playlists, and control transport
(pause, resume, skip, shuffle, volume) plus report what's playing. Requires
Spotify Premium and an open Spotify device (the Web API can only control
playback under those conditions).

Auth is handled by integrations/spotify_oauth.py (OAuth + PKCE, token cached on
disk). Every public function is best-effort and never raises — it returns a
readable string for Claude, the same convention as the weather/todoist tools.
Transient API failures are retried with the same write-safe backoff as Todoist.
"""

from __future__ import annotations

import time

import requests

from app.config import CONFIG
from app.logging_setup import get_logger

log = get_logger("spotify")

_API = "https://api.spotify.com/v1"

# Same write-safe retry policy as integrations/todoist.py: 502/503/504/429 mean
# the request wasn't applied (safe to retry, even for a non-idempotent skip); an
# ambiguous 500 or a timeout is only retried on reads.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # seconds → 0.5s, 1.0s between attempts

_KIND_TYPES = {"track": "tracks", "album": "albums", "artist": "artists", "playlist": "playlists"}


def _request(method: str, path: str, **kwargs) -> requests.Response:
    """Spotify Web API call with bearer auth, timeout, and transient retries."""
    from integrations import spotify_oauth

    url = path if path.startswith("http") else f"{_API}{path}"
    kwargs.setdefault("timeout", 15)
    headers = kwargs.pop("headers", None) or {}
    headers["Authorization"] = f"Bearer {spotify_oauth.get_spotify_token()}"
    kwargs["headers"] = headers
    is_get = method.upper() == "GET"

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        delay = _BACKOFF_BASE * (2 ** (attempt - 1))
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            retryable = status in _RETRY_STATUSES and (status != 500 or is_get)
            if not retryable or attempt == _MAX_ATTEMPTS:
                raise
            last_exc = exc
            if status == 429:  # honor Spotify's rate-limit backoff hint
                retry_after = (exc.response.headers or {}).get("Retry-After", "")
                if str(retry_after).isdigit():
                    delay = max(delay, int(retry_after))
        except (requests.Timeout, requests.ConnectionError) as exc:
            if not is_get or attempt == _MAX_ATTEMPTS:
                raise
            last_exc = exc

        log.warning(
            "Spotify %s %s failed (attempt %d/%d: %s); retrying in %.1fs",
            method, path, attempt, _MAX_ATTEMPTS, last_exc, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover — loop always returns or raises above


def _not_configured() -> str:
    return (
        "Spotify isn't set up. Set SPOTIFY_ENABLED=true and SPOTIFY_CLIENT_ID in "
        ".env, then ask again to authorize."
    )


def _no_device() -> str:
    return (
        "No active Spotify device found. Open Spotify on your computer or phone "
        "(start playing something for a second), then try again."
    )


def _active_device_id() -> str | None:
    """The active device id, or the first available one, or None."""
    devices = _request("GET", "/me/player/devices").json().get("devices", [])
    if not devices:
        return None
    for device in devices:
        if device.get("is_active"):
            return device.get("id")
    return devices[0].get("id")


def play_music(query: str, kind: str = "track") -> str:
    """Search Spotify for *query* and play the top match. Never raises."""
    if not CONFIG.spotify_available:
        return _not_configured()
    kind = (kind or "track").lower()
    if kind not in _KIND_TYPES:
        kind = "track"

    try:
        device_id = _active_device_id()
        if not device_id:
            return _no_device()

        results = _request(
            "GET", "/search", params={"q": query, "type": kind, "limit": 1}
        ).json()
        items = (results.get(_KIND_TYPES[kind]) or {}).get("items") or []
        if not items:
            return f"Couldn't find a {kind} matching '{query}' on Spotify."
        item = items[0]
        name = item.get("name", "(unknown)")
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a)

        if kind == "track":
            body = {"uris": [item["uri"]]}
            label = name + (f" by {artists}" if artists else "")
        else:
            body = {"context_uri": item["uri"]}
            if kind == "album":
                label = f"the album {name}" + (f" by {artists}" if artists else "")
            elif kind == "artist":
                label = f"{name} (artist mix)"
            else:
                label = f"the playlist {name}"

        _request("PUT", "/me/player/play", params={"device_id": device_id}, json=body)
        log.info("spotify playing %s: %s", kind, label)
        return f"Now playing {label}."
    except Exception as exc:  # noqa: BLE001
        log.error("play_music(%r, %s) failed: %s", query, kind, exc)
        return f"Error playing music: {exc}"


def control_playback(action: str, volume_percent: int | None = None) -> str:
    """Pause/resume/skip/shuffle/volume on the active device. Never raises."""
    if not CONFIG.spotify_available:
        return _not_configured()
    action = (action or "").lower()

    try:
        device_id = _active_device_id()
        if not device_id:
            return _no_device()
        params = {"device_id": device_id}

        if action in ("pause", "stop"):
            _request("PUT", "/me/player/pause", params=params)
            return "Paused."
        if action in ("resume", "play", "unpause"):
            _request("PUT", "/me/player/play", params=params)
            return "Resumed playback."
        if action in ("next", "skip"):
            _request("POST", "/me/player/next", params=params)
            return "Skipped to the next track."
        if action in ("previous", "prev", "back"):
            _request("POST", "/me/player/previous", params=params)
            return "Went back to the previous track."
        if action in ("shuffle_on", "shuffle"):
            _request("PUT", "/me/player/shuffle", params={**params, "state": "true"})
            return "Shuffle on."
        if action == "shuffle_off":
            _request("PUT", "/me/player/shuffle", params={**params, "state": "false"})
            return "Shuffle off."
        if action == "volume":
            if volume_percent is None:
                return "Tell me a volume level from 0 to 100."
            vol = max(0, min(100, int(volume_percent)))
            _request("PUT", "/me/player/volume", params={**params, "volume_percent": vol})
            return f"Volume set to {vol}%."
        return f"Unknown playback action '{action}'."
    except Exception as exc:  # noqa: BLE001
        log.error("control_playback(%s) failed: %s", action, exc)
        return f"Error controlling playback: {exc}"


def now_playing() -> str:
    """Report the currently playing track, or that nothing's playing. Never raises."""
    if not CONFIG.spotify_available:
        return _not_configured()
    try:
        resp = _request("GET", "/me/player/currently-playing")
        if resp.status_code == 204 or not resp.content:
            return "Nothing is playing on Spotify right now."
        data = resp.json()
        item = data.get("item")
        if not item:
            return "Nothing is playing on Spotify right now."
        name = item.get("name", "(unknown)")
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a)
        state = "Playing" if data.get("is_playing") else "Paused"
        return f"{state}: {name}" + (f" by {artists}" if artists else "") + "."
    except Exception as exc:  # noqa: BLE001
        log.error("now_playing failed: %s", exc)
        return f"Error checking playback: {exc}"


def play_track_query(query: str) -> str:
    """Play the top track match for *query* — used by the easter egg."""
    return play_music(query, kind="track")
