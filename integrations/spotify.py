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

import os
import re
import subprocess
import sys
import time

import requests

from app.config import CONFIG
from app.logging_setup import get_logger
from integrations import _http

log = get_logger("spotify")

_API = "https://api.spotify.com/v1"

_KIND_TYPES = {"track": "tracks", "album": "albums", "artist": "artists", "playlist": "playlists"}


def _request(method: str, path: str, **kwargs) -> requests.Response:
    """Spotify Web API call with bearer auth, timeout, and transient retries.

    Uses the shared write-safe policy and honors Spotify's ``Retry-After`` hint
    on a 429. Unlike Todoist, a bare ``ConnectionError`` is only retried on reads
    (a non-idempotent skip/next must not fire twice).
    """
    from integrations import spotify_oauth

    url = path if path.startswith("http") else f"{_API}{path}"
    headers = kwargs.pop("headers", None) or {}
    headers["Authorization"] = f"Bearer {spotify_oauth.get_spotify_token()}"
    kwargs["headers"] = headers
    return _http.request_with_retries(
        method, url,
        label="Spotify",
        honor_retry_after=True,
        logger=log,
        **kwargs,
    )


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
    """The active device id, or the first available one, or None.

    A device only appears here if Spotify is *running* somewhere — but it need
    not be actively playing; an idle open app still counts and can be played to.
    """
    devices = _request("GET", "/me/player/devices").json().get("devices", [])
    if not devices:
        return None
    for device in devices:
        if device.get("is_active"):
            return device.get("id")
    return devices[0].get("id")


def _launch_spotify(uri: str | None = None) -> None:
    """Open the local Spotify app (optionally to a content URI) via the OS.

    The Web API can't cold-start Spotify, but JARVIS runs on the user's machine,
    so it can launch the desktop app — which then registers as a device we can
    play to. Best-effort; never raises.
    """
    target = uri or "spotify:"
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        log.info("launched Spotify app (%s)", target)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not launch Spotify app: %s", exc)


def _wake_device(launch_uri: str | None = None, timeout: float = 10.0) -> str | None:
    """Launch the Spotify app and wait for a playable device to appear."""
    _launch_spotify(launch_uri)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            device_id = _active_device_id()
        except Exception:  # noqa: BLE001
            device_id = None
        if device_id:
            return device_id
    return None


# Matches a Spotify link or URI, e.g. "spotify:track:ID" or
# "https://open.spotify.com/track/ID?si=...". Lets the user paste a link.
_REF_RE = re.compile(
    r"(?:open\.spotify\.com/|spotify:)(track|album|artist|playlist)[/:]([A-Za-z0-9]+)"
)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def _parse_ref(query: str) -> tuple[str, str] | None:
    """Return (kind, id) if *query* is/contains a Spotify link or URI, else None."""
    m = _REF_RE.search(query or "")
    return (m.group(1), m.group(2)) if m else None


def _build_play(kind: str, item: dict) -> tuple[str, dict, str]:
    """Return (uri, play-body, human label) for a track/album/artist/playlist."""
    uri = item["uri"]
    name = item.get("name", "(unknown)")
    artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a)
    if kind == "track":
        return uri, {"uris": [uri]}, name + (f" by {artists}" if artists else "")
    body = {"context_uri": uri}
    if kind == "album":
        label = f"the album {name}" + (f" by {artists}" if artists else "")
    elif kind == "artist":
        label = f"{name} (artist mix)"
    else:
        label = f"the playlist {name}"
    return uri, body, label


def _search_item(query: str, kind: str) -> dict | None:
    """Find the best match. For tracks, prefer an exact title match over the
    top (popularity-ranked) hit, so 'Back in Black' doesn't return the artist's
    most-streamed song instead."""
    limit = 5 if kind == "track" else 1
    results = _request(
        "GET", "/search", params={"q": query, "type": kind, "limit": limit}
    ).json()
    items = (results.get(_KIND_TYPES[kind]) or {}).get("items") or []
    if not items:
        return None
    if kind == "track":
        wanted = _norm(query)
        for item in items:
            if _norm(item.get("name", "")) == wanted:
                return item
    return items[0]


def play_music(query: str, kind: str = "track") -> str:
    """Play a Spotify track/album/artist/playlist by name, link, or URI. Never raises."""
    if not CONFIG.spotify_available:
        return _not_configured()
    kind = (kind or "track").lower()
    if kind not in _KIND_TYPES:
        kind = "track"

    try:
        # A pasted Spotify link/URI plays exactly that item — no search guessing.
        ref = _parse_ref(query)
        if ref:
            kind, ref_id = ref
            item = _request("GET", f"/{kind}s/{ref_id}").json()
        else:
            item = _search_item(query, kind)
            if not item:
                return f"Couldn't find a {kind} matching '{query}' on Spotify."
        uri, body, label = _build_play(kind, item)

        # If Spotify isn't open anywhere, launch the local app and wait for it to
        # register as a device, rather than giving up.
        device_id = _active_device_id()
        if not device_id:
            device_id = _wake_device(uri)
        if not device_id:
            return (
                f"Spotify wasn't running, so I opened it — {label} should start "
                "in a moment. If it doesn't, press play once and ask me again."
            )

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
