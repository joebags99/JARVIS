"""Central configuration for JARVIS.

Loads settings from the .env file (via python-dotenv) and exposes them as a
single ``CONFIG`` object plus a few well-known paths. Everything personal lives
on disk in gitignored files; this module just reads it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the app/ package.
ROOT_DIR = Path(__file__).resolve().parent.parent

# Load .env from the project root if present (no error if missing).
load_dotenv(ROOT_DIR / ".env")

# ── Well-known directories ───────────────────────────────────────────────────
CONTEXT_DIR = ROOT_DIR / "context"
NOTES_DIR = ROOT_DIR / "notes"
LOGS_DIR = ROOT_DIR / "logs"
ASSETS_DIR = ROOT_DIR / "assets"
LOG_FILE = LOGS_DIR / "jarvis.log"
TRAY_ICON_PATH = ASSETS_DIR / "tray_icon.png"

# Ensure runtime dirs exist (they're gitignored but must be present at runtime).
for _d in (CONTEXT_DIR, NOTES_DIR, LOGS_DIR, ASSETS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Default note/task categories (work/personal "streams"). Each maps 1:1 to a
# notes/<category>/ subfolder and a Todoist project. Override per-user via the
# JARVIS_CATEGORIES env var (comma-separated); kept here as the back-compat
# default for the original single-user setup.
DEFAULT_CATEGORIES = ["Daedabyte", "General", "Brightpoint", "DnD"]

# Panel-editable overrides written by the in-app settings panel. Gitignored,
# same convention as persona_dials.json; values here win over the .env defaults.
USER_CONFIG_FILE = ROOT_DIR / "jarvis_config.json"


# ── UI color palette (from the spec) ─────────────────────────────────────────
@dataclass(frozen=True)
class Palette:
    background: str = "#0f0f0f"
    surface: str = "#1a1a1a"
    border: str = "#2a2a2a"
    accent: str = "#00bcd4"
    accent_dim: str = "#006f7e"
    text_primary: str = "#e8e8e8"
    text_muted: str = "#666666"
    error: str = "#cf6679"
    success: str = "#4caf79"


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _read_user_config() -> dict:
    """Read the gitignored jarvis_config.json. Returns {} if absent or unreadable."""
    if not USER_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(USER_CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _clean_categories(categories: list[str]) -> list[str]:
    """Strip blanks and case-insensitive duplicates, preserving order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for c in categories or []:
        name = str(c).strip()
        if name and name.lower() not in seen:
            cleaned.append(name)
            seen.add(name.lower())
    return cleaned


def _resolve_categories() -> list[str]:
    """Categories from jarvis_config.json, else JARVIS_CATEGORIES, else defaults."""
    saved = _clean_categories(_read_user_config().get("categories") or [])
    if saved:
        return saved
    return _get_list("JARVIS_CATEGORIES", list(DEFAULT_CATEGORIES))


