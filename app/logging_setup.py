"""Logging configuration for JARVIS.

All errors and lifecycle events are written to ``logs/jarvis.log`` with
timestamps (per the spec). A rotating file handler keeps the log from growing
without bound.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once and return the JARVIS logger."""
    global _CONFIGURED

    logger = logging.getLogger("jarvis")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
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
