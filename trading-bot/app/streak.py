"""Losing-streak cooldown per bot, after the "drawdown-circuit-breaker" skill
(claude-trading-skills, MIT licence, TraderMonty).

Every closed bot position is recorded with its last observed profit or loss. After
COOLDOWN_LOSING_TRADES losing trades in a row, that bot opens no new positions for COOLDOWN_HOURS
after the latest loss; open positions keep their exits. A winning trade ends the streak.
The result is the last observed unrealised P&L before the position disappeared, so it is close
to, not exactly, the realised result (fees, the final fill).
"""
import math

import pandas as pd

from .store import utcnow


def remember(store, position):
    """Last observed P&L of an owned open position, for the result once it is closed."""
    try:
        value = float(position.get("unrealized_pl") or 0)
        cost = float(position.get("market_value") or 0) - value
        fraction = float(position["unrealized_plpc"]) if position.get("unrealized_plpc") not in (None, "") else (
            value / cost if cost > 0 else 0.)
    except (TypeError, ValueError):
        return
    if math.isfinite(value) and math.isfinite(fraction):
        store.set(f"last_pnl:{position['symbol']}", {"pl": value, "plpc": fraction, "at": utcnow()})


def closed(store, symbol):
    """Record the result of a position that just went flat (once)."""
    last = store.get(f"last_pnl:{symbol}")
    if not last:
        return None
    store.set(f"last_pnl:{symbol}", None)
    result = {"symbol": symbol, "pl": round(last["pl"], 2), "plpc": round(last["plpc"], 5), "win": last["pl"] > 0}
    store.event("trade_result", result)
    return result


def status(config, store, now=None):
    """{"active", "losses", "until", "reason", "recent"} for this bot's book."""
    limit = config.cooldown_losses
    results = store.history("trade_result", 50)
    losses = 0
    for result in results:
        if result.get("win"):
            break
        losses += 1
    recent = [{k: r.get(k) for k in ("at", "symbol", "pl", "plpc", "win")} for r in results[:10]]
    if not limit or losses < limit or not results:
        return {"active": False, "losses": losses, "limit": limit, "recent": recent}
    now = now or pd.Timestamp.now(tz="UTC")
    until = pd.Timestamp(results[0]["at"]) + pd.Timedelta(hours=config.cooldown_hours)
    active = now < until
    return {"active": active, "losses": losses, "limit": limit, "until": until.isoformat(), "recent": recent,
            "reason": (f"Pause nach {losses} Verlust-Trades in Folge bis {until.tz_convert('Europe/Berlin'):%d.%m. %H:%M} Uhr"
                       if active else None)}