@dataclass
class Config:
    """Resolved runtime configuration."""

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: _get("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(
        default_factory=lambda: _get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    )
    # Cheap model used only for throwaway summarization (session summaries +
    # history compaction). Defaults to Haiku — those calls don't need the main
    # model's quality and run far cheaper here.
    summary_model: str = field(
        default_factory=lambda: _get("ANTHROPIC_SUMMARY_MODEL", "claude-haiku-4-5")
    )

    # App
    user_name: str = field(default_factory=lambda: _get("JARVIS_USER_NAME", "User"))
    # Note/task categories — the named buckets notes and Todoist tasks file into.
    # Resolved from jarvis_config.json (settings panel) → JARVIS_CATEGORIES env →
    # the back-compat defaults, so categories aren't baked into code.
    categories: list[str] = field(default_factory=_resolve_categories)
    window_position: str = field(
        default_factory=lambda: _get("JARVIS_WINDOW_POSITION", "top-right")
    )
    hotkey: str = field(default_factory=lambda: _get("JARVIS_HOTKEY"))
    # Default location for weather / "what's it like out" when the user doesn't
    # name one, e.g. "Chicago, IL". Blank = JARVIS asks which city.
    location: str = field(default_factory=lambda: _get("JARVIS_LOCATION"))
    max_context_chars: int = field(
        default_factory=lambda: _get_int("JARVIS_MAX_CONTEXT_CHARS", 32000)
    )

    # Voice
    whisper_model: str = field(default_factory=lambda: _get("WHISPER_MODEL", "small"))
    # Device index (int) or partial name string. Empty = system default.
    audio_input_device: str = field(
        default_factory=lambda: _get("AUDIO_INPUT_DEVICE", "")
    )
    # Glossary of canonical fantasy/proper names + known misspellings, used to
    # correct transcription/typed input and bias Whisper. See the .example file.
    name_corrections_file: str = field(
        default_factory=lambda: _get("NAME_CORRECTIONS_FILE", "name_corrections.json")
    )
    # Similarity (0-1) a capitalized word must reach to be auto-corrected to a
    # canonical name when it isn't an explicitly-listed variant. High by default
    # so only near-certain typos are fixed (real names like "Adrian" stay put);
    # lower it toward ~0.78 for more aggressive matching.
    name_fuzzy_cutoff: float = field(
        default_factory=lambda: _get_float("NAME_FUZZY_CUTOFF", 0.85)
    )

    # ── Text-to-speech (optional) ────────────────────────────────────────────
    # Read replies aloud. Off by default; toggled at runtime via the speaker
    # button or the tray. Engine: "edge" (free neural, online) | "system"
    # (pyttsx3, offline/robotic) | "elevenlabs" (premium, needs an API key).
    tts_enabled: bool = field(
        default_factory=lambda: _get("TTS_ENABLED").lower() in ("true", "1", "yes")
    )
    tts_engine: str = field(default_factory=lambda: _get("TTS_ENGINE", "edge"))
    # Engine-specific voice id/name. Blank → the backend's own default
    # (edge: en-GB-RyanNeural, system: OS default, elevenlabs: ELEVENLABS_VOICE_ID).
    tts_voice: str = field(default_factory=lambda: _get("TTS_VOICE"))
    # pyttsx3 speaking rate (words per minute); ignored by the other engines.
    tts_rate: int = field(default_factory=lambda: _get_int("TTS_RATE", 175))
    # ElevenLabs (only used when tts_engine == "elevenlabs").
    elevenlabs_api_key: str = field(default_factory=lambda: _get("ELEVENLABS_API_KEY"))
    elevenlabs_voice_id: str = field(
        default_factory=lambda: _get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    )
    elevenlabs_model: str = field(
        default_factory=lambda: _get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
    )

    # Google Calendar
    google_credentials_path: str = field(
        default_factory=lambda: _get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    )
    # Comma-separated account names, e.g. "personal,work,northrop".
    # Each name maps to tokens/google/{name}.json.
    # Defaults to ["default"] for single-account backward compatibility.
    google_accounts: list[str] = field(
        default_factory=lambda: _get_list("GOOGLE_ACCOUNTS", ["default"])
    )

    # Optional IANA zone override (e.g. "America/Chicago") for calendar events
    # Claude creates without a UTC offset. Auto-detected via tzlocal if unset.
    jarvis_timezone: str = field(default_factory=lambda: _get("JARVIS_TIMEZONE"))

    # Knowledge pools — JSON file defining named doc pools (see knowledge_pools.json.example).
    knowledge_pools_file: str = field(
        default_factory=lambda: _get("KNOWLEDGE_POOLS_FILE", "knowledge_pools.json")
    )

    # ── Obsidian vault — JARVIS's "second brain" (single home for notes + memory) ──
    # A local Obsidian vault is just a folder of markdown files (with frontmatter,
    # [[wikilinks]], #tags). JARVIS reads/writes it directly on disk — no plugin and
    # no running Obsidian required. When enabled, the vault replaces the legacy
    # notes/<category>/ folders and memory.db as the durable store. Off by default.
    obsidian_enabled: bool = field(
        default_factory=lambda: _get_bool("OBSIDIAN_ENABLED")
    )
    # Absolute path to the vault folder, e.g. C:\Users\you\Documents\Brain. Created
    # on first use if missing, so a brand-new vault path works just as well as an
    # existing vault.
    obsidian_vault_path: str = field(
        default_factory=lambda: _get("OBSIDIAN_VAULT_PATH")
    )
    # On startup, keep the vault tidy automatically: stamp `type:` on notes,
    # refresh the hub Maps of Content + index.md, and write the graph color config.
    # Idempotent (only rewrites what changed). Set OBSIDIAN_AUTO_ORGANIZE=false to
    # manage those with the `vault_cli graph`/`moc` commands yourself instead.
    obsidian_auto_organize: bool = field(
        default_factory=lambda: _get_bool("OBSIDIAN_AUTO_ORGANIZE", True)
    )

    # ── Proactivity (optional, off by default) ───────────────────────────────
    # Master switch for the background scheduler (scheduled briefing, meeting
    # alerts, important-email pings). Nothing proactive runs unless this is true.
    proactive_enabled: bool = field(
        default_factory=lambda: _get_bool("JARVIS_PROACTIVE_ENABLED")
    )
    # Daily-briefing auto-trigger time as "HH:MM" (24h). Blank = no auto-briefing.
    briefing_time: str = field(default_factory=lambda: _get("JARVIS_BRIEFING_TIME"))
    # Quiet-hours window "HH:MM-HH:MM" during which meeting/email alerts are
    # suppressed (the scheduled briefing still fires). Blank = no quiet hours.
    quiet_hours: str = field(default_factory=lambda: _get("JARVIS_QUIET_HOURS"))
    # Meeting "starts soon" alerts + how many minutes ahead to fire them.
    meeting_alerts: bool = field(default_factory=lambda: _get_bool("JARVIS_MEETING_ALERTS"))
    meeting_lead_min: int = field(
        default_factory=lambda: _get_int("JARVIS_MEETING_LEAD_MIN", 15)
    )
    # Important-email pings (requires Gmail configured).
    email_alerts: bool = field(default_factory=lambda: _get_bool("JARVIS_EMAIL_ALERTS"))
    # Speak proactive notifications aloud (in addition to the tray balloon).
    proactive_speak: bool = field(default_factory=lambda: _get_bool("JARVIS_PROACTIVE_SPEAK"))

    # Monarch Money — connects via official MCP server using OAuth.
    # Set MONARCH_ENABLED=true; a browser opens on first use for authorization.
    monarch_enabled: bool = field(
        default_factory=lambda: _get("MONARCH_ENABLED").lower() in ("true", "1", "yes")
    )

    # Spotify — Web API playback control via OAuth (Authorization Code + PKCE,
    # so only the client id is needed). Requires Spotify Premium and an open
    # Spotify device to control playback. First music request opens a browser.
    spotify_enabled: bool = field(
        default_factory=lambda: _get("SPOTIFY_ENABLED").lower() in ("true", "1", "yes")
    )
    spotify_client_id: str = field(default_factory=lambda: _get("SPOTIFY_CLIENT_ID"))

    # Todoist — personal API token, no OAuth.
    todoist_api_key: str = field(default_factory=lambda: _get("TODOIST_API_KEY"))

    # Gmail — reuses the Google OAuth credentials.json but needs its own consent
    # (mail scopes), so it's opt-in. Set GMAIL_ENABLED=true to surface the email
    # read/draft tools; first use opens a browser to authorize the mail scopes.
    gmail_enabled: bool = field(
        default_factory=lambda: _get("GMAIL_ENABLED").lower() in ("true", "1", "yes")
    )
    # Which Google accounts to use for Gmail (comma-separated names, each with
    # its own mail token under tokens/google_mail/{name}.json). Defaults to the
    # calendar's GOOGLE_ACCOUNTS list when unset, so a single GOOGLE_ACCOUNTS
    # covers both — set GMAIL_ACCOUNTS only when the mail accounts differ.
    gmail_accounts: list[str] = field(
        default_factory=lambda: _get_list("GMAIL_ACCOUNTS", [])
    )

    # Outlook / Microsoft Graph
    outlook_client_id: str = field(default_factory=lambda: _get("OUTLOOK_CLIENT_ID"))
    outlook_tenant_id: str = field(
        default_factory=lambda: _get("OUTLOOK_TENANT_ID", "common")
    )
    outlook_client_secret: str = field(
        default_factory=lambda: _get("OUTLOOK_CLIENT_SECRET")
    )
    # Fallback when an Azure App Registration isn't available: a published
    # free/busy-only ICS feed URL (Outlook on the web → Settings → Calendar
    # → Shared calendars → Publish a calendar). Treat as a secret.
    outlook_ics_url: str = field(default_factory=lambda: _get("OUTLOOK_ICS_URL"))

    palette: Palette = field(default_factory=Palette)

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def has_anthropic_key(self) -> bool:
        return bool(self.anthropic_api_key) and self.anthropic_api_key != "sk-ant-..."

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_credentials_path) and (
            ROOT_DIR / self.google_credentials_path
        ).exists()

    @property
    def outlook_enabled(self) -> bool:
        return bool(self.outlook_client_id)

    @property
    def outlook_ics_enabled(self) -> bool:
        return bool(self.outlook_ics_url)

    @property
    def todoist_enabled(self) -> bool:
        return bool(self.todoist_api_key)

    @property
    def gmail_available(self) -> bool:
        """Gmail is usable only if opted in AND Google credentials exist on disk."""
        return self.gmail_enabled and self.google_enabled

    @property
    def spotify_available(self) -> bool:
        """Spotify is usable when opted in AND a client id is configured.

        The token need not exist yet — the first music request runs the browser
        OAuth flow, same as Monarch.
        """
        return self.spotify_enabled and bool(self.spotify_client_id)

    @property
    def gmail_accounts_resolved(self) -> list[str]:
        """Gmail account names, falling back to the calendar's GOOGLE_ACCOUNTS."""
        return self.gmail_accounts or self.google_accounts

    @property
    def obsidian_vault(self) -> Path | None:
        """The vault folder as a Path, or None when no path is configured."""
        return Path(self.obsidian_vault_path).expanduser() if self.obsidian_vault_path else None

    @property
    def obsidian_available(self) -> bool:
        """Vault tools are usable when opted in AND a vault path is configured.

        The folder itself is created on first use if missing (like notes/), so a
        brand-new path works — point JARVIS at an existing vault or a fresh one.
        Mirrors spotify_available: enabled + minimally configured, existence handled
        at use time.
        """
        return self.obsidian_enabled and bool(self.obsidian_vault_path)

    def save_categories(self, categories: list[str]) -> list[str]:
        """Validate, persist, and apply a new category set (settings panel).

        Strips blanks and case-insensitive duplicates and requires at least one
        category. Writes the result to jarvis_config.json, updates the in-memory
        value, and creates any new notes/<category>/ folders. The model's tool
        enums are built at import, so a restart is needed for JARVIS's tools to
        advertise the new set; notes resolution uses this value live. Raises
        ValueError if no valid category remains.
        """
        cleaned = _clean_categories(categories)
        if not cleaned:
            raise ValueError("At least one category is required.")
        data = _read_user_config()
        data["categories"] = cleaned
        USER_CONFIG_FILE.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        self.categories = cleaned
        for name in cleaned:
            (NOTES_DIR / name).mkdir(parents=True, exist_ok=True)
        return cleaned

    def diagnostics(self) -> list[tuple[str, bool, str]]:
        """Readiness of each integration as ``(name, ok, detail)`` rows.

        Used for the startup self-check so a misconfigured integration is visible
        in the logs at launch instead of only surfacing when a tool is first
        called. ``detail`` explains what's missing (or, when ok, how it's set).
        """
        return [
            ("Anthropic API", self.has_anthropic_key,
             "required — set ANTHROPIC_API_KEY in .env" if not self.has_anthropic_key
             else f"model={self.anthropic_model}"),
            ("Google (calendar/docs)", self.google_enabled,
             f"accounts={','.join(self.google_accounts)}" if self.google_enabled
             else f"missing {self.google_credentials_path}"),
            ("Gmail", self.gmail_available,
             f"accounts={','.join(self.gmail_accounts_resolved)}" if self.gmail_available
             else "set GMAIL_ENABLED=true + Google credentials"),
            ("Outlook (Graph)", self.outlook_enabled,
             "via Azure app" if self.outlook_enabled else "set OUTLOOK_CLIENT_ID"),
            ("Outlook (ICS)", self.outlook_ics_enabled,
             "published feed" if self.outlook_ics_enabled else "set OUTLOOK_ICS_URL"),
            ("Todoist", self.todoist_enabled,
             "token set" if self.todoist_enabled else "set TODOIST_API_KEY"),
            ("Spotify", self.spotify_available,
             "client id set" if self.spotify_available
             else "set SPOTIFY_ENABLED=true + SPOTIFY_CLIENT_ID"),
            ("Monarch Money", self.monarch_enabled,
             "enabled (MCP)" if self.monarch_enabled else "set MONARCH_ENABLED=true"),
            ("Obsidian vault", self.obsidian_available,
             f"vault={self.obsidian_vault_path}" if self.obsidian_available
             else "set OBSIDIAN_ENABLED=true + OBSIDIAN_VAULT_PATH"),
            ("Proactive", self.proactive_enabled,
             "scheduler on" if self.proactive_enabled
             else "set JARVIS_PROACTIVE_ENABLED=true"),
        ]


# Singleton-ish config used across the app.
CONFIG = Config()

# Notes are split into per-category subfolders so separate work streams never
# mix. Created from CONFIG.categories (configurable via JARVIS_CATEGORIES); done
# after CONFIG exists so the set is whatever the user configured.
for _cat in CONFIG.categories:
    (NOTES_DIR / _cat).mkdir(parents=True, exist_ok=True)
