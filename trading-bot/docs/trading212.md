# Alpaca and Trading 212

## Parallel predictions and strategy execution

Both brokers run independently during each listing's exchange hours. The US open does not stop Trading 212 buying on an open European exchange. Each uses its own funds, data and order journal. Pausing entries still permits protective exits. No positions or cash are transferred between brokers.

Trading 212 now has **two strategies**: daily predictions from Ridge, Extra Trees and Histogram Gradient Boosting, plus the daily strategy-file Trading-Loop. Previously this account only ran the loop; it had no prediction training/forecasting worker. The models now train on validated non-US daily candles, publish separate votes and validation status per stock, expire independently and learn from their own recorded outcomes. No news source covers these listings, so only the three models vote: by default 2 of 3 must agree (`TRADING212_MIN_AGREEING_VOTES=2`, or 3 for unanimity). Earlier versions also counted a news vote that can never be cast for European stocks, so effectively all three had to agree. Unvalidated or expired models cannot authorize buys. US stock news is not reused as a fourth vote for European listings.

`TRADING212_PREDICTION_SHARE=0.50` reserves half this account's configured bot budget for predictions and half for the daily loop. Each share has its own position/gross limits; an account-wide cash/exposure check still applies. A stock belongs to one strategy at a time, so the other cannot add to it or sell it. Dashboard switches separately control prediction entries and loop entries; **Retrain non-US models** requests training. Prediction training needs no activated loop version and no Alpaca credentials.

Alpaca and Trading 212 share **one training-process slot** with the existing memory/time watchdog. Training cannot block either broker's risk polling. One failed strategy does not stop the other strategy receiving validated data. Broker-held stops are added for filled shares, and exits remain pending across stop cancellation even if the forecast changes.

The free feed supports the daily strategies. Alpaca continues to run its US intraday prediction/playbook and intraday strategy-file loop. Intraday strategy files are not evaluated against Trading 212 daily candles. The Yahoo service is unofficial and can be delayed or unavailable; listing-identity, quote-age, limit-order and session checks remain in force.

The Trading 212 dashboard displays the three model votes, training errors, validation/freshness and decision reason per stock. Its read-only API includes the prediction book too. The six combined/separate money and return figures are unchanged. Existing loop positions/journals are retained; prediction state has its own namespace in the isolated Trading 212 database. Entries restart paused, while data collection and training start automatically when configured.

Alpaca executes **US-listed stocks/ETFs only**. Trading 212 Invest executes **verified non-US stock/ETF listings only**. Routing follows the listing's exchange, not company nationality or ISIN country. A US-listed foreign-company ADR still belongs to Alpaca. Unknown exchanges, crypto/CFDs and mismatched data cannot trade. A failed broker never routes an order to the other broker.

Alpaca retains prediction, day trading and the trading-loop. Trading 212 adds a separate **daily trading-loop**, using the versions activated in the existing Trading-Loop section. `SYMBOL = "LISTE"` evaluates each broker's own list independently. US results never authorize a European purchase: each listing needs its own green test and BUY from its highest-return green version (ties: smallest absolute drawdown). Existing unowned positions are shown, not adopted.

## Setup

1. Keep `TRADING_MODE=both`. Add the Trading 212 settings in `.env.example` to your server environment. Set `TRADING212_ENABLED=true`, retain `TRADING212_PAPER=true`, and enter your **demo Invest API key and secret** from Settings → API (Beta). Enable account, metadata, portfolio, pending/historical orders, transaction history and order execution permissions. Keys remain server-side.
2. Market data: Trading 212's public API provides accounts/orders/metadata, **not the historical candles these backtests need**. By default (`TRADING212_DATA_SOURCE=yahoo`) the bot uses Yahoo Finance's free public chart endpoint, which covers Xetra, Euronext and the other supported exchanges: about six years of daily candles (at least 1,000 needed) and a price that may be a few minutes old (`TRADING212_MAX_QUOTE_AGE_SECONDS`, default 1200). It is unofficial, needs no key, is meant for personal use and has no guaranteed availability. Each response must match the mapping's exchange, currency and instrument type. Daily candles are downloaded once per listing and exchange day. Twelve Data remains available with `TRADING212_DATA_SOURCE=twelvedata` and `TWELVEDATA_API_KEY`, but its free plan covers **US data only**; European exchanges need a paid plan (Grow for end-of-day, Pro for real-time).
3. `config/trading212-instruments.json` ships with 20 euro listings (10 DAX stocks, ASML, four CAC 40 stocks and five broad ETFs: MSCI World, FTSE All-World, S&P 500, DAX, MSCI EM IMI). The bot checks every entry against your account's metadata and Twelve Data before trading; an entry that does not match shows an error on its row and is never traded. Edit the file to add or remove listings. The adjacent example shows the format. Replace its placeholder key with your account's **exact Trading 212 ticker**, and specify the corresponding provider symbol, exchange MIC, currency and ISIN. After connecting, find accessible listings through the authenticated endpoint `/api/trading212/instruments?q=Siemens`. Never guess ticker suffixes. A key that looks like a bare US symbol (e.g. `SIE`) is refused, because the Alpaca book would treat it as a US stock; Trading 212's own tickers carry a suffix such as `SIEd_EQ`. Maximum 50 mappings. Only explicitly mapped listings are entry candidates.
4. Restart the app. Docker Compose mounts `config/` read-only. Mapping edits reload without a restart; click **Retest non-US stocks** to refresh signals immediately. Set a positive Trading-Loop weight in the Alpaca allocator if that book should buy. Trading 212 uses its own `TRADING212_CAPITAL_FRACTION` and `TRADING212_CAPITAL_CAP` (in its account currency). It cannot spend Alpaca funds.
5. Review both brokers' per-stock results and errors. **Alle Bots einschalten** now switches Trading 212 on too, unless credentials are missing, live trading is not allowed or its drawdown stop is active; anything else still missing (data, mappings, an active daily version) is reported and blocks single buys. The Trading 212 panel shows how many listings were loaded from the JSON file, or why it could not be read. Both books restart paused.

   Which daily version to activate matters: on the shipped 20 listings (checked 23 Oct 2026) `grid_v1` was green on 15, `grid_v2` on 11, the mean-reversion versions on 5–6, breakout/trend_v1 on 3 and `trend_v2` on none. Results change with every retest. The top pause/resume control covers both books; Trading 212 also has an independent switch. Pausing new entries retains protective exits.

