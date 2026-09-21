"""Daily change-triggered refresh of the Nifty 50.

Scans every ticker in the universe using free data only (prices, NSE filings,
shareholding), re-analyses just the ones where something changed, reports any
rating that flipped, and rebuilds the latest-per-stock dashboard. See
tradingagents/dataflows/refresh_triggers.py for the triggers and why.

    python scripts/daily_refresh.py --dry-run     # scan + plan only, no LLM calls
    python scripts/daily_refresh.py               # scan, then analyse the flagged
    python scripts/daily_refresh.py --max-tickers 10 --price-threshold 3

Designed to run unattended after the close (see register_daily_refresh.ps1,
which runs it with --until 20:00 --log ...). Safe to run twice in a day: a
ticker analysed today has no new price move, filing or age since that run, so
it is never re-analysed the same day and the quota is not spent twice. On a
day with no trading session (weekend, market holiday) it exits without doing
anything.

Every real scan is appended to <results_dir>/refresh_log.csv, and a readable
summary is written to <results_dir>/refresh_<date>.md (a second run that day is
added below the first; a --dry-run writes refresh_<date>_dryrun.md instead).

Crash safety — the machine may sleep, lose power or be killed at any point:
  - only one refresh or batch runs at a time (an OS lock, never left stale);
  - india_universe_runs.csv is backed up to <results_dir>/backups before any
    analysis, and each result is flushed to disk as soon as it exists;
  - the summary is written before the analyses start and again after, each
    time atomically, so there is always a readable one;
  - with --until, no ticker is started that would not finish in time, and
    the batch is killed outright if it is somehow still running at the end.
A ticker lost to a crash is simply not recorded; its triggers still hold, so
the next run picks it up.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from contextlib import suppress
from datetime import date, datetime, timedelta
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
from tradingagents.dataflows.safe_io import (
    LOCK_HELD_ENV,
    AlreadyRunning,
    append_csv_row,
    atomic_write_text,
    backup,
    read_complete_rows,
    rotate_log,
    single_instance,
)
from tradingagents.dataflows.utils import get_current_date
from tradingagents.default_config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(DEFAULT_CONFIG["results_dir"])
RUNS_CSV = RESULTS / "india_universe_runs.csv"
LOG_CSV = RESULTS / "refresh_log.csv"
LOCK = RESULTS / "refresh.lock"
LOG_FIELDS = ["scan_date", "ticker", "decision", "score", "triggers", "notes"]

# ~15 requests per ticker against a 500/day free tier; 25 leaves room for a
# retry or a manual one-off the same day. With --until the window usually
# binds first (a ticker takes 3-5 minutes), and the rest wait for tomorrow.
DEFAULT_MAX_TICKERS = 25

_LOG = None  # the --log file handle, once opened

# Left free at the end of the --until window for the summary and dashboard.
WRAP_UP = timedelta(minutes=3)
# The scan itself needs a few minutes; later than this, a run does nothing.
MIN_USEFUL = timedelta(minutes=10)


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


def _download_closes(tickers: list[str], attempts: int = 3, wait_s: float = 60.0):
    """Closes for every ticker, or None if the download keeps failing. Retried
    because a laptop woken for the run can take a minute to get online."""
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    for attempt in range(1, attempts + 1):
        try:
            data = yf.download(tickers, period="1y", progress=False, auto_adjust=False,
                               group_by="column", threads=True)
            closes = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
            closes = closes.dropna(how="all")
            if not closes.empty:
                closes.index = pd.to_datetime(closes.index).tz_localize(None)
                return closes
            print(f"  price download returned nothing (attempt {attempt}/{attempts})")
        except Exception as exc:  # noqa: BLE001 — network trouble must not crash the run
            print(f"  price download failed (attempt {attempt}/{attempts}): {exc}")
        if attempt < attempts:
            time.sleep(wait_s)
    return None


def window_end(until: str | None, now: datetime) -> datetime | None:
    """--until HH:MM as a datetime today."""
    if not until:
        return None
    hh, mm = (int(x) for x in until.split(":"))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _quota_exhausted(today_s: str, tickers: set[str]) -> bool:
    """Whether today's failures were the daily quota, which a retry cannot fix
    (a 503 "high demand" or a network blip can be)."""
    return any(
        row.get("date") == today_s and row.get("ticker") in tickers
        and row.get("status") == "error"
        and ("RESOURCE_EXHAUSTED" in row.get("error", "") or "PerDay" in row.get("error", ""))
        for row in read_complete_rows(RUNS_CSV)
    )


def _append_log(today: str, assessments, selected, deferred) -> None:
    chosen = {a.ticker for a in selected}
    later = {a.ticker for a in deferred}
    for a in assessments:
        decision = "analyse" if a.ticker in chosen else "deferred" if a.ticker in later else "skip"
        append_csv_row(LOG_CSV, LOG_FIELDS, {
            "scan_date": today, "ticker": a.ticker, "decision": decision,
            "score": f"{a.score:.0f}",
            "triggers": " | ".join(f"{t.kind}: {t.detail}" for t in a.triggers),
            "notes": " | ".join(a.notes),
        })


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
    ap.add_argument("--until", default=None, metavar="HH:MM",
                    help="Finish by this local time: no ticker is started that would "
                         "overrun, and a run started too late does nothing.")
    ap.add_argument("--log", default=None,
                    help="Append all output (this script and the batch) to this file, "
                         "rotating it past 5 MB. Used by the scheduled task.")
    args = ap.parse_args(argv)

    if args.log:
        _log_to(Path(args.log))
    print(f"\n=== daily refresh {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)
    try:
        with single_instance(LOCK):
            return _refresh(args)
    except AlreadyRunning as exc:
        print(f"Not starting: {exc}. A refresh or batch run is already in progress.")
        return 3
    finally:
        print(f"=== ended {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)


def _log_to(path: Path) -> None:
    global _LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):  # another process has it open; rotate next time
        rotate_log(path)
    # Line-buffered, so everything up to a crash is already on disk.
    _LOG = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _LOG


def _refresh(args) -> int:
    started = datetime.now()
    end = window_end(args.until, started)
    if end is not None and end - started < MIN_USEFUL:
        print(f"  too late for the run window (ends {args.until}); nothing done. "
              f"Triggers still hold, so the next run catches up.")
        return 0

    today_s = get_current_date()
    today = datetime.strptime(today_s, "%Y-%m-%d").date()
    tickers = list(NIFTY_50_APPROX)
    print(f"Daily refresh {today_s}: scanning {len(tickers)} tickers (no LLM calls)")

    closes = _download_closes(tickers)
    if closes is None:
        print("  no price data after retries (offline?). Nothing done; the next run catches up.")
        return 1
    latest_bar = closes.index.max().date()
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
    if not args.dry_run:  # the scan log records decisions that were acted on
        _append_log(today_s, assessments, selected, deferred)
    # A dry run gets its own file, and a second run on the same day goes below
    # the first, so neither can erase a real run's rating changes.
    summary = RESULTS / (f"refresh_{today_s}_dryrun.md" if args.dry_run
                         else f"refresh_{today_s}.md")
    earlier = "" if args.dry_run or not summary.exists() else (
        summary.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n")

    lines = [f"# Daily refresh — {today_s} {started:%H:%M}", "",
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
        _write_summary(summary, earlier, lines)
        return 0

    saved = backup(RUNS_CSV, RESULTS / "backups", f"{today_s}_{started:%H%M%S}")
    if saved:
        print(f"\nBacked up run history to {saved}")
    # Written now so a crash during the analyses still leaves today's plan.
    _write_summary(summary, earlier, lines + [
        "", f"**Status:** analyses started {started:%H:%M}. If this line is still here, "
        "the run was interrupted; every ticker that finished is saved."])

    tickers_file = RESULTS / f"refresh_{today_s}_tickers.txt"
    atomic_write_text(tickers_file, "\n".join(a.ticker for a in selected))
    deadline = end - WRAP_UP if end else None
    wanted = {a.ticker for a in selected}
    print(f"\nAnalysing {len(selected)} ticker(s)"
          + (f", starting none after it would overrun {deadline:%H:%M}" if deadline else "")
          + "...")
    killed = _run_batch(tickers_file, today_s, deadline)

    # One retry pass for tickers that errored (a 503 "high demand" usually
    # clears within minutes). --resume skips everything already done.
    errored = _errored_today(today_s, wanted - _ok_today(today_s))
    if errored and not killed and not _quota_exhausted(today_s, errored):
        print(f"\nRetrying {len(errored)} failed ticker(s): {', '.join(sorted(errored))}")
        time.sleep(60)
        killed = _run_batch(tickers_file, today_s, deadline)

    after = load_last_reports(RUNS_CSV)
    changes, unfinished = [], []
    for a in selected:
        new = after.get(a.ticker)
        if new is None or new.date != today:
            unfinished.append(a.ticker)
        elif a.last is None or new.signal != a.last.signal:
            before = a.last.signal if a.last else "(none)"
            changes.append(f"| **{a.ticker}** | {before} | **{new.signal}** |")
    lines += ["", "## Rating changes", ""]
    lines += (["| Ticker | Was | Now |", "|---|---|---|", *changes] if changes
              else ["No ratings changed."])
    if unfinished:
        lines += ["", "Not finished this run (failed, out of time or quota; their triggers "
                  f"still hold, so the next run picks them up): {', '.join(unfinished)}"]
    print("\n".join(lines[-(len(changes) + 6):]))
    _write_summary(summary, earlier, lines)

    sys.stdout.flush()
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_report_dashboard.py"),
                        "--latest"], cwd=ROOT, env=_child_env(), stdout=_child_out(),
                       stderr=_child_out(), check=False, timeout=300)
    except subprocess.TimeoutExpired:
        print("  dashboard rebuild timed out; the previous review_latest.html is intact.")
    return 0


def _child_env() -> dict[str, str]:
    # The lock is already held here; UTF-8 because the output may be a file,
    # where Windows would otherwise pick a code page that cannot print ₹.
    return {**os.environ, LOCK_HELD_ENV: "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1"}


def _child_out():
    """The --log file, so the batch's output lands in it too; else inherit."""
    return _LOG


