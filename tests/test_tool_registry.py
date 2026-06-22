"""Tests for the tool registry (app/tool_registry.py).

The base-tool snapshot is the guard the Phase-1 refactor promised: it pins the
set and order of tools the model sees so the registry stays equivalent to the
old hand-maintained TOOLS list.
"""

from __future__ import annotations

from app import tool_registry as tr

EXPECTED_BASE_TOOLS = [
    "get_calendar_events",
    "get_recent_notes",
    "create_note",
    "get_weather",
    "recall_session_history",
    "update_calendar_event",
    "create_calendar_event",
    "get_todos",
    "create_todo",
    "complete_todo",
    "update_todo",
    "get_meal_history",
    "create_meal_plan",
    "set_personality",
    "load_knowledge_pool",
]


def test_base_tools_snapshot():
    # Gmail/Spotify aren't configured in the test environment, so api_tools()
    # returns exactly the always-available base tools, in registration order.
    names = [t["name"] for t in tr.api_tools()]
    assert names == EXPECTED_BASE_TOOLS


def test_every_spec_has_required_keys():
    for spec in tr.api_tools():
        assert set(spec) == {"name", "description", "input_schema"}
        assert spec["input_schema"]["type"] == "object"


def test_execute_unknown_tool():
    assert tr.execute_tool("does_not_exist", {}) == "Unknown tool: does_not_exist"


def test_category_enums_match_config():
    """The note/task category enums are derived from CONFIG.categories."""
    from app.config import CONFIG

    specs = {t["name"]: t for t in tr.api_tools()}
    for name, prop in (
        ("get_recent_notes", "category"),
        ("create_note", "category"),
        ("create_todo", "category"),
        ("update_todo", "new_category"),
    ):
        enum = specs[name]["input_schema"]["properties"][prop]["enum"]
        assert enum == CONFIG.categories


def test_availability_gating(monkeypatch):
    sentinel = tr.Tool(
        name="_temp_test_tool",
        description="temp",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda d: "ok",
        available=lambda: False,
    )
    tr.register(sentinel)
    try:
        assert "_temp_test_tool" not in [t["name"] for t in tr.api_tools()]
        # dispatch still works regardless of availability gating
        assert tr.execute_tool("_temp_test_tool", {}) == "ok"
    finally:
        tr._REGISTRY.pop("_temp_test_tool", None)


def test_tool_default_available_is_true():
    t = tr.Tool(
        name="x", description="d",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda d: "",
    )
    assert t.available() is True
