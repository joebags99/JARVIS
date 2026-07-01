"""Anthropic Claude API client with streaming, context injection, and tool use.

Maintains the per-session conversation history and streams responses token by
token via a callback so the overlay can update the UI in real time. All network
work happens on a background thread (the caller is responsible for that); this
module is intentionally synchronous and stream-oriented.

Tool use flow:
  Bounded loop (see MAX_TOOL_ROUNDS): each round streams a response, and if
  Claude emits a local tool_use block, the tool runs and its result is fed
  back for another round. Parallel tool use is disabled so a local tool_use
  and the remote Monarch MCP tool are never requested in the same turn —
  that combination left a dangling, unpaired mcp_tool_use block in history.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

from . import history
from .config import CONFIG
from .context_builder import ContextBuilder
from .logging_setup import get_logger, new_turn_id
from .tool_registry import api_tools, execute_tool
from . import usage

log = get_logger("claude")

MAX_TOKENS = 2048
# Meal-plan tool calls carry 14 dinners with recipe text plus a shopping
# list — easily several times MAX_TOKENS — so they get a bigger budget for
# that turn instead of risking a truncated, invalid tool_use JSON payload.
_MEAL_MAX_TOKENS = 4096
# If a round still hits the output cap *while emitting a tool_use*, its JSON
# arguments are truncated and would fail to execute (e.g. a half-written
# create_note loses its 'content'). We re-run that round once with this larger
# budget rather than running a malformed call. Output is billed per token
# actually generated, so a high ceiling costs nothing on normal short replies.
_MAX_TOKENS_CEILING = 8192
MAX_TOOL_ROUNDS = 16  # safety cap on local tool_use <-> tool_result round trips

# Transient Anthropic API failures (529 overloaded, 429 rate-limit, 5xx) are
# server-side blips, not bad requests, so the turn is retried with backoff on top
# of the SDK's own quick retries — a brief overload then self-heals instead of
# failing the user's request. Only retried before any tool has run this turn (see
# _run_send), so a retry can never re-fire a tool's side effect.
_MAX_API_RETRIES = 2
_API_RETRY_BACKOFF = 2.0  # seconds → 2s, 4s
_TRANSIENT_API_STATUSES = frozenset({429, 500, 503, 529})

_MONARCH_MCP_URL = "https://api.monarch.com/mcp"
_MCP_BETA = "mcp-client-2025-04-04"

# Finance-related keywords used to decide whether a message warrants attaching
# the remote Monarch MCP server. Attaching it on every request — regardless of
# topic — burns tokens on tool definitions the model never uses.
_FINANCIAL_KEYWORDS = (
    "spend", "spent", "spending", "budget", "transaction", "expense",
    "balance", "money", "financ", "income", "invest", "bill", "savings",
    "credit", "debit", "bank", "monarch", "net worth", "cash flow",
    "subscription", "paycheck", "afford", "owe", "debt",
)

# Meal-related keywords used to decide whether to attach the native web
# search tool — same reasoning as _FINANCIAL_KEYWORDS: only pay for it when
# the conversation is actually about food.
_MEAL_KEYWORDS = (
    "meal", "dinner", "recipe", "cook", "cooking", "groceries", "grocery",
    "meal prep", "meal plan", "what's for dinner", "leftovers",
)

# Native server-side web search tool (no separate API key — billed through
# the Anthropic account). Dated tool versions follow the same convention as
# other Claude tool types; bump this if Anthropic ships a newer one.
_WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
}

def _looks_financial(message: str) -> bool:
    lowered = message.lower()
    return any(kw in lowered for kw in _FINANCIAL_KEYWORDS)


def _looks_meal_related(message: str) -> bool:
    lowered = message.lower()
    return any(kw in lowered for kw in _MEAL_KEYWORDS)

# ── Tool list ────────────────────────────────────────────────────────────────
# Built once from the registry; per-install availability gating (Gmail/Spotify)
# lives in tool_registry.api_tools(), so the cached tool prefix stays stable.
TOOLS = api_tools()


# ── Easter egg ──────────────────────────────────────────────────────────────
# Saying the specific incantation "Hey Jarvis, what on the calendar for today?"
# maxes humor + sarcasm for that one reply and cues up Back in Black. Gated on
# the "hey jarvis" prefix so ordinary calendar questions stay normal.
# Pin the exact track by URI so search ranking can't substitute the artist's
# most-streamed song (a bare "Back in Black AC/DC" search returned Thunderstruck).
_EASTER_EGG_SONG = "spotify:track:08mG3Y1vljYA6bvDt4Wqkj"  # AC/DC — Back in Black


def _is_easter_egg(message: str) -> bool:
    norm = " ".join(re.sub(r"[^a-z0-9 ]", " ", (message or "").lower()).split())
    return "hey jarvis" in norm and "calendar" in norm and "today" in norm



class _MonarchUnavailable(Exception):
    """Internal signal that the Monarch MCP server couldn't be reached this turn.

    Raised from the per-turn helper so ``send`` can retry once with the MCP
    server detached instead of failing the whole answer."""


def _is_mcp_connection_error(exc: Exception) -> bool:
    """True for the transient 400 the API raises when it can't reach an MCP server.

    Monarch's hosted MCP server occasionally goes unavailable/unresponsive; the
    API surfaces that as ``invalid_request_error`` with a "Connection error
    while communicating with MCP server" message. That's not a problem with the
    request itself, so it's worth retrying the turn without the MCP server
    attached rather than failing the whole answer.
    """
    msg = str(exc).lower()
    return "mcp server" in msg and (
        "connection error" in msg or "unavailable" in msg or "unresponsive" in msg
    )


def _is_transient_api_error(exc: Exception) -> bool:
    """True for a transient Anthropic API failure (overload / rate-limit / 5xx).

    These are server-side blips worth retrying, distinct from a malformed request
    (400/401/404). Detected by HTTP status when the SDK exposes one, else by the
    overloaded/rate-limit keywords in the message.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_API_STATUSES:
        return True
    msg = str(exc).lower()
    return "overloaded" in msg or "rate limit" in msg or "rate_limit" in msg


