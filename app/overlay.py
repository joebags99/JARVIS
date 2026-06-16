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

import ctypes
import datetime as dt
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .config import CONFIG, NOTES_DIR
from .logging_setup import get_logger

log = get_logger("overlay")

WINDOW_W = 420
WINDOW_H = 640
MARGIN = 16
MIN_TURNS_FOR_SUMMARY = 5  # user messages required before auto-saving a summary
STREAM_FLUSH_INTERVAL = 0.05  # seconds between appendStreamChunk evaluate_js calls

STATUS_IDLE = "Idle"
STATUS_LISTENING = "Listening…"
STATUS_TRANSCRIBING = "Transcribing…"
STATUS_THINKING = "Thinking…"
STATUS_DONE = "Done"

_UI_DIR = Path(__file__).resolve().parent.parent / "assets" / "ui"
_INDEX_HTML = _UI_DIR / "index.html"


def _screen_size() -> tuple[int, int]:
    """Primary display size, used to position the overlay on startup."""
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1080


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


class Overlay:
    def __init__(
        self,
        claude_client,
        recorder,
        transcriber,
        on_state_change: Callable[[str], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        import webview

        self.claude = claude_client
        self.recorder = recorder
        self.transcriber = transcriber
        self.on_state_change = on_state_change
        self.on_quit = on_quit

        self._visible = False
        self._recording = False
        self._win_x, self._win_y = self._initial_position()

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
            hidden=True,
        )
        self.window.events.loaded += self._on_loaded

    def _initial_position(self) -> tuple[int, int]:
        sw, sh = _screen_size()
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
        self._eval("clearFade")
        self._visible = True
        self.set_status(STATUS_IDLE)
        log.info("overlay shown")

    def hide(self) -> None:
        """Hide window without clearing chat (used internally for fade-hide)."""
        if self._recording:
            self._stop_recording()
        self.window.hide()
        self._visible = False
        log.info("overlay hidden")

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def schedule(self, fn: Callable[[], None]) -> None:
        """Run ``fn``. pywebview's Window methods are thread-safe, so unlike
        tkinter's ``.after(0, ...)`` marshaling this can call straight through.
        """
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled callback failed: %s", exc)

    def _clear_chat(self) -> None:
        """↺ button: save summary if warranted, clear chat, stay open."""
        user_turns = sum(
            1 for m in self.claude.history if m.get("role") == "user"
        )
        if user_turns >= MIN_TURNS_FOR_SUMMARY:
            history_snapshot = list(self.claude.history)
            threading.Thread(
                target=self._save_session_summary,
                args=(history_snapshot,),
                daemon=True,
            ).start()
        self.claude.reset_session()
        self._eval("clearTranscript")
        self._eval("resetEntry")
        self.set_status(STATUS_IDLE)
        log.info("chat cleared")

    def _on_close(self) -> None:
        """✕ / Escape: save summary if warranted, clear chat, hide."""
        if self._recording:
            self._stop_recording()

        user_turns = sum(
            1 for m in self.claude.history if m.get("role") == "user"
        )
        if user_turns >= MIN_TURNS_FOR_SUMMARY:
            history_snapshot = list(self.claude.history)
            threading.Thread(
                target=self._save_session_summary,
                args=(history_snapshot,),
                daemon=True,
            ).start()

        self.claude.reset_session()
        self._eval("clearTranscript")
        self._eval("resetEntry")
        self.window.hide()
        self._visible = False
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
        """Generate a summary via Claude and write it to notes/. Background thread."""
        summary = self.claude.summarize_session(history)
        if not summary:
            return
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

    # ── Input handling ────────────────────────────────────────────────────────

    def _submit(self, text: str) -> None:
        self._append_message("user", text)
        self._eval("startAssistantMessage")
        self.set_status(STATUS_THINKING)
        self._set_state("thinking")
        self._eval("setInputsEnabled", False)

        def worker() -> None:
            # Batch streamed tokens and flush on an interval rather than once per
            # token — calling evaluate_js per token competes with the particle
            # canvas's requestAnimationFrame loop on the webview's UI thread and
            # freezes the animation while a response streams in.
            buffer: list[str] = []
            last_flush = time.monotonic()

            def flush(force: bool = False) -> None:
                nonlocal last_flush
                if not buffer:
                    return
                now = time.monotonic()
                if not force and now - last_flush < STREAM_FLUSH_INTERVAL:
                    return
                chunk = "".join(buffer)
                buffer.clear()
                last_flush = now
                self._eval("appendStreamChunk", chunk)

            def on_delta(chunk: str) -> None:
                buffer.append(chunk)
                flush()

            reply = self.claude.send(text, on_delta=on_delta)
            flush(force=True)
            self._eval("finishAssistantMessage", reply)
            self.set_status(STATUS_DONE)
            self._set_state("idle")
            self._eval("setInputsEnabled", True)

        threading.Thread(target=worker, daemon=True).start()

    # ── Voice ─────────────────────────────────────────────────────────────────

    def _toggle_recording(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        if not self.recorder.available or self._recording:
            return
        if self.recorder.start():
            self._recording = True
            self.set_status(STATUS_LISTENING)
            self._set_state("listening")
            self._eval("setRecording", True)

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._eval("setRecording", False)
        wav_path = self.recorder.stop()
        self.set_status(STATUS_TRANSCRIBING)
        self._set_state("thinking")

        def worker() -> None:
            if wav_path is None:
                self.set_status("No audio captured — check mic")
                self._set_state("idle")
                return

            def on_status(msg: str) -> None:
                self.set_status(msg)

            text = self.transcriber.transcribe(wav_path, on_status=on_status)
            if text:
                self._submit(text)
            else:
                self.set_status("No speech detected — check logs")
                self._set_state("idle")

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
        if self._visible:
            self.set_status("Context reloaded")

    def quit(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        import webview

        webview.start()
