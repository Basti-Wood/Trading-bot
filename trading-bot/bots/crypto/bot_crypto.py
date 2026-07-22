"""
bots/crypto/bot_crypto.py — Crypto Trading Bot (24/7)

LSTM + fallback, GA-evolved DNA, shorting allowed (gated by dashboard switch).
Universe is a fixed whitelist of major coins — honeypot scams are ignored.
"""

import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.trading_bot import TradingBot
from shared.data.fetcher import CRYPTO_SYMBOLS


def main() -> None:
    bot = TradingBot(asset_class="crypto", symbols=CRYPTO_SYMBOLS)
    bot.run()   # crypto trades 24/7 — no market-hours gate


if __name__ == "__main__":
    main()
