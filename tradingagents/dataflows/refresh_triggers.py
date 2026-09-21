"""Decide which tickers deserve a fresh (LLM) analysis today.

A full analysis costs ~15 LLM requests, and the free Gemini tier allows 500 a
day, so re-running all 50 Nifty names daily is impossible — and pointless: most
of what a report rests on (quarterly shareholding, statements, trend) does not
change overnight. Re-running an unchanged stock buys the same "Hold" again.

So the whole universe is *scanned* every day using only free data — no LLM —
and a ticker is re-analysed only when something actually happened to it:

    never analysed     no report exists yet
    price move         close moved >= N% since the day of its last report
    material filing    a non-routine NSE announcement filed after that report
                       (routine ones — newspaper notices, conference
                       intimations — are ignored; see nse_india triage)
    new shareholding   a quarterly shareholding filing published after it
    stale              the last report is older than a maximum age

Each trigger carries a weight; tickers are ranked by total weight and the day's
run is capped, so the scan can flag everything worth a look without ever being
able to exhaust the quota. Anything over the cap is deferred, not dropped —
its triggers are still true tomorrow, when it will rank again.

Everything here is pure except the two NSE lookups, which are injected so the
decision logic is testable without a network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .errors import VendorError
from .safe_io import read_complete_rows

logger = logging.getLogger(__name__)

# Weights. A never-analysed ticker always outranks everything; beyond that a
# large move dominates, then new information (filings), then plain age.
W_NEVER = 1000.0
W_PRICE_PER_PCT = 10.0  # 5% move -> 50
W_FILING = 30.0         # per material filing, capped below
MAX_FILINGS_COUNTED = 3
W_SHAREHOLDING = 40.0
W_STALE = 20.0          # plus 1 per day beyond the limit


@dataclass(frozen=True)
class LastReport:
    """The most recent successful analysis of a ticker."""

    ticker: str
    date: date          # the trade date analysed
    signal: str
    run_at: datetime    # when it actually ran; filings after this are new


@dataclass(frozen=True)
class Trigger:
    kind: str
    detail: str
    weight: float


@dataclass
class Assessment:
    ticker: str
    last: LastReport | None
    triggers: list[Trigger] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # lookups that failed

    @property
    def score(self) -> float:
        return sum(t.weight for t in self.triggers)

    @property
    def triggered(self) -> bool:
        return bool(self.triggers)


def load_last_reports(csv_path: Path) -> dict[str, LastReport]:
    """Latest successful run per ticker from the batch runner's CSV."""
    latest: dict[str, LastReport] = {}
    for row in read_complete_rows(csv_path):  # skips a row torn by a crash
        if row.get("status") != "ok":
            continue
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
            run_at = datetime.fromisoformat(row["run_at"])
        except (KeyError, ValueError):
            continue
        current = latest.get(row["ticker"])
        if current is None or (d, run_at) > (current.date, current.run_at):
            latest[row["ticker"]] = LastReport(row["ticker"], d, row.get("signal", ""), run_at)
    return latest


def close_on_or_before(closes: pd.Series, day: date) -> float | None:
    """The last close at or before ``day`` (reports can be dated a weekend)."""
    upto = closes[closes.index.date <= day].dropna()
    return float(upto.iloc[-1]) if len(upto) else None


def price_move_trigger(
    closes: pd.Series, since: date, threshold_pct: float
) -> tuple[Trigger | None, float | None]:
    """Trigger when the latest close is ``threshold_pct`` away from the close
    on the report date. Returns the trigger (or None) and the move itself."""
    series = closes.dropna()
    if series.empty:
        return None, None
    ref = close_on_or_before(series, since)
    if ref is None or ref <= 0:
        return None, None
    move = (float(series.iloc[-1]) / ref - 1.0) * 100.0
    if abs(move) < threshold_pct:
        return None, move
    return (
        Trigger("price", f"{move:+.1f}% since {since.isoformat()}", abs(move) * W_PRICE_PER_PCT),
        move,
    )


def evaluate(
    ticker: str,
    last: LastReport | None,
    closes: pd.Series | None,
    today: date,
    *,
    price_threshold_pct: float,
    max_age_days: int,
    material_filings: Callable[[str, datetime], list[str]] | None = None,
    new_shareholding: Callable[[str, datetime], str | None] | None = None,
) -> Assessment:
    """Assess one ticker. The two NSE lookups are optional and injected; a
    lookup that fails is recorded as a note and simply contributes nothing,
    so a blocked NSE can never stop the price and age checks working."""
    result = Assessment(ticker, last)
    if last is None:
        result.triggers.append(Trigger("new", "never analysed", W_NEVER))
        return result

    if closes is not None:
        trig, _ = price_move_trigger(closes, last.date, price_threshold_pct)
        if trig:
            result.triggers.append(trig)
    else:
        result.notes.append("no price data")

    if material_filings is not None:
        try:
            filings = material_filings(ticker, last.run_at)
        except VendorError as exc:
            result.notes.append(f"filings unavailable: {getattr(exc, 'reason', exc)}")
        else:
            if filings:
                n = min(len(filings), MAX_FILINGS_COUNTED)
                shown = "; ".join(filings[:2])
                more = f" (+{len(filings) - 2} more)" if len(filings) > 2 else ""
                result.triggers.append(
                    Trigger("filing", f"{len(filings)} material filing(s): {shown}{more}",
                            n * W_FILING)
                )

    if new_shareholding is not None:
        try:
            filed = new_shareholding(ticker, last.run_at)
        except VendorError as exc:
            result.notes.append(f"shareholding unavailable: {getattr(exc, 'reason', exc)}")
        else:
            if filed:
                result.triggers.append(Trigger("shareholding", filed, W_SHAREHOLDING))

    age = (today - last.date).days
    if age > max_age_days:
        result.triggers.append(
            Trigger("stale", f"last report {age} days old", W_STALE + (age - max_age_days))
        )
    return result


def select(
    assessments: Iterable[Assessment], max_tickers: int
) -> tuple[list[Assessment], list[Assessment]]:
    """Highest-scoring triggered tickers up to the cap; the rest are deferred."""
    ranked = sorted(
        (a for a in assessments if a.triggered), key=lambda a: (-a.score, a.ticker)
    )
    return ranked[:max_tickers], ranked[max_tickers:]
