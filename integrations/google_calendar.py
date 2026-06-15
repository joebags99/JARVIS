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

# calendar (not calendar.readonly) so we can create/edit events too.
# Existing readonly tokens will be detected as insufficient and re-authorized.
SCOPES = ["https://www.googleapis.com/auth/calendar"]
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
        # Treat missing scope info (None) as insufficient — some token files
        # don't store scopes. Also catches tokens created with calendar.readonly.
        has_sufficient_scopes = (
            bool(creds.scopes) and set(SCOPES).issubset(set(creds.scopes))
        )
        if not has_sufficient_scopes:
            log.info("[%s] token missing required scopes; re-authorizing", account_name)
            creds = None  # fall through to OAuth flow
        else:
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

            seen_ids: set[str] = set()
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
                    fetched = []
                    for item in result.get("items", []):
                        event_id = item.get("id")
                        if event_id in seen_ids:
                            continue
                        seen_ids.add(event_id)
                        parsed = _parse(item, source)
                        if parsed is not None:
                            fetched.append(parsed)
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


def create_event(
    account_name: str,
    calendar_name: str,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str | None = None,
    location: str | None = None,
) -> str:
    """Create a calendar event. Returns a human-readable status string for Claude."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return "Error: Google API libraries not installed."

    creds = _load_credentials(account_name)
    if creds is None:
        return f"Error: no valid credentials for account '{account_name}'."

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        # Resolve calendar name → ID (exact match first, then partial).
        cal_list = service.calendarList().list().execute()
        cal_id: str | None = None
        matched_name = calendar_name
        for cal in cal_list.get("items", []):
            if cal.get("summary", "").lower() == calendar_name.lower():
                cal_id = cal["id"]
                matched_name = cal.get("summary", calendar_name)
                break
        if cal_id is None:
            for cal in cal_list.get("items", []):
                if calendar_name.lower() in cal.get("summary", "").lower():
                    cal_id = cal["id"]
                    matched_name = cal.get("summary", calendar_name)
                    break
        if cal_id is None:
            available = [cal.get("summary", "?") for cal in cal_list.get("items", [])]
            return (
                f"Error: calendar '{calendar_name}' not found on account '{account_name}'. "
                f"Available calendars: {', '.join(available)}"
            )

        # All-day events use date strings ("YYYY-MM-DD"); timed events use dateTime.
        def _start_end(iso: str) -> dict:
            return {"date": iso} if len(iso) == 10 else {"dateTime": iso}

        body: dict = {
            "summary": summary,
            "start": _start_end(start_iso),
            "end": _start_end(end_iso),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        created = service.events().insert(calendarId=cal_id, body=body).execute()
        log.info(
            "[%s/%s] created event '%s' at %s (id=%s)",
            account_name, matched_name, summary, start_iso, created.get("id"),
        )
        return (
            f"Event '{summary}' created on the '{matched_name}' calendar "
            f"starting {start_iso}."
        )
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] create_event failed: %s", account_name, exc)
        return f"Error creating event: {exc}"


def update_event(
    account_name: str,
    calendar_name: str,
    event_summary: str,
    start_hint: str | None = None,
    new_summary: str | None = None,
    new_start_iso: str | None = None,
    new_end_iso: str | None = None,
    new_description: str | None = None,
    new_location: str | None = None,
) -> str:
    """Find an existing event by title and PATCH only the supplied fields.

    Returns a human-readable status string for Claude. Uses Google's PATCH
    so unmentioned fields are left untouched.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return "Error: Google API libraries not installed."

    creds = _load_credentials(account_name)
    if creds is None:
        return f"Error: no valid credentials for account '{account_name}'."

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        # Resolve calendar name → ID.
        cal_list = service.calendarList().list().execute()
        cal_id: str | None = None
        matched_cal_name = calendar_name
        for cal in cal_list.get("items", []):
            if cal.get("summary", "").lower() == calendar_name.lower():
                cal_id = cal["id"]
                matched_cal_name = cal.get("summary", calendar_name)
                break
        if cal_id is None:
            for cal in cal_list.get("items", []):
                if calendar_name.lower() in cal.get("summary", "").lower():
                    cal_id = cal["id"]
                    matched_cal_name = cal.get("summary", calendar_name)
                    break
        if cal_id is None:
            available = [cal.get("summary", "?") for cal in cal_list.get("items", [])]
            return (
                f"Error: calendar '{calendar_name}' not found on account '{account_name}'. "
                f"Available: {', '.join(available)}"
            )

        # Search a wide window so we catch recent and upcoming events.
        now = dt.datetime.now(dt.timezone.utc)
        result = service.events().list(
            calendarId=cal_id,
            timeMin=(now - dt.timedelta(days=7)).isoformat(),
            timeMax=(now + dt.timedelta(days=90)).isoformat(),
            singleEvents=True,
            q=event_summary,
        ).execute()

        matches = [
            item for item in result.get("items", [])
            if event_summary.lower() in item.get("summary", "").lower()
        ]

        if not matches:
            return (
                f"Error: no event matching '{event_summary}' found on the "
                f"'{matched_cal_name}' calendar in the next 90 days."
            )

        if len(matches) > 1 and start_hint is None:
            options = "; ".join(
                f"'{m.get('summary')}' on "
                f"{m['start'].get('dateTime', m['start'].get('date', '?'))}"
                for m in matches[:5]
            )
            return (
                f"Found {len(matches)} events matching '{event_summary}'. "
                f"Specify which one with a start_hint date. Options: {options}"
            )

        target = matches[0]
        if start_hint and len(matches) > 1:
            hint_date = start_hint[:10]
            for m in matches:
                event_dt = m["start"].get("dateTime", m["start"].get("date", ""))
                if event_dt.startswith(hint_date):
                    target = m
                    break

        # Build a partial patch — only include fields the caller specified.
        patch: dict = {}
        if new_summary is not None:
            patch["summary"] = new_summary
        if new_description is not None:
            patch["description"] = new_description
        if new_location is not None:
            patch["location"] = new_location

        def _start_end(iso: str) -> dict:
            return {"date": iso} if len(iso) == 10 else {"dateTime": iso}

        if new_start_iso is not None:
            patch["start"] = _start_end(new_start_iso)
        if new_end_iso is not None:
            patch["end"] = _start_end(new_end_iso)

        if not patch:
            return "Error: no fields to update were specified."

        service.events().patch(
            calendarId=cal_id,
            eventId=target["id"],
            body=patch,
        ).execute()

        display_name = new_summary or target.get("summary", event_summary)
        log.info(
            "[%s/%s] updated event '%s' (id=%s)",
            account_name, matched_cal_name, display_name, target["id"],
        )
        return f"Event '{display_name}' updated on the '{matched_cal_name}' calendar."

    except Exception as exc:  # noqa: BLE001
        log.error("[%s] update_event failed: %s", account_name, exc)
        return f"Error updating event: {exc}"


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
