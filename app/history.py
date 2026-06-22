"""Conversation-history helpers: block serialization, cache breakpoints, compaction.

These are the pure transforms over the session ``history`` list (a list of
``{"role": ..., "content": ...}`` dicts) that ``ClaudeClient`` relies on. They
were extracted from ``claude_client.py`` so they can be reasoned about and
unit-tested without standing up the Anthropic SDK or the overlay: every function
here either returns a value or mutates the list it is given, and none of them
touch the network.
"""

from __future__ import annotations

from typing import Callable

from .logging_setup import get_logger

log = get_logger("history")

# Once a session accumulates more than HISTORY_MAX_TURNS user turns, older turns
# are summarized away to bound per-request token cost, keeping the most recent
# HISTORY_KEEP_TURNS verbatim.
HISTORY_MAX_TURNS = 8
HISTORY_KEEP_TURNS = 3
# When compacting, fat tool_result payloads (calendar dumps, note bodies, email
# lists) from older turns are truncated to this many chars — the model already
# acted on them, so the full text no longer needs to ride along every request.
TOOL_RESULT_KEEP_CHARS = 600


def block_to_dict(block) -> dict:
    """Serialize an Anthropic content block to a plain dict for history storage."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # MCP-related block types (mcp_tool_use, mcp_tool_result, server_tool_use, ...)
    # carry fields we don't model explicitly — keep them intact so replaying this
    # history back to the API doesn't drop data the API itself put there.
    return block.model_dump()


def with_message_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Return ``messages`` with an ephemeral cache breakpoint on the final block.

    Prompt caching is a prefix match over ``tools → system → messages``. The
    tools+system prefix already carries its own breakpoint, but the *messages*
    array is otherwise re-billed as fresh input on every request. A single
    ``send()`` can fire several API calls, each re-sending the whole (and
    growing) history; follow-up turns in a session re-send it again.

    Marking the last block moves the breakpoint to the end of the conversation
    each round. The previous request's breakpoint is now a prefix of this one,
    so the API serves that span from cache (a ~10x cheaper read) and only the
    newest turn is written fresh — the standard incremental multi-turn pattern.

    The stored history is never mutated: only a shallow copy down to the last
    block is made, so cache_control markers never accumulate or persist.
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    breakpoint = {"type": "ephemeral"}
    if isinstance(content, str):
        if not content:
            return messages  # empty text block would be rejected by the API
        last["content"] = [
            {"type": "text", "text": content, "cache_control": breakpoint}
        ]
    elif isinstance(content, list) and content:
        new_content = list(content)
        block = dict(new_content[-1])
        block["cache_control"] = breakpoint
        new_content[-1] = block
        last["content"] = new_content
    else:
        return messages  # nothing markable (e.g. empty content list)
    return messages[:-1] + [last]


def trim_old_tool_results(history: list[dict]) -> int:
    """Truncate large tool_result payloads from all but the most recent turn.

    Only touches the ``tool_result`` blocks JARVIS itself appends (plain string
    content in user-role messages) and leaves the latest turn's results full, so
    the current working context is untouched while stale calendar/notes/email
    dumps stop riding along on every request. Mutates ``history`` in place and
    returns the number of blocks trimmed.
    """
    boundaries = [
        i for i, m in enumerate(history)
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    cutoff = boundaries[-1] if boundaries else len(history)
    trimmed = 0
    for m in history[:cutoff]:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            content = block.get("content")
            if isinstance(content, str) and len(content) > TOOL_RESULT_KEEP_CHARS:
                block["content"] = (
                    content[:TOOL_RESULT_KEEP_CHARS].rstrip()
                    + "\n…(older tool output trimmed to save context)"
                )
                trimmed += 1
    if trimmed:
        log.info("trimmed %d older tool-result block(s) during compaction", trimmed)
    return trimmed


def compact_history(
    history: list[dict],
    summarize: Callable[[list[dict]], str],
    *,
    max_turns: int = HISTORY_MAX_TURNS,
    keep_turns: int = HISTORY_KEEP_TURNS,
) -> list[dict]:
    """Summarize older turns once history grows long, to bound token cost.

    Returns the (possibly rebuilt) history. Only cuts at "turn boundaries" —
    history entries that are a plain string user message, never a tool_result
    list — so the cut point always falls between complete assistant turns and
    can't orphan a tool_use/tool_result pairing. If there's nothing to compact,
    or ``summarize`` returns an empty string, the original list is returned
    unchanged.
    """
    boundaries = [
        i for i, m in enumerate(history)
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    if len(boundaries) <= max_turns:
        return history

    cut = boundaries[-keep_turns]
    older, recent = history[:cut], history[cut:]
    summary = summarize(older)
    if not summary:
        return history

    new_history = [
        {"role": "user", "content": f"[Earlier in this session:]\n{summary}"},
        {"role": "assistant", "content": "Got it, I'll keep that in mind."},
    ] + recent
    trim_old_tool_results(new_history)
    log.info("compacted %d older turn(s) into a summary", len(boundaries) - keep_turns)
    return new_history
