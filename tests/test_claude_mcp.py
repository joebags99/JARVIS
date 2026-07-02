"""Generic MCP plug-in support (mcp_servers.json -> CONFIG.extra_mcp_servers).

Monarch-specific behavior stays untested here (no live server, and this repo
has never had live Monarch tests — see tests/test_claude_retry.py's docstring
for the same reasoning re: transient errors). This file covers the new,
generalized attach/detach mechanism with a fake transport, extended from
test_claude_retry.py's pattern with a `.beta.messages` route too, since
attaching any MCP server switches claude_client.py to the beta endpoint.
"""

from __future__ import annotations

import types

import app.claude_client as cc
from app.claude_client import ClaudeClient
from app.config import McpServerSpec


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Final:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        yield self._text

    def get_final_message(self):
        return _Final(self._text)


class _FakeMessages:
    """Records every call's kwargs so tests can inspect what was actually sent."""

    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.calls = 0
        self.calls_kwargs: list[dict] = []

    def stream(self, **kwargs):
        self.calls_kwargs.append(kwargs)
        b = self.behaviors[min(self.calls, len(self.behaviors) - 1)]
        self.calls += 1
        if isinstance(b, Exception):
            raise b
        return _FakeStream(b)


class _FakeClient:
    def __init__(self, behaviors):
        messages = _FakeMessages(behaviors)
        self.messages = messages
        # The real Anthropic client exposes both .messages and .beta.messages;
        # route both through the same fake so call count/ordering stays
        # coherent regardless of which endpoint claude_client.py picks.
        self.beta = types.SimpleNamespace(messages=messages)


def _client_with(behaviors, monkeypatch):
    monkeypatch.setattr(cc, "_API_RETRY_BACKOFF", 0)  # don't actually sleep
    client = ClaudeClient()
    client._client = _FakeClient(behaviors)  # force ready + fake transport
    return client


def _spec(**over) -> McpServerSpec:
    defaults = dict(name="widgets", url="https://mcp.example.com/mcp")
    defaults.update(over)
    return McpServerSpec(**defaults)


def test_configured_server_attaches_mcp_servers_and_beta_endpoint(monkeypatch):
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [_spec()])
    client = _client_with(["Sure, here you go."], monkeypatch)

    reply = client.send("tell me about widgets")

    assert reply == "Sure, here you go."
    kwargs = client._client.messages.calls_kwargs[0]
    assert kwargs["mcp_servers"] == [
        {"type": "url", "name": "widgets", "url": "https://mcp.example.com/mcp"}
    ]
    assert kwargs["betas"] == [cc._MCP_BETA]
    assert kwargs["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


def test_configured_server_keyword_gating(monkeypatch):
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [_spec(keywords=("widget",))])

    unrelated = _client_with(["no widgets mentioned"], monkeypatch)
    unrelated.send("what's the weather today")
    assert "mcp_servers" not in unrelated._client.messages.calls_kwargs[0]

    related = _client_with(["widget info"], monkeypatch)
    related.send("tell me about my widget")
    kwargs = related._client.messages.calls_kwargs[0]
    assert kwargs["mcp_servers"][0]["name"] == "widgets"


def test_configured_server_bearer_env_auth_reads_token(monkeypatch):
    monkeypatch.setenv("WIDGET_TOKEN", "secret123")
    spec = _spec(auth_type="bearer_env", auth_env_var="WIDGET_TOKEN")
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [spec])
    client = _client_with(["ok"], monkeypatch)

    client.send("tell me about widgets")

    kwargs = client._client.messages.calls_kwargs[0]
    assert kwargs["mcp_servers"][0]["authorization_token"] == "secret123"


def test_configured_server_skipped_when_bearer_env_token_missing(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    spec = _spec(auth_type="bearer_env", auth_env_var="MISSING_TOKEN")
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [spec])
    client = _client_with(["ok"], monkeypatch)

    client.send("tell me about widgets")

    assert "mcp_servers" not in client._client.messages.calls_kwargs[0]


def test_disabled_configured_server_never_attaches(monkeypatch):
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [_spec(enabled=False)])
    client = _client_with(["ok"], monkeypatch)

    client.send("tell me about widgets")

    assert "mcp_servers" not in client._client.messages.calls_kwargs[0]


def test_monarch_and_configured_server_can_coexist(monkeypatch):
    monkeypatch.setattr(cc.CONFIG, "monarch_enabled", True)
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [_spec()])
    import integrations.monarch_oauth as monarch_oauth
    monkeypatch.setattr(monarch_oauth, "get_monarch_token", lambda: "monarch-token")
    client = _client_with(["ok"], monkeypatch)

    client.send("how much did I spend on groceries this month?")

    kwargs = client._client.messages.calls_kwargs[0]
    assert {s["name"] for s in kwargs["mcp_servers"]} == {"monarch", "widgets"}


def test_any_mcp_attached_disables_want_web(monkeypatch):
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [_spec()])  # always-on
    client = _client_with(["ok"], monkeypatch)

    # A meal-related message would normally attach web search, but the
    # always-on configured server takes the MCP-safe path instead.
    client.send("what's for dinner tonight?")

    kwargs = client._client.messages.calls_kwargs[0]
    assert "mcp_servers" in kwargs
    assert kwargs["tools"] == cc.TOOLS  # not TOOLS + [_WEB_SEARCH_TOOL]


def test_mcp_connection_error_retries_with_all_servers_detached(monkeypatch):
    monkeypatch.setattr(cc.CONFIG, "extra_mcp_servers", [_spec()])
    mcp_error = Exception("Connection error while communicating with MCP server")
    client = _client_with([mcp_error, "Fallback answer."], monkeypatch)

    reply = client.send("tell me about widgets")

    assert reply == "Fallback answer."
    assert client._client.messages.calls == 2
    first_kwargs, second_kwargs = client._client.messages.calls_kwargs
    assert "mcp_servers" in first_kwargs
    assert "mcp_servers" not in second_kwargs
    assert [m["role"] for m in client.history] == ["user", "assistant"]
