# Signal Room

Three open-source forecasting models + independent FreeLLM news analysis + Alpaca execution, in one server application. No custom neural network, genetic optimizer, separate asset bots, CUDA installation, or paid model API is required by the forecasting engine.

## Start on your server

1. Open this `trading-bot` directory. Copy `.env.example` to `.env`.
2. Fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `DASHBOARD_PASSWORD`. Add `FREELLM_API_KEY` for the optional news filter. The username is `admin`. Use **paper account** credentials with the default `ALPACA_PAPER=true`.
3. Your proxy is already configured as `FREELLM_BASE_URL=https://ai.bastiwood.com/v1`, `FREELLM_MODELS=` (discovers an available Llama model; alternatively set its installed ID). The key is the proxy's unified API key. Do not commit it or paste it into the dashboard.
4. Run `docker compose up -d --build` (or `./start.sh` / `start.bat`). Open `http://localhost:1337`.
5. For access from another device, set `BIND_ADDRESS` to your server's LAN address, then recreate the container. For internet access, put it behind your own HTTPS reverse proxy; Basic authentication requires transport encryption on untrusted networks.
6. Review validation and news opinions. Click **Enable both** when ready, or enable each strategy separately. Initial training starts automatically once Alpaca is configured; entries always start paused after a restart.

Local Python alternative (Python 3.12 recommended):

