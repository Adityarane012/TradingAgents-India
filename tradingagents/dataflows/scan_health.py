"""Tell a quiet scan apart from a broken one.

The daily refresh used to end with "Nothing changed enough to re-analyse today"
in two completely different situations:

1. Every source answered and genuinely nothing crossed a threshold.
2. NSE blocked us, every filing lookup failed, and the triggers that depend on
   filings could never fire.

In the summary and in the log those looked identical, which means a broken scan
reported success — the worst failure mode an unattended job can have, because
nothing ever asks about it.

This module measures the scan itself rather than the market. Everything here is
computed from what the scan already collected (``Assessment.notes``), so it costs
no request and no token.

Three states worth distinguishing:

- **healthy** — sources answered; whatever the trigger count is, it means something.
- **degraded** — a fifth or more of the universe failed a lookup, so a source is
  broken rather than a handful of symbols being odd. Triggers that survived are
  still real, but absence of others proves nothing.
- **silent failure** — degraded *and* nothing triggered. This is the case that
  used to look like a calm day. It gets a warning at the top of the briefing and
  a distinct exit code so Task Scheduler shows something other than success.

A zero-trigger day is called out even when nothing failed: the observed history
is 5, 8, 14, 19 triggers, so zero has never happened and deserves a second look
rather than silence.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .safe_io import read_complete_rows

logger = logging.getLogger(__name__)

# A fifth of the universe failing the same way is a broken source, not bad luck
# with a few symbols. On the Nifty 50 that is 10 stocks.
DEGRADED_FRACTION = 0.2

# How many previous scans to quote as context for today's trigger count.
HISTORY_SCANS = 5

# Decisions in refresh_log.csv that mean the stock was flagged.
_TRIGGERED_DECISIONS = frozenset({"analyse", "deferred"})


def reason_kind(note: str) -> str:
    """The kind of failure a note describes, without the symbol-specific detail.

    ``evaluate`` writes notes like "filings unavailable: NSE blocked" — grouping
    on the text before the colon turns 40 near-identical notes into one count.
    """
    return note.split(":", 1)[0].strip().lower() or "unknown"


@dataclass(frozen=True)
class ScanHealth:
    """What the scan itself did, as opposed to what the market did."""

    scanned: int
    triggered: int
    failed_lookups: int          # stocks where at least one source errored
    reasons: dict[str, int] = field(default_factory=dict)
    history: list[int] = field(default_factory=list)  # previous scans, oldest first

    @property
    def failure_rate(self) -> float:
        return self.failed_lookups / self.scanned if self.scanned else 0.0

    @property
    def degraded(self) -> bool:
        """Enough lookups failed that a source, not a symbol, is the problem."""
        return self.scanned > 0 and self.failure_rate >= DEGRADED_FRACTION

    @property
    def silent_failure(self) -> bool:
        """Degraded *and* nothing triggered: the case that looks like calm."""
        return self.degraded and self.triggered == 0

    @property
    def unusual_quiet(self) -> bool:
        """Nothing triggered while every source answered. Not an error, but the
        trigger count has never been zero, so it is worth a second look."""
        return self.triggered == 0 and not self.degraded

    @property
    def verdict(self) -> str:
        """One line for the briefing and the log."""
        if self.silent_failure:
            return (f"SCAN DEGRADED AND NOTHING TRIGGERED: {self.failed_lookups} of "
                    f"{self.scanned} stocks had a failed lookup ({self.reason_text()}). "
                    f"Nothing triggering is NOT evidence that nothing happened — the "
                    f"triggers that depend on those sources could not fire. Treat today as "
                    f"unscanned and check the source before trusting tomorrow's comparison.")
        if self.degraded:
            return (f"Scan degraded: {self.failed_lookups} of {self.scanned} stocks had a "
                    f"failed lookup ({self.reason_text()}). The {self.triggered} stock(s) "
                    f"flagged below are real, but stocks that depend on the failed source "
                    f"may have been missed.")
        if self.unusual_quiet:
            return (f"Nothing triggered, and every source answered for all {self.scanned} "
                    f"stocks. That is unusual{self.history_text()} — the scan looks healthy, "
                    f"so this reads as a genuinely quiet day rather than a failure, but it "
                    f"has not happened before.")
        return (f"Scan healthy: all {self.scanned} stocks checked against every source"
                + (f", {self.failed_lookups} with a partial failure "
                   f"({self.reason_text()})" if self.failed_lookups else "")
                + f". {self.triggered} triggered{self.history_text()}.")

    def reason_text(self) -> str:
        if not self.reasons:
            return "no reason recorded"
        return ", ".join(f"{kind} x{count}"
                         for kind, count in sorted(self.reasons.items(), key=lambda kv: -kv[1]))

    def history_text(self) -> str:
        if not self.history:
            return ""
        return "; previous scans triggered " + ", ".join(str(n) for n in self.history)


def assess(assessments, triggered: int, history: list[int] | None = None) -> ScanHealth:
    """Measure the scan from the assessments it produced."""
    items = list(assessments)
    reasons: Counter[str] = Counter()
    failed = 0
    for assessment in items:
        notes = getattr(assessment, "notes", None) or []
        if notes:
            failed += 1
            for note in notes:
                reasons[reason_kind(note)] += 1
    return ScanHealth(
        scanned=len(items),
        triggered=triggered,
        failed_lookups=failed,
        reasons=dict(reasons),
        history=list(history or []),
    )


def scan_history(log_csv: Path, limit: int = HISTORY_SCANS, exclude: str | None = None) -> list[int]:
    """Trigger counts of previous scans from refresh_log.csv, oldest first.

    Context for judging today's number: "0 triggered" reads very differently
    beside 5, 8, 14, 19 than it would beside 0, 0, 1.

    The log carries one row per stock per scan and only a ``scan_date``, never a
    run time, so a day that was scanned twice — a missed evening picked up by
    ``--catch-up`` the next morning, or a manual re-run — holds two rows per
    stock. Summing them reported 24 September as a single scan of 27 triggers
    when it was really 19 in the morning and 8 in the evening, inflating the
    baseline that today's count is judged against.

    So each stock keeps only its last decision for the day, which counts the
    last scan of that date rather than every run added together. That is the
    scan whose triggers were acted on, and it needs no new column.
    """
    # scan_date -> ticker -> that ticker's most recent decision on the day.
    per_date: dict[str, dict[str, str]] = {}
    try:
        # scan_date is a date, not a timestamp, so a complete value is 10 chars.
        for row in read_complete_rows(Path(log_csv), last_field="scan_date",
                                      min_length=10):
            scan_date = row.get("scan_date")
            if not scan_date or scan_date == exclude:
                continue
            per_date.setdefault(scan_date, {})
            ticker = (row.get("ticker") or "").strip()
            if not ticker:  # can't tell a re-scan from a new stock without one
                continue
            per_date[scan_date][ticker] = (row.get("decision") or "").strip().lower()
    except OSError as exc:  # a missing or unreadable log is context, not an error
        logger.debug("no scan history available: %s", exc)
        return []
    return [sum(1 for decision in per_date[day].values()
                if decision in _TRIGGERED_DECISIONS)
            for day in sorted(per_date)][-limit:]


def scope_lines(health: ScanHealth, sources: list[str], parameters: list[str]) -> list[str]:
    """The "what was actually checked" section, written whatever the outcome.

    A briefing that says nothing happened has to say what it looked at, or the
    reader cannot tell coverage from silence.
    """
    lines = [
        "## Scan scope",
        "",
        f"- Universe: {health.scanned} stocks",
        f"- Sources: {', '.join(sources)}",
        f"- Thresholds: {', '.join(parameters)}",
        f"- Lookups that failed: {health.failed_lookups} of {health.scanned}"
        + (f" ({health.reason_text()})" if health.failed_lookups else ""),
    ]
    if health.history:
        lines.append(f"- Previous scans triggered: "
                     f"{', '.join(str(n) for n in health.history)} · today: {health.triggered}")
    lines += ["", health.verdict]
    return lines
