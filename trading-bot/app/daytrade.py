"""Classic intraday setups scored on completed five-minute regular-session bars.

Four checks per direction, each a widely used day-trading confirmation:
VWAP side (institutional fair price), EMA 9/20 momentum, a breakout of the
15-minute opening range, and a relative-volume push in the bar's direction.
Three of four make the setup's vote; RSI blocks chasing exhausted moves.
"""
import pandas as pd

OPENING_BARS = 3          # 9:30-9:45 opening range
ATR_STOP_MULTIPLE = 2.    # bracket stop distance in five-minute ATRs
MIN_STOP = .003           # never tighter than normal bid/ask noise


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    loss = -delta.clip(upper=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return float(100 * gain / (gain + loss)) if gain + loss > 0 else 50.


def setup(bars):
    local = bars.index.tz_convert("America/New_York")
    session = bars[local.date == local[-1].date()]
    last = session.iloc[-1]
    close = float(last.close)
    typical = (session.high + session.low + session.close) / 3
    volume = float(session.volume.sum())
    vwap = float((typical * session.volume).sum() / volume) if volume > 0 else float(typical.mean())
    fast = float(bars.close.ewm(span=9, adjust=False).mean().iloc[-1])
    slow = float(bars.close.ewm(span=20, adjust=False).mean().iloc[-1])
    prior = float(bars.volume.iloc[-21:-1].mean())
    relative_volume = float(last.volume) / prior if prior > 0 else 1.
    previous = bars.close.shift()
    true_range = pd.concat([bars.high - bars.low, (bars.high - previous).abs(), (bars.low - previous).abs()], axis=1).max(axis=1)
    atr = float(true_range.tail(14).mean()) / close
    strength = rsi(bars.close)
    # The range is only tradable once it is complete.
    ranged = len(session) > OPENING_BARS
    range_high = float(session.high.iloc[:OPENING_BARS].max())
    range_low = float(session.low.iloc[:OPENING_BARS].min())
    pushing = relative_volume >= 1.5
    bullish = {"above_vwap": close > vwap, "ema_9_above_20": fast > slow,
               "opening_range_breakout": ranged and close > range_high,
               "volume_push_up": pushing and close > float(last.open)}
    bearish = {"below_vwap": close < vwap, "ema_9_below_20": fast < slow,
               "opening_range_breakdown": ranged and close < range_low,
               "volume_push_down": pushing and close < float(last.open)}
    ups, downs = sum(bullish.values()), sum(bearish.values())
    if ups >= 3 and strength < 75:
        signal = "BUY"
    elif downs >= 3 and strength > 25:
        signal = "SELL"
    else:
        signal = "HOLD"
    return {"signal": signal, "bullish_checks": ups, "bearish_checks": downs,
            "checks": {k: bool(v) for k, v in {**bullish, **bearish}.items()},
            "vwap": vwap, "ema_fast": fast, "ema_slow": slow, "rsi": strength,
            "opening_high": range_high, "opening_low": range_low,
            "relative_volume": relative_volume, "atr": atr}


def bracket_stop(config, forecast):
    """Volatility-sized stop: wide enough for normal noise, never beyond the configured cap."""
    atr = (forecast.get("setup") or {}).get("atr")
    if not isinstance(atr, (int, float)) or not atr > 0:
        return config.daytrade_stop_loss
    return min(config.daytrade_stop_loss, max(MIN_STOP, ATR_STOP_MULTIPLE * atr))
