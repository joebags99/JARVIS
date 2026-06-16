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
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .config import CONFIG, NOTES_DIR
from .logging_setup import get_logger

log = get_logger("overlay")

WINDOW_W = 420
WINDOW_H = 640
MARGIN = 16
MIN_TURNS_FOR_SUMMARY = 5  # user messages required before auto-saving a summary

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


def _screen_size() -> tuple[int, int]:
    """Primary display size, used to position the overlay on startup."""
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1080


def _enable_real_transparency(title: str, alpha: int) -> None:
    """Blend the whole window against the desktop with a constant alpha.

    pywebview's own ``transparent=True`` only sets the WebView2 control's
    background to transparent *inside* the host Form (avoiding a white
    flash and giving the rounded corners clean edges) — the Form itself is
    still a perfectly opaque top-level window as far as the desktop
    compositor is concerned, which is why the window never actually showed
    anything behind it no matter what alpha the CSS background used. Real
    desktop blending needs the Win32 layered-window attribute, which
    pywebview doesn't set up, so we do it ourselves here.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            log.warning("could not find window handle for layered transparency")
            return

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        LWA_ALPHA = 0x2

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
        log.info("layered window transparency enabled (alpha=%d)", alpha)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enable layered transparency: %s", exc)


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
        self._current_alpha: int = WINDOW_ALPHA_IDLE
        self._alpha_cancel: threading.Event | None = None

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
        # pywebview only wires up real per-pixel transparency on EdgeChromium if
        # the window is shown (not created with hidden=True) — see the "hack to
        # make transparent window work" in its winforms backend. So we start
        # visible and hide ourselves now that the page has finished loading.
        _enable_real_transparency("JARVIS", WINDOW_ALPHA_IDLE)
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
        self.set_status(STATUS_IDLE)
        log.info("overlay shown")

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
                _enable_real_transparency("JARVIS", alpha)
                if i < steps:
                    cancel.wait(STEP_MS / 1000)

        threading.Thread(target=_animate, daemon=True).start()

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
            # Stream the reply token by token into the live message bubble. If a
            # round's pre-tool text is discarded (model spoke, then called a
            # tool), on_reset rolls the bubble back to the thinking indicator so
            # the soon-to-be-replaced text doesn't linger.
            def on_delta(chunk: str) -> None:
                self._eval("appendAssistantDelta", chunk)

            def on_reset() -> None:
                self._eval("resetAssistantStream")

            reply = self.claude.send(text, on_delta=on_delta, on_reset=on_reset)
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
