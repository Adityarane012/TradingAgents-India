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

### F0. Measure requests per ticker before optimising
Add an LLM callback that counts calls per agent, and write the totals to the
runs CSV. Today "~15 per ticker" is an estimate. With real numbers, F2 and F5
can be judged by the calls they save rather than by guesswork.
*Effort: small.*

### F1. Google News India as a news vendor — **verified 2026-09-22**
`https://news.google.com/rss/search?q="<company>"&hl=en-IN&gl=IN&ceid=IN:en`
needs no key.

Live test, last 7 days:

| Company | Articles | Sources |
|---|---|---|
| Bharti Airtel | 72 | 32 |
| Nestle India | 100 | 69 |
| UltraTech Cement | 46 | 31 |

The Nestle results included Reuters, Bloomberg and The Hindu on FSSAI's legal
action. Yahoo's feed has nothing like this coverage for Indian names.

It needs cleaning before it goes into a prompt:

- **Date filter.** Filter `pubDate <= trade date` and use `after:`/`before:`
  in the query for historical runs, so there is no look-ahead.
- **De-duplication.** The same story appears across 5–10 outlets.
- **Junk removal.** Drop quote pages such as "Option Chain - Live".
- **Stale-content check.** One item dated 17-Sep described a July board meeting.
- **Size cap.** The feed caps at about 100 items with no pagination.

Wire it into the news and sentiment analysts behind the existing
`news_data` vendor chain. *Effort: medium. Impact: the biggest data-quality gain
available.*

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

### F4. Score headlines locally with FinBERT (open source)
`ProsusAI/finbert` runs on a CPU, scores each headline as
positive/negative/neutral, and costs nothing per call. The sentiment analyst
would then get "34 headlines: 21 negative, 9 neutral, 4 positive; top 8 by
relevance: ..." instead of all 34.

- **Benefit:** a large token cut per call and a consistent, reproducible
  sentiment baseline.
- **Cost:** a PyTorch dependency, about 1 GB of download, a few seconds per
  ticker on a CPU.
- **Why optional:** keep it opt-in (`local_sentiment=True`) so a plain install
  stays light. It does not reduce the request count (see §0).

*Effort: medium.*

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

Try these after F1. They add entity tagging and sentiment scores, which save
LLM work, not just raw volume.

| API | Free tier | Why it's interesting | Watch out |
|---|---|---|---|
| **Marketaux** | ~100 requests/day (per its listing) | Tags articles with the stocks they mention and a sentiment score from −1 to 1; 5,000+ sources | **To verify:** depth of NSE ticker coverage. 100/day fits one call per Nifty 50 name |
| **NewsData.io** | 200 credits/day, commercial use allowed | Broad, 80+ languages | Keyword search only; no entity tags |
| GNews | 100/day, development use only | Simple | No full text |
| NewsAPI.org | Developer plan, effectively localhost only, delayed | — | Weakest fit |
| GDELT | Free, no key | Global, with a "tone" score per article | **To verify:** Indian business coverage; the data is noisy |

The Google News feed (F1) is free with no key and has the most coverage, so it
comes first. Marketaux is the best second source, because it pre-computes
sentiment.

---

## Tier 4 — open gaps from suggestions.md

| Gap | Possible route | Status |
|---|---|---|
| **Promoter pledging** | BSE publishes SAST/pledge disclosures, possibly behind a JSON API like NSE's | To verify. NSE's pledge endpoint returns empty; screener has no pledge data (checked 2026-09-21) |
| **Event calendar** (RBI MPC dates, results dates) | NSE has an event-calendar page, probably an API behind it; RBI publishes MPC dates in press releases | To verify. Would let prompts say "results are due in 3 days" instead of generic watch-items |
| RBI forex reserves, WPI | RBI's Weekly Statistical Supplement | To verify: format (HTML/PDF) |
| GST, IIP, PMI | MoSPI's newer data portal may have an API | To verify |
| Historical FII/DII | NSE only serves recent days publicly | Open. Could build our own history by saving the daily figure from the 7 PM run |
| Telegram channels | Needs a user account and joined channels; no search API | Low priority; noisy and hard to attribute |
| X/Twitter | 💰 $100+/month | Out of budget |

---

## Tier 5 — smoothing the daily process

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

- **Several Google accounts or keys to get around the 500/day limit.** Against
  Google's terms and would risk the key. Use F5 (local model) or F2 (fewer
  calls) instead.
- **Hardcoded "benchmarks"** (Nifty P/E "~22x", USD/INR "83–86", round-number
  support levels). These go stale or were never true. USD/INR is ~95.8 today.
- **Moneycontrol and Business Standard scraping.** No API; Business Standard
  returns 403 to RSS clients. Google News already carries their headlines.
