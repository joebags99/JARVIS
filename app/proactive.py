"""Proactive scheduler — JARVIS acts without being asked.

A lightweight background daemon thread (no extra dependency) that ticks once a
minute and runs four opt-in jobs:

  * **Scheduled briefing** — fires the daily briefing once at a configured time.
  * **Meeting alerts** — "<title> starts in N min" before calendar events.
  * **Important-email pings** — a balloon when high-signal unread mail arrives.
  * **Vault callbacks** — a nudge when a Sessions/ note's Action Items/Open
    Questions have sat untouched for a few days.

The side-effecting work (fetching events/mail/vault notes, showing
notifications, triggering the briefing) is injected as callables so the
decision logic can be unit-tested with fakes. All scheduling *decisions* —
quiet hours, "is the briefing due", "is a meeting close enough", "is a
callback stale enough", dedup — are pure module functions below.

Everything respects ``CONFIG``: nothing runs unless ``proactive_enabled`` is
set, each job is individually gated, and quiet hours suppress the interrupting
alerts (but not the explicitly-scheduled briefing). The job is to be helpful,
never naggy: each meeting is alerted once, each email pinged once, each vault
callback nudged once ever, and the briefing fires once per day.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from typing import Callable

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("proactive")

TICK_SECONDS = 60
# Only fire the scheduled briefing within this many minutes after its target
# time, so launching the app at noon doesn't replay a 7:30 AM briefing.
BRIEFING_WINDOW_MIN = 30
# Cap the remembered email-ping ids so the dedup set can't grow without bound.
_MAX_PINGED_IDS = 500


# ── Pure decision helpers ─────────────────────────────────────────────────────

def parse_hhmm(value: str) -> dt.time | None:
    """Parse 'HH:MM' (24h) into a time, or None if blank/invalid."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        hh, mm = value.split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def parse_quiet_window(value: str) -> tuple[dt.time, dt.time] | None:
    """Parse 'HH:MM-HH:MM' into a (start, end) pair, or None if blank/invalid."""
    value = (value or "").strip()
    if "-" not in value:
        return None
    start_s, end_s = value.split("-", 1)
    start, end = parse_hhmm(start_s), parse_hhmm(end_s)
    if start is None or end is None:
        return None
    return start, end


def in_quiet_hours(now_t: dt.time, window: tuple[dt.time, dt.time] | None) -> bool:
    """Whether ``now_t`` falls inside a quiet-hours window (handles wrap past midnight)."""
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= now_t < end
    # Wraps midnight, e.g. 22:00–07:00.
    return now_t >= start or now_t < end


def briefing_due(
    now: dt.datetime,
    target: dt.time | None,
    last_date: dt.date | None,
    window_min: int = BRIEFING_WINDOW_MIN,
) -> bool:
    """True when the daily briefing should fire now (once per day, within window)."""
    if target is None or last_date == now.date():
        return False
    target_today = dt.datetime.combine(now.date(), target)
    minutes_since = (now - target_today).total_seconds() / 60.0
    return 0 <= minutes_since <= window_min


def _event_start_naive(start) -> dt.datetime:
    """Normalize a (possibly tz-aware) event start to naive local time for comparison."""
    if getattr(start, "tzinfo", None) is not None:
        return start.astimezone().replace(tzinfo=None)
    return start


@dataclass
class MeetingAlert:
    key: str
    summary: str
    minutes: int


def meeting_alerts_due(
    events, now: dt.datetime, lead_min: int, already: set[str]
) -> list[MeetingAlert]:
    """Events starting within ``lead_min`` minutes that haven't been alerted yet.

    Skips all-day events and anything already started. Dedup key is summary+start
    (CalEvent has no stable id), so the same occurrence is only alerted once.
    """
    out: list[MeetingAlert] = []
    for e in events:
        if getattr(e, "all_day", False):
            continue
        try:
            start = _event_start_naive(e.start)
            minutes = (start - now).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            continue
        if not (0 <= minutes <= lead_min):
            continue
        key = f"{e.summary}@{start.isoformat()}"
        if key not in already:
            out.append(MeetingAlert(key=key, summary=e.summary, minutes=int(round(minutes))))
    return out


def short_sender(sender: str) -> str:
    """Trim a 'Name <addr@x>' From header down to the display name (or address)."""
    sender = (sender or "").strip()
    if "<" in sender:
        name = sender.split("<", 1)[0].strip().strip('"')
        if name:
            return name
    return sender or "(unknown sender)"


def callback_due(meta: dict, threshold_days: int, today: dt.date) -> bool:
    """True when a vault note's open items are stale enough to nudge about.

    Fires once per note: a note already stamped ``callback_nudged`` (see
    ``integrations.obsidian.mark_callback_nudged``) never fires again. A
    missing/malformed date is treated as not-due rather than raising, since a
    vault note's frontmatter isn't guaranteed clean.
    """
    if meta.get("callback_nudged"):
        return False
    updated = meta.get("updated") or meta.get("created")
    if not updated:
        return False
    try:
        updated_date = dt.date.fromisoformat(str(updated))
    except ValueError:
        return False
    return (today - updated_date).days >= threshold_days


# ── Scheduler ─────────────────────────────────────────────────────────────────

