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
