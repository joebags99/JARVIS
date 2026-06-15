"""The floating overlay window — JARVIS's face.

A frameless, always-on-top, dark window pinned to a screen corner. It owns the
tkinter root and is driven by callbacks from the tray. All slow work (API calls,
transcription, recording) is dispatched to background threads; UI mutations are
marshalled back with ``self.root.after`` so the UI thread is never blocked.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("overlay")

WINDOW_W = 400
WINDOW_H = 600
MARGIN = 16

# Status labels
STATUS_IDLE = "Idle"
STATUS_LISTENING = "Listening…"
STATUS_TRANSCRIBING = "Transcribing…"
STATUS_THINKING = "Thinking…"
STATUS_DONE = "Done"


class Overlay:
    def __init__(
        self,
        claude_client,
        recorder,
        transcriber,
        on_state_change: Callable[[str], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        import customtkinter as ctk

        self.ctk = ctk
        self.claude = claude_client
        self.recorder = recorder
        self.transcriber = transcriber
        self.on_state_change = on_state_change
        self.on_quit = on_quit

        self._visible = False
        self._recording = False
        self._drag_offset = (0, 0)
        self.palette = CONFIG.palette

        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk()
        self._build_window()
        self._build_widgets()
        self.root.withdraw()  # start hidden in tray

        # Surface an init error (e.g. missing API key) immediately.
        if not self.claude.ready and self.claude.init_error:
            self._append_message("system", self.claude.init_error)

    # ── Window chrome ─────────────────────────────────────────────────────────
    def _build_window(self) -> None:
        self.root.title("JARVIS")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.overrideredirect(True)  # frameless
        self.root.attributes("-topmost", True)
        try:
            self.root.configure(fg_color=self.palette.background)
        except Exception:  # noqa: BLE001
            pass
        self._position_window()

        # Esc hides; clicking outside hides.
        self.root.bind("<Escape>", lambda _e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)

    def _position_window(self) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pos = CONFIG.window_position
        x = sw - WINDOW_W - MARGIN if "right" in pos else MARGIN
        y = sh - WINDOW_H - MARGIN if "bottom" in pos else MARGIN
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _on_focus_out(self, _event) -> None:
        # Hide when focus leaves the window (click outside). Guard against the
        # transient focus loss that happens while interacting with child widgets.
        if not self._visible:
            return
        try:
            focus = self.root.focus_get()
        except Exception:  # noqa: BLE001
            focus = None
        if focus is None:
            self.hide()

    # ── Widgets ───────────────────────────────────────────────────────────────
    def _build_widgets(self) -> None:
        ctk = self.ctk
        p = self.palette

        # Header (draggable)
        header = ctk.CTkFrame(self.root, fg_color=p.surface, corner_radius=0, height=44)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        self._bind_drag(header)

        title = ctk.CTkLabel(
            header,
            text="◉  J A R V I S",
            text_color=p.accent,
            font=("Segoe UI", 16, "bold"),
        )
        title.pack(side="left", padx=14)
        self._bind_drag(title)

        close_btn = ctk.CTkButton(
            header,
            text="✕",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=p.border,
            text_color=p.text_muted,
            command=self.hide,
        )
        close_btn.pack(side="right", padx=8)

        # Conversation area (scrollable)
        self.transcript = ctk.CTkTextbox(
            self.root,
            fg_color=p.background,
            text_color=p.text_primary,
            border_width=0,
            wrap="word",
            font=("Segoe UI", 13),
        )
        self.transcript.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        self.transcript.configure(state="disabled")
        self._configure_tags()

        # Status line
        self.status_label = ctk.CTkLabel(
            self.root,
            text=STATUS_IDLE,
            text_color=p.text_muted,
            font=("Segoe UI", 11),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=14, pady=(0, 4))

        # Input row
        input_row = ctk.CTkFrame(self.root, fg_color=p.background)
        input_row.pack(fill="x", padx=12, pady=(0, 12))

        self.entry = ctk.CTkEntry(
            input_row,
            placeholder_text="Ask JARVIS…",
            fg_color=p.surface,
            border_color=p.border,
            text_color=p.text_primary,
            height=40,
        )
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self._on_send())

        self.record_btn = ctk.CTkButton(
            input_row,
            text="🎤",
            width=44,
            height=40,
            fg_color=p.surface,
            hover_color=p.accent_dim,
            text_color=p.accent,
            command=None,
        )
        self.record_btn.pack(side="left", padx=(8, 0))
        # Push-to-talk: press to start, release to stop.
        self.record_btn.bind("<ButtonPress-1>", lambda _e: self._start_recording())
        self.record_btn.bind("<ButtonRelease-1>", lambda _e: self._stop_recording())

        self.send_btn = ctk.CTkButton(
            input_row,
            text="➤",
            width=44,
            height=40,
            fg_color=p.accent_dim,
            hover_color=p.accent,
            text_color=p.text_primary,
            command=self._on_send,
        )
        self.send_btn.pack(side="left", padx=(8, 0))

        if not self.recorder.available or not self.transcriber.available:
            self.record_btn.configure(state="disabled", text_color=p.text_muted)
            log.info("voice disabled (recorder=%s, stt=%s)",
                     self.recorder.available, self.transcriber.available)

    def _configure_tags(self) -> None:
        p = self.palette
        box = self.transcript
        box.tag_config("user", foreground=p.accent)
        box.tag_config("assistant", foreground=p.text_primary)
        box.tag_config("system", foreground=p.error)
        box.tag_config("label_user", foreground=p.text_muted)
        box.tag_config("label_assistant", foreground=p.accent_dim)

    # ── Dragging ──────────────────────────────────────────────────────────────
    def _bind_drag(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    # ── Visibility ────────────────────────────────────────────────────────────
    def show(self) -> None:
        # Fresh session each time the overlay is opened.
        self.claude.reset_session()
        self._clear_transcript()
        if not self.claude.ready and self.claude.init_error:
            self._append_message("system", self.claude.init_error)
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._apply_windows_shadow()
        self.entry.focus_force()
        self._visible = True
        self.set_status(STATUS_IDLE)
        log.info("overlay shown")

    def hide(self) -> None:
        if self._recording:
            self._stop_recording()
        self.root.withdraw()
        self._visible = False
        log.info("overlay hidden")

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    # ── Messaging ─────────────────────────────────────────────────────────────
    def _on_send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._submit(text)

    def _submit(self, text: str) -> None:
        self._append_message("user", text)
        self._start_assistant_message()
        self.set_status(STATUS_THINKING)
        self._set_state("thinking")
        self._set_inputs_enabled(False)

        def worker() -> None:
            def on_delta(chunk: str) -> None:
                self.root.after(0, lambda: self._append_stream(chunk))

            reply = self.claude.send(text, on_delta=on_delta)
            self.root.after(0, lambda: self._finish_assistant_message(reply))

        threading.Thread(target=worker, daemon=True).start()

    # ── Voice ─────────────────────────────────────────────────────────────────
    def _start_recording(self) -> None:
        if not self.recorder.available or self._recording:
            return
        if self.recorder.start():
            self._recording = True
            self.set_status(STATUS_LISTENING)
            self._set_state("listening")
            self.record_btn.configure(fg_color=self.palette.accent)

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.record_btn.configure(fg_color=self.palette.surface)
        wav_path = self.recorder.stop()
        self.set_status(STATUS_TRANSCRIBING)
        self._set_state("thinking")

        def worker() -> None:
            if wav_path is None:
                self.root.after(0, lambda: self.set_status(STATUS_IDLE))
                return

            def on_status(msg: str) -> None:
                self.root.after(0, lambda: self.set_status(msg))

            text = self.transcriber.transcribe(wav_path, on_status=on_status)
            if text:
                self.root.after(0, lambda: self._submit(text))
            else:
                self.root.after(0, lambda: self.set_status("No speech detected"))
                self.root.after(0, lambda: self._set_state("idle"))

        threading.Thread(target=worker, daemon=True).start()

    # ── Transcript rendering ──────────────────────────────────────────────────
    def _clear_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def _append_message(self, role: str, text: str) -> None:
        labels = {"user": f"{CONFIG.user_name}", "assistant": "JARVIS",
                  "system": "⚠ System"}
        self.transcript.configure(state="normal")
        label_tag = "label_user" if role == "user" else "label_assistant"
        self.transcript.insert("end", f"\n{labels.get(role, role)}\n", label_tag)
        self.transcript.insert("end", f"{text}\n", role)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _start_assistant_message(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", "\nJARVIS\n", "label_assistant")
        self._assistant_mark = self.transcript.index("end-1c")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _append_stream(self, chunk: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", chunk, "assistant")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _finish_assistant_message(self, full_reply: str) -> None:
        # If streaming produced nothing (e.g. an error string), render it now.
        self.transcript.configure(state="normal")
        self.transcript.insert("end", "\n", "assistant")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")
        self.set_status(STATUS_DONE)
        self._set_state("idle")
        self._set_inputs_enabled(True)
        self.entry.focus_set()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _set_state(self, state: str) -> None:
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception:  # noqa: BLE001
                pass

    def _set_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        self.send_btn.configure(state=state)

    def _apply_windows_shadow(self) -> None:
        """Apply a DWM drop shadow on Windows (no-op elsewhere)."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            margins = wintypes.RECT(1, 1, 1, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(margins)
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not apply windows shadow: %s", exc)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def reload_context(self) -> None:
        self.claude.reload_context()
        if self._visible:
            self.set_status("Context reloaded")

    def quit(self) -> None:
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        self.root.mainloop()
