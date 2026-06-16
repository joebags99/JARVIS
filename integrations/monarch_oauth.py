"""OAuth 2.0 + PKCE token management for Monarch Money MCP integration.

First use opens a browser for the user to authorize JARVIS. The resulting
tokens are cached to tokens/monarch_oauth.json and refreshed automatically
when they expire. Subsequent calls return the cached token instantly.

Flow:
  1. Discover OAuth endpoints from Monarch's .well-known metadata.
  2. (Optional) Dynamic client registration to get a client_id.
  3. Open browser → authorization code flow with PKCE.
  4. Local HTTP server on port 9432 captures the callback.
  5. Exchange code for access + refresh tokens → cache to disk.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from app.config import ROOT_DIR
from app.logging_setup import get_logger

log = get_logger("monarch_oauth")

_OAUTH_BASE = "https://api.monarch.com"
_MCP_RESOURCE = f"{_OAUTH_BASE}/mcp"
_MCP_SCOPE = "mcp:read mcp:write"
_DISCOVERY_URLS = [
    f"{_OAUTH_BASE}/mcp/.well-known/oauth-authorization-server",
    f"{_OAUTH_BASE}/.well-known/oauth-authorization-server",
]
_TOKEN_CACHE = ROOT_DIR / "tokens" / "monarch_oauth.json"
_CALLBACK_PORT = 9432
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/callback"
_EXPIRY_BUFFER = 300  # refresh 5 minutes before actual expiry


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)


def _get_json(url: str, timeout: int = 10) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post_form(url: str, data: dict, timeout: int = 15) -> dict:
    body = urlencode(data).encode()
    req = Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body_text = _read_http_error(exc)
        log.error("POST %s failed (%s): %s", url, exc.code, body_text)
        raise RuntimeError(f"Monarch request to {url} failed ({exc.code}): {body_text}") from exc


def _post_json(url: str, data: dict, timeout: int = 10) -> dict:
    body = json.dumps(data).encode()
    req = Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body_text = _read_http_error(exc)
        log.error("POST %s failed (%s): %s", url, exc.code, body_text)
        raise RuntimeError(f"Monarch request to {url} failed ({exc.code}): {body_text}") from exc


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def _discover() -> dict:
    """Return OAuth server metadata, trying discovery URLs in order."""
    for url in _DISCOVERY_URLS:
        try:
            meta = _get_json(url)
            log.debug("OAuth metadata from %s", url)
            return meta
        except Exception as exc:
            log.debug("discovery at %s failed: %s", url, exc)
    log.warning("OAuth discovery failed for all URLs — using default endpoints")
    return {
        "authorization_endpoint": f"{_OAUTH_BASE}/oauth/authorize",
        "token_endpoint": f"{_OAUTH_BASE}/oauth/token",
    }


def _dynamic_register(registration_endpoint: str) -> str | None:
    """Try RFC 7591 dynamic client registration; returns client_id or None."""
    try:
        resp = _post_json(registration_endpoint, {
            "client_name": "JARVIS",
            "redirect_uris": [_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": _MCP_SCOPE,
        })
        cid = resp.get("client_id")
        if cid:
            log.info("dynamic registration OK: client_id=%s", cid)
        else:
            log.error("dynamic registration returned no client_id: %s", resp)
        return cid
    except Exception as exc:
        log.error("dynamic registration at %s failed: %s", registration_endpoint, exc)
        return None


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
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
                b"<p>Monarch Money is connected. You can close this tab.</p>"
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
    """Start local server, wait for OAuth redirect, return authorization code."""
    _CallbackHandler.code = None
    _CallbackHandler.error = None
    event = threading.Event()
    _CallbackHandler.done = event

    server = http.server.HTTPServer(("localhost", _CALLBACK_PORT), _CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        event.wait(timeout=timeout)
    finally:
        server.shutdown()

    if _CallbackHandler.error:
        raise RuntimeError(f"Monarch OAuth error: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise RuntimeError(
            "Monarch authorization timed out — no browser response within "
            f"{timeout}s. Try again."
        )
    return _CallbackHandler.code


# ── Token cache ───────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _valid(cache: dict) -> bool:
    return bool(
        cache.get("access_token")
        and float(cache.get("expires_at", 0)) > time.time() + _EXPIRY_BUFFER
    )


# ── Public API ────────────────────────────────────────────────────────────────

def get_monarch_token() -> str:
    """Return a valid Monarch Bearer token, running the browser flow if needed.

    Blocks the calling thread until the user authorizes (first run only).
    Subsequent calls return the cached token or silently refresh it.
    """
    cache = _load()
    if _valid(cache):
        return cache["access_token"]

    meta = _discover()
    auth_ep = meta.get("authorization_endpoint", f"{_OAUTH_BASE}/oauth/authorize")
    token_ep = meta.get("token_endpoint", f"{_OAUTH_BASE}/oauth/token")

    # Resolve client_id: cached → dynamic registration (required — Monarch
    # rejects unregistered/guessed client_ids with a 400 on the consent page).
    client_id: str = cache.get("client_id") or ""
    if not client_id:
        reg_ep = meta.get("registration_endpoint", "")
        if not reg_ep:
            raise RuntimeError(
                "Monarch OAuth discovery did not return a registration_endpoint "
                "— cannot register JARVIS as a client. Discovery metadata: "
                f"{meta}"
            )
        client_id = _dynamic_register(reg_ep) or ""
        if not client_id:
            raise RuntimeError(
                "Monarch dynamic client registration failed — see the log above "
                "for the server's error response. Cannot proceed without a "
                "valid client_id."
            )

    # Try silent refresh before full re-auth
    if cache.get("refresh_token"):
        try:
            tokens = _post_form(token_ep, {
                "grant_type": "refresh_token",
                "refresh_token": cache["refresh_token"],
                "client_id": client_id,
                "resource": _MCP_RESOURCE,
            })
            _save({
                **cache,
                "client_id": client_id,
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", cache["refresh_token"]),
                "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
                "token_endpoint": token_ep,
                "authorization_endpoint": auth_ep,
            })
            log.info("monarch token refreshed")
            return tokens["access_token"]
        except Exception as exc:
            log.warning("token refresh failed, re-authorizing: %s", exc)

    # Full authorization code + PKCE flow
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_url = auth_ep + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": _MCP_SCOPE,
        "resource": _MCP_RESOURCE,
    })

    print(
        "\n[JARVIS] Opening browser to authorize Monarch Money...\n"
        f"If it doesn't open automatically, visit:\n{auth_url}\n"
    )
    log.info("starting Monarch OAuth browser flow")
    webbrowser.open(auth_url)

    code = _await_callback()

    tokens = _post_form(token_ep, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": _MCP_RESOURCE,
    })

    _save({
        "client_id": client_id,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        "token_endpoint": token_ep,
        "authorization_endpoint": auth_ep,
    })
    log.info("monarch authorization complete — token cached")
    return tokens["access_token"]


def clear_token() -> None:
    """Delete cached token, forcing re-authorization on next use."""
    try:
        _TOKEN_CACHE.unlink()
        log.info("monarch token cache cleared")
    except FileNotFoundError:
        pass
