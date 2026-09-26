# Future plans — TradingAgents-India

Written 2026-09-22 after rechecking `issues.md` and `suggestions.md`. Every item
is zero-cost unless marked 💰. Claims marked **verified** were checked live or
against the provider's own page on the date shown. **To verify** means the
first step is to prove the claim before building anything. That rule has
already paid off: the NSE "403", the RBI "broken certificate" and the Yahoo
sector indices were all wrong on first reading.

---

## 0. The constraint that decides everything: requests, not tokens

Gemini's free tier on this key allows **500 requests a day**. Verified from the
429 error text, and it matches Google's published limits for 3.x Flash-Lite. It
is a count of calls, not of tokens.

Every LLM round-trip is one request. That includes each tool-call round, where
the model asks for data, gets it and is called again. A ticker costs about 15
requests today, so the free tier covers about 33 tickers a day.

So:

- **Cutting tokens** makes runs faster and cheaper on a paid tier, but it does
  not raise the daily ticker count.
- **Cutting calls** does. Each call removed from the pipeline is about 1/15th
  more tickers a day.
- **Pre-fetching data into the prompt** is the proven way to cut calls. The NSE
  blocks, the relative-strength block and the crude/rupee block each replace
  tool rounds the model would otherwise spend fetching data. More of the
  pipeline can work this way (F2).

---

## Tier 1 — do next (free, highest value)

> **Done so far:** F1 (Google News India) and F4 (FinBERT scoring), both on
> 2026-09-22. The next highest-value items are F0 then F2, which are what
> actually raise the number of stocks a day, and F7, which tells you whether
> any of this produces good calls.

### F0. Measure requests per ticker before optimising
Add an LLM callback that counts calls per agent, and write the totals to the
runs CSV. Today "~15 per ticker" is an estimate. With real numbers, F2 and F5
can be judged by the calls they save rather than by guesswork.
*Effort: small.*

### F1. Google News India as a news vendor — ✅ **done 2026-09-22**
`tradingagents/dataflows/google_news_india.py`, first in the `get_news` chain.

Why it mattered more than expected: for 15–22 Sep, **Yahoo returned zero
articles** for NESTLEIND, BHARTIARTL, ULTRACEMCO and TCS. The analysts had no
company news at all beyond NSE filings. Google News returned 36–100 results per
stock from 30–70 outlets, including Reuters and Bloomberg on FSSAI's action
against Nestle India.

Filters, each aimed at noise found in live results: disambiguation (a "Trent"
search returns footballers, "ITC" returns GST input tax credit), sister
companies (SBI Mutual Fund is not State Bank; 30–50% of hits before the fix),
quote pages, social reposts, stale re-dated pages, and near-duplicate merging
that keeps an outlet count as a rough importance signal. No look-ahead: the
query is bounded with `after:`/`before:` and every item is re-checked in IST.

Output is about 500 tokens per stock for ~15 distinct stories.

### F1b. Remaining polish on the news feed — open
- Merging is word-overlap based, so heavily reworded versions of one story
  still split. Sentence embeddings would fix it; that is another model to load.
- The noise filters are per-ticker lists tuned on one week of results. Check
  them again after a few weeks, especially for names outside the Nifty 50,
  which get no alias or sister list at all.
- `get_global_news` still uses the ET/Mint feeds. The same search could serve
  macro headlines.

### F2. Pre-compute the market analyst's indicators
The market analyst spends several requests fetching prices and then up to
`market_indicator_budget` indicators, one tool round each. Compute a fixed set
locally with stockstats, which already runs here: 50/200 SMA, RSI, MACD,
Bollinger, ATR and VWMA. Paste them in as a table and drop the tools.

Estimated saving: **4–6 requests per ticker**, roughly 30–40% more tickers a
day. Confirm the figure with F0 first. The trade-off is that the model can no
longer choose its indicators. Keep the tool path behind a config flag.
*Effort: medium.*

### F3. One news fetch per ticker, shared by both analysts
The news analyst and the sentiment analyst each fetch company news. Fetch once,
de-duplicate, and give each analyst the part it needs. This cuts duplicate
tokens and duplicate vendor calls. *Effort: small.*

### F4. Score headlines locally with FinBERT — ✅ **done 2026-09-22**
`tradingagents/dataflows/finbert_sentiment.py`, on in the free preset
(`local_sentiment="finbert"`), optional extra `tradingagents[sentiment]`.

Measured before enabling: agreed with hand labels on **13 of 16** real Nifty 50
headlines. Its two systematic misses were broker rating actions ("Rated Sell",
"maintains BUY") read as neutral, now labelled by rule before the model runs.
On the RTX 3050 it loads in ~1 s and scores 160 headlines in ~0.4 s; the model
is ~440 MB, downloaded once, then loaded offline.

