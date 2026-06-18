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
from zoneinfo import ZoneInfo

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger
from integrations import google_api

log = get_logger("google_cal")

# calendar (not calendar.readonly) so we can create/edit events too.
# Existing readonly tokens will be detected as insufficient and re-authorized.
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_DIR = ROOT_DIR / "tokens" / "google"

_cached_timezone: str | None = None


def _default_timezone() -> str:
    """Best-effort IANA zone name to attach when Claude omits a UTC offset.

    Never raises — falls back to UTC so a missing/failed tz lookup can't
    block event creation. Resolution order: explicit override ->
    auto-detected system zone -> "UTC".
    """
    global _cached_timezone
    if _cached_timezone is not None:
        return _cached_timezone
    if CONFIG.jarvis_timezone:
        _cached_timezone = CONFIG.jarvis_timezone
        return _cached_timezone
    try:
        from tzlocal import get_localzone_name
        _cached_timezone = get_localzone_name()
    except Exception:  # noqa: BLE001
        _cached_timezone = "UTC"
    return _cached_timezone


def _user_tz() -> dt.tzinfo:
    """The user's timezone as a (DST-aware) tzinfo for localizing naive times.

    JARVIS_TIMEZONE override → the system's local zone (tzlocal) → the current
    local fixed offset. Used to turn a bare wall-clock time the model gives
    (e.g. '22:00:00' for "10pm") into a properly offset time.
    """
    name = CONFIG.jarvis_timezone
    if name:
        try:
            return ZoneInfo(name)
        except Exception as exc:  # noqa: BLE001
            log.warning("invalid JARVIS_TIMEZONE %r: %s", name, exc)
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except Exception:  # noqa: BLE001
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _start_end(iso: str, force_timezone: bool = False) -> dict:
    """Build a start/end dict for the Calendar API from an ISO date/datetime.

    The model is asked to pass the user's local wall-clock time, but it's
    unreliable about timezones — it often sends a naive time, or worse stamps a
    local time with 'Z'/+00:00 without actually converting it (that's the
    "10pm shows as 6pm" bug: 22:00 read as UTC = 18:00 Eastern). So we treat a
    naive OR UTC-stamped time as the user's *local wall-clock* time and attach
    the real local offset/zone ourselves. A genuine non-UTC offset (an explicit
    "10pm Pacific") is honored as given.

    Google also requires a timeZone on recurring events, so for those we emit
    the wall-clock time plus an IANA timeZone (its recommended form).
    """
    if len(iso) == 10:
        return {"date": iso}

    parsed = dt.datetime.fromisoformat(iso)
    # A naive time, or a UTC offset the model almost certainly didn't mean, is
    # reinterpreted as the user's local wall-clock time.
    if parsed.tzinfo is None or parsed.utcoffset() == dt.timedelta(0):
        wall = parsed.replace(tzinfo=None)
        if force_timezone:  # recurring → wall time + IANA zone
            return {"dateTime": wall.isoformat(), "timeZone": _default_timezone()}
        local = wall.replace(tzinfo=_user_tz())
        return {"dateTime": local.isoformat()}

    # Already carries a real (non-UTC) offset — trust it.
    if force_timezone:
        return {"dateTime": iso, "timeZone": _default_timezone()}
    return {"dateTime": iso}


