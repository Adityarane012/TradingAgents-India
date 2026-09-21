"""Daily change-triggered refresh of the Nifty 50.

Scans every ticker in the universe using free data only (prices, NSE filings,
shareholding), re-analyses just the ones where something changed, reports any
rating that flipped, and rebuilds the latest-per-stock dashboard. See
tradingagents/dataflows/refresh_triggers.py for the triggers and why.

    python scripts/daily_refresh.py --dry-run     # scan + plan only, no LLM calls
    python scripts/daily_refresh.py               # scan, then analyse the flagged
    python scripts/daily_refresh.py --max-tickers 10 --price-threshold 3

Designed to run unattended after the close (see scripts/daily_refresh.cmd and
register_daily_refresh.ps1). Safe to run twice in a day: a ticker analysed
today has no new price move, filing or age since that run, so it is never
re-analysed the same day and the quota is not spent twice. On a day with no
trading session (weekend, market holiday) it exits without doing anything.

Every scan is appended to <results_dir>/refresh_log.csv, and a readable summary
is written to <results_dir>/refresh_<date>.md.
"""

from __future__ import annotations

import argparse
import csv
import logging
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from tradingagents.dataflows import nse_india
from tradingagents.dataflows.india_data_common import IST
from tradingagents.dataflows.india_universe import NIFTY_50_APPROX
from tradingagents.dataflows.refresh_triggers import (
    evaluate,
    load_last_reports,
    select,
)
from tradingagents.dataflows.utils import get_current_date
from tradingagents.default_config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(DEFAULT_CONFIG["results_dir"])
RUNS_CSV = RESULTS / "india_universe_runs.csv"
LOG_CSV = RESULTS / "refresh_log.csv"

# ~15 requests per ticker against a 500/day free tier; 25 leaves room for a
# retry or a manual one-off the same day.
DEFAULT_MAX_TICKERS = 25


def _as_ist(moment: datetime) -> datetime:
    """run_at in the CSV is naive local time; this machine runs in IST."""
    return moment if moment.tzinfo else moment.replace(tzinfo=IST)


def _material_filings(today: date):
    def lookup(ticker: str, since: datetime) -> list[str]:
        since = _as_ist(since)
        lookback = min(max((today - since.date()).days, 1) + 1, 60)
        items = nse_india.get_announcements(ticker, today, lookback_days=lookback, limit=50)
        return [
            a.category or "announcement"
            for a in items
            if not a.routine and a.at > since
        ]

    return lookup


def _new_shareholding(today: date):
    def lookup(ticker: str, since: datetime) -> str | None:
        rows = nse_india.get_shareholding(ticker, today, quarters=1)
        if rows and rows[0].filed > _as_ist(since):
            return (f"quarter to {rows[0].quarter_end:%d-%b-%Y} filed "
                    f"{rows[0].filed:%d-%b-%Y}, promoter {rows[0].promoter_pct:.2f}%")
        return None

    return lookup


def _download_closes(tickers: list[str]) -> pd.DataFrame:
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    data = yf.download(tickers, period="1y", progress=False, auto_adjust=False,
                       group_by="column", threads=True)
    closes = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes


