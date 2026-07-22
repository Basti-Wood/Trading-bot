"""
shared/monitoring/circuit_breaker.py — Circuit Breaker & Champion Promotion

Monitors live portfolio value every cycle:
  - 5% drawdown from peak      -> DEMOTE champion (promote challenger)
  - 2% loss in one session     -> PAUSE (no demotion)
  - 3% loss on a single short  -> force-close that short immediately
"""

import shutil
import json
import os
from dataclasses import dataclass
from datetime import datetime, date

CHAMPIONS_DIR = os.environ.get("CHAMPIONS_DIR", "champions")
CHALLENGERS_DIR = os.environ.get("CHALLENGERS_DIR", "challengers")
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "archive")


@dataclass
class CircuitBreaker:
    max_drawdown: float = 0.05    # 5% from peak -> DEMOTE
    max_daily_loss: float = 0.02  # 2% in one session -> PAUSE (no demotion)
    max_short_loss: float = 0.03  # 3% loss on any single short -> force close it

    peak_value: float = None
    session_start: float = None
    session_date: date = None
    tripped: bool = False

    def update(self, portfolio_value: float) -> str:
        """Returns: 'OK' | 'PAUSE' | 'DEMOTE'"""
        if portfolio_value is None or portfolio_value <= 0:
            return "OK"

        # Initialise
        if self.peak_value is None:
            self.peak_value = portfolio_value
        if self.session_date != date.today():
            self.session_start = portfolio_value
            self.session_date = date.today()

        # Update peak
        self.peak_value = max(self.peak_value, portfolio_value)

        # Check max drawdown from peak -> DEMOTE
        drawdown = (self.peak_value - portfolio_value) / self.peak_value
        if drawdown >= self.max_drawdown:
            self.tripped = True
            return "DEMOTE"

        # Check daily loss -> PAUSE
        if self.session_start:
            daily_loss = (self.session_start - portfolio_value) / self.session_start
            if daily_loss >= self.max_daily_loss:
                return "PAUSE"

        return "OK"

    def reset(self) -> None:
        self.peak_value = None
        self.session_start = None
        self.session_date = None
        self.tripped = False


def check_short_positions(tracker, current_prices: dict,
                          max_loss: float = 0.03) -> list:
    """
    Separately monitor each open short.
    Returns tickers of shorts that moved against us by more than max_loss —
    caller force-closes these IMMEDIATELY. Shorts have asymmetric risk
    (unlimited upside loss) so they are checked every cycle.
    """
    to_close = []
    for ticker, pos in tracker.positions.items():
        if pos.side != "SHORT":
            continue
        price = current_prices.get(ticker)
        if price is None:
            continue
        loss_pct = (price - pos.entry) / pos.entry
        if loss_pct >= max_loss:
            to_close.append(ticker)
    return to_close


def promote_challenger(asset_class: str) -> bool:
    """Swap challenger -> champion. Archive old champion. Returns success."""
    champion_path = os.path.join(CHAMPIONS_DIR, f"{asset_class}_champion.json")
    challenger_path = os.path.join(CHALLENGERS_DIR, f"{asset_class}_challenger.json")

    if not os.path.exists(challenger_path):
        print(f"[{asset_class}] No challenger available to promote.")
        return False

    # Archive old champion with timestamp
    if os.path.exists(champion_path):
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(champion_path, os.path.join(ARCHIVE_DIR, f"{asset_class}_{ts}.json"))

    os.makedirs(CHAMPIONS_DIR, exist_ok=True)
    shutil.copy(challenger_path, champion_path)
    print(f"[{asset_class}] New champion promoted at {datetime.now()}")
    return True


def weekly_review(asset_class: str, evolver, live_sharpe: float) -> bool:
    """
    Every 7 days: compare champion's live Sharpe vs challenger's backtest Sharpe.
    Promote proactively if challenger is meaningfully better (> 0.2 delta).
    Respects the dashboard auto_promote switch.
    """
    from shared.monitoring.controls import auto_promote_allowed
    challenger = evolver.get_challenger()
    if challenger and challenger.last_sharpe > live_sharpe + 0.2:
        if not auto_promote_allowed(asset_class):
            print(f"[{asset_class}] Challenger outperforms champion, but "
                  f"auto-promote is disabled — waiting for manual promotion.")
            return False
        print(f"[{asset_class}] Challenger outperforms champion — promoting proactively.")
        return promote_challenger(asset_class)
    return False
