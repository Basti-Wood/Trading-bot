"""Bot budget and per-bot weights, adjustable from the dashboard without a restart.

The budget is the share of account equity (optionally capped in dollars) that the
automatic traders may use together. Weights split that budget between them.
External holdings are observed and stay outside the bots' allowances.
"""
import math

BOTS = ("daily", "daytrade", "loop")
NAMES = {"daily": "Prediction-Bot", "daytrade": "Daytrading-Bot", "loop": "Trading-Loop"}


def allocation(config, store):
    saved = store.get("allocation") or {}
    share = config.daytrade_capital_share
    # Without saved settings the previous daily/day-trading split applies; newer bots start at 0.
    weights = {"daily": round((1 - share) * 100), "daytrade": round(share * 100), "loop": 0}
    weights.update({k: v for k, v in (saved.get("weights") or {}).items() if k in BOTS})
    return {"capital_fraction": saved.get("capital_fraction", config.capital_fraction),
            "capital_cap": saved.get("capital_cap", config.capital),
            "weights": weights, "saved_at": saved.get("at")}


def validate(body):
    """Clean dashboard input or raise ValueError with a message for the user."""
    try:
        fraction = float(body["capital_fraction"])
        cap = float(body.get("capital_cap") or 0)
        # A bot the dashboard does not send keeps weight 0.
        weights = {bot: float(body["weights"].get(bot, 0)) for bot in BOTS}
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("Budget, Obergrenze und Gewichtungen als Zahlen angeben")
    if not math.isfinite(fraction) or not .01 <= fraction <= 1:
        raise ValueError("Bot-Budget muss zwischen 1 % und 100 % des Kontos liegen")
    if not math.isfinite(cap) or cap != 0 and not 100 <= cap <= 10000000:
        raise ValueError("Obergrenze muss 0 (keine) oder zwischen 100 und 10.000.000 USD liegen")
    if any(not math.isfinite(w) or not 0 <= w <= 100 for w in weights.values()):
        raise ValueError("Jede Gewichtung muss zwischen 0 und 100 liegen")
    if sum(weights.values()) <= 0:
        raise ValueError("Mindestens ein Bot braucht eine Gewichtung über 0")
    return {"capital_fraction": fraction, "capital_cap": cap, "weights": {k: round(v, 1) for k, v in weights.items()}}


def managed(config):
    """True for the combined portfolio and each of its child strategies."""
    return config.coordinated or config.trading_mode == "both"


def bot_capital(config, store, equity):
    """Capital all automatic traders may use together at the current equity."""
    if not managed(config):
        return config.sizing_capital(equity)
    current = allocation(config, store)
    base = max(0., equity) * current["capital_fraction"]
    return min(base, current["capital_cap"]) if current["capital_cap"] else base


def bot_share(config, store, strategy=None):
    """This bot's fraction of the bot capital."""
    if not managed(config):
        return config.strategy_fraction
    weights = allocation(config, store)["weights"]
    total = sum(weights.values())
    return weights.get(strategy or config.trading_mode, 0) / total if total else 0.
