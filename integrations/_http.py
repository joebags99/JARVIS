"""Shared HTTP resilience policy for the integrations.

Todoist, Spotify, and the Google APIs all wrap their network calls in the same
write-safe retry-and-backoff policy: transient gateway/rate-limit failures are
retried with exponential backoff, while an ambiguous 500 or a mid-flight drop on
a *write* is surfaced rather than risk a duplicate. That policy used to be
copy-pasted into each integration; it now lives here so there is one definition
to reason about and tune.

Two entry points:
  * ``request_with_retries`` — drives a ``requests`` call (Todoist, Spotify).
  * ``should_retry_status`` / ``backoff_delay`` — the bare policy, reused by the
    Google integration whose transport raises ``HttpError`` from ``.execute()``
    rather than going through ``requests``.

This module deliberately depends only on ``requests`` + the stdlib so it stays
cheap to import and test (no app/config import, no dotenv side effects).
"""

from __future__ import annotations

import logging
import time

import requests

# 502/503/504 (gateway/unavailable) and 429 (rate limit) mean the request almost
# certainly wasn't applied, so they're safe to retry even for writes. 500 is
# ambiguous for a write (it may have taken effect), so it's only retried for
# idempotent calls. Non-transient errors (400/401/403/404…) are never retried.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5  # seconds → 0.5s, 1.0s between attempts

_log = logging.getLogger("jarvis.http")


def backoff_delay(attempt: int) -> float:
    """Exponential backoff for a 1-based attempt number: 0.5s, 1.0s, 2.0s, …"""
    return BACKOFF_BASE * (2 ** (attempt - 1))


def should_retry_status(status: int | None, idempotent: bool) -> bool:
    """Whether an HTTP status warrants a retry under the write-safe policy."""
    if status not in RETRY_STATUSES:
        return False
    return status != 500 or idempotent


def request_with_retries(
    method: str,
    url: str,
    *,
    idempotent: bool | None = None,
    honor_retry_after: bool = False,
    always_retry_connection_error: bool = False,
    label: str = "http",
    logger: logging.Logger | None = None,
    **kwargs,
) -> requests.Response:
    """``requests.request`` with auth-agnostic transient-failure retries.

    Callers inject their own auth headers/url before calling. ``idempotent``
    defaults to ``method == "GET"``; pass it explicitly for a safe-to-retry
    write. Behavioural knobs preserve each integration's original policy:

    * ``honor_retry_after`` — on a 429, wait at least the server's ``Retry-After``
      seconds (Spotify).
    * ``always_retry_connection_error`` — treat a ``ConnectionError`` (connection
      never established) as safe to retry even for writes, since the request
      never reached the server (Todoist).

    Non-transient errors and the final attempt raise. The ``Timeout`` clause is
    checked before ``ConnectionError`` so a ``ConnectTimeout`` (a subclass of
    both) keeps its timeout semantics.
    """
    log = logger or _log
    if idempotent is None:
        idempotent = method.upper() == "GET"
    kwargs.setdefault("timeout", 15)

    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = backoff_delay(attempt)
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if not should_retry_status(status, idempotent) or attempt == MAX_ATTEMPTS:
                raise
            last_exc = exc
            if honor_retry_after and status == 429:
                retry_after = (exc.response.headers or {}).get("Retry-After", "")
                if str(retry_after).isdigit():
                    delay = max(delay, int(retry_after))
        except requests.Timeout as exc:
            # A write may have landed server-side before timing out, so only
            # retry timeouts on idempotent calls to avoid a duplicate.
            if not idempotent or attempt == MAX_ATTEMPTS:
                raise
            last_exc = exc
        except requests.ConnectionError as exc:
            retryable = always_retry_connection_error or idempotent
            if not retryable or attempt == MAX_ATTEMPTS:
                raise
            last_exc = exc

        log.warning(
            "%s %s %s failed (attempt %d/%d: %s); retrying in %.1fs",
            label, method, url, attempt, MAX_ATTEMPTS, last_exc, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover — loop always returns or raises above
