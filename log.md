# Decision log and handoff — TradingAgents-India

Written 2026-09-26. This file exists because the reasoning behind this fork lives
in conversations that do not survive into a new session. Code shows *what* was
built; commit messages show *why* one change was made; this file is the map: how
the system works, which decisions are settled and on what evidence, what was
tried and rejected, what will bite you, and what is left.

If you are a model or a person picking this up cold, read sections 1–4 first.
They are enough to work safely. The rest is reference.

---

## 1. What this project is

A fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0, attribution in `NOTICE`) adapted to Indian equities, running on free
data and a free LLM tier, unattended, on one Windows laptop.

Upstream is a multi-agent LLM pipeline: four analysts (market, social/sentiment,
news, fundamentals) feed a bull/bear debate, a research manager, a trader, three
risk voices and a portfolio manager, which emits a rating. This fork adds:

- **Indian market data** NSE (institutional flows, India VIX, Nifty level,
  put-call ratio, shareholding, corporate actions, announcements), RBI policy
  rates, screener.in (the FII/DII split and quarterly P&L).
- **Indian news** Google News India edition, with heavy filtering, plus local
  FinBERT headline scoring.
- **Context the model cannot fetch itself** sector relative strength, crude and
  the rupee, and an earnings-to-cash reconciliation.
- **An unattended daily loop** that scans all 50 Nifty names on free data and
  spends LLM quota only where something changed.
- **Crash safety** because it runs on a laptop that sleeps, loses power and gets
  killed mid-run.

The universe is 50 names (`india_universe.NIFTY_50_APPROX`). Results live in
`~/.tradingagents/logs` (93 report folders as of writing).

This is a personal research tool. It is not investment advice, it is not
published, and it has never been validated against outcomes (see §9).

---

## 2. The constraint that shapes every design decision

**Gemini's free tier on this key allows 500 requests per day.** Measured from the
429 error text on 2026-09-21, confirmed against Google's published limits.

It counts **calls, not tokens**. Every LLM round-trip is one request, including
each tool-calling round where the model asks for data, receives it and is called
again. One stock costs roughly 15 requests, so the ceiling is about 33 stocks a
day, and re-analysing all 50 daily is impossible.

Three consequences that explain otherwise-odd choices:

1. **Data is pre-fetched into prompts, not exposed as tools.** A tool round costs
   a request. Every India block (NSE, relative strength, crude/rupee, earnings
   quality) is computed in Python and pasted in. This is why `india_context.py`
   exists and why new data should follow that pattern.
2. **Ratios are computed in Python, never asked of the model.** Deterministic, no
   arithmetic errors, no tokens spent showing work.
3. **Only changed stocks get re-analysed** (§5.2).

Cutting tokens is not the lever; cutting calls is. The largest remaining saving
is pre-computing the market analyst's indicators (F2 in `future_plans.md`).

---

## 3. Architecture map

### Data sources (`tradingagents/dataflows/`)
| Module | Serves | Notes |
|---|---|---|
| `india_data_common.py` | shared plumbing | look-ahead guards, IST handling, throttle, TTL cache, circuit breaker, `sentinel()` |
| `nse_india.py` | FII/DII, India VIX, Nifty level, put-call ratio, shareholding, corporate actions, announcements | needs a browser User-Agent; endpoints move |
| `rbi_rates.py` | repo/SDF/MSF/bank rate/CRR/SLR | scraped from rbi.org.in's homepage box; needs OpenSSL legacy renegotiation |
| `screener_in.py` | FII/DII split, headline ratios, **13 quarters of P&L**, annual cash flow incl. CFO/OP | opt-in, live runs only, one throttled request per company, cached 12h |
| `google_news_india.py` | company news | no key; heavy filtering (§5.5) |
| `finbert_sentiment.py` | local headline sentiment | optional extra, ~440 MB model |
| `india_relative.py` | sector relative strength, Brent + USD/INR | peer baskets, not sector indices (§5.6) |
| `earnings_quality.py` | profit-to-cash reconciliation | annual cash + quarterly P&L (§5.7) |
| `india_news.py` | macro headlines | Economic Times + Mint RSS |
| `india_context.py` | **assembles the prompt blocks** | the seam between data and agents |
| `refresh_triggers.py` | which stocks deserve re-analysis | pure logic, no I/O |
| `safe_io.py` | crash-safe files | atomic writes, sealed CSV appends, OS lock, backups, log rotation |
| `llm_clients/fallback.py` | model understudies | §5.8 |

