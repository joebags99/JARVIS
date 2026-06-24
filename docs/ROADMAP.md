# JARVIS — Roadmap to the Ultimate AI Assistant

_A codebase-grounded analysis and phased plan. Every recommendation references
the real file (and where useful, the line range) it touches, so this document is
executable, not aspirational._

> **Status:** planning document. No source code has been changed by this review.
> Implementation happens in later sessions, phase by phase, after review.

---

## 1. Executive summary

JARVIS is a **mature, thoughtfully-built personal assistant** — far past
prototype quality. The Claude integration in `app/claude_client.py` does things
most hobby assistants never get right: prompt caching with a stable/volatile
split, history compaction with safe turn-boundary cuts, conditional attachment
of the Monarch MCP server and native web search, a bounded tool-use loop with
mid-`tool_use` truncation recovery, and graceful degradation everywhere.

Two things hold it back from "ultimate":

1. **Reliability scaffolding is absent.** There are **no tests and no CI** on a
   **1,686-line brain**, and adding a single tool requires edits in three
   places. This makes every future change riskier than it should be.
2. **It is purely reactive and its memory is shallow.** JARVIS only acts when
   spoken to, and "memory" is the last three flat summary files. The leap to
   *ultimate* is becoming **proactive** and **durably remembering**.

**Strategy:** invest in the foundation first (the chosen north star), because a
clean tool registry + a test harness is what makes proactivity, long-term
memory, and a wake-word voice loop safe and cheap to build afterward.

---

## 2. Current-state analysis

### 2.1 What's already excellent (keep and build on)

| Strength | Where |
| --- | --- |
| Prompt caching via a 2-block (stable cached + tiny volatile) system prompt | `app/context_builder.py:119-178` |
| Per-message cache breakpoint for multi-turn/multi-round caching | `app/claude_client.py:1139-1175` |
| History compaction at safe turn boundaries + stale tool-result trimming | `app/claude_client.py:1304-1360` |
| Conditional MCP (Monarch) + native web-search attachment by topic | `app/claude_client.py:1445-1552` |
| Bounded tool loop with mid-`tool_use` max-token recovery & forced wrap-up | `app/claude_client.py:1558-1676` |
| Live persona "voice dials" with no LLM round-trip | `app/persona.py`, `app/overlay.py:124-146` |
| Graceful degradation (missing creds/mic/STT = feature off, app runs) | `main.py:30-42`, integrations throughout |
| Privacy-first: all personal data gitignored | `.gitignore`, `context/`, `notes/`, `tokens/` |
| Retry/backoff on transient API failures | `integrations/todoist.py`, `spotify.py`, `google_api.py` |
| Strong user-facing docs | `README.md` |

### 2.2 Gaps and risks (prioritized)

1. **No tests, no CI — highest risk.** Any refactor of the tool loop or history
   logic can break silently until it crashes mid-conversation. Nothing guards
   `app/claude_client.py`.
2. **Monolith + 3-place tool edits.** Adding one tool means touching the `TOOLS`
   list (`claude_client.py:92-733`), the `_execute_tool` if/elif chain
   (`:913-1119`), and config gating (`:815-896`). This is the single biggest
   drag on future feature velocity.
3. **Hardcoded personal categories.** `Daedabyte / General / Brightpoint / DnD`
   are baked into tool enums (`claude_client.py`), directory creation
   (`app/config.py:37-38`), and `integrations/notes_watcher.py`. Blocks reuse by
   anyone else and is brittle to change.
4. **Shallow memory.** `recall_session_history` returns the *most recent 3* flat
   markdown summaries (`claude_client.py:965-977`; written by
   `overlay.py:362-378`) with no relevance ranking, no durable facts/preferences
   store, and no transcript persistence. Conversation history is in-memory only
   and lost on close.
5. **Purely reactive.** The daily briefing is a manual tray click
   (`app/overlay.py:290-303`). There is no scheduler, no reminders, no
   background monitoring of calendar or email.
6. **Push-to-talk only.** No wake word; the overlay also *deliberately* disables
   live streaming and reveals the full reply at once (`overlay.py:396-407`) even
   though `claude_client.send` already supports an `on_delta` stream callback.
7. **Duplicated retry/backoff** across `integrations/todoist.py`, `spotify.py`,
   and `google_api.py`.
