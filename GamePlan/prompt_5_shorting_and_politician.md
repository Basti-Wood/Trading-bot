# Trading Bot — Prompt 5/5: Shorting & Politician Signal

---

## Shorting System

Bots profit in both directions:
- **LONG**: buy low → sell high (price goes UP)
- **SHORT**: borrow → sell → buy back cheaper (price goes DOWN)

The LSTM already predicts DOWN — shorting just acts on it instead of ignoring it.

### Risk Profile
```
Long  position: max loss = 100% (stock hits zero)
Short position: max loss = UNLIMITED (stock can rise forever)
→ Shorts always get tighter stop-losses and their own circuit breaker check
→ Force-close any short that moves against us by >3% immediately
→ Never hold a short overnight unless dna.short_hold_days explicitly allows it
```

### Fractional Trading & Shorting — Critical Constraint
Alpaca does NOT support fractional short positions. This affects the broker layer:

```
Long  order → use notional ($100 of AAPL)     ✅ fractional supported
Short order → use whole qty (2 shares of AAPL) ❌ fractional NOT supported
```

The `open_short()` function in `shared/broker/alpaca.py` always converts the risk manager's
notional dollar amount into whole shares by rounding down:
```python
whole_shares = int(notional_usd // current_price)
if whole_shares < 1:
    return   # position too small to short — skip entirely
```

This means small portfolios may find some stocks too expensive to short at all.
The bot should silently skip the short rather than error — log it to the ledger.

### Which Bots Short
```python
SHORTING_ALLOWED = {
    "crypto"     : True,
    "stocks"     : True,
    "etf"        : True,
    "politician" : True,
    "funds"      : False,   # never, under any circumstances
}
```

### Alpaca Short Orders (`shared/broker/alpaca.py`)

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

client = TradingClient(API_KEY, SECRET_KEY, paper=True)   # paper=False for live

def open_long(symbol: str, qty: float, stop_loss: float):
    client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
    ))
    client.submit_order(StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL,
        stop_price=stop_loss, time_in_force=TimeInForce.GTC
    ))

def close_long(symbol: str, qty: float):
    client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
    ))

def open_short(symbol: str, qty: float, stop_loss: float):
    # Alpaca handles share borrowing automatically (requires margin account)
    client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
    ))
    # Stop-loss for short = BUY order above entry to cap losses
    client.submit_order(StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY,
        stop_price=stop_loss, time_in_force=TimeInForce.GTC
    ))

def close_short(symbol: str, qty: float):
    client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
    ))
```

### AgentDNA Short Fields (already in dna.py — listed here for reference)
```python
short_enabled      : bool  = True    # GA evolves this — if False, bot never shorts
max_short_exposure : float = 0.5     # max fraction of pool in shorts simultaneously
short_hold_days    : int   = 3       # auto-close shorts older than this many days
```

If the GA evolves `short_enabled = False` for a given asset class, trust it and leave it.

---

## Politician Trading Signal (`shared/data/politician_trades.py`)

US politicians must disclose trades within 45 days (STOCK Act — fully legal, public data).
Studies show politician trades still beat the market even after the disclosure delay.
**US equities only** — not useful for crypto or EU assets.

Data source: **Quiver Quantitative** free tier (`https://api.quiverquant.com`)

### Fetcher
```python
import requests, pandas as pd
from datetime import datetime, timedelta

QUIVER_BASE = "https://api.quiverquant.com/beta"

POLITICIAN_WEIGHTS = {
    "House Financial Services Committee" : 1.5,
    "Senate Banking Committee"           : 1.5,
    "House Intelligence Committee"       : 1.3,
    "Senate Intelligence Committee"      : 1.3,
    "default"                            : 1.0,
}

def get_politician_trades(ticker: str, api_key: str) -> pd.DataFrame:
    """
    GET /historical/congresstrading/{ticker}
    Columns: Date, Politician, Party, Chamber, Committee, Transaction, Amount
    Apply POLITICIAN_WEIGHTS to weight by committee relevance.
    Sort by Date descending.
    """

def compute_politician_signal(ticker: str, api_key: str, lookback_days: int = 60) -> dict:
    """
    Returns 3 ML-ready features:
      pol_net_sentiment : float  weighted (-1.0 all sells → +1.0 all buys)
      pol_signal_age    : int    days since most recent trade (999 = no recent signal)
      pol_buy_count     : int    number of buy transactions in lookback window
    """
    df     = get_politician_trades(ticker, api_key)
    cutoff = datetime.now() - timedelta(days=lookback_days)
    recent = df[df["Date"] >= cutoff]

    if recent.empty:
        return {"pol_net_sentiment": 0.0, "pol_signal_age": 999, "pol_buy_count": 0}

    buys  = (recent["Transaction"] == "Purchase").sum()
    sells = (recent["Transaction"] == "Sale").sum()
    total = buys + sells
    net   = (buys - sells) / total if total > 0 else 0.0

    return {
        "pol_net_sentiment" : round(float(net), 3),
        "pol_signal_age"    : (datetime.now() - recent["Date"].max()).days,
        "pol_buy_count"     : int(buys),
    }
```