### Scripts
| Script | Job |
|---|---|
| `daily_refresh.py` | the daily loop: scan 50, pick what changed, run the batch, report, rebuild dashboard |
| `analyze_india_universe.py` | the batch runner: one full pipeline per stock, resumable, deadline-aware |
| `build_report_dashboard.py` | HTML review page (`--latest` = newest report per stock) |
| `register_daily_refresh.ps1` | registers the Windows scheduled task |
| `verify_india_sources.py` | cross-checks every India source against an independent one |

### Outputs (`~/.tradingagents/logs`)
- `india_universe_runs.csv` — append-only history, one row per stock per run
- `reports/<TICKER>_<date>/` — the full report tree
- `refresh_<date>.md` — the day's briefing; `_dryrun.md` for dry runs
- `refresh_log.csv` — every scan decision, one row per stock per scan; carries
  `scan_date` but no run time, so a day scanned twice needs last-wins (trap 7.11)
- `review_latest.html` — dashboard
- `backups/` — dated copies of the runs CSV
- `daily_refresh.log` — the scheduled task's output, rotated at 5 MB
- `refresh.lock` — the OS lock file

### Configuration
`FREE_TIER_CONFIG` in `default_config.py` is what `--free` and the scheduled job
use. Current values:

```
llm_provider        google
deep/quick model    gemini-3.1-flash-lite
fallback models     gemini-3.5-flash-lite, gemini-3.6-flash
news_data           google_news,india_rss,yfinance
local_sentiment     finbert
indicators/stock    4          (halved from 8; the tool loop is the token cost)
debate rounds       1          risk rounds 1
india_data_enabled  True       screener_enabled True
```

---

## 4. Conventions this codebase holds itself to

Break these and the code will look wrong to whoever comes next.

1. **Verify before claiming.** Every "this works" in commit messages is backed by
   a live check with a date. This rule has repeatedly paid: NSE's "403", RBI's
   "broken certificate" and Yahoo's sector indices were all wrong on first
   reading, and `gemini-2.5-flash` passed a bare probe then 404'd in the real
   code path.
2. **Verify through the path the code uses.** A probe with a bare client is not a
   probe of the pipeline. See §7.4.
3. **Missing data becomes a sentinel, never silence or a guess.**
   `<X unavailable: reason; this is not an absence of data>`. The model is told
   explicitly that a sentinel is not a zero.
4. **No look-ahead.** Every source refuses, or labels, data that postdates the
   analysis date. Live-only sources (RBI, PCR, screener ratios) refuse historical
   runs outright rather than serve current values into the past.
5. **Tests pin behaviour, and the important ones are mutation-checked** — break
   the guard, watch the test fail, restore. Done for the look-ahead guard, the
   torn-CSV seal, the session-finality check, the thread-safe temp name and the
   filing-deadline labels.
6. **Commit messages explain the why**, name the evidence, and end with the
   Co-Authored-By line. Subject ≤72 chars, imperative.
7. **No hardcoded market "facts".** No "Nifty P/E is ~22x", no "USD/INR is 83–86"
   (it is ~95.8 now), no named F&O expiry weekday (NSE has changed it). Anything
   that goes stale is fetched or omitted.
