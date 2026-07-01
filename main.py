"""JARVIS — entry point.

Wires together the tray icon, overlay window, Claude client, voice pipeline, and
the knowledge store (an Obsidian vault when configured, else the legacy notes +
SQLite memory), then hands control to the webview event loop. ``python main.py``
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

    # Durable knowledge store: the Obsidian vault when configured (scaffold +
    # one-time migration + search index + watcher), else the legacy SQLite memory
    # and notes watcher. Returns the file watcher to stop on quit.
    knowledge_watcher = _init_knowledge(log)

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
    scheduler_holder: dict = {}
    wakeword_holder: dict = {}

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

    # System tray.
    from app.tray import Tray

    def on_quit() -> None:
        log.info("quit requested")
        knowledge_watcher.stop()
        scheduler = scheduler_holder.get("scheduler")
        if scheduler is not None:
            scheduler.stop()
        listener = wakeword_holder.get("listener")
        if listener is not None:
            listener.stop()
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

    # Proactive scheduler (optional) — scheduled briefing, meeting alerts, and
    # important-email pings. No-ops unless JARVIS_PROACTIVE_ENABLED is set.
    _start_proactive(overlay, speaker, tray_holder, scheduler_holder, schedule, log)

    # Optional global hotkey to toggle the overlay from anywhere.
    _setup_hotkey(overlay, schedule, log)

    # Wake word (optional) — "Hey JARVIS" starts a recording hands-free, on
    # top of the existing push-to-talk pipeline. No-ops unless
    # JARVIS_WAKE_WORD_ENABLED is set.
    _start_wake_word(overlay, recorder, wakeword_holder, schedule, log)

    log.info("JARVIS ready — click the tray icon to open the overlay.")
    try:
        overlay.run()
    except KeyboardInterrupt:
        on_quit()
    log.info("JARVIS shut down")
    return 0


def _init_knowledge(log):
    """Set up JARVIS's durable knowledge store and return its file watcher.

    With an Obsidian vault configured, the vault is the brain: seed its scaffold,
    run the one-time (idempotent, non-destructive) migration of legacy notes +
    facts, build the FTS5 search index, and return a vault watcher that keeps the
    index fresh. Otherwise fall back to the legacy SQLite memory + notes watcher
    so users who haven't enabled a vault keep their existing recall. Each piece is
    best-effort — a failure here must never stop JARVIS from starting.
    """
    if CONFIG.obsidian_available:
        try:
            from integrations import obsidian
            obsidian.ensure_scaffold()
            migrated = obsidian.migrate_legacy()
            if migrated:
                log.info("  Vault: migrated %d legacy item(s)", migrated)
            if CONFIG.obsidian_auto_organize:
                # Keep the vault tidy: refile meetings that slipped into an entity
                # folder back to Sessions/, type-stamp notes, refresh the hub Maps
                # of Content, the graph color config, the stats dashboard, and the
                # entity Canvas. Idempotent — only rewrites what changed — so it's
                # cheap on every launch.
                refiled = obsidian.refile_meetings(dry_run=False)
                typed = obsidian.backfill_types()
                maps = obsidian.rebuild_mocs()
                obsidian.write_graph_config()
                obsidian.write_dashboard()
                obsidian.write_canvas()
                log.info("  Vault: organized (%d meetings refiled, %d newly typed, "
                         "%d maps, graph colored, dashboard + canvas refreshed)",
                         len(refiled), typed, maps)
            indexed = obsidian.reindex()
            log.info("  Vault: %s (%d note(s) indexed)",
                     CONFIG.obsidian_vault_path, indexed)
            watcher = obsidian.ObsidianWatcher()
            watcher.start()
            return watcher
        except Exception as exc:  # noqa: BLE001
            log.warning("vault init failed; falling back to legacy memory: %s", exc)

    # Legacy fallback: SQLite long-term memory + notes-folder watcher.
    try:
        from app.memory import get_memory
        mem = get_memory()
        mem.import_legacy_sessions()
        log.info("  Memory: %d item(s)", mem.count())
    except Exception as exc:  # noqa: BLE001
        log.warning("memory init failed: %s", exc)
    from integrations.notes_watcher import NotesWatcher
    watcher = NotesWatcher()
    watcher.start()
    return watcher


def _setup_hotkey(overlay, schedule, log) -> None:
    """Register the global toggle hotkey and the screenshot-capture hotkey.

    Each is independent (a blank/failed one never blocks the other) — a user
    can set just one, both, or neither.
    """
    if CONFIG.hotkey:
        _register_hotkey(CONFIG.hotkey, lambda: schedule(overlay.toggle), log)
    if CONFIG.screenshot_hotkey:
        _register_hotkey(
            CONFIG.screenshot_hotkey, lambda: schedule(overlay.start_screenshot_capture), log
        )


def _register_hotkey(combo: str, callback, log) -> None:
    try:
        import keyboard

        keyboard.add_hotkey(combo, callback)
        log.info("global hotkey registered: %s", combo)
    except Exception as exc:  # noqa: BLE001
        # keyboard often needs root/admin on Linux/macOS; fail soft.
        log.warning("could not register global hotkey '%s': %s", combo, exc)


def _start_proactive(overlay, speaker, tray_holder, scheduler_holder, schedule, log) -> None:
    """Wire the proactive scheduler to the real tray/calendar/email side effects."""
    if not CONFIG.proactive_enabled:
        return
    from app.proactive import ProactiveScheduler

    def notify(title: str, message: str) -> None:
        log.info("PROACTIVE [%s] %s", title, message)
        tray = tray_holder.get("tray")
        delivered = tray.notify(title, message) if tray is not None else False
        if not delivered:
            # No balloon support — surface it in the overlay status instead.
            try:
                schedule(lambda: overlay.set_status(f"{title}: {message}"))
            except Exception as exc:  # noqa: BLE001
                log.debug("overlay status fallback failed: %s", exc)
        if CONFIG.proactive_speak and speaker is not None and getattr(speaker, "available", False):
            try:
                speaker.speak(f"{title}. {message}")
            except Exception as exc:  # noqa: BLE001
                log.debug("proactive speak failed: %s", exc)

    def fetch_events() -> list:
        from integrations import google_calendar, outlook_calendar, outlook_ics
        events: list = []
        for src in (google_calendar, outlook_calendar, outlook_ics):
            try:
                events += src.get_events(1, 30)
            except Exception as exc:  # noqa: BLE001
                log.debug("proactive event fetch failed (%s): %s", src.__name__, exc)
        return events

    def fetch_email() -> list:
        from integrations import gmail
        return gmail.list_unread()

    def fetch_vault_items() -> list:
        from integrations import obsidian
        return obsidian.list_open_callbacks()

    def mark_vault_nudged(rel: str) -> None:
        from integrations import obsidian
        obsidian.mark_callback_nudged(rel)

    scheduler = ProactiveScheduler(
        notify=notify,
        briefing=lambda: schedule(overlay.daily_briefing),
        fetch_events=fetch_events,
        fetch_email=fetch_email,
        fetch_vault_items=fetch_vault_items,
        mark_vault_nudged=mark_vault_nudged,
    )
    scheduler.start()
    scheduler_holder["scheduler"] = scheduler


def _start_wake_word(overlay, recorder, wakeword_holder, schedule, log) -> None:
    """Wire "Hey JARVIS" to start a hands-free recording on top of push-to-talk.

    The listener pauses itself automatically whenever ANY recording starts
    (push-to-talk button/hotkey included — see Overlay._start_recording/
    _stop_recording) so its own mic stream and Recorder's are never open at
    the same time. A wake fires overlay._start_recording(); since there's no
    button to release, a silence-timeout watchdog calls _stop_recording() once
    the user's stopped talking (or a hard duration cap is hit either way).
    """
    if not CONFIG.wake_word_enabled:
        return
    if not recorder.available:
        log.warning("wake word enabled but the microphone isn't available; skipping")
        return
    from app.wakeword import WakeWordListener, watch_for_silence

    def on_wake() -> None:
        overlay.show()
        overlay._start_recording()
        if overlay._recording:
            watch_for_silence(recorder, on_timeout=overlay._stop_recording)

    listener = WakeWordListener(on_wake=lambda: schedule(on_wake))
    if not listener.available:
        log.warning(
            "wake word enabled but the listener didn't come up — see the "
            "'wake-word' log line just above for the specific reason "
            "(missing deps, a failed model download, or a load error); "
            "skipping (voice still works via push-to-talk)",
        )
        return
    overlay.wake_word_listener = listener
    listener.start()
    wakeword_holder["listener"] = listener


if __name__ == "__main__":
    raise SystemExit(main())