It does **not** cut requests (§0), and it slightly increases tokens. What it
buys is a consistent baseline the LLM can be checked against.

Still open: nothing verifies FinBERT against what prices did next. Fold it into
F7 — if its tally has no relationship to the following week's return, it is
decoration.

### F8. Tell a quiet scan apart from a broken one — agreed, not yet built

`refresh_<date>.md` says "Nothing changed enough to re-analyse today" in two
different situations: a genuinely calm day, and a day when NSE blocked every
filing lookup so the filing triggers could never fire. Those look identical
today, which means a broken scan reports success.

Agreed with the user 2026-09-25/26. Scoring, ranking and the bullish/bearish
wording stay exactly as they are; this is about honesty in the summary.

## The problem being solved
`refresh_<date>.md` currently says "Nothing changed enough to re-analyse today."
That sentence is produced by **two completely different situations**:

1. The scan ran, every source answered, and genuinely nothing crossed a threshold.
2. NSE blocked us, every filing lookup failed, and the triggers that depend on
   filings could never fire.

Today those look identical, in the log and in the summary. That is the failsafe
gap you pointed at: a broken scan reports success.

## What is NOT changing (your call)
- Trigger scoring, the numbers, and the ranking stay exactly as they are.
- Bullish/bearish wording in reports stays.
- No new LLM calls, no new network calls. Everything here is computed from data
  the scan already collected.

## Design

### 1. New pure module: `tradingagents/dataflows/scan_health.py`
```python
@dataclass(frozen=True)
class ScanHealth:
    scanned: int              # tickers in the universe
    triggered: int
    failed_lookups: int       # tickers where >=1 source errored (Assessment.notes)
    reasons: dict[str, int]   # "filings unavailable" -> 12, "no price data" -> 3
    history: list[int]        # triggered counts of previous scans, oldest first

    degraded: bool            # a source is broken: failed >= 20% of universe
    silent_failure: bool      # degraded AND triggered == 0  <- the dangerous case
    verdict: str              # one line for the summary and the log
```
- `assess(assessments, triggered, history)` builds it.
- `scan_history(refresh_log_csv, limit)` reads previous scans' trigger counts —
  context for judging whether today's count is plausible.

Thresholds (named constants, not magic numbers):
- `DEGRADED_FRACTION = 0.2` — 10 of 50 tickers failing a lookup means the source,
  not the ticker, is broken.
- Zero triggers is always called out, because it has never happened: the
  observed history is 5, 8, 14, 19.

### 2. `daily_refresh.py` wiring
- Always write a **scope section** to the summary, whatever the outcome:
  ```
  ## Scan scope
  50 tickers · NSE filings + shareholding, yfinance closes, Google News (per stock)
  · price threshold 5% · max age 14 days · window ending 2026-09-25
  Lookups that failed: 3 of 50 (filings unavailable: 3)
  Previous scans triggered: 5, 8, 14, 19 · today: 8
  ```
- If `silent_failure`: put a warning **at the top** of the summary, not the
  bottom, and say plainly that the absence of triggers is not evidence of calm.
- Exit code **4** for a silent failure, so Task Scheduler's LastTaskResult shows
  something other than 0 and the operator can see it without opening files.
  (0 = normal, 1 = no price data, 3 = another run holds the lock, 4 = silent failure.)
- Zero triggers without failures: not an error, but stated as unusual with the
  history line beside it.

### 3. Evidence grouping in the "Re-analysing" table
Trigger kinds already carry this; just label them so a reader is not left to
infer why a stock is listed:
- `filing`, `shareholding` -> **Filed** (an exchange document, with its date)
- `price` -> **Measured** (computed from closes)
- `new`, `stale` -> **Housekeeping** (no new information, just coverage age)

One extra column, no new data.

## Tests (`tests/test_scan_health.py`)
1. clean scan, some triggers -> not degraded, no warning
2. 12 of 50 lookups failed -> degraded
3. 12 failed AND 0 triggered -> silent_failure, exit code 4
4. 0 triggered, 0 failures -> not degraded, but flagged unusual
5. reason counting groups by kind, not by ticker
6. history read from refresh_log.csv, newest last, malformed rows skipped
7. threshold boundary: exactly 20% degraded, 19.9% not
8. verdict wording differs for each state
9. daily_refresh writes the scope section on a nothing-triggered run
10. mutation check: break the fraction test, confirm failures

## Order of work
1. `scan_health.py` + its tests (pure, fast)
2. wire into `daily_refresh.py`, exit code, summary sections
3. evidence labels in the table
4. full suite + ruff, live dry-run, commit

## Deliberately out of scope
- Changing what triggers (that is the scoring model, which you kept)
- Backtesting whether triggers predict anything — that is F7
- Alerting on a rating change (F6)


