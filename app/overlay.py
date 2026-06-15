"""The floating overlay window — JARVIS's face.

Behavior:
- Click away  → fades to 30% opacity (stays on screen)
- Hover back  → restores to 100%
- ✕ / Escape  → saves session summary (if 5+ user turns), clears chat, hides
- Chat persists across show/hide cycles until explicitly closed
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import threading
from typing import Callable

from .config import CONFIG, NOTES_DIR
from .logging_setup import get_logger

log = get_logger("overlay")

WINDOW_W = 400
WINDOW_H = 600
MARGIN = 16
ALPHA_FULL = 1.0
ALPHA_FADE = 0.3
MIN_TURNS_FOR_SUMMARY = 5  # user messages required before auto-saving a summary

STATUS_IDLE = "Idle"
STATUS_LISTENING = "Listening…"
STATUS_TRANSCRIBING = "Transcribing…"
STATUS_THINKING = "Thinking…"
STATUS_DONE = "Done"

# Inline markdown: **bold**, *italic*, `code`
_INLINE_RE = re.compile(r"(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`)")


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
        self.root.withdraw()

        if not self.claude.ready and self.claude.init_error:
            self._append_message("system", self.claude.init_error)

    # ── Window chrome ─────────────────────────────────────────────────────────

    def _build_window(self) -> None:
        self.root.title("JARVIS")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.configure(fg_color=self.palette.background)
        except Exception:  # noqa: BLE001
            pass
        self._position_window()

        # Escape / focus-out fade; hover restores.
        self.root.bind("<Escape>", lambda _e: self._on_close())
        self.root.bind("<FocusOut>", self._on_focus_out)
        self.root.bind("<Enter>", lambda _e: self._restore_opacity())
        self.root.bind("<Leave>", self._on_mouse_leave)

    def _position_window(self) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pos = CONFIG.window_position
        x = sw - WINDOW_W - MARGIN if "right" in pos else MARGIN
        y = sh - WINDOW_H - MARGIN if "bottom" in pos else MARGIN
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _on_focus_out(self, _event) -> None:
        if not self._visible:
            return
        try:
            focus = self.root.focus_get()
        except Exception:  # noqa: BLE001
            focus = None
        if focus is None:
            self._fade()

    def _on_mouse_leave(self, event) -> None:
        # Only fade when the mouse truly leaves the window bounds.
        wx, wy = self.root.winfo_rootx(), self.root.winfo_rooty()
        ww, wh = self.root.winfo_width(), self.root.winfo_height()
        if not (wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh):
            self._fade()

    def _fade(self) -> None:
        if self._visible:
            self.root.attributes("-alpha", ALPHA_FADE)

    def _restore_opacity(self) -> None:
        if self._visible:
            self.root.attributes("-alpha", ALPHA_FULL)

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

        # Clear-chat button (saves summary then resets)
        clear_btn = ctk.CTkButton(
            header,
            text="↺",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=p.border,
            text_color=p.text_muted,
            command=self._on_close,
            font=("Segoe UI", 16),
        )
        clear_btn.pack(side="right", padx=(0, 4))

        close_btn = ctk.CTkButton(
            header,
            text="✕",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=p.border,
            text_color=p.text_muted,
            command=self._on_close,
        )
        close_btn.pack(side="right", padx=(0, 4))

        # Conversation area
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

        # Multi-line input that grows up to ~4 lines then scrolls.
        self.entry = ctk.CTkTextbox(
            input_row,
            fg_color=p.surface,
            border_color=p.border,
            border_width=1,
            text_color=p.text_primary,
            height=40,
            wrap="word",
            font=("Segoe UI", 13),
        )
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_entry_return)
        self.entry.bind("<KeyRelease>", self._on_entry_key)
        # Insert placeholder-style hint (cleared on first keypress)
        self._entry_placeholder = True
        self._show_entry_placeholder()

        self.record_btn = ctk.CTkButton(
            input_row,
            text="🎤",
            width=44,
            height=40,
            fg_color=p.surface,
            hover_color=p.accent_dim,
            text_color=p.accent,
            command=self._toggle_recording,
        )
        self.record_btn.pack(side="left", padx=(8, 0))

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
            log.info(
                "voice disabled (recorder=%s, stt=%s)",
                self.recorder.available, self.transcriber.available,
            )

    def _show_entry_placeholder(self) -> None:
        self.entry.configure(state="normal")
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", "Ask JARVIS…")
        self.entry.configure(text_color=self.palette.text_muted)
        self._entry_placeholder = True

    def _clear_entry_placeholder(self) -> None:
        if self._entry_placeholder:
            self.entry.delete("1.0", "end")
            self.entry.configure(text_color=self.palette.text_primary)
            self._entry_placeholder = False

    def _configure_tags(self) -> None:
        p = self.palette
        box = self.transcript
        # Role tags — no font, CTkTextbox wrapper is fine.
        box.tag_config("user", foreground=p.accent)
        box.tag_config("assistant", foreground=p.text_primary)
        box.tag_config("system", foreground=p.error)
        box.tag_config("label_user", foreground=p.text_muted)
        box.tag_config("label_assistant", foreground=p.accent_dim)
        box.tag_config("md_bullet", foreground=p.accent_dim)
        # Font-bearing tags must go on the underlying tk.Text directly because
        # CTkTextbox.tag_config() forbids 'font' to protect its DPI scaling.
        tb = box._textbox
        tb.tag_config("md_bold",       font=("Segoe UI", 13, "bold"))
        tb.tag_config("md_italic",     font=("Segoe UI", 13, "italic"))
        tb.tag_config("md_h1",         font=("Segoe UI", 17, "bold"))
        tb.tag_config("md_h2",         font=("Segoe UI", 15, "bold"))
        tb.tag_config("md_h3",         font=("Segoe UI", 13, "bold"))
        tb.tag_config("md_code",       font=("Consolas", 12), foreground="#a8c7fa")
        tb.tag_config("md_code_block", font=("Consolas", 12), foreground="#a8c7fa",
                      lmargin1=16, lmargin2=16)
        box.tag_config("md_bullet",    foreground=p.accent_dim)

    # ── Dragging ──────────────────────────────────────────────────────────────

    def _bind_drag(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event) -> None:
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _drag_move(self, event) -> None:
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    # ── Visibility ────────────────────────────────────────────────────────────

    def show(self) -> None:
        """Show (or un-fade) the overlay. Chat history is preserved."""
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", ALPHA_FULL)
        self._apply_windows_shadow()
        self.entry.focus_force()
        self._visible = True
        self.set_status(STATUS_IDLE)
        log.info("overlay shown")

    def hide(self) -> None:
        """Withdraw window without clearing chat (used internally for fade-hide)."""
        if self._recording:
            self._stop_recording()
        self.root.withdraw()
        self._visible = False
        log.info("overlay hidden")

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
        self._clear_transcript()
        self._show_entry_placeholder()
        self.root.attributes("-alpha", ALPHA_FULL)
        self.root.withdraw()
        self._visible = False
        log.info("overlay closed and chat cleared")

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

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

    def _on_entry_return(self, event) -> str:
        if event.state & 0x1:  # Shift held — insert literal newline
            self.entry.insert("insert", "\n")
            self._on_entry_key()
        else:
            self._on_send()
        return "break"  # always suppress CTkTextbox default

    def _on_entry_key(self, _event=None) -> None:
        """Auto-resize the input box (1–4 lines)."""
        if self._entry_placeholder:
            self._clear_entry_placeholder()
            return
        content = self.entry.get("1.0", "end-1c")
        lines = content.count("\n") + 1
        new_h = min(max(40, lines * 24 + 16), 112)
        self.entry.configure(height=new_h)

    def _on_send(self) -> None:
        if self._entry_placeholder:
            return
        text = self.entry.get("1.0", "end-1c").strip()
        if not text:
            return
        self.entry.delete("1.0", "end")
        self.entry.configure(height=40)
        self._entry_placeholder = False
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
            self.record_btn.configure(
                text="⏹",
                fg_color=self.palette.error,
                hover_color=self.palette.error,
                text_color=self.palette.text_primary,
            )

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.record_btn.configure(
            text="🎤",
            fg_color=self.palette.surface,
            hover_color=self.palette.accent_dim,
            text_color=self.palette.accent,
        )
        wav_path = self.recorder.stop()
        self.set_status(STATUS_TRANSCRIBING)
        self._set_state("thinking")

        def worker() -> None:
            if wav_path is None:
                self.root.after(0, lambda: self.set_status("No audio captured — check mic"))
                self.root.after(0, lambda: self._set_state("idle"))
                return

            def on_status(msg: str) -> None:
                self.root.after(0, lambda: self.set_status(msg))

            text = self.transcriber.transcribe(wav_path, on_status=on_status)
            if text:
                self.root.after(0, lambda: self._submit(text))
            else:
                self.root.after(0, lambda: self.set_status("No speech detected — check logs"))
                self.root.after(0, lambda: self._set_state("idle"))

        threading.Thread(target=worker, daemon=True).start()

    # ── Transcript rendering ──────────────────────────────────────────────────

    def _clear_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def _append_message(self, role: str, text: str) -> None:
        labels = {
            "user": CONFIG.user_name,
            "assistant": "JARVIS",
            "system": "⚠ System",
        }
        label_tag = "label_user" if role == "user" else "label_assistant"
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"\n{labels.get(role, role)}\n", label_tag)
        if role == "assistant":
            self._insert_markdown(text, "assistant")
            self.transcript.insert("end", "\n")
        else:
            self.transcript.insert("end", f"{text}\n", role)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _start_assistant_message(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", "\nJARVIS\n", "label_assistant")
        # Mark where streamed content begins so we can replace it on finish.
        self.transcript.mark_set("_stream_start", "end")
        self.transcript.mark_gravity("_stream_start", "left")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _append_stream(self, chunk: str) -> None:
        """Insert raw streaming text; replaced with formatted version on finish."""
        self.transcript.configure(state="normal")
        self.transcript.insert("end", chunk, "assistant")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _finish_assistant_message(self, full_reply: str) -> None:
        """Replace raw streamed text with markdown-formatted version."""
        self.transcript.configure(state="normal")
        try:
            self.transcript.delete("_stream_start", "end")
        except Exception:  # noqa: BLE001
            pass
        self._insert_markdown(full_reply, "assistant")
        self.transcript.insert("end", "\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")
        self.set_status(STATUS_DONE)
        self._set_state("idle")
        self._set_inputs_enabled(True)
        self.entry.focus_set()

    # ── Markdown renderer ─────────────────────────────────────────────────────

    def _insert_markdown(self, text: str, base_tag: str) -> None:
        """Parse and insert markdown-formatted text into the transcript."""
        box = self.transcript
        in_code_block = False
        lines = text.rstrip("\n").split("\n")

        for i, line in enumerate(lines):
            nl = "\n" if i < len(lines) - 1 else ""

            if line.startswith("```"):
                in_code_block = not in_code_block
                continue

            if in_code_block:
                box.insert("end", line + nl, "md_code_block")
                continue

            if line.startswith("### "):
                box.insert("end", line[4:] + nl, (base_tag, "md_h3"))
            elif line.startswith("## "):
                box.insert("end", line[3:] + nl, (base_tag, "md_h2"))
            elif line.startswith("# "):
                box.insert("end", line[2:] + nl, (base_tag, "md_h1"))
            elif re.match(r"^[-*] ", line):
                box.insert("end", "  • ", "md_bullet")
                self._insert_inline(line[2:], base_tag)
                box.insert("end", nl)
            elif re.match(r"^\d+\. ", line):
                m = re.match(r"^(\d+\.) (.*)", line)
                if m:
                    box.insert("end", f"  {m.group(1)} ", "md_bullet")
                    self._insert_inline(m.group(2), base_tag)
                    box.insert("end", nl)
                else:
                    self._insert_inline(line, base_tag)
                    box.insert("end", nl)
            else:
                self._insert_inline(line, base_tag)
                box.insert("end", nl)

    def _insert_inline(self, text: str, base_tag: str) -> None:
        """Insert a line of text with bold/italic/code spans parsed out."""
        box = self.transcript
        for part in _INLINE_RE.split(text):
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                box.insert("end", part[2:-2], (base_tag, "md_bold"))
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                box.insert("end", part[1:-1], (base_tag, "md_italic"))
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                box.insert("end", part[1:-1], "md_code")
            else:
                box.insert("end", part, base_tag)

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
