"""Does reported profit turn into cash? The block that lets an analyst check.

A company can report rising profit while collecting cash more slowly. Nothing
in this pipeline looked at that gap: the fundamentals analyst saw statements
and ratios, but never a reconciliation, so "profit grew 20%" passed unchallenged.

**What the free sources actually allow.** Checked 2026-09-25:

- yfinance has **annual** cash flow for NSE names with the line items that
  matter (net income, operating cash flow, capex, free cash flow, and the
  receivables/inventory/payables movements), but **no quarterly cash flow** for
  many of them — MARUTI returns nothing, TCS works. So the reconciliation is
  annual, full stop.
- screener.in has **13 quarters of P&L** (sales, operating margin, other
  income, tax, net profit) and its own annual **CFO/OP** ratio, which is an
  independent second opinion on the same question.

So the block has two halves: an annual cash reconciliation, and a quarterly
profit-quality read that never pretends to be about cash.

**The arithmetic is done here, not by the model.** Ratios computed in Python
are deterministic and cost no tokens to derive; a model asked to divide
numbers in a prompt sometimes gets it wrong and always spends tokens showing
its work.

**Freshness is labelled, not assumed.** screener's columns are quarter *ends*,
not filing dates. SEBI requires quarterly results within 45 days of quarter
end and annual results within 60, so a period older than that deadline was
certainly public on the analysis date; anything newer may not have been, and
is marked "?" rather than hidden. The quarterly half is live-runs-only anyway,
because screener serves current values with no publication dates (see
screener_in's module docstring).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .config import get_config
from .errors import VendorError
from .india_data_common import sentinel
from .screener_in import get_snapshot, screener_enabled
from .symbol_utils import is_india_ticker

logger = logging.getLogger(__name__)

# SEBI filing deadlines: quarterly results within 45 days of the quarter end,
# annual within 60. A period whose deadline has passed was certainly public.
QUARTER_DEADLINE_DAYS = 45
ANNUAL_DEADLINE_DAYS = 60

CRORE = 1e7  # yfinance reports absolute rupees; screener reports Rs crore
ANNUAL_YEARS = 3
QUARTERS_SHOWN = 4

# yfinance row labels vary by company and version, so each field matches on a
# fragment rather than an exact name.
_CF_ROWS = {
    "net_income": ("net income",),
    "cfo": ("operating cash flow", "total cash from operating"),
    "capex": ("capital expenditure",),
    "fcf": ("free cash flow",),
    "receivables": ("change in receivables", "changes in receivables"),
    "inventory": ("change in inventory",),
    "payables": ("change in payable",),
}


def _match(rows: list[str], fragments: tuple[str, ...]) -> str | None:
    for fragment in fragments:
        for row in rows:
            if fragment in row.lower():
                return row
    return None


def _as_date(value: str | date | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def was_public(period_end: date, as_of: date, deadline_days: int) -> bool:
    """Whether results for a period ending ``period_end`` must have been
    published by ``as_of``, going by SEBI's filing deadline."""
    return as_of >= period_end + timedelta(days=deadline_days)


def quarter_end(label: str) -> date | None:
    """screener's "Jun 2026" -> 2026-06-30 (the last day of that month)."""
    try:
        first = datetime.strptime(label.strip(), "%b %Y").date()
    except ValueError:
        return None
    next_month = first.replace(day=28) + timedelta(days=4)
    return next_month.replace(day=1) - timedelta(days=1)


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new / old - 1.0) * 100.0


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f}{unit}" if abs(value) >= 100 else f"{value:,.1f}{unit}"


def _signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.0f}%"