```sh
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Run from this directory so the default `data/` location is consistent. `requirements.lock.txt` records the exact dependency versions used for verification; install it instead of `requirements.txt` to reproduce that environment. Docker installs the same lock file.

No broker or proxy credentials are included. Without them, the dashboard shows setup status; it does not display invented prices or profits.

## Market scanning and strategy modes

The app now defaults to `UNIVERSE_MODE=market`. It enumerates Alpaca's active, tradable US equities on supported listed exchanges, fetches snapshots in batches of 100, and filters by price and previous-day dollar volume. It includes stocks and ETFs, excludes OTC and non-tradable instruments, and only considers symbols covered by your configured data feed. It cannot scan every stock exchange worldwide through this account.

The defaults are a $5 minimum price, $1 million in previous-day dollar volume, a refresh every 30 minutes, and a **20-symbol shortlist**. Liquidity, relative volume and price movement select that shortlist. Three local models are then trained per shortlisted symbol; forecasts rank entry opportunities, and FreeLLM checks recent news for each candidate. Held stocks get news priority. A failed batch blocks new market-universe entries until a complete scan succeeds; previously selected holdings still receive risk checks. The dashboard shows total assets, snapshot coverage, qualifying stocks and the shortlist.

This is a two-stage scan: it screens the available universe but does **not** fit models or call an LLM for thousands of stocks every minute. Adjust `SCAN_CANDIDATES` (1-50), `SCAN_MIN_PRICE`, `SCAN_MIN_DOLLAR_VOLUME` and `SCAN_INTERVAL_MINUTES` to control coverage and workload. IEX volume is exchange-specific; the same liquidity threshold on SIP selects a different universe. The scanner's activity score is not a probability of profit. Universe selection is not historically backtested and has current-universe selection bias.

For a fixed list, use `UNIVERSE_MODE=watchlist` and set `SYMBOLS` to up to 50 symbols. In market mode, `SYMBOLS` does not restrict discovery. The previous SPY/QQQ/IWM list remains available for fixed-watchlist use.

### Daily investing and day trading together

Use the combined configuration in `.env.example`:

```dotenv
TRADING_MODE=both
UNIVERSE_MODE=market
ALPACA_PAPER=true
DATA_DIR=data
DAYTRADE_CAPITAL_SHARE=0.50
```

One app process coordinates all three stock bots. Keep your existing daily `DATA_DIR`: daily models, positions, order history and feedback retain their identity. Intraday models, forecasts, news, cooldowns and training state occupy a separate namespace in the same SQLite database; intraday checkpoint filenames are distinct. You do not need to close daily holdings to add the day-trading strategy. A standalone day-trading journal cannot be reinterpreted as a daily journal; use the existing daily directory when enabling `both`.

The strategies share one scanner, one account journal and one order coordinator. Both books' risk exits are checked before either can enter. At most one order-changing action is allowed per broker snapshot; the next 15-second cycle fetches fresh account state. Entry opportunities alternate between strategies so neither permanently gets first choice. One training worker and one FreeLLM request run at a time across both strategies.

**Position ownership:** a symbol belongs to one strategy at a time. If daily investing owns AAPL, day trading may trade other stocks but cannot buy, sell or cancel orders for that AAPL position. Ownership is reserved atomically with the entry intent, retained across restarts, and released after two flat observations with no pending orders. Day-trading time exits and pre-close flattening apply only to day-trading positions. Longer-term positions keep their original exit rules.

**Shared capital:** `DAYTRADE_CAPITAL_SHARE=0.50` gives each strategy half the existing capital allowance; it does not double the account budget. With an optional $10,000 capital ceiling, 10% per position and 30% gross limit, each strategy gets a $5,000 sizing base, up to $500 per new position and $1,500 gross allocation. The account-wide bot gross ceiling remains $3,000; external holdings are outside it. Existing daily holdings are retained even if they exceed their new allocation; entries wait for available room. Set `DAYTRADE_CAPITAL_SHARE` between 0.05 and 0.95 to adjust the split; daily investing gets the remainder. The account daily-loss halt disables entries for both strategies.

The dashboard shows both strategy cards, capital shares, entry status and training status. **View forecasts** changes the displayed analysis only; both strategies continue running. Each card has its own entry switch; the top button enables or pauses both. The ledger and positions identify their owning strategy. Retraining applies to the strategy being viewed. Both start with entries paused after a restart.

Keep `TRADING_MODE=both` once a journal contains both strategies so all existing holdings remain monitored. To pause one strategy, use its dashboard control. Standalone `daily` and `daytrade` modes remain available for separate installations, but do not run separate bot processes against the same account. Parallel mode and day trading are paper-only.

### Three stock bots and per-stock trading-loop selection

`TRADING_MODE=both` runs **Prediction-Bot** (`daily`), **Daytrading-Bot** (`daytrade`) and **Trading-Loop** (`loop`). The loop reads `strategien/`, `berichte/` and `risiko.py` from `TRADING_LOOP_DIR` (default `trading-loop`).

**Budget and weights.** In *Deine Trader*, set a share of account equity, an optional dollar cap and a weight per bot (0–100). Capital = budget × weight / sum of weights. Per-position and gross limits apply to each bot's share and to the combined budget. Weight 0 blocks new entries while protective exits continue. Defaults split capital between daily/daytrade; the loop starts at 0. External holdings stay outside these allowances, receive analysis and are never sold by a bot. Pending account buys block new allocation until exposure is reconciled.

**Activation.** A version needs a saved stress-test report, an unchanged SHA-256, current `risiko.py` settings and a US stock/ETF or `SYMBOL = "LISTE"`. The list has no green-count threshold and no group eligibility gate. Existing red list reports remain usable when their hash and risk values match. A fresh per-stock backtest still determines whether each stock may be bought. Multiple versions of the same idea can be active together, up to ten total; activating one does not replace another.

**Selection.** For each stock the bot compares all active versions' own green tests, selects the highest return and breaks ties by the smallest absolute drawdown, then version name for a deterministic tie. It buys only when that selected version signals BUY. A lower-ranked BUY cannot override the winner's HOLD/SELL. No green test means no entry. The dashboard shows the selected version, test, signal, decision and competing versions for every stock.

The strategy runs unchanged in an isolated `app.loop_runner` subprocess over the stress test's five-year window and costs (0.1% per order). Signals are pending orders after the last completed daily bar: entry = BUY, pending close/flat = SELL, invested = HOLD. BUY targets the next market session and uses the daily entry/queue timing. File/risk changes block entries until retesting. The owning version keeps control of an existing position's exits; explicitly deactivating it closes the position unless an active successor of the same idea takes over. Each whole-share entry carries an OTO stop from `risiko.py`; fractional protection is enforced by the engine. There is no take-profit for the loop. The loop reads risk values without executing the file and stops/ closes its positions when the configured account-peak drawdown is reached. Restarting leaves entries paused.

**Self-tuning (walk-forward).** A strategy may declare `PARAMETER = {"NAME": [values, ...]}` for its own module constants (all eight bundled strategies do; at most 10 values per constant and 36 combinations per fit). For each stock, `stresstest.pruefe` then:

1. for each of the last two years, picks the combination with the best *return − ½·|drawdown|* (at least the pro-rated 10 trades and the `risiko.py` drawdown limit) using only the years **before** it, and trades that unseen year with it;
2. judges the stock on those two out-of-sample years only: green needs a positive return, drawdown within `MAX_DRAWDOWN_PROZENT` and at least 4 trades (10 per 5 years, pro-rated);
3. picks the live values on all five years.

So the verdict never comes from values that already knew the prices they are tested on. The report keeps per stock `parameter`, `standard`, the yearly picks (`test.jahre`) and the five-year figures for information (`gesamt`). The daily 08:00 retest re-tunes every stock as the window moves; strategy files are never changed (values are set in memory for each run). Strategies without `PARAMETER` keep the previous five-year verdict. The bot reuses a report's tuned values and verdict when its hash and `risiko.py` values match and it is at most 7 days older than the latest bar; a stock the report does not cover is tuned by `app.loop_runner` itself (about 2 s per stock; `TRADING_LOOP_TIMEOUT_SECONDS` now defaults to 900). Walk-forward testing reduces, but does not remove, overfitting: two test years are a small sample and the parameter lists themselves were chosen by people who had seen past markets.

`stresstest.py` retests unchanged versions daily from 08:00 New York time and on demand; the dashboard's "Alle neu testen" also includes edited files (for example after adding `PARAMETER`). LISTE reports save `aktien[symbol]` with that stock's metrics and verdict, including individual data errors. The list summary (`ergebnis=einzeltests`) is informational. Strategy files and `risiko.py` are never changed by retesting. Reports already in the repository are historical evidence and are not rewritten by this rebuild.


### Daily strategy

The daily strategy retains the forecast horizon and morning entry window described below. It can hold positions overnight. New candidates train automatically, including during market hours when they have no matching model. Full scheduled retraining remains outside regular market hours. Stocks that leave the shortlist remain monitored while bot-owned positions or pending orders exist.

### Day-trading strategy

Day trading uses the same three estimator families fitted independently on completed **five-minute regular-session bars**, predicting the next **15-minute close-to-close return**. Historical calendar filtering removes extended hours and respects early closes. Intraday labels require three contiguous future bars in the same session, and validation purges overlapping labels. The holdout contains 390 labeled bars, roughly a trading week; simulated returns use non-overlapping 15-minute opportunities. Features use bar counts (the stored legacy feature IDs ending in `d` refer to five-minute bars in this mode). Intraday feedback has its own schema, resolves only after the future bars complete, and excludes forecasts captured more than 90 seconds after the input close.

**Entries come from a rule-based playbook, not from the models.** Every 30 seconds, one Alpaca request fetches completed **one-minute bars** for all monitored stocks, and each setup is evaluated. Once per day per stock, a baseline (daily ATR, previous close, 14 sessions of opening volume and time-of-day moves) is built from prior sessions only. No trained model and no LLM is needed to trade; the prediction models **veto** an entry when two are bearish and **close** a position when three are bearish.

| Setup (`DAYTRADE_STRATEGIES`) | Entry | Stop | Exit | Source |
|---|---|---|---|---|
| `orb` (default) | First close above a **green 5-minute opening range** with opening volume ≥ its 14-day norm, daily ATR ≥ $0.50, within 0.1 ATR of the range high, until 11:30 | 10% of daily ATR | Held to the pre-close flatten | Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy for the U.S. Equity Market" |
| `noise_breakout` (default) | On a half-hour mark, close above `max(open, prev close) × (1 + 14-day average move from the open at this time)`, above VWAP, EMA 9 > EMA 20 | max(band, VWAP) | Half-hour close below max(band, VWAP), or pre-close | Zarattini, Aziz & Barbon (2024), "Beat the Market" |
| `vwap_pullback` (off) | Rising VWAP, EMA 9 > EMA 20, pullback touches VWAP, close reclaims prior bar high | Pullback low − 0.5 one-minute ATR | 2R target, trail or 60-minute time stop | Practitioner setup |
| `vwap_reversion` (off) | After 10:00, stretch below VWAP − 2σ, RSI < 35, reversal bar, reward ≥ 1.5× risk | Recent low − 1 one-minute ATR | Back at VWAP, trail or 60-minute time stop | Practitioner setup |

Every setup must expect a reward of at least three times round-trip costs where it has a target. Each bracket stop lies between 0.2% and `DAYTRADE_STOP_LOSS`. The dashboard shows which setups fired and their indicators.

**Replay check (not a backtest).** Replaying the setups minute by minute over 20 sessions (2026-09-17 to 2026-10-14) on SPY, QQQ, IWM, NVDA, TSLA, AAPL, AMD and META gave these average returns per trade before costs: ORB +0.20% (24 trades), noise breakout +0.17% (42), VWAP pullback −0.12% (87), VWAP reversion −0.01% (81). After the configured 0.2% round-trip cost, ORB was flat and the others negative. Only the two published setups are enabled by default. This sample is small and recent; it is no evidence of future profit.

Other day-trading controls:

* Up to three simultaneous day-trading positions, three entries per stock per session, and a 5-minute cooldown after a position closes. Entries from 09:35 New York time until 15 minutes before the close; intraday entries are never queued overnight.
* A fresh, uncrossed quote with a spread no larger than 0.3% and a whole-share bracket entry carrying the setup's stop and target (8% far cap for trend setups). Unfilled entries are canceled after 60 seconds.
* Flattening starts 10 minutes before the actual close, including half-days. Exit cancellation and broker confirmation take multiple polls; these are attempted exits, not a guarantee that positions will be flat at the close.

The `DAYTRADE_*` settings are listed in `.env.example`. Day trading is **paper-only** in this implementation, and entries start paused after every restart. Neither the scanner nor the setups guarantee a profitable selection.

Intraday validation uses completed closing prices and approximate stops, not actual bid/ask execution. It does not reproduce latency, partial fills, entry limits, the portfolio allocator, news decisions or every timed exit. Changing the maximum holding time does not change the fixed 15-minute prediction horizon. Evaluate complete behavior through forward paper trading; broker entitlement and account restrictions still apply.

## What the models do

The following table describes daily mode; day trading substitutes the five-minute inputs and 15-minute horizon above.

| Component | Input | Output | Learning |
|---|---|---|---|
| Ridge regression | Trailing price/volume indicators | Next-session open-to-following-open return | Regularized fit with training-only scaling |
| Extra Trees | Same indicators | Same return horizon | 96 bounded trees, depth 6 |
| Histogram gradient boosting | Same indicators | Same return horizon | 80 boosting iterations, 10 leaves |
| FreeLLM `auto` | Symbol, prior close, timestamped news summaries | BUY / SELL / HOLD, confidence, brief rationale and source IDs | Inference only; no local LLM fine-tuning |

The forecasting algorithms come from scikit-learn (BSD-3-Clause). They are established open-source implementations trained on your price history, **not pretrained financial foundation models**. FreeLLM `auto` can route to open or proprietary models depending on your proxy settings; the returned model name is shown when provided. The app does not promise that `auto` always uses open weights or remains local.

The dashboard's explanations are actual feature sensitivities and concise evidence-based LLM rationales. They are not simulated thoughts or private reasoning traces. LLM confidence is a self-assessment, not a calibrated probability. The displayed indicative price applies the return forecast to the prior close; the actual next opening price is not known in advance.

## Decisions and execution

* **Three of four votes.** Each forecasting model votes BUY or SELL when its prediction clears the cost hurdle, otherwise neutral. The fourth voter is the LLM news opinion (daily) or the day-trading setup (intraday). A trade needs at least `MIN_AGREEING_VOTES` (default 3) votes in the same direction; neutral, missing or stale votes abstain. So all three models agreeing trade even when the LLM is neutral, and two models need the LLM to agree. Two SELL-qualified votes are not enough to exit; three are. Buying still requires a validated ensemble and passing account/risk checks.
* **LLM once per day.** After each daily bar completes, one FreeLLM request covers every stock on the list and returns a JSON outlook per stock. It waits up to 10 minutes for all forecasts of the new bar, retries a failed request at most three times an hour apart, and the opinion stays valid until the next daily bar. Cited evidence with confidence at least 0.65 is required for a directional vote. An LLM cannot buy alone. No FreeLLM key is required to enable prediction-driven entries.
* Decisions use the selected strategy's actual holdings. **BUY** opens a new position after bullish agreement and risk checks. **HOLD** keeps shares the strategy already owns, including when bullish agreement continues; the bot does not add to an existing position. **WAIT** means no trade for a stock it does not own. A neutral forecast is not a reason to buy.
* Three SELL votes close an existing bot-owned long **even when it is currently losing money but remains above its stop-loss**. Insufficient agreeing votes keep owned shares on HOLD unless a protective exit applies. Bearish predictions without owned shares produce WAIT; there are no short sales. Positions owned by the other strategy or externally are left unchanged.
* Model and news cards show **BULLISH / BEARISH / NEUTRAL** outlooks, separately from the portfolio decision and holding quantity. Internally these outlooks retain BUY/SELL/HOLD labels for forecast history and training. BUY and SELL decisions express intent; the execution ledger reports order acceptance and confirmed fills. Once an exit has begun, it remains SELL while bracket cancellation and closing are completed, even if later forecasts become neutral.
* Protective stops and the account daily-loss halt can exit positions even if the models disagree, FreeLLM is down, or entries are paused. Pausing entries also cancels this bot's pending buys on the next successful broker cycle; it does not liquidate all holdings.
* US stocks/ETFs only. Market discovery shortlists 20 symbols by default; fixed-watchlist mode supports up to 50. In daily mode, Entry window: **09:35–09:59 America/New_York**, only while Alpaca says the market is open. Standalone daily mode polls every minute; parallel mode checks both strategies every 15 seconds. The daily LLM opinion is requested once per completed daily bar.
* In daily mode, whole-share limit entries (0.2% above the reference price, rounded up to the cent) include an Alpaca bracket with a 5% stop and 10% take-profit by default. Regular-session entries use the latest trade. Whole shares avoid fractional bracket restrictions. Small allocations may be unable to buy a share.
* **Closed-market buys:** `QUEUE_DAILY_ORDERS=true` (default) permits daily limit/bracket orders to be submitted to Alpaca while the regular market is closed. They use the latest completed daily close as their reference and target the broker's next regular session, including weekends and holidays. The forecast's calendar-derived target must match that session. `extended_hours=false` retains the brackets; these orders may fill from 09:30 New York time at the next open, while new regular-session submissions still start at 09:35. This queues orders for regular-session execution, not extended-hours fills. A limit price caps the purchase price but may leave the order unfilled. See [Alpaca's order rules](https://docs.alpaca.markets/us/docs/orders-at-alpaca).
* Queued entries are journaled against the target session to prevent duplicates across midnight/restarts. They survive the closed period and the first five open minutes, but are canceled when paused, halted, the qualifying signal/model/shortlist is lost, opposing news arrives, or they remain unfilled after 09:59. Existing account limits still apply, and a pending buy blocks further entries across both strategies until its outcome is reconciled. Restarting pauses entries and cancels pending buys on the next successful cycle.
* **Sizing follows the account.** The bot's capital is `CAPITAL_FRACTION` (default 100%) of current account equity, optionally capped by `MAX_CAPITAL_USD` (0 = no cap). With `TRADING_MODE=both` it is split by `DAYTRADE_CAPITAL_SHARE`. Each position gets at most 10% of its strategy's share, and gross exposure is capped at 30% of the account. Examples: $2,000 equity → $100 per position in each strategy; $100,000 → $5,000. When one share costs more than the per-position budget, the bot buys **fractional shares** (stocks Alpaca marks `fractionable`, $1 minimum) with a plain day limit order. Alpaca does not offer brackets for fractions, so the bot enforces that position's stop-loss and take-profit itself on every poll; a gap through the stop fills at the next available price. Queued closed-market orders stay whole-share brackets. Sizing uses cash and buying power; it does not intentionally borrow or short. All account positions count toward exposure. Daily loss halt: 2% of the account's previous closing equity, latched for the New York trading day. This is account-wide and may include external activity or transfers.
* Order intents are committed to SQLite before submission. Deterministic client IDs prevent duplicate submissions across restarts. Daily mode permits one buy attempt per symbol/session; day-trading mode uses bounded per-trade entry IDs. Acceptance is recorded separately from actual fills. An ambiguous timeout is reconciled by client ID; a continuing 404 remains unresolved and blocks new decisions. Inspect such cases in Alpaca rather than deleting the journal or blindly retrying.
* Closing a bracket position first cancels existing exit legs and pending entry limits, then waits for broker confirmation before submitting a closing market order. There can be a protection gap during cancellation/exit, and stop orders cannot guarantee a fill price. Definite rejected/canceled/expired exits can be retried up to three times per session using fresh position quantities; uncertain outcomes are never blindly retried. Monitor broker rejections; the bot never labels them as filled.
* Use a dedicated Alpaca account for this bot. It leaves pre-existing unowned positions alone, but account-level limits and reconciliation cannot reliably separate manual activity in the same symbol after the bot has traded it. Do not run another bot against the same account.

Standalone daily live mode requires both `ALPACA_PAPER=false` and `ALLOW_LIVE_TRADING=true`, live account keys, and a **separate `DATA_DIR`**. Changing account/mode against an existing journal is refused. Paper and live execution differ. Live operation has not been verified in this workspace.

## Continued learning and validation

In daily mode, training fetches up to 1,500 completed, adjusted daily bars per symbol from Alpaca (default IEX). IEX is an individual exchange feed, so its volume and prices are not the consolidated SIP feed. Use a subscribed `sip` feed consistently if available. Pagination is handled and unfinished daily bars are excluded. Today's bar becomes eligible 16 minutes after the calendar-reported close, including half-days. Daily forecasts refresh every 30 minutes so a completed session can generate a queued order that evening; intraday forecasts refresh every five minutes.

Every scheduled training cycle, normally once daily outside market hours, refits each model on a rolling window. Labels use the **next** opening price to the following opening price; features use only completed observations. Two-row gaps purge labels across chronological validation boundaries. There are three expanding walk-forward folds followed by a separate holdout of the latest 30 labeled sessions (`VALIDATION_BARS`, allowed range 20–120). This replaces the old 20% split, which could leave almost a year of recent data out of training at the full history cap. The exact evaluated candidate is saved; there is no unvalidated refit on holdout observations.

### Learning from the bot's own forecasts

The engine now saves an immutable experience record for each symbol and completed daily bar: all nine feature values as actually observed, every local model's predicted return, the ensemble prediction, model version, feed and capture time. The first snapshot wins across repeated polls, restarts and model updates. Capture happens for BUY, SELL and HOLD predictions, including research-only models and paused entries; a filled trade is not required.

As completed market data arrives, the engine and scheduled trainer match each snapshot to the next two trading sessions. The observed opening-price return becomes the training label. Predictions recorded after the target session opened are excluded. Unfinished horizons remain pending; feed changes and feature-schema changes stay separate. Model guesses, LLM opinions and accepted orders never become ground-truth labels. Recorded inputs and resolved outcomes are preserved instead of being regenerated from revised history.

During scheduled retraining, resolved experiences replace matching historical rows rather than adding duplicate market days. Their original features and observed return are used in the actual `.fit()` calls for all three local models, with **2× sample weight** by default (`FEEDBACK_SAMPLE_WEIGHT`, allowed range 1–3). Scaling for Ridge uses the same training-only weights. All chronological splits and promotion gates still apply. These estimators are refitted on the bounded rolling dataset; this is not an incremental update to language-model weights.

**Timing:** the newest 30 labeled sessions remain reserved for validation by default. A newly resolved experience is scored and can be used for validation first; it enters fitting only after it moves into the training partition, outside the two-row boundary gap—roughly six trading weeks after resolution with the defaults. The bot does not immediately fit a fresh outcome and then claim that the same outcome was an independent test. A trained candidate that fails promotion leaves the current model unchanged. A 30-session holdout is a small sample and cannot establish profitability; the older chronological folds and forward paper trading remain necessary checks.

The dashboard's Learning lab shows pending/resolved/excluded counts, the number of local examples actually used by the current model and latest candidate, per-model forward prediction error, and recent predicted-versus-observed returns. These scores cover the recorded model versions and are not portfolio P&L. Records begin with forecasts produced by this version; the previous decision logs did not contain complete immutable inputs and are not retroactively invented. Up to 2,000 experiences per symbol/feed/feature schema are retained separately from the rotating event journal; fitting remains within the existing market-data and memory caps.

The ensemble promotion gate requires positive holdout net return, at least five active days, maximum drawdown at most 15%, and positive mean walk-forward model return. An incumbent is compared on the same holdout only when its training labels precede it; replacement also requires an improvement of 0.1 percentage points. Models that fail initial validation remain visible as **research only** and cannot buy. Changed feed/cost/stop, feedback-weight or validation-window settings invalidate trading eligibility until retraining. Models older than 14 calendar days cannot generate new discretionary actions.

The diagnostic backtest assumes daily round trips, deducts 10 bps per side by default, and uses daily highs/lows for approximate bracket outcomes, choosing the stop when both levels were touched. The buy-and-hold benchmark is displayed, but outperforming it is not a promotion requirement. Candidate selection focuses on positive after-cost return with a drawdown bound; it does not establish expected profitability.

**Validation limits:** daily bars cannot reproduce intraday execution, gaps, spreads, changing feed coverage, corporate-action timing, or partial fills. The actual bot can hold during disagreement; the diagnostic simulation uses daily long/cash forecast decisions. The LLM filter is not historically backtested, because doing that with current news/models would create hindsight leakage. Rolling holdouts are reused over successive training cycles and are not an independent final test. Evaluate the complete combined strategy through forward paper trading; no profitable result is claimed.

## News and FreeLLM

Alpaca supplies recent timestamped news (the last three days for daily research). Each stock gets its own Llama request, source IDs, summary, action and confidence. The request contains only that stock's news and forecast horizon. Missing/invalid evidence abstains. The daily decision still combines three model ballots and **one** news ballot per stock; the market overview has no vote. Day trading uses its own playbook/model veto and never calls the news LLM.

Requests are sequential, with a 120-second read timeout. Answers must contain exactly one entry for the requested symbol and valid citations. A failure affects only that stock; successes are cached by stock, New York day and completed input bar. Each stock has up to three attempts, at least an hour apart, so adding a stock or refreshing one bar does not wait for other stocks. Returning model names are recorded. News remains untrusted data; the LLM has no execution tools or account credentials.

The report combines these per-stock summaries with a separate Llama market overview grounded in SPY/QQQ/IWM news. It is stored in SQLite under `news_report` and `news_report:YYYY-MM-DD` and shown in **Market & stock report**. A missing overview never blocks stock votes; its failure is shown explicitly. No report is invented before data and the proxy are available.

Set `FREELLM_BASE_URL` and `FREELLM_API_KEY` for your proxy. Leave `FREELLM_MODELS` empty to discover an available Llama model via `/models?available=true`, or set one installed Llama model ID. Only one model is used per stock. Cached daily forecasts keep their original chronology and learning labels.

### Intraday-Loop (trading-loop strategies on intraday candles)

A strategy file with `ZEITRAHMEN = "5Min"` (also `15Min`, `30Min`, `1Hour`) is traded by a fourth bot, **Intraday-Loop** (`loop_day`), with its own weight in *Deine Trader* (starts at 0). Files without `ZEITRAHMEN` stay with the daily Trading-Loop. `strategien/intraday_trend_v1.py` is an example (EMA cross above the session VWAP, 5-minute bars, tunable `PARAMETER`); it is untested until you run its stress test.

* **Stress test:** about 60 trading days (90 calendar days) of IEX regular-session bars in New York time, cached for 30 minutes. The strategy runs unchanged inside a wrapper that cancels entries and closes the position on every bar ending 10 minutes or less before its session's close (half days included), so no test trade is held overnight. The walk-forward always applies: values are chosen on the sessions before each half of the last 20 sessions and judged on those 20 unseen sessions; green needs a positive return, drawdown within `risiko.py` and at least 10 trades. At least 40 sessions of history are required.
* **Re-tuning:** at 08:00 New York time like daily versions, plus every 60 minutes while the market is open. Each bot only re-tests its own versions; "Alle neu testen" asks both.
* **Signals:** after every completed bar (20 seconds grace for the data feed), one bar request per timeframe covers all of the bot's stocks; each active version is evaluated in the same isolated `app.loop_runner` subprocess and reuses the report's tuned values when the report is from today or yesterday. A BUY is valid only for the bar right after its signal bar. Exits: the strategy's own sell signal, the `risiko.py` stop (broker OTO stop), the `risiko.py` account drawdown halt, and flattening 10 minutes before the actual close (`DAYTRADE_FLATTEN_MINUTES`). Entries use fresh quotes, a 60-second unfilled-entry cancel, numbered client order IDs and at most `DAYTRADE_MAX_ENTRIES_PER_SYMBOL` entries per stock and day. Paper only.
* **Limits:** 20 test sessions are a small sample, IEX volume is a single exchange, and the backtest fills at the next bar's open without spread or partial fills. Intraday strategies trade often; the 0.1% cost per order matters more than on daily candles.

### Trading-loop candle chart

In the Trading-Loop section, choose a stock to see `GET /api/loop/chart/<symbol>?bot=loop|loop_day`: the last 130 daily (or intraday) candles, the deciding version's own indicators (`self.I(...)`; price-scale lines on the candles, oscillators such as ADX/RSI in a lower pane), its backtest buys/sells with the current tuned values, the live values next to the defaults, the yearly walk-forward picks and the current signal. The chart comes from the same subprocess run that produces the signal; `/api/state` omits it to stay small.

### Forecast versus reality

Under *Inside the models*, `GET /api/predictions/<symbol>?strategy=daily|daytrade` draws candles (about 90 daily sessions, or the latest session's five-minute bars) with every model's **recorded** forecast in price terms: reference price × (1 + predicted return) at the forecast's target candle, next to the actually observed price (white diamonds) and this bot's fills (▲/▼). Daily reference is the next session's open and the target the open after it; intraday reference is the input bar's close and the target 15 minutes later. Open forecasts are dashed; for daily ones the next open is still unknown, so they use the latest close as an indicative reference. Points come only from the immutable experience records used for learning, never recomputed after the fact. Legend checkboxes hide single models; the table lists the latest eight forecasts with direction hits in green. Bars are cached for 2 minutes (intraday) or 30 minutes (daily). The order journal now also records Alpaca's `filled_at`.

### Day-trading candle chart

Select the day-trading view and a monitored stock to see the dependency-free SVG chart. `GET /api/candles/<symbol>` returns today's **completed one-minute regular-session OHLCV**, aligned indicator arrays (VWAP, EMA9/20, five-minute opening range, upper/lower noise boundaries), observed fired setup signals and an available current 15-minute model forecast. Data uses New York session dates, exchange close times including half-days, and the same indicator formulas as the playbook. Opening-range levels appear only after five bars complete. Setup markers are signals, not fills.

The engine refreshes chart snapshots alongside its playbook polls. On a cold dashboard read, the endpoint fetches today's bars for a monitored stock. Empty sessions, missing baselines and stale forecasts are explicit: unavailable forecasts are `null`, never extrapolated. A current forecast starts at its completed five-minute input close, is accepted for 90 seconds and is omitted if its horizon crosses the session close. The endpoint uses the dashboard's existing authentication; it cannot create orders.

## Hardware limits

The bot's forecasting workload is CPU-only: **0 GB GPU VRAM**, two native compute threads, one isolated training process at a time, maximum 4 GB training RSS, and a 15-minute training timeout. A separate watchdog checks worker resource usage every two seconds. Docker additionally enforces **2 CPUs and 6 GB total RAM**, with no swap expansion. This leaves ample memory on your 80 GB server and does not use the GTX 1060's 6 GB VRAM.

Native Python installs have thread/data/model caps and the training watchdog but do not have Docker's hard whole-process memory/CPU ceilings. FreeLLM's process is outside this budget: configure model size, quantization, context and concurrency in that service. The bot cannot enforce VRAM limits on a remote proxy, and it does not fine-tune language-model weights. No actual benchmark on your server has been run.

## Operations and tests

```sh
docker compose logs -f
docker compose stop
python -m unittest discover -s tests -v
```

SQLite state, order journal and model checkpoints live in `data/`; back up this directory. One OS lock prevents two app instances sharing the directory. Do not launch multiple WSGI workers. Old model files are bounded to three per symbol; event histories are bounded. Order records are retained for reconciliation. API controls require authentication when exposed off loopback and a same-origin custom header; secrets are never returned by `/api/state`.

The tests use synthetic data and mocked broker/proxy responses. Parallel-strategy tests cover independent ownership, preserved daily holdings, shared capital limits, alternating entries, risk priority, shared loss halts, pending-order isolation, atomic ownership claims, restart recovery and dashboard controls. Scanner/day-trading coverage includes batched discovery, partial scans, feed coverage, intraday labels and replay, real intraday fitting, calendar filtering, quote checks, cooldowns, repeated trades, position limits, half-day exits and stale entry cancellation. They cover chronological feature isolation, target alignment, actual fitting/checkpoint/inference, immutable forecast capture, matured outcome labels, replay weights and validation isolation, costs/stops, model agreement, stale evidence, citation validation, safe order submission/reconciliation, risk exits, authentication and duplicate-process protection. They do not prove real-market profitability or verify your API entitlements.

Removed: the custom PyTorch LSTM, 10,000-agent genetic search, six old asset-bot directories, duplicate dashboards, politician-data integration and multi-container base-image scaffolding. The original planning documents remain outside the application; existing external Docker volumes are untouched.

## References

* [scikit-learn ensembles](https://scikit-learn.org/stable/modules/ensemble.html) and [chronological splitting](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)
* [Alpaca orders and bracket behavior](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [market data plans](https://docs.alpaca.markets/us/docs/about-market-data-api), and [historical news](https://docs.alpaca.markets/us/v1.4.2/reference/news-3)
* [FreeLLM proxy API reference](https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/api/01-rest-api.md)

Scanner/data API references: [Alpaca assets](https://docs.alpaca.markets/us/docs/working-with-assets), [snapshots](https://alpaca.markets/learn/snapshot-api), [historical bars and timeframes](https://docs.alpaca.markets/us/reference/stockbars), and [broker protections](https://docs.alpaca.markets/us/docs/user-protection).

The separate [AI bot review](docs/ai-bot-review.md) records the FreqAI and TradingAgents methods inspected and the local additions. `MODEL_MAX_AGE_HOURS` expires each stock's trained model independently; API state reports its quality status and age. News decisions retain their exact evidence cutoff and a reproducible source hash with model/prompt metadata. These checks preserve the existing per-stock vote counts and strategy ownership.

Upgrade note: existing broker positions, orders and historical journal records are preserved. Positions owned by retired strategies are outside the rebuilt stock bots and receive no automated management from them. Review those positions directly at Alpaca. Existing `FREELLM_MODELS=auto` is treated as Llama discovery; an explicit installed model ID takes precedence.
