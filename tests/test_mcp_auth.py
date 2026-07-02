"""Tests for the MCP server auth-token resolvers (integrations/mcp_auth.py)."""

from __future__ import annotations

from app.config import McpServerSpec
from integrations.mcp_auth import resolve_token


def _spec(**over) -> McpServerSpec:
    defaults = dict(name="s", url="https://x.example.com/mcp")
    defaults.update(over)
    return McpServerSpec(**defaults)


def test_resolve_none_returns_empty_string():
    assert resolve_token(_spec(auth_type="none")) == ""


def test_resolve_bearer_env_reads_env_var(monkeypatch):
    monkeypatch.setenv("FOO_TOKEN", "abc123")
    spec = _spec(auth_type="bearer_env", auth_env_var="FOO_TOKEN")
    assert resolve_token(spec) == "abc123"


def test_resolve_bearer_env_missing_var_returns_empty(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    spec = _spec(auth_type="bearer_env", auth_env_var="MISSING_TOKEN")
    assert resolve_token(spec) == ""


def test_resolve_bearer_env_blank_var_returns_empty(monkeypatch):
    monkeypatch.setenv("BLANK_TOKEN", "   ")
    spec = _spec(auth_type="bearer_env", auth_env_var="BLANK_TOKEN")
    assert resolve_token(spec) == ""


def test_resolve_bearer_env_no_env_var_configured_returns_empty():
    spec = _spec(auth_type="bearer_env", auth_env_var="")
    assert resolve_token(spec) == ""


def test_resolve_token_unknown_auth_type_falls_back_to_none():
    # Bypasses config-time validation (which would normally reject this) to
    # confirm the resolver itself never raises on an unexpected type.
    spec = _spec(auth_type="oauth2")
    assert resolve_token(spec) == ""
