"""Company news for Indian tickers from Google News' India edition.

Yahoo's news feed is thin for NSE names: a handful of items a week, often
none. Google News' RSS search (no key, no account) returned 46-100 items per
Nifty 50 company per week from 30-70 outlets when checked on 2026-09-22,
including the Reuters/Bloomberg/The Hindu coverage of FSSAI's action against
Nestle India that Yahoo did not carry. It serves the ``get_news`` vendor
chain, so the news and sentiment analysts both get it.

Raw search results are not fit to paste into a prompt, so this module:

- **disambiguates** — a bare name search for "Trent" returns a footballer,
  "ITC" GST input tax credit, "Eternal" a film. The query adds market
  context words, and a headline is kept only if it names the company or a
  known short form (Airtel, L&T, SBI, Zomato ...);
- **drops quote pages** ("Option Chain - Live", "Stock Price & Chart"), which
  are price widgets, not news;
- **merges duplicates** — one story is typically carried by 5-10 outlets.
  Near-identical headlines collapse into one line that keeps the outlet
  count, which is itself a signal of how big the story is;
- **never looks ahead** — the query is bounded with ``after:``/``before:``
  (Google honours these; verified on a March 2025 window) and every item is
  re-checked against the requested dates in IST;
- ranks by outlet count, then recency, and caps the list, which is also what
  keeps the token cost down.

Optionally each headline is scored by FinBERT (``local_sentiment="finbert"``;
see finbert_sentiment.py), giving the analysts a consistent tally to reason
from instead of 30 raw headlines.

Non-Indian tickers raise ``NoMarketDataError`` so the chain falls through to
the next vendor, the same way sec_edgar declines non-US filers.
"""

from __future__ import annotations

import email.utils
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests

from .config import get_config
from .errors import NoMarketDataError, VendorRateLimitError
from .india_data_common import IST
from .india_universe import NIFTY_50_APPROX
from .symbol_utils import is_india_ticker, strip_india_suffix

logger = logging.getLogger(__name__)

