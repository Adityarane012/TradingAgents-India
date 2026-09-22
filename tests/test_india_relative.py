"""Tests for the relative-strength and crude/rupee prompt blocks.

The price download is stubbed throughout: these pin the arithmetic, the
no-look-ahead rule, the sector fallback, and that a failure degrades to a
sentinel rather than breaking the analysis.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.dataflows import config as config_module, india_relative as rel
from tradingagents.dataflows.india_context import india_relative_context
from tradingagents.dataflows.india_universe import NIFTY_50_APPROX

AS_OF = date(2026, 9, 21)


def _series(values, end="2026-09-21"):
    return pd.Series(values, index=pd.bdate_range(end=end, periods=len(values)), dtype=float)


def _frame(**cols):
    return pd.DataFrame({k: _series(v) for k, v in cols.items()})


@pytest.fixture
def stub_prices(monkeypatch):
    """Every symbol flat at 100 for 200 sessions, then the last close set per test."""
    frames = {}

    def download(symbols, as_of):
        data = {s: [100.0] * 199 + [frames.get(s, 100.0)] for s in symbols}
        return _frame(**data)

    monkeypatch.setattr(rel, "_download", download)
    return frames


class TestTrailingReturns:
    def test_each_window_ends_at_the_last_close(self):
        s = _series([100.0] * 130 + [110.0])
        day, rets = rel.trailing_returns(s, AS_OF)
        assert day == AS_OF
        assert rets == pytest.approx({"1M": 10.0, "3M": 10.0, "6M": 10.0})

    def test_bars_after_the_analysis_date_are_never_read(self):
        s = _series([100.0] * 130 + [100.0, 500.0], end="2026-09-22")  # 22nd is the future
        day, rets = rel.trailing_returns(s, AS_OF)
        assert day == AS_OF
        assert rets["1M"] == pytest.approx(0.0)

    def test_short_history_leaves_long_windows_out(self):
        _, rets = rel.trailing_returns(_series([100.0] * 30 + [120.0]), AS_OF)
        assert set(rets) == {"1M"}

    def test_nothing_by_the_date_is_none(self):
        assert rel.trailing_returns(_series([100.0], end="2026-10-01"), AS_OF) is None


class TestBasket:
    def test_equal_weighted_mean_of_member_returns(self):
        closes = _frame(A=[100.0] * 130 + [110.0], B=[100.0] * 130 + [90.0],
                        C=[100.0] * 130 + [130.0])
        day, rets, n = rel.basket_returns(closes, ["A", "B", "C", "MISSING"], AS_OF)
        assert n == 3
        assert rets["3M"] == pytest.approx(10.0)


class TestRelativeStrengthBlock:
    def test_sector_with_index_history_uses_the_index(self, stub_prices):
        block = rel.relative_strength_block("HDFCBANK.NS", AS_OF)
        assert "NSE index ^NSEBANK" in block and "basket" not in block

    def test_sector_without_index_history_uses_a_peer_basket(self, stub_prices):
        stub_prices.update({"MARUTI.NS": 90.0, "M&M.NS": 120.0})
        block = rel.relative_strength_block("MARUTI.NS", AS_OF)
        assert "equal-weighted basket of 5 Nifty 50 peer(s)" in block
        assert "MARUTI.NS: 1M -10.0%" in block
        assert "MARUTI.NS," not in block.split("peer(s):")[1]  # not its own peer

    def test_unmapped_ticker_gets_the_nifty_line_only(self, stub_prices):
        block = rel.relative_strength_block("RELIANCE.NS", AS_OF)
        assert block.count("\n- ") == 2 and "Nifty 50 (^NSEI)" in block

    def test_a_failed_download_is_a_sentinel(self, monkeypatch):
        def boom(*a):
            raise ConnectionError("offline")

        monkeypatch.setattr(rel, "_download", boom)
        assert "<relative strength unavailable" in rel.relative_strength_block("TCS.NS", AS_OF)

    def test_non_india_ticker_is_empty(self, stub_prices):
        assert rel.relative_strength_block("AAPL", AS_OF) == ""


class TestCommodityBlock:
    def test_success_is_cached_failure_is_not(self, monkeypatch):
        rel._COMMODITY_CACHE.clear()
        calls = []

        def flaky(symbols, as_of):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("blip")
            return _frame(**{"BZ=F": [80.0] * 30 + [88.0], "USDINR=X": [95.0] * 31})

        monkeypatch.setattr(rel, "_download", flaky)
        assert "unavailable" in rel.commodity_fx_block(AS_OF)
        first = rel.commodity_fx_block(AS_OF)
        assert "Brent crude (USD/bbl) 88.00" in first and "1M +10.0%" in first
        assert rel.commodity_fx_block(AS_OF) == first
        assert len(calls) == 2  # the blip was retried, the success was reused
        rel._COMMODITY_CACHE.clear()


class TestMaps:
    def test_every_mapped_ticker_is_in_the_universe(self):
        assert set(rel.SECTOR_OF) <= set(NIFTY_50_APPROX)

    def test_every_index_sector_has_members(self):
        assert set(rel.SECTOR_INDEX) <= set(rel.SECTOR_OF.values())


class TestWiring:
    def test_disabled_india_data_skips_the_download(self, monkeypatch):
        monkeypatch.setattr(rel, "_download", lambda *a: pytest.fail("network used"))
        assert india_relative_context("TCS.NS", AS_OF) == ""  # conftest disables it

    def test_market_analyst_prompt_carries_the_block(self, stub_prices, monkeypatch):
        from tests.test_india_analyst_prompts import _capturing_tool_llm, _prompt_text, _state

        monkeypatch.setitem(config_module._config, "india_data_enabled", True)
        captured = {}
        create_market_analyst(_capturing_tool_llm(captured))(_state("TCS.NS"))
        assert "Relative strength" in _prompt_text(captured)
        assert "NSE index ^CNXIT" in _prompt_text(captured)