No credentials are included or requested through the dashboard. The instrument JSON contains public identifiers only. Its example ticker is deliberately a placeholder until verified against your account metadata.

Only the **daily** Trading-Loop versions run on Trading 212 (intraday `ZEITRAHMEN` versions stay with Alpaca's Intraday-Loop). Use `SYMBOL = "LISTE"`: the stress test downloads US data only, so a version written for one Trading 212 ticker cannot get a report. For each mapped listing the bot tests and self-tunes the strategy itself on the Twelve Data candles (walk-forward as in the stress test), which needs at least about 900 completed daily candles. The Trading-Loop section shows these rows as "Trading 212", with their own candle chart in the listing's currency; "Alle neu testen" also refreshes them. The read-only GPT API includes the Trading 212 account, decisions and orders.

## Orders and risk

Trading 212 uses dated working-schedule events from its API, including breaks and holidays. Missing/stale schedules block entries. When the response contains only today's events, `exchange_calendars` supplies the missing previous close and next session dates, including holidays and daylight-saving changes. Those reference dates never override a closed or missing broker session. Candles retain exchange-local dates; signals must use the latest completed session and target the current session. Whole-share day limit entries are attempted during the whole regular session, from 5 minutes after the open until 30 minutes before the close, subject to a fresh price, cash and position/gross limits. A buy that has not filled after 15 minutes is canceled and may be retried in a later 15-minute slot; an order with an unknown outcome still blocks all new entries. All account holdings count toward gross exposure. Paused, halted or expired pending bot buys are canceled; external orders are left alone. There is no overnight entry queue or US intraday playbook on this broker.

Confirmed filled shares receive a separate broker-held stop from `risiko.py`. This API does not provide Alpaca's atomic OTO/bracket behaviour: there is a fill-to-stop gap, and partial fills/stop replacement require reconciliation across polls. A rejected stop blocks entries and triggers an attempted exit. The loop also enforces its strategy exits and `risiko.py` account-peak drawdown limit. Unknown submission outcomes block new entries while existing broker stops remain in place. Stops cannot guarantee an execution price. Manually changing bot-owned holdings can require operator review.

Every submission is journaled before POST. Trading 212's endpoint is **not idempotent** and has no client-order-ID contract, so a timeout is never blindly retried, including after restart. Known IDs reconcile against pending orders and paginated history; a pending-order 404 is not treated as a fill. If an attempt has no confirmed ID, inspect the broker and keep this book paused; do not delete its journal or resubmit. Account identity changes require a separate `DATA_DIR`. State is isolated under `DATA_DIR/trading212/paper` or `live`.

An explicit HTTP 429 rate-limit rejection is recorded as rejected, with its error, and may be tried in a later entry slot. It does not create an ambiguous order that blocks the account permanently. Timeouts, connection failures and server errors still remain unresolved until checked; they are never automatically resubmitted.

## When Trading 212 appears idle

The dashboard shows daily-data progress, per-listing failures, loop-evaluation progress and prediction training separately. Failed daily-data requests retry after about a minute. An early response missing the previous completed candle is not retained for the entire day. Slow strategy backtests run in a separate worker so they cannot prevent new forecasts after training. Unchanged inputs reuse the existing loop evaluation; **Retest non-US stocks** requests a fresh evaluation.

Yahoo's public chart hosts can fail independently. A connection failure or server error on one host tries the other Yahoo host and retains the working host for later requests; exact listing validation still applies. HTTP 429 pauses requests for a minute without switching hosts, and access-denied responses are reported directly.

Use **Download diagnostics** in the Trading 212 panel (authenticated `GET /api/trading212/diagnostics`) to inspect the current engine error, data/session dates, active versions, model decisions and unresolved order statuses. The download excludes API keys, authentication headers, account IDs, balances and position quantities. The read-only summary also exposes data errors and progress. HTTP 401 identifies a key/environment problem; HTTP 403 points to endpoint permissions or IP restrictions. A connected account alone does not mean candles, models, sessions and order permissions are ready.

After updating these fixes, rebuild the container so the new calendar dependency is installed: `docker compose up -d --build`. For a local installation, install `requirements.lock.txt` in its virtual environment and restart the process. Entries restart paused; review the status, enable the intended Trading 212 books and use **Retest non-US stocks**. No live connection or order submission is needed for the automated regression tests.

Same-account-currency entries are the default (`TRADING212_ALLOW_FX=false`; GBX quotes scale to GBP correctly). Foreign-currency entries require `TRADING212_ALLOW_FX=true` and fresh execution FX from the data provider. FX fees and local transaction taxes can apply. Cash sizing reserves 1% for fees/price uncertainty. Backtests retain the existing trading-loop cost assumption (0.1% per side); assess local taxes and currency costs before interpreting a green result. Commission-free does not mean zero total cost.

## The six money figures

| Card | Meaning |
|---|---|
| A | Combined account equity in `REPORTING_CURRENCY` (EUR by default) |
| B | Combined capital-weighted return |
| C | Alpaca equity in native currency |
| D | Alpaca return |
| E | Trading 212 equity in native currency |
| F | Trading 212 return |

Equity includes cash and holdings; the cards also show available cash, mode and update time. Percentages measure investment returns, not each account's share of the total. Returns start with the first complete funding-history snapshot, **not account inception**, and persist across restarts:

- Net capital = initial tracked equity + later external deposits − withdrawals.
- P&L = current equity − net capital.
- Return = P&L / net capital (unavailable if net capital is zero/negative).

Complete funding history is required so deposits are not labeled as profit. Unvalued securities transfers or missing permissions leave the return unavailable. This is a simple cash-flow-adjusted capital return, not a time-weighted return or annualized performance.

Combined equity and P&L use daily reference FX from [Frankfurter](https://frankfurter.dev/), with the rate date shown. Combined return = converted total P&L / converted total net capital, **not the average of the percentages**. Current reference FX converts both numerator and denominator, so this excludes investment performance from FX movements. FX is for display only; no conversion, transfer or splitting of funds is performed.

Balances refresh roughly every minute. Missing/stale account data, absent FX or mixed demo/live modes hide the combined figure. Each account's own available balance remains visible. Data/history outages never stop the other broker's risk worker.

## Modes and references

Trading 212 defaults to demo. To read a live account, set `TRADING212_PAPER=false`; execution additionally requires `TRADING212_ALLOW_LIVE_TRADING=true` and deliberate dashboard activation. This is independent of Alpaca's live gate. No live broker connection or real order was verified in this workspace. Validate your account entitlements, listing IDs and order handling in demo first.

- [Trading 212 API and OpenAPI contract](https://docs.trading212.com/)
- [API keys and live order types](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key)
- [Trading 212 fees](https://helpcentre.trading212.com/hc/en-us/articles/11471996799517-What-are-the-fees-in-the-Invest-ISAs-and-SIPP)
- [Twelve Data API](https://twelvedata.com/docs)
- [Frankfurter API](https://frankfurter.dev/)


## Why nothing is bought: the diagnosis

The Trading 212 panel lists each step with ✓/✗: account connection, listings loaded from the JSON file, listings found in the account's metadata (including exchange names the router does not know), daily candles, active Trading-Loop versions with green tests and today's buy signals, trained and validated prediction models, entry switches, exchange hours and the last order. The first ✗ is where the chain stops.

Two causes found on 28 Oct 2026 and fixed:

- **Yahoo's newest daily candle has no close for hours after the session.** The bot dropped that row, so its newest candle was always one session older than Trading 212's calendar, and every signal of both books was rejected as outdated ("Waiting for the latest completed exchange session's daily candle"). The row is now completed from the response's final trade price when both are from the same exchange day.
- **An impossible news vote.** See above.

Exchange names: Trading 212 does not publish them. If a mapped listing's exchange name is unknown, the mapping's MIC is used, but only when the broker's ISIN and currency match the mapping and nothing points to a US listing. Unknown names appear in the diagnosis.

Prediction models only buy after passing validation (positive return on the last 30 sessions with at least 5 active days, positive walk-forward return). On the shipped listings they rarely signal strongly enough to pass, so expect most Trading 212 trades from the Trading-Loop until the models find an edge.
