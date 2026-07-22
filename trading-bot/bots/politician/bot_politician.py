"""
bots/politician/bot_politician.py — Politician Signal Bot (Option B, Prompt 5)

Rule-based only — no LSTM, no GA. Trades purely on STOCK Act disclosure
signals from Quiver Quantitative. Own isolated pool (5% of capital).
Subject to circuit breaker; shorting gated by dashboard kill switch.
"""

import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.data.fetcher import (
    POLITICIAN_WATCHLIST, POOL_CONFIG, get_recent_candles, get_current_price)
from shared.data.preprocessor import compute_atr
from shared.data.politician_trades import (
    compute_politician_signal, is_strong_buy, is_strong_sell, is_stale)
from shared.risk.position_tracker import PositionTracker
from shared.monitoring.circuit_breaker import CircuitBreaker, check_short_positions
from shared.monitoring import controls as ctl
import csv

from shared.broker import alpaca as broker

ET = ZoneInfo("America/New_York")
CHECK_SECONDS = 3600   # politician disclosures move slowly — hourly is plenty
POSITION_FRACTION = 0.10   # of the pool per position, scaled by |sentiment|
SHORT_STOP_PCT = 0.03      # 3% stop above entry for shorts
LONG_STOP_ATR = 2.0


def market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


class PoliticianBot:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.tracker = PositionTracker()
        self.breaker = CircuitBreaker(max_drawdown=0.05)
        self.pool_fraction = POOL_CONFIG["politician"]["allocation"]
        self._warned_no_broker = False
        self._trades_path = os.path.join(
            os.environ.get("LOGS_DIR", "logs"), "trades_politician.csv")

    def _pool_value(self) -> float:
        return broker.get_portfolio_value() * self.pool_fraction

    def _log_trade(self, symbol: str, side: str, price: float,
                   notional_usd: float = 0.0, qty: float = 0.0) -> None:
        os.makedirs(os.path.dirname(self._trades_path) or ".", exist_ok=True)
        write_header = not os.path.exists(self._trades_path)
        with open(self._trades_path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["timestamp", "asset_class", "symbol", "side",
                            "price", "notional_usd", "qty", "confidence"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"),
                "politician", symbol, side,
                round(price, 4), round(notional_usd, 2), round(qty, 6), 0.0,
            ])

    def _buy(self, ticker: str, strength: float, price: float, atr: float) -> None:
        notional = self._pool_value() * POSITION_FRACTION * min(1.0, strength)
        sl = price - atr * LONG_STOP_ATR
        if broker.open_long(ticker, notional, sl, price):
            self.tracker.open_long(ticker, notional, price, sl,
                                   fractional=broker.is_fractionable(ticker))
            self._log_trade(ticker, "LONG", price, notional_usd=notional)
            print(f"[politician] LONG {ticker} ${notional:.2f} @ {price:.2f}")

    def _short(self, ticker: str, strength: float, price: float) -> None:
        notional = self._pool_value() * POSITION_FRACTION * min(1.0, strength)
        sl = price * (1 + SHORT_STOP_PCT)
        qty = broker.open_short(ticker, notional, sl, price)
        if qty >= 1:
            self.tracker.open_short(ticker, qty, price, sl)
            self._log_trade(ticker, "SHORT", price, notional_usd=notional, qty=qty)
            print(f"[politician] SHORT {ticker} {qty} sh @ {price:.2f}")
        else:
            print(f"[politician] short {ticker} skipped - rounds to 0 shares")

    def _close(self, ticker: str) -> None:
        pos = self.tracker.positions.get(ticker)
        if not pos:
            return
        # Get current price for the log (best-effort)
        close_price = get_current_price(ticker)
        side = f"CLOSE_{pos.side}" if pos.side in ("LONG", "SHORT") else "CLOSE"
        if pos.side == "LONG":
            closed = broker.close_long(ticker, pos.qty)
        else:
            closed = broker.close_short(ticker, pos.qty)
        if not closed:
            print(f"[politician] close {ticker} failed; position retained")
            return
        self._log_trade(ticker, side, close_price, qty=pos.qty)
        self.tracker.close(ticker)
        print(f"[politician] closed {pos.side} {ticker}")

    def run(self) -> None:
        print("[politician] Bot starting (hourly, rule-based).")
        while True:
            try:
                if market_open():
                    self._cycle()
            except Exception as exc:
                print(f"[politician] cycle error: {exc}")
            time.sleep(CHECK_SECONDS)

    def _cycle(self) -> None:
        if not broker.is_configured():
            if not self._warned_no_broker:
                print("[politician] Alpaca credentials not configured. "
                      "Bot is idling until .env is set.")
                self._warned_no_broker = True
            return
        self._warned_no_broker = False

        controls = ctl.load_controls()
        if not ctl.trading_allowed("politician", controls):
            return
        if not ctl.strategy_enabled("politician", "politician", controls):
            return

        prices: dict = {}
        for ticker in POLITICIAN_WATCHLIST:
            sig = compute_politician_signal(ticker, self.api_key)
            df = get_recent_candles(ticker, days=60)
            if df.empty:
                continue
            price = float(df["close"].iloc[-1])
            prices[ticker] = price
            atr = compute_atr(df)

            if is_strong_buy(sig):
                if self.tracker.is_short(ticker):
                    self._close(ticker)
                if self.tracker.is_flat(ticker):
                    self._buy(ticker, sig["pol_net_sentiment"], price, atr)

            elif is_strong_sell(sig):
                if self.tracker.is_long(ticker):
                    self._close(ticker)
                if (self.tracker.is_flat(ticker)
                        and ctl.shorting_allowed("politician", controls)):
                    self._short(ticker, abs(sig["pol_net_sentiment"]), price)

            elif is_stale(sig) and not self.tracker.is_flat(ticker):
                self._close(ticker)   # stale signal -> exit

        # Short safety: force-close >3% losers every cycle
        for ticker in check_short_positions(self.tracker, prices):
            print(f"[politician] FORCE-CLOSING short {ticker} (>3% loss)")
            self._close(ticker)

        # Kill switch: flatten shorts if shorting was disabled from dashboard
        if not ctl.shorting_allowed("politician", controls):
            for ticker in [t for t, p in self.tracker.positions.items()
                           if p.side == "SHORT"]:
                print(f"[politician] Kill switch - closing short {ticker}")
                self._close(ticker)

        status = self.breaker.update(broker.get_portfolio_value())
        if status == "DEMOTE":
            print("[politician] Circuit breaker tripped - closing all positions.")
            for ticker in list(self.tracker.positions):
                self._close(ticker)
            self.breaker.reset()


def main() -> None:
    api_key = os.environ.get("QUIVER_API_KEY", "")
    if not api_key:
        print("[politician] QUIVER_API_KEY missing - bot idle.")
        while True:
            time.sleep(3600)
    PoliticianBot(api_key).run()


if __name__ == "__main__":
    main()
