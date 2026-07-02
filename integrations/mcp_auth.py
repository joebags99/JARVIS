"""Auth-token resolvers for user-configured MCP servers (mcp_servers.json).

Each resolver takes a validated ``McpServerSpec`` and returns the bearer
token string to send as the Anthropic API's ``authorization_token`` (or ``""``
for no auth). Deliberately NOT extensible to arbitrary OAuth — Monarch's flow
is uniquely complex (dynamic client registration, PKCE, a local callback
server, a full browser flow — see monarch_oauth.py) and stays its own special
case wired directly into claude_client.py. This registry only covers the two
simple, common cases a self-hosted or API-key-gated MCP server actually needs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.config import McpServerSpec

log = get_logger("mcp_auth")


def _resolve_none(spec: "McpServerSpec") -> str:
    return ""


def _resolve_bearer_env(spec: "McpServerSpec") -> str:
    """Read a static bearer token from the server's configured env var.

    Returns "" (never raises) if the env var is unset/blank — the caller
    decides whether an empty token should still be sent or the server's
    attachment skipped entirely for this turn.
    """
    if not spec.auth_env_var:
        log.warning("mcp server %r: auth_type=bearer_env but no env_var set", spec.name)
        return ""
    token = os.environ.get(spec.auth_env_var, "").strip()
    if not token:
        log.warning("mcp server %r: env var %s is unset/blank", spec.name, spec.auth_env_var)
    return token


_RESOLVERS: dict[str, Callable[["McpServerSpec"], str]] = {
    "none": _resolve_none,
    "bearer_env": _resolve_bearer_env,
}


def resolve_token(spec: "McpServerSpec") -> str:
    """Return the bearer token for *spec*, or "" if its auth_type is unknown."""
    return _RESOLVERS.get(spec.auth_type, _resolve_none)(spec)
