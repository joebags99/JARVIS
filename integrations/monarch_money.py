"""Monarch Money integration for JARVIS.

Fetches live financial data (accounts, cashflow, spending by category) from
Monarch Money. Uses a pickled session file so re-login only happens when the
session expires (typically several months).

Session cache: tokens/monarch_session.pickle
"""

from __future__ import annotations

import asyncio
import datetime as dt

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger

log = get_logger("monarch")

SESSION_PATH = ROOT_DIR / "tokens" / "monarch_session.pickle"


async def _get_client():
    """Return an authenticated MonarchMoney client, reusing cached session if valid."""
    try:
        from monarchmoney import MonarchMoney, RequireMFAException
    except ImportError:
        raise RuntimeError(
            "monarchmoney package not installed. Run: pip install monarchmoney"
        )

    if not CONFIG.monarch_email or not CONFIG.monarch_password:
        raise RuntimeError(
            "Set MONARCH_EMAIL and MONARCH_PASSWORD in your .env file to enable "
            "Monarch Money integration."
        )

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    mm = MonarchMoney(session_file=str(SESSION_PATH))

    try:
        await mm.login(
            email=CONFIG.monarch_email,
            password=CONFIG.monarch_password,
            mfa_secret_key=CONFIG.monarch_mfa_secret or None,
            use_saved_session=True,
            save_session=True,
        )
    except RequireMFAException:
        raise RuntimeError(
            "Monarch Money requires MFA. Add MONARCH_MFA_SECRET to your .env — "
            "find the secret in Monarch → Settings → Security → Authenticator App "
            "→ 'show secret key'."
        )

    log.debug("monarch client ready")
    return mm


def _month_range() -> tuple[str, str]:
    today = dt.date.today()
    start = today.replace(day=1)
    if today.month == 12:
        end = today.replace(year=today.year + 1, month=1, day=1) - dt.timedelta(days=1)
    else:
        end = today.replace(month=today.month + 1, day=1) - dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


async def _build_summary() -> str:
    mm = await _get_client()
    start, end = _month_range()
    month_label = dt.date.today().strftime("%B %Y")

    cashflow_data, accounts_data = await asyncio.gather(
        mm.get_cashflow(start_date=start, end_date=end),
        mm.get_accounts(),
    )

    lines: list[str] = [f"## Financial Snapshot — {month_label}"]

    # ── Month-to-date overview ────────────────────────────────────────────────
    raw_summary = cashflow_data.get("summary", [])
    if raw_summary:
        s = raw_summary[0] if isinstance(raw_summary, list) else raw_summary
        income = s.get("sumIncome") or 0
        expenses = abs(s.get("sumExpense") or 0)
        savings = s.get("savings") or 0
        rate = s.get("savingsRate") or 0
        lines += [
            "\n### Month-to-Date",
            f"Income:   {_money(income)}",
            f"Expenses: {_money(expenses)}",
            f"Savings:  {_money(savings)} ({rate * 100:.1f}%)",
        ]

    # ── Top spending categories ────────────────────────────────────────────────
    by_cat = cashflow_data.get("byCategory", [])
    expense_cats: list[tuple[str, float]] = []
    for c in by_cat:
        # The library may use 'sum' or nest under 'summary.sumExpense'
        amt = c.get("sum") or c.get("summary", {}).get("sumExpense") or 0
        if amt < 0:
            name = c.get("category", {}).get("name", "Unknown")
            expense_cats.append((name, abs(amt)))
    expense_cats.sort(key=lambda x: x[1], reverse=True)

    if expense_cats:
        lines.append("\n### Top Spending Categories (this month)")
        for name, amt in expense_cats[:10]:
            lines.append(f"  {name}: {_money(amt)}")

    # ── Accounts & net worth ──────────────────────────────────────────────────
    accounts = accounts_data.get("accounts", [])
    assets = [
        a for a in accounts
        if a.get("isAsset") and not a.get("isHidden") and a.get("includeInNetWorth")
    ]
    liabilities = [
        a for a in accounts
        if not a.get("isAsset") and not a.get("isHidden") and a.get("includeInNetWorth")
    ]

    if assets or liabilities:
        lines.append("\n### Accounts")
        if assets:
            lines.append("Assets:")
            for a in sorted(assets, key=lambda x: abs(x.get("currentBalance") or 0), reverse=True):
                lines.append(f"  {a.get('displayName', '?')}: {_money(a.get('currentBalance') or 0)}")
        if liabilities:
            lines.append("Liabilities:")
            for a in sorted(liabilities, key=lambda x: abs(x.get("currentBalance") or 0), reverse=True):
                bal = abs(a.get("currentBalance") or 0)
                lines.append(f"  {a.get('displayName', '?')}: -{_money(bal)}")

        total_assets = sum(a.get("currentBalance") or 0 for a in assets)
        total_liab = sum(abs(a.get("currentBalance") or 0) for a in liabilities)
        lines.append(f"\nNet Worth: {_money(total_assets - total_liab)}")

    return "\n".join(lines)


def get_financial_summary() -> str:
    """Fetch a financial snapshot from Monarch Money. Safe to call from any thread."""
    try:
        return asyncio.run(_build_summary())
    except RuntimeError as exc:
        if "event loop" in str(exc).lower():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_build_summary())
            finally:
                loop.close()
        log.error("get_financial_summary failed: %s", exc)
        return f"[Monarch Money error: {exc}]"
    except Exception as exc:  # noqa: BLE001
        log.error("get_financial_summary failed: %s", exc)
        return f"[Monarch Money error: {exc}]"
