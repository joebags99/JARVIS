"""Outlook / Microsoft Graph calendar integration.

Uses MSAL device-code or interactive auth with an on-disk token cache
(``.msal_cache.bin``, gitignored). Falls back silently to no events if the app
isn't configured or auth fails.
"""

from __future__ import annotations

import datetime as dt

import requests

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger
from integrations.google_calendar import CalEvent  # reuse normalized event type

log = get_logger("outlook_cal")

SCOPES = ["Calendars.Read"]
CACHE_PATH = ROOT_DIR / ".msal_cache.bin"
GRAPH = "https://graph.microsoft.com/v1.0"


def _load_cache():
    from msal import SerializableTokenCache

    cache = SerializableTokenCache()
    if CACHE_PATH.exists():
        try:
            cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read MSAL cache: %s", exc)
    return cache


def _save_cache(cache) -> None:
    if cache.has_state_changed:
        try:
            CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist MSAL cache: %s", exc)


def _acquire_token() -> str | None:
    try:
        from msal import PublicClientApplication
    except ImportError:
        log.warning("msal not installed; skipping Outlook")
        return None

    cache = _load_cache()
    authority = f"https://login.microsoftonline.com/{CONFIG.outlook_tenant_id}"
    app = PublicClientApplication(
        CONFIG.outlook_client_id, authority=authority, token_cache=cache
    )

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        # Device-code flow: prints a URL + code to the log/console for first auth.
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            log.error("failed to start Outlook device flow: %s", flow.get("error"))
            return None
        log.info("Outlook auth required: %s", flow.get("message"))
        print(flow.get("message"))  # surfaces the device-code instructions
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if result and "access_token" in result:
        return result["access_token"]
    log.error("Outlook token acquisition failed: %s", result.get("error_description") if result else "no result")
    return None


def get_events(days: int = 7, max_events: int = 20) -> list[CalEvent]:
    """Return upcoming Outlook events. Never raises."""
    if not CONFIG.outlook_enabled:
        return []

    token = _acquire_token()
    if not token:
        return []

    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(days=days)
    params = {
        "startDateTime": now.isoformat(),
        "endDateTime": end.isoformat(),
        "$orderby": "start/dateTime",
        "$top": str(max_events),
        "$select": "subject,start,end,location,isAllDay",
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.timezone="UTC"',
    }

    try:
        resp = requests.get(
            f"{GRAPH}/me/calendarview", headers=headers, params=params, timeout=15
        )
        resp.raise_for_status()
        items = resp.json().get("value", [])
        events = [e for e in (_parse(i) for i in items) if e is not None]
        log.info("fetched %d Outlook events", len(events))
        return events
    except Exception as exc:  # noqa: BLE001
        log.error("failed to fetch Outlook events: %s", exc)
        return []


def _parse(item: dict) -> CalEvent | None:
    try:
        all_day = item.get("isAllDay", False)
        start = dt.datetime.fromisoformat(item["start"]["dateTime"].split(".")[0])
        end = dt.datetime.fromisoformat(item["end"]["dateTime"].split(".")[0])
        loc = (item.get("location") or {}).get("displayName") or None
        return CalEvent(
            summary=item.get("subject", "(no title)"),
            start=start,
            end=end,
            location=loc,
            all_day=all_day,
            source="Outlook",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("could not parse Outlook event: %s", exc)
        return None
