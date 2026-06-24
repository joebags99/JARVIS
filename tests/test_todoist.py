"""Tests for Todoist due-date resolution (integrations/todoist.py).

``_resolve_due`` anchors natural-language phrases to the real clock locally
rather than trusting the model, so these assert which Todoist field a phrase
maps to (date vs. datetime vs. passed-through string) rather than exact values.
"""

from __future__ import annotations

from integrations import todoist


def test_recurring_phrase_passes_through_as_due_string():
    assert todoist._resolve_due("every Monday") == {"due_string": "every Monday"}
    assert todoist._resolve_due("daily") == {"due_string": "daily"}


def test_date_only_phrase_maps_to_due_date():
    out = todoist._resolve_due("tomorrow")
    assert "due_date" in out and "due_datetime" not in out


def test_phrase_with_time_maps_to_due_datetime():
    out = todoist._resolve_due("tomorrow at 3pm")
    assert "due_datetime" in out


def test_unparseable_phrase_passes_through():
    assert todoist._resolve_due("no date") == {"due_string": "no date"}
