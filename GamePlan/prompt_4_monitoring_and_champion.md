# Trading Bot — Prompt 4/5: Monitoring, Circuit Breaker & Champion System

## Champion / Challenger Lifecycle

```
Week 0:   evolve_<asset>.py runs → champion DNA saved → bot goes live
             │
             ▼
          Live trading
          BackgroundEvolver running in daemon thread
          CircuitBreaker monitoring every cycle
             │
      ┌──────┴────────────────────┐
      │                           │
  No trips                   Trips at -5% drawdown
  for 1 week                      │
      │                           ▼
      ▼                      Archive champion
  Weekly review:             Promote challenger
  compare live Sharpe        Reset circuit breaker
  vs challenger Sharpe            │
  → promote if better             ▼
                            New champion runs live
                                  │
                                  └── (cycle repeats)
```

---

## Circuit Breaker (`shared/monitoring/circuit_breaker.py`)

```python
from dataclasses import dataclass
from datetime import datetime, date

@dataclass
class CircuitBreaker:
    max_drawdown    : float = 0.05    # 5% from peak → DEMOTE
    max_daily_loss  : float = 0.02    # 2% in one session → PAUSE (no demotion)
    max_short_loss  : float = 0.03    # 3% loss on any single short → force close it

    peak_value      : float = None
    session_start   : float = None
    session_date    : date  = None
    tripped         : bool  = False

    def update(self, portfolio_value: float) -> str:
        """Returns: 'OK' | 'PAUSE' | 'DEMOTE'"""

        # Initialise
        if self.peak_value is None:
            self.peak_value = portfolio_value
        if self.session_date != date.today():
            self.session_start = portfolio_value
            self.session_date  = date.today()

        # Update peak
        self.peak_value = max(self.peak_value, portfolio_value)

        # Check max drawdown from peak → DEMOTE
        drawdown = (self.peak_value - portfolio_value) / self.peak_value
        if drawdown >= self.max_drawdown:
            self.tripped = True
            return "DEMOTE"

        # Check daily loss → PAUSE
        if self.session_start:
            daily_loss = (self.session_start - portfolio_value) / self.session_start
            if daily_loss >= self.max_daily_loss:
                return "PAUSE"

        return "OK"

    def reset(self):
        self.peak_value   = None
        self.session_start = None
        self.tripped      = False


def check_short_positions(tracker, current_prices, max_loss=0.03):
    """
    Separately monitor each open short.
    Force-close any short that has moved against us by more than max_loss.
    Shorts have asymmetric risk (unlimited upside loss) — check them every cycle.
    """
    to_close = []
    for ticker, pos in tracker.positions.items():
        if pos.side != "SHORT":
            continue
        loss_pct = (current_prices[ticker] - pos.entry) / pos.entry
        if loss_pct >= max_loss:
            to_close.append(ticker)
    return to_close   # caller force-closes these immediately
```

---

## Champion Promotion (`shared/monitoring/circuit_breaker.py`)

```python
import shutil, json, os
from datetime import datetime
from dataclasses import asdict

def promote_challenger(asset_class: str):
    """Swap challenger → champion. Archive old champion."""
    champion_path   = f"champions/{asset_class}_champion.json"
    challenger_path = f"challengers/{asset_class}_challenger.json"

    # Archive old champion with timestamp
    if os.path.exists(champion_path):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(champion_path, f"archive/{asset_class}_{ts}.json")

    # Promote
    shutil.copy(challenger_path, champion_path)
    print(f"🏆 [{asset_class}] New champion promoted at {datetime.now()}")


def weekly_review(asset_class: str, evolver: BackgroundEvolver, live_sharpe: float):
    """
    Every 7 days: compare champion's live Sharpe vs challenger's backtest Sharpe.
    Promote proactively if challenger is meaningfully better (> 0.2 delta).
    """
    challenger = evolver.get_challenger()
    if challenger and challenger.last_sharpe > live_sharpe + 0.2:
        print(f"📈 Challenger outperforms champion — promoting proactively.")
        promote_challenger(asset_class)
```

---

## Performance Ledger (`shared/monitoring/ledger.py`)

```python
import csv, os
from dataclasses import dataclass
from datetime import datetime

@dataclass
class BotRecord:
    asset_class    : str
    generation     : int
    promoted_at    : datetime
    demoted_at     : datetime | None = None
    peak_value     : float = 0.0
    final_value    : float = 0.0
    max_drawdown   : float = 0.0
    sharpe_live    : float = 0.0
    total_trades   : int   = 0
    short_trades   : int   = 0
    reason_demoted : str | None = None   # "5% drawdown" | "weekly review" | "manual"

class PerformanceLedger:
    def __init__(self, path="ledger.csv"):
        self.path = path

    def log(self, record: BotRecord):
        write_header = not os.path.exists(self.path)
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(BotRecord.__dataclass_fields__.keys())
            w.writerow([getattr(record, k) for k in BotRecord.__dataclass_fields__])

    def average_lifespan(self, asset_class: str) -> float:
        """Average days a champion survives before demotion."""
        ...

    def demotion_reasons(self) -> dict:
        """Breakdown of why bots got demoted — useful for tuning thresholds."""
        ...
```

---

## Funds Bot (`bots/funds/bot_funds.py`)

Completely different from trading bots. No LSTM. No GA. No circuit breaker. Never sells.

```python
import yfinance as yf

FUND_HOLDINGS = {
    "VYM"  : 0,   # shares owned (starts at 0, bot buys over time)
    "JEPI" : 0,
    "SCHD" : 0,
}

def get_dividend_info(ticker: str) -> dict:
    t    = yf.Ticker(ticker)
    info = t.info
    return {
        "annual_yield"     : info.get("dividendYield", 0) * 100,
        "quarterly_payout" : info.get("dividendRate", 0) / 4,
        "next_div_date"    : info.get("dividendDate"),
        "ex_div_date"      : info.get("exDividendDate"),
        "history"          : t.dividends.tail(8).to_dict(),
    }

class DividendCalendar:
    def __init__(self, holdings: dict):
        self.holdings = holdings   # {ticker: shares_owned}

    def upcoming_payouts(self) -> list[dict]:
        # For each ticker: payout = shares * quarterly_payout
        # Return sorted by next_div_date ascending
        # Fields per entry: fund, shares, payout_usd, next_div_date, annual_yield_pct

class FundBot:
    """
    Strategy: buy on dips, never sell, collect dividends quarterly.
    Dip = price is >3% below its 50-SMA.
    Has its own isolated capital pool — no other bot can access it.
    """
    def __init__(self, pool_value: float):
        self.pool      = pool_value
        self.calendar  = DividendCalendar(FUND_HOLDINGS)

    def run(self):
        while True:
            for ticker in FUND_HOLDINGS:
                price = get_current_price(ticker)
                sma50 = get_sma(ticker, 50)
                dip   = (sma50 - price) / sma50

                if dip > 0.03:                         # >3% below SMA → buy
                    qty = self.pool * 0.05 / price     # deploy 5% of pool per dip
                    broker.buy(ticker, qty)
                    FUND_HOLDINGS[ticker] += qty

            print("📅 Upcoming dividends:")
            for p in self.calendar.upcoming_payouts():
                print(f"  {p['fund']}: €{p['payout_usd']:.2f} on {p['next_div_date']}")

            sleep(86400)   # check once per day
```
