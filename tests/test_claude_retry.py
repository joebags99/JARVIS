"""Transient-error retry in ClaudeClient._run_send.

A momentary Anthropic overload (529) / rate-limit (429) should retry with backoff
instead of failing the user's turn — but only before any tool has run, so a
retry can never duplicate a tool side effect. Driven here with a fake transport.
"""

from __future__ import annotations

import app.claude_client as cc
from app.claude_client import ClaudeClient


class _Overloaded(Exception):
    status_code = 529

    def __str__(self):
        return "Overloaded"


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
    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.calls = 0

    def stream(self, **kwargs):
        b = self.behaviors[min(self.calls, len(self.behaviors) - 1)]
        self.calls += 1
        if isinstance(b, Exception):
            raise b
        return _FakeStream(b)


class _FakeClient:
    def __init__(self, behaviors):
        self.messages = _FakeMessages(behaviors)


def _client_with(behaviors, monkeypatch):
    monkeypatch.setattr(cc, "_API_RETRY_BACKOFF", 0)  # don't actually sleep
    client = ClaudeClient()
    client._client = _FakeClient(behaviors)  # force ready + fake transport
    return client


def test_retries_transient_overload_then_succeeds(monkeypatch):
    client = _client_with([_Overloaded(), "Hello there."], monkeypatch)
    reply = client.send("hi")
    assert reply == "Hello there."
    assert client._client.messages.calls == 2  # one fail, one success
    assert [m["role"] for m in client.history] == ["user", "assistant"]


def test_non_transient_error_is_not_retried(monkeypatch):
    client = _client_with([ValueError("bad request")], monkeypatch)
    reply = client.send("hi")
    assert reply.startswith("⚠️ Error contacting Claude")
    assert client._client.messages.calls == 1


def test_gives_up_after_max_retries(monkeypatch):
    client = _client_with([_Overloaded()] * 5, monkeypatch)
    reply = client.send("hi")
    assert reply.startswith("⚠️ Error contacting Claude")
    assert client._client.messages.calls == cc._MAX_API_RETRIES + 1
    assert client.history == []  # turn fully rolled back on final failure