def _run_batch(tickers_file: Path, today_s: str, deadline: datetime | None) -> bool:
    """Run the batch runner; True if it had to be killed at the window's end."""
    cmd = [sys.executable, str(ROOT / "scripts" / "analyze_india_universe.py"),
           "--free", "--no-reddit", "--resume", "--delay", "5",
           "--date", today_s, "--tickers-file", str(tickers_file)]
    timeout = None
    if deadline is not None:
        cmd += ["--deadline", deadline.isoformat(timespec="minutes")]
        # Backstop only: the batch stops itself at the deadline. This fires if
        # a ticker hangs, leaving a minute for the summary and dashboard.
        hard_stop = deadline + WRAP_UP - timedelta(minutes=1)
        timeout = max((hard_stop - datetime.now()).total_seconds(), 60)
    sys.stdout.flush()
    try:
        subprocess.run(cmd, cwd=ROOT, env=_child_env(), stdout=_child_out(),
                       stderr=_child_out(), check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("\n  batch still running at the end of the window — stopped. The ticker in "
              "progress is not recorded and will be picked up next run.")
        return True
    return False


def _ok_today(today_s: str) -> set[str]:
    return {row["ticker"] for row in read_complete_rows(RUNS_CSV)
            if row.get("date") == today_s and row.get("status") == "ok"}


def _errored_today(today_s: str, tickers: set[str]) -> set[str]:
    return {row["ticker"] for row in read_complete_rows(RUNS_CSV)
            if row.get("date") == today_s and row.get("status") == "error"
            and row.get("ticker") in tickers}


def _write_summary(out: Path, earlier: str, lines: list[str]) -> None:
    atomic_write_text(out, earlier + "\n".join(lines) + "\n")
    print(f"\nSummary: {out}")


if __name__ == "__main__":
    sys.exit(main())
