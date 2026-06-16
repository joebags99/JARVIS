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
import re
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

def _probe_for_resource_metadata_url(req: Request) -> str | None:
    """Send one probe request and look for a 401 + WWW-Authenticate
    resource_metadata hint. Logs whatever it actually got back (status code,
    headers, body excerpt) at warning level so failures are diagnosable.
    """
    try:
        with urlopen(req, timeout=10) as r:
            log.warning(
                "probe %s %s returned %s (expected 401) — no auth challenge to read",
                req.get_method(), req.full_url, r.status,
            )
            return None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            www_auth = exc.headers.get("WWW-Authenticate", "")
            m = re.search(r'resource_metadata="([^"]+)"', www_auth)
            if m:
                log.info("found resource_metadata hint: %s", m.group(1))
                return m.group(1)
            log.warning(
                "probe %s %s got 401 but no resource_metadata hint in "
                "WWW-Authenticate: %r (body: %s)",
                req.get_method(), req.full_url, www_auth, _read_http_error(exc)[:300],
            )
        else:
            log.warning(
                "probe %s %s got %s (expected 401): %s",
                req.get_method(), req.full_url, exc.code, _read_http_error(exc)[:300],
            )
    except Exception as exc:
        log.warning("probe %s %s failed: %s", req.get_method(), req.full_url, exc)
    return None


def _protected_resource_metadata() -> dict | None:
    """Probe the MCP endpoint for a 401 + WWW-Authenticate resource_metadata
    hint (RFC 9728), as defined by the MCP authorization spec. The resulting
    document lists the actual authorization server(s) for this resource —
    which may live on a completely different host than api.monarch.com.

    Tries both GET and POST (with a minimal JSON-RPC body) since the MCP
    streamable-HTTP transport expects POST as the primary request type and
    some servers reject a bare GET before ever checking auth.
    """
    probes = [
        Request(
            _MCP_RESOURCE,
            headers={"Accept": "application/json, text/event-stream"},
        ),
        Request(
            _MCP_RESOURCE,
            data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "JARVIS", "version": "1.0"},
                },
            }).encode(),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        ),
    ]

    metadata_url = None
    for req in probes:
        metadata_url = _probe_for_resource_metadata_url(req)
        if metadata_url:
            break

    if not metadata_url:
        return None

    try:
        return _get_json(metadata_url)
    except Exception as exc:
        log.warning("failed to fetch resource metadata %s: %s", metadata_url, exc)
        return None


def _discover() -> dict:
    """Return OAuth authorization-server metadata.

    Follows RFC 9728 + RFC 8414: probe the MCP resource for the
    authorization server(s) it trusts, then fetch each one's metadata
    (trying both the OAuth and OpenID Connect discovery document names,
    since the AS may be a third-party identity provider). Falls back to
    guessing well-known paths on api.monarch.com itself as a last resort.
    """
    candidates: list[str] = []
    resource_meta = _protected_resource_metadata()
    if resource_meta:
        for server in resource_meta.get("authorization_servers", []):
            server = server.rstrip("/")
            candidates.append(f"{server}/.well-known/oauth-authorization-server")
            candidates.append(f"{server}/.well-known/openid-configuration")
    candidates.extend(_DISCOVERY_URLS)

    for url in candidates:
        try:
            meta = _get_json(url)
            log.info("OAuth metadata from %s", url)
            return meta
        except Exception as exc:
            log.debug("discovery at %s failed: %s", url, exc)

    raise RuntimeError(
        "Could not discover Monarch's OAuth authorization server metadata. "
        f"Tried: {candidates}"
    )


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