8. **A vendor failure must subclass `VendorError`.** `evaluate` catches only
   that, and turns it into an `Assessment` note — which is the sole input to
   scan health, so the error type is what decides whether a broken source shows
   up as "degraded" or kills the scan. The India chain is
   `IndiaSourceUnavailable` → `IndiaDataError` → `VendorError`, and NSE's
   `_get_json` converts HTTP and socket errors into it. Raising anything else
   from a lookup takes the whole scan down: proved on 2026-09-27 with a plain
   `Exception`, which propagated out of `evaluate` and ended the run.
9. **Push only when asked.** The user runs `git push` themselves.

---

## 5. Decisions and their reasoning

### 5.1 India data is injected into prompts, not exposed as tools
Three reasons: free-tier providers are unreliable at tool calling (Groq rejected
a structured call outright); market-wide figures are identical for every stock in
a batch, so one fetch serves 50; and there is nothing for the model to choose —
unlike a parameterised tool, these blocks have no arguments worth varying. The
cost is that the model cannot ask for a different date, so **every block states
its own as-of date** and the prompt tells the model to cite it.

### 5.2 The daily loop is change-triggered
Re-running 50 stocks daily is impossible on 500 requests, and pointless: most of
what a report rests on does not change overnight. So all 50 are scanned on free
data and a stock is re-analysed only when something happened:

| Trigger | Weight |
|---|---|
| never analysed | 1000 (always wins) |
| price move ≥5% since its last report | 10 per percent |
| material NSE filing after that report | 30 each, capped at 3 |
| new shareholding filing | 40 |
| report older than 14 days | 20 + 1 per extra day |

Ranked by score, capped at 25 a day. Anything over the cap is **deferred, not
dropped** — its triggers are still true tomorrow. Routine filings (newspaper
notices, conference intimations) are triaged out in `nse_india`.

Observed trigger counts: 5, 8, 14, 19. Zero has never happened, which is why a
zero-trigger day is treated as suspicious (F8).

### 5.3 Crash safety, because the platform is a laptop
Every choice here came from a real failure:

- **Atomic whole-file writes** (temp + `os.replace`) for summaries, the dashboard
  and the price cache. A crash leaves the old file, never half of one.
- **The runs CSV is append-only and fsynced per stock.** A power cut loses at most
  the stock in flight. A torn last line is sealed with a newline before the next
  append, or the two rows would merge and both be lost; readers skip incomplete
  rows. All 85 existing rows passed when the stricter reader landed.
- **One run at a time via an OS file lock**, not a PID file, so the OS releases it
  when a process dies and a crash can never leave a stale lock. Verified by
  killing a holder and reacquiring. A child process inherits permission through
  `TRADINGAGENTS_REFRESH_LOCK_HELD`.
- **The runs CSV is backed up** to `backups/` before any analysis.
- **The summary is written before the analyses and again after**, so an
  interrupted run still leaves a readable briefing saying it was interrupted.
- **Dry runs and same-day re-runs cannot erase a real summary** — a dry run writes
  `_dryrun.md`, a second run appends below the first.

Proven repeatedly in practice: runs were killed four times on 2026-09-24 and the
lock always released with the CSV intact at 89/89 complete rows.

### 5.4 Scheduling: a bounded evening window, with catch-up
The task runs weekdays 19:00–21:00 IST (NSE closes 15:30 and publishes FII/DII in
the evening; Gemini's quota resets ~12:30 IST, so an evening run sees the final
close with a full quota).

**Widened from one hour to two on 2026-10-02**, with `--max-tickers` 25 → 30. A
stock takes a median 136s, so one hour fitted only ~25 and the clock bound before
the quota did: on 1 October 29 triggered and four were deferred with most of an
hour's quota unspent. Two hours makes the quota the limit (~33 stocks at ~15
requests against 500/day). Raising the window alone would have changed nothing,
because the cap would simply have bound first. The cost is that the laptop has to
stay awake an hour longer.