class ProactiveScheduler:
    """Background ticker driving the proactive jobs. Best-effort throughout."""

    def __init__(
        self,
        *,
        notify: Callable[[str, str], None],
        briefing: Callable[[], None],
        fetch_events: Callable[[], list],
        fetch_email: Callable[[], list[dict]],
        fetch_vault_items: Callable[[], list[tuple[str, dict, list[str]]]] = lambda: [],
        mark_vault_nudged: Callable[[str], None] = lambda rel: None,
        refresh_vault_callbacks: Callable[[], None] = lambda: None,
        tick_seconds: int = TICK_SECONDS,
    ) -> None:
        self._notify = notify
        self._briefing = briefing
        self._fetch_events = fetch_events
        self._fetch_email = fetch_email
        self._fetch_vault_items = fetch_vault_items
        self._mark_vault_nudged = mark_vault_nudged
        self._refresh_vault_callbacks = refresh_vault_callbacks
        self._tick_seconds = tick_seconds

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._briefing_last_date: dt.date | None = None
        self._alerted_events: set[str] = set()
        self._alerted_events_date: dt.date | None = None
        self._pinged_email_ids: set[str] = set()
        self._email_seeded = False

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not CONFIG.proactive_enabled:
            log.info("proactive features disabled (set JARVIS_PROACTIVE_ENABLED=true)")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(
            "proactive scheduler started (briefing=%s, meeting_alerts=%s, "
            "email_alerts=%s, vault_callbacks=%s)",
            CONFIG.briefing_time or "off", CONFIG.meeting_alerts, CONFIG.email_alerts,
            CONFIG.vault_callbacks_enabled,
        )

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(dt.datetime.now())
            except Exception as exc:  # noqa: BLE001
                log.error("proactive tick failed: %s", exc)
            self._stop.wait(self._tick_seconds)

    # ── One tick (pure-ish: reads CONFIG + injected fetchers) ────────────────
    def tick(self, now: dt.datetime) -> None:
        if not CONFIG.proactive_enabled:
            return
        self._maybe_briefing(now)
        # Quiet hours suppress the interrupting alerts, but not the briefing the
        # user explicitly scheduled for a specific time.
        if in_quiet_hours(now.time(), parse_quiet_window(CONFIG.quiet_hours)):
            return
        self._maybe_meeting_alerts(now)
        self._maybe_email_pings(now)
        self._maybe_vault_callbacks(now)

    def _maybe_briefing(self, now: dt.datetime) -> None:
        target = parse_hhmm(CONFIG.briefing_time)
        if not briefing_due(now, target, self._briefing_last_date):
            return
        self._briefing_last_date = now.date()
        log.info("firing scheduled daily briefing")
        try:
            self._briefing()
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled briefing failed: %s", exc)

    def _maybe_meeting_alerts(self, now: dt.datetime) -> None:
        if not CONFIG.meeting_alerts:
            return
        # Reset the per-day dedup set so tomorrow's meetings can alert.
        if self._alerted_events_date != now.date():
            self._alerted_events.clear()
            self._alerted_events_date = now.date()
        try:
            events = self._fetch_events() or []
        except Exception as exc:  # noqa: BLE001
            log.error("meeting-alert calendar fetch failed: %s", exc)
            return
        for alert in meeting_alerts_due(events, now, CONFIG.meeting_lead_min, self._alerted_events):
            self._alerted_events.add(alert.key)
            when = f"in {alert.minutes} min" if alert.minutes > 0 else "now"
            self._notify("Upcoming meeting", f"{alert.summary} starts {when}.")

    def _maybe_email_pings(self, now: dt.datetime) -> None:
        if not (CONFIG.email_alerts and CONFIG.gmail_available):
            return
        try:
            messages = self._fetch_email() or []
        except Exception as exc:  # noqa: BLE001
            log.error("email-ping fetch failed: %s", exc)
            return
        # First poll just seeds the seen-set: don't blast a ping for every email
        # that was already sitting unread when JARVIS started.
        if not self._email_seeded:
            self._pinged_email_ids.update(m.get("id") for m in messages if m.get("id"))
            self._email_seeded = True
            return
        for m in messages:
            mid = m.get("id")
            if not mid or mid in self._pinged_email_ids:
                continue
            self._pinged_email_ids.add(mid)
            self._notify(
                "Important email",
                f"{short_sender(m.get('sender', ''))}: {m.get('subject', '(no subject)')}",
            )
        if len(self._pinged_email_ids) > _MAX_PINGED_IDS:
            # Drop the oldest-ish half; exact order doesn't matter for dedup.
            self._pinged_email_ids = set(list(self._pinged_email_ids)[-_MAX_PINGED_IDS // 2:])

    def _maybe_vault_callbacks(self, now: dt.datetime) -> None:
        if not (CONFIG.vault_callbacks_enabled and CONFIG.obsidian_available):
            return
        try:
            items = self._fetch_vault_items() or []
        except Exception as exc:  # noqa: BLE001
            log.error("vault-callback fetch failed: %s", exc)
            return
        for rel, meta, open_items in items:
            if not callback_due(meta, CONFIG.vault_callback_days, now.date()):
                continue
            title = meta.get("title") or rel
            preview = open_items[0]
            extra = f" (+{len(open_items) - 1} more)" if len(open_items) > 1 else ""
            self._notify("Still open?", f"{title}: {preview}{extra}")
            try:
                self._mark_vault_nudged(rel)
            except Exception as exc:  # noqa: BLE001
                log.error("could not mark vault callback nudged for %s: %s", rel, exc)
        # Refreshed every tick (not just when something newly went stale) so
        # the note also reflects items resolved/checked-off directly in
        # Obsidian, not only JARVIS's own writes — a plain vault scan, cheap
        # enough to run alongside the fetch above.
        try:
            self._refresh_vault_callbacks()
        except Exception as exc:  # noqa: BLE001
            log.error("could not refresh vault callbacks note: %s", exc)