### F5. Local model for the quick-thinking roles (zero quota)
Run `quick_think_llm` on a local open-weight model through Ollama. That covers
the analysts' tool-calling and the debate turns. Keep Gemini for
`deep_think_llm` (research manager and portfolio manager). Every quick role
moved off Gemini is a request that no longer counts against the 500.

The limit is hardware: 7–8B models need about 8 GB of RAM or VRAM and are
noticeably weaker at tool calling. Pairing this with F2 removes most of the tool
calling. **To verify** on this laptop first: run one ticker and compare the
report with Gemini's. *Effort: small to set up; the real cost is quality
checking.*

### F6. Know when a rating changes
Send a free Windows toast from the 7 PM task when a rating flips. Later, a
Telegram bot message (the Bot API is free) or an email. Today you have to open
`refresh_<date>.md` to find out. *Effort: small.*

### F7. Check whether the ratings are any good
The upstream repo has a backtester. After 2–3 weeks of daily refresh, score
past ratings against what the prices did next. Until then there is no evidence
the Overweight/Hold calls beat a coin. This matters more than any new data
source. *Effort: medium.*

---

## Tier 2 — brokerage APIs: what they add, and whether it's worth it

**Short answer:** yes, it's possible, and two brokers are free. For this project
they add less than you'd expect: daily prices from yfinance are already
adequate. What a broker adds that nothing else free does is **per-stock
derivatives data** (open interest, futures basis, per-stock put-call ratio),
**official exchange prices**, and **market depth**.

| Broker | Cost for data | What's included | Catch |
|---|---|---|---|
| **Upstox** | ₹0 — **verified** ("All trading + data APIs are free of cost") | Historical candles (daily up to 1 yr, weekly/monthly up to 10 yrs), live websocket, option chain | Needs an Upstox account; access token expires daily, so the 7 PM job needs a re-login step |
| **Angel One SmartAPI** | ₹0 — **verified** (free historical data for NSE/BSE/NFO/indices) | Historical + live data, websocket | Needs an Angel One account; login uses TOTP, which can be automated |
| **Zerodha Kite Connect** | 💰 ₹500/month — **verified** | Live + historical data | The free "Personal" plan has **no** market data, only orders and portfolio |
| Dhan, Fyers, Shoonya | Advertised as free | Similar | **To verify**: terms and data scope |

Recommendation:

- **Only integrate a broker you already have an account with.** Upstox is the
  cleanest free option.
- **Use it read-only.** Request data scopes only, never enable order placement,
  and keep tokens in `.env`, which is already excluded from git and from my
  reads.
- **What to build first:** per-stock F&O open interest and PCR for the Nifty 50
  names. These are the one real signal yfinance cannot give.
- **Security note:** a broker token can place trades. Treat it like a password.

### Free exchange data that needs no broker — to verify
NSE's daily "full bhavcopy" CSV (`sec_bhavdata_full_DDMMYYYY.csv` in NSE
archives) reportedly includes **delivery percentage** per stock. That is the
share of traded volume actually taken into demat, a widely watched conviction
signal. It would fit straight into `nse_india.py`. Verify the URL and fields
first.

---

## Tier 3 — news APIs with keys (free tiers)

With F1 and F4 done, none of these is needed. Kept for reference if the Google
feed degrades.

| API | Free tier | Why it's interesting | Watch out |
|---|---|---|---|
| ~~Marketaux~~ | 100 requests/day, **3 articles per request**, no entity sentiment | — | ❌ **Rejected 2026-09-22** (checked on its pricing page). 3 headlines per stock is far less than Google News gives, and the sentiment scores that made it attractive are not in the free plan. FinBERT supplies those locally |
| **NewsData.io** | 200 credits/day, commercial use allowed | Broad, 80+ languages | Keyword search only; no entity tags |
| GNews | 100/day, development use only | Simple | No full text |
| NewsAPI.org | Developer plan, effectively localhost only, delayed | — | Weakest fit |
| GDELT | Free, no key | Global, with a "tone" score per article | **To verify:** Indian business coverage; the data is noisy |

A key-based API only earns its place if it carries Indian business news the
Google feed misses. Check that before adding one.

---

## Tier 4 — open gaps from suggestions.md