The window is enforced **by the script**, not only by Task Scheduler (times below
are for a 21:00 end, derived from `--until`):
- no stock is started that would not finish by ~20:57 (the estimate is the
  slowest stock seen this run, never below 240s);
- the batch is killed at ~20:59 if still running;
- a run starting after ~20:50 does nothing.

Two live failures shaped the rest:
- **A run must never analyse an open session.** On 2026-09-24 Windows ran the
  missed evening job at 09:26, eleven minutes after the open; yfinance already
  had a bar dated that day, so the old "is the newest bar today's?" check passed
  and the batch began analysing an intraday quote as a close. Now a bar counts as
  a close only after 15:30 plus a 15-minute settle.
- **A missed evening must still produce something.** With the above fix alone,
  the day produced nothing. `--catch-up` (in the task) analyses the last *closed*
  session instead, and skips it if already analysed so quota is never re-spent.

Both scheduled evenings before 2026-09-24 were missed because the laptop was off.
Wake timers are now enabled and the task has `WakeToRun`; sleep is covered,
shutdown is not. Nor is being logged out: the task's `LogonType` is `Interactive`
(`RunLevel Limited`), so it runs only while that user is logged on — a locked
screen is fine, a signed-out session is not.

Audited 2026-09-27 and correct: `State Ready`, `DaysOfWeek 62` (Mon–Fri), last
run 25-Sep 19:00 → `LastTaskResult 0`, `NumberOfMissedRuns 0`,
`DisallowStartIfOnBatteries False`, `StartWhenAvailable True`, `IgnoreNew` (which
complements the lock file), `ExecutionTimeLimit PT1H5M` against the 20:00 window.

### 5.5 News: Google News India, filtered hard
**Why:** for 15–22 Sep 2026, Yahoo returned **zero** articles for NESTLEIND,
BHARTIARTL, ULTRACEMCO and TCS. The analysts had no company news at all beyond
filings. Google News' India edition returned 36–100 results per stock from 30–70
outlets, needs no key, and honours `after:`/`before:` for historical windows.

Raw results are unusable, so each filter targets noise that was actually observed:
- **Name collisions** — "Trent" returned footballers, "ITC" GST input tax credit,
  "Eternal" a film. The query adds market context words, a headline must name the
  company or a known short form, and for ambiguous names it must also carry a
  market word.
- **Sister companies** — 30–50% of hits. "SBI" matches SBI Mutual Fund and SBI
  Life; "Mahindra" matches Tech Mahindra and Kotak Mahindra; "Kotak" matches
  Kotak Securities. Sister names are **removed from the headline first**, then the
  company must still be named — so "SBI MF buys a stake" is dropped while "SBI,
  SBI Life shares rise" is kept. Down to ~2%.
- **Quote pages and social reposts** dropped.
- **Stale re-posts** dropped: a headline naming a date more than 14 days before
  its publication is an old page re-dated by the aggregator.
- **Near-duplicates merged**, keeping the **outlet count** as a rough importance
  signal. The company's own name is excluded from the similarity comparison,
  because every headline shares it.

Output: ~15 stories, ~500 tokens. Non-Indian tickers fall through to Yahoo.

**Caveat for whoever tunes this:** the alias, sister and exclude lists were tuned
on one week of results, and names outside the Nifty 50 have no lists at all.

### 5.6 Relative strength uses peer baskets, not sector indices
Yahoo quotes every NSE sector index, but **only ^NSEBANK, ^CNXIT and ^CNXPHARMA
carry price history** — the rest return a single current bar, so a 3-month return
cannot be computed. A "does it return data" probe passes them anyway; count the
bars. Other sectors are measured against an equal-weighted basket of the stock's
Nifty 50 peers, and the block names which method it used. `^CNXMIDCAP` and
`^CNXSMALLCAP` return nothing at all (use `NIFTY_MIDCAP_100.NS`, `^CNXSC`).

