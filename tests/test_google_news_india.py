"""Tests for the Google News India company-news vendor and FinBERT scoring.

No network: the RSS response and the classifier are stubbed. The headlines
are real ones from the 2026-09-22 live check, so the filters are tested
against the noise they were written for.
"""

from __future__ import annotations

from datetime import date
from email.utils import format_datetime

import pytest

from tradingagents.dataflows import (
    config as config_module,
    finbert_sentiment as fb,
    google_news_india as gn,
)
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.dataflows.india_data_common import IST
from tradingagents.dataflows.interface import VENDOR_METHODS


def _item(title, source, day, hour=12):
    from datetime import datetime

    when = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
    return (f"<item><title>{title} - {source}</title><pubDate>{format_datetime(when)}"
            f"</pubDate><source url='https://x'>{source}</source></item>")


def _rss(*items):
    return ("<?xml version='1.0'?><rss><channel>" + "".join(items)
            + "</channel></rss>").encode()


@pytest.fixture
def feed(monkeypatch):
    """Serve whatever items the test puts in the list; record the query."""
    state = {"items": [], "queries": []}

    class Resp:
        status_code = 200

        @property
        def content(self):
            return _rss(*state["items"])

    def get(url, params=None, **kw):
        state["queries"].append(params["q"])
        return Resp()

    monkeypatch.setattr(gn.requests, "get", get)
    return state


D = date(2026, 9, 18)


class TestFiltering:
    def test_near_duplicate_headlines_become_one_story_with_an_outlet_count(self, feed):
        feed["items"] = [
            _item("FSSAI initiates legal action against Nestle India on baby formula", "Reuters", D),
            _item("FSSAI initiates legal action against Nestle India over baby formula products",
                  "Mint", D, 13),
            _item("Nestle India legal action: FSSAI initiates action on baby formula", "ET", D, 14),
            _item("Nestle India advances Monday, outperforms competitors", "MarketWatch", D),
        ]
        stories, raw = gn.fetch_stories("NESTLEIND.NS", date(2026, 9, 15), date(2026, 9, 22))
        assert raw == 4
        assert [len(s.sources) for s in stories] == [3, 1]
        assert stories[0].title.startswith("FSSAI initiates")  # earliest version kept

    def test_quote_pages_social_posts_and_unrelated_names_are_dropped(self, feed):
        feed["items"] = [
            _item("TRENT LTD Option Chain - Live TRENT LTD Option Chain Data", "Upstox", D),
            _item("Trent Williams declares Brock Purdy is in his prime", "NBC Sports", D),
            _item("Case not proven: Trent Alexander-Arnold's England recall", "Guardian", D),
            _item("Trent stock rises as Indian markets open firmer", "Facebook", D),
            _item("Trent stock rises as Indian markets open firmer", "AD HOC NEWS", D),
        ]
        stories, _ = gn.fetch_stories("TRENT.NS", date(2026, 9, 15), date(2026, 9, 22))
        assert [s.title for s in stories] == ["Trent stock rises as Indian markets open firmer"]
        assert stories[0].sources == ["AD HOC NEWS"]

    def test_short_forms_count_as_mentions(self, feed):
        feed["items"] = [_item("Airtel shares snap 3-day gains", "Mint", D)]
        stories, _ = gn.fetch_stories("BHARTIARTL.NS", date(2026, 9, 15), date(2026, 9, 22))
        assert len(stories) == 1

    def test_whole_word_match_only(self):
        assert gn._mentions("BEL wins defence order", ["BEL"])
        assert not gn._mentions("Belgium trade talks", ["BEL"])

    @pytest.mark.parametrize("title, published, stale", [
        ("Stocks to Watch Today, Aug 3: ZEEL, ITC", date(2026, 9, 19), True),
        ("UltraTech board to meet on July 20 for results", date(2026, 9, 17), True),
        ("Eternal board meeting in January 2026", date(2026, 9, 22), True),
        ("ITC record date Oct 5 for dividend", date(2026, 9, 19), False),
        ("Top gainers September 21: Eternal, ITC", date(2026, 9, 21), False),
        ("Stocks to watch Dec 29", date(2027, 1, 2), False),
    ])
    def test_stale_headlines(self, title, published, stale):
        assert gn.is_stale_headline(title, published) is stale


