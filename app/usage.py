"""Cache & token usage diagnostics.

Every Claude API response carries a ``usage`` block — input/output tokens plus
the prompt-cache counters (``cache_read_input_tokens`` served cheaply from cache,
``cache_creation_input_tokens`` written to it). JARVIS captures that on every call
so you can see, per turn and per session:

* how many tokens you're spending (and where),
* how well prompt caching is working (the cache **hit rate** — cache reads as a
  share of total input), and
* an **estimated cost** in USD.

Per-call rows are appended to ``logs/usage.jsonl`` (gitignored) for historical
analysis via ``python -m app.usage_report``; a one-line summary is also logged.
Everything is best-effort — a logging failure never breaks a conversation.

Prices change. The defaults below are Anthropic's published per-1M-token rates;
override them without editing code by dropping a ``jarvis_usage_prices.json`` at
the repo root, e.g. ``{"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}}``.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from .config import LOGS_DIR, ROOT_DIR
from .logging_setup import get_logger

log = get_logger("usage")

USAGE_LOG = LOGS_DIR / "usage.jsonl"

# Per-1M-token USD prices (input / output). Source: Anthropic pricing.
_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
# Cache-tier multipliers on the input price. JARVIS caches with the default
# 5-minute ephemeral TTL, so writes are 1.25x; reads are always 0.1x.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10
# Fallback when a model isn't in the table (assume Sonnet-tier rather than $0).
_DEFAULT_PRICE = {"input": 3.0, "output": 15.0}


def _load_price_overrides() -> None:
    path = ROOT_DIR / "jarvis_usage_prices.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for model, price in (data or {}).items():
            _PRICES[model] = {**_PRICES.get(model, {}), **price}
        log.info("loaded usage price overrides for %d model(s)", len(data))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read jarvis_usage_prices.json: %s", exc)


_load_price_overrides()


def _price_for(model: str) -> dict[str, float]:
    return _PRICES.get(model, _DEFAULT_PRICE)


@dataclass
class Usage:
    """The four token counts that matter for cost + cache diagnostics."""

    input: int = 0          # uncached input tokens (full price)
    output: int = 0
    cache_write: int = 0    # cache_creation_input_tokens (1.25x input)
    cache_read: int = 0     # cache_read_input_tokens (0.1x input)

    @classmethod
    def from_sdk(cls, sdk_usage) -> Usage:
        """Pull the counts off an SDK ``usage`` object, coercing missing/None to 0."""
        def g(name: str) -> int:
            return int(getattr(sdk_usage, name, 0) or 0)

        return cls(
            input=g("input_tokens"),
            output=g("output_tokens"),
            cache_write=g("cache_creation_input_tokens"),
            cache_read=g("cache_read_input_tokens"),
        )

    @property
    def total_input(self) -> int:
        """Full prompt size: uncached + cache writes + cache reads."""
        return self.input + self.cache_write + self.cache_read

    @property
    def cache_hit_rate(self) -> float:
        """Share of input served from cache (0–1). Higher = caching is working."""
        return self.cache_read / self.total_input if self.total_input else 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input + other.input,
            self.output + other.output,
            self.cache_write + other.cache_write,
            self.cache_read + other.cache_read,
        )


def estimate_cost(model: str, u: Usage) -> float:
    """Estimated USD cost of one call's usage, applying the cache multipliers."""
    p = _price_for(model)
    inp, out = p["input"], p["output"]
    return (
        u.input * inp
        + u.cache_write * inp * _CACHE_WRITE_MULT
        + u.cache_read * inp * _CACHE_READ_MULT
        + u.output * out
    ) / 1_000_000


class UsageTracker:
    """Accumulates usage for the current session and logs every call."""

    def __init__(self, log_path: str | Path = USAGE_LOG) -> None:
        self._log_path = Path(log_path)
        self.session = Usage()
        self.session_cost = 0.0
        self.calls = 0

    def record(self, model: str, sdk_usage, kind: str = "turn") -> float:
        """Record one API call's usage; returns its estimated cost. Never raises."""
        if sdk_usage is None:
            return 0.0
        try:
            u = Usage.from_sdk(sdk_usage)
        except Exception as exc:  # noqa: BLE001
            log.debug("usage record skipped (bad usage object): %s", exc)
            return 0.0
        cost = estimate_cost(model, u)
        self.session += u
        self.session_cost += cost
        self.calls += 1
        self._append(model, kind, u, cost)
        log.info(
            "usage[%s] in=%d (cache: read=%d write=%d, hit %.0f%%) out=%d | "
            "$%.5f (session $%.4f)",
            kind, u.input, u.cache_read, u.cache_write, 100 * u.cache_hit_rate,
            u.output, cost, self.session_cost,
        )
        return cost

    def _append(self, model: str, kind: str, u: Usage, cost: float) -> None:
        try:
            row = {
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "model": model, "kind": kind,
                "input": u.input, "output": u.output,
                "cache_write": u.cache_write, "cache_read": u.cache_read,
                "cost_usd": round(cost, 6),
            }
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.debug("usage log write failed: %s", exc)

    def session_summary(self) -> str:
        s = self.session
        return (
            f"session usage: {self.calls} call(s), input={s.total_input} "
            f"(cache hit {100 * s.cache_hit_rate:.0f}%), output={s.output}, "
            f"est ${self.session_cost:.4f}"
        )

    def reset_session(self) -> None:
        """Log the session total (if any calls ran) and clear the accumulator."""
        if self.calls:
            log.info(self.session_summary())
        self.session = Usage()
        self.session_cost = 0.0
        self.calls = 0


_TRACKER: UsageTracker | None = None


def get_tracker() -> UsageTracker:
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = UsageTracker()
    return _TRACKER


def record(model: str, sdk_usage, kind: str = "turn") -> float:
    """Module-level convenience: record a call on the process-wide tracker."""
    return get_tracker().record(model, sdk_usage, kind)
