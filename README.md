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

- **System tray** icon (idle / listening / thinking states) with Open, Reload
  Context, Settings, and Quit.
- **Floating overlay** — frameless, always-on-top, draggable, dark theme
  (`#0f0f0f` + cyan `#00bcd4`). Closes on `Esc` or click-away.
- **Type or talk** — push-to-talk voice via local `faster-whisper` (no audio
  ever leaves your machine; transcription is free and offline).
- **Context-aware** — assembles a system prompt from your `context/*.md` files,
  Google + Outlook calendars (next 7 days), recent `notes/`, and the date/time.
- **Meal prep** — plan dinners two weeks at a time in conversation (with real
  web search for recipe ideas), then push the plan to your Google Calendar and
  a Todoist shopping list in one go.
- **Streaming replies** that fill in token by token.
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

### 8. Add your personal context
```bash
cp context/profile.example.md context/profile.md
```
Edit `context/profile.md` with your roles, jobs, and preferences. Add any other
`.md` files to `context/` and they're automatically included. (All real
`context/*.md` files are gitignored.)

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
| `JARVIS_USER_NAME` | Your name, used in the prompt + UI. |
| `JARVIS_WINDOW_POSITION` | `top-right` / `top-left` / `bottom-right` / `bottom-left`. |
| `JARVIS_HOTKEY` | Global toggle hotkey, e.g. `ctrl+space` (blank = off). |
| `JARVIS_MAX_CONTEXT_CHARS` | Hard cap on assembled context (default 32000). |
| `JARVIS_TIMEZONE` | IANA zone override for calendar events (blank = auto-detect). |
| `WHISPER_MODEL` | `tiny` / `base` / `small` / `medium`. |
| `GOOGLE_CREDENTIALS_PATH` | Path to Google OAuth `credentials.json`. |
| `OUTLOOK_CLIENT_ID` / `_TENANT_ID` / `_CLIENT_SECRET` | Azure app registration. |
| `OUTLOOK_ICS_URL` | Published calendar ICS link — no-Azure fallback, busy/free only. |
| `TODOIST_API_KEY` | Personal API token from Todoist's Developer settings. |

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
