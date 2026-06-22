"""Tests for the pure routing helpers in app/claude_client.py."""

from __future__ import annotations

from app import claude_client as cc


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


def test_parse_fact_list():
    assert cc._parse_fact_list('["a", "b"]') == ["a", "b"]
    # tolerates surrounding prose around the JSON array
    assert cc._parse_fact_list('Here are the facts: ["x"]. Done.') == ["x"]
    assert cc._parse_fact_list("[]") == []
    assert cc._parse_fact_list("not json at all") == []
    # drops non-strings, blanks, and trims
    assert cc._parse_fact_list('["ok", 3, "", "  trim  "]') == ["ok", "trim"]


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
