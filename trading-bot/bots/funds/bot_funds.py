"""
bots/funds/bot_funds.py — Dividend Fund Bot (Prompt 4)

Completely different from trading bots: no LSTM, no GA, no circuit breaker.
NEVER sells — buys dips (>3% below 50-SMA), collects dividends quarterly.
Isolated capital pool (40%). Shorting is impossible by design.
"""

import os
import sys
import time
from datetime import datetime

import yfinance as yf

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.data.fetcher import FUND_HOLDINGS, POOL_CONFIG, get_current_price, get_sma
from shared.monitoring import controls as ctl
import csv

from shared.broker import alpaca as broker

DIP_THRESHOLD = 0.03      # >3% below 50-SMA -> buy
DEPLOY_FRACTION = 0.05    # deploy 5% of pool per dip
CHECK_SECONDS = 86400     # once per day


def get_dividend_info(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info
    return {
        "annual_yield": (info.get("dividendYield") or 0) * 100,
        "quarterly_payout": (info.get("dividendRate") or 0) / 4,
        "next_div_date": info.get("dividendDate"),
        "ex_div_date": info.get("exDividendDate"),
        "history": t.dividends.tail(8).to_dict(),
    }


class DividendCalendar:
    def __init__(self, holdings: dict):
        self.holdings = holdings   # {ticker: shares_owned}

    def upcoming_payouts(self) -> list:
        payouts = []
        for ticker, shares in self.holdings.items():
            if shares <= 0:
                continue
            try:
                info = get_dividend_info(ticker)
            except Exception:
                continue
            payouts.append({
                "fund": ticker,
                "shares": shares,
                "payout_usd": round(shares * info["quarterly_payout"], 2),
                "next_div_date": info["next_div_date"],
                "annual_yield_pct": round(info["annual_yield"], 2),
            })
        return sorted(payouts, key=lambda p: p["next_div_date"] or 0)


class FundBot:
    """Buy on dips, never sell, collect dividends quarterly."""

    def __init__(self):
        self.holdings = dict(FUND_HOLDINGS)
        self.calendar = DividendCalendar(self.holdings)
        self.pool_fraction = POOL_CONFIG["funds"]["allocation"]
        self._warned_no_broker = False
        self._trades_path = os.path.join(
            os.environ.get("LOGS_DIR", "logs"), "trades_funds.csv")

    def _pool_value(self) -> float:
        return broker.get_portfolio_value() * self.pool_fraction

    def run(self) -> None:
        print("[funds] Bot starting (daily dip-buyer, never sells).")
        while True:
            try:
                self._cycle()
            except Exception as exc:
                print(f"[funds] cycle error: {exc}")
            time.sleep(CHECK_SECONDS)

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
                "funds", symbol, side,
                round(price, 4), round(notional_usd, 2), round(qty, 6), 0.0,
            ])

    def _cycle(self) -> None:
        if not broker.is_configured():
            if not self._warned_no_broker:
                print("[funds] Alpaca credentials not configured. "
                      "Bot is idling until .env is set.")
                self._warned_no_broker = True
            return
        self._warned_no_broker = False

        if not ctl.trading_allowed("funds"):
            return

        pool = self._pool_value()
        for ticker in self.holdings:
            price = get_current_price(ticker)
            sma50 = get_sma(ticker, 50)
            if price <= 0 or sma50 <= 0:
                continue
            dip = (sma50 - price) / sma50

            if dip > DIP_THRESHOLD:
                notional = pool * DEPLOY_FRACTION
                # Funds never sell — stop placed at 50% of price is effectively
                # disabled while satisfying the broker layer's interface.
                if broker.open_long(ticker, notional, price * 0.5, price):
                    qty = notional / price
                    self.holdings[ticker] += qty
                    self._log_trade(ticker, "BUY", price, notional_usd=notional, qty=qty)
                    print(f"[funds] Dip buy {ticker}: ${notional:.2f} "
                          f"({dip * 100:.1f}% below SMA50)")

        print("[funds] Upcoming dividends:")
        for p in self.calendar.upcoming_payouts():
            print(f"  {p['fund']}: ${p['payout_usd']:.2f} on {p['next_div_date']}")


def main() -> None:
    FundBot().run()


if __name__ == "__main__":
    main()
