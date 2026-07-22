"""
shared/data/preprocessor.py — Feature Engineering

Builds ML-ready features from raw OHLCV data.
Base features (7) for all bots, +3 politician features for stocks bot.
No lookahead bias — every feature only uses past data.
"""

import numpy as np
import pandas as pd

BASE_FEATURES = [
    "return_1", "return_5", "return_20",   # price momentum
    "rsi",                                  # 14-period RSI
    "volatility",                           # 20-period rolling std of return_1
    "ma_cross",                             # sma_20 / sma_50 - 1
    "vol_ratio",                            # volume / 20-period avg volume
]

STOCKS_FEATURES = BASE_FEATURES + [
    "pol_net_sentiment",   # politician net buy/sell signal (-1.0 to +1.0)
    "pol_signal_age",      # days since most recent politician trade (999 = none)
    "pol_buy_count",       # number of politician buys in last 60 days
]

FEATURES_BY_ASSET = {
    "crypto": BASE_FEATURES,
    "stocks": STOCKS_FEATURES,
    "etf": BASE_FEATURES,
    "politician": BASE_FEATURES,
    "funds": BASE_FEATURES,
}


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Standard Wilder RSI on closing prices."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def engineer_features(df: pd.DataFrame, asset_class: str = None,
                      ticker: str = None, pol_api_key: str = None) -> pd.DataFrame:
    """
    Add feature columns to an OHLCV DataFrame.
    Keeps raw ohlcv + sma_50 columns (needed by fallback strategy and backtest).
    Politician features added for stocks only (Option A, prompt 5).
    """
    df = df.copy()
    close = df["close"]

    df["return_1"] = close.pct_change(1)
    df["return_5"] = close.pct_change(5)
    df["return_20"] = close.pct_change(20)
    df["rsi"] = compute_rsi(close)
    df["volatility"] = df["return_1"].rolling(20).std()

    sma_20 = close.rolling(20).mean()
    sma_50 = close.rolling(50).mean()
    df["sma_20"] = sma_20
    df["sma_50"] = sma_50
    df["ma_cross"] = sma_20 / sma_50 - 1

    vol_avg = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / vol_avg.replace(0, np.nan)

    if asset_class == "stocks" and ticker and pol_api_key:
        # Import here to avoid hard dependency for non-stock bots
        from shared.data.politician_trades import compute_politician_signal
        try:
            pol = compute_politician_signal(ticker, pol_api_key)
        except Exception:
            pol = {"pol_net_sentiment": 0.0, "pol_signal_age": 999, "pol_buy_count": 0}
        df["pol_net_sentiment"] = pol["pol_net_sentiment"]
        df["pol_signal_age"] = pol["pol_signal_age"]
        df["pol_buy_count"] = pol["pol_buy_count"]

    return df.dropna()


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Latest ATR value (Average True Range) — used for stop-loss placement."""
    series = compute_atr_series(df, period)
    return float(series.iloc[-1]) if len(series) else 0.0


def compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Full ATR series over the DataFrame."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)
