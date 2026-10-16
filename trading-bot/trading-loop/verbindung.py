"""Verbindungstest: Paper-Konto abrufen und die letzten 5 Tageskerzen von SPY laden.

  python verbindung.py

Gibt keine Schlüssel aus.
"""
import datetime as dt
import os
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

ORDNER = Path(__file__).resolve().parent


def main():
    load_dotenv(ORDNER / ".env")
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Alpaca-Schlüssel fehlen in .env (ALPACA_API_KEY, ALPACA_SECRET_KEY).")

    try:
        konto = TradingClient(key, secret, paper=True).get_account()
    except Exception as fehler:
        raise SystemExit(f"Paper-Konto nicht erreichbar: {fehler}. Sind es Schlüssel aus dem Paper-Konto?")
    print(f"Paper-Konto: Status {konto.status.value}, Kontowert {float(konto.equity):,.2f} USD")

    ende = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)
    bars = StockHistoricalDataClient(key, secret).get_stock_bars(StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame.Day, start=ende - dt.timedelta(days=12), end=ende,
        feed=DataFeed.IEX)).df.tail(5)
    print("SPY, letzte 5 Tageskerzen (Schlusskurs):")
    for (_, zeit), zeile in bars.iterrows():
        print(f"  {zeit:%d.%m.%Y}  {zeile['close']:.2f}")


if __name__ == "__main__":
    main()