### 5.7 Earnings quality: annual cash, quarterly profit, never mixed
Nothing checked whether reported profit becomes cash, so "profit grew 20%" passed
unchallenged. What the free sources allow (checked 2026-09-25):

- yfinance has **annual** cash flow with the needed line items (net income, CFO,
  capex, FCF, receivables/inventory/payables) but **no quarterly cash flow** for
  many NSE names — MARUTI returns nothing, TCS works. A related bug: the model
  often asked for quarterly and therefore saw *no cash data at all*.
- screener.in has **13 quarters of P&L** and its own annual **CFO/OP** ratio, an
  independent second opinion, from a page already cached for the ownership block
  (so no extra request).

Hence two halves: an annual reconciliation (CFO/PAT, capex, FCF, working capital
against sales growth) and a quarterly profit-quality read (margin, **other income
as a share of pre-tax profit**, tax rate, QoQ deltas) that never claims to be
about cash. The block says plainly that quarterly cash flow does not exist free.

**Freshness is labelled, not assumed.** screener's columns are quarter *ends*, not
filing dates, so each period is marked against SEBI's deadlines — 45 days for a
quarter, 60 for a year. Past it, the results were certainly public; inside it the
row reads "filing date unknown" rather than being hidden. The user chose fresher
data with a caveat over a blanket cutoff.

First live output was immediately useful: Maruti's operating margin fell 11%→8%
across 13 quarters while other income rose to 44% of pre-tax profit, on a stock
rated Overweight the day before.

### 5.8 LLM understudies for provider outages
On 2026-09-24 the 19:00 run fired correctly and then failed completely: every
call to `gemini-3.1-flash-lite` returned 503 "high demand" for over an hour,
zero of eight stocks analysed, while the same key worked on another model. Third
time this had cost runs.

A model can now have understudies. Each call tries the primary and moves down the
list **only on a transient failure** (503/502/504, "overloaded", timeouts). Quota
exhaustion and auth errors raise immediately — retrying those elsewhere spends a
second model's budget for nothing. The wrapper forwards `bind_tools` and
`with_structured_output` so tool-calling and structured analysts keep their
fallbacks, and composes behind a prompt like any chat model.

### 5.9 Ratings use a five-tier vocabulary, unparseable output is REVIEW
`Buy, Overweight, Hold, Underweight, Sell`. A decision with no parseable rating
becomes `REVIEW`, never a Hold — a Hold recorded in place of an unreadable
decision gets quoted back to the next run as a call that was never made.

---

## 6. Verified facts, with dates

Numbers that were measured, not assumed. Re-check before relying on them.

