"""Local headline sentiment with FinBERT (ProsusAI/finbert, Apache-2.0).

Opt-in with ``local_sentiment="finbert"`` (on in the free-tier preset). Each
distinct news story gets a positive / negative / neutral label and a
confidence, and the block gets a tally weighted by how many outlets carried
each story. That gives the sentiment analyst a consistent, reproducible
baseline to reason from.

**Checked before enabling (2026-09-22).** On 16 real Nifty 50 headlines
labelled by hand, FinBERT agreed on 13. Its misses:

- broker rating actions ("ITC Ltd. is Rated Sell", "maintains BUY, sees 68%
  upside") read as neutral. Those follow fixed wording, so a rule labels them
  first (``_rating_action``);
- one genuinely mixed headline ("top picks as monsoon deficit puts rural
  demand at risk") read as negative.

On the RTX 3050 here it loads in ~1 s and scores 160 headlines in ~0.4 s.
The model is ~440 MB, downloaded once.

What it does not do: reduce the number of Gemini requests, the free tier's
real limit (future_plans.md §0). Its value is signal quality. The token
saving comes from google_news_india's de-duplication and cap.

Needs ``torch`` and ``transformers`` (``pip install "tradingagents[sentiment]"``).
Once the model is cached it is loaded offline: a newer transformers otherwise
tries to fetch a safetensors copy of the same weights first, and that fetch
hung a test run here. If anything is missing or fails, scoring is skipped and
the headlines go through unscored. A sentiment model must never break a run.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

MODEL = "ProsusAI/finbert"
_BATCH = 32

# Broker and rating-agency actions, which FinBERT reads as neutral. Checked
# before FinBERT; a headline matching both sides is left to the model.
_POSITIVE_ACTION = re.compile(
    r"\b(upgrades?d?|rated (buy|outperform|overweight|accumulate)|"
    r"(maintains?|reiterates?|initiates?|retains?|keeps?) (a )?(buy|outperform|overweight)|"
    r"target (price )?(raised|hiked|increased)|raises? target)\b",
    re.IGNORECASE,
)
_NEGATIVE_ACTION = re.compile(
    r"\b(downgrades?d?|rated (sell|underperform|underweight|reduce)|"
    r"(maintains?|reiterates?|initiates?|retains?|keeps?) (a )?(sell|underperform|underweight)|"
    r"target (price )?(cut|lowered|reduced|slashed)|cuts? target)\b",
    re.IGNORECASE,
)


def _rating_action(title: str) -> str | None:
    pos, neg = bool(_POSITIVE_ACTION.search(title)), bool(_NEGATIVE_ACTION.search(title))
    if pos != neg:
        return "positive" if pos else "negative"
    return None


@lru_cache(maxsize=1)
def _pipeline():
    """The classifier, loaded once per process, or None if unavailable."""
    try:
        import torch
        from huggingface_hub import try_to_load_from_cache
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
            pipeline,
        )

        cached = isinstance(try_to_load_from_cache(MODEL, "config.json"), str)
        tokenizer = AutoTokenizer.from_pretrained(MODEL, local_files_only=cached)
        model = AutoModelForSequenceClassification.from_pretrained(
            MODEL, local_files_only=cached, use_safetensors=False)
        return pipeline("text-classification", model=model, tokenizer=tokenizer,
                        device=0 if torch.cuda.is_available() else -1, truncation=True)
    except Exception as exc:  # noqa: BLE001 — optional dependency, never fatal
        logger.warning("FinBERT unavailable, headlines go unscored: %s", exc)
        return None


def classify(titles: list[str]) -> list[tuple[str, float]] | None:
    """(label, confidence) per title, or None if FinBERT is unavailable.
    Rating actions get confidence 1.0: they are labelled by rule."""
    if not titles:
        return []
    clf = _pipeline()
    if clf is None:
        return None
    ruled = [_rating_action(t) for t in titles]
    todo = [t for t, r in zip(titles, ruled, strict=True) if r is None]
    try:
        scored = iter(clf(todo, batch_size=_BATCH) if todo else [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("FinBERT scoring failed: %s", exc)
        return None
    out = []
    for rule in ruled:
        if rule:
            out.append((rule, 1.0))
        else:
            r = next(scored)
            out.append((r["label"].lower(), float(r["score"])))
    return out


def score_stories(stories) -> str:
    """Label every story in place and return a one-line weighted tally, or
    "" if scoring was not possible."""
    results = classify([s.title for s in stories])
    if not results:
        return ""
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    weighted = dict(counts)
    for story, (label, score) in zip(stories, results, strict=True):
        story.sentiment, story.score = label, score
        counts[label] = counts.get(label, 0) + 1
        weighted[label] = weighted.get(label, 0) + len(story.sources)
    total = sum(weighted.values()) or 1
    return (
        f"FinBERT headline tally (a local finance-tuned classifier, not the LLM): "
        f"{counts['negative']} negative, {counts['neutral']} neutral, {counts['positive']} "
        f"positive of {len(stories)} stories; weighted by outlet count "
        f"{weighted['negative'] / total:.0%} negative / {weighted['neutral'] / total:.0%} "
        f"neutral / {weighted['positive'] / total:.0%} positive. It reads headlines only "
        f"and misjudges sarcasm, questions and mixed news — treat it as a baseline to "
        f"check, not a verdict."
    )
