"""Tests for the profit-to-cash block.

Both sources are stubbed. What these pin: the arithmetic (so the model never
has to do it), the freshness labelling against SEBI's filing deadlines, and
that a missing source degrades to a sentinel instead of breaking an analysis.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tradingagents.dataflows import config as config_module, earnings_quality as eq
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.screener_in import CompanySnapshot, QuarterResult

CRORE = 1e7
AS_OF = date(2026, 9, 25)


def _cash_frame(years=("2026-03-31", "2025-03-31", "2024-03-31")):
    """A yfinance-shaped annual cash-flow frame, in absolute rupees."""
    data = {
        "Net Income From Continuing Operations": [100, 90, 80],
        "Operating Cash Flow": [50, 180, 160],
        "Capital Expenditure": [-30, -40, -20],
        "Free Cash Flow": [20, 140, 140],
        "Change In Receivables": [-25, 5, 1],
        "Change In Inventory": [-10, -2, 3],
        "Change In Payable": [15, 8, 2],
    }
    return pd.DataFrame({pd.Timestamp(y): [v[i] * CRORE for v in data.values()]
                         for i, y in enumerate(years)}, index=list(data))


def _income_frame(years=("2026-03-31", "2025-03-31", "2024-03-31")):
    return pd.DataFrame({pd.Timestamp(y): [v] for y, v in
                         zip(years, [120 * CRORE, 100 * CRORE, 90 * CRORE], strict=True)},
                        index=["Total Revenue"])


@pytest.fixture
def stub_yfinance(monkeypatch):
    class Ticker:
        def __init__(self, symbol):
            self.cashflow = _cash_frame()
            self.income_stmt = _income_frame()

    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    return Ticker


def _quarters(*specs):
    """(label, sales, opm, other_income, pbt, net_profit) tuples."""
    return [QuarterResult(quarter=q, sales=s, opm_pct=o, other_income=oi, pbt=p,
                          tax_pct=25.0, net_profit=n) for q, s, o, oi, p, n in specs]


@pytest.fixture
def stub_screener(monkeypatch):
    state = {"snapshot": CompanySnapshot(
        ratios={"ROCE": "15%"},
        quarters=_quarters(
            ("Jun 2025", 1000.0, 12.0, 20.0, 120.0, 90.0),
            ("Sep 2025", 1100.0, 11.0, 30.0, 120.0, 90.0),
            ("Dec 2025", 1200.0, 10.0, 40.0, 120.0, 99.0),
            ("Mar 2026", 1300.0, 9.0, 50.0, 125.0, 95.0),
            ("Jun 2026", 1250.0, 8.0, 60.0, 130.0, 80.0),
        ),
        annual_cash={"Mar 2025": {"cfo_over_op": 96.0}, "Mar 2026": {"cfo_over_op": 61.0}},
    )}
    monkeypatch.setattr(eq, "screener_enabled", lambda: True)
    monkeypatch.setattr(eq, "get_snapshot", lambda t, d: state["snapshot"])
    monkeypatch.setitem(config_module._config, "india_data_enabled", True)
    return state


class TestFilingDeadlines:
    @pytest.mark.parametrize("period_end, as_of, deadline, public", [
        # quarterly: 45 days
        (date(2026, 6, 30), date(2026, 8, 20), eq.QUARTER_DEADLINE_DAYS, True),
        (date(2026, 6, 30), date(2026, 8, 1), eq.QUARTER_DEADLINE_DAYS, False),
        (date(2026, 6, 30), date(2026, 8, 14), eq.QUARTER_DEADLINE_DAYS, True),
        # annual: 60 days
        (date(2026, 3, 31), date(2026, 5, 15), eq.ANNUAL_DEADLINE_DAYS, False),
        (date(2026, 3, 31), date(2026, 6, 1), eq.ANNUAL_DEADLINE_DAYS, True),
    ])
    def test_public_only_after_the_deadline_has_passed(self, period_end, as_of, deadline, public):
        assert eq.was_public(period_end, as_of, deadline) is public

    @pytest.mark.parametrize("label, expected", [
        ("Jun 2026", date(2026, 6, 30)),
        ("Mar 2026", date(2026, 3, 31)),
        ("Dec 2025", date(2025, 12, 31)),
        ("Feb 2024", date(2024, 2, 29)),  # leap year
        ("nonsense", None),
    ])
    def test_quarter_label_becomes_its_last_day(self, label, expected):
        assert eq.quarter_end(label) is expected or eq.quarter_end(label) == expected


class TestAnnualReconciliation:
    def test_ratios_are_computed_for_the_model(self, stub_yfinance):
        lines = "\n".join(eq.annual_cash_conversion("X.NS", AS_OF))
        # FY26: CFO 50 on net profit 100 -> 0.50, and FCF = 50 + (-30) = 20.
        # Values under 100 carry one decimal, which real crore figures rarely hit.
        assert "| Mar-2026 | 100 | 50.0 | 0.50 | 30.0 | 20.0 | yes |" in lines
        # FY25 converted better than FY26, which is the point of showing three
        assert "| Mar-2025 | 90.0 | 180 | 2.00 |" in lines

    def test_working_capital_is_compared_with_sales_growth(self, stub_yfinance):
        lines = "\n".join(eq.annual_cash_conversion("X.NS", AS_OF))
        assert "receivables -25" in lines and "inventory -10" in lines
        assert "sales +20% year on year" in lines

    def test_a_year_that_had_not_ended_is_left_out(self, stub_yfinance):
        lines = "\n".join(eq.annual_cash_conversion("X.NS", date(2025, 6, 1)))
        assert "Mar-2026" not in lines and "Mar-2025" in lines

    def test_a_year_inside_the_filing_window_is_flagged(self, stub_yfinance):
        # 10 April 2026: FY26 ended but its 60-day deadline has not passed.
        lines = "\n".join(eq.annual_cash_conversion("X.NS", date(2026, 4, 10)))
        assert "| Mar-2026 |" in lines and "filing date unknown" in lines

    def test_no_cash_data_yields_no_lines(self, monkeypatch):
        class Empty:
            def __init__(self, symbol):
                self.cashflow = pd.DataFrame()
                self.income_stmt = pd.DataFrame()

        import yfinance
        monkeypatch.setattr(yfinance, "Ticker", Empty)
        assert eq.annual_cash_conversion("X.NS", AS_OF) == []


class TestQuarterlyProfile:
    def test_quarter_on_quarter_and_other_income_share_are_computed(self, stub_screener):
        lines = "\n".join(eq.quarterly_profile("X.NS", AS_OF))
        # Jun 2026: sales 1250 from 1300 = -4%; other income 60 of PBT 130 = 46%
        assert "| Jun 2026 | 1,250 | -4% | 8.0% | 46.2% |" in lines
        assert "| Dec 2025 | 1,200 | +9% |" in lines

    def test_only_the_last_four_quarters_are_tabled(self, stub_screener):
        rows = [ln for ln in eq.quarterly_profile("X.NS", AS_OF) if ln.startswith("| ")]
        quarters = [r for r in rows if "2025" in r or "2026" in r]
        assert len(quarters) == 4
        assert not any("Jun 2025" in r for r in quarters)  # the fifth-oldest is dropped

    def test_the_trend_spans_every_quarter_available(self, stub_screener):
        lines = "\n".join(eq.quarterly_profile("X.NS", AS_OF))
        assert "Across 5 quarters (Jun 2025 to Jun 2026)" in lines
        assert "operating margin 12.0% -> 8.0%" in lines
        assert "other income as a share of pre-tax profit 17% -> 46%" in lines

    def test_screeners_own_ratio_is_offered_as_a_cross_check(self, stub_screener):
        lines = "\n".join(eq.quarterly_profile("X.NS", AS_OF))
        assert "Mar 2025 96%, Mar 2026 61%" in lines
        assert "say so rather than choosing one" in lines

    def test_a_fresh_quarter_is_flagged_not_hidden(self, stub_screener):
        # 20 July 2026: Jun-2026 results are not due for another 25 days.
        lines = "\n".join(eq.quarterly_profile("X.NS", date(2026, 7, 20)))
        assert "| Jun 2026 |" in lines
        assert lines.count("filing date unknown") == 1

    def test_screener_off_means_no_quarterly_half(self, monkeypatch):
        monkeypatch.setattr(eq, "screener_enabled", lambda: False)
        assert eq.quarterly_profile("X.NS", AS_OF) == []


class TestBlock:
    def test_non_india_ticker_gets_nothing(self, stub_yfinance, stub_screener):
        assert eq.earnings_quality_block("AAPL", AS_OF) == ""

    def test_both_halves_appear_with_the_reading_rules(self, stub_yfinance, stub_screener):
        block = eq.earnings_quality_block("X.NS", AS_OF)
        assert "Annual cash reconciliation" in block
        assert "Quarterly profit quality" in block
        assert "do not infer cash conversion from these" in block
        assert "state which capex definition you used" in block

    def test_a_refusing_screener_becomes_a_sentinel_not_a_failure(self, stub_yfinance,
                                                                 stub_screener, monkeypatch):
        def refuse(ticker, curr_date):
            raise VendorError("screener.in", "live runs only")

        monkeypatch.setattr(eq, "get_snapshot", refuse)
        block = eq.earnings_quality_block("X.NS", AS_OF)
        assert "unavailable" in block and "Annual cash reconciliation" in block

    def test_everything_missing_is_one_honest_sentinel(self, monkeypatch, stub_screener):
        monkeypatch.setattr(eq, "annual_cash_conversion", lambda t, d: [])
        monkeypatch.setattr(eq, "quarterly_profile", lambda t, d: [])
        block = eq.earnings_quality_block("X.NS", AS_OF)
        assert "no statements available from any source" in block

    def test_india_data_disabled_skips_it(self, stub_yfinance, monkeypatch):
        monkeypatch.setitem(config_module._config, "india_data_enabled", False)
        assert eq.earnings_quality_block("X.NS", AS_OF) == ""