_URL = "https://news.google.com/rss/search"
_PARAMS = {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
_UA = "Mozilla/5.0 (compatible; tradingagents-india)"
_TIMEOUT = 15.0
_MAX_BYTES = 3 * 1024 * 1024

# Makes a name search about the listed company rather than anything sharing
# its name. Checked live: cut "Trent" from mostly football to mostly stock
# coverage without losing the company's real news.
_MARKET_CONTEXT = ("(share OR shares OR stock OR NSE OR BSE OR Sensex OR Nifty OR results "
                   "OR Q1 OR Q2 OR Q3 OR Q4)")

# Short forms the press uses, where the listed name alone would miss them.
# Also used to keep a headline: it must mention one of these or the name.
ALIASES: dict[str, tuple[str, ...]] = {
    "BHARTIARTL.NS": ("Airtel",),
    "LT.NS": ("L&T", "Larsen"),
    "SBIN.NS": ("SBI",),
    "ETERNAL.NS": ("Zomato", "Blinkit"),
    "TCS.NS": ("TCS",),
    "HINDUNILVR.NS": ("HUL",),
    "M&M.NS": ("M&M", "Mahindra"),
    "POWERGRID.NS": ("Power Grid", "PowerGrid"),
    "ADANIPORTS.NS": ("Adani Ports", "APSEZ"),
    "CHOLAFIN.NS": ("Chola",),
    "APOLLOHOSP.NS": ("Apollo Hospitals",),
    "SUNPHARMA.NS": ("Sun Pharma",),
    "DRREDDY.NS": ("Dr Reddy", "Dr. Reddy"),
    "KOTAKBANK.NS": ("Kotak",),
    "HCLTECH.NS": ("HCLTech", "HCL Tech"),
    "DIVISLAB.NS": ("Divi's", "Divis"),
    "BEL.NS": ("BEL",),
    "TMCV.NS": ("Tata Motors",),
    "SBILIFE.NS": ("SBI Life",),
    "HDFCLIFE.NS": ("HDFC Life",),
    "ULTRACEMCO.NS": ("UltraTech",),
    "TITAN.NS": ("Titan",),
    "MARUTI.NS": ("Maruti",),
    "EICHERMOT.NS": ("Eicher", "Royal Enfield"),
    "BAJAJ-AUTO.NS": ("Bajaj Auto",),
    "HEROMOTOCO.NS": ("Hero MotoCorp",),
    "INDUSINDBK.NS": ("IndusInd",),
    "COALINDIA.NS": ("Coal India", "CIL"),
    "TRENT.NS": ("Zudio",),
}

# Names that are also ordinary words or other people's names. Live results
# for "Trent" were footballers and baseball players, "ITC" GST input tax
# credit, "Eternal" films. For these a headline must also carry a market word
# to count as being about the company.
AMBIGUOUS = frozenset({"TRENT.NS", "ITC.NS", "ETERNAL.NS", "TITAN.NS", "BEL.NS"})
_MARKET_WORD = re.compile(
    r"\b(shares?|stocks?|nse|bse|sensex|nifty|results?|q[1-4]|fy\d\d|target|brokerage|"
    r"rating|buy|sell|profit|revenue|dividend|gainers?|losers?|tata|zudio|zomato|blinkit)\b",
    re.IGNORECASE,
)
# Still collide even with a market word nearby.
_EXCLUDE: dict[str, tuple[str, ...]] = {
    "TRENT.NS": ("Severn Trent", "Alexander-Arnold", "Trent Bridge", "Trent Boult"),
    "ITC.NS": ("input tax credit", "GSTR", "ITC Hotels"),
}

# Separately listed group companies and fund houses whose names contain this
# company's name or short form. Measured on 2026-09-22 before this existed:
# 20 of 59 "SBI" stories were SBI Mutual Fund / SBI Life / SBI Funds, 23 of
# 47 "Mahindra" stories were Tech Mahindra or Kotak Mahindra, 17 of 44
# "Kotak" stories were Kotak Securities / Kotak MF. These phrases are removed
# from a headline before checking it names the company, so "SBI MF buys a
# stake in X" is dropped but "SBI, SBI Life shares rise" is kept.
SISTERS: dict[str, tuple[str, ...]] = {
    "SBIN.NS": ("SBI Mutual Fund", "SBI MF", "SBI Funds", "SBI Life", "SBI Cards", "SBI Card",
                "SBI General", "SBI Securities", "SBI Caps", "SBICAP"),
    "M&M.NS": ("Tech Mahindra", "Kotak Mahindra", "Mahindra Finance", "Mahindra & Mahindra "
               "Financial", "Mahindra Lifespace", "Mahindra Holidays", "Mahindra Logistics",
               "Mahindra Manulife", "Mahindra CIE"),
    "KOTAKBANK.NS": ("Kotak Mahindra MF", "Kotak Mahindra Mutual Fund", "Kotak Mutual Fund",
                     "Kotak MF", "Kotak Securities", "Kotak Mahindra AMC", "Kotak Mahindra Asset",
                     "Kotak Mahindra Life", "Kotak Life", "Kotak Mahindra Capital",
                     "Kotak Alternate"),
    "RELIANCE.NS": ("Reliance Power", "Reliance Infrastructure", "Reliance Infra",
                    "Reliance Capital", "Reliance Communications", "Reliance Home Finance",
                    "Reliance Nippon", "Reliance General"),
    "LT.NS": ("L&T Finance", "L&T Technology", "L&T Tech", "LTTS", "LTIMindtree",
              "L&T Infotech"),
    "TMCV.NS": ("Tata Motors Passenger Vehicles", "Tata Motors PV", "TMPV"),
    "HCLTECH.NS": ("HCL Infosystems", "HCL Foundation"),
    "ADANIENT.NS": ("Adani Ports", "Adani Green", "Adani Power", "Adani Energy", "Adani Total",
                    "Adani Wilmar", "Adani Transmission"),
}
# Reposts and videos, not reporting.
_SOCIAL = re.compile(r"facebook|youtube|instagram|x\.com|twitter|threads|linkedin",
                     re.IGNORECASE)

_QUOTE_PAGE = re.compile(
    r"option chain|stock price & chart|share price - live|live (nse|bse)[: ]|"
    r"share price live|stock price live|price chart",
    re.IGNORECASE,
)
_GENERIC_SUFFIX = re.compile(
    r"\b(limited|ltd|industries|company|enterprises?|laboratories|insurance|"
    r"corporation of india|technologies|services|and sez)\b\.?",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9&']+")
_STOP = frozenset([
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "as", "at", "by",
    "is", "are", "with", "from", "its", "after", "over", "amid", "s", "ltd", "india",
    "share", "shares", "stock", "stocks",
])

MAX_STORIES = 15
# Overlap of headline words (company name excluded) that counts as one story.
# Outlets paraphrase: on the FSSAI/Nestle story 0.6 left it split five ways,
# 0.35 merged it into two while ITC's 26 unrelated stories stayed 26.
_DUP_THRESHOLD = 0.35

# "Stocks to Watch Today, Aug 3" republished on 19-Sep, "board to meet on
# July 20" dated September: a headline naming a month far from its own
# publication date is a stale page re-dated by the aggregator, not news.
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_MONTH_IN_TITLE = re.compile(
    rf"\b(?P<m1>{_MON}) (?P<d1>\d{{1,2}})\b(?!,? ?\d{{4}}\b)"   # "Aug 3", "July 20"
    rf"|\b(?P<d2>\d{{1,2}}) (?P<m2>{_MON})"                    # "3 August"
    rf"|\b(?P<m3>{_MON}),? (?P<y3>(19|20)\d\d)\b",             # "January 2026"
    re.IGNORECASE,
)
_STALE_DAYS = 14   # a dated headline this far before publication is a re-post
_FUTURE_DAYS = 75  # dates ahead are fine (record dates, AGMs) up to this


def _month(word: str) -> int:
    return _MONTHS.index(word[:3].lower()) + 1


def is_stale_headline(title: str, published: date) -> bool:
    """True when a date named in the headline is well before its publication
    (an old page re-dated by the aggregator) or implausibly far ahead."""
    for m in _MONTH_IN_TITLE.finditer(title):
        try:
            if m["m3"]:  # month and year only: compare whole months
                named = date(int(m["y3"]), _month(m["m3"]), 1)
                months = (published.year - named.year) * 12 + published.month - named.month
                if months > 1 or months < -3:
                    return True
                continue
            month, day = (_month(m["m1"]), int(m["d1"])) if m["m1"] else (
                _month(m["m2"]), int(m["d2"]))
            named = date(published.year, month, day)
        except ValueError:  # "Feb 30", a number that is not a day
            continue
        if (named - published).days > 180:  # "Dec 28" read in January
            named = named.replace(year=named.year - 1)
        elif (published - named).days > 180:  # "Jan 5" read in December
            named = named.replace(year=named.year + 1)
        lag = (published - named).days
        if lag > _STALE_DAYS or -lag > _FUTURE_DAYS:
            return True
    return False


def _fold(text: str) -> str:
    """Lowercase, strip accents (Nestlé == Nestle) and odd quotes."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return text.lower().replace("’", "'")


def company_terms(ticker: str) -> tuple[str, list[str]]:
    """(primary name for the query, every term a headline may use)."""
    key = ticker.strip().upper()
    name = NIFTY_50_APPROX.get(key) or strip_india_suffix(key)
    name = re.sub(r"\s*\(.*?\)", "", name).strip()  # "Eternal (formerly Zomato)"
    short = re.sub(r"\s+", " ", _GENERIC_SUFFIX.sub("", name)).strip(" .,&")
    terms = [name] + ([short] if short and short != name else []) + list(ALIASES.get(key, ()))
    return name, list(dict.fromkeys(terms))


def _mentions(title: str, terms: list[str], sisters: tuple[str, ...] = ()) -> bool:
    folded = _fold(title)
    for sister in sisters:
        folded = re.sub(rf"(?<![a-z0-9]){re.escape(_fold(sister))}(?![a-z0-9])", " ", folded)
    for term in terms:
        t = _fold(term)
        # Whole-word match, so "BEL" does not match "Belgium".
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", folded):
            return True
    return False


def build_query(ticker: str, start: date, end: date) -> str:
    name, terms = company_terms(ticker)
    names = " OR ".join(f'"{t}"' for t in terms[:4])
    # before: is exclusive of that day, so +1 to include the end date.
    return (f"({names}) {_MARKET_CONTEXT} after:{start.isoformat()} "
            f"before:{(end + timedelta(days=1)).isoformat()}")


@dataclass
class Story:
    title: str
    published: datetime
    sources: list[str] = field(default_factory=list)
    words: frozenset[str] = frozenset()
    sentiment: str | None = None
    score: float | None = None


def _words(title: str, ignore: frozenset[str] = frozenset()) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(_fold(title))
                     if w not in _STOP and w not in ignore and len(w) > 1)


def _split_source(title: str, source: str | None) -> tuple[str, str]:
    """Google appends " - Outlet" to every title; move it to the source."""
    if source and title.endswith(f" - {source}"):
        return title[: -len(source) - 3].strip(), source
    head, sep, tail = title.rpartition(" - ")
    return (head.strip(), tail.strip()) if sep and len(tail) < 40 else (title.strip(), source or "?")


def cluster(items: list[tuple[str, str, datetime]], name_terms: list[str] = ()) -> list[Story]:
    """Merge near-duplicate headlines into stories, earliest version kept as
    the headline, every outlet counted once. The company's own name is left
    out of the comparison: every headline shares it, so counting it would
    make unrelated stories look alike."""
    ignore = frozenset(w for t in name_terms for w in _WORD.findall(_fold(t)))
    stories: list[Story] = []
    for title, source, published in sorted(items, key=lambda x: x[2]):
        words = _words(title, ignore)
        if not words:
            continue
        for s in stories:
            union = len(words | s.words)
            if union and len(words & s.words) / union >= _DUP_THRESHOLD:
                if source not in s.sources:
                    s.sources.append(source)
                break
        else:
            stories.append(Story(title, published, [source], words))
    stories.sort(key=lambda s: (-len(s.sources), -s.published.timestamp()))
    return stories


def _fetch(query: str) -> list[ET.Element]:
    try:
        resp = requests.get(_URL, params={"q": query, **_PARAMS},
                            headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise VendorRateLimitError(f"Google News unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise VendorRateLimitError(f"Google News returned HTTP {resp.status_code}")
    if resp.status_code != 200 or len(resp.content) > _MAX_BYTES:
        raise VendorRateLimitError(f"Google News returned HTTP {resp.status_code}")
    try:
        return ET.fromstring(resp.content).findall(".//item")
    except ET.ParseError as exc:
        raise VendorRateLimitError("Google News returned unreadable RSS") from exc


def fetch_stories(ticker: str, start: date, end: date) -> tuple[list[Story], int]:
    """Stories about ``ticker`` published in [start, end] (IST), and how many
    raw items the search returned before filtering."""
    _, terms = company_terms(ticker)
    key = ticker.strip().upper()
    exclude = [_fold(x) for x in _EXCLUDE.get(key, ())]
    ambiguous = key in AMBIGUOUS
    sisters = SISTERS.get(key, ())
    raw = _fetch(build_query(ticker, start, end))
    kept: list[tuple[str, str, datetime]] = []
    for item in raw:
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        day = published.astimezone(IST).date()
        if not (start <= day <= end):  # look-ahead guard, independent of the query
            continue
        title, source = _split_source(item.findtext("title") or "", item.findtext("source"))
        if (not title or _QUOTE_PAGE.search(title) or _SOCIAL.search(source)
                or not _mentions(title, terms, sisters)
                or (ambiguous and not _MARKET_WORD.search(title))
                or any(x in _fold(title) for x in exclude)
                or is_stale_headline(title, day)):
            continue
        kept.append((title, source, published))
    return cluster(kept, terms), len(raw)


def _as_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def get_news_google_india(ticker: str, start_date: str, end_date: str) -> str:
    if not is_india_ticker(ticker):
        raise NoMarketDataError(ticker, ticker, "Google News India covers NSE/BSE tickers only")
    start, end = _as_date(start_date), _as_date(end_date)
    stories, raw_count = fetch_stories(ticker, start, end)
    if not stories:
        # Nothing about this company survived the filters: let the next
        # vendor in the chain (Yahoo) try rather than asserting "no news".
        raise NoMarketDataError(ticker, ticker,
                                f"no company headlines on Google News India {start}..{end}")
    limit = min(MAX_STORIES, get_config().get("news_article_limit", MAX_STORIES))
    shown = stories[:limit]

    tally = ""
    if get_config().get("local_sentiment") == "finbert":
        from .finbert_sentiment import score_stories

        tally = score_stories(stories)

    lines = [
        f"## {ticker} news, {start_date} to {end_date} (Google News India)",
        f"{raw_count} search results -> {len(stories)} distinct stories about the company "
        f"after removing duplicates, quote pages and unrelated items. Showing the "
        f"{len(shown)} most widely reported. \"N outlets\" is how many publications "
        f"carried the story: a rough measure of its importance, not of its truth. "
        f"Headlines only: do not infer details a headline does not state.",
    ]
    if tally:
        lines.append(tally)
    lines.append("")
    for s in shown:
        tag = f" [FinBERT: {s.sentiment} {s.score:.2f}]" if s.sentiment else ""
        outlets = f"{len(s.sources)} outlets" if len(s.sources) > 1 else s.sources[0]
        lines.append(f"- {s.published.astimezone(IST):%d-%b} | {s.title} ({outlets}){tag}")
    return "\n".join(lines)