def _derive_end_iso(target: dict, new_start_iso: str) -> str:
    """Pick an end that's consistent with a newly-set start.

    When the caller moves an event's start but doesn't give a new end, Google
    rejects a mismatched pair (e.g. a timed start with an all-day end → "Invalid
    start time"). Derive the end here: preserve the original duration when the
    start's all-day/timed nature is unchanged, otherwise fall back to a sensible
    default (1 hour for timed, 1 day for all-day) so all-day↔timed conversions
    don't blow up.
    """
    start_raw = target.get("start", {})
    end_raw = target.get("end", {})
    new_all_day = len(new_start_iso) == 10

    if new_all_day:
        span_days = 1
        try:
            s = dt.date.fromisoformat(start_raw["date"])
            e = dt.date.fromisoformat(end_raw["date"])
            span_days = (e - s).days or 1
        except Exception:  # noqa: BLE001
            pass
        new_start = dt.date.fromisoformat(new_start_iso)
        return (new_start + dt.timedelta(days=span_days)).isoformat()

    duration = dt.timedelta(hours=1)
    try:
        s = dt.datetime.fromisoformat(start_raw["dateTime"])
        e = dt.datetime.fromisoformat(end_raw["dateTime"])
        if e > s:
            duration = e - s
    except Exception:  # noqa: BLE001
        pass
    new_start = dt.datetime.fromisoformat(new_start_iso)
    return (new_start + duration).isoformat()


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

            cal_list = google_api.execute(
                service.calendarList().list(), label="calendar.calendarList.list"
            )
            calendars = cal_list.get("items", [])
            log.info("[%s] found %d calendars", account_name, len(calendars))

            seen_ids: set[str] = set()
            for cal in calendars:
                cal_id = cal["id"]
                cal_name = cal.get("summary", cal_id)
                source = f"Google/{account_name}/{cal_name}"
                try:
                    result = google_api.execute(
                        service.events().list(
                            calendarId=cal_id,
                            timeMin=now.isoformat(),
                            timeMax=end.isoformat(),
                            singleEvents=True,
                            orderBy="startTime",
                            maxResults=max_events,
                        ),
                        label="calendar.events.list",
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


def _build_rrule(
    recurrence_freq: str,
    recurrence_count: int | None,
    recurrence_until: str | None,
    recurrence_interval: int | None = None,
) -> str:
    """Build an RFC 5545 RRULE from structured fields. Raises ValueError if invalid.

    Built deterministically in Python rather than asked of Claude — same
    reasoning as the Todoist due-date handling: date/recurrence math should
    never depend on LLM arithmetic.
    """
    if bool(recurrence_count) == bool(recurrence_until):
        raise ValueError(
            "exactly one of recurrence_count or recurrence_until is required "
            "when recurrence_freq is set"
        )
    rrule = f"RRULE:FREQ={recurrence_freq}"
    if recurrence_interval and recurrence_interval > 1:
        rrule += f";INTERVAL={recurrence_interval}"
    if recurrence_count:
        return f"{rrule};COUNT={recurrence_count}"
    until = dt.date.fromisoformat(recurrence_until).strftime("%Y%m%dT000000Z")
    return f"{rrule};UNTIL={until}"


def create_event(
    account_name: str,
    calendar_name: str,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str | None = None,
    location: str | None = None,
    recurrence_freq: str | None = None,
    recurrence_count: int | None = None,
    recurrence_until: str | None = None,
    recurrence_interval: int | None = None,
) -> str:
    """Create a calendar event. Returns a human-readable status string for Claude."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return "Error: Google API libraries not installed."

    rrule: str | None = None
    if recurrence_freq:
        try:
            rrule = _build_rrule(
                recurrence_freq, recurrence_count, recurrence_until, recurrence_interval
            )
        except ValueError as exc:
            return f"Error: {exc}"

    creds = _load_credentials(account_name)
    if creds is None:
        return f"Error: no valid credentials for account '{account_name}'."

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        # Resolve calendar name → ID (exact match first, then partial).
        cal_list = google_api.execute(
            service.calendarList().list(), label="calendar.calendarList.list"
        )
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

        body: dict = {
            "summary": summary,
            "start": _start_end(start_iso, force_timezone=bool(rrule)),
            "end": _start_end(end_iso, force_timezone=bool(rrule)),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if rrule:
            body["recurrence"] = [rrule]

        created = google_api.execute(
            service.events().insert(calendarId=cal_id, body=body),
            idempotent=False,  # creating twice would duplicate the event
            label="calendar.events.insert",
        )
        recur_note = ""
        if rrule:
            unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(
                recurrence_freq, recurrence_freq.lower()
            )
            freq_note = (
                f"every {recurrence_interval} {unit}s"
                if recurrence_interval and recurrence_interval > 1
                else recurrence_freq
            )
            if recurrence_count:
                recur_note = f" repeating {freq_note}, {recurrence_count} times"
            elif recurrence_until:
                recur_note = f" repeating {freq_note} until {recurrence_until}"
        log.info(
            "[%s/%s] created event '%s' at %s%s (id=%s)",
            account_name, matched_name, summary, start_iso, recur_note, created.get("id"),
        )
        return (
            f"Event '{summary}' created on the '{matched_name}' calendar "
            f"starting {start_iso}{recur_note}."
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

    Known limitation: no concept of editing a recurring series (single
    instance vs. "this and following" vs. "all") — that needs
    recurringEventId/originalStartTime handling on instances, which isn't
    implemented here.
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
        cal_list = google_api.execute(
            service.calendarList().list(), label="calendar.calendarList.list"
        )
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
        result = google_api.execute(
            service.events().list(
                calendarId=cal_id,
                timeMin=(now - dt.timedelta(days=7)).isoformat(),
                timeMax=(now + dt.timedelta(days=90)).isoformat(),
                singleEvents=True,
                q=event_summary,
            ),
            label="calendar.events.list",
        )

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

        if new_start_iso is not None:
            patch["start"] = _start_end(new_start_iso)
            # Always send a matching end — if the caller didn't supply one,
            # derive it so we never pair a timed start with an all-day end (or
            # vice versa), which Google rejects as "Invalid start time".
            end_iso = new_end_iso if new_end_iso is not None else _derive_end_iso(
                target, new_start_iso
            )
            patch["end"] = _start_end(end_iso)
        elif new_end_iso is not None:
            patch["end"] = _start_end(new_end_iso)

        if not patch:
            return "Error: no fields to update were specified."

        # PATCH with the same body is idempotent — re-applying yields the same
        # result, so a retry is safe.
        google_api.execute(
            service.events().patch(
                calendarId=cal_id,
                eventId=target["id"],
                body=patch,
            ),
            label="calendar.events.patch",
        )

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