# ── Memory: durable-fact extraction ──────────────────────────────────────────
_FACT_EXTRACTION_PROMPT = (
    "From this JARVIS conversation, extract DURABLE facts and stable preferences "
    "worth remembering long-term — about the user, or about the people, companies, "
    "and projects they mention. Examples: \"Allergic to shellfish\", \"Prefers "
    "concise answers\", \"Leads infrastructure at Daedabyte\", \"Targeting ~$50k a "
    "year in web revenue\". Exclude transient one-off details (today's schedule, "
    "this week's tasks, the current question).\n\n"
    "Return ONLY a JSON array; each item is an object "
    '{"fact": "<concise fact>", "subject": "<the person, company, or project it '
    'is about>", "kind": "person" | "company" | "project" | "self"}. Use "person" '
    'for an individual, "company" for an organization/business, "project" for a '
    'project or initiative, and "self" (with the user as the subject) for a fact '
    "about the user. Return [] if nothing is durable."
)


def _history_to_lines(history: list[dict]) -> list[str]:
    """Flatten a conversation history to 'ROLE: text' lines (text blocks only)."""
    lines: list[str] = []
    for m in history:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, str) and content.strip():
            lines.append(f"{role.upper()}: {content.strip()}")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "").strip()
                    if t:
                        lines.append(f"{role.upper()}: {t}")
    return lines


def _parse_facts(text: str) -> list[dict]:
    """Parse the extraction reply into ``[{fact, subject, kind}]`` (best-effort).

    Accepts the structured object form and, for resilience, a bare JSON array of
    strings (treated as subjectless facts). Unknown ``kind`` values and blank
    facts are dropped.
    """
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        import json
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if isinstance(item, str):
            fact, subject, kind = item.strip(), "", ""
        elif isinstance(item, dict):
            fact = str(item.get("fact") or "").strip()
            subject = str(item.get("subject") or "").strip()
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in ("person", "company", "project", "self"):
                kind = ""
        else:
            continue
        if fact:
            out.append({"fact": fact, "subject": subject, "kind": kind})
    return out[:20]


# ── Client ────────────────────────────────────────────────────────────────────