### Option A — Extra LSTM Features in Stocks Bot
Add the 3 politician features to `FEATURES` in `ml_model.py` for stocks only.
Call `compute_politician_signal()` inside `engineer_features()` gated by asset class.
The LSTM learns the weighting automatically during training.

```python
# in shared/data/preprocessor.py
def engineer_features(df, asset_class=None, ticker=None, pol_api_key=None):
    # ... existing 7 features ...

    if asset_class == "stocks" and ticker and pol_api_key:
        pol = compute_politician_signal(ticker, pol_api_key)
        df["pol_net_sentiment"] = pol["pol_net_sentiment"]
        df["pol_signal_age"]    = pol["pol_signal_age"]
        df["pol_buy_count"]     = pol["pol_buy_count"]

    return df.dropna()
```

### Option B — Standalone Politician Bot (`bots/politician/bot_politician.py`)
Rule-based only — no LSTM, no GA. Trades purely on disclosure signals.
Own isolated pool (5% of total capital). Subject to circuit breaker.

```python
WATCHLIST = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AMD"]

class PoliticianBot:
    def __init__(self, pool_value: float, api_key: str):
        self.pool      = pool_value
        self.api_key   = api_key
        self.tracker   = PositionTracker()
        self.breaker   = CircuitBreaker(max_drawdown=0.05)

    def run(self):
        while True:
            for ticker in WATCHLIST:
                sig = compute_politician_signal(ticker, self.api_key)

                # Strong buy: net sentiment > 0.6, signal fresh (< 30 days)
                if sig["pol_net_sentiment"] > 0.6 and sig["pol_signal_age"] < 30:
                    if self.tracker.is_flat(ticker):
                        self._buy(ticker, sig["pol_net_sentiment"])

                # Strong sell/short: net sentiment < -0.6, signal fresh
                elif sig["pol_net_sentiment"] < -0.6 and sig["pol_signal_age"] < 30:
                    if self.tracker.is_long(ticker):
                        broker.close_long(ticker, ...)
                    if self.tracker.is_flat(ticker):
                        self._short(ticker, abs(sig["pol_net_sentiment"]))

                # Stale signal → close position
                elif sig["pol_signal_age"] > 45 and not self.tracker.is_flat(ticker):
                    broker.close_long(ticker, ...) if self.tracker.is_long(ticker) \
                        else broker.close_short(ticker, ...)

            status = self.breaker.update(get_portfolio_value())
            if status == "DEMOTE":
                close_all_positions()
                break
            sleep(3600)   # check every hour
```

### Build Order for Politician System
1. Build `shared/data/politician_trades.py` (after fetcher in step 3)
2. Plug features into stocks bot LSTM — Option A (after stocks bot in step 16)
3. Build standalone `bots/politician/bot_politician.py` — Option B (after funds bot)
4. Run both for 4+ weeks, compare Sharpe scores in the ledger
5. Keep whichever approach scores better; optionally drop the other

---

## Summary: All Bot Behaviours

| Bot | LSTM | GA evolved | Shorts | Politician signal | Sells |
|---|---|---|---|---|---|
| crypto | ✅ | ✅ | ✅ | ❌ | ✅ |
| stocks | ✅ | ✅ | ✅ | ✅ as features | ✅ |
| etf | ✅ | ✅ | ✅ | ❌ | ✅ |
| politician | ❌ rule-based | ❌ | ✅ | ✅ only signal | ✅ |
| funds | ❌ | ❌ | ❌ | ❌ | ❌ never |
