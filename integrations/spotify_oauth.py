"""OAuth 2.0 Authorization Code + PKCE for the Spotify Web API.

First use opens a browser for the user to authorize JARVIS. Tokens cache to
tokens/spotify_oauth.json (gitignored) and refresh automatically when they
expire. Public client (PKCE), so only ``SPOTIFY_CLIENT_ID`` is needed — no
client secret is stored.

Spotify requires the redirect URI to be registered *exactly* in the app's
dashboard, and it rejects ``localhost`` — it must be the loopback IP. Register:

    http://127.0.0.1:9433/callback

Mirrors integrations/monarch_oauth.py (PKCE pair, local callback server, token
cache + silent refresh), but Spotify's endpoints are fixed so there's no
discovery or dynamic registration step.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from app.config import CONFIG, ROOT_DIR
from app.logging_setup import get_logger

log = get_logger("spotify_oauth")

_AUTH_ENDPOINT = "https://accounts.spotify.com/authorize"
_TOKEN_ENDPOINT = "https://accounts.spotify.com/api/token"
# Search needs no scope; the rest cover playback read/control + finding the
# user's own (private) playlists by name.
_SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing playlist-read-private"
)
_TOKEN_CACHE = ROOT_DIR / "tokens" / "spotify_oauth.json"
_CALLBACK_PORT = 9433
_REDIRECT_URI = f"http://127.0.0.1:{_CALLBACK_PORT}/callback"
_EXPIRY_BUFFER = 60  # refresh a minute before actual expiry


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Local callback server ─────────────────────────────────────────────────────

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    done: threading.Event

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            body = (
                b"<html><body style='font-family:sans-serif;padding:40px'>"
                b"<h2>JARVIS authorized!</h2>"
                b"<p>Spotify is connected. You can close this tab.</p>"
                b"</body></html>"
            )
        else:
            _CallbackHandler.error = params.get("error", ["unknown"])[0]
            body = (
                b"<html><body style='font-family:sans-serif;padding:40px'>"
                b"<h2>Authorization failed.</h2>"
                b"<p>Check the JARVIS console for details.</p>"
                b"</body></html>"
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
        _CallbackHandler.done.set()

    def log_message(self, *_):
        pass  # silence access log


def _await_callback(timeout: int = 120) -> str:
    """Start the local server, wait for the OAuth redirect, return the code."""
    _CallbackHandler.code = None
    _CallbackHandler.error = None
    event = threading.Event()
    _CallbackHandler.done = event

    server = http.server.HTTPServer(("127.0.0.1", _CALLBACK_PORT), _CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        event.wait(timeout=timeout)
    finally:
        server.shutdown()

    if _CallbackHandler.error:
        raise RuntimeError(f"Spotify OAuth error: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise RuntimeError(
            f"Spotify authorization timed out — no browser response within "
            f"{timeout}s. Try again."
        )
    return _CallbackHandler.code


# ── Token cache ───────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _valid(cache: dict) -> bool:
    return bool(
        cache.get("access_token")
        and float(cache.get("expires_at", 0)) > time.time() + _EXPIRY_BUFFER
    )


def _store(tokens: dict, prev: dict | None = None) -> str:
    """Persist a token response. Spotify omits refresh_token on refresh, so keep
    the previous one when the response doesn't include a new value."""
    prev = prev or {}
    access = tokens["access_token"]
    _save({
        "access_token": access,
        "refresh_token": tokens.get("refresh_token") or prev.get("refresh_token"),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
    })
    return access


# ── Public API ────────────────────────────────────────────────────────────────

def get_spotify_token() -> str:
    """Return a valid Spotify Bearer token, running the browser flow if needed.

    Blocks the calling thread until the user authorizes (first run only).
    Subsequent calls return the cached token or silently refresh it.
    """
    client_id = CONFIG.spotify_client_id
    if not client_id:
        raise RuntimeError("SPOTIFY_CLIENT_ID is not set in .env.")

    cache = _load()
    if _valid(cache):
        return cache["access_token"]

    # Silent refresh before a full re-auth.
    if cache.get("refresh_token"):
        try:
            resp = requests.post(_TOKEN_ENDPOINT, data={
                "grant_type": "refresh_token",
                "refresh_token": cache["refresh_token"],
                "client_id": client_id,
            }, timeout=15)
            resp.raise_for_status()
            log.info("spotify token refreshed")
            return _store(resp.json(), cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("spotify token refresh failed, re-authorizing: %s", exc)

    # Full authorization code + PKCE flow.
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    auth_url = _AUTH_ENDPOINT + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": _SCOPES,
    })

    print(
        "\n[JARVIS] Opening browser to authorize Spotify...\n"
        f"If it doesn't open automatically, visit:\n{auth_url}\n"
    )
    log.info("starting Spotify OAuth browser flow")
    webbrowser.open(auth_url)

    code = _await_callback()
    resp = requests.post(_TOKEN_ENDPOINT, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }, timeout=15)
    resp.raise_for_status()
    log.info("spotify authorization complete — token cached")
    return _store(resp.json())


def clear_token() -> None:
    """Delete the cached token, forcing re-authorization on next use."""
    try:
        _TOKEN_CACHE.unlink()
        log.info("spotify token cache cleared")
    except FileNotFoundError:
        pass