8. **Dependency hygiene.** All `requirements.txt` pins are `>=` with no upper
   bounds and no lockfile; `OUTLOOK_CLIENT_SECRET` lives in plaintext `.env`
   (prefer device-code/PKCE like Spotify already uses).
9. **Thin observability.** File logging only (`app/logging_setup.py`); no
   correlation IDs to trace one question across its tool calls, no metrics.

---

## 3. Phased roadmap

Each phase has a **definition of done (DoD)**. Phase 1 is the prerequisite for
the rest; Phases 3–5 are independent and can be reordered freely on top of it.

### Phase 1 — Foundation (north star; do first)

**Goal:** make the codebase safe and cheap to change.

1. **Test harness — `pytest` + `ruff`.** Start with pure logic that needs no
   network, where bugs are most damaging:
   - `app/persona.py` — `adjust` (set/change/clamp/reset), band selection.
   - `app/name_corrector.py` — normalization + fuzzy cutoff behavior.
   - `app/context_builder.py` — section assembly, `_truncate` cap behavior,
     stable/volatile split shape.
   - `app/claude_client.py` (pure helpers) — `_looks_financial`,
     `_looks_meal_related`, `_is_easter_egg`, `_with_message_cache_breakpoint`,
     `_compact_history` / `_trim_old_tool_results` boundary math, `_block_to_dict`.
   - `integrations/todoist.py` — due-date resolution (`parsedatetime`).
   - Mock the Anthropic SDK and HTTP so the tool loop can be tested without a key.
   - **DoD:** `pytest` green locally and in CI; the tool-loop happy path +
     max-rounds + mid-`tool_use` truncation recovery are covered with mocks.

2. **Tool-registry refactor.** Replace the `TOOLS` list + `_execute_tool` chain
   with a registry. Each tool is declared once:
   ```python
   @tool(name="get_weather", schema=..., available=lambda: True)
   def get_weather(location=None, days=1): ...
   ```
   The registry produces the `tools` array for the API and dispatches by name.
   `claude_client.py` keeps only orchestration (streaming, the bounded loop,
   history). Split the file into: `tools/` (definitions + registry), `dispatch`,
   `history` (compaction/trim), and the slimmed `ClaudeClient`.
   - **DoD:** adding a tool is a one-place change; the assembled tool list and
     dispatch behavior are byte-for-byte equivalent to today (snapshot test).

3. **Shared HTTP utility — `integrations/_http.py`.** One retry/backoff/timeout
   helper; refactor `todoist.py`, `spotify.py`, `google_api.py` onto it.
   - **DoD:** the three modules call one helper; backoff/timeout covered by tests.

4. **CI — GitHub Actions.** Run `ruff` + `pytest` on push/PR. Use the
   `session-start-hook` skill so Claude Code web sessions can run lint/tests too.
   - **DoD:** a green check on the feature branch.

5. **Dependency hygiene.** Add `pyproject.toml`, generate a lockfile, and add
   upper bounds to the riskiest pins (`pywebview`, `anthropic`, `numpy`).
   - **DoD:** a clean-room install reproduces a known-good environment.

6. **Startup self-check.** On launch, log a one-line readiness table (which
   integrations are live vs. misconfigured), surfaced via `main.py` + a
   `CONFIG.diagnostics()` helper building on the existing `*_available` props
   (`app/config.py:219-258`).
   - **DoD:** first log lines show per-integration status.

### Phase 2 — User-configurable setup

**Goal:** de-personalize so JARVIS is reusable and categories aren't code.

- Move categories out of code into config (`jarvis_config.json` or new `.env`
  keys). Generate the Todoist/notes tool enums and `notes/` subfolders
  dynamically from it — replaces the hardcoded tuple at `app/config.py:37-38`,
  the enum literals in the registry tools, and the list in
  `integrations/notes_watcher.py`.
- Add an **in-app settings panel** in the overlay (toggle integrations, edit
  categories, location, hotkey) reusing the existing dials JS-API pattern
  (`app/overlay.py:124-146`, `assets/ui/app.js`).
- **DoD:** a fresh user configures categories/integrations without editing
  Python; dial panel and settings panel share one UI pattern.

### Phase 3 — Long-term memory _(marquee feature)_

**Goal:** durable, relevance-ranked memory instead of "last 3 files."

- Introduce a durable store — **SQLite** for structured records plus an
  embedding index for **semantic recall**. Records: extracted facts/preferences,
  session summaries, and (optionally) full transcripts.
