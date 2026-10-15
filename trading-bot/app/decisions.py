"""Translate a market outlook into an action for this strategy's holdings."""


def position_context(config, store, symbol, positions):
    position = next((p for p in positions if p["symbol"] == symbol), None)
    qty = float(position["qty"]) if position else 0.0
    claimed = bool(store.get(f"owned:{symbol}", False))
    owner = store.owner(symbol) if config.coordinated else config.trading_mode if claimed else None
    return {
        "qty": qty,
        "owned": qty > 0 and claimed,
        "strategy": owner or ("external" if qty else None),
        "avg_entry_price": position.get("avg_entry_price") if position else None,
        "unrealized_pl": position.get("unrealized_pl") if position else None,
        "unrealized_plpc": position.get("unrealized_plpc") if position else None,
    }


def position_decision(signal, reason, position):
    """Neutral is WAIT when flat, HOLD when owned; only bearish evidence sells.

    Entry cost never vetoes a bearish exit. Protective exits are handled by the
    engine before forecasts and override this discretionary decision.
    """
    if position["qty"] < 0:
        return "WAIT", "Existing short position requires manual review; bot is long-only"
    if position["qty"] > 0 and not position["owned"]:
        owner = position["strategy"] or "external"
        return "WAIT", f"Position belongs to {owner}; this strategy leaves it unchanged"
    if position["owned"]:
        if signal == "SELL":
            return "SELL", "Forecast ensemble and news models expect a decline; sell regardless of current profit or loss"
        if signal == "BUY":
            return "HOLD", "Bullish agreement; keep the existing shares without adding to the position"
        return "HOLD", "Keep the existing shares; " + reason
    if signal == "BUY":
        return "BUY", reason
    if signal == "SELL":
        return "WAIT", "Bearish outlook, but no shares are owned; no sale or short position requested"
    return "WAIT", "No shares owned; " + reason
