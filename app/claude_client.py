"""Anthropic Claude API client with streaming and context injection.

Maintains the per-session conversation history and streams responses token by
token via a callback so the overlay can update the UI in real time. All network
work happens on a background thread (the caller is responsible for that); this
module is intentionally synchronous and stream-oriented.
"""

from __future__ import annotations

from typing import Callable

from .config import CONFIG
from .context_builder import ContextBuilder
from .logging_setup import get_logger

log = get_logger("claude")

MAX_TOKENS = 1024


class ClaudeClient:
    """Wraps the Anthropic SDK and owns session conversation history."""

    def __init__(self, context_builder: ContextBuilder | None = None) -> None:
        self._client = None
        self._init_error: str | None = None
        self.context = context_builder or ContextBuilder()
        # Session memory: list of {"role": ..., "content": ...} dicts.
        self.history: list[dict] = []
        self._init_client()

    def _init_client(self) -> None:
        if not CONFIG.has_anthropic_key:
            self._init_error = (
                "No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file."
            )
            log.error(self._init_error)
            return
        try:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=CONFIG.anthropic_api_key)
            log.info("Anthropic client ready (model=%s)", CONFIG.anthropic_model)
        except ImportError:
            self._init_error = "The 'anthropic' package is not installed."
            log.error(self._init_error)
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"Could not initialize Anthropic client: {exc}"
            log.error(self._init_error)

    @property
    def ready(self) -> bool:
        return self._client is not None

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def reset_session(self) -> None:
        """Clear conversation history (called when overlay reopens)."""
        self.history.clear()
        log.info("session history cleared")

    def reload_context(self) -> None:
        self.context.reload_static()

    def send(
        self,
        user_message: str,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        """Send a message with full context; stream deltas via ``on_delta``.

        Returns the complete assistant reply. Raises no network errors to the
        caller — failures return a readable error string instead.
        """
        if not self.ready:
            return self._init_error or "Claude client is not available."

        self.history.append({"role": "user", "content": user_message})
        system_prompt = self.context.build_system_prompt()

        full_text = ""
        try:
            with self._client.messages.stream(
                model=CONFIG.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=self.history,
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    if on_delta:
                        on_delta(text)
            self.history.append({"role": "assistant", "content": full_text})
            log.info("response received (%d chars)", len(full_text))
            return full_text
        except Exception as exc:  # noqa: BLE001
            log.error("Claude API call failed: %s", exc)
            # Roll back the unanswered user turn so history stays consistent.
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
            return f"⚠️ Error contacting Claude: {exc}"
