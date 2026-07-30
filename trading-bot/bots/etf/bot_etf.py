"""
bots/etf/bot_etf.py — ETF Trading Bot (09:30–16:00 ET)

LSTM + fallback, GA-evolved DNA, shorting allowed (gated by dashboard switch).
"""

import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.trading_bot import TradingBot
from shared.data.fetcher import ETF_SYMBOLS

ET = ZoneInfo("America/New_York")


def market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


class ETFBot(TradingBot):
    def run(self) -> None:
        print("[etf] Bot starting (market hours 09:30-16:00 ET).")
        warned_closed = False
        while True:
            if market_open():
                warned_closed = False
                try:
                    self._cycle()
                except Exception as exc:
                    print(f"[etf] cycle error: {exc}")
                time.sleep(60)
            else:
                if not warned_closed:
                    print("[etf] Market is closed — waiting for 09:30-16:00 ET.")
                    warned_closed = True
                time.sleep(300)


def main() -> None:
    ETFBot(asset_class="etf", symbols=ETF_SYMBOLS).run()


if __name__ == "__main__":
    main()
