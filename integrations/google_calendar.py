"""Google Calendar integration.

Supports multiple Google accounts, each with its own OAuth token cached under
``tokens/google/{account}.json`` (gitignored). Accounts are configured via the
``GOOGLE_ACCOUNTS`` env var (comma-separated, e.g. ``personal,work``).

First run for each account opens a browser for OAuth2 consent. If credentials
are missing or auth fails for an account, that account is skipped and JARVIS
keeps working with the remaining accounts.
"""

from __future__ import annotations

import datetime as dt
import inspect
from dataclasses import dataclass
from pathlib import Path

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger

log = get_logger("google_cal")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_DIR = ROOT_DIR / "tokens" / "google"


@dataclass
class CalEvent:
    """A normalized calendar event, source-agnostic."""

    summary: str
    start: dt.datetime
    end: dt.datetime | None
    location: str | None
    all_day: bool
    source: str  # e.g. "Google/personal", "Google/work", "Outlook"

    def format_line(self) -> str:
        when = self.start.strftime("%a %b %d") if self.all_day else self.start.strftime(
            "%a %b %d %I:%M %p"
        )
        loc = f" @ {self.location}" if self.location else ""
        return f"- {when} — {self.summary}{loc} [{self.source}]"


def _load_credentials(account_name: str):
    """Load cached creds for *account_name*, refreshing or re-running OAuth as needed."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        log.warning("google api libraries not installed; skipping Google Calendar")
        return None

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKEN_DIR / f"{account_name}.json"

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] could not read token file: %s", account_name, exc)

    if creds and creds.valid:
        log.debug("[%s] Google credentials valid", account_name)
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            log.info("[%s] Google credentials refreshed", account_name)
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] token refresh failed, re-authenticating: %s", account_name, exc)

    cred_file = ROOT_DIR / CONFIG.google_credentials_path
    if not cred_file.exists():
        log.info(
            "[%s] Google credentials file not found (%s); skipping",
            account_name,
            cred_file.name,
        )
        return None

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
        # prompt="consent" forces account-chooser so multi-account flows work cleanly.
        # Fall back silently if this version of google-auth-oauthlib doesn't support it;
        # in that case use an incognito window or the account-switcher in the browser.
        sig = inspect.signature(flow.run_local_server)
        if "prompt" in sig.parameters:
            creds = flow.run_local_server(port=0, prompt="consent")
        else:
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        log.info("[%s] Google Calendar authorized; token cached to %s", account_name, token_path)
        return creds
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] Google OAuth flow failed: %s", account_name, exc)
        return None


def get_events(days: int = 7, max_events: int = 20) -> list[CalEvent]:
    """Return upcoming events across all configured Google accounts. Never raises."""
    if not CONFIG.google_enabled:
        return []

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return []

    accounts: list[str] = getattr(CONFIG, "google_accounts", None) or ["default"]

    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(days=days)
    all_events: list[CalEvent] = []

    for account_name in accounts:
        creds = _load_credentials(account_name)
        if creds is None:
            log.warning("[%s] skipping — no valid credentials", account_name)
            continue

        try:
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)

            cal_list = service.calendarList().list().execute()
            calendars = cal_list.get("items", [])
            log.info("[%s] found %d calendars", account_name, len(calendars))

            for cal in calendars:
                cal_id = cal["id"]
                cal_name = cal.get("summary", cal_id)
                source = f"Google/{account_name}/{cal_name}"
                try:
                    result = (
                        service.events()
                        .list(
                            calendarId=cal_id,
                            timeMin=now.isoformat(),
                            timeMax=end.isoformat(),
                            singleEvents=True,
                            orderBy="startTime",
                            maxResults=max_events,
                        )
                        .execute()
                    )
                    fetched = [
                        parsed
                        for item in result.get("items", [])
                        if (parsed := _parse(item, source)) is not None
                    ]
                    if fetched:
                        log.info("[%s/%s] fetched %d events", account_name, cal_name, len(fetched))
                    all_events.extend(fetched)
                except Exception as exc:  # noqa: BLE001
                    log.error("[%s/%s] failed to fetch events: %s", account_name, cal_name, exc)

        except Exception as exc:  # noqa: BLE001
            log.error("[%s] failed to list calendars: %s", account_name, exc)

    def _sort_key(e: CalEvent) -> dt.datetime:
        if e.start.tzinfo is None:
            return e.start.replace(tzinfo=dt.timezone.utc)
        return e.start

    all_events.sort(key=_sort_key)
    return all_events[:max_events]


def _parse(item: dict, source: str = "Google") -> CalEvent | None:
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
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("could not parse event: %s", exc)
        return None
