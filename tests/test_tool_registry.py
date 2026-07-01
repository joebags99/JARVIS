"""Tests for the tool registry (app/tool_registry.py).

The base-tool snapshot is the guard the Phase-1 refactor promised: it pins the
set and order of tools the model sees so the registry stays equivalent to the
old hand-maintained TOOLS list.
"""

from __future__ import annotations

from app import tool_registry as tr

EXPECTED_BASE_TOOLS = [
    "get_calendar_events",
    "get_weather",
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

# The five vault tools, advertised only when an Obsidian vault is configured.
VAULT_TOOLS = ["search_vault", "read_note", "write_note", "append_note", "list_notes"]


def test_base_tools_snapshot():
    # Gmail/Spotify/Obsidian aren't configured in the test environment, so
    # api_tools() returns exactly the always-available base tools, in order.
    names = [t["name"] for t in tr.api_tools()]
    assert names == EXPECTED_BASE_TOOLS


def test_vault_tools_appear_when_obsidian_available(monkeypatch):
    from app.config import CONFIG

    monkeypatch.setattr(CONFIG, "obsidian_enabled", True)
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", "/tmp/vault")
    names = [t["name"] for t in tr.api_tools()]
    assert all(v in names for v in VAULT_TOOLS)


def test_vault_tools_hidden_when_obsidian_unavailable():
    names = [t["name"] for t in tr.api_tools()]
    assert not any(v in names for v in VAULT_TOOLS)


def test_every_spec_has_required_keys():
    for spec in tr.api_tools():
        assert set(spec) == {"name", "description", "input_schema"}
        assert spec["input_schema"]["type"] == "object"


def test_execute_unknown_tool():
    assert tr.execute_tool("does_not_exist", {}) == "Unknown tool: does_not_exist"


def test_category_enums_match_config():
    """The task category enums are derived from CONFIG.categories."""
    from app.config import CONFIG

    specs = {t["name"]: t for t in tr.api_tools()}
    for name, prop in (
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
