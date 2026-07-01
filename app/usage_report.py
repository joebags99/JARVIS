"""Token-free report over the usage log — your credit/token & cache dashboard.

Reads ``logs/usage.jsonl`` (written by :mod:`app.usage` on every API call) and
prints totals, cache hit rate, and estimated spend. It never calls the API, so
it costs nothing. Run from the repo root:

    python -m app.usage_report                 # all-time totals + per-model breakdown
    python -m app.usage_report --since 2026-06-01
    python -m app.usage_report --by-day        # daily rows (spot spend spikes)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import LOGS_DIR


def load(path: str | Path = LOGS_DIR / "usage.jsonl", since: str | None = None) -> list[dict]:
    """Read usage rows, optionally filtered to ts >= *since* (YYYY-MM-DD)."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if since and str(row.get("ts", "")) < since:
            continue
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Aggregate rows into totals + a cache hit rate + cost."""
    agg = {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost_usd": 0.0}
    for r in rows:
        agg["calls"] += 1
        for k in ("input", "output", "cache_write", "cache_read"):
            agg[k] += int(r.get(k, 0) or 0)
        agg["cost_usd"] += float(r.get("cost_usd", 0) or 0)
    total_input = agg["input"] + agg["cache_write"] + agg["cache_read"]
    agg["total_input"] = total_input
    agg["cache_hit_rate"] = (agg["cache_read"] / total_input) if total_input else 0.0
    return agg


def _group(rows: list[dict], key) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def _line(label: str, a: dict) -> str:
    return (f"{label:<22} {a['calls']:>5} calls  in={a['total_input']:>9,}  "
            f"out={a['output']:>8,}  cache hit {100 * a['cache_hit_rate']:>3.0f}%  "
            f"${a['cost_usd']:>9.4f}")


def render(rows: list[dict], by_day: bool = False) -> str:
    if not rows:
        return "No usage recorded yet. Talk to JARVIS, then re-run this."
    lines = ["JARVIS — token & cache usage", "=" * 78, _line("TOTAL", summarize(rows)), ""]
    lines.append("By model:")
    for model, group in sorted(_group(rows, lambda r: r.get("model", "?")).items()):
        lines.append("  " + _line(model, summarize(group)))
    lines.append("")
    lines.append("By kind:")
    for kind, group in sorted(_group(rows, lambda r: r.get("kind", "?")).items()):
        lines.append("  " + _line(kind, summarize(group)))
    if by_day:
        lines.append("")
        lines.append("By day:")
        for day, group in sorted(_group(rows, lambda r: str(r.get("ts", ""))[:10]).items()):
            lines.append("  " + _line(day, summarize(group)))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usage_report", description="JARVIS token/cache usage report.")
    p.add_argument("--since", help="Only include entries on/after this date (YYYY-MM-DD).")
    p.add_argument("--by-day", action="store_true", help="Add a per-day breakdown.")
    args = p.parse_args(argv)
    print(render(load(since=args.since), by_day=args.by_day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