class ClaudeClient:
    """Wraps the Anthropic SDK and owns session conversation history."""

    def __init__(self, context_builder: ContextBuilder | None = None) -> None:
        self._client = None
        self._init_error: str | None = None
        self.context = context_builder or ContextBuilder()
        # Session memory: list of {"role": ..., "content": ...} dicts.
        self.history: list[dict] = []
        # Sticky for the session: once a question triggers Monarch, keep it
        # attached for follow-ups (e.g. "what about last month?") even if
        # they don't repeat a financial keyword.
        self._monarch_active = False
        # Same stickiness for meal-prep conversations and the web search tool.
        self._meal_active = False
        # The "Hey Jarvis, what on the calendar for today?" gag fires once per
        # session so it stays a surprise rather than replaying every time.
        self._easter_egg_fired = False
        self._init_client()

    def _init_client(self) -> None:
        if not CONFIG.has_anthropic_key:
            self._init_error = (
                "No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file."
            )
            log.error(self._init_error)
            return
        try:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=CONFIG.anthropic_api_key)
            log.info("Anthropic client ready (model=%s)", CONFIG.anthropic_model)
        except ImportError:
            self._init_error = "The 'anthropic' package is not installed."
            log.error(self._init_error)
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"Could not initialize Anthropic client: {exc}"
            log.error(self._init_error)

    @property
    def ready(self) -> bool:
        return self._client is not None

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def reset_session(self) -> None:
        """Clear conversation history (called when overlay reopens)."""
        self.history.clear()
        self._monarch_active = False
        self._meal_active = False
        self._easter_egg_fired = False  # re-arm the gag for the new session
        # Voice-dial tweaks are scoped to a conversation ("...for this convo"),
        # so a fresh session restores the saved defaults.
        from .persona import PERSONA
        PERSONA.reset()
        usage.get_tracker().reset_session()  # logs the session usage/cost total
        log.info("session history cleared")

    def reload_context(self) -> None:
        self.context.reload_static()

    def summarize_session(self, history: list[dict]) -> str:
        """One-shot summary of a conversation history. Returns '' on failure."""
        if not self.ready or not history:
            return ""

        lines = _history_to_lines(history)
        if not lines:
            return ""

        try:
            response = self._client.messages.create(
                model=CONFIG.summary_model,
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize this JARVIS assistant conversation in 3-5 concise "
                        "bullet points. Focus on topics discussed, decisions made, and "
                        "any calendar events or actions taken.\n\n"
                        + "\n".join(lines)
                    ),
                }],
            )
            usage.record(CONFIG.summary_model, getattr(response, "usage", None), kind="summary")
            return response.content[0].text if response.content else ""
        except Exception as exc:  # noqa: BLE001
            log.error("summarize_session failed: %s", exc)
            return ""

    def extract_facts(self, history: list[dict]) -> list[dict]:
        """Extract durable facts as ``[{fact, subject, kind}]``. [] on failure."""
        if not self.ready or not history:
            return []
        lines = _history_to_lines(history)
        if not lines:
            return []
        try:
            response = self._client.messages.create(
                model=CONFIG.summary_model,
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": _FACT_EXTRACTION_PROMPT + "\n\n" + "\n".join(lines),
                }],
            )
            usage.record(CONFIG.summary_model, getattr(response, "usage", None), kind="facts")
            text = response.content[0].text if response.content else ""
            facts = _parse_facts(text)
            if facts:
                log.info("extracted %d durable fact(s) from session", len(facts))
            return facts
        except Exception as exc:  # noqa: BLE001
            log.error("extract_facts failed: %s", exc)
            return []

    def _compact_history(self) -> None:
        """Summarize older turns once history grows long, to bound token cost.

        Delegates to the pure :func:`history.compact_history`, which cuts only at
        turn boundaries so a tool_use/tool_result pairing can never be orphaned.
        """
        self.history = history.compact_history(self.history, self.summarize_session)

    def send(
        self,
        user_message: str,
        on_delta: Callable[[str], None] | None = None,
        on_reset: Callable[[], None] | None = None,
    ) -> str:
        """Send a message with full context; stream deltas via ``on_delta``.

        Handles tool use transparently: if Claude calls a tool (e.g.
        create_calendar_event), the tool executes locally and Claude streams a
        confirmation response. Returns the complete assistant reply. Never
        raises — failures return a readable error string.

        ``on_delta`` receives each streamed text chunk. ``on_reset`` is called
        whenever a round's pre-tool text is discarded (the model spoke, then
        decided to call a tool) so the UI can roll its live stream back to a
        "thinking" state instead of showing text that's about to be replaced.
        """
        new_turn_id()
        if not self.ready:
            return self._init_error or "Claude client is not available."

        log.info("turn start: %r", user_message[:120])
        # Fire the easter egg (if the phrase was said) before the prompt is
        # built, so the boosted dials land in this turn — then restore them once
        # the one snarky reply is done.
        egg_restore = self._maybe_fire_easter_egg(user_message)
        try:
            return self._run_send(user_message, on_delta, on_reset)
        finally:
            if egg_restore:
                egg_restore()

    def _maybe_fire_easter_egg(self, message: str):
        """If the secret phrase was said, max humor+sarcasm and cue Back in Black.

        Returns a restore callable that undoes the dial boost after this one
        reply, or ``None`` when not triggered. Fires at most once per session.
        """
        if (
            self._easter_egg_fired
            or not CONFIG.spotify_available
            or not _is_easter_egg(message)
        ):
            return None
        self._easter_egg_fired = True
        from .persona import PERSONA

        saved = {
            "humor": PERSONA.dials.get("humor"),
            "sarcasm": PERSONA.dials.get("sarcasm"),
        }
        PERSONA.adjust("humor", set_to=100)
        PERSONA.adjust("sarcasm", set_to=100)
        log.info("easter egg: humor+sarcasm -> 100, cueing %s", _EASTER_EGG_SONG)

        def _play() -> None:
            try:
                from integrations import spotify
                spotify.play_track_query(_EASTER_EGG_SONG)
            except Exception as exc:  # noqa: BLE001
                log.error("easter egg playback failed: %s", exc)

        threading.Thread(target=_play, daemon=True).start()

        def _restore() -> None:
            for dial, value in saved.items():
                if value is not None:
                    PERSONA.adjust(dial, set_to=value)
            log.info("easter egg dials restored")

        return _restore

    def _run_send(
        self,
        user_message: str,
        on_delta: Callable[[str], None] | None = None,
        on_reset: Callable[[], None] | None = None,
    ) -> str:
        """Core send: history compaction, prompt build, the bounded tool loop."""
        self._compact_history()
        history_snapshot = len(self.history)
        self.history.append({"role": "user", "content": user_message})
        system_prompt = self.context.build_system_prompt()

        # The Monarch MCP path needs parallel tool use DISABLED (so a local
        # tool_use and an mcp_tool_use never land in the same turn and leave
        # a dangling block); the web-search path needs it ENABLED (the API
        # rejects disable_parallel_tool_use alongside that programmatic
        # server tool). Those requirements are mutually exclusive, so at
        # most one may attach per turn. Both flags stay sticky for the
        # session, so we pick per-turn by the *current* message's topic:
        # finance wins a finance message, meal wins a meal message, and a
        # neutral follow-up favors Monarch's parallel-safe path.
        fin_now = _looks_financial(user_message)
        meal_now = _looks_meal_related(user_message)
        if fin_now:
            self._monarch_active = True
        if meal_now:
            self._meal_active = True

        want_monarch = CONFIG.monarch_enabled and (
            fin_now or (self._monarch_active and not meal_now)
        )
        want_web = meal_now or (self._meal_active and not fin_now)
        if want_monarch and want_web:
            want_web = False  # can't share a turn — keep the MCP-safe path

        # If Monarch's hosted MCP server is unreachable mid-turn the API rejects
        # the whole request; rather than fail the answer we retry once with the
        # MCP server detached, so the user still gets a (finance-less) reply.
        allow_monarch = True
        transient_attempts = 0
        while True:
            base_kwargs: dict = dict(
                model=CONFIG.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=TOOLS,
                # Allow parallel tool use so the model can batch several actions
                # (e.g. one note + six to-dos) into a single round instead of
                # burning one round each and exhausting MAX_TOOL_ROUNDS. Only the
                # Monarch MCP path re-disables this (in _run_turn), where a local
                # tool_use sharing a turn with an mcp_tool_use leaves a dangling,
                # unpaired block.
                tool_choice={"type": "auto"},
            )
            try:
                return self._run_turn(
                    base_kwargs, want_monarch, want_web, allow_monarch,
                    on_delta, on_reset,
                )
            except _MonarchUnavailable as exc:
                log.warning(
                    "Monarch MCP server unreachable; retrying without finance data: %s",
                    exc.__cause__,
                )
                del self.history[history_snapshot + 1:]  # keep the user message
                allow_monarch = False
                continue
            except Exception as exc:  # noqa: BLE001
                # A transient overload/rate-limit before any tool has executed
                # this turn (history still holds only the user message) is safe
                # to retry with backoff — no tool side effect can be duplicated.
                if (
                    _is_transient_api_error(exc)
                    and transient_attempts < _MAX_API_RETRIES
                    and len(self.history) == history_snapshot + 1
                ):
                    transient_attempts += 1
                    delay = _API_RETRY_BACKOFF * (2 ** (transient_attempts - 1))
                    log.warning(
                        "transient Claude API error (attempt %d/%d: %s); retrying in %.1fs",
                        transient_attempts, _MAX_API_RETRIES, exc, delay,
                    )
                    if on_reset:
                        try:
                            on_reset()
                        except Exception:  # noqa: BLE001
                            pass
                    time.sleep(delay)
                    continue
                log.error("Claude API call failed: %s", exc)
                # Roll back the entire failed turn (the user message plus any
                # partial assistant/tool_result exchanges already appended this
                # round) so a dangling tool_use can never linger into the next
                # request — truncating to the pre-turn length handles that
                # regardless of which round the failure happened in.
                del self.history[history_snapshot:]
                return f"⚠️ Error contacting Claude: {exc}"

    def _run_turn(
        self,
        base_kwargs: dict,
        want_monarch: bool,
        want_web: bool,
        allow_monarch: bool,
        on_delta: Callable[[str], None] | None,
        on_reset: Callable[[], None] | None,
    ) -> str:
        """Run one full tool-use loop and return the assistant's reply.

        Attaches the Monarch MCP server (or web search) per the want_* flags.
        Raises ``_MonarchUnavailable`` if the MCP server can't be reached so the
        caller can retry without it; any other failure propagates for the caller
        to roll back and surface.
        """
        full_text = ""
        last_pretext = ""  # most recent pre-tool narration, kept as a fallback
        stream_fn = self._client.messages.stream
        monarch_attached = False
        if want_monarch and allow_monarch:
            try:
                from integrations.monarch_oauth import get_monarch_token
                base_kwargs["mcp_servers"] = [{
                    "type": "url",
                    "name": "monarch",
                    "url": _MONARCH_MCP_URL,
                    "authorization_token": get_monarch_token(),
                }]
                base_kwargs["betas"] = [_MCP_BETA]
                # Keep local and MCP tool calls on separate rounds so a local
                # tool_use never shares a turn with an mcp_tool_use (which would
                # leave a dangling, unpaired block).
                base_kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
                stream_fn = self._client.beta.messages.stream
                monarch_attached = True
                log.info("using beta endpoint with monarch mcp_servers attached")
            except Exception as exc:
                log.error("Monarch token error, falling back to plain endpoint: %s", exc, exc_info=True)
        elif want_web:
            base_kwargs["tools"] = TOOLS + [_WEB_SEARCH_TOOL]
            base_kwargs["tool_choice"] = {"type": "auto"}
            base_kwargs["max_tokens"] = _MEAL_MAX_TOKENS
            log.info("web search tool attached (meal-related conversation)")

        try:
            # ── Bounded tool-use loop: keep tools/mcp_servers attached on every
            # round so local tools (e.g. load_knowledge_pool) and the Monarch
            # MCP tool can both be used for one question, just sequentially.
            for round_num in range(1, MAX_TOOL_ROUNDS + 1):
                messages = history.with_message_cache_breakpoint(self.history)
                with stream_fn(messages=messages, **base_kwargs) as stream:
                    for text in stream.text_stream:
                        full_text += text
                        if on_delta:
                            on_delta(text)
                    final_msg = stream.get_final_message()

                usage.record(CONFIG.anthropic_model, getattr(final_msg, "usage", None), kind="turn")
                log.info(
                    "round %d content block types: %s",
                    round_num, [b.type for b in final_msg.content],
                )

                tool_uses = [b for b in final_msg.content if b.type == "tool_use"]

                # If the output cap was hit *while emitting a tool_use*, the
                # tool's JSON arguments are truncated and would fail to execute
                # (e.g. a long create_note loses its 'content'). Discard this
                # half-formed attempt and re-run the round with a bigger budget,
                # rather than running a broken call and looping on the error.
                if (
                    tool_uses
                    and final_msg.stop_reason == "max_tokens"
                    and base_kwargs.get("max_tokens", MAX_TOKENS) < _MAX_TOKENS_CEILING
                ):
                    log.warning(
                        "round %d truncated mid tool_use (max_tokens); retrying with %d-token budget",
                        round_num, _MAX_TOKENS_CEILING,
                    )
                    base_kwargs["max_tokens"] = _MAX_TOKENS_CEILING
                    if full_text and on_reset:
                        on_reset()
                    full_text = ""
                    continue

                self.history.append({
                    "role": "assistant",
                    "content": [history.block_to_dict(b) for b in final_msg.content],
                })

                if not tool_uses:
                    break

                # Discard any pre-tool text streamed this round — the real
                # answer comes after the tool result, not before it. Tell the UI
                # to roll its live stream back to "thinking" so the discarded
                # text doesn't linger on screen until the next round renders.
                # Keep the most recent narration, though: if we later exhaust the
                # round budget and the forced wrap-up comes back empty, it's the
                # best summary we have of what just happened.
                if full_text.strip():
                    last_pretext = full_text
                if full_text and on_reset:
                    on_reset()
                full_text = ""

                results = []
                for tu in tool_uses:
                    is_error = False
                    try:
                        outcome = execute_tool(tu.name, tu.input)
                        # Integrations report handled failures as "Error: ..."
                        # strings — flag those so the model treats them as
                        # failures to recover from, not as data.
                        is_error = outcome.lstrip().startswith("Error")
                    except Exception as exc:  # noqa: BLE001
                        # A tool_use block always needs a paired tool_result —
                        # if execution raises (e.g. a required field was missing
                        # from a truncated/malformed call), report it as the
                        # result instead of letting it escape and leave a
                        # dangling tool_use in history for the next request.
                        log.error("tool %s raised: %s", tu.name, exc)
                        outcome = (
                            f"Error: tool '{tu.name}' failed ({exc}). "
                            "Check that all required fields were provided and try again."
                        )
                        is_error = True
                    log.info("tool %s -> %r", tu.name, outcome[:120])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": outcome,
                        "is_error": is_error,
                    })
                self.history.append({"role": "user", "content": results})
            else:
                log.warning("hit max tool rounds (%d); forcing a final answer", MAX_TOOL_ROUNDS)
                try:
                    forced_kwargs = dict(base_kwargs)
                    forced_kwargs["tool_choice"] = {"type": "none"}
                    full_text = ""
                    messages = history.with_message_cache_breakpoint(self.history)
                    with stream_fn(messages=messages, **forced_kwargs) as stream:
                        for text in stream.text_stream:
                            full_text += text
                            if on_delta:
                                on_delta(text)
                        final_msg = stream.get_final_message()
                    usage.record(CONFIG.anthropic_model, getattr(final_msg, "usage", None), kind="turn")
                    self.history.append({
                        "role": "assistant",
                        "content": [history.block_to_dict(b) for b in final_msg.content],
                    })
                    log.info("forced final response after exhausting tool rounds (%d chars)", len(full_text))
                except Exception as exc:  # noqa: BLE001
                    log.error("forced final response failed: %s", exc)
                    full_text = ""
                # The model often goes quiet after a long run of actions (it
                # already "narrated" in pre-tool text we discarded each round).
                # Fall back to that last narration, or a generic confirmation,
                # so the user never gets an empty reply.
                if not full_text.strip():
                    full_text = last_pretext.strip() or (
                        "Done — I've completed those actions. Check your "
                        "calendar, notes, and to-dos to confirm."
                    )
                    if on_delta:
                        on_delta(full_text)

            log.info("response received (%d chars)", len(full_text))
            return full_text

        except Exception as exc:  # noqa: BLE001
            # A transient Monarch MCP outage is recoverable — signal the caller
            # to retry without it. Everything else propagates for the caller to
            # roll back the turn and surface a readable error.
            if monarch_attached and _is_mcp_connection_error(exc):
                raise _MonarchUnavailable from exc
            raise