| Fact | Value | Checked |
|---|---|---|
| Gemini free tier, flash-lite | 500 requests/day, resets ~12:30 IST | 2026-09-21 |
| `gemini-3.6-flash` free tier | **20 requests/day** (~one stock) | 2026-09-24 |
| `gemini-2.5-flash` | retired: "no longer available to new users" (404) | 2026-09-24 |
| `gemini-3.5-flash-lite` | works; first understudy | 2026-09-24 |
| `gemini-3.1-flash`, `gemini-3.6-flash-lite`, `gemini-2.0-flash` | 404 | 2026-09-24 |
| Cost per stock | ~15 LLM requests, 90–350s wall time | ongoing |
| Yahoo news for NSE names | **zero** articles for 4 of 4 tested over a week | 2026-09-22 |
| Google News India per stock/week | 36–100 items, 30–70 outlets | 2026-09-22 |
| Yahoo NSE sector indices with history | only ^NSEBANK, ^CNXIT, ^CNXPHARMA | 2026-09-22 |
| yfinance quarterly cash flow, NSE | missing for MARUTI, present for TCS | 2026-09-25 |
| screener.in quarterly P&L | 13 quarters; cash flow and balance sheet annual only | 2026-09-25 |
| FinBERT agreement with hand labels | 13/16 headlines; misses broker rating calls | 2026-09-22 |
| FinBERT speed (RTX 3050) | ~1s load, 160 headlines in ~0.4s, model ~440 MB | 2026-09-22 |
| Reddit anonymous RSS | ~1 request/minute per IP | earlier |
| StockTwits coverage of the universe | 5 of 50 (via ADRs) | 2026-09-25 |
| MoSPI API | keyless, live; WPI Apr-2026 = 167; needs legacy TLS | 2026-09-22 |
| FRED India CPI | stale since March 2025 | 2026-09-20 |
| Promoter pledging | unavailable: NSE endpoint empty, screener has none | 2026-09-21 |
| Trigger counts observed | 5, 5, 7, 8, 14, 19 per scan — never zero | to 2026-09-27 |
| Universe coverage | 50/50 have a good report, oldest 7 days (max age 14) | 2026-09-27 |
| Scan health's degraded path, end to end | proven: NSE's socket killed → 50/50 notes, exit 4 | 2026-10-02 |
| NSE circuit breaker | opens after 3 failed calls, so a degraded scan costs 3 not 100 | 2026-10-02 |
| Runs CSV vs report tree | 93 `ok` rows, 93 report dirs, no orphans either way | 2026-09-27 |
| Batch errors | none since 2026-09-24, when the LLM fallback fixes landed | 2026-09-27 |
| `TATAMOTORS.NS` | dead since the demerger; use `TMCV.NS` | earlier |

---

## 7. Traps — things that have already caused bugs

**7.1 A "does it return data" probe is not enough.** Yahoo's sector indices all
answered with a current price and no history. Count the bars.

**7.2 pandas columns are new objects each access.** `column is df.columns[0]`
silently never matched and quietly dropped the working-capital line from the
earnings block. Compare by position.

**7.3 Non-atomic writes corrupt caches.** A plain `to_csv` on the price cache left
a truncated row when a process was killed; the next analysis failed with
`unconverted data remains: ,`. Fixed by writing through a temp file — which then
introduced **7.3b**: the temp name was per-process, so two threads writing one
path collided and the second found nothing to rename. Temp names are now unique
per call.

**7.4 Probe through the real code path.** `gemini-2.5-flash` answered a bare
`ChatGoogleGenerativeAI` call and then 404'd through this project's client. A
probe that skips your own layers can be worse than no probe.

**7.5 Windows, shells and escaping.** The project runs on Windows with Git Bash
available. Heredoc `\n` escaping has silently written literal backspace bytes into
a regex (`\b` became `0x08`), breaking word boundaries with no syntax error. Write
non-trivial patches from a file, not an inline heredoc. `!` commands run in Git
Bash, which eats backslashes in Windows paths — use forward slashes.

**7.6 Console encoding.** Reports contain `₹` and Hindi text. Child processes get
`PYTHONIOENCODING=utf-8`; without it the Windows code page crashes `print()`.
`grep` treats em-dash output as binary — use `grep -a`.

**7.7 Same-day quota sharing.** A manual batch and the evening run draw on the
same 500. On 2026-09-21 a manual batch consumed it and the refresh lost NESTLEIND
and ULTRACEMCO to `RESOURCE_EXHAUSTED`.

**7.8 "Complete" depends on the file.** `is_complete_row` guards against rows
torn by a crash by checking the last column parses and is long enough. The runs
CSV ends in a timestamp (19 chars) and the scan log in a date (10), and
"2026-09-21" is both a whole date and a truncated timestamp — so the caller
passes the length it expects. Reusing the default silently returned no history.

**7.9 An analyst tool's frequency argument matters.** The model chooses quarterly
or annual; for NSE names quarterly cash flow often does not exist, so it saw
nothing. Prefer annual, or fall back.