def annual_cash_conversion(ticker: str, as_of: date) -> list[str]:
    """Annual reconciliation lines from yfinance, newest first."""
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    stock = yf.Ticker(ticker)
    cash, income = stock.cashflow, stock.income_stmt
    if cash is None or cash.empty:
        return []
    rows = [str(i) for i in cash.index]
    picked = {key: _match(rows, frags) for key, frags in _CF_ROWS.items()}
    revenue_row = _match([str(i) for i in income.index], ("total revenue",)) if (
        income is not None and not income.empty) else None

    lines = [
        "| FY ending | Net profit | Cash from ops | CFO/PAT | Capex | FCF (CFO-capex) | public? |",
        "|---|---|---|---|---|---|---|",
    ]
    detail: list[str] = []
    for position, column in enumerate(list(cash.columns)[:ANNUAL_YEARS]):
        fy = column.date() if hasattr(column, "date") else _as_date(str(column))
        if fy > as_of:
            continue  # a period that had not ended by the analysis date
        def val(key: str, col=column):
            row = picked.get(key)
            if not row:
                return None
            try:
                raw = cash.loc[row, col]
            except KeyError:
                return None
            return None if raw != raw else float(raw) / CRORE  # NaN check

        net, cfo, capex, fcf = val("net_income"), val("cfo"), val("capex"), val("fcf")
        ratio = f"{cfo / net:.2f}" if (cfo is not None and net not in (None, 0)) else "n/a"
        derived = None if (cfo is None or capex is None) else cfo + capex  # capex is negative
        flag = "yes" if was_public(fy, as_of, ANNUAL_DEADLINE_DAYS) else "filing date unknown"
        lines.append(f"| {fy:%b-%Y} | {_fmt(net)} | {_fmt(cfo)} | {ratio} | "
                     f"{_fmt(abs(capex) if capex is not None else None)} | "
                     f"{_fmt(derived if derived is not None else fcf)} | {flag} |")

        # Working capital against sales, for the newest year only: the point is
        # whether receivables are outgrowing the business. Compared by position,
        # not identity — pandas hands back a fresh Timestamp each time, so an
        # `is` check silently never matched and this line went missing.
        if position == 0 and revenue_row is not None and len(income.columns) > 1:
            try:
                sales_now = float(income.loc[revenue_row, income.columns[0]])
                sales_prev = float(income.loc[revenue_row, income.columns[1]])
            except (KeyError, TypeError, ValueError):
                sales_now = sales_prev = 0.0
            moves = {name: val(name) for name in ("receivables", "inventory", "payables")}
            if any(v is not None for v in moves.values()):
                detail.append(
                    f"Working capital in {fy:%b-%Y} (a negative change consumed cash): "
                    + ", ".join(f"{k} {_fmt(v)}" for k, v in moves.items() if v is not None)
                    + f"; sales {_signed(_pct(sales_now, sales_prev))} year on year."
                )
    if len(lines) == 2:
        return []
    return lines + detail


