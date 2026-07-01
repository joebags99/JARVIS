"""Logging configuration for JARVIS.

All errors and lifecycle events are written to ``logs/jarvis.log`` with
timestamps (per the spec). A rotating file handler keeps the log from growing
without bound.

Every log line also carries a **turn correlation id** so the several log
statements one user question produces (the send() call, each tool it invokes,
the integration calls those tools make) can be grepped together, e.g.
``grep '[a1b2c3d4]' logs/jarvis.log``. Call :func:`new_turn_id` once per user
turn (see ``ClaudeClient.send``); everything logged on that thread afterward
picks it up automatically via a context variable, so call sites elsewhere in
the codebase don't need to pass an id around.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE

_CONFIGURED = False

# contextvars (not a plain global) so concurrent turns on different threads
# each see their own id instead of clobbering one another.
_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar("turn_id", default="-")


def new_turn_id() -> str:
    """Start a new turn: generate a short id, activate it, and return it."""
    turn_id = uuid.uuid4().hex[:8]
    _turn_id.set(turn_id)
    return turn_id


class _TurnIdFilter(logging.Filter):
    """Stamps each log record with the current thread's active turn id."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.turn_id = _turn_id.get()
        return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once and return the JARVIS logger."""
    global _CONFIGURED

    logger = logging.getLogger("jarvis")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | [%(turn_id)s] | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    turn_filter = _TurnIdFilter()

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(turn_filter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(turn_filter)
    logger.addHandler(console)

    # Quiet down noisy third-party libraries.
    for noisy in ("httpx", "urllib3", "google", "faster_whisper", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str = "jarvis") -> logging.Logger:
    """Return a child logger under the configured ``jarvis`` logger."""
    if name == "jarvis":
        return logging.getLogger("jarvis")
    return logging.getLogger(f"jarvis.{name}")
