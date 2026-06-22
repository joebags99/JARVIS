"""System tray icon and menu.

Runs ``pystray`` on a background thread (the webview's event loop owns the
main thread). Menu callbacks go through ``schedule`` so tray-thread code never
touches the UI directly. The icon image swaps with JARVIS's state
(idle / listening / thinking).
"""

from __future__ import annotations

import threading
from typing import Callable

from .icon import load_icon
from .logging_setup import get_logger

log = get_logger("tray")


class Tray:
    def __init__(
        self,
        on_open: Callable[[], None],
        on_reload: Callable[[], None],
        on_quit: Callable[[], None],
        schedule: Callable[[Callable[[], None]], None],
        on_settings: Callable[[], None] | None = None,
        on_briefing: Callable[[], None] | None = None,
        on_tts_toggle: Callable[[], None] | None = None,
        tts_state: Callable[[], bool] | None = None,
    ) -> None:
        self._on_open = on_open
        self._on_reload = on_reload
        self._on_quit = on_quit
        self._on_settings = on_settings
        self._on_briefing = on_briefing
        self._on_tts_toggle = on_tts_toggle
        self._tts_state = tts_state
        self._schedule = schedule
        self._icon = None
        self._thread = None

    def _build(self):
        import pystray
        from pystray import MenuItem as Item, Menu

        def wrap(fn):
            return lambda _icon=None, _item=None: self._schedule(fn)

        menu = Menu(
            Item("Open", wrap(self._on_open), default=True),
            Item("Daily Briefing", wrap(self._on_briefing or (lambda: None)),
                 enabled=self._on_briefing is not None),
            Item(
                "Speak Replies",
                wrap(self._on_tts_toggle or (lambda: None)),
                # Live checkmark reflecting whether TTS is currently on.
                checked=(lambda _i: bool(self._tts_state and self._tts_state()))
                        if self._on_tts_toggle is not None else None,
                enabled=self._on_tts_toggle is not None,
            ),
            Item("Reload Context", wrap(self._on_reload)),
            Item("Settings", wrap(self._on_settings or (lambda: None)),
                 enabled=self._on_settings is not None),
            Menu.SEPARATOR,
            Item("Quit", wrap(self._on_quit)),
        )
        self._icon = pystray.Icon(
            "jarvis", icon=load_icon("idle"), title="JARVIS", menu=menu
        )

    def start(self) -> None:
        """Start the tray icon on a daemon thread."""
        try:
            self._build()
        except Exception as exc:  # noqa: BLE001
            log.error("could not build tray icon: %s", exc)
            return
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        log.info("system tray started")

    def notify(self, title: str, message: str) -> bool:
        """Show a desktop balloon via the tray icon. Returns True if delivered.

        ``pystray`` only implements notifications on some backends (Windows
        balloons work); on others ``notify`` raises, so this is best-effort and
        the caller falls back to logging.
        """
        if self._icon is None:
            return False
        try:
            self._icon.notify(message, title)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("tray notify unavailable: %s", exc)
            return False

    def set_state(self, state: str) -> None:
        """Swap the tray icon to reflect idle/listening/thinking."""
        if self._icon is None:
            return
        try:
            self._icon.icon = load_icon(state)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not update tray icon: %s", exc)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