| Gap | Possible route | Status |
|---|---|---|
| **Promoter pledging** | BSE publishes SAST/pledge disclosures, possibly behind a JSON API like NSE's | To verify. NSE's pledge endpoint returns empty; screener has no pledge data (checked 2026-09-21) |
| **Event calendar** (RBI MPC dates, results dates) | NSE has an event-calendar page, probably an API behind it; RBI publishes MPC dates in press releases | To verify. Would let prompts say "results are due in 3 days" instead of generic watch-items |
| RBI forex reserves, WPI | RBI's Weekly Statistical Supplement | To verify: format (HTML/PDF) |
| CPI, WPI, IIP (and GDP, employment) | **MoSPI e-Sankhyiki — verified live 2026-09-22.** `https://api.mospi.gov.in`, keyless GET, e.g. `/api/wpi/getWpiRecords?year=2026` returned WPI April 2026 = 167. Also `/api/cpi/getCPIIndex`, `/api/iip/getIipData`, `/api/nas/getNASData`. Needs OpenSSL legacy renegotiation, like RBI. The ministry publishes an MIT-licensed reference client (github.com/nso-india/esankhyiki-mcp) that documents every endpoint and parameter | **Ready to build.** This is the official source for the macro series FRED serves stale (its India CPI stops at March 2025). Exact parameter names per dataset are in that repo's `definitions/*.json` |
| GST collections, PMI | Not in the MoSPI API (GST is CBIC, PMI is S&P Global, licensed) | Open |
| Historical FII/DII | NSE only serves recent days publicly | Open. Could build our own history by saving the daily figure from the 7 PM run |
| Telegram channels | Needs a user account and joined channels; no search API | Low priority; noisy and hard to attribute |
| X/Twitter | 💰 $100+/month | Out of budget |

---

## Tier 5 — smoothing the daily process

- **The 7 PM run needs the laptop awake and logged in — it has not fired yet.**
  22-Sep it started at 21:28 (too late, refused); 23-Sep it did not run at all
  and Windows started it at 09:26 on the 24th, mid-session. Both cases are now
  handled in code: a run never analyses an open session, and `--catch-up`
  (in the task since 2026-09-24) analyses the last closed session instead.
  Still worth deciding: re-register with `-Wake` so the evening slot actually
  fires, or move the window to when the machine is reliably on. A catch-up run
  is a day late and gets no live RBI/PCR/screener figures, since those refuse
  past dates.
- **Weekend catch-up run.** Saturday has the full quota and no competing manual
  runs. It could clear anything deferred during the week.
- **Quota guard.** Before a manual batch on a weekday, warn if it would eat into
  the 7 PM run's share of the 500. That is exactly what cost NESTLEIND and
  ULTRACEMCO on 21-Sep.
- **Dashboard "what changed" view.** Show only the stocks whose rating changed
  since the last visit, with the trigger that caused the re-analysis.
- **Build our own history.** Store the daily FII/DII, VIX and PCR figures so
  historical runs stop getting sentinels.
- **Cache unchanged analyst reports.** If a stock's inputs haven't changed since
  its last run, for example the fundamentals, reuse that analyst's report
  instead of regenerating it.

---

## Considered and rejected

- **Finnhub** (checked 2026-09-25). Its marketing lists NSE, and that is true —
  but not on the free tier. Historical candles moved to the premium plans and a
  free key gets `403 "You don't have access to this resource"`; the free tier is
  US equities for most endpoints, and international markets start around
  $50/month. It would cost money for the daily bars yfinance already serves
  free, and it still would not give the one thing worth paying for here
  (per-stock NSE derivatives — see Tier 2).

- **MCP-India-Stack** (github.com/rehan1020/MCP-India-Stack), reviewed
  2026-09-22. 76+ tools for GSTIN/PAN/IFSC/UPI validation, income-tax and
  GST calculators, EMI/PPF/SIP calculators, court and RTI helpers. It is built
  for compliance and personal-finance workflows, not equity research: no
  filings, shareholding, ownership or news. Its only market tools wrap
  yfinance, which this project already calls directly, and its identity
  validators check format and checksums only, not registration status.
  Two further reasons it does not fit here:
  - **The pipeline cannot consume MCP.** MCP servers expose tools to an
    assistant (Claude Code, Claude Desktop). TradingAgents' analysts call
    their own vendor layer (`interface.py`). Using such a server's data would
    mean calling the underlying API directly anyway.
  - **Tools cost requests.** Every tool round is one of the 500/day (§0), and
    tool schemas cost tokens on every call. The direction here is the
    opposite: pre-fetch data into the prompt.
  It is a reasonable MCP server to attach to Claude Code for one-off GST or
  IFSC lookups. That is a different job from this project.

- **Several Google accounts or keys to get around the 500/day limit.** Against
  Google's terms and would risk the key. Use F5 (local model) or F2 (fewer
  calls) instead.
- **Hardcoded "benchmarks"** (Nifty P/E "~22x", USD/INR "83–86", round-number
  support levels). These go stale or were never true. USD/INR is ~95.8 today.
- **Moneycontrol and Business Standard scraping.** No API; Business Standard
  returns 403 to RSS clients. Google News already carries their headlines.
