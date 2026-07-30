"""
bots/stocks/bot_stocks.py — Stocks Trading Bot (09:30–16:00 ET)

LSTM (with 3 politician features) + fallback, GA-evolved DNA, shorting allowed
(gated by dashboard switch). Sleeps outside market hours.
"""

import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.trading_bot import TradingBot
from shared.data.fetcher import STOCK_SYMBOLS

ET = ZoneInfo("America/New_York")


def market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:   # Sat/Sun
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


class StocksBot(TradingBot):
    def run(self) -> None:
        print("[stocks] Bot starting (market hours 09:30-16:00 ET).")
        warned_closed = False
        while True:
            if market_open():
                warned_closed = False
                try:
                    self._cycle()
                except Exception as exc:
                    print(f"[stocks] cycle error: {exc}")
                time.sleep(60)
            else:
                if not warned_closed:
                    print("[stocks] Market is closed — waiting for 09:30-16:00 ET.")
                    warned_closed = True
                time.sleep(300)   # check every 5 min while closed


def main() -> None:
    StocksBot(asset_class="stocks", symbols=STOCK_SYMBOLS).run()


if __name__ == "__main__":
    main()
