"""Relative strength and commodity/FX context for Indian equities.

Two prompt blocks built from yfinance prices, both measured as of the trade
date (bars after it are never read, so a historical run sees no future):

- ``relative_strength_block``: the stock's 1/3/6-month return beside its
  sector and the Nifty 50. "Up 8% in three months" means little on its own;
  beside a sector up 15% it is a laggard. Given to the market analyst.
- ``commodity_fx_block``: Brent crude and USD/INR. India imports most of its
  crude, so oil and the rupee move margins for refiners, paints, chemicals,
  airlines and autos, and the rupee moves IT exporters' earnings. Given to the
  news analyst beside the NSE/RBI figures.

**Why most sectors are peer baskets, not NSE indices.** Yahoo quotes every
NSE sector index, but checked on 2026-09-21 only three carry price history:
Nifty Bank, Nifty IT and Nifty Pharma. The rest (Auto, FMCG, Metal, Energy,
Infra, Financial Services, ...) return today's quote and nothing before it,
so a 3-month return cannot be computed from them. Those sectors are measured
instead by an equal-weighted basket of the stock's Nifty 50 peers, which all
have full history. The block names which one it used. A ticker outside the
map gets the Nifty 50 line only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from .symbol_utils import is_india_ticker

logger = logging.getLogger(__name__)

NIFTY_50 = "^NSEI"

# Sector -> the NSE index to compare against, where Yahoo has its history.
SECTOR_INDEX: dict[str, str] = {
    "Banks": "^NSEBANK",
    "IT services": "^CNXIT",
    "Pharma": "^CNXPHARMA",
}

# The Nifty 50 universe by business. Peers for the basket come from here.
SECTOR_OF: dict[str, str] = {
    **dict.fromkeys(["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS",
                     "AXISBANK.NS", "INDUSINDBK.NS"], "Banks"),
    **dict.fromkeys(["BAJFINANCE.NS", "SHRIRAMFIN.NS", "CHOLAFIN.NS", "BAJAJFINSV.NS"],
                    "Non-bank lenders"),
    **dict.fromkeys(["SBILIFE.NS", "HDFCLIFE.NS"], "Life insurers"),
    **dict.fromkeys(["TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS"],
                    "IT services"),
    **dict.fromkeys(["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS"], "Pharma"),
    **dict.fromkeys(["MARUTI.NS", "M&M.NS", "TMCV.NS", "EICHERMOT.NS", "HEROMOTOCO.NS",
                     "BAJAJ-AUTO.NS"], "Autos"),
    **dict.fromkeys(["ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "HINDUNILVR.NS"], "FMCG"),
    **dict.fromkeys(["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS"], "Metals"),
    **dict.fromkeys(["NTPC.NS", "POWERGRID.NS", "COALINDIA.NS"], "Power and coal"),
    **dict.fromkeys(["ULTRACEMCO.NS", "GRASIM.NS"], "Cement"),
    **dict.fromkeys(["LT.NS", "BEL.NS"], "Capital goods"),
    **dict.fromkeys(["TITAN.NS", "ASIANPAINT.NS", "TRENT.NS"], "Consumer discretionary"),
    # Deliberately unmapped (no fair Nifty 50 peer): RELIANCE, BHARTIARTL,
    # ADANIENT, ADANIPORTS, APOLLOHOSP, ETERNAL. They get the Nifty 50 line.
}

# Trading-day windows: about 1, 3 and 6 months.
WINDOWS = ((21, "1M"), (63, "3M"), (126, "6M"))
_HISTORY_DAYS = 280  # calendar days fetched; comfortably covers 126 sessions


def _as_date(curr_date: str | date | None) -> date:
    if curr_date is None:
        return date.today()
    if isinstance(curr_date, date):
        return curr_date
    return datetime.strptime(str(curr_date)[:10], "%Y-%m-%d").date()


def trailing_returns(closes: pd.Series, as_of: date) -> tuple[date, dict[str, float]] | None:
    """Percent returns over each window ending at the last close on or before
    ``as_of``. Returns (date of that close, {label: pct}), or None if there
    is no close by then. A window longer than the history is left out."""
    series = closes.dropna()
    series = series[series.index.date <= as_of]
    if series.empty:
        return None
    last = float(series.iloc[-1])
    out = {}
    for n, label in WINDOWS:
        if len(series) > n and float(series.iloc[-1 - n]) > 0:
            out[label] = (last / float(series.iloc[-1 - n]) - 1.0) * 100.0
    return series.index[-1].date(), out


def basket_returns(
    closes: pd.DataFrame, members: list[str], as_of: date
) -> tuple[date, dict[str, float], int] | None:
    """Equal-weighted mean of the members' returns per window, and how many
    members contributed. None if no member has prices by ``as_of``."""
    results = [r for m in members if m in closes
               and (r := trailing_returns(closes[m], as_of)) is not None]
    if not results:
        return None
    day = max(d for d, _ in results)
    out = {}
    for _, label in WINDOWS:
        vals = [rets[label] for _, rets in results if label in rets]
        if vals:
            out[label] = sum(vals) / len(vals)
    return day, out, len(results)


def _download(symbols: tuple[str, ...], as_of: date) -> pd.DataFrame:
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    data = yf.download(list(symbols), start=as_of - timedelta(days=_HISTORY_DAYS),
                       end=as_of + timedelta(days=1),  # end is exclusive
                       progress=False, auto_adjust=True, group_by="column", threads=True)
    closes = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    if not isinstance(data.columns, pd.MultiIndex):
        closes.columns = list(symbols)
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes


def _fmt(label: str, result) -> str:
    if result is None:
        return f"{label}: no prices on or before the analysis date"
    day, rets = result[0], result[1]
    parts = ", ".join(f"{k} {v:+.1f}%" for k, v in rets.items()) or "too little history"
    return f"{label}: {parts} (to the close of {day:%d-%b-%Y})"


def relative_strength_block(ticker: str, curr_date: str | date | None = None) -> str:
    if not is_india_ticker(ticker):
        return ""
    as_of = _as_date(curr_date)
    key = ticker.strip().upper()
    sector = SECTOR_OF.get(key)
    index = SECTOR_INDEX.get(sector) if sector else None
    peers = [t for t, s in SECTOR_OF.items() if s == sector and t != key] if sector else []
    symbols = tuple(dict.fromkeys([ticker, NIFTY_50] + ([index] if index else []) + peers))
    try:
        closes = _download(symbols, as_of)
    except Exception as exc:  # noqa: BLE001 — context must never fail the analysis
        logger.warning("relative strength download failed for %s: %s", ticker, exc)
        return ("\n\n### Relative strength\n<relative strength unavailable: price download "
                "failed; this is not an absence of data>")

    lines = [_fmt(ticker, trailing_returns(closes[ticker], as_of) if ticker in closes else None)]
    if index:
        lines.append(_fmt(f"{sector} — NSE index {index}",
                          trailing_returns(closes[index], as_of) if index in closes else None))
    elif peers:
        basket = basket_returns(closes, peers, as_of)
        n = basket[2] if basket else 0
        lines.append(_fmt(f"{sector} — equal-weighted basket of {n} Nifty 50 peer(s): "
                          + ", ".join(peers), basket))
    lines.append(_fmt(f"Nifty 50 ({NIFTY_50})",
                      trailing_returns(closes[NIFTY_50], as_of) if NIFTY_50 in closes else None))
    return (
        "\n\n### Relative strength (computed from prices, pasted in)\n"
        "Price returns over about 1, 3 and 6 months of trading days, ending at the "
        "last close on or before the analysis date. Say whether the stock is leading "
        "or lagging its sector and the market, and do not restate these figures as "
        "anything more precise than they are.\n"
        + "\n".join(f"- {line}" for line in lines)
    )


# Identical for every ticker in a batch, so fetched once per date. Only
# successes are kept: a transient failure must not stick for the whole batch.
_COMMODITY_CACHE: dict[date, str] = {}


def commodity_fx_block(curr_date: str | date | None = None) -> str:
    as_of = _as_date(curr_date)
    if as_of in _COMMODITY_CACHE:
        return _COMMODITY_CACHE[as_of]
    pairs = (("BZ=F", "Brent crude (USD/bbl)"), ("USDINR=X", "USD/INR (rupees per dollar)"))
    try:
        closes = _download(tuple(s for s, _ in pairs), as_of)
    except Exception as exc:  # noqa: BLE001
        logger.warning("commodity/FX download failed: %s", exc)
        return ("<crude and rupee unavailable: price download failed; this is not an "
                "absence of data>")
    parts = []
    for symbol, label in pairs:
        series = closes[symbol].dropna() if symbol in closes else pd.Series(dtype=float)
        series = series[series.index.date <= as_of]
        result = trailing_returns(series, as_of)
        if result is None:
            parts.append(f"{label}: no data on or before the analysis date")
            continue
        day, rets = result
        change = f", 1M {rets['1M']:+.1f}%" if "1M" in rets else ""
        parts.append(f"{label} {float(series.iloc[-1]):.2f} on {day:%d-%b-%Y}{change}")
    block = "; ".join(parts)
    _COMMODITY_CACHE[as_of] = block
    return block
