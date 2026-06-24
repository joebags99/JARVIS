"""Tests for the conversation-history helpers (app/history.py)."""

from __future__ import annotations

from app import history


class _Block:
    """Minimal stand-in for an Anthropic content block."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return {"type": self.type, "dumped": True}


def test_block_to_dict_text():
    b = _Block(type="text", text="hello")
    assert history.block_to_dict(b) == {"type": "text", "text": "hello"}


def test_block_to_dict_tool_use():
    b = _Block(type="tool_use", id="t1", name="get_weather", input={"days": 1})
    assert history.block_to_dict(b) == {
        "type": "tool_use", "id": "t1", "name": "get_weather", "input": {"days": 1},
    }


def test_block_to_dict_other_uses_model_dump():
    b = _Block(type="mcp_tool_use")
    assert history.block_to_dict(b) == {"type": "mcp_tool_use", "dumped": True}


def test_cache_breakpoint_empty():
    assert history.with_message_cache_breakpoint([]) == []


def test_cache_breakpoint_string_content():
    msgs = [{"role": "user", "content": "hi"}]
    out = history.with_message_cache_breakpoint(msgs)
    assert out[-1]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]
    # original is not mutated
    assert msgs[-1]["content"] == "hi"


def test_cache_breakpoint_empty_string_is_left_alone():
    msgs = [{"role": "user", "content": ""}]
    assert history.with_message_cache_breakpoint(msgs) is msgs


def test_cache_breakpoint_list_content():
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
    out = history.with_message_cache_breakpoint(msgs)
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[-1]["content"][-1]


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def test_compact_history_below_threshold_unchanged():
    hist = [_user("u%d" % i) for i in range(3)]
    out = history.compact_history(hist, lambda older: "SUMMARY")
    assert out is hist


def test_compact_history_empty_summary_unchanged():
    hist = [_user(f"u{i}") for i in range(history.HISTORY_MAX_TURNS + 2)]
    out = history.compact_history(hist, lambda older: "")
    assert out is hist


def test_compact_history_compacts_old_turns():
    # 10 alternating user/assistant turns → 10 user boundaries (> max of 8).
    hist = []
    for i in range(history.HISTORY_MAX_TURNS + 2):
        hist.append(_user(f"u{i}"))
        hist.append(_assistant(f"a{i}"))

    out = history.compact_history(hist, lambda older: "RECAP")
    assert out[0] == {"role": "user", "content": "[Earlier in this session:]\nRECAP"}
    assert out[1] == {"role": "assistant", "content": "Got it, I'll keep that in mind."}
    # The most recent HISTORY_KEEP_TURNS user turns survive verbatim.
    kept_users = [m["content"] for m in out if m["role"] == "user"]
    assert "u9" in kept_users and "u8" in kept_users and "u7" in kept_users


def test_trim_old_tool_results():
    long_old = "x" * (history.TOOL_RESULT_KEEP_CHARS + 50)
    long_new = "y" * (history.TOOL_RESULT_KEEP_CHARS + 50)
    hist = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1", "content": long_old},
        ]},
        {"role": "user", "content": "latest"},  # newest boundary
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "2", "content": long_new},
        ]},
    ]
    trimmed = history.trim_old_tool_results(hist)
    assert trimmed == 1
    assert "trimmed to save context" in hist[1]["content"][0]["content"]
    assert hist[3]["content"][0]["content"] == long_new  # latest untouched
