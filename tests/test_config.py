"""Tests for the MCP-server config file (mcp_servers.json)."""

from __future__ import annotations

import json

import pytest

from app import config as cfg
from app.config import McpServerSpec


@pytest.fixture
def isolated_mcp_config(monkeypatch, tmp_path):
    """Point the MCP-servers config file at a throwaway location."""
    monkeypatch.setattr(cfg, "MCP_SERVERS_FILE", tmp_path / "mcp_servers.json")
    return tmp_path


def _write(path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_read_mcp_servers_config_missing_file_returns_empty(isolated_mcp_config):
    assert cfg._read_mcp_servers_config() == {}


def test_read_mcp_servers_config_malformed_json_returns_empty(isolated_mcp_config):
    cfg.MCP_SERVERS_FILE.write_text("{not valid json", encoding="utf-8")
    assert cfg._read_mcp_servers_config() == {}


def test_resolve_extra_mcp_servers_empty_when_file_absent(isolated_mcp_config):
    assert cfg._resolve_extra_mcp_servers() == []


def test_resolve_extra_mcp_servers_parses_valid_entries(isolated_mcp_config):
    _write(cfg.MCP_SERVERS_FILE, {"servers": [
        {
            "name": "my-server", "url": "https://mcp.example.com/mcp",
            "enabled": True, "auth": {"type": "bearer_env", "env_var": "MY_TOKEN"},
            "keywords": ["Widget", " gadget "],
        },
        {"name": "bare-server", "url": "https://bare.example.com/mcp"},
    ]})
    specs = cfg._resolve_extra_mcp_servers()
    assert specs == [
        McpServerSpec(
            name="my-server", url="https://mcp.example.com/mcp", enabled=True,
            auth_type="bearer_env", auth_env_var="MY_TOKEN", keywords=("widget", "gadget"),
        ),
        # defaults: enabled=True, auth.type="none", keywords=()
        McpServerSpec(name="bare-server", url="https://bare.example.com/mcp"),
    ]


def test_resolve_extra_mcp_servers_skips_missing_name_or_url(isolated_mcp_config):
    _write(cfg.MCP_SERVERS_FILE, {"servers": [
        {"name": "no-url"},
        {"url": "https://no-name.example.com/mcp"},
        {"name": "ok", "url": "https://ok.example.com/mcp"},
    ]})
    specs = cfg._resolve_extra_mcp_servers()
    assert [s.name for s in specs] == ["ok"]


def test_resolve_extra_mcp_servers_skips_unknown_auth_type(isolated_mcp_config):
    _write(cfg.MCP_SERVERS_FILE, {"servers": [
        {"name": "oauth-one", "url": "https://x.example.com/mcp", "auth": {"type": "oauth2"}},
        {"name": "ok", "url": "https://ok.example.com/mcp"},
    ]})
    specs = cfg._resolve_extra_mcp_servers()
    assert [s.name for s in specs] == ["ok"]


def test_resolve_extra_mcp_servers_skips_monarch_name_collision(isolated_mcp_config):
    _write(cfg.MCP_SERVERS_FILE, {"servers": [
        {"name": "Monarch", "url": "https://x.example.com/mcp"},
        {"name": "ok", "url": "https://ok.example.com/mcp"},
    ]})
    specs = cfg._resolve_extra_mcp_servers()
    assert [s.name for s in specs] == ["ok"]


def test_resolve_extra_mcp_servers_dedupes_by_name_keeping_last(isolated_mcp_config):
    _write(cfg.MCP_SERVERS_FILE, {"servers": [
        {"name": "dup", "url": "https://first.example.com/mcp"},
        {"name": "dup", "url": "https://second.example.com/mcp"},
    ]})
    specs = cfg._resolve_extra_mcp_servers()
    assert len(specs) == 1
    assert specs[0].url == "https://second.example.com/mcp"


def test_resolve_extra_mcp_servers_ignores_non_list_servers_key(isolated_mcp_config):
    _write(cfg.MCP_SERVERS_FILE, {"servers": "not-a-list"})
    assert cfg._resolve_extra_mcp_servers() == []


# ── Ambient HUD (see app/hud.py) ──────────────────────────────────────────────

def test_hud_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv("JARVIS_HUD_ENABLED", raising=False)
    assert cfg.Config().hud_enabled is False


def test_hud_enabled_true_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_HUD_ENABLED", "true")
    assert cfg.Config().hud_enabled is True


def test_hud_position_defaults_top_left(monkeypatch):
    monkeypatch.delenv("JARVIS_HUD_POSITION", raising=False)
    assert cfg.Config().hud_position == "top-left"
