"""JARVIS — entry point.

Wires together the tray icon, overlay window, Claude client, voice pipeline, and
notes watcher, then hands control to the webview event loop. ``python main.py``
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

    # Startup self-check: log which integrations are live vs. misconfigured, so a
    # missing token/flag is visible at launch instead of only when first used.
    log.info("Integration readiness:")
    for name, ok, detail in CONFIG.diagnostics():
        log.info("  [%s] %-24s %s", "ON " if ok else "off", name, detail)
    log.info("  Categories: %s", ", ".join(CONFIG.categories))

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
    from app.tts import Speaker

    context = ContextBuilder()
    context.reload_static()
    claude = ClaudeClient(context_builder=context)
    recorder = Recorder()
    transcriber = Transcriber()
    speaker = Speaker()

    # Overlay owns the webview window and its event loop.
    try:
        from app.overlay import Overlay
    except Exception as exc:  # noqa: BLE001
        log.error("could not import UI (is pywebview installed?): %s", exc)
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
        speaker=speaker,
        on_state_change=on_state_change,
    )

    # schedule(): run a callable safely regardless of calling thread.
    def schedule(fn) -> None:
        overlay.schedule(fn)

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
        on_briefing=overlay.daily_briefing,
        on_tts_toggle=overlay._toggle_tts,
        tts_state=lambda: overlay._tts_enabled,
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
