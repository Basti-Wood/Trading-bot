"""
shared/backtest/engine.py — Fast Rule-Based Backtest (GA Phase 1)

No LSTM, no GPU — DNA params drive RSI + MA-cross rules directly.
Simulates transaction cost of 0.001 per trade.
No lookahead bias: signals at bar i are executed using bar i close,
and only data up to bar i is ever used.
"""

import numpy as np
import pandas as pd

from shared.evolution.dna import AgentDNA
from shared.data.preprocessor import compute_rsi, compute_atr_series

TRANSACTION_COST = 0.001


def _precompute(df: pd.DataFrame) -> pd.DataFrame:
    """Compute indicator columns once (cached on the DataFrame object)."""
    if "_bt_ready" in df.attrs:
        return df.attrs["_bt_ready"]
    out = pd.DataFrame(index=df.index)
    close = df["close"].astype(float)
    out["close"] = close
    out["rsi"] = compute_rsi(close)
    out["sma_50"] = close.rolling(50).mean()
    out["atr"] = compute_atr_series(df)
    out["ret"] = close.pct_change()
    out = out.dropna()
    df.attrs["_bt_ready"] = out
    return out


def run_backtest(dna: AgentDNA, historical_data: pd.DataFrame) -> list:
    """
    Fast rule-based backtest — CPU only.
    Uses dna.temperature to derive thresholds/sizing/stops.
    Uses dna.short_enabled / max_short_exposure / short_hold_days for shorts.
    Returns list of daily % returns — never uses future data.
    """
    if historical_data is None or len(historical_data) < 60:
        return []

    # If multiple symbols are concatenated, backtest each and pool the returns
    if "symbol" in historical_data.columns and historical_data["symbol"].nunique() > 1:
        all_returns: list = []
        for _, sub in historical_data.groupby("symbol", sort=False):
            all_returns.extend(run_backtest(dna, sub))
        return all_returns

    data = _precompute(historical_data)
    if len(data) < 20:
        return []

    # Temperature-derived params (prompt 2 formulas)
    confidence_threshold = 0.80 - (dna.temperature * 0.28)
    position_size_mult = 0.5 + dna.temperature
    atr_multiplier = 3.0 - (dna.temperature * 2.0)

    # RSI thresholds tighten as required confidence rises
    rsi_buy = 35.0 - (confidence_threshold - 0.52) * 25    # more passive -> deeper oversold
    rsi_sell = 65.0 + (confidence_threshold - 0.52) * 25

    exposure = dna.max_risk * position_size_mult           # fraction of equity per trade
    exposure = min(exposure, 0.10)

    close = data["close"].to_numpy()
    rsi = data["rsi"].to_numpy()
    sma50 = data["sma_50"].to_numpy()
    atr = data["atr"].to_numpy()
    rets = data["ret"].to_numpy()

    position = 0          # +1 long, -1 short, 0 flat
    entry_price = 0.0
    stop = 0.0
    bars_in_short = 0
    daily_returns = []

    for i in range(1, len(close)):
        # Mark-to-market return for the bar we just lived through
        pnl = rets[i] * exposure * position
        # --- exits ---
        if position == 1 and close[i] <= stop:
            pnl -= TRANSACTION_COST * exposure
            position = 0
        elif position == -1:
            bars_in_short += 1
            forced = close[i] >= stop or bars_in_short >= dna.short_hold_days
            if forced:
                pnl -= TRANSACTION_COST * exposure
                position = 0
                bars_in_short = 0

        # --- entries / flips (signal from data up to bar i only) ---
        buy_sig = rsi[i] < rsi_buy and close[i] > sma50[i]
        sell_sig = rsi[i] > rsi_sell and close[i] < sma50[i]

        if buy_sig and position <= 0:
            if position == -1:
                pnl -= TRANSACTION_COST * exposure
                bars_in_short = 0
            position = 1
            entry_price = close[i]
            stop = entry_price - atr[i] * atr_multiplier
            pnl -= TRANSACTION_COST * exposure
        elif sell_sig and position >= 0:
            if position == 1:
                pnl -= TRANSACTION_COST * exposure
            if dna.short_enabled:
                position = -1
                entry_price = close[i]
                # Shorts get 40% tighter stops (prompt 2)
                stop = entry_price + atr[i] * atr_multiplier * 0.6
                bars_in_short = 0
                pnl -= TRANSACTION_COST * exposure
            else:
                position = 0

        daily_returns.append(pnl)

    return daily_returns


def fitness(dna: AgentDNA, historical_data) -> float:
    """
    Sharpe ratio fitness — never raw profit.
    `historical_data` may be a DataFrame or a dict[symbol, DataFrame].
    """
    if isinstance(historical_data, dict):
        returns: list = []
        for df in historical_data.values():
            returns.extend(run_backtest(dna, df))
    else:
        returns = run_backtest(dna, historical_data)

    if len(returns) == 0:
        return -999.0
    arr = np.asarray(returns, dtype=float)
    std = arr.std()
    if std == 0 or np.isnan(std):
        return -999.0
    sharpe = arr.mean() / std * np.sqrt(252)
    dna.last_sharpe = float(sharpe)
    return float(sharpe)
