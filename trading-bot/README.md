# Signal Room

Three open-source forecasting models + independent FreeLLM news analysis + Alpaca execution, in one server application. No custom neural network, genetic optimizer, separate asset bots, CUDA installation, or paid model API is required by the forecasting engine.

## Start on your server

1. Open this `trading-bot` directory. Copy `.env.example` to `.env`.
2. Fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FREELLM_API_KEY`, and `DASHBOARD_PASSWORD`. The username is `admin`. Use **paper account** credentials with the default `ALPACA_PAPER=true`.
3. Your proxy is already configured as `FREELLM_BASE_URL=https://ai.bastiwood.com/v1`, `FREELLM_MODELS=auto`. The key is the proxy's unified API key. Do not commit it or paste it into the dashboard.
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

One app process runs both strategies concurrently. Keep your existing daily `DATA_DIR`: daily models, positions, order history and feedback retain their identity. Intraday models, forecasts, news, cooldowns and training state occupy a separate namespace in the same SQLite database; intraday checkpoint filenames are distinct. You do not need to close daily holdings to add the day-trading strategy. A standalone day-trading journal cannot be reinterpreted as a daily journal; use the existing daily directory when enabling `both`.

The strategies share one scanner, one account journal and one order coordinator. Both books' risk exits are checked before either can enter. At most one order-changing action is allowed per broker snapshot; the next 15-second cycle fetches fresh account state. Entry opportunities alternate between strategies so neither permanently gets first choice. One training worker and one FreeLLM request run at a time across both strategies.

**Position ownership:** a symbol belongs to one strategy at a time. If daily investing owns AAPL, day trading may trade other stocks but cannot buy, sell or cancel orders for that AAPL position. Ownership is reserved atomically with the entry intent, retained across restarts, and released after two flat observations with no pending orders. Day-trading time exits and pre-close flattening apply only to day-trading positions. Longer-term positions keep their original exit rules.

**Shared capital:** `DAYTRADE_CAPITAL_SHARE=0.50` gives each strategy half the existing capital allowance; it does not double the account budget. With the default $10,000 capital ceiling, 10% per position and 30% gross limit, each strategy gets a $5,000 sizing base, up to $500 per new position and $1,500 gross allocation. The account-wide gross ceiling remains $3,000, including external positions. Existing daily holdings are retained even if they exceed their new allocation; entries wait for available room. Set `DAYTRADE_CAPITAL_SHARE` between 0.05 and 0.95 to adjust the split; daily investing gets the remainder. The account daily-loss halt disables entries for both strategies.

The dashboard shows both strategy cards, capital shares, entry status and training status. **View forecasts** changes the displayed analysis only; both strategies continue running. Each card has its own entry switch; the top button enables or pauses both. The ledger and positions identify their owning strategy. Retraining applies to the strategy being viewed. Both start with entries paused after a restart.

Keep `TRADING_MODE=both` once a journal contains both strategies so all existing holdings remain monitored. To pause one strategy, use its dashboard control. Standalone `daily` and `daytrade` modes remain available for separate installations, but do not run separate bot processes against the same account. Parallel mode and day trading are paper-only.

### Daily strategy

The daily strategy retains the forecast horizon and morning entry window described below. It can hold positions overnight. New candidates train automatically, including during market hours when they have no matching model. Full scheduled retraining remains outside regular market hours. Stocks that leave the shortlist remain monitored while bot-owned positions or pending orders exist.

### Day-trading strategy

Day trading uses the same three estimator families fitted independently on completed **five-minute regular-session bars**, predicting the next **15-minute close-to-close return**. Historical calendar filtering removes extended hours and respects early closes. Intraday labels require three contiguous future bars in the same session, and validation purges overlapping labels. The holdout contains 390 labeled bars, roughly a trading week; simulated returns use non-overlapping 15-minute opportunities. Features use bar counts (the stored legacy feature IDs ending in `d` refer to five-minute bars in this mode). Intraday feedback has its own schema, resolves only after the future bars complete, and excludes forecasts captured more than 90 seconds after the input close.

Default day-trading controls:

* Risk polling every 15 seconds, with predictions refreshed each five-minute bar. Scanning, fitting, forecasting and news requests run separately from the risk loop. This is a polling strategy, not high-frequency execution.
* Up to three simultaneous day-trading positions in parallel mode (account positions in standalone mode), three entry attempts per stock per New York session, and a 15-minute cooldown after a position is confirmed closed. Deterministic per-trade order IDs support repeated round trips without blind retries after timeouts.
* Entries from 09:40 New York time until 25 minutes before the broker-reported close. New entries require a completed bar no more than 90 seconds past its close, current validated models, fresh news agreement and the existing cash/exposure limits.
* A fresh, uncrossed quote with a spread no larger than 0.3%, a whole-share bracket entry, a 1% stop and a 2% take-profit. Unfilled entries are canceled after 60 seconds. Broker-reported buying-power restrictions can only reduce the app's cash allocation.
* Timed exits after 15 minutes from the entry intent, even if news is unavailable or entries are paused. Flattening starts 10 minutes before the actual close, including half-days. Exit cancellation and broker confirmation take multiple polls; these are attempted exits, not a guarantee that positions will be flat at the close.
* News sources must be from the last two hours. Opinions refresh about every five minutes and expire after ten minutes; each work batch covers at most five symbols, prioritizing holdings and stronger BUY forecasts. A news opinion can cover successive intraday bars within its expiry.

