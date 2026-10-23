# Alpaca and Trading 212

Alpaca executes **US-listed stocks/ETFs only**. Trading 212 Invest executes **verified non-US stock/ETF listings only**. Routing follows the listing's exchange, not company nationality or ISIN country. A US-listed foreign-company ADR still belongs to Alpaca. Unknown exchanges, crypto/CFDs and mismatched data cannot trade. A failed broker never routes an order to the other broker.

Alpaca retains prediction, day trading and the trading-loop. Trading 212 adds a separate **daily trading-loop**, using the versions activated in the existing Trading-Loop section. `SYMBOL = "LISTE"` evaluates each broker's own list independently. US results never authorize a European purchase: each listing needs its own green test and BUY from its highest-return green version (ties: smallest absolute drawdown). Existing unowned positions are shown, not adopted.

## Setup

1. Keep `TRADING_MODE=both`. Add the Trading 212 settings in `.env.example` to your server environment. Set `TRADING212_ENABLED=true`, retain `TRADING212_PAPER=true`, and enter your **demo Invest API key and secret** from Settings → API (Beta). Enable account, metadata, portfolio, pending/historical orders, transaction history and order execution permissions. Keys remain server-side.
2. Set `TWELVEDATA_API_KEY`. Trading 212's public API provides accounts/orders/metadata, **not the historical candle feed these backtests need**. The data plan must cover each exact listing with at least 1,000 complete daily candles and quotes no older than 120 seconds. Missing, delayed or mismatched data blocks buys. The bot does not purchase a subscription; international data or real-time entitlements may cost money.
3. Edit `config/trading212-instruments.json` (initially `{}`). The adjacent example shows the format. Replace its placeholder key with your account's **exact Trading 212 ticker**, and specify the corresponding provider symbol, exchange MIC, currency and ISIN. After connecting, find accessible listings through the authenticated endpoint `/api/trading212/instruments?q=Siemens`. Never guess ticker suffixes. A key that looks like a bare US symbol (e.g. `SIE`) is refused, because the Alpaca book would treat it as a US stock; Trading 212's own tickers carry a suffix such as `SIEd_EQ`. Maximum 50 mappings. Only explicitly mapped listings are entry candidates.
4. Restart the app. Docker Compose mounts `config/` read-only. Mapping edits reload without a restart; click **Retest non-US stocks** to refresh signals immediately. Set a positive Trading-Loop weight in the Alpaca allocator if that book should buy. Trading 212 uses its own `TRADING212_CAPITAL_FRACTION` and `TRADING212_CAPITAL_CAP` (in its account currency). It cannot spend Alpaca funds.
5. Review both brokers' per-stock results and errors, then enable their entries separately. Both restart paused. The top pause/resume control covers both books; Trading 212 also has an independent switch. Pausing new entries retains protective exits.

No credentials are included or requested through the dashboard. The instrument JSON contains public identifiers only. Its example ticker is deliberately a placeholder until verified against your account metadata.

Only the **daily** Trading-Loop versions run on Trading 212 (intraday `ZEITRAHMEN` versions stay with Alpaca's Intraday-Loop). Use `SYMBOL = "LISTE"`: the stress test downloads US data only, so a version written for one Trading 212 ticker cannot get a report. For each mapped listing the bot tests and self-tunes the strategy itself on the Twelve Data candles (walk-forward as in the stress test), which needs at least about 900 completed daily candles. The Trading-Loop section shows these rows as "Trading 212", with their own candle chart in the listing's currency; "Alle neu testen" also refreshes them. The read-only GPT API includes the Trading 212 account, decisions and orders.

## Orders and risk

Trading 212 uses dated working-schedule events from its API, including breaks and holidays. Missing/stale schedules block entries. Candles retain exchange-local dates; signals must use the latest completed session and target the current session. Whole-share day limit entries are attempted 5–60 minutes after the regular open, subject to fresh quotes, cash and position/gross limits. All account holdings count toward gross exposure. Paused, halted or expired pending bot buys are canceled; external orders are left alone. There is no overnight entry queue or US intraday playbook on this broker.

Confirmed filled shares receive a separate broker-held stop from `risiko.py`. This API does not provide Alpaca's atomic OTO/bracket behaviour: there is a fill-to-stop gap, and partial fills/stop replacement require reconciliation across polls. A rejected stop blocks entries and triggers an attempted exit. The loop also enforces its strategy exits and `risiko.py` account-peak drawdown limit. Unknown submission outcomes block new entries while existing broker stops remain in place. Stops cannot guarantee an execution price. Manually changing bot-owned holdings can require operator review.

Every submission is journaled before POST. Trading 212's endpoint is **not idempotent** and has no client-order-ID contract, so a timeout is never blindly retried, including after restart. Known IDs reconcile against pending orders and paginated history; a pending-order 404 is not treated as a fill. If an attempt has no confirmed ID, inspect the broker and keep this book paused; do not delete its journal or resubmit. Account identity changes require a separate `DATA_DIR`. State is isolated under `DATA_DIR/trading212/paper` or `live`.

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
