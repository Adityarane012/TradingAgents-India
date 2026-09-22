"""Tests for local FinBERT headline scoring. The model is stubbed; what is
pinned is the rule layer, the outlet weighting, and that a missing model
leaves headlines unscored instead of failing."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tradingagents.dataflows import finbert_sentiment as fb


@dataclass
class Story:
    title: str
    sources: list[str] = field(default_factory=list)
    sentiment: str | None = None
    score: float | None = None


class TestFinbert:
    @pytest.fixture
    def stub_model(self, monkeypatch):
        seen = []

        def clf(titles, batch_size):
            seen.extend(titles)
            return [{"label": "negative" if "fall" in t else "neutral", "score": 0.9}
                    for t in titles]

        fb._pipeline.cache_clear()
        monkeypatch.setattr(fb, "_pipeline", lambda: clf)
        return seen

    def test_rating_actions_are_labelled_by_rule_not_the_model(self, stub_model):
        out = fb.classify(["ITC Ltd. is Rated Sell by MarketsMOJO",
                           "360 ONE maintains BUY on ITC, sees 68% upside",
                           "ITC shares fall 2%"])
        assert out == [("negative", 1.0), ("positive", 1.0), ("negative", 0.9)]
        assert stub_model == ["ITC shares fall 2%"]

    def test_tally_is_weighted_by_outlets(self, stub_model):
        stories = [Story("Shares fall on probe", ["a", "b", "c"]),
                   Story("Plant opens", ["d"])]
        tally = fb.score_stories(stories)
        assert "1 negative, 1 neutral, 0 positive of 2 stories" in tally
        assert "75% negative" in tally
        assert stories[0].sentiment == "negative"

    def test_missing_model_means_unscored_not_broken(self, monkeypatch):
        monkeypatch.setattr(fb, "_pipeline", lambda: None)
        assert fb.classify(["anything"]) is None
        assert fb.score_stories([Story("x", ["a"])]) == ""