def quarterly_profile(ticker: str, as_of: date) -> list[str]:
    """Quarterly P&L lines from screener, plus a trend over every quarter it
    carries. Empty when screener is off or the run is not live."""
    if not screener_enabled():
        return []
    snap = get_snapshot(ticker, as_of)  # raises for a past-dated run, by design
    quarters = [q for q in snap.quarters if quarter_end(q.quarter) is not None]
    if not quarters:
        return []

    lines = [
        "| Quarter | Sales | QoQ | Operating margin | Other income / PBT | Tax | Net profit "
        "| QoQ | public? |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    shown = quarters[-QUARTERS_SHOWN:]
    for index, q in enumerate(shown):
        previous = quarters[quarters.index(q) - 1] if quarters.index(q) > 0 else None
        share = None
        if q.other_income is not None and q.pbt not in (None, 0):
            share = q.other_income / q.pbt * 100.0
        end = quarter_end(q.quarter)
        flag = ("yes" if was_public(end, as_of, QUARTER_DEADLINE_DAYS)
                else "filing date unknown")
        lines.append(
            f"| {q.quarter} | {_fmt(q.sales)} | "
            f"{_signed(_pct(q.sales, previous.sales if previous else None))} | "
            f"{_fmt(q.opm_pct, '%')} | {_fmt(share, '%') if share is not None else 'n/a'} | "
            f"{_fmt(q.tax_pct, '%')} | {_fmt(q.net_profit)} | "
            f"{_signed(_pct(q.net_profit, previous.net_profit if previous else None))} | {flag} |"
        )
        del index

    # Trend across everything screener carries, so four quarters are read in
    # context rather than as a level.
    first, last = quarters[0], quarters[-1]
    trend = []
    if first.opm_pct is not None and last.opm_pct is not None:
        trend.append(f"operating margin {first.opm_pct:.1f}% -> {last.opm_pct:.1f}%")
    def share_of(q):
        return None if (q.other_income is None or q.pbt in (None, 0)) else q.other_income / q.pbt
    a, b = share_of(first), share_of(last)
    if a is not None and b is not None:
        trend.append(f"other income as a share of pre-tax profit {a:.0%} -> {b:.0%}")
    if trend:
        lines.append(f"Across {len(quarters)} quarters ({first.quarter} to {last.quarter}): "
                     + "; ".join(trend) + ".")
    cfo_series = [(y, v.get("cfo_over_op")) for y, v in snap.annual_cash.items()
                  if v.get("cfo_over_op") is not None]
    if cfo_series:
        recent = cfo_series[-ANNUAL_YEARS:]
        lines.append(
            "screener's own cash-from-operations over operating-profit ratio: "
            + ", ".join(f"{y} {v:.0f}%" for y, v in recent)
            + ". This is a second source computing the same question — if it disagrees with "
              "CFO/PAT above, say so rather than choosing one."
        )
    return lines


def earnings_quality_block(ticker: str, curr_date: str | date | None = None) -> str:
    """The prompt block. ``""`` for non-Indian tickers; a sentinel on failure."""
    if not is_india_ticker(ticker) or not get_config().get("india_data_enabled", True):
        return ""
    as_of = _as_date(curr_date)

    try:
        annual = annual_cash_conversion(ticker, as_of)
    except Exception as exc:  # noqa: BLE001 — context must never fail an analysis
        logger.warning("annual cash conversion unavailable for %s: %s", ticker, exc)
        annual = []
    try:
        quarterly = quarterly_profile(ticker, as_of)
    except VendorError as exc:
        logger.info("quarterly profile unavailable for %s: %s", ticker, exc)
        quarterly = [sentinel(f"quarterly P&L for {ticker}", getattr(exc, "reason", exc))]
    except Exception as exc:  # noqa: BLE001
        logger.warning("quarterly profile failed for %s: %s", ticker, exc)
        quarterly = [sentinel(f"quarterly P&L for {ticker}", f"{type(exc).__name__}")]

    if not annual and not quarterly:
        return "\n\n### Earnings quality\n" + sentinel(
            f"earnings-to-cash figures for {ticker}", "no statements available from any source")

    out = [
        "\n\n### Earnings quality: does reported profit become cash?",
        "Every ratio below is computed from the filed statements, so use them as given rather "
        "than recomputing. Figures are Rs crore unless marked. \"public?\" says whether SEBI's "
        "filing deadline for that period had passed on the analysis date: \"filing date unknown\" "
        "means the results may not have been public yet, so lean on the older periods for "
        "anything load-bearing.",
    ]
    if annual:
        out += ["", "**Annual cash reconciliation** (the only period where cash data exists):",
                *annual]
    if quarterly:
        out += ["", "**Quarterly profit quality** (no free source publishes quarterly cash flow "
                "for NSE companies, so do not infer cash conversion from these):", *quarterly]
    out.append(
        "\nRead this as evidence, not a verdict: a gap between profit and cash can be timing "
        "(a large order collected next quarter) or deterioration (customers not paying). Say "
        "which the evidence supports, or that it cannot be told apart yet. Do not call it fraud "
        "or poor earnings quality without more than one period pointing the same way, and state "
        "which capex definition you used whenever you quote free cash flow."
    )
    return "\n".join(out)
