"""Tests for the pure routing helpers in app/claude_client.py."""

from __future__ import annotations

from app import claude_client as cc
from app.config import McpServerSpec


def test_looks_financial():
    assert cc._looks_financial("how much did I spend on groceries?")
    assert cc._looks_financial("what's my net worth")
    assert not cc._looks_financial("what's on my calendar today")


def test_looks_meal_related():
    assert cc._looks_meal_related("what's for dinner this week")
    assert cc._looks_meal_related("plan my meal prep")
    assert not cc._looks_meal_related("schedule a meeting tomorrow")


def test_is_easter_egg():
    assert cc._is_easter_egg("Hey JARVIS, what's on the calendar for today?")
    assert cc._is_easter_egg("hey jarvis what on the calendar for today")
    # ordinary calendar questions must not trigger it
    assert not cc._is_easter_egg("what's on my calendar today")
    assert not cc._is_easter_egg("hey jarvis play some music")


def test_parse_facts():
    # structured objects with subject + kind
    out = cc._parse_facts('[{"fact":"Allergic to shellfish","subject":"Joe","kind":"person"}]')
    assert out == [{"fact": "Allergic to shellfish", "subject": "Joe", "kind": "person"}]
    # company is a recognized kind
    assert cc._parse_facts('[{"fact":"B2B SaaS","subject":"Acme","kind":"company"}]') == \
        [{"fact": "B2B SaaS", "subject": "Acme", "kind": "company"}]
    # bare strings (back-compat) become subjectless facts
    assert cc._parse_facts('["x"]') == [{"fact": "x", "subject": "", "kind": ""}]
    # tolerates surrounding prose, normalizes an unknown kind, drops blanks
    assert cc._parse_facts('facts: [{"fact":"a","kind":"weird"}]. done')[0] == \
        {"fact": "a", "subject": "", "kind": ""}
    assert cc._parse_facts('[{"fact":"  "}, "", 3]') == []
    assert cc._parse_facts("[]") == []
    assert cc._parse_facts("not json at all") == []


def test_is_transient_api_error():
    class _Status(Exception):
        def __init__(self, code):
            self.status_code = code
            super().__init__("api error")

    assert cc._is_transient_api_error(_Status(529))   # overloaded
    assert cc._is_transient_api_error(_Status(429))   # rate limit
    assert cc._is_transient_api_error(_Status(503))
    assert not cc._is_transient_api_error(_Status(400))
    assert not cc._is_transient_api_error(_Status(404))
    # message-based detection when no status_code is exposed
    assert cc._is_transient_api_error(Exception("Overloaded"))
    assert cc._is_transient_api_error(Exception("rate limit exceeded"))
    assert not cc._is_transient_api_error(ValueError("bad input"))


def _spec(**over) -> McpServerSpec:
    defaults = dict(name="s", url="https://x.example.com/mcp")
    defaults.update(over)
    return McpServerSpec(**defaults)


def test_server_wanted_no_keywords_always_true():
    spec = _spec(keywords=())
    assert cc._server_wanted(spec, "anything at all", was_active=False, other_sticky_topic_now=False)
    assert cc._server_wanted(spec, "anything at all", was_active=False, other_sticky_topic_now=True)


def test_server_wanted_keyword_match_now():
    spec = _spec(keywords=("widget",))
    assert cc._server_wanted(spec, "tell me about my widget", was_active=False, other_sticky_topic_now=False)


def test_server_wanted_sticky_when_previously_active():
    spec = _spec(keywords=("widget",))
    assert cc._server_wanted(spec, "what about yesterday?", was_active=True, other_sticky_topic_now=False)


def test_server_wanted_sticky_broken_by_other_topic():
    spec = _spec(keywords=("widget",))
    assert not cc._server_wanted(spec, "what's for dinner?", was_active=True, other_sticky_topic_now=True)


def test_server_wanted_no_match_not_active_not_sticky():
    spec = _spec(keywords=("widget",))
    assert not cc._server_wanted(spec, "what's on my calendar", was_active=False, other_sticky_topic_now=False)


def test_history_to_lines_text_blocks_only():
    hist = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "1", "name": "x", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1", "content": "data"},
        ]},
    ]
    assert cc._history_to_lines(hist) == ["USER: hello", "ASSISTANT: hi"]
