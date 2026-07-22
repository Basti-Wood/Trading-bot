"""
shared/strategy/fallback.py — RSI + Trend Fallback Strategy

Activates when ML confidence < confidence_threshold.
BUY  when RSI < 35 AND close > sma_50   (oversold in uptrend)
SELL when RSI > 65 AND close < sma_50   (overbought in downtrend)
Same (signal, confidence) interface as MLPredictor.
"""


def fallback_signal(row: dict) -> tuple:
    """
    `row` must contain: rsi, close, sma_50.
    Returns (signal, confidence).
    """
    rsi = float(row.get("rsi", 50.0))
    close = float(row.get("close", 0.0))
    sma_50 = float(row.get("sma_50", 0.0))

    if rsi < 35 and close > sma_50:
        # deeper oversold -> more confident
        confidence = min(1.0, 0.55 + (35 - rsi) / 35 * 0.45)
        return "BUY", round(confidence, 3)

    if rsi > 65 and close < sma_50:
        confidence = min(1.0, 0.55 + (rsi - 65) / 35 * 0.45)
        return "SELL", round(confidence, 3)

    return "HOLD", 0.0


class FallbackStrategy:
    """Class wrapper matching the MLPredictor interface."""

    def predict(self, row: dict) -> tuple:
        return fallback_signal(row)
