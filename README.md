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
- **Talk back (optional)** — JARVIS can read replies aloud, off by default and
  toggled live with the speaker button or the tray. Pick your engine: free
  neural `edge-tts`, fully-offline `pyttsx3`, or premium ElevenLabs.
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
- **Cross-session memory** — when you close a longer chat, JARVIS saves a short
  recap and can recall it later ("pick up where we left off").
- **Smooth replies** — an animated "thinking" indicator while JARVIS composes,
  then the answer fades in line by line (no half-formed text filling in).
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
alone). The canonical names are also fed to Whisper as hints so transcription
gets them right more often to begin with. Edit the file and hit **Reload Context**
(tray) to apply changes without restarting. The file is gitignored.

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

## Using the `/notes/` folder

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

## Configuration reference (`.env`)

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | **Required.** Your Claude API key. |
| `ANTHROPIC_MODEL` | Model id (default `claude-sonnet-4-6`). |
| `ANTHROPIC_SUMMARY_MODEL` | Cheap model for session/compaction summaries (default `claude-haiku-4-5`). |
| `JARVIS_USER_NAME` | Your name, used in the prompt + UI. |
| `JARVIS_WINDOW_POSITION` | `top-right` / `top-left` / `bottom-right` / `bottom-left`. |
| `JARVIS_HOTKEY` | Global toggle hotkey, e.g. `ctrl+space` (blank = off). |
| `JARVIS_LOCATION` | Default city for weather / daily briefing (blank = JARVIS asks). |
| `JARVIS_MAX_CONTEXT_CHARS` | Hard cap on assembled context (default 32000). |
| `JARVIS_TIMEZONE` | IANA zone override for calendar events (blank = auto-detect). |
| `WHISPER_MODEL` | `tiny` / `base` / `small` / `medium`. |
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

---

## Privacy

This repo is built so your personal data **never** reaches GitHub. The
`.gitignore` excludes:

- `.env` and any `*.env` (your API keys)
- `token.json`, `credentials.json`, `.msal_cache.bin` (auth tokens)
- `context/*` **except** the `*.example.md` templates (your profile/notes)
- `notes/*` (your meeting notes)
- `meal_plans.json` (your dinner plans/shopping lists)
- `logs/*` (may contain calendar/notes content)
- recorded `*.wav` audio and downloaded model caches

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
│   ├── recorder.py         # Mic capture (sounddevice)
│   ├── transcriber.py      # faster-whisper STT
│   ├── overlay.py          # The floating UI window
│   ├── tray.py             # System tray icon + menu
│   └── icon.py             # Runtime tray-icon drawing
├── integrations/
│   ├── google_calendar.py
│   ├── outlook_calendar.py
│   ├── todoist.py
│   ├── meal_prep.py
│   └── notes_watcher.py
├── context/                # Your *.md context (gitignored; .example tracked)
├── notes/                  # Drop meeting notes here (gitignored)
├── logs/                   # jarvis.log (gitignored)
└── assets/                 # Generated tray icon (gitignored)
```

---

## License

Personal project — use it however you like.
