"""Tests for the change-triggered refresh decision logic.

The whole point of the refresh is to spend LLM quota only where something
changed, so these pin both halves: every trigger fires when it should, and an
unchanged ticker costs nothing.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.refresh_triggers import (
    W_NEVER,
    LastReport,
    evaluate,
    load_last_reports,
    price_move_trigger,
    select,
)

TODAY = date(2026, 9, 21)
LAST = LastReport("X.NS", date(2026, 9, 20), "Hold", datetime(2026, 9, 20, 15, 0))


def closes(*values, end="2026-09-21"):
    idx = pd.bdate_range(end=end, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def run(last=LAST, series=None, **kw):
    kw.setdefault("price_threshold_pct", 5.0)
    kw.setdefault("max_age_days", 14)
    return evaluate("X.NS", last, series if series is not None else closes(100, 100, 100),
                    TODAY, **kw)


class TestUnchangedCostsNothing:
    def test_no_move_no_filing_recent_report_is_not_triggered(self):
        a = run(material_filings=lambda t, s: [], new_shareholding=lambda t, s: None)
        assert not a.triggered
        assert a.score == 0

    def test_a_ticker_analysed_today_never_retriggers(self):
        today_run = LastReport("X.NS", TODAY, "Hold", datetime(2026, 9, 21, 20, 0))
        a = run(last=today_run, series=closes(100, 120, 130))
        assert not any(t.kind in ("price", "stale") for t in a.triggers)


class TestTriggers:
    def test_never_analysed_outranks_everything(self):
        a = run(last=None)
        assert [t.kind for t in a.triggers] == ["new"]
        assert a.score == W_NEVER

    @pytest.mark.parametrize("new_close, fires", [(105.0, True), (95.0, True), (104.0, False)])
    def test_price_threshold_both_directions(self, new_close, fires):
        a = run(series=closes(100, 100, new_close))
        assert any(t.kind == "price" for t in a.triggers) is fires

    def test_price_move_is_measured_from_the_report_date(self):
        # Report dated a Sunday: the reference is Friday's close, not today's.
        s = closes(100, 110, end="2026-09-21")  # Fri 18th=100, Mon 21st=110
        trig, move = price_move_trigger(s, date(2026, 9, 20), 5.0)
        assert move == pytest.approx(10.0)
        assert trig is not None and "+10.0%" in trig.detail

    def test_material_filings_trigger_and_are_capped(self):
        a = run(material_filings=lambda t, s: ["Acquisition"] * 7)
        f = next(t for t in a.triggers if t.kind == "filing")
        assert "7 material filing" in f.detail
        assert f.weight == 90  # capped at 3 x 30

    def test_new_shareholding_filing_triggers(self):
        a = run(new_shareholding=lambda t, s: "quarter to 30-Sep-2026 filed 15-Oct-2026")
        assert any(t.kind == "shareholding" for t in a.triggers)

    def test_stale_report_triggers(self):
        old = LastReport("X.NS", date(2026, 9, 1), "Hold", datetime(2026, 9, 1, 15, 0))
        a = run(last=old, series=closes(*[100] * 20))
        stale = next(t for t in a.triggers if t.kind == "stale")
        assert "20 days old" in stale.detail

    def test_the_lookup_receives_the_run_time_not_the_date(self):
        seen = []
        run(material_filings=lambda t, since: seen.append(since) or [])
        assert seen == [LAST.run_at]


class TestResilience:
    def test_a_failing_nse_lookup_is_a_note_not_a_crash(self):
        def boom(t, s):
            raise VendorError("NSE down")

        a = run(series=closes(100, 100, 110), material_filings=boom, new_shareholding=boom)
        assert any(t.kind == "price" for t in a.triggers)  # price check still works
        assert len(a.notes) == 2

    def test_missing_price_data_is_noted(self):
        a = evaluate("X.NS", LAST, None, TODAY, price_threshold_pct=5, max_age_days=14)
        assert "no price data" in a.notes

    def test_a_programming_error_in_a_lookup_is_not_swallowed(self):
        def bug(t, s):
            raise KeyError("bug")

        with pytest.raises(KeyError):
            run(material_filings=bug)


class TestSelection:
    def _a(self, ticker, score_close):
        return evaluate(ticker, LAST, closes(100, 100, score_close), TODAY,
                        price_threshold_pct=5, max_age_days=14)

    def test_highest_scores_first_and_capped(self):
        items = [self._a("A.NS", 106), self._a("B.NS", 130), self._a("C.NS", 110),
                 self._a("D.NS", 100)]
        chosen, deferred = select(items, max_tickers=2)
        assert [a.ticker for a in chosen] == ["B.NS", "C.NS"]
        assert [a.ticker for a in deferred] == ["A.NS"]  # D never triggered

    def test_untriggered_tickers_are_neither_chosen_nor_deferred(self):
        chosen, deferred = select([self._a("D.NS", 100)], max_tickers=5)
        assert chosen == [] and deferred == []


class TestLoadLastReports:
    def test_latest_successful_run_wins(self, tmp_path):
        p = tmp_path / "runs.csv"
        p.write_text(
            "ticker,company_name,date,status,signal,decision_excerpt,elapsed_seconds,error,run_at\n"
            "X.NS,X,2026-09-20,ok,Hold,,,,2026-09-20T15:00:00\n"
            "X.NS,X,2026-09-21,error,,,,boom,2026-09-21T20:00:00\n"
            "Y.NS,Y,2026-09-19,ok,Buy,,,,2026-09-19T15:00:00\n"
            "Y.NS,Y,2026-09-21,ok,Overweight,,,,2026-09-21T20:00:00\n",
            encoding="utf-8",
        )
        last = load_last_reports(p)
        assert last["X.NS"].date == date(2026, 9, 20)  # the failure does not count
        assert last["Y.NS"].signal == "Overweight"
        assert last["Y.NS"].run_at == datetime(2026, 9, 21, 20, 0)

    def test_missing_file_is_empty(self, tmp_path):
        assert load_last_reports(tmp_path / "nope.csv") == {}
