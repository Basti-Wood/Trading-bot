# Trading Bot — Prompt 2/5: ML Model, Strategy & Broker

## PyTorch LSTM (`shared/strategy/ml_model.py`)

### Architecture
```python
class TradingLSTM(nn.Module):
    def __init__(self, input_size, hidden_size=128, num_layers=2, dropout=0.2):
        # LSTM (batch_first=True)
        # → Linear(hidden_size → 64) → ReLU → Dropout → Linear(64 → 2)
        # input shape : (batch, seq_len, n_features)
        # output      : logits for 2 classes — price UP or DOWN in next 5 candles
```

### Features (input_size = 10 for stocks, 7 for crypto/ETF)
Base features (all bots):
- `return_1`, `return_5`, `return_20` — price momentum
- `rsi` — 14-period RSI
- `volatility` — 20-period rolling std of return_1
- `ma_cross` — sma_20 / sma_50 - 1
- `vol_ratio` — volume / 20-period avg volume
- Important, Honeypot scams should be ignored

Stocks bot only (add these 3):
- `pol_net_sentiment` — politician net buy/sell signal (-1.0 to +1.0)
- `pol_signal_age` — days since most recent politician trade (999 = no signal)
- `pol_buy_count` — number of politician buys in last 60 days

### Dataset (`PriceSequenceDataset`)
- Sliding window of `dna.seq_len` candles per sample
- Target: `1` if `close.shift(-5) > close` else `0`
- Time-series split 80/20 — never shuffle, no lookahead bias

### Training
```python
optimizer = Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)
criterion = CrossEntropyLoss()
device    = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# epochs=50, batch_size=64
# No data shuffling — DataLoader(shuffle=False)
```

### Prediction
```python
def predict(self) -> tuple[str, float]:
    # Returns ('BUY' | 'SELL' | 'HOLD', confidence)
    # Needs >= dna.seq_len candles in rolling buffer before predicting
    # Apply softmax to logits
    # If max(proba) < confidence_threshold → return 'HOLD'
    # proba[1] > proba[0] → 'BUY', else → 'SELL'
```

---

## Temperature System (Aggression Control)

Single `temperature` float (0.0–1.0) inside AgentDNA — evolved by GA, never hardcoded.
Pass into both `MLStrategy` and `RiskManager` at construction.

| Temperature | Behaviour |
|---|---|
| 0.0–0.3 | Passive — high confidence required, small positions, wide stops |
| 0.4–0.6 | Balanced — default thresholds and sizing |
| 0.7–1.0 | Aggressive — low threshold, larger positions, tight stops |

```python
# Implement these formulas exactly:
confidence_threshold = 0.80 - (temperature * 0.28)   # 0→0.80,  1→0.52
position_size_mult   = 0.5  + temperature             # 0→0.5x,  1→1.5x
atr_multiplier       = 3.0  - (temperature * 2.0)    # 0→3.0 wide, 1→1.0 tight
```

---

## Fallback Strategy (`shared/strategy/fallback.py`)

Activates when ML confidence < `confidence_threshold`.

```
BUY  when RSI < 35 AND close > sma_50   (oversold in uptrend)
SELL when RSI > 65 AND close < sma_50   (overbought in downtrend)
HOLD otherwise
```

Returns `(signal: str, confidence: float)` — same interface as MLStrategy.
If signal is SELL and shorting is enabled → caller opens a short, not just closes a long.

---

## Risk Manager (`shared/risk/manager.py`)

Position sizes are always calculated in **dollar/notional amounts**, not share quantities.
This maps directly to Alpaca's notional order parameter and supports fractional trading naturally.

```python
class RiskManager:
    def __init__(self, dna: AgentDNA, pool_config: dict):
        # Reads: dna.temperature, dna.max_risk, dna.max_short_exposure
        # Derives: confidence_threshold, position_size_mult, atr_multiplier

    def position_size_usd(self, portfolio_value: float) -> float:
        """Returns dollar amount to deploy — used directly as notional in orders."""
        return portfolio_value * dna.max_risk * position_size_mult
        # e.g. $5,000 × 2% × 1.0x = $100.00 notional

    def stop_loss(self, entry_price: float, side: str, atr: float) -> float:
        # LONG:  entry_price - (atr * atr_multiplier)
        # SHORT: entry_price + (atr * atr_multiplier)
        # Shorts get 40% tighter multiplier than longs (extra safety)

    def short_exposure_ok(self, current_short_value: float, portfolio_value: float) -> bool:
        # True if current shorts < pool_config["max_short_exposure"] * portfolio_value
```

---

## Position Tracker (`shared/risk/position_tracker.py`)

```python
@dataclass
class Position:
    ticker       : str
    side         : str        # "LONG" or "SHORT"
    notional_usd : float      # dollar value at entry (for fractional longs)
    qty          : float      # share quantity (for shorts — must be whole shares)
    entry        : float      # entry price
    stop_loss    : float
    fractional   : bool       # True if this is a fractional position
    opened_at    : datetime

class PositionTracker:
    positions: dict[str, Position]

    def is_long(self, ticker)  -> bool: ...
    def is_short(self, ticker) -> bool: ...
    def is_flat(self, ticker)  -> bool: ...
    def open_long(self, ticker, notional_usd, entry, stop_loss, fractional): ...
    def open_short(self, ticker, qty, entry, stop_loss): ...
    def close(self, ticker): ...
    def all_short_value(self, current_prices: dict) -> float: ...
```

