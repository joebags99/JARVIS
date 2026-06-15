"""Google Calendar integration.

First run opens a browser for OAuth2 consent; the resulting token is cached in
``token.json`` (gitignored) so subsequent runs are silent. If credentials are
missing or auth fails, every public function returns empty / no-ops and logs a
warning — JARVIS keeps working without calendar context.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger

log = get_logger("google_cal")

# Read-only access to calendars.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = ROOT_DIR / "token.json"


@dataclass
class CalEvent:
    """A normalized calendar event, source-agnostic."""

    summary: str
    start: dt.datetime
    end: dt.datetime | None
    location: str | None
    all_day: bool
    source: str  # "Google" | "Outlook"

    def format_line(self) -> str:
        when = self.start.strftime("%a %b %d") if self.all_day else self.start.strftime(
            "%a %b %d %I:%M %p"
        )
        loc = f" @ {self.location}" if self.location else ""
        return f"- {when} — {self.summary}{loc} [{self.source}]"


def _load_credentials():
    """Load cached creds, refreshing or running the OAuth flow as needed."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        log.warning("google api libraries not installed; skipping Google Calendar")
        return None

    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read token.json: %s", exc)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning("token refresh failed, re-authenticating: %s", exc)

    cred_file = Path(ROOT_DIR / CONFIG.google_credentials_path)
    if not cred_file.exists():
        log.info("Google credentials file not found (%s); skipping", cred_file.name)
        return None

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        log.info("Google Calendar authorized; token cached to token.json")
        return creds
    except Exception as exc:  # noqa: BLE001
        log.error("Google OAuth flow failed: %s", exc)
        return None


def get_events(days: int = 7, max_events: int = 20) -> list[CalEvent]:
    """Return upcoming events for the next ``days`` days. Never raises."""
    if not CONFIG.google_enabled:
        return []

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return []

    creds = _load_credentials()
    if creds is None:
        return []

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        now = dt.datetime.now(dt.timezone.utc)
        end = now + dt.timedelta(days=days)
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_events,
            )
            .execute()
        )
        events: list[CalEvent] = []
        for item in result.get("items", []):
            events.append(_parse(item))
        log.info("fetched %d Google Calendar events", len(events))
        return [e for e in events if e is not None]
    except Exception as exc:  # noqa: BLE001
        log.error("failed to fetch Google Calendar events: %s", exc)
        return []


def _parse(item: dict) -> CalEvent | None:
    try:
        start_raw = item["start"]
        end_raw = item.get("end", {})
        all_day = "date" in start_raw
        if all_day:
            start = dt.datetime.fromisoformat(start_raw["date"])
            end = (
                dt.datetime.fromisoformat(end_raw["date"])
                if "date" in end_raw
                else None
            )
        else:
            start = dt.datetime.fromisoformat(start_raw["dateTime"])
            end = (
                dt.datetime.fromisoformat(end_raw["dateTime"])
                if "dateTime" in end_raw
                else None
            )
        return CalEvent(
            summary=item.get("summary", "(no title)"),
            start=start,
            end=end,
            location=item.get("location"),
            all_day=all_day,
            source="Google",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("could not parse event: %s", exc)
        return None