The `DAYTRADE_*` settings are listed in `.env.example`. Day trading is **paper-only** in this implementation, and entries start paused after every restart. The LLM remains an independent news filter: it cannot bypass validation, position limits or execution checks. Neither the scanner nor the LLM guarantees a profitable selection.

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

* At least two of the three forecasting models must vote BUY, and their mean expected return must exceed the cost hurdle. All configured LLM opinions must independently say BUY. The forecasting ensemble must also have passed validation. Only then can the risk checks permit an entry.
* Agreement on SELL closes an existing bot-owned long. Disagreement, HOLD, missing news, stale news, malformed responses, or weak/uncited LLM opinions produce HOLD. There are no short sales.
* Protective stops and the account daily-loss halt can exit positions even if the models disagree, FreeLLM is down, or entries are paused. Pausing entries also cancels this bot's pending buys on the next successful broker cycle; it does not liquidate all holdings.
* US stocks/ETFs only. Market discovery shortlists 20 symbols by default; fixed-watchlist mode supports up to 50. In daily mode, Entry window: **09:35–09:59 America/New_York**, only while Alpaca says the market is open. Standalone daily mode polls every minute; parallel mode checks both strategies every 15 seconds. Daily news refreshes about every 30 minutes and expires after one hour.
* In daily mode, whole-share limit entries (up to 0.2% above the latest trade) include an Alpaca bracket with a 5% stop and 10% take-profit by default. Whole shares avoid fractional bracket restrictions. Small allocations may be unable to buy a share. Unfilled buys are canceled outside the entry window.
* Default capital ceiling: $10,000; maximum 10% of each strategy's sizing base per position and 30% gross exposure across the account. Sizing uses cash and buying power; it does not intentionally borrow or short. All account positions count toward exposure. Daily loss halt: 2% of the account's previous closing equity, latched for the New York trading day. This is account-wide and may include external activity or transfers.
* Order intents are committed to SQLite before submission. Deterministic client IDs prevent duplicate submissions across restarts. Daily mode permits one buy attempt per symbol/session; day-trading mode uses bounded per-trade entry IDs. Acceptance is recorded separately from actual fills. An ambiguous timeout is reconciled by client ID; a continuing 404 remains unresolved and blocks new decisions. Inspect such cases in Alpaca rather than deleting the journal or blindly retrying.
* Closing a bracket position first cancels existing exit legs and pending entry limits, then waits for broker confirmation before submitting a closing market order. There can be a protection gap during cancellation/exit, and stop orders cannot guarantee a fill price. Definite rejected/canceled/expired exits can be retried up to three times per session using fresh position quantities; uncertain outcomes are never blindly retried. Monitor broker rejections; the bot never labels them as filled.
* Use a dedicated Alpaca account for this bot. It leaves pre-existing unowned positions alone, but account-level limits and reconciliation cannot reliably separate manual activity in the same symbol after the bot has traded it. Do not run another bot against the same account.

Standalone daily live mode requires both `ALPACA_PAPER=false` and `ALLOW_LIVE_TRADING=true`, live account keys, and a **separate `DATA_DIR`**. Changing account/mode against an existing journal is refused. Paper and live execution differ. Live operation has not been verified in this workspace.

## Continued learning and validation

In daily mode, training fetches up to 1,500 completed, adjusted daily bars per symbol from Alpaca (default IEX). IEX is an individual exchange feed, so its volume and prices are not the consolidated SIP feed. Use a subscribed `sip` feed consistently if available. Pagination is handled and unfinished daily bars are excluded.

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

Alpaca's news endpoint supplies recent articles (last three days in daily mode; last two hours in day-trading mode), source URLs and publication timestamps. Optional `BRAVE_SEARCH_API_KEY` enables broader web news search using Brave's news endpoint; there is no unrestricted autonomous browser. Failed providers, missing dates and invalid sources do not silently become bullish signals. When no recent sources are available, new entries remain blocked.

Requests to FreeLLM are sequential, have a 45-second read timeout, a small bounded news context and a 350-token output cap. Responses must pass a strict action/confidence/citation check. News is untrusted input; the LLM has no execution tools, broker credentials or account balances. Prompts include the selected symbol, latest completed close, forecast horizon and news excerpts. Source URLs are shown but not fetched as instructions. `auto` requests follow your proxy's routing; inference may happen remotely. The dashboard reports returned model names, which can differ between requests.

For an alternative local proxy, set `FREELLM_BASE_URL=http://host.docker.internal:3001/v1` in Docker, or `http://127.0.0.1:3001/v1` in a native install. Keep the unified key configured. `FREELLM_MODELS` can hold up to two explicit model IDs; requests remain sequential and both must agree. Leaving it empty discovers one available open-weight family via `/models?available=true`.

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
* [Brave News Search API](https://api-dashboard.search.brave.com/app/documentation/news-search/get-started)

Scanner/data API references: [Alpaca assets](https://docs.alpaca.markets/us/docs/working-with-assets), [snapshots](https://alpaca.markets/learn/snapshot-api), [historical bars and timeframes](https://docs.alpaca.markets/us/reference/stockbars), and [broker protections](https://docs.alpaca.markets/us/docs/user-protection).