class TestNoLookAhead:
    def test_items_after_the_end_date_are_dropped_even_if_google_returns_them(self, feed):
        feed["items"] = [
            _item("Infosys wins large deal", "ET", date(2025, 3, 5)),
            _item("Infosys shares crash on guidance cut", "ET", date(2025, 3, 9)),  # future
        ]
        stories, _ = gn.fetch_stories("INFY.NS", date(2025, 3, 1), date(2025, 3, 7))
        assert [s.title for s in stories] == ["Infosys wins large deal"]

    def test_the_query_is_bounded_to_the_window(self, feed):
        feed["items"] = [_item("Infosys wins large deal", "ET", date(2025, 3, 5))]
        gn.get_news_google_india("INFY.NS", "2025-03-01", "2025-03-07")
        assert "after:2025-03-01" in feed["queries"][0]
        assert "before:2025-03-08" in feed["queries"][0]  # before: is exclusive


class TestVendorContract:
    def test_non_india_ticker_falls_through_to_the_next_vendor(self, feed):
        with pytest.raises(NoMarketDataError):
            gn.get_news_google_india("AAPL", "2026-09-15", "2026-09-22")
        assert feed["queries"] == []  # no request made

    def test_nothing_about_the_company_is_no_data_not_an_empty_block(self, feed):
        feed["items"] = [_item("Trent Williams declares Brock Purdy is in his prime", "NBC", D)]
        with pytest.raises(NoMarketDataError):
            gn.get_news_google_india("TRENT.NS", "2026-09-15", "2026-09-22")

    def test_throttling_is_a_rate_limit_so_the_chain_moves_on(self, monkeypatch):
        class Resp:
            status_code = 429
            content = b""

        monkeypatch.setattr(gn.requests, "get", lambda *a, **k: Resp())
        with pytest.raises(VendorRateLimitError):
            gn.get_news_google_india("TCS.NS", "2026-09-15", "2026-09-22")

    def test_registered_for_company_news(self):
        assert VENDOR_METHODS["get_news"]["google_news"] is gn.get_news_google_india

    def test_block_is_capped_and_ranked(self, feed, monkeypatch):
        monkeypatch.setitem(config_module._config, "news_article_limit", 2)
        feed["items"] = [
            _item("TCS signs AI deal with European bank", "ET", D),
            _item("TCS signs AI deal with a European bank", "Mint", D, 13),
            _item("TCS shares slip after brokerage note", "BS", D),
            _item("TCS to hire 5000 freshers", "ToI", D),
        ]
        block = gn.get_news_google_india("TCS.NS", "2026-09-15", "2026-09-22")
        lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
        assert len(lines) == 2
        assert "(2 outlets)" in lines[0]


class TestFinbertInTheBlock:
    @pytest.fixture
    def stub_model(self, monkeypatch):
        fb._pipeline.cache_clear()
        monkeypatch.setattr(fb, "_pipeline", lambda: (
            lambda titles, batch_size: [{"label": "negative", "score": 0.9} for _ in titles]))

    def test_enabled_scoring_appears_in_the_block(self, feed, stub_model, monkeypatch):
        monkeypatch.setitem(config_module._config, "local_sentiment", "finbert")
        feed["items"] = [_item("Nestle India shares fall on FSSAI action", "ET", D)]
        block = gn.get_news_google_india("NESTLEIND.NS", "2026-09-15", "2026-09-22")
        assert "FinBERT headline tally" in block and "[FinBERT: negative 0.90]" in block

    def test_disabled_by_default(self, feed, monkeypatch):
        monkeypatch.setattr(fb, "_pipeline", lambda: pytest.fail("model loaded"))
        feed["items"] = [_item("Nestle India shares fall on FSSAI action", "ET", D)]
        assert "FinBERT" not in gn.get_news_google_india("NESTLEIND.NS", "2026-09-15",
                                                        "2026-09-22")
