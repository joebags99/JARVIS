"""Tests for usage/cache diagnostics (app/usage.py + app/usage_report.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app import usage, usage_report


def _sdk(**kw):
    """A stand-in for an SDK usage object (fields may be missing or None)."""
    return SimpleNamespace(**kw)


# ── Usage extraction ────────────────────────────────────────────────────────
def test_from_sdk_coerces_missing_and_none():
    u = usage.Usage.from_sdk(_sdk(input_tokens=100, output_tokens=50,
                                  cache_read_input_tokens=None))  # cache_write absent
    assert (u.input, u.output, u.cache_read, u.cache_write) == (100, 50, 0, 0)


def test_total_input_and_cache_hit_rate():
    u = usage.Usage(input=200, cache_read=800, cache_write=0, output=10)
    assert u.total_input == 1000
    assert u.cache_hit_rate == 0.8
    assert usage.Usage().cache_hit_rate == 0.0   # no divide-by-zero


# ── Cost math (Sonnet: $3 in / $15 out; write 1.25x; read 0.1x) ───────────────
def test_estimate_cost_components():
    m = "claude-sonnet-4-6"
    assert usage.estimate_cost(m, usage.Usage(input=1_000_000)) == 3.0
    assert usage.estimate_cost(m, usage.Usage(output=1_000_000)) == 15.0
    assert usage.estimate_cost(m, usage.Usage(cache_write=1_000_000)) == 3.75   # 1.25x
    assert round(usage.estimate_cost(m, usage.Usage(cache_read=1_000_000)), 6) == 0.30  # 0.1x


def test_unknown_model_uses_default_price():
    # Falls back to Sonnet-tier rather than $0, so cost is never silently zero.
    assert usage.estimate_cost("made-up-model", usage.Usage(input=1_000_000)) == 3.0


# ── Tracker: accumulation + JSONL logging ─────────────────────────────────────
def test_tracker_records_accumulates_and_logs(tmp_path):
    t = usage.UsageTracker(log_path=tmp_path / "u.jsonl")
    c1 = t.record("claude-sonnet-4-6",
                  _sdk(input_tokens=100, output_tokens=20,
                       cache_creation_input_tokens=0, cache_read_input_tokens=900),
                  kind="turn")
    t.record("claude-haiku-4-5",
             _sdk(input_tokens=50, output_tokens=10), kind="summary")

    assert c1 > 0
    assert t.calls == 2
    assert t.session.output == 30
    assert t.session.total_input == 100 + 900 + 50
    assert t.session_cost > 0

    rows = [json.loads(line) for line in (tmp_path / "u.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["kind"] == "turn" and rows[0]["cache_read"] == 900
    assert {"ts", "model", "kind", "input", "output", "cache_read", "cost_usd"} <= rows[0].keys()


def test_reset_session_clears(tmp_path):
    t = usage.UsageTracker(log_path=tmp_path / "u.jsonl")
    t.record("claude-sonnet-4-6", _sdk(input_tokens=10, output_tokens=5))
    t.reset_session()
    assert t.calls == 0 and t.session_cost == 0.0 and t.session.total_input == 0


def test_record_never_raises_on_bad_usage(tmp_path):
    t = usage.UsageTracker(log_path=tmp_path / "u.jsonl")
    assert t.record("claude-sonnet-4-6", None) == 0.0   # None usage → no crash
    assert t.calls == 0


# ── Report aggregation ────────────────────────────────────────────────────────
def test_report_summarize_and_render(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text(
        "\n".join(json.dumps(r) for r in [
            {"ts": "2026-06-24T09:00:00", "model": "claude-sonnet-4-6", "kind": "turn",
             "input": 100, "output": 20, "cache_write": 0, "cache_read": 900, "cost_usd": 0.001},
            {"ts": "2026-06-25T09:00:00", "model": "claude-haiku-4-5", "kind": "summary",
             "input": 50, "output": 10, "cache_write": 0, "cache_read": 0, "cost_usd": 0.0001},
        ]) + "\n",
        encoding="utf-8",
    )
    rows = usage_report.load(log)
    agg = usage_report.summarize(rows)
    assert agg["calls"] == 2
    assert agg["total_input"] == 100 + 900 + 50
    assert round(agg["cache_hit_rate"], 4) == round(900 / 1050, 4)

    # since-filter drops the earlier day
    assert len(usage_report.load(log, since="2026-06-25")) == 1

    out = usage_report.render(rows, by_day=True)
    assert "TOTAL" in out and "claude-sonnet-4-6" in out and "By day:" in out


def test_report_empty(tmp_path):
    assert "No usage recorded" in usage_report.render(usage_report.load(tmp_path / "nope.jsonl"))