**7.10 A naive datetime sentinel poisons a tz-aware sort.** The India RSS sort
fell back to `datetime.min` for an item whose `pubDate` would not parse, and the
ET/Mint feeds are tz-aware — so one undated headline raised `can't compare
offset-naive and offset-aware datetimes` and lost the whole vendor. Nothing
failed loudly, because `news_data` is a chain: the run fell through to
google_news and finished while India headlines went missing. Normalise every
sort key through `date_window.to_utc`, sentinel included. Seen live 2026-09-25,
fixed 2026-09-27.

**7.11 Appended-per-day logs double-count when a day is scanned twice.**
`refresh_log.csv` holds one row per stock per scan and only a `scan_date`, never
a run time. Summing by date reported 24 September as one scan of 27 triggers when
it was 19 in the morning catch-up and 8 in the evening, inflating the baseline the
briefing quotes. `scan_history` now keeps each stock's last decision for the day,
so the count is the last scan of that date. Any new per-day aggregate over an
append-only log needs the same care. Found 2026-09-27.

**7.12 `india_universe_runs.csv` cannot be read with `cut` or `awk`.**
`decision_excerpt` holds commas *and* embedded newlines inside quoted fields, so
splitting on `,` reported 181 tickers and nine different column counts for a file
that is actually 134 clean 9-field rows. Parse it with Python's `csv` module.

---

## 8. Operating it

```bash
# See what would be analysed, no LLM calls
python scripts/daily_refresh.py --dry-run

# The real thing (what the scheduled task runs)
python scripts/daily_refresh.py --until 20:00 --catch-up --log <path>

# One or more specific stocks for a given session
python scripts/analyze_india_universe.py --free --no-reddit --resume \
    --date 2026-09-24 --tickers "MARUTI.NS"

# Rebuild the review page
python scripts/build_report_dashboard.py --latest

# Register / re-register the scheduled task (user runs this)
powershell -ExecutionPolicy Bypass -File scripts/register_daily_refresh.ps1 -Wake
```

**Exit codes.** `daily_refresh`: 0 normal, 1 no price data after retries, 3
another run holds the lock, 4 the scan reported nothing while a fifth or more of
its lookups failed (a silent failure — treat the day as unscanned).
`analyze_india_universe`: 0 normal, 2 `--free` without `GOOGLE_API_KEY`, 3 lock
held.

**After a run, look at:** `refresh_<date>.md` for the briefing and rating
changes, `review_latest.html` for the whole universe, `daily_refresh.log` for what
the scheduler actually did, and `india_universe_runs.csv` for history.

**Tests:** `python -m pytest -q` — 1497 collected, 1492 passing, 5 skipped
(optional deps, live API). `python -m ruff check .` must be clean.

**Environment:** Windows 11, Python 3.13 at `C:\Python313`, IST. torch and
transformers are installed **globally** from the user's other projects, so
FinBERT works but is exposed to their upgrades; `pyproject.toml` declares them as
the optional `[sentiment]` extra. `.env` holds the keys and must never be read or
echoed.

---

## 9. Open threads and future work

Full detail in `future_plans.md`. The honest ranking:

1. **F7 — validate the ratings (highest value, not started).** After 2–3 weeks of
   daily refresh, score past ratings against what prices did next, using
   upstream's backtester. **Nothing in this system has ever been checked against
   outcomes.** This matters more than any new data source, and more so now: the
   mix has drifted bullish (2 Buy, 13 Overweight, 34 Hold on 2026-09-25, from 8
   Overweight and 41 Hold on 2026-09-21). That may be a real market move, or the
   richer news feed shifting tone, or selection bias from filing-triggered stocks.
   Unknown until measured. The user asked to be reminded of this.
2. ~~**F8 — tell a quiet scan from a broken one.**~~ **Done 2026-09-26**
   (`scan_health.py`). Four states, a scope section in every briefing, exit code
   4 for a scan that reported nothing while its sources failed, and an evidence
   column in the table. **It has not yet run in production**: it landed on a
   Saturday, and 25-Sep's briefing predates it, so the first real exercise is the
   Monday 2026-09-28 19:00 run — read that briefing rather than assuming it.
   Still open from that thread: showing an event date separately from a
   publication date.
