"""
shared/data/fetcher.py — Market Data Fetcher

Downloads historical OHLCV data from yfinance.
Pre-loads everything into RAM at startup (80GB RAM available).
"""

import time
import pandas as pd
import yfinance as yf
from typing import List, Union

# ---------------------------------------------------------------------------
# Symbol lists per asset class
# ---------------------------------------------------------------------------
# NOTE: Only established, exchange-listed assets. Honeypot scams are ignored —
# the crypto universe is a fixed whitelist of majors, never dynamically
# expanded from unvetted token lists.

CRYPTO_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD",
    "MATIC-USD", "LINK-USD", "DOT-USD", "UNI-USD", "ATOM-USD",
]

STOCK_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD",
    "DIS", "ADBE", "NFLX", "CRM", "AMD", "INTC", "QCOM",
    "TXN", "AVGO", "COST", "PEP", "KO", "ABT", "MRK",
]

ETF_SYMBOLS = [
    "SPY", "QQQ", "VTI", "IWM", "DIA", "XLF", "XLE", "XLK",
    "XLV", "XLI", "XLU", "XLP", "XLY", "XLB", "TLT",
]

POLITICIAN_WATCHLIST = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AMD"]

FUND_HOLDINGS = {"VYM": 0, "JEPI": 0, "SCHD": 0}

ASSET_CLASS_CONFIG = {
    "crypto":     {"symbols": CRYPTO_SYMBOLS,       "shorting": True,  "data_source": "yfinance"},
    "stocks":     {"symbols": STOCK_SYMBOLS,        "shorting": True,  "data_source": "yfinance"},
    "etf":        {"symbols": ETF_SYMBOLS,          "shorting": True,  "data_source": "yfinance"},
    "politician": {"symbols": POLITICIAN_WATCHLIST, "shorting": True,  "data_source": "yfinance"},
    "funds":      {"symbols": list(FUND_HOLDINGS.keys()), "shorting": False, "data_source": "yfinance"},
}

POOL_CONFIG = {
    "funds":      {"allocation": 0.40, "shorting": False, "isolated": True},
    "etf":        {"allocation": 0.25, "shorting": True,  "max_short_exposure": 0.3},
    "stocks":     {"allocation": 0.20, "shorting": True,  "max_short_exposure": 0.5},
    "crypto":     {"allocation": 0.10, "shorting": True,  "max_short_exposure": 0.4},
    "politician": {"allocation": 0.05, "shorting": True,  "max_short_exposure": 0.5},
}

SHORTING_ALLOWED = {
    "crypto": True, "stocks": True, "etf": True,
    "politician": True, "funds": False,
}

# In-memory cache: {(symbol, interval): DataFrame} — zero disk I/O during GA
_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _download(symbol: str, years: int, interval: str) -> pd.DataFrame:
    """Download one symbol from yfinance with basic retry."""
    key = (symbol, interval, years)
    if key in _CACHE:
        return _CACHE[key]

    for attempt in range(3):
        try:
            df = yf.download(
                symbol,
                period=f"{years}y",
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if df is None or df.empty:
                raise ValueError(f"empty data for {symbol}")
            # yfinance may return MultiIndex columns for single symbol
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            df = df.dropna()
            df["symbol"] = symbol
            _CACHE[key] = df
            return df
        except Exception as exc:  # network hiccups — retry with backoff
            if attempt == 2:
                print(f"[fetcher] FAILED {symbol}: {exc}")
                return pd.DataFrame()
            time.sleep(2 * (attempt + 1))
    return pd.DataFrame()


def fetch_historical(symbols: Union[str, List[str]], years: int = 3,
                     interval: str = "1d") -> pd.DataFrame:
    """
    Fetch OHLCV data for given symbols.
    Returns a single DataFrame with columns open,high,low,close,volume,symbol
    (concatenated, chronological per symbol — never shuffled).
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    frames = [_download(s, years, interval) for s in symbols]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "symbol"])
    return pd.concat(frames).sort_index(kind="stable")


def fetch_crypto(symbols: List[str] = None, years: int = 3) -> pd.DataFrame:
    """Fetch crypto OHLCV. Fixed whitelist only — honeypot scams are ignored."""
    return fetch_historical(symbols or CRYPTO_SYMBOLS, years)


def fetch_stocks(symbols: List[str] = None, years: int = 3) -> pd.DataFrame:
    return fetch_historical(symbols or STOCK_SYMBOLS, years)


def fetch_etfs(symbols: List[str] = None, years: int = 3) -> pd.DataFrame:
    return fetch_historical(symbols or ETF_SYMBOLS, years)


def fetch_politician_stocks(symbols: List[str] = None, years: int = 3) -> pd.DataFrame:
    return fetch_historical(symbols or POLITICIAN_WATCHLIST, years)


def fetch_funds(symbols: List[str] = None, years: int = 3) -> pd.DataFrame:
    return fetch_historical(symbols or list(FUND_HOLDINGS.keys()), years)


# ---------------------------------------------------------------------------
# Live helpers
# ---------------------------------------------------------------------------

def get_current_price(symbol: str) -> float:
    """Latest closing price (near-real-time via yfinance fast_info)."""
    try:
        price = yf.Ticker(symbol).fast_info["last_price"]
        return float(price)
    except Exception:
        df = _download(symbol, 1, "1d")
        return float(df["close"].iloc[-1]) if not df.empty else 0.0


def get_sma(symbol: str, period: int = 50) -> float:
    """Latest SMA value over `period` days."""
    df = yf.download(symbol, period=f"{max(period * 2, 100)}d",
                     interval="1d", auto_adjust=True, progress=False)
    if df is None or df.empty:
        return 0.0
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df.rename(columns=str.lower)["close"].dropna()
    if len(close) < period:
        return float(close.mean())
    return float(close.rolling(period).mean().iloc[-1])


def get_recent_candles(symbol: str, days: int = 120) -> pd.DataFrame:
    """Recent daily OHLCV for live feature engineering / rolling buffers."""
    df = yf.download(symbol, period=f"{days}d", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    df["symbol"] = symbol
    return df


def preload_all_data(years: int = 3) -> dict:
    """
    Pre-load ALL historical data into RAM (evolver startup).
    Returns dict[asset_class, dict[symbol, DataFrame]].
    """
    result = {}
    for asset_class, cfg in ASSET_CLASS_CONFIG.items():
        per_symbol = {}
        for sym in cfg["symbols"]:
            df = _download(sym, years, "1d")
            if not df.empty:
                per_symbol[sym] = df
        result[asset_class] = per_symbol
        print(f"[fetcher] {asset_class}: {len(per_symbol)} symbols loaded")
    return result
