# JARVIS — Roadmap to the Ultimate AI Assistant

_A codebase-grounded analysis and phased plan. Every recommendation references
the real file (and where useful, the line range) it touches, so this document is
executable, not aspirational._

> **Status:** Phases 1–4 are implemented and merged (tool registry, tests + CI,
> config-driven categories, SQLite/FTS5 memory superseded by the Obsidian
> vault, and proactive scheduling). §2.2 below is kept as a historical record
> of the gaps that motivated those phases — see the strikethroughs. Phases 5–6
> and the cross-cutting items remain open.

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

1. ~~**No tests, no CI.**~~ **Addressed (Phase 1).** `pytest` + `ruff` run in CI
   (`.github/workflows/ci.yml`); the tool loop, history compaction, persona,
   and every integration's pure helpers have unit coverage under `tests/`.
2. ~~**Monolith + 3-place tool edits.**~~ **Addressed (Phase 1).** Tools are
   declared once in `app/tool_registry.py` and dispatched by name;
   `claude_client.py` only orchestrates streaming, the bounded loop, and
   history.
3. ~~**Hardcoded personal categories.**~~ **Addressed (Phase 2).** Categories
   are config-driven with an in-app settings panel to edit them, rather than
   baked into code.
4. ~~**Shallow memory.**~~ **Addressed.** Long-term memory is a relevance-ranked
   SQLite/FTS5 store (Phase 3), and is now superseded by an optional **Obsidian
   vault "second brain"** (`integrations/obsidian.py` + `app/vault_index.py`):
   one linked-markdown home for notes *and* memory that JARVIS reads/writes and
   the user can browse/edit in Obsidian. The SQLite store remains as the no-vault
   fallback.
5. ~~**Purely reactive.**~~ **Addressed (Phase 4).** `app/proactive.py` runs a
   background scheduler for the daily briefing, meeting alerts, and
   important-email pings, with quiet hours and dedup.
6. ~~**Push-to-talk only.**~~ **Addressed (Phase 5).** The overlay streams
   text live via `claude_client.send`'s `on_delta`/`on_reset` callbacks
   (`app/overlay.py:_submit`, `assets/ui/app.js`), speech now streams
   sentence-by-sentence too (`app/tts.py`'s `start_utterance`/`feed`/
   `finish`), and "Hey JARVIS" hands-free activation is live via
   `app/wakeword.py` (openWakeWord, off by default —
   `JARVIS_WAKE_WORD_ENABLED`).
7. ~~**Duplicated retry/backoff**~~ **Addressed (Phase 1).** `todoist.py`,
   `spotify.py`, and `google_api.py` share one helper in `integrations/_http.py`.
8. **Dependency hygiene — partially addressed.** `requirements.txt` now has
   upper bounds on the three riskiest pins (`anthropic`, `pywebview`, `numpy`)
   and `pyproject.toml` configures `ruff`/`pytest`, but there is still no full
   lockfile and `OUTLOOK_CLIENT_SECRET` still lives in plaintext `.env` (prefer
   device-code/PKCE like Spotify already uses). Both remain open.
9. **Thin observability — partially addressed.** Every log line now carries a
   per-turn correlation id (`app/logging_setup.py:new_turn_id`, set once per
   `ClaudeClient.send`), so `grep '[a1b2c3d4]' logs/jarvis.log` traces one
   question across all the tool calls it made. Still no metrics
   (tool latency, error rate) — that part remains open.

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

### Phase 3 — Long-term memory _(marquee feature — ✅ delivered)_

**Mechanism:** an optional **Obsidian vault** (direct filesystem access) is the
durable store — a single, human-readable, inter-linked markdown home for both
notes and memory, kept searchable by a local **FTS5 index** over the vault files
(`app/vault_index.py`), with the original SQLite store (`app/memory.py`) retained
as the no-vault fallback. Session summaries become `Sessions/` notes and extracted
facts append to `Memory/Facts.md`; the model reads/writes via the `search_vault` /
`read_note` / `write_note` / `append_note` / `list_notes` tools.

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

### Phase 4 — Proactivity & automation _(marquee feature — ✅ delivered)_

**Goal:** JARVIS acts without being asked.

- ✅ A background scheduler (`app/proactive.py`'s `ProactiveScheduler`,
  wired in `main.py`). Jobs:
  - ✅ **Scheduled morning briefing** at a configured time.
  - ✅ **Meeting alerts** — poll calendar for "starts in N min."
  - ✅ **Important-email pings** — poll Gmail for high-signal unread mail.
  - ✅ **Vault callbacks** (bonus, beyond the original scope) — nudge once
    when a `Sessions/` note's Action Items/Open Questions go stale, purely
    local/no API calls, dedup stamped in the note's own frontmatter.