---

## Broker (`shared/broker/alpaca.py`)

### Fractional Trading Rules
- Alpaca supports fractional shares on 5,000+ US equities — enabled by default
- Minimum order: $1 notional
- Precision: up to 6 decimal places for qty
- **Fractional LONGS only — fractional SHORTS are NOT supported**
- Short positions must always be whole share quantities
- Always check `fractionable` flag before placing fractional orders

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

client = TradingClient(API_KEY, SECRET_KEY, paper=True)  # paper=False for live

def is_fractionable(symbol: str) -> bool:
    """Check if asset supports fractional trading before ordering."""
    return client.get_asset(symbol).fractionable


def open_long(symbol: str, notional_usd: float, stop_loss: float, current_price: float):
    """
    Place a long order. Uses notional (dollar amount) for fractionable assets,
    falls back to whole shares for non-fractionable ones.
    Notional maps directly from RiskManager.position_size_usd() — no conversion needed.
    """
    if is_fractionable(symbol):
        # Fractional: buy exactly $X of the stock
        client.submit_order(MarketOrderRequest(
            symbol        = symbol,
            notional      = str(round(notional_usd, 2)),   # e.g. "100.00"
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.DAY,
        ))
        # Stop-loss: approximate whole share qty for stop order
        approx_qty = max(1, round(notional_usd / current_price))
        client.submit_order(StopOrderRequest(
            symbol        = symbol,
            qty           = str(approx_qty),
            side          = OrderSide.SELL,
            stop_price    = str(round(stop_loss, 2)),
            time_in_force = TimeInForce.GTC,
        ))
    else:
        # Non-fractionable: round down to whole shares we can afford
        whole_shares = int(notional_usd // current_price)
        if whole_shares < 1:
            return   # can't afford even one share — skip
        client.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = str(whole_shares),
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.DAY,
        ))
        client.submit_order(StopOrderRequest(
            symbol        = symbol,
            qty           = str(whole_shares),
            side          = OrderSide.SELL,
            stop_price    = str(round(stop_loss, 2)),
            time_in_force = TimeInForce.GTC,
        ))


def close_long(symbol: str, qty: float):
    client.submit_order(MarketOrderRequest(
        symbol        = symbol,
        qty           = str(round(qty, 6)),
        side          = OrderSide.SELL,
        time_in_force = TimeInForce.DAY,
    ))


def open_short(symbol: str, notional_usd: float, stop_loss: float, current_price: float):
    """
    Short positions must be WHOLE SHARES — Alpaca does not support fractional shorts.
    Convert notional to whole share qty, rounding down.
    """
    whole_shares = int(notional_usd // current_price)
    if whole_shares < 1:
        return   # position too small to short — skip

    client.submit_order(MarketOrderRequest(
        symbol        = symbol,
        qty           = str(whole_shares),
        side          = OrderSide.SELL,    # sell to open short
        time_in_force = TimeInForce.DAY,
    ))
    # Stop-loss for short: BUY order above entry to cap losses
    client.submit_order(StopOrderRequest(
        symbol        = symbol,
        qty           = str(whole_shares),
        side          = OrderSide.BUY,     # buy to close short
        stop_price    = str(round(stop_loss, 2)),
        time_in_force = TimeInForce.GTC,
    ))


def close_short(symbol: str, qty: float):
    client.submit_order(MarketOrderRequest(
        symbol        = symbol,
        qty           = str(int(qty)),     # always whole shares for shorts
        side          = OrderSide.BUY,
        time_in_force = TimeInForce.DAY,
    ))
```

---

## Main Bot Loop (all trading bots follow this pattern)

```python
# Every 60 seconds:
for symbol in SYMBOLS:
    row    = fetch_and_engineer(symbol)
    signal, conf = ml.predict()

    if signal == "HOLD":
        signal, conf = fallback.predict(row)
    if signal == "HOLD":
        continue

    atr          = compute_atr(symbol)
    sl           = risk.stop_loss(row["close"], signal, atr)
    notional_usd = risk.position_size_usd(portfolio_value)

    if signal == "BUY":
        if tracker.is_short(symbol):
            broker.close_short(symbol, tracker.positions[symbol].qty)
        if tracker.is_flat(symbol):
            broker.open_long(symbol, notional_usd, sl, row["close"])
            tracker.open_long(symbol, notional_usd, row["close"], sl,
                              fractional=broker.is_fractionable(symbol))

    elif signal == "SELL":
        if tracker.is_long(symbol):
            broker.close_long(symbol, tracker.positions[symbol].qty)
        if tracker.is_flat(symbol) and dna.short_enabled:
            if risk.short_exposure_ok(tracker.all_short_value(prices), portfolio_value):
                broker.open_short(symbol, notional_usd, sl, row["close"])
                # open_short converts notional → whole shares internally
                whole_qty = int(notional_usd // row["close"])
                tracker.open_short(symbol, whole_qty, row["close"], sl)

    status = circuit_breaker.update(get_portfolio_value())
    if   status == "DEMOTE": promote_challenger(); break
    elif status == "PAUSE":  sleep_until_next_session()
```
