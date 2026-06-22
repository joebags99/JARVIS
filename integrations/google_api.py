"""Shared resilience helper for the Google API integrations.

Google's APIs (Calendar, Gmail, Drive) occasionally return a transient 5xx /
429 or drop the connection mid-request. Rather than fail the user's command on
a momentary blip, ``execute`` wraps a googleapiclient request's ``.execute()``
with retry-and-backoff — the same write-safe policy the Todoist integration
uses, so a retry can never silently duplicate a created event or draft.
"""

from __future__ import annotations

import socket
import ssl
import time

from app.logging_setup import get_logger

log = get_logger("google_api")

# 502/503/504 (gateway/unavailable) and 429 (rate limit) mean the request wasn't
# applied → safe to retry even for writes. 500 is ambiguous for a write (it may
# have taken effect), so it's only retried for idempotent calls.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # seconds → 0.5s, 1.0s between attempts

# Network-level failures before a response arrived.
_TRANSIENT_NET = (
    socket.timeout,
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
    BrokenPipeError,
)


def _status_of(exc: Exception) -> int | None:
    """Best-effort HTTP status from a googleapiclient HttpError across versions."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def execute(request, *, idempotent: bool = True, label: str = "google api"):
    """Call ``request.execute()`` with transient-failure retries.

    ``idempotent`` must be False for non-idempotent writes (e.g. events.insert,
    drafts.create) where a retry could create a duplicate — those retry only on
    statuses that guarantee the request never reached/affected the server, and
    never on an ambiguous network drop or 500. Non-transient errors (400/401/
    403/404…) always raise immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except Exception as exc:  # noqa: BLE001
            status = _status_of(exc)
            if status is not None:
                retryable = status in _RETRY_STATUSES and (status != 500 or idempotent)
            elif isinstance(exc, _TRANSIENT_NET):
                # A drop before a response: safe to retry reads; for a write it
                # may already have landed, so don't risk a duplicate.
                retryable = idempotent
            else:
                retryable = False  # programming/auth/other error → surface it

            if not retryable or attempt == _MAX_ATTEMPTS:
                raise
            last_exc = exc

        delay = _BACKOFF_BASE * (2 ** (attempt - 1))
        log.warning(
            "%s failed (attempt %d/%d: %s); retrying in %.1fs",
            label, attempt, _MAX_ATTEMPTS, last_exc, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover — loop always returns or raises above