3. **F0/F2 — count requests per stock, then pre-compute the market analyst's
   indicators.** The only real way to raise the daily stock count (~30–40% more).
4. **Rating rubric + "track disconfirming evidence as rigorously as confirming
   evidence"** (from Anthropic's `financial-services` `thesis-tracker` skill,
   Apache-2.0). Nothing obliges the agents to hunt the counter-case. Deferred
   until the earnings-quality block has proven itself live. The user did not
   recall this thread — re-explain before acting.
5. **MoSPI macro API** — verified, ready to build, replaces FRED's stale India CPI.
6. **Reddit with API credentials** — the only realistic second sentiment source;
   blocked on the user creating an OAuth app.
7. **A run time in `refresh_log.csv`** — deliberately not added when trap 7.11
   was fixed. Last-wins gives the briefing the right number, but a day's two
   scans still cannot be compared against each other. Worth a `run_at` column
   only if that comparison is ever wanted; otherwise leave it.
8. **Confirm whether the two Gemini keys are one key.** `GOOGLE_API_KEY` comes
   from `.env` and `GEMINI_API_KEY` from the system environment; the client logs
   "Both set. Using GOOGLE_API_KEY". If they are different keys there are two
   500/day quotas, which changes the budget arithmetic in §2. Unverified — the
   check was blocked as key material, so the user has to run it.
9. **Also outstanding:** review the ICICIBANK and INFY reports (asked for long
   ago); alerting when a rating changes (F6); promoter pledging (no free source).

---

## 10. Rejected, with reasons

Do not re-propose these without new evidence.

| Option | Why not |
|---|---|
| **Polymarket / any prediction market** | Removed entirely 2026-10-02: prediction markets fall under India's gambling prohibition, so this fork should not be able to query one. It was also measurably worthless here — 28 calls, 28 failures, zero successes, each a 30s connect timeout, costing ~6 minutes of the 1 October window. Do not re-add it as "free keyless data" |
| **Marketaux** | Free plan: 3 articles per request, no entity sentiment — less than the free Google feed |
| **Finnhub** | Historical candles are premium; free tier is US equities; India ~$50/month for data yfinance gives free |
| **MCP-India-Stack** | GSTIN/PAN/tax/court tools, no filings or ownership; its market tools wrap yfinance; the pipeline cannot consume MCP anyway |
| **Brave Search** | Free tier killed for new users; ~$5/month credits ≈ 1,000 queries against ~1,500 needed |
| **Firecrawl** | 1,000 free credits/month against ~1,500 pages; markdown output is worse than DOM for numeric tables; screener needs no JS rendering |
| **Zerodha Kite Connect** | ₹500/month for market data; the free "Personal" plan has none |
| **X/Twitter** | Paid API, $100+/month |
| **Telegram, Moneycontrol forums** | No search API; scraping only |
| **SEBI filings scraper** | Superseded by NSE shareholding and announcements |
| **Several Google keys to dodge the quota** | Against Google's terms; use understudies or fewer calls |
| **Hardcoded benchmarks** (Nifty P/E ~22x, USD/INR 83–86, round-number support) | Stale or never true |
| **Static market-structure facts in prompts** (trading hours, circuit limits, T+1) | The model knows them; they cost tokens every call and the limits vary by stock |
| **Upstox / Angel One** (free with an account) | Not rejected — deferred. The only thing worth having from a broker is per-stock F&O open interest, and only read-only, with the token treated as a password |

---

## 11. How to leave this file

Add to it when a decision is made, a fact is measured, or something bites you.
One line in the right table beats a paragraph elsewhere. If a decision here turns
out wrong, say so in place with the date and the evidence — the value of this file
is that it records what was actually true when checked, not what was hoped.
