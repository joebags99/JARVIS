"""JARVIS — entry point.

Wires together the tray icon, overlay window, Claude client, voice pipeline, and
notes watcher, then hands control to the tkinter main loop. ``python main.py``
starts everything; the app lives in the system tray until you quit it.
"""

from __future__ import annotations

import sys

from app.config import CONFIG
from app.logging_setup import setup_logging
from app.icon import ensure_icon_file


def main() -> int:
    log = setup_logging()
    log.info("─" * 50)
    log.info("JARVIS starting up (user=%s)", CONFIG.user_name)

    if not CONFIG.has_anthropic_key:
        log.warning(
            "No ANTHROPIC_API_KEY configured — JARVIS will start but show an "
            "error in the overlay until you add one to .env."
        )

    ensure_icon_file()

    # Build core services. Each degrades gracefully if its deps are missing.
    from app.context_builder import ContextBuilder
    from app.claude_client import ClaudeClient
    from app.recorder import Recorder
    from app.transcriber import Transcriber

    context = ContextBuilder()
    context.reload_static()
    claude = ClaudeClient(context_builder=context)
    recorder = Recorder()
    transcriber = Transcriber()

    # Overlay owns the tkinter root.
    try:
        from app.overlay import Overlay
    except Exception as exc:  # noqa: BLE001
        log.error("could not import UI (is customtkinter installed?): %s", exc)
        print(f"Fatal: UI dependencies missing — {exc}", file=sys.stderr)
        return 1

    tray_holder: dict = {}

    def on_state_change(state: str) -> None:
        tray = tray_holder.get("tray")
        if tray is not None:
            tray.set_state(state)

    overlay = Overlay(
        claude_client=claude,
        recorder=recorder,
        transcriber=transcriber,
        on_state_change=on_state_change,
    )

    # schedule(): run a callable on the tkinter thread from any thread.
    def schedule(fn) -> None:
        try:
            overlay.root.after(0, fn)
        except Exception:  # noqa: BLE001
            pass

    # Notes watcher (optional) — refreshes nothing directly, just logs activity.
    from integrations.notes_watcher import NotesWatcher

    notes_watcher = NotesWatcher()
    notes_watcher.start()

    # System tray.
    from app.tray import Tray

    def on_quit() -> None:
        log.info("quit requested")
        notes_watcher.stop()
        tray = tray_holder.get("tray")
        if tray is not None:
            tray.stop()
        overlay.quit()

    tray = Tray(
        on_open=overlay.show,
        on_reload=overlay.reload_context,
        on_quit=on_quit,
        schedule=schedule,
    )
    tray_holder["tray"] = tray
    tray.start()

    # Optional global hotkey to toggle the overlay from anywhere.
    _setup_hotkey(overlay, schedule, log)

    log.info("JARVIS ready — click the tray icon to open the overlay.")
    try:
        overlay.run()
    except KeyboardInterrupt:
        on_quit()
    log.info("JARVIS shut down")
    return 0


def _setup_hotkey(overlay, schedule, log) -> None:
    if not CONFIG.hotkey:
        return
    try:
        import keyboard

        keyboard.add_hotkey(CONFIG.hotkey, lambda: schedule(overlay.toggle))
        log.info("global hotkey registered: %s", CONFIG.hotkey)
    except Exception as exc:  # noqa: BLE001
        # keyboard often needs root/admin on Linux/macOS; fail soft.
        log.warning("could not register global hotkey '%s': %s", CONFIG.hotkey, exc)


if __name__ == "__main__":
    raise SystemExit(main())
