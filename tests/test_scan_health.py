"""Tests for telling a quiet scan apart from a broken one.

The failure these exist to prevent: NSE blocks every filing lookup, no trigger
can fire, and the briefing reports "nothing changed enough to re-analyse today"
— indistinguishable from a calm market. An unattended job that reports success
while broken is never questioned.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tradingagents.dataflows.refresh_triggers import evidence_class
from tradingagents.dataflows.safe_io import append_csv_row
from tradingagents.dataflows.scan_health import (
    DEGRADED_FRACTION,
    ScanHealth,
    assess,
    reason_kind,
    scan_history,
    scope_lines,
)

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
LOG_FIELDS = ["scan_date", "ticker", "decision", "score", "triggers", "notes"]


@dataclass
class FakeAssessment:
    """Stands in for refresh_triggers.Assessment: only notes matter here."""

    ticker: str = "X.NS"
    notes: list[str] = field(default_factory=list)


def _universe(size: int, failures: int, note: str = "filings unavailable: NSE blocked"):
    """A scan of ``size`` stocks where ``failures`` of them lost a lookup."""
    return [FakeAssessment(f"S{i}.NS", [note] if i < failures else [])
            for i in range(size)]


class TestReasonGrouping:
    @pytest.mark.parametrize("note, kind", [
        ("filings unavailable: NSE blocked the request", "filings unavailable"),
        ("shareholding unavailable: circuit breaker open", "shareholding unavailable"),
        ("no price data", "no price data"),
        ("", "unknown"),
    ])
    def test_the_symbol_specific_detail_is_stripped(self, note, kind):
        assert reason_kind(note) == kind

    def test_forty_similar_notes_become_one_count(self):
        health = assess(_universe(50, 40), triggered=0)
        assert health.reasons == {"filings unavailable": 40}
        assert "filings unavailable x40" in health.reason_text()

    def test_several_kinds_are_counted_separately(self):
        scan = _universe(10, 4) + [FakeAssessment("P.NS", ["no price data"])]
        health = assess(scan, triggered=1)
        assert health.reasons == {"filings unavailable": 4, "no price data": 1}
        assert health.failed_lookups == 5


class TestStates:
    def test_a_healthy_scan_with_triggers_says_so(self):
        health = assess(_universe(50, 0), triggered=8, history=[5, 14, 19])
        assert not health.degraded and not health.silent_failure
        assert not health.unusual_quiet
        assert "Scan healthy" in health.verdict
        assert "previous scans triggered 5, 14, 19" in health.verdict

    def test_a_few_failures_is_still_healthy(self):
        health = assess(_universe(50, 3), triggered=8)
        assert not health.degraded
        assert "3 with a partial failure" in health.verdict

    def test_a_fifth_failing_is_degraded(self):
        health = assess(_universe(50, 10), triggered=6)
        assert health.degraded and not health.silent_failure
        assert "Scan degraded" in health.verdict
        # the survivors are still real, and the verdict has to say that
        assert "flagged below are real" in health.verdict

    def test_degraded_with_nothing_triggered_is_the_dangerous_case(self):
        health = assess(_universe(50, 12), triggered=0)
        assert health.silent_failure
        assert "NOT evidence that nothing happened" in health.verdict
        assert "Treat today as unscanned" in health.verdict

    def test_a_quiet_day_with_healthy_sources_is_flagged_but_not_an_error(self):
        health = assess(_universe(50, 0), triggered=0, history=[5, 8, 14, 19])
        assert health.unusual_quiet
        assert not health.degraded and not health.silent_failure
        assert "That is unusual" in health.verdict
        assert "has not happened before" in health.verdict

    @pytest.mark.parametrize("failures, degraded", [
        (9, False),    # 18% — a handful of odd symbols
        (10, True),    # exactly 20% — the threshold counts as degraded
        (11, True),
    ])
    def test_the_threshold_boundary(self, failures, degraded):
        assert assess(_universe(50, failures), triggered=1).degraded is degraded

    def test_the_threshold_is_a_fraction_not_a_count(self):
        # A five-stock universe with one failure is 20%, so degraded.
        assert assess(_universe(5, 1), triggered=1).degraded is True
        assert DEGRADED_FRACTION == 0.2

    def test_an_empty_scan_is_not_called_degraded(self):
        health = assess([], triggered=0)
        assert not health.degraded and not health.silent_failure
        assert health.failure_rate == 0.0


class TestHistory:
    def _log(self, tmp_path, rows):
        path = tmp_path / "refresh_log.csv"
        for scan_date, ticker, decision in rows:
            append_csv_row(path, LOG_FIELDS, {
                "scan_date": scan_date, "ticker": ticker, "decision": decision,
                "score": "30", "triggers": "filing: x", "notes": "",
            })
        return path

    def test_counts_flagged_stocks_per_scan_oldest_first(self, tmp_path):
        path = self._log(tmp_path, [
            ("2026-09-21", "A.NS", "analyse"), ("2026-09-21", "B.NS", "skip"),
            ("2026-09-22", "A.NS", "analyse"), ("2026-09-22", "B.NS", "deferred"),
            ("2026-09-22", "C.NS", "skip"),
        ])
        assert scan_history(path) == [1, 2]

    def test_todays_scan_can_be_excluded(self, tmp_path):
        path = self._log(tmp_path, [
            ("2026-09-21", "A.NS", "analyse"), ("2026-09-22", "A.NS", "analyse"),
        ])
        assert scan_history(path, exclude="2026-09-22") == [1]

    def test_only_the_most_recent_scans_are_quoted(self, tmp_path):
        rows = [(f"2026-09-{day:02d}", "A.NS", "analyse") for day in range(1, 10)]
        assert len(scan_history(self._log(tmp_path, rows), limit=3)) == 3

    def test_a_scan_where_nothing_triggered_counts_as_zero(self, tmp_path):
        path = self._log(tmp_path, [("2026-09-21", "A.NS", "skip"),
                                    ("2026-09-22", "A.NS", "analyse")])
        assert scan_history(path) == [0, 1]

    def test_a_missing_log_is_no_history_not_a_crash(self, tmp_path):
        assert scan_history(tmp_path / "nope.csv") == []


class TestScopeSection:
    def test_it_states_universe_sources_and_thresholds(self):
        health = assess(_universe(50, 0), triggered=8, history=[5, 14])
        text = "\n".join(scope_lines(health, ["yfinance closes", "NSE announcements"],
                                     ["price move >= 5%"]))
        assert "Universe: 50 stocks" in text
        assert "NSE announcements" in text
        assert "price move >= 5%" in text
        assert "Lookups that failed: 0 of 50" in text
        assert "Previous scans triggered: 5, 14 · today: 8" in text

    def test_failure_reasons_appear_beside_the_count(self):
        health = assess(_universe(50, 12), triggered=0)
        text = "\n".join(scope_lines(health, ["yfinance closes"], ["x"]))
        assert "Lookups that failed: 12 of 50 (filings unavailable x12)" in text
        assert "NOT evidence that nothing happened" in text  # the verdict is included

    def test_no_history_is_simply_omitted(self):
        text = "\n".join(scope_lines(assess(_universe(5, 0), 1), ["s"], ["p"]))
        assert "Previous scans" not in text


class TestEvidenceClasses:
    @pytest.mark.parametrize("kind, label", [
        ("filing", "Filed"),
        ("shareholding", "Filed"),
        ("price", "Measured"),
        ("new", "Housekeeping"),
        ("stale", "Housekeeping"),
        ("something_new", "Other"),
    ])
    def test_each_trigger_kind_has_a_class(self, kind, label):
        assert evidence_class(kind) == label


class TestRefreshWiring:
    """The exit code and the summary, through daily_refresh itself."""

    def _refresh(self):
        spec = importlib.util.spec_from_file_location("_dr", SCRIPTS / "daily_refresh.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exit_code_4_is_reserved_for_a_silent_failure(self):
        refresh = self._refresh()
        # The contract the scheduled task relies on: 0 normal, 1 no price data,
        # 3 lock held, 4 a scan that reported nothing while failing.
        source = (SCRIPTS / "daily_refresh.py").read_text(encoding="utf-8")
        assert "return 4 if health.silent_failure else 0" in source
        assert refresh.__doc__ and "exits\n4" in refresh.__doc__.replace(" \n", "\n")

    def test_the_summary_always_carries_a_scope_section(self):
        source = (SCRIPTS / "daily_refresh.py").read_text(encoding="utf-8")
        # Built before the dry-run/no-selection early return, so it is present
        # even on the runs that report nothing.
        scope_at = source.index("scope_lines(")
        early_return = source.index("if args.dry_run or not selected:")
        assert scope_at < early_return

    def test_a_silent_failure_warns_at_the_top_of_the_briefing(self):
        health = ScanHealth(scanned=50, triggered=0, failed_lookups=12,
                            reasons={"filings unavailable": 12})
        assert health.silent_failure
        source = (SCRIPTS / "daily_refresh.py").read_text(encoding="utf-8")
        assert 'if health.silent_failure:' in source
        # the warning line is inserted before the "Scanned N" summary line
        warn_at = source.index('> **{health.verdict}**')
        counts_at = source.index('f"Scanned {len(tickers)}')
        assert warn_at < counts_at


class TestSilentFailureEndToEnd:
    """The whole refresh, with NSE broken and nothing triggering.

    No network: prices are stubbed and the NSE lookups raise the way a blocked
    host does. What this pins is the behaviour the scheduled task depends on —
    a non-zero exit and a warning in the file, rather than "nothing changed".
    """

    @pytest.fixture
    def refresh(self, tmp_path, monkeypatch):
        import pandas as pd

        spec = importlib.util.spec_from_file_location("_dr_e2e", SCRIPTS / "daily_refresh.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Redirect every output path into the temp directory.
        for name in ("RESULTS", "RUNS_CSV", "LOG_CSV", "LOCK"):
            target = tmp_path if name == "RESULTS" else tmp_path / {
                "RUNS_CSV": "india_universe_runs.csv",
                "LOG_CSV": "refresh_log.csv",
                "LOCK": "refresh.lock",
            }[name]
            monkeypatch.setattr(module, name, target)

        # Flat closes dated today for every ticker: no price move, session final.
        tickers = list(module.NIFTY_50_APPROX)
        index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=200)
        closes = pd.DataFrame({t: [100.0] * len(index) for t in tickers}, index=index)
        monkeypatch.setattr(module, "_download_closes", lambda *a, **k: closes)

        # Give every stock a recent report, so neither "new" nor "stale" fires.
        from tradingagents.dataflows.safe_io import append_csv_row
        fields = ["ticker", "company_name", "date", "status", "signal",
                  "decision_excerpt", "elapsed_seconds", "error", "run_at"]
        today = index[-1].date().isoformat()
        for t in tickers:
            append_csv_row(module.RUNS_CSV, fields, {
                "ticker": t, "company_name": t, "date": today, "status": "ok",
                "signal": "Hold", "decision_excerpt": "", "elapsed_seconds": "1",
                "error": "", "run_at": f"{today}T10:00:00",
            })
        return module

    def _break_nse(self, module, monkeypatch):
        from tradingagents.dataflows.errors import VendorError

        def blocked(*args, **kwargs):
            raise VendorError("NSE", "connection blocked by the host")

        monkeypatch.setattr(module.nse_india, "get_announcements", blocked)
        monkeypatch.setattr(module.nse_india, "get_shareholding", blocked)

    def test_a_blocked_source_and_no_triggers_exits_4_and_warns(self, refresh, monkeypatch):
        self._break_nse(refresh, monkeypatch)
        code = refresh.main(["--dry-run", "--force"])
        assert code == 4

        summary = next(iter(refresh.RESULTS.glob("refresh_*_dryrun.md"))).read_text(
            encoding="utf-8")
        assert "SCAN DEGRADED AND NOTHING TRIGGERED" in summary
        assert "NOT evidence that nothing happened" in summary
        # the warning comes before the counts, not buried at the end
        assert summary.index("SCAN DEGRADED") < summary.index("Scanned 50")
        # and the scope section states what was actually checked
        assert "Lookups that failed: 50 of 50" in summary
        assert "filings unavailable x50" in summary

    def test_the_same_scan_with_sources_working_exits_0(self, refresh, monkeypatch):
        monkeypatch.setattr(refresh.nse_india, "get_announcements", lambda *a, **k: [])
        monkeypatch.setattr(refresh.nse_india, "get_shareholding", lambda *a, **k: [])
        code = refresh.main(["--dry-run", "--force"])
        assert code == 0

        summary = next(iter(refresh.RESULTS.glob("refresh_*_dryrun.md"))).read_text(
            encoding="utf-8")
        assert "SCAN DEGRADED" not in summary
        assert "That is unusual" in summary  # nothing triggered, but sources answered
        assert "Lookups that failed: 0 of 50" in summary
