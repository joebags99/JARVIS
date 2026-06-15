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

### 7. Add your personal context
```bash
cp context/profile.example.md context/profile.md
```
Edit `context/profile.md` with your roles, jobs, and preferences. Add any other
`.md` files to `context/` and they're automatically included. (All real
`context/*.md` files are gitignored.)

### 8. Run it
```bash
python main.py
```
JARVIS starts in the system tray. **Left-click** (or right-click → Open) to show
the overlay.

---

## Using the `/notes/` folder

Drop meeting notes as `.txt` or `.md` files into `notes/`. The **5 most recent**
(by modified time) are included in JARVIS's context automatically, newest first.

Suggested naming: `YYYY-MM-DD_topic.md` (e.g. `2026-06-15_standup.md`).

To keep prompts lean, each note is truncated to ~2000 characters in context.

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
| `WHISPER_MODEL` | `tiny` / `base` / `small` / `medium`. |
| `GOOGLE_CREDENTIALS_PATH` | Path to Google OAuth `credentials.json`. |
| `OUTLOOK_CLIENT_ID` / `_TENANT_ID` / `_CLIENT_SECRET` | Azure app registration. |

---

## Privacy

This repo is built so your personal data **never** reaches GitHub. The
`.gitignore` excludes:

- `.env` and any `*.env` (your API keys)
- `token.json`, `credentials.json`, `.msal_cache.bin` (auth tokens)
- `context/*` **except** the `*.example.md` templates (your profile/notes)
- `notes/*` (your meeting notes)
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
│   └── notes_watcher.py
├── context/                # Your *.md context (gitignored; .example tracked)
├── notes/                  # Drop meeting notes here (gitignored)
├── logs/                   # jarvis.log (gitignored)
└── assets/                 # Generated tray icon (gitignored)
```

---

## License

Personal project — use it however you like.
