"""The floating overlay window — JARVIS's face.

Rendered as an embedded web view (assets/ui/*.html|css|js) rather than native
tkinter widgets, so the UI can do real particle effects, glow, and smooth
animations that a Tk canvas can't easily produce. Python owns all state and
business logic; the page is a thin renderer driven by window.evaluate_js()
calls, and posts user actions back via window.pywebview.api.*.

Behavior:
- Click away  → dims to a low-opacity ghost (stays on screen)
- Hover back  → restores full opacity
- ✕ / Escape  → saves session summary (if 5+ user turns), clears chat, hides
- Chat persists across show/hide cycles until explicitly closed
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Callable

from .config import CONFIG, NOTES_DIR
from .logging_setup import get_logger
from .screen import enable_real_transparency, screen_size

log = get_logger("overlay")

WINDOW_W = 420
WINDOW_H = 640
MARGIN = 16
MIN_TURNS_FOR_SUMMARY = 5  # user messages required before auto-saving a summary
_MAX_NOTIFICATIONS = 50  # bell-icon history — most recent kept, oldest dropped

# 0 = fully invisible, 255 = fully opaque.
WINDOW_ALPHA_IDLE = 150  # ~58% opaque — tints the desktop through when unfocused
WINDOW_ALPHA_FOCUSED = 255  # fully opaque while focused/hovered/interacting

STATUS_IDLE = "Idle"
STATUS_LISTENING = "Listening…"
STATUS_TRANSCRIBING = "Transcribing…"
STATUS_THINKING = "Thinking…"
STATUS_DONE = "Done"

_UI_DIR = Path(__file__).resolve().parent.parent / "assets" / "ui"
_INDEX_HTML = _UI_DIR / "index.html"
_SELECTOR_HTML = _UI_DIR / "selector.html"


class _JSApi:
    """Methods callable from the page via ``window.pywebview.api.<name>()``."""

    def __init__(self, overlay: "Overlay") -> None:
        self._overlay = overlay

    def send_message(self, text: str) -> None:
        self._overlay._submit(text)

    def toggle_recording(self) -> None:
        self._overlay._toggle_recording()

    def clear_chat(self) -> None:
        self._overlay._clear_chat()

    def close_overlay(self) -> None:
        self._overlay._on_close()

    def move_window(self, dx: float, dy: float) -> None:
        self._overlay._drag_move(dx, dy)

    def set_focused(self, is_focused: bool) -> None:
        self._overlay._set_focused(is_focused)

    def toggle_tts(self) -> None:
        self._overlay._toggle_tts()

    # ── Voice dials (adjusted directly in the UI — no LLM round-trip) ─────────
    def get_dials(self) -> list[dict]:
        from .persona import PERSONA
        return PERSONA.state()

    def set_dial(self, name: str, value: int) -> list[dict]:
        from .persona import PERSONA
        PERSONA.adjust(name, set_to=int(value))
        log.info("dial %s set to %s via UI", name, value)
        return PERSONA.state()

    def reset_dials(self) -> list[dict]:
        from .persona import PERSONA
        PERSONA.reset()
        log.info("voice dials reset to defaults via UI")
        return PERSONA.state()

    def save_dials_default(self) -> list[dict]:
        from .persona import PERSONA
        PERSONA.persist_current()
        log.info("voice dials saved as defaults via UI")
        return PERSONA.state()

    # ── Settings panel (system status + editable categories) ─────────────────
    def get_settings(self) -> dict:
        return self._overlay._get_settings()

    # ── Proactive-nudge history (bell icon) ───────────────────────────────────
    def get_notifications(self) -> dict:
        return self._overlay._get_notifications()

    def save_categories(self, categories: list[str]) -> dict:
        return self._overlay._save_categories(categories)

    # ── Vision: screenshot capture ────────────────────────────────────────────
    def start_screenshot_capture(self) -> None:
        self._overlay.start_screenshot_capture()

    def clear_attached_screenshot(self) -> None:
        self._overlay.clear_attached_screenshot()


class _SelectorApi:
    """Methods callable from the screenshot selector page (its own tiny window)."""

    def __init__(
        self, on_select: Callable[[float, float, float, float], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._on_select = on_select
        self._on_cancel = on_cancel

    def report_selection(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._on_select(x1, y1, x2, y2)

    def cancel(self) -> None:
        self._on_cancel()


class Overlay:
    def __init__(
        self,
        claude_client,
        recorder,
        transcriber,
        speaker=None,
        on_state_change: Callable[[str], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        import webview

        self.claude = claude_client
        self.recorder = recorder
        self.transcriber = transcriber
        self.speaker = speaker
        self.on_state_change = on_state_change
        self.on_quit = on_quit

        self._visible = False
        self._recording = False
        self._wake_triggered_recording = False
        # TTS starts on only if both opted-in via config AND actually available.
        self._tts_enabled = bool(
            CONFIG.tts_enabled and speaker is not None and speaker.available
        )
        self._win_x, self._win_y = self._initial_position()
        self._current_alpha: int = WINDOW_ALPHA_IDLE
        self._alpha_cancel: threading.Event | None = None

        # Vision: a captured screenshot waiting to ride along with the next
        # sent message, and the (transient) region-selector window while open.
        self._pending_screenshot_b64: str | None = None
        self._selector_window = None

        # Proactive-nudge history (bell icon): a tray balloon disappears the
        # moment it's dismissed, so this durable, in-app list — most recent
        # first — is what actually lets meeting alerts/email pings/vault
        # callbacks be reviewed later instead of only living in the log.
        self._notifications: list[dict] = []
        self._unread_notifications = 0

        # Set by main.py after construction, same pattern as
        # wake_word_listener below — the ambient HUD (app/hud.py) needs to
        # know when this window's own visibility changes (to auto-hide
        # itself while the user is actively chatting) and when the unread
        # notification count changes (to mirror the bell badge). None when
        # the HUD is disabled or not yet wired up.
        self.on_visibility_changed: Callable[[bool], None] | None = None
        self.on_notifications_changed: Callable[[int], None] | None = None

        # Set by main.py after construction (mutual reference: the listener's
        # on_wake needs this Overlay, and this Overlay needs the listener to
        # pause/resume around every recording). None when wake word is off.
        self.wake_word_listener = None

        self._js_api = _JSApi(self)
        self.window = webview.create_window(
            "JARVIS",
            url=str(_INDEX_HTML),
            js_api=self._js_api,
            width=WINDOW_W,
            height=WINDOW_H,
            x=self._win_x,
            y=self._win_y,
            frameless=True,
            on_top=True,
            transparent=True,
            background_color="#0f0f0f",
            resizable=False,
            # pywebview makes the WHOLE frameless window a drag region by
            # default, which hijacks click-drag in the transcript (so text can't
            # be selected) and moves the window instead. We do our own dragging
            # from the header via move_window, so disable the blanket behavior.
            easy_drag=False,
        )
        self.window.events.loaded += self._on_loaded

    def _initial_position(self) -> tuple[int, int]:
        sw, sh = screen_size()
        pos = CONFIG.window_position
        x = sw - WINDOW_W - MARGIN if "right" in pos else MARGIN
        y = sh - WINDOW_H - MARGIN if "bottom" in pos else MARGIN
        return x, y

    # ── webview lifecycle ────────────────────────────────────────────────────

    def _on_loaded(self) -> None:
        self._eval("setUserName", CONFIG.user_name)
        if not self.claude.ready and self.claude.init_error:
            self._append_message("system", self.claude.init_error)
        if not self.recorder.available or not self.transcriber.available:
            self._eval("setVoiceAvailable", False)
            log.info(
                "voice disabled (recorder=%s, stt=%s)",
                self.recorder.available, self.transcriber.available,
            )
        tts_ok = self.speaker is not None and self.speaker.available
        self._eval("setTtsAvailable", tts_ok)
        self._eval("setTTSEnabled", self._tts_enabled)
        if not tts_ok:
            log.info("TTS disabled (speaker unavailable)")
        self._eval("setVisionAvailable", CONFIG.vision_available)
        if not CONFIG.vision_available:
            log.info("screen capture disabled (Pillow's ImageGrab unavailable on this platform)")
        # pywebview only wires up real per-pixel transparency on EdgeChromium if
        # the window is shown (not created with hidden=True) — see the "hack to
        # make transparent window work" in its winforms backend. So we start
        # visible and hide ourselves now that the page has finished loading.
        enable_real_transparency("JARVIS", WINDOW_ALPHA_IDLE)
        self.window.hide()

    def _eval(self, fn_name: str, *args) -> None:
        """Call a JS function in the page with JSON-encoded args."""
        try:
            arg_str = ", ".join(json.dumps(a) for a in args)
            self.window.evaluate_js(f"{fn_name}({arg_str})")
        except Exception as exc:  # noqa: BLE001
            log.debug("evaluate_js(%s) failed: %s", fn_name, exc)

    # ── Visibility ────────────────────────────────────────────────────────────

    def show(self) -> None:
        """Show (or un-fade) the overlay. Chat history is preserved."""
        self.window.show()
        self._set_focused(True)
        self._visible = True
        self._notify_visibility(True)
        self.set_status(STATUS_IDLE)
        log.info("overlay shown")

    def _notify_visibility(self, visible: bool) -> None:
        if self.on_visibility_changed:
            try:
                self.on_visibility_changed(visible)
            except Exception:  # noqa: BLE001
                pass

    def _set_focused(self, is_focused: bool) -> None:
        """Animate the window alpha toward fully opaque (focused) or idle (unfocused)."""
        target = WINDOW_ALPHA_FOCUSED if is_focused else WINDOW_ALPHA_IDLE
        if self._current_alpha == target:
            return

        # Cancel any in-flight animation.
        if self._alpha_cancel is not None:
            self._alpha_cancel.set()

        cancel = threading.Event()
        self._alpha_cancel = cancel

        start = self._current_alpha

        def _animate() -> None:
            # 200 ms total, ~10 ms per step → ~20 steps.
            DURATION_MS = 200
            STEP_MS = 10
            steps = max(1, DURATION_MS // STEP_MS)
            for i in range(1, steps + 1):
                if cancel.is_set():
                    return
                alpha = round(start + (target - start) * i / steps)
                self._current_alpha = alpha
                enable_real_transparency("JARVIS", alpha)
                if i < steps:
                    cancel.wait(STEP_MS / 1000)

        threading.Thread(target=_animate, daemon=True).start()

    def hide(self) -> None:
        """Hide window without clearing chat (used internally for fade-hide)."""
        if self._recording:
            self._stop_recording()
        self.window.hide()
        self._visible = False
        self._notify_visibility(False)
        log.info("overlay hidden")

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def daily_briefing(self) -> None:
        """Open the overlay and ask JARVIS for a one-shot daily briefing.

        Composes the existing tools — calendar, to-dos, weather, and (if
        configured) email — into a single morning summary. JARVIS only calls
        the tools it actually has, so this degrades cleanly when integrations
        aren't set up.
        """
        self.show()
        self._submit(
            "Give me my daily briefing for today: what's on my calendar, what's "
            "due or overdue on my to-do list, today's weather, and anything "
            "notable in my recent unread email. Keep it tight and scannable."
        )

    def schedule(self, fn: Callable[[], None]) -> None:
        """Run ``fn``. pywebview's Window methods are thread-safe, so unlike
        tkinter's ``.after(0, ...)`` marshaling this can call straight through.
        """
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled callback failed: %s", exc)

    def _maybe_save_summary(self) -> None:
        """Spawn a background session-summary save if the chat was substantial."""
        user_turns = sum(
            1 for m in self.claude.history if m.get("role") == "user"
        )
        if user_turns < MIN_TURNS_FOR_SUMMARY:
            return
        history_snapshot = list(self.claude.history)
        threading.Thread(
            target=self._save_session_summary,
            args=(history_snapshot,),
            daemon=True,
        ).start()

    def _clear_chat(self) -> None:
        """↺ button: save summary if warranted, clear chat, stay open."""
        self._maybe_save_summary()
        self.claude.reset_session()
        self._eval("clearTranscript")
        self._eval("resetEntry")
        self.set_status(STATUS_IDLE)
        log.info("chat cleared")

    def _on_close(self) -> None:
        """✕ / Escape: save summary if warranted, clear chat, hide."""
        if self._recording:
            self._stop_recording()

        self._maybe_save_summary()
        self.claude.reset_session()
        self._eval("clearTranscript")
        self._eval("resetEntry")
        self.window.hide()
        self._visible = False
        self._notify_visibility(False)
        log.info("overlay closed and chat cleared")

    # ── Dragging ──────────────────────────────────────────────────────────────

    def _drag_move(self, dx: float, dy: float) -> None:
        self._win_x += int(dx)
        self._win_y += int(dy)
        try:
            self.window.move(self._win_x, self._win_y)
        except Exception as exc:  # noqa: BLE001
            log.debug("window.move failed: %s", exc)

    # ── Session summary ───────────────────────────────────────────────────────

    def _save_session_summary(self, history: list[dict]) -> None:
        """Summarize a closed session and store it durably. Background thread.

        When an Obsidian vault is configured it becomes the sink — the summary is
        a ``Sessions/`` note and any extracted facts append to ``Memory/Facts.md``,
        so cross-session memory lives in the browsable, linked second brain. With
        no vault, it falls back to the legacy ``notes/session_*.md`` + ``memory.db``
        path so users who haven't migrated keep their memory. Best-effort
        throughout — a storage failure must never break session close.
        """
        summary = self.claude.summarize_session(history)
        if not summary:
            return
        facts = self.claude.extract_facts(history)
        if CONFIG.obsidian_available:
            self._store_session_in_vault(summary, facts)
        else:
            self._store_session_in_memory(summary, facts)

    def _store_session_in_vault(self, summary: str, facts: list[dict]) -> None:
        now = dt.datetime.now()
        try:
            from integrations import obsidian
            # Facts first: they create/grow the People/Projects notes (and the
            # roster), so the recap below can wikilink to entities they introduce.
            stored = obsidian.record_session_facts(facts)
            body = obsidian.linkify_entities(summary.strip())
            obsidian.write_note(
                now.strftime("Sessions/%Y-%m-%d_%H-%M.md"),
                f"{body}\n",
                title=now.strftime("Session — %B %d, %Y %I:%M %p"),
                tags=["session"],
                overwrite=False,
            )
            log.info("session summary written to vault (%d fact(s) routed)", stored)
        except Exception as exc:  # noqa: BLE001
            log.error("could not store session in vault: %s", exc)

    def _store_session_in_memory(self, summary: str, facts: list[dict]) -> None:
        now = dt.datetime.now()
        filename = now.strftime("session_%Y-%m-%d_%H-%M.md")
        path = NOTES_DIR / filename
        content = (
            f"# JARVIS Session — {now.strftime('%B %d, %Y %I:%M %p')}\n\n"
            f"{summary}\n"
        )
        try:
            path.write_text(content, encoding="utf-8")
            log.info("session summary saved: %s", filename)
        except Exception as exc:  # noqa: BLE001
            log.error("could not save session summary: %s", exc)
        try:
            from .memory import get_memory
            mem = get_memory()
            mem.add_session(summary)
            for fact in facts:
                text = fact.get("fact", "").strip() if isinstance(fact, dict) else str(fact)
                if text:
                    mem.add_fact(text, source="auto-extracted")
        except Exception as exc:  # noqa: BLE001
            log.error("could not store session in long-term memory: %s", exc)

    # ── Input handling ────────────────────────────────────────────────────────

    def _submit(self, text: str, on_done: Callable[[], None] | None = None) -> None:
        # Fix misheard/misspelled proper names (voice + typed) before it reaches
        # the chat, the session notes, or any tool call.
        from . import name_corrector
        text = name_corrector.normalize_names(text)
        # A pending screenshot rides along with this one message only — clear
        # it (and its UI chip) now so it can never silently reattach to the
        # next message too, regardless of how this turn turns out.
        image_b64 = self._pending_screenshot_b64
        if image_b64:
            self._pending_screenshot_b64 = None
            self._eval("clearAttachedImage")
        # Barge-in: never let JARVIS talk over a new request.
        if self.speaker is not None:
            self.speaker.stop()
        self._append_message("user", text)
        self._eval("startAssistantMessage")
        self.set_status(STATUS_THINKING)
        self._set_state("thinking")
        self._eval("setInputsEnabled", False)

        def worker() -> None:
            speaking = self._tts_enabled and self.speaker is not None
            if speaking:
                self.speaker.start_utterance()

            # Stream text as Claude generates it; on_reset rolls the live text
            # back to the thinking state when a round's pre-tool narration gets
            # discarded (the model spoke, then decided to call a tool instead).
            def on_delta(chunk: str) -> None:
                self._eval("appendAssistantDelta", chunk)
                if speaking:
                    self.speaker.feed(chunk)

            def on_reset() -> None:
                self._eval("resetAssistantStream")
                if speaking:
                    # The narration spoken so far is about to be replaced by
                    # the real answer — restart clean so the two don't run
                    # together mid-sentence.
                    self.speaker.start_utterance()

            reply = self.claude.send(text, on_delta=on_delta, on_reset=on_reset, image_b64=image_b64)
            # Flush whatever's left in the sentence buffer as the final clip.
            if speaking:
                self.speaker.finish()
            self._eval("finishAssistantMessage", reply)
            self.set_status(STATUS_DONE)
            self._set_state("idle")
            self._eval("setInputsEnabled", True)
            if speaking:
                # finish() only queues the last sentence — the audio itself
                # may still be playing. Block (we're on a background thread
                # already) until it's truly done before on_done runs, so
                # JARVIS's own voice can never be mistaken for a fresh wake.
                self.speaker.wait_idle(timeout=30.0)
            if on_done:
                try:
                    on_done()
                except Exception as exc:  # noqa: BLE001
                    log.error("submit on_done callback failed: %s", exc)

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_tts(self) -> None:
        """Flip whether JARVIS reads replies aloud; stop any speech when muting."""
        if self.speaker is None or not self.speaker.available:
            return
        self._tts_enabled = not self._tts_enabled
        if not self._tts_enabled:
            self.speaker.stop()
        self._eval("setTTSEnabled", self._tts_enabled)
        log.info("TTS %s", "on" if self._tts_enabled else "off")

    # ── Vision: screenshot capture ──────────────────────────────────────────────

    def start_screenshot_capture(self) -> None:
        """Open a full-screen drag-to-select overlay to capture a screen region.

        A second, transient pywebview window (not the main overlay) — a
        darkened full-screen page the user drags a rectangle on. Reports back
        via ``_SelectorApi`` to ``_finish_screenshot_capture``/
        ``_cancel_screenshot_capture``, which close it again.
        """
        if not CONFIG.vision_available:
            self.set_status("Screen capture isn't available on this system.")
            return
        if self._selector_window is not None:
            return  # a capture is already in progress
        import webview

        sw, sh = screen_size()
        selector_api = _SelectorApi(
            on_select=self._finish_screenshot_capture,
            on_cancel=self._cancel_screenshot_capture,
        )
        self._selector_window = webview.create_window(
            "JARVIS Capture",
            url=str(_SELECTOR_HTML),
            js_api=selector_api,
            width=sw,
            height=sh,
            x=0,
            y=0,
            frameless=True,
            on_top=True,
            transparent=True,
            background_color="#000000",
            resizable=False,
        )
        log.info("screenshot selector opened")

    def _close_selector(self) -> None:
        if self._selector_window is not None:
            try:
                self._selector_window.destroy()
            except Exception as exc:  # noqa: BLE001
                log.debug("closing screenshot selector failed: %s", exc)
            self._selector_window = None

    def _cancel_screenshot_capture(self) -> None:
        self._close_selector()
        log.info("screenshot capture cancelled")

    def _finish_screenshot_capture(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._close_selector()
        try:
            from . import screenshot
            image = screenshot.capture_region(x1, y1, x2, y2)
            self._pending_screenshot_b64 = screenshot.to_base64_png(image)
            self._eval(
                "setAttachedImage",
                f"data:image/png;base64,{self._pending_screenshot_b64}",
            )
            log.info("screenshot captured and attached (%dx%d region)", image.width, image.height)
        except Exception as exc:  # noqa: BLE001
            log.error("screenshot capture failed: %s", exc)
            self.set_status("Screenshot capture failed — check logs.")

    def clear_attached_screenshot(self) -> None:
        """✕ on the attached-image chip: drop the pending screenshot."""
        self._pending_screenshot_b64 = None
        self._eval("clearAttachedImage")

    # ── Voice ─────────────────────────────────────────────────────────────────

    def _toggle_recording(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self, wake_triggered: bool = False) -> None:
        if not self.recorder.available or self._recording:
            return
        # Stop any in-progress speech so it doesn't bleed into the mic.
        if self.speaker is not None:
            self.speaker.stop()
        # The wake-word listener's own mic stream must never be open at the
        # same time as this one (regardless of what triggered this recording
        # — button, hotkey, or the wake word itself), so pause it here, the
        # one place every recording path goes through.
        if self.wake_word_listener is not None:
            self.wake_word_listener.pause()
        if self.recorder.start():
            self._recording = True
            self._wake_triggered_recording = wake_triggered
            self.set_status(STATUS_LISTENING)
            self._set_state("listening")
            self._eval("setRecording", True)
        elif self.wake_word_listener is not None:
            self.wake_word_listener.resume()  # start failed — don't leave it paused

    def _resume_wake_word(self) -> None:
        if self.wake_word_listener is not None:
            self.wake_word_listener.resume()

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._eval("setRecording", False)
        wake_triggered = self._wake_triggered_recording
        wav_path = self.recorder.stop()
        # Deliberately NOT resuming the wake-word listener here: it stays
        # paused through transcription + the full Claude turn (+ speech, via
        # _submit's on_done) so "Hey JARVIS" can't false-trigger again while
        # JARVIS is still thinking or talking — resume happens once the whole
        # turn is truly over, in every branch below.
        self.set_status(STATUS_TRANSCRIBING)
        self._set_state("thinking")

        def worker() -> None:
            if wav_path is None:
                self.set_status("No audio captured — check mic")
                self._set_state("idle")
                self._resume_wake_word()
                return

            def on_status(msg: str) -> None:
                self.set_status(msg)

            text = self.transcriber.transcribe(wav_path, on_status=on_status)
            if text:
                if wake_triggered:
                    # The wake engine consumes "Hey JARVIS" as the activation
                    # trigger, so it's never part of the recorded/transcribed
                    # command — restore it so phrase-matching (e.g. the "Hey
                    # Jarvis, what's on the calendar for today?" easter egg)
                    # sees the same text push-to-talk would have produced.
                    text = f"Hey JARVIS, {text}"
                self._submit(text, on_done=self._resume_wake_word)
            else:
                self.set_status("No speech detected — check logs")
                self._set_state("idle")
                self._resume_wake_word()

        threading.Thread(target=worker, daemon=True).start()

    # ── Transcript rendering ──────────────────────────────────────────────────

    def _append_message(self, role: str, text: str) -> None:
        labels = {
            "user": CONFIG.user_name,
            "assistant": "JARVIS",
            "system": "⚠ System",
        }
        self._eval("addMessage", role, labels.get(role, role), text)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def set_status(self, text: str) -> None:
        self._eval("setStatus", text)

    def _set_state(self, state: str) -> None:
        self._eval("setState", state)
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception:  # noqa: BLE001
                pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def reload_context(self) -> None:
        self.claude.reload_context()
        from . import name_corrector
        name_corrector.reload()
        if self._visible:
            self.set_status("Context reloaded")

    # ── Settings ──────────────────────────────────────────────────────────────

    def _get_settings(self) -> dict:
        """Snapshot for the settings panel: status + editable categories."""
        return {
            "categories": list(CONFIG.categories),
            "user_name": CONFIG.user_name,
            "location": CONFIG.location or "",
            "diagnostics": [
                {"name": name, "ok": bool(ok), "detail": detail}
                for name, ok, detail in CONFIG.diagnostics()
            ],
        }

    def _save_categories(self, categories: list[str]) -> dict:
        """Persist edited categories; returns the refreshed settings (or an error)."""
        try:
            CONFIG.save_categories(list(categories or []))
            log.info("categories updated via settings panel: %s", CONFIG.categories)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_categories failed: %s", exc)
            return {**self._get_settings(), "error": str(exc)}
        return self._get_settings()

    # ── Proactive-nudge history (bell icon) ─────────────────────────────────────

    def record_notification(self, title: str, message: str) -> None:
        """Durable, in-app record of a proactive nudge (meeting alert, email
        ping, vault "still open?" callback, etc.).

        The tray balloon (or spoken line) these also go through disappears
        the moment it's dismissed — this list, surfaced via the bell icon, is
        what actually lets them be reviewed later instead of only living in
        the log. Pushed live to the page too, so a panel left open updates
        without the user having to reopen it.
        """
        entry = {
            "title": title,
            "message": message,
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._notifications.insert(0, entry)
        del self._notifications[_MAX_NOTIFICATIONS:]
        self._unread_notifications += 1
        self._eval("pushNotification", entry, self._unread_notifications)
        self._notify_unread_count()

    def _get_notifications(self) -> dict:
        """Snapshot for the notifications panel; opening it marks all read."""
        self._unread_notifications = 0
        self._notify_unread_count()
        return {"items": list(self._notifications), "unread": 0}

    def _notify_unread_count(self) -> None:
        if self.on_notifications_changed:
            try:
                self.on_notifications_changed(self._unread_notifications)
            except Exception:  # noqa: BLE001
                pass

    def quit(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        import webview

        webview.start()