def _append_log(today: str, assessments, selected, deferred) -> None:
    chosen = {a.ticker for a in selected}
    later = {a.ticker for a in deferred}
    new = not LOG_CSV.exists()
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    with LOG_CSV.open("a", encoding="utf-8", newline="") as handle:
        w = csv.writer(handle)
        if new:
            w.writerow(["scan_date", "ticker", "decision", "score", "triggers", "notes"])
        for a in assessments:
            decision = "analyse" if a.ticker in chosen else "deferred" if a.ticker in later else "skip"
            w.writerow([today, a.ticker, decision, f"{a.score:.0f}",
                        " | ".join(f"{t.kind}: {t.detail}" for t in a.triggers),
                        " | ".join(a.notes)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Scan and plan; no LLM calls.")
    ap.add_argument("--max-tickers", type=int, default=DEFAULT_MAX_TICKERS,
                    help=f"Cap on analyses per run (default {DEFAULT_MAX_TICKERS}).")
    ap.add_argument("--price-threshold", type=float, default=5.0,
                    help="Percent move since the last report that triggers (default 5).")
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="Re-analyse anything whose report is older than this (default 14).")
    ap.add_argument("--force", action="store_true",
                    help="Run even if no trading session is found for today.")
    ap.add_argument("--no-nse", action="store_true",
                    help="Skip the NSE filing checks (price and age triggers only).")
    args = ap.parse_args(argv)

    today_s = get_current_date()
    today = datetime.strptime(today_s, "%Y-%m-%d").date()
    tickers = list(NIFTY_50_APPROX)
    print(f"Daily refresh {today_s}: scanning {len(tickers)} tickers (no LLM calls)")

    closes = _download_closes(tickers)
    latest_bar = closes.dropna(how="all").index.max().date()
    if latest_bar != today and not args.force:
        print(f"  latest trading session is {latest_bar}, not today — market closed or "
              f"data not published yet. Nothing to do (use --force to override).")
        return 0

    last = load_last_reports(RUNS_CSV)
    assessments = [
        evaluate(
            t, last.get(t),
            closes[t] if t in closes.columns else None,
            today,
            price_threshold_pct=args.price_threshold,
            max_age_days=args.max_age_days,
            material_filings=None if args.no_nse else _material_filings(today),
            new_shareholding=None if args.no_nse else _new_shareholding(today),
        )
        for t in tickers
    ]
    selected, deferred = select(assessments, args.max_tickers)
    _append_log(today_s, assessments, selected, deferred)

    lines = [f"# Daily refresh — {today_s}", "",
             f"Scanned {len(tickers)} · triggered {len(selected) + len(deferred)} · "
             f"analysing {len(selected)} · deferred {len(deferred)}", ""]
    if selected:
        lines += ["## Re-analysing", "", "| Ticker | Score | Why |", "|---|---|---|"]
        lines += [f"| {a.ticker} | {a.score:.0f} | "
                  + "; ".join(f"{t.kind}: {t.detail}" for t in a.triggers) + " |"
                  for a in selected]
    if deferred:
        lines += ["", "## Deferred to a later run (over today's cap)", ""]
        lines += [f"- {a.ticker} ({a.score:.0f}): "
                  + "; ".join(t.kind for t in a.triggers) for a in deferred]
    notes = [a for a in assessments if a.notes]
    if notes:
        lines += ["", "## Lookups that failed (did not block the scan)", ""]
        lines += [f"- {a.ticker}: {'; '.join(a.notes)}" for a in notes]
    print("\n".join(lines))

    if args.dry_run or not selected:
        if not selected:
            print("\nNothing changed enough to re-analyse today.")
        _write_summary(today_s, lines)
        return 0

    tickers_file = RESULTS / f"refresh_{today_s}_tickers.txt"
    tickers_file.write_text("\n".join(a.ticker for a in selected), encoding="utf-8")
    print(f"\nAnalysing {len(selected)} ticker(s)...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "analyze_india_universe.py"),
         "--free", "--no-reddit", "--resume", "--delay", "5",
         "--date", today_s, "--tickers-file", str(tickers_file)],
        cwd=ROOT, check=False,
    )

    after = load_last_reports(RUNS_CSV)
    changes, failed = [], []
    for a in selected:
        new = after.get(a.ticker)
        if new is None or new.date != today:
            failed.append(a.ticker)
        elif a.last is None or new.signal != a.last.signal:
            before = a.last.signal if a.last else "(none)"
            changes.append(f"| **{a.ticker}** | {before} | **{new.signal}** |")
    lines += ["", "## Rating changes", ""]
    lines += (["| Ticker | Was | Now |", "|---|---|---|", *changes] if changes
              else ["No ratings changed."])
    if failed:
        lines += ["", f"Failed this run (will be picked up again tomorrow): {', '.join(failed)}"]
    print("\n".join(lines[-(len(changes) + 6):]))
    _write_summary(today_s, lines)

    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_report_dashboard.py"),
                    "--latest"], cwd=ROOT, check=False)
    return 0


def _write_summary(today_s: str, lines: list[str]) -> None:
    out = RESULTS / f"refresh_{today_s}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSummary: {out}")


if __name__ == "__main__":
    sys.exit(main())
