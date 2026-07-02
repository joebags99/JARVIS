"""Always-on ambient HUD — a small, persistent, always-on-top second window.

Shows a next-meeting countdown, current weather, and the unread proactive-
notification count (the bell icon in app/overlay.py). Opt-in
(JARVIS_HUD_ENABLED, off by default, matching every other optional feature).

Mirrors the screenshot selector's "second pywebview window" pattern in
app/overlay.py (its own tiny JS-API class, its own webview.create_window()
call), except this window is PERSISTENT — created once at startup and shown/
hidden repeatedly — rather than transient-per-action.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import Callable

from .config import CONFIG
from .logging_setup import get_logger
from .screen import enable_real_transparency, screen_size

log = get_logger("hud")

_UI_DIR = Path(__file__).resolve().parent.parent / "assets" / "ui"
_HUD_HTML = _UI_DIR / "hud.html"

HUD_W = 260
HUD_H = 76
MARGIN = 16
HUD_ALPHA = 235  # near-opaque layered window; the glass look comes from hud.html's CSS

# A meeting countdown doesn't need minute-perfect precision, and refreshing
# calendar sources every tick would double Google Calendar API traffic for
# anyone who also has JARVIS_MEETING_ALERTS on (which polls the same sources
# on its own 60s cadence) — so the HUD uses its own, coarser cadences.
CALENDAR_REFRESH_SECONDS = 300  # 5 min
WEATHER_REFRESH_SECONDS = 900  # 15 min — conditions don't change minute to minute


def _format_countdown(event) -> str:
    """'Standup in 12m' / 'Standup in 2h 5m' / 'Standup — now' / no-event text."""
    if event is None:
        return "No upcoming meetings"
    now = dt.datetime.now(event.start.tzinfo) if event.start.tzinfo else dt.datetime.now()
    delta = event.start - now
    if delta.total_seconds() <= 0:
        return f"{event.summary} — now"
    total_min = int(delta.total_seconds() // 60)
    if total_min < 60:
        return f"{event.summary} in {total_min}m"
    hours, mins = divmod(total_min, 60)
    return f"{event.summary} in {hours}h {mins}m"


def _weather_due(now_mono: float, next_fetch: float) -> bool:
    return now_mono >= next_fetch


class _HudApi:
    """Methods callable from hud.html via ``window.pywebview.api.<name>()``."""

    def __init__(
        self, on_click: Callable[[], None], on_drag: Callable[[float, float], None],
    ) -> None:
        self._on_click = on_click
        self._on_drag = on_drag

    def open_overlay(self) -> None:
        self._on_click()

    def move_window(self, dx: float, dy: float) -> None:
        self._on_drag(dx, dy)


class Hud:
    """Owns the HUD's window + background refresh loop.

    __init__ is side-effect-free (no webview import, no window/thread) so it
    can always be constructed unconditionally — matching
    ProactiveScheduler's/WakeWordListener's "construct freely, start() is
    the real no-op-if-disabled gate" convention — and so tests can build a
    bare instance via __new__ without pywebview installed, the same bypass
    already used for Overlay/WakeWordListener/Speaker elsewhere in this
    suite.
    """

    def __init__(self, on_click: Callable[[], None]) -> None:
        self._on_click = on_click
        self.window = None
        self._visible = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_weather_fetch = 0.0
        self._win_x = 0
        self._win_y = 0

    def start(self) -> None:
        """Create the window and start the refresh loop. No-op if disabled."""
        if not CONFIG.hud_enabled:
            return
        import webview

        self._win_x, self._win_y = self._initial_position()
        api = _HudApi(on_click=self._on_click, on_drag=self._drag_move)
        self.window = webview.create_window(
            "JARVIS HUD",
            url=str(_HUD_HTML),
            js_api=api,
            width=HUD_W,
            height=HUD_H,
            x=self._win_x,
            y=self._win_y,
            frameless=True,
            on_top=True,
            transparent=True,
            background_color="#0f0f0f",
            resizable=False,
            # The whole card is a drag handle (own mousedown/mousemove/mouseup
            # in hud.html, same pattern as the main overlay's header drag) so
            # a stationary click can still open the overlay while a real drag
            # repositions the window — pywebview's blanket easy_drag can't
            # tell those apart, so it stays off here too.
            easy_drag=False,
        )
        self.window.events.loaded += self._on_loaded
        self._visible = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("HUD started (position=%s)", CONFIG.hud_position)

    def _initial_position(self) -> tuple[int, int]:
        sw, sh = screen_size()
        pos = CONFIG.hud_position
        x = sw - HUD_W - MARGIN if "right" in pos else MARGIN
        y = sh - HUD_H - MARGIN if "bottom" in pos else MARGIN
        return x, y

    def _on_loaded(self) -> None:
        # Unlike Overlay, the HUD is meant to be visible immediately — no
        # show-then-hide dance, just wire up real transparency once loaded.
        enable_real_transparency("JARVIS HUD", HUD_ALPHA)

    def _eval(self, fn_name: str, *args) -> None:
        try:
            arg_str = ", ".join(json.dumps(a) for a in args)
            self.window.evaluate_js(f"{fn_name}({arg_str})")
        except Exception as exc:  # noqa: BLE001
            log.debug("HUD evaluate_js(%s) failed: %s", fn_name, exc)

    def _drag_move(self, dx: float, dy: float) -> None:
        """Reposition by a screen-pixel delta — called from hud.html's own
        mousedown/mousemove drag tracking, same pattern as the main
        overlay's header drag (Overlay._drag_move)."""
        self._win_x += int(dx)
        self._win_y += int(dy)
        try:
            self.window.move(self._win_x, self._win_y)
        except Exception as exc:  # noqa: BLE001
            log.debug("HUD window.move failed: %s", exc)

    # ── external hooks (wired by main.py) ────────────────────────────────────

    def set_unread_count(self, count: int) -> None:
        self._eval("setUnread", count)

    def on_overlay_visibility_changed(self, overlay_visible: bool) -> None:
        """Auto-hide the HUD while the main chat overlay is shown, reappear
        once it's hidden again — avoids the two windows visually competing
        for the same corner."""
        if self.window is None:
            return
        if overlay_visible and self._visible:
            self.window.hide()
            self._visible = False
        elif not overlay_visible and not self._visible:
            self.window.show()
            self._visible = True

    # ── refresh loop ──────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.debug("HUD refresh tick failed: %s", exc)
            self._stop.wait(CALENDAR_REFRESH_SECONDS)

    def _tick(self) -> None:
        from integrations.calendar_sources import get_all_events, next_event

        events = get_all_events(days=1, max_events=20)
        nxt = next_event(events, dt.datetime.now(dt.timezone.utc))
        self._eval("setMeeting", _format_countdown(nxt))

        now_mono = time.monotonic()
        if _weather_due(now_mono, self._next_weather_fetch):
            from integrations.weather import get_current_compact

            self._eval("setWeather", get_current_compact())
            self._next_weather_fetch = now_mono + WEATHER_REFRESH_SECONDS

    def stop(self) -> None:
        self._stop.set()

    def destroy(self) -> None:
        self.stop()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.window = None
