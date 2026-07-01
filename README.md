# JARVIS — Personal AI Assistant

A Windows desktop overlay assistant powered by the Anthropic Claude API. JARVIS
lives in your system tray; click it to open a sleek, always-on-top overlay,
then **type or speak** a question. It answers with awareness of your profile,
calendars, and meeting notes.

> **Your data stays yours.** Everything personal — your `.env` secrets, OAuth
> tokens, `context/` profile, and `notes/` — is **gitignored** and never pushed
> to GitHub. See [Privacy](#privacy) below.

---

## Features

- **System tray** icon (idle / listening / thinking states) with Open, Daily
  Briefing, Reload Context, Settings, and Quit.
- **Floating overlay** — frameless, always-on-top, draggable, dark theme
  (`#0f0f0f` + cyan `#00bcd4`). Closes on `Esc` or click-away.
- **Type or talk** — push-to-talk voice via local `faster-whisper` (no audio
  ever leaves your machine; transcription is free and offline).
- **Wake word (optional)** — say "Hey JARVIS" for hands-free activation, on top
  of (not instead of) push-to-talk. Fully local via
  [openWakeWord](https://github.com/dscripka/openWakeWord), no account needed;
  off by default (`JARVIS_WAKE_WORD_ENABLED`).
- **Talk back (optional)** — JARVIS can read replies aloud, off by default and
  toggled live with the speaker button or the tray. Speech starts sentence by
  sentence as the reply streams in, not after the whole answer arrives. Pick
  your engine: free neural `edge-tts`, fully-offline `pyttsx3`, or premium
  ElevenLabs.
- **Vision (optional)** — drag-to-select any part of your screen (camera
  button, or a global hotkey) and ask about it; the capture rides along with
  your next message as an image Claude can see.
- **Name corrections** — a glossary of your fantasy/proper names fixes Whisper's
  (and your typos') misspellings, and biases transcription toward the right
  spelling. "Cailynn" → "Kailin" everywhere it matters.
- **Personality & voice** — a dedicated `context/persona.md` doc defines exactly
  how JARVIS talks (movie-accurate, "Sir", short and dry by default), plus five
  live **voice dials** — brevity, formality, humor, sarcasm, proactivity — you
  can nudge mid-conversation: *"turn humor down 15%"*, *"max formality"*,
  *"reset your personality"*. Tweaks last the session; ask him to remember one
  and it sticks.
- **Context-aware** — assembles a system prompt from your `context/*.md` files,
  Google + Outlook calendars (next 7 days), recent `notes/`, and the date/time.
- **Meal prep** — plan dinners two weeks at a time in conversation (with real
  web search for recipe ideas), then push the plan to your Google Calendar and
  a Todoist shopping list in one go.
- **Daily Briefing** — one click from the tray (or just ask): today's calendar,
  overdue/today to-dos, the weather, and notable unread email in one summary.
- **Weather** — current conditions and today's forecast for any city via
  Open-Meteo (no API key needed); set `JARVIS_LOCATION` for a default.
- **Email** (optional) — read and summarize recent Gmail, and draft replies for
  you to review. JARVIS never sends mail on its own; it only saves drafts.
- **Music** (optional) — control Spotify by voice or chat: play a song, artist,
  album, or playlist, pause/skip, shuffle, set volume, and ask what's playing.
  Requires Spotify Premium. (There's also a hidden incantation… 🎸)
- **Knowledge vault — a second brain (optional)** — point JARVIS at an
  [Obsidian](https://obsidian.md) vault and it becomes a single, linked markdown
  home for both your notes **and** its long-term memory: it searches the vault
  before answering, writes new notes (with frontmatter, `[[wikilinks]]`, and
  `#tags`), and records session recaps + durable facts there for you to browse
  and edit in Obsidian. Notes are organized automatically — people, companies, and
  projects each get their own canonical note, meetings always land in `Sessions/`,
  and every note follows a per-type template. A `Maps/Dashboard.md` (note counts,
  most-linked notes, recent activity) and a `Vault Overview.canvas` (people/
  companies/projects laid out and linked) refresh automatically — see
  `python -m app.vault_cli stats` / `canvas`. Without a vault, it falls back to a
  local notes folder + recall store.
- **Cross-session memory** — when you close a longer chat, JARVIS saves a short
  recap and any durable facts, then recalls them later ("pick up where we left
  off"). With a vault, the recap is auto-**wikilinked** to the people/projects it
  mentions, and each fact is filed under the note for the person/project it's
  **about** (e.g. an allergy lands on that person's note) — facts about you go to
  `Memory/Facts.md`. Name variants resolve to the canonical note, so a nickname
  never creates a duplicate. Without a vault, it falls back to a local SQLite store.
- **Proactive vault callbacks (optional)** — if a `Sessions/` note's Action
  Items/Open Questions sit untouched for a few days, JARVIS nudges you once
  (never repeats itself for that note). Purely local, no API calls
  (`JARVIS_VAULT_CALLBACKS_ENABLED`).
- **Smooth, streamed replies** — text appears as JARVIS composes it, not after
  the full answer arrives; a thinking indicator covers any gap before the
  first token.
- **Token & cache diagnostics** — every API call's token usage (including
  prompt-cache hits) is logged with an estimated cost, so you can see what you're
  spending and how well caching is working (`python -m app.usage_report`).
- **Graceful degradation** — missing mic, missing calendar creds, or a missing
  API key are handled with clear messages, never a crash.

---

## Requirements

- **Python 3.11+**
- Windows 10/11 (the overlay is built for Windows; it will *run* on macOS/Linux
  for development, but always-on-top + drop shadow are tuned for Windows DWM).

---

## Setup

### 1. Install Python 3.11+
Download from [python.org](https://www.python.org/downloads/). On the installer,
check **"Add Python to PATH."**

### 2. Install dependencies
```bash
pip install -r requirements.txt
```
> If `faster-whisper` or `sounddevice` fail to install, JARVIS still runs in
> **text-only** mode — see [Troubleshooting](#troubleshooting).

### 3. Configure your environment
```bash
cp .env.example .env
```
Open `.env` and fill in at minimum your **`ANTHROPIC_API_KEY`**. Everything else
is optional.

### 4. Get an Anthropic API key
1. Go to the [Anthropic Console](https://console.anthropic.com/settings/keys).
2. Create a key and paste it into `.env` as `ANTHROPIC_API_KEY=sk-ant-...`.

### 5. (Optional) Google Calendar
1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project → enable the **Google Calendar API**.
3. Create an **OAuth client ID** of type **Desktop app**.
4. Download the JSON, save it as `credentials.json` in the project root.
5. Make sure `GOOGLE_CREDENTIALS_PATH=credentials.json` in `.env`.
6. On first run, a browser opens for consent; a `token.json` is cached locally.

### 6. (Optional) Microsoft / Outlook Calendar
1. Open the [Azure Portal](https://portal.azure.com/) → **App registrations** →
   **New registration**.
2. Set the account type, and add **`Calendars.Read`** delegated permission under
   *API permissions* (Microsoft Graph).
3. Enable **public client / device code flow** under *Authentication*.
4. Copy the **Application (client) ID** and **Directory (tenant) ID** into
   `.env` (`OUTLOOK_CLIENT_ID`, `OUTLOOK_TENANT_ID`).
5. On first run, follow the device-code prompt printed to the console/log.

### 6b. No Azure access? Use the published-calendar ICS fallback
If your org won't grant an Azure App Registration, you can still surface
your Outlook busy/free times via a published calendar feed — no admin
approval needed, just your own Outlook-on-the-web settings:
1. Outlook on the web → **Settings → Calendar → Shared calendars → Publish
   a calendar**.
2. Choose **"Can view when I'm busy"** and copy the **ICS link**.
3. Paste it into `.env` as `OUTLOOK_ICS_URL=...`.

This only shows blocks of busy/free time — no titles, locations, or
descriptions, since that's all the publish mode exposes. It's labeled
`[Outlook-ICS]` in JARVIS so it's clearly distinct from full Graph-based
events, and it works alongside (not instead of) the Azure-based setup
above if you ever get access to both.

> **Treat this URL as a secret.** Anyone who has it can see your busy/free
> times. Never share it or commit it to a repo.

### 7. (Optional) Todoist
1. In Todoist, go to **Settings → Integrations → Developer** and copy your
   personal **API token**.
2. Paste it into `.env` as `TODOIST_API_KEY=...`.
3. Categories (e.g. "Daedabyte", "General", "Brightpoint", "DnD") map to
   Todoist projects — JARVIS creates the project automatically the first time
   it files a task under a category that doesn't exist yet.
4. Multi-step tasks — ask for something like "make a task to plan the team
   offsite with steps to book a venue, send invites, and order catering" and
   JARVIS nests each step as a Todoist subtask under the parent task, shown
   indented when you ask for your task list.

### 7b. (Optional) Meal prep
Reuses the Google Calendar and Todoist setup above — no extra config. Just
ask JARVIS to plan your dinners for the next two weeks; it searches the web
for ideas, proposes a plan for you to approve, then creates the calendar
events and a "Groceries" Todoist project. Plans are recorded in
`meal_plans.json` (gitignored) so future cycles avoid recent repeats.

### 7c. (Optional) Gmail
Reuses the Google `credentials.json` from step 5 but needs its own consent for
mail scopes (read + draft), so it's opt-in:
1. In the [Google Cloud Console](https://console.cloud.google.com/), enable the
   **Gmail API** on the same project.
2. Set `GMAIL_ENABLED=true` in `.env`.
3. List the inboxes you want under `GMAIL_ACCOUNTS` (comma-separated names). For
   three accounts: `GMAIL_ACCOUNTS=personal,work,side`. Leave it blank to reuse
   the calendar's `GOOGLE_ACCOUNTS` instead.
4. The **first** time each account is used, a browser opens to authorize it —
   pick the matching Google login in the account chooser for each one. Tokens
   are cached per account under `tokens/google_mail/{name}.json` (gitignored).

JARVIS searches **all** configured inboxes at once (each result is tagged with
its account, e.g. `[work]`), reads recent mail, and **drafts** replies for you
to review — it never sends mail on its own, since sending is hard to undo. With
several accounts, tell it which to draft from ("draft a reply from my work
email"). Ask things like "any unread from Sam this week across my inboxes?" or
"summarize today's email."

> **Authorizing 3 accounts:** the very first "check my email" will walk through
> the browser consent for each account in turn (one window per account). After
> that the tokens are cached and it's silent. If a window picks the wrong Google
> login, delete that account's file under `tokens/google_mail/` and try again.

### 7d. (Optional) Spoken replies (TTS)
JARVIS can read answers aloud. It's **off by default** — turn it on live with the
speaker button in the overlay or the **Speak Replies** tray item, or start it on
with `TTS_ENABLED=true`. Choose an engine with `TTS_ENGINE` and install just what
it needs:
- **`edge`** (default) — free, natural neural voices via `edge-tts`. Needs
  `edge-tts` + `miniaudio` and an internet connection. Note the reply text is
  sent to Microsoft (unlike the fully-local speech-to-text).
- **`system`** — fully offline via `pyttsx3` and your OS voices. Private and
  free, but a more robotic voice. Nothing leaves your machine.
- **`elevenlabs`** — premium, most expressive. Set `ELEVENLABS_API_KEY` (and
  optionally `ELEVENLABS_VOICE_ID`); uses only `requests`.

Speech stops automatically when you send a new message or start the mic, so
JARVIS never talks over you.

### 7e. (Optional) Name corrections
Voice transcription and typing mangle fantasy/proper names ("Kailin" → "Cailynn",
"Adaria" → "Ederia"). Give JARVIS a glossary and it fixes them — in the chat, in
saved notes, and in tool calls — for both spoken and typed input:

```bash
cp name_corrections.example.json name_corrections.json
```

Each canonical spelling lists its known misspellings:

```json
{ "names": { "Kailin": ["Cailynn", "Caelyn"], "Adaria": ["Ederia"] } }
```

Listed variants are corrected exactly; a conservative fuzzy matcher also catches
new, unlisted close variants (capitalized, near-exact only — normal prose is left
alone). Ordinary English words and common first names are never auto-corrected, so
"Mark" stays "Mark" instead of becoming "Marik"; add your own exceptions (coworkers,
terms close to a fantasy name) under an optional `"ignore"` list:

```json
{ "names": { "Marik": ["Marikh"] }, "ignore": ["Sharyl", "Daedabyte"] }
```

The canonical names are also fed to Whisper as hints so transcription gets them
right more often to begin with. Edit the file and hit **Reload Context** (tray) to
apply changes without restarting. The file is gitignored.

### 7f. (Optional) Spotify music
Let JARVIS play and control music. **Requires Spotify Premium** and an open
Spotify device (the desktop/phone app running) — the Web API can only control
playback under those conditions.
1. Create an app in the
   [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. In its settings, add this **Redirect URI** exactly — it must be the loopback
   IP, not `localhost`, which Spotify now rejects:
   `http://127.0.0.1:9433/callback`
3. Copy the app's **Client ID** into `.env` as `SPOTIFY_CLIENT_ID` (no client
   secret needed — JARVIS uses PKCE), and set `SPOTIFY_ENABLED=true`.
4. The first music request opens a browser once to authorize; the token caches
   to `tokens/spotify_oauth.json` (gitignored) and refreshes silently after.

Then just ask: "play Back in Black", "play some Daft Punk", "put on my Focus
playlist", "pause", "skip", "set the volume to 30", "shuffle on", "what's
playing?". And there may be a certain phrase that summons a certain AC/DC song
with… maximum attitude. 🎸

### 7g. (Optional) Obsidian knowledge vault (your second brain)
Give JARVIS a real, browsable memory by pointing it at an
[Obsidian](https://obsidian.md) vault — which is just a folder of markdown files.
JARVIS reads and writes it directly on disk (no Obsidian plugin, and Obsidian
doesn't even need to be running), so the vault becomes one linked home for your
notes **and** its long-term memory.

1. Pick a folder for the vault — an existing Obsidian vault, or a brand-new empty
   folder (JARVIS seeds a starter structure on first run).
2. In `.env`, set both:
   ```
   OBSIDIAN_ENABLED=true
   OBSIDIAN_VAULT_PATH=C:\Users\you\Documents\Brain
   ```
3. Run JARVIS. On first launch it scaffolds default folders (`Sessions/`,
   `Daily/`, `People/`, `Companies/`, `Projects/`, `Topics/`, `Memory/`) plus an
   `index.md` Map of Content, then does a **one-time, non-destructive** import of
   any existing `notes/<category>/` files and `memory.db` facts into the vault —
   your originals are left untouched as a safety net.

Once enabled, the vault **replaces** the per-category notes folder and the SQLite
recall store as JARVIS's durable brain. Ask it to "make a note about my meeting
with Sam", "what do you know about the Q3 launch?", or "what did we decide last
week?" and it searches/writes the vault — then open the same files in Obsidian to
read, edit, and follow the links yourself. The model-facing tools are
`search_vault`, `read_note`, `write_note`, `append_note`, and `list_notes`. A
rebuildable search index (`vault_index.db`) is kept beside the app and is
gitignored.

**Consistent organization, by design.** Every note lands in the right folder and
follows a uniform shape, so the vault stays readable as it grows:

- **Entity folders** — `People/` (one individual per note), `Companies/` (an org,
  client, or team), and `Projects/` (a project, product, or campaign). Notes here
  are *canonical* identities (de-duplicated by their `aliases:`).
- **Deterministic routing** — a meeting/standup/call note can **never** land in an
  entity folder. However it's created (a tool call, a paste, a fact write), it is
  redirected to `Sessions/`, so people and meetings never get mixed up.
- **Per-type templates** — a fresh note is seeded with its type's section skeleton
  (a person gets *Facts · Projects · Notes*; a meeting gets *Summary · Attendees ·
  Decisions · Action Items · Open Questions*; etc.), and JARVIS is told the same
  templates so its free-form writing matches. The taxonomy (folders → type → graph
  color) lives in `app/vault_taxonomy.py` and is extensible via `vault_config.json`.

> JARVIS can create and edit any note **inside** the vault (never outside it —
> paths that escape the vault are refused). It reads a note before overwriting and
> prefers appending for logs, but if you want a hands-off zone, keep those notes in
> a separate vault.

**Verify it first — token-free.** A small management CLI lets you check the wiring
and preview the import **without spending any Claude tokens** (it never calls the
API). From the repo root:

```bash
python -m app.vault_cli check            # config + scaffold + what would import
python -m app.vault_cli migrate --dry-run  # preview the conversion (writes nothing)
python -m app.vault_cli migrate          # do the import (idempotent; copies, never moves)
python -m app.vault_cli search "budget"  # confirm retrieval works
python -m app.vault_cli list [folder]    # browse the vault
```

`check` and `migrate --dry-run` are read-only previews — start there. The same
migration also runs automatically the first time you launch JARVIS with the vault
enabled, so the CLI is optional; it just lets you look before you leap.

**Tidy up the imported notes (optional, uses the API).** The migration drops your
old notes into `Imported/` as-is. To have Claude reformat each one (frontmatter,
tags, `[[wikilinks]]`) and refile it into the right folder, run the cleanup pass.
Unlike the commands above, **this one calls the Claude API per note, so it costs
tokens** — it's preview-first and non-destructive:

```bash
python -m app.vault_organize              # PREVIEW the plan (writes nothing)
python -m app.vault_organize --limit 5    # try it on the first 5 notes
python -m app.vault_organize --apply       # tidy + refile; originals move to Archive/
```

With `--apply`, each tidied note is written to its new home and the original is
moved to `Archive/` (never deleted, and excluded from search), so you can review in
Obsidian and delete `Archive/` once you're happy. Faithful cleanup only — it
preserves every decision, action item, and date rather than summarizing them away.

**Consolidate identities (one note per person, company, and project).** Imported
notes often refer to the same entity several ways — a person (`Joe`, `Joe K`,
`Joe Konkle`), a company (`Databyte`/`Daedabyte`), or a project (`CCC`/`CCC Legacy`)
— splitting them across notes and links. The consolidation pass (also API-backed)
clusters the variants per kind and fixes them:

```bash
python -m app.vault_entities                  # PREVIEW people + companies + projects (writes nothing)
python -m app.vault_entities --apply           # consolidate all kinds, rewrite links
python -m app.vault_entities --kind companies  # just companies
```

`--apply` records the canonical name + `aliases:` on one note in the right folder
(`People/`, `Companies/`, or `Projects/`), folds any duplicate notes into it
(originals → `Archive/`), and rewrites every `[[alias]]` in the vault to the
canonical name. Going forward this mostly takes care of itself: JARVIS knows the
roster (people + companies + projects, in its prompt) and **canonicalizes links
automatically on every write**, so each entity stays one note. The source of truth
is the `aliases:` list in each entity note — edit it in Obsidian anytime to teach a
new nickname. (`python -m app.vault_people` still works as a people-only shortcut.)

**Fix misfiled notes (uses the API).** If notes ended up in the wrong folder (a
company under `People/`, a meeting under `Projects/`), the reclassify pass asks
Claude what each entity note is really about and moves it to the matching folder —
merging into an existing same-named entity instead of duplicating it:

```bash
python -m app.vault_entities --reclassify          # PREVIEW the folder fixes (writes nothing)
python -m app.vault_entities --reclassify --apply  # move person/company/project/meeting notes home
```

**Keep the graph organized (token-free).** A few commands keep the vault tidy and
make the graph view look like a colored brain:

```bash
python -m app.vault_cli graph     # color the graph by folder + stamp `type:` on every note
python -m app.vault_cli moc       # rebuild hub "Maps of Content" that link each folder's notes
python -m app.vault_cli doctor    # health: orphans, dangling links, misfiled notes, dupes
python -m app.vault_cli refile    # move meeting notes wrongly filed in an entity folder → Sessions/
python -m app.vault_cli dedupe    # merge the same name appearing in two entity folders
python -m app.vault_cli idea "ship a wake word"   # quick-capture an idea into Ideas/Inbox.md
```

- **`graph`** writes `.obsidian/graph.json` so Obsidian's graph colors nodes by
  folder (people green, projects cyan, ideas pink, …) — instant visual clusters.
- **`moc`** generates a `Maps/<Folder>.md` hub linking every note in that folder
  (and refreshes `index.md`), which forms the bright cluster-centers in the graph
  and eliminates orphans.
- **`doctor`** flags islands (notes with no links), dangling `[[links]]`, **misfiled
  meetings** (a meeting sitting in an entity folder — at any depth), and
  **cross-folder duplicates** (the same name in two entity folders) so you can keep
  the brain tidy.
- **`refile`** moves misfiled meeting notes into `Sessions/` (preview-first;
  `--apply` to commit) — token-free, and catches meetings nested under a project too.
- **`dedupe`** merges cross-folder duplicates — the `People/Joe Konkle.md` +
  `Projects/joe_konkle.md` pairs the old fact-router used to create. It keeps the
  copy in the highest-priority folder (People → Companies → Projects) and folds the
  rest in (originals → `Archive/`, reversible). For an entity that's simply in the
  *wrong* folder (a campaign filed under People), use `vault_entities --reclassify
  --apply`, which sorts placement with the API.

New notes are now routed and de-duplicated correctly on write — meetings always go
to `Sessions/`, and a fact about an existing person is never split into a second
folder even if it's mis-tagged — so these cleanups are one-time. JARVIS also runs
**`refile` + `graph` + `moc` automatically on startup** (so stray meetings self-heal
back to `Sessions/`), idempotently — set `OBSIDIAN_AUTO_ORGANIZE=false` to manage it
yourself.

**Fix everything mechanical in one shot.** `upgrade` is the token-free "clean it all
up" command: it refiles stray meetings to `Sessions/`, merges cross-folder
duplicates, type-stamps, canonicalizes + wikilinks bare mentions, and rebuilds the
hubs + graph colors across the whole vault. Moves/merges are reversible (originals →
`Archive/`):

```bash
python -m app.vault_cli upgrade   # refile + dedupe + type-stamp + link + rebuild hubs
```

For the deeper, content-level cleanup, run the two API-backed passes it points you to
(`vault_organize --apply` to reformat/refile the `Imported/` dump,
`vault_entities --reclassify --apply` to move misfiled entities to the right folder).

**Future-proof for new categories.** Folders, their note `type`, whether they hold
de-duplicated entities, and their graph color all live in the **taxonomy**
(`app/vault_taxonomy.py`). Add a category without touching code by dropping a
`vault_config.json` at the repo root:

```json
{"folders": [
  {"folder": "Books", "type": "book", "entity": true,  "color": "#ff8a65"},
  {"folder": "Goals", "type": "goal", "entity": false, "color": "#7e57c2"}
]}
```

JARVIS will scaffold the folder, stamp the new `type:`, color it in the graph, and
(for `entity: true`) de-duplicate names in it — all automatically.

### 8. Add your personal context
```bash
cp context/profile.example.md context/profile.md
```
Edit `context/profile.md` with your roles, jobs, and preferences. Add any other
`.md` files to `context/` and they're automatically included. (All real
`context/*.md` files are gitignored.)

### 8b. (Optional) Tailor JARVIS's personality
```bash
cp context/persona.example.md context/persona.md
```
`persona.md` is the *character* doc — how JARVIS talks, separate from
`profile.md` (which is about you). Skip it and he uses a movie-accurate default.

Five **voice dials** (0–100) fine-tune him live. Two ways to change them:

- **Sliders** — click the 🎛 button in the overlay header for a panel of
  sliders, with **Save** (make current values your defaults) and **Reset**.
  Adjusting a slider talks straight to Python — no model call, so it costs
  zero tokens.
- **Just ask** — *"turn the sarcasm up to 70"*, *"humor down 15% for this
  convo"*, *"max brevity"*, *"stop calling me Sir"*, *"reset your personality"*.

Dials are `brevity`, `formality`, `humor`, `sarcasm`, `proactivity`. Changes
apply to the current session unless you Save them (or ask him to *remember*).
To set the defaults directly, `cp persona_dials.example.json persona_dials.json`
and edit the numbers (both gitignored).

### 9. Run it
```bash
python main.py
```
JARVIS starts in the system tray. **Left-click** (or right-click → Open) to show
the overlay.

---

## Notes & memory

> **With an Obsidian vault enabled (step 7g), it is the single home for notes and
> memory** and supersedes everything in this section — JARVIS reads/writes the
> vault instead of the `/notes/` folder. The folder-based system below is the
> fallback used only when no vault is configured.

### Using the `/notes/` folder (no-vault fallback)

Notes are split into separate streams, one subfolder each, so they never mix:
`notes/Daedabyte/`, `notes/Brightpoint/`, `notes/DnD/`, and `notes/General/`
(anything that isn't a job or D&D). Drop meeting/session notes as `.txt` or
`.md` files into the matching subfolder; when asked, JARVIS reads the **5
most recent** (by modified time) from whichever single category you mean,
newest first — it never merges streams together. If you don't say which one
and it's not obvious, JARVIS asks rather than guessing.

Suggested naming: `YYYY-MM-DD_topic.md` (e.g. `2026-06-15_standup.md`).

To keep prompts lean, each note is truncated to ~2000 characters in context.

You can also ask JARVIS to write the note for you — e.g. "make a note for
Daedabyte about my meeting with Sam on the 16th, we discussed Q3 timelines" —
and it saves a `YYYY-MM-DD_topic.md` file into the right subfolder using what
you told it, no manual file-dropping required. Tell it the same way to extract
Todoist tasks from notes you've already dropped in, e.g. "check the Brightpoint
notes from the 16th and add any action items to Todoist."

---

## Token & cache usage

JARVIS records the `usage` from every Claude API call — input/output tokens plus
the prompt-cache counters — to `logs/usage.jsonl` (gitignored), and logs a
one-line summary per turn and per session. To see totals, the **cache hit rate**
(how much of your input was served cheaply from cache), and an estimated cost:

```bash
python -m app.usage_report            # all-time totals + per-model / per-kind breakdown
python -m app.usage_report --by-day   # daily rows, to spot spend spikes
python -m app.usage_report --since 2026-06-01
```

This is token-free (it only reads the log). Cost is estimated from Anthropic's
published per-1M-token rates with the cache multipliers applied (5-min cache
writes at 1.25×, reads at 0.1×). If prices drift, drop a
`jarvis_usage_prices.json` at the repo root to override them, e.g.
`{"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}}` — no code change needed.

A healthy cache hit rate on multi-turn chats confirms the prompt-caching split
(`app/context_builder.py`) is doing its job; a persistent 0% means something is
invalidating the cached prefix.

---

## Configuration reference (`.env`)

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | **Required.** Your Claude API key. |
| `ANTHROPIC_MODEL` | Model id (default `claude-sonnet-4-6`). |
| `ANTHROPIC_SUMMARY_MODEL` | Cheap model for session/compaction summaries (default `claude-haiku-4-5`). |
| `JARVIS_USER_NAME` | Your name, used in the prompt + UI. |
| `JARVIS_WINDOW_POSITION` | `top-right` / `top-left` / `bottom-right` / `bottom-left`. |
| `JARVIS_HOTKEY` | Global toggle hotkey, e.g. `ctrl+space` (blank = off). |
| `JARVIS_SCREENSHOT_HOTKEY` | Global hotkey for the drag-to-select screenshot capture (blank = camera button only). |
| `JARVIS_LOCATION` | Default city for weather / daily briefing (blank = JARVIS asks). |
| `JARVIS_MAX_CONTEXT_CHARS` | Hard cap on assembled context (default 32000). |
| `JARVIS_TIMEZONE` | IANA zone override for calendar events (blank = auto-detect). |
| `WHISPER_MODEL` | `tiny` / `base` / `small` / `medium`. |
| `JARVIS_WAKE_WORD_ENABLED` | `true` for hands-free "Hey JARVIS" activation on top of push-to-talk. Default off. |
| `JARVIS_WAKE_WORD_PHRASE` | A bundled openWakeWord phrase, default `hey_jarvis`. |
| `JARVIS_WAKE_WORD_THRESHOLD` | Detection confidence 0-1 (default `0.5`) — tune after trying it live. |
| `TTS_ENABLED` | `true` to start with spoken replies on (toggle live anytime). Default off. |
| `TTS_ENGINE` | `edge` (free neural) / `system` (offline pyttsx3) / `elevenlabs` (premium). |
| `TTS_VOICE` | Engine-specific voice (blank = engine default). |
| `ELEVENLABS_API_KEY` | Required only when `TTS_ENGINE=elevenlabs`. |
| `GOOGLE_CREDENTIALS_PATH` | Path to Google OAuth `credentials.json`. |
| `OUTLOOK_CLIENT_ID` / `_TENANT_ID` / `_CLIENT_SECRET` | Azure app registration. |
| `OUTLOOK_ICS_URL` | Published calendar ICS link — no-Azure fallback, busy/free only. |
| `TODOIST_API_KEY` | Personal API token from Todoist's Developer settings. |
| `GMAIL_ENABLED` | `true` to enable Gmail read/draft tools (reuses Google `credentials.json`). |
| `GMAIL_ACCOUNTS` | Comma-separated Gmail account names, e.g. `personal,work,side` (blank = reuse `GOOGLE_ACCOUNTS`). |
| `OBSIDIAN_ENABLED` | `true` to use an Obsidian vault as the notes + memory store (second brain). Default off. |
| `OBSIDIAN_VAULT_PATH` | Absolute path to the vault folder (created if missing), e.g. `C:\Users\you\Documents\Brain`. |
| `JARVIS_VAULT_CALLBACKS_ENABLED` | `true` to nudge once when a Sessions/ note's open items go stale. Default off. |
| `JARVIS_VAULT_CALLBACK_DAYS` | How many days untouched before a nudge (default `4`). |

---

## Privacy

This repo is built so your personal data **never** reaches GitHub. The
`.gitignore` excludes:

- `.env` and any `*.env` (your API keys)
- `token.json`, `credentials.json`, `.msal_cache.bin` (auth tokens)
- `context/*` **except** the `*.example.md` templates (your profile/notes)
- `notes/*` (your meeting notes)
- `memory.db` and `vault_index.db` (your recall store and the vault search index)
- `meal_plans.json` (your dinner plans/shopping lists)
- `logs/*` (may contain calendar/notes content)
- recorded `*.wav` audio and downloaded model caches

Your Obsidian vault lives wherever `OBSIDIAN_VAULT_PATH` points (outside this
repo), so its contents are never part of the project in the first place.

Only code and the `.example` templates are tracked. Verify any time with:
```bash
git status --ignored
```

---

## Troubleshooting

**"No Anthropic API key found."** Add `ANTHROPIC_API_KEY` to `.env`.

**Voice button is disabled.** Either `sounddevice`/`faster-whisper` aren't
installed or no microphone was detected. Text input always works. On first voice
use, the Whisper model downloads automatically (this can take a minute).

**Speaker button is greyed out.** TTS deps aren't installed or the engine isn't
configured. For `edge`, install `edge-tts` + `miniaudio`; for `system`, install
`pyttsx3`; for `elevenlabs`, set `ELEVENLABS_API_KEY`. Text replies always work.

**Calendar shows nothing.** Calendars are optional and skip silently when not
configured. Check `logs/jarvis.log` for auth details.

**Vault tools don't appear / "the knowledge vault isn't configured."** Set
**both** `OBSIDIAN_ENABLED=true` and `OBSIDIAN_VAULT_PATH` (an absolute path). The
startup log's readiness table has an "Obsidian vault" row; if the path is blank
the vault tools stay hidden and JARVIS falls back to the local notes folder +
recall store. If search comes up empty, the index rebuilds from your files on the
next launch.

**Global hotkey doesn't work.** The `keyboard` library needs elevated
privileges on some systems. It's optional — leave `JARVIS_HOTKEY` blank to skip.

**Logs.** Everything is timestamped in `logs/jarvis.log`.

---

## Project structure

```
jarvis/
├── main.py                 # Entry point — tray + overlay + services
├── app/
│   ├── config.py           # .env loading + paths + palette
│   ├── logging_setup.py    # Rotating file logger
│   ├── context_builder.py  # Assembles the system prompt (the brain)
│   ├── claude_client.py    # Anthropic streaming + session memory
│   ├── memory.py           # SQLite recall store (no-vault fallback)
│   ├── vault_index.py      # FTS5 search index over the Obsidian vault
│   ├── vault_taxonomy.py   # Folder → type → entity → graph-color taxonomy
│   ├── vault_templates.py  # Per-type note section templates
│   ├── vault_cli.py        # Token-free vault tools (check/migrate/doctor/refile…)
│   ├── vault_entities.py   # Consolidate + reclassify entities (uses the API)
│   ├── recorder.py         # Mic capture (sounddevice)
│   ├── transcriber.py      # faster-whisper STT
│   ├── wakeword.py         # "Hey JARVIS" hands-free activation (openWakeWord)
│   ├── tts.py              # Text-to-speech (edge/pyttsx3/elevenlabs), sentence-streamed
│   ├── screenshot.py       # Screen capture + encoding for vision-aware questions
│   ├── proactive.py        # Background scheduler (briefing/meetings/email/vault callbacks)
│   ├── overlay.py          # The floating UI window
│   ├── tray.py             # System tray icon + menu
│   └── icon.py             # Runtime tray-icon drawing
├── integrations/
│   ├── google_calendar.py
│   ├── outlook_calendar.py
│   ├── todoist.py
│   ├── meal_prep.py
│   ├── obsidian.py         # Obsidian vault engine (second brain)
│   └── notes_watcher.py    # notes/ folder watcher (no-vault fallback)
├── context/                # Your *.md context (gitignored; .example tracked)
├── notes/                  # Drop meeting notes here (gitignored)
├── logs/                   # jarvis.log (gitignored)
└── assets/                 # Generated tray icon (gitignored)
```

---

## License

Personal project — use it however you like.
