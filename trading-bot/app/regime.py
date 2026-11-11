"""Market posture: how much of its budget a bot may have invested, from trend and breadth.

A light version of the "exposure-coach" and "uptrend-analyzer" skills (claude-trading-skills,
MIT licence, TraderMonty), using only daily candles the bot already loads:
- Trend of a benchmark (Alpaca: SPY; Trading 212: TRADING212_BENCHMARK): close and 50-day average
  against the 200-day average.
- Breadth: share of the bot's own stocks trading above their 50-day average.

Angriff (uptrend, broad participation): the full "Höchstens investiert" applies.
Vorsicht (mixed): REGIME_CAUTION_SHARE of it. Defensive (downtrend, narrow): REGIME_DEFENSIVE_SHARE.
The posture only limits new buys; open positions keep their own exits and stops.
"""
import math

import pandas as pd

from .store import utcnow

MAX_AGE_DAYS = 4
NAMES = {"angriff": "Angriff", "vorsicht": "Vorsicht", "defensive": "Defensive"}


def closes(frame):
    if frame is None or not len(frame):
        return None
    column = "close" if "close" in frame else "Close" if "Close" in frame else None
    return frame[column].astype(float).dropna() if column else None


def breadth(frames):
    above = total = 0
    for frame in frames.values():
        close = closes(frame)
        if close is None or len(close) < 50:
            continue
        total += 1
        above += bool(close.iloc[-1] > close.iloc[-50:].mean())
    return (above / total, total) if total >= 5 else (None, total)


def posture(config, benchmark, frames, name=""):
    """{"state", "ceiling", "reasons", ...} from the benchmark's candles and the bot's stock candles."""
    reasons = []
    trend = "unbekannt"
    close = closes(benchmark)
    if close is not None and len(close) >= 200:
        last, sma50, sma200 = float(close.iloc[-1]), float(close.iloc[-50:].mean()), float(close.iloc[-200:].mean())
        if last > sma200 and sma50 > sma200:
            trend = "aufwärts"
        elif last < sma200 and sma50 < sma200:
            trend = "abwärts"
        else:
            trend = "seitwärts"
        reasons.append(f"{name or 'Index'}: Kurs {last / sma200 - 1:+.1%} zum 200-Tage-Schnitt, Trend {trend}")
    else:
        reasons.append(f"{name or 'Index'}: zu wenig Kerzen für den 200-Tage-Schnitt")
    share, counted = breadth(frames)
    if share is not None:
        reasons.append(f"Breite: {share:.0%} von {counted} Aktien über ihrem 50-Tage-Schnitt")
    if trend == "aufwärts" and (share is None or share >= .5):
        state = "angriff"
    elif trend == "abwärts" and (share is None or share < .4):
        state = "defensive"
    elif trend == "unbekannt" and share is None:
        state = "angriff"  # No data: no restriction, but say so.
        reasons.append("ohne Daten keine Einschränkung")
    else:
        state = "vorsicht"
    ceiling = {"angriff": 1., "vorsicht": config.regime_caution, "defensive": config.regime_defensive}[state]
    return {"state": state, "name": NAMES[state], "ceiling": ceiling, "trend": trend,
            "breadth": None if share is None else round(share, 3), "reasons": reasons, "at": utcnow()}


def ceiling(store, broker):
    """Usable share of "Höchstens investiert" for new buys; 1 when no fresh posture exists."""
    root = getattr(store, "root", store)
    saved = root.get(f"market_regime:{broker}") or {}
    try:
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(saved["at"])).total_seconds() / 86400
        value = float(saved["ceiling"])
    except (KeyError, TypeError, ValueError):
        return 1.
    return value if 0 <= age <= MAX_AGE_DAYS and math.isfinite(value) else 1.


def save(store, broker, result):
    getattr(store, "root", store).set(f"market_regime:{broker}", result)