- ✅ Surface via tray balloon (`app/tray.py`) + optional TTS; honors **quiet
  hours** and dedups so it never nags.
- **DoD:** briefing fires on schedule; a meeting alert, an important-email
  ping, and a stale-vault-item nudge all appear as notifications; quiet
  hours suppress the interrupting ones.

### Phase 5 — Wake-word voice _(marquee feature — ✅ delivered)_

**Goal:** a true hands-free voice loop.

- ✅ **Live token streaming** — `claude_client.send(..., on_delta=...)` now
  drives the overlay live (`app/overlay.py:_submit`,
  `assets/ui/app.js`'s `appendAssistantDelta`/`resetAssistantStream`) instead
  of being discarded.
- ✅ **Sentence-streamed speech** — `app/tts.py`'s `Speaker.start_utterance`/
  `feed`/`finish` speak each sentence as soon as it's complete instead of
  waiting for the full reply, via `split_ready_sentences()` (a conservative
  boundary detector — abbreviations/decimals don't misfire).
- ✅ **Always-listening wake word** ("Hey JARVIS") via `app/wakeword.py`
  using openWakeWord (local, no account/API key — matches the existing
  `faster-whisper` choice), feeding the existing `app/recorder.py` →
  `app/transcriber.py` pipeline through `overlay._start_recording`. A
  silence-timeout watchdog (`watch_for_silence`) auto-stops the recording
  since there's no button release to signal "done talking." Off by default
  (`JARVIS_WAKE_WORD_ENABLED`).
- ✅ **Barge-in** — `speaker.stop()` in `overlay._submit`/`_start_recording`
  now also drains the sentence-streaming queue, and `_start_recording`/
  `_stop_recording` pause/resume the wake-word listener automatically so its
  mic stream and push-to-talk's are never open at once.
- **DoD:** speaking the wake word starts a conversation hands-free; replies
  stream as audio + text; speaking over JARVIS interrupts cleanly. ⚠️ Built
  and unit-tested on the pure logic, but sounddevice needs a native
  PortAudio lib not present in the build/CI sandbox, so the actual
  hands-free feel and wake-word accuracy need to be confirmed on real
  hardware — treat the threshold/timeout defaults as starting points to tune.

### Phase 6 — Reach (future / optional)

- **Mobile / remote access to the same assistant** (e.g. JARVIS on a Google
  Pixel) — see [§7 Portability & the path to mobile](#7-portability--the-path-to-mobile-pixel--android)
  for the full analysis, blockers, and the client–server design that gets there.
- Additional integrations (Slack, smart home).
- ~~richer multi-modal I/O (images, screen context)~~ **Addressed.** A
  drag-to-select screenshot capture (`app/screenshot.py`, the selector window
  in `assets/ui/selector.html`) attaches to the next message as a vision
  content block — `claude_client.send(image_b64=...)`. Verified the image+text
  content-block shape flows correctly through `history.py`'s existing
  cache-breakpoint/compaction logic (already handles list content for
  assistant/tool_result messages) with zero changes needed there.
- Sequenced last because each is large and none blocks the others.

### Cross-cutting (alongside any phase)

- **Observability:** a per-request correlation ID threaded through logs so one
  question's tool calls can be traced; a few counters (tool latency, error
  rate).
- **Security:** migrate `OUTLOOK_CLIENT_SECRET` (`app/config.py:208-210`) to a
  device-code/PKCE public-client flow, matching Spotify's secret-free pattern.
- **Portability rule (keeps mobile cheap):** keep all UI/OS-specific code inside
  `app/overlay.py` + `app/tray.py`; the core (`claude_client`, `context_builder`,
  `tool_registry`, `memory`, `vault_index`, `integrations/*`) must stay
  import-clean of the shell, and vault IO must stay behind
  `obsidian.vault_root()` / `_safe_path()`. Following this one rule today is what
  makes §7 a port, not a rewrite. (Already true as of the vault work — verified:
  no core module imports `pywebview`/`pystray`/`ctypes`.)

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

---

## 7. Portability & the path to mobile (Pixel / Android)

_Goal: one day run JARVIS on a Google Pixel. This section records what's portable,
what isn't, and the design that gets there — so future decisions don't quietly
paint mobile into a corner._

### 7.1 Verdict

**Nothing about the current setup is a hard blocker for mobile, and the Obsidian
vault is one of the more mobile-friendly choices in the codebase.** The real
hurdles are the Windows UI/voice shell — and they are already isolated. The single
most important fact: every Windows/UI import (`pywebview`, `pystray`,
`ctypes`/`wintypes`, `pythonnet`) lives in **only two files** —
`app/overlay.py` and `app/tray.py`. The brain imports none of them.

### 7.2 What's portable vs. not

| Layer | Mobile status | Where |
| --- | --- | --- |
| Claude brain, tools, history, persona | ✅ Pure Python — portable | `app/claude_client.py`, `context_builder.py`, `tool_registry.py`, `persona.py`, `history.py` |
| Long-term memory + vault search | ✅ `sqlite3`/FTS5 is native on Android | `app/memory.py`, `app/vault_index.py` |
| Obsidian vault store | ✅ Markdown folder; Obsidian has a native Android app | `integrations/obsidian.py` |
| Cloud integrations | ✅ REST/OAuth over HTTP — portable | `integrations/google_*`, `outlook_*`, `gmail`, `todoist`, `spotify`, `weather`, `monarch_oauth` |
| Overlay UI | ❌ pywebview + Win32 layered window | `app/overlay.py` |
| Tray | ❌ no system tray on Android | `app/tray.py` |
| Local voice (STT) | ⚠️ `faster-whisper`/`sounddevice` are heavy native deps — use the phone mic + cloud/on-device STT | `app/recorder.py`, `app/transcriber.py` |
| TTS | ⚠️ `edge`/`elevenlabs` are HTTP (portable); `pyttsx3` is desktop-only | `app/tts.py` |
| Global hotkey, desktop-browser OAuth, tray toasts | ⚠️ Different primitives on Android (Quick Settings/WorkManager/Custom Tabs/notifications) | `keyboard`, `proactive.py` |

### 7.3 The Obsidian vault on Android — specifics

The vault is portable, with two contained adaptations:

- **Filesystem access.** Desktop Python points `Path` at any folder; Android 11+
  *scoped storage* forbids that. But all vault IO already funnels through one
  chokepoint (`obsidian.vault_root()` / `_safe_path()`), so the storage backend
  (Storage Access Framework, a synced folder, or a remote backend) is a swap in
  one place — not a rewrite. **Keep it that way** (portability rule, §Cross-cutting).
- **File watching.** `watchdog`/inotify may not fire on Android shared storage —
  but `ObsidianWatcher` is a *freshness optimization, not correctness*: every
  write upserts the index and startup runs a full `reindex()`/`sync()`, so search
  stays correct with the watcher disabled. It already degrades gracefully.
- **Format is already cross-platform.** Obsidian's own Android app reads/writes
  the same vault, so the phone and desktop can share one brain via any sync
  (Obsidian Sync, Syncthing, Git, or — preferably — a JARVIS backend).

### 7.4 Recommended architecture: phone-as-client to a JARVIS core

The cleanest mobile build is **not** "Python on the phone." It's a **client–server
split**: the brain + vault + API key + OAuth tokens live in one trusted place (a
home PC or a small always-on box), and the Pixel runs a thin chat/voice client.
This sidesteps Android Python packaging *and* scoped storage at once, and keeps
secrets off the device.

Enabling step (do when mobile work actually starts, not before): extract a
**UI-agnostic core facade** — `JarvisCore` / `AssistantSession` — exposing
`send_message(...)`, tool dispatch, and session lifecycle, then expose it over a
small local API (FastAPI + websocket for streaming). `app/overlay.py` becomes just
*one* client of that facade; the Android app is another.

- **We're ~90% there:** the brain has no UI imports. The only remaining coupling
  is that `overlay.py` instantiates and drives `ClaudeClient` directly and owns
  session save/close.
- **Alternative (heavier):** fully on-device via Kivy/BeeWare — possible, but
  you'd drop `faster-whisper`/`pywebview`/`pystray` and fight scoped storage.
  Documented only as a fallback; the client–server path is preferred.

### 7.5 Definition of done (when we pursue it)

- A `JarvisCore` facade with no import of `overlay`/`tray`; the Windows overlay
  rebuilt as a thin client of it (no behavior change on desktop).
- A small authenticated local API streaming replies and dispatching tools.
- A Pixel client (native or web) that chats, captures voice, and reaches the
  shared vault/brain through the backend — no secrets stored on the phone.