- Replace the "last 3" behavior of `recall_session_history`
  (`claude_client.py:965-977`) with relevance-ranked retrieval against the
  current question. Add `remember` / `recall` tools via the Phase-1 registry.
- Auto-extract durable facts at session close, extending the existing summary
  pipeline (`overlay._save_session_summary` → `app/overlay.py:362-378`, and
  `integrations/notes_watcher.py`). Keep everything local and gitignored to
  preserve the privacy posture.
- **DoD:** "what did we decide about X weeks ago" retrieves the right session by
  relevance; stable user facts persist across sessions and inform replies.

### Phase 4 — Proactivity & automation _(marquee feature)_

**Goal:** JARVIS acts without being asked.

- Add a background scheduler (a daemon thread, or `APScheduler`) wired in
  `main.py` alongside the notes watcher. Jobs:
  - **Scheduled morning briefing** at a configured time — reuse
    `overlay.daily_briefing` (`app/overlay.py:290-303`).
  - **Meeting alerts** — poll calendar for "leave now" / "starts in 15 min."
  - **Important-email pings** — poll Gmail for high-signal unread mail.
- Surface via **Windows toast notifications** + tray state
  (`app/tray.py`, `app/icon.py`) and optional TTS; honor **quiet hours** and
  rate-limit so it never nags.
- **DoD:** briefing fires on schedule; a meeting alert and an important-email
  ping appear as toasts; quiet hours suppress them.

### Phase 5 — Wake-word voice _(marquee feature)_

**Goal:** a true hands-free voice loop.

- Always-listening **wake word** ("Hey JARVIS") via a local engine
  (openWakeWord or Porcupine) plus continuous VAD, feeding the existing
  `app/recorder.py` → `app/transcriber.py` pipeline.
- Flip the overlay to **live token streaming** — the path already exists
  (`claude_client.send(..., on_delta=...)`); `overlay._submit`
  (`app/overlay.py:396-407`) currently discards it by design, so make streaming
  a mode toggle.
- Full **barge-in** (already partly handled by `speaker.stop()` in
  `overlay._submit` / `_start_recording`).
- **DoD:** speaking the wake word starts a conversation hands-free; replies
  stream as audio + text; speaking over JARVIS interrupts cleanly.

### Phase 6 — Reach (future / optional)

- Mobile or remote access to the same assistant; additional integrations
  (Slack, smart home); richer multi-modal I/O (images, screen context).
- Sequenced last because each is large and none blocks the others.

### Cross-cutting (alongside any phase)

- **Observability:** a per-request correlation ID threaded through logs so one
  question's tool calls can be traced; a few counters (tool latency, error
  rate).
- **Security:** migrate `OUTLOOK_CLIENT_SECRET` (`app/config.py:208-210`) to a
  device-code/PKCE public-client flow, matching Spotify's secret-free pattern.

---

## 4. Sequencing & dependencies

```
Phase 1 (Foundation) ──┬─> Phase 2 (Configurable)
                       ├─> Phase 3 (Memory)      ┐
                       ├─> Phase 4 (Proactivity)  ├─ independent; any order
                       └─> Phase 5 (Wake-word)   ┘
                                   └─> Phase 6 (Reach)
```

- **Phase 1 unblocks everything:** the registry makes Phases 3–5's new tools a
  one-line add; the test harness makes the refactors safe.
- **Phase 2 should precede broad sharing** so categories/integrations aren't
  hardcoded.
- **Phases 3–5 are independent** and can be prioritized by appetite.

---

## 5. Quick wins (low effort, high signal — can slot into Phase 1)

- Consolidate retry/backoff into `integrations/_http.py` (removes real duplication).
- Add the startup self-check table (instant operability win).
- Pin the three riskiest dependencies with upper bounds.
- Add `ruff` + a minimal `pytest` smoke test and a CI check.

---

## 6. Definition of "ultimate" (how we'll know we got there)

- **Safe to evolve:** green CI, meaningful test coverage on the brain, new tools
  added in one place.
- **Reusable:** no personal data or categories in code.
- **Remembers:** durable, relevance-ranked memory of facts and past sessions.
- **Anticipates:** scheduled briefings and timely, quiet-hours-aware alerts.
- **Conversational, hands-free:** wake-word activation with streaming voice I/O.
- **Trustworthy & private:** local-first data, secret-free OAuth, traceable logs.
