"""Trade yourself: the user's own orders from the dashboard, and how the bots watch them.

Manual positions belong to "manual": no bot buys, sells or cancels them, and they do not
consume the bots' budget. The bots only report what they see for these stocks.
"""
import hashlib
import math
import re
import uuid

from .allocation import BOTS, NAMES
from .broker import BrokerError
from .loop import combine as combine_loop

SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
SIGNALS = {"BUY": "Kaufsignal", "SELL": "kein Kaufsignal / Ausstieg", "HOLD": "investiert, hält"}


def signal_text(view):
    if view.get("error"):
        return "nicht berechenbar"
    test = view.get("test") or {}
    return f"{SIGNALS.get(view.get('signal'), view.get('signal'))} (Stresstest {'grün' if test.get('gruen') else 'rot'})"


def number(body, key, lo, hi, message):
    value = body.get(key)
    if value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(message)
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(message)
    return value


def parse(body):
    symbol = str(body.get("symbol") or "").strip().upper()
    if not SYMBOL.fullmatch(symbol):
        raise ValueError("Symbol ungültig (US-Aktie oder ETF, z. B. AAPL)")
    side = body.get("side")
    if side not in {"buy", "sell"}:
        raise ValueError("Kaufen oder Verkaufen wählen")
    kind = body.get("type", "market")
    if kind not in {"market", "limit"}:
        raise ValueError("Ordertyp muss Market oder Limit sein")
    qty = number(body, "qty", .0001, 1000000, "Stückzahl muss zwischen 0,0001 und 1.000.000 liegen")
    notional = number(body, "notional", 1, 10000000, "Betrag muss mindestens 1 USD sein")
    if (qty is None) == (notional is None):
        raise ValueError("Entweder Stückzahl oder Betrag in USD angeben")
    limit = number(body, "limit_price", .01, 1000000, "Limitpreis muss größer 0 sein")
    if kind == "limit" and limit is None:
        raise ValueError("Für eine Limit-Order einen Limitpreis angeben")
    stop = number(body, "stop_loss_pct", .5, 50, "Stop-Loss muss zwischen 0,5 und 50 % liegen")
    target = number(body, "take_profit_pct", .5, 200, "Kursziel muss zwischen 0,5 und 200 % liegen")
    if (stop or target) and (side != "buy" or notional is not None or qty != int(qty)):
        raise ValueError("Stop-Loss und Kursziel gehen nur beim Kauf mit ganzen Stückzahlen")
    return {"symbol": symbol, "side": side, "type": kind, "qty": qty, "notional": notional,
            "limit_price": limit if kind == "limit" else None, "stop": stop, "target": target}


def place(config, store, broker, body):
    """Validate and submit one manual order; returns a message for the user."""
    order = parse(body)
    symbol, side = order["symbol"], order["side"]
    owner = store.owner(symbol)
    if owner in BOTS:
        raise ValueError(f"{symbol} gehört gerade dem {NAMES[owner]}. Der Bot verwaltet diese Position selbst.")
    account = broker.account()
    if account.get("trading_blocked") or account.get("account_blocked") or account.get("status") != "ACTIVE":
        raise ValueError("Alpaca-Konto ist gesperrt oder nicht aktiv")
    positions = broker.positions()
    held = float(next((p["qty"] for p in positions if p["symbol"] == symbol), 0) or 0)
    try:
        price = order["limit_price"] or broker.latest_price(symbol, max_age=None)
    except (BrokerError, KeyError, TypeError, ValueError):
        raise ValueError(f"Kein aktueller Kurs für {symbol} verfügbar")
    if side == "sell":
        if held <= 0:
            raise ValueError(f"Du hältst keine {symbol}-Aktien")
        qty = order["qty"] if order["qty"] is not None else order["notional"] / price
        if qty > held + 1e-9:
            raise ValueError(f"Du hältst nur {held:g} {symbol}-Aktien")
        # Resting stop/target legs reserve the shares; cancel them first.
        resting = [o for o in broker.open_orders() if o["symbol"] == symbol and o["side"] == "sell"]
        if resting:
            for o in resting:
                broker.cancel(o["id"])
            return f"{len(resting)} offene Schutz-Order(s) für {symbol} werden storniert. Klick in ein paar Sekunden nochmal auf Verkaufen."
    else:
        asset = broker.asset(symbol)
        if not asset.get("tradable") or asset.get("status") != "active":
            raise ValueError(f"{symbol} ist bei Alpaca nicht handelbar")
        whole = order["notional"] is None and order["qty"] == int(order["qty"])
        if not whole and not asset.get("fractionable"):
            raise ValueError(f"{symbol} gibt es nur in ganzen Stück")
        cost = order["notional"] or order["qty"] * price
        if cost > float(account["cash"]):
            raise ValueError(f"Nicht genug Bargeld: Order etwa {cost:,.2f} USD, verfügbar {float(account['cash']):,.2f} USD (kein Kredit)")
    payload = {"symbol": symbol, "side": side, "type": order["type"], "time_in_force": "day"}
    if order["qty"] is not None:
        payload["qty"] = format(order["qty"], "f").rstrip("0").rstrip(".")
    else:
        payload["notional"] = f"{order['notional']:.2f}"
    if order["limit_price"]:
        payload["limit_price"] = f"{order['limit_price']:.2f}"
    if order["stop"] or order["target"]:
        payload["order_class"] = "bracket" if order["stop"] and order["target"] else "oto"
        if order["stop"]:
            payload["stop_loss"] = {"stop_price": f"{price * (1 - order['stop'] / 100):.2f}"}
        if order["target"]:
            payload["take_profit"] = {"limit_price": f"{price * (1 + order['target'] / 100):.2f}"}
    scope = hashlib.sha256((broker.base + config.api_key).encode()).hexdigest()[:8]
    cid = f"manual-{scope}-{uuid.uuid4().hex[:16]}"
    payload["client_order_id"] = cid
    record = {"symbol": symbol, "side": side, "qty": payload.get("qty") or f"${payload['notional']}",
              "status": "pending_submission", "mode": "paper" if config.paper else "live", "session": None,
              "strategy": "manual", "queued_for_open": False, "limit_price": payload.get("limit_price")}
    # Reserving a buy claims the symbol for "manual" atomically, so no bot can buy it meanwhile.
    if not store.reserve_order(cid, record, strategy="manual"):
        raise ValueError(f"{symbol} wurde gerade von einem Bot beansprucht")
    try:
        remote = broker.submit(payload)
    except BrokerError as exc:
        store.update_order(cid, {**record, "status": "rejected" if exc.status in {400, 401, 403, 422} else "unknown"})
        raise ValueError(f"Alpaca hat die Order nicht angenommen ({exc})")
    store.update_order(cid, {**record, "status": remote["status"], "broker_id": remote["id"],
                             "filled_qty": remote.get("filled_qty"), "filled_avg_price": remote.get("filled_avg_price")})
    store.event("manual", {"symbol": symbol, "side": side, "qty": record["qty"], "status": remote["status"]})
    return f"Order an Alpaca übergeben: {'Kauf' if side == 'buy' else 'Verkauf'} {record['qty']} {symbol} (Status {remote['status']})"


def cancel(store, broker, order_id):
    if not isinstance(order_id, str) or not re.fullmatch(r"[0-9a-fA-F\-]{8,64}", order_id):
        raise ValueError("Ungültige Order")
    remote = broker.order_by_id(order_id)
    if store.owner(remote["symbol"]) in BOTS or remote.get("client_order_id", "").startswith("ensemble-"):
        raise ValueError("Diese Order gehört einem Bot")
    broker.cancel(order_id)
    return f"Stornierung für {remote['symbol']} angefragt"


def outlook(config, contexts, position, open_orders):
    """What each bot currently sees for one of the user's holdings. Advice only, no action."""
    symbol = position["symbol"]
    daily = contexts.get("daily")
    daytrade = contexts.get("daytrade")
    loop = contexts.get("loop")
    views = []
    forecast = daily[1].get(f"forecast:{symbol}") if daily else None
    if forecast:
        votes = list((forecast.get("votes") or {}).values())
        views.append({"bot": "daily", "name": NAMES["daily"], "bullish": votes.count("BUY"), "bearish": votes.count("SELL"),
                      "voters": len(votes), "expected_return": forecast.get("expected_return"),
                      "text": f"{votes.count('BUY')} bullisch / {votes.count('SELL')} bärisch von {len(votes)} Stimmen"})
    else:
        views.append({"bot": "daily", "name": NAMES["daily"], "text": "Trainiert noch ein Modell für diese Aktie"})
    book = daytrade[1].get(f"playbook:{symbol}") if daytrade else None
    if book:
        fired = [s["name"] for s in book.get("signals") or []]
        views.append({"bot": "daytrade", "name": NAMES["daytrade"],
                      "text": ("Setup aktiv: " + ", ".join(fired)) if fired else (book.get("note") or "Kein Daytrading-Setup aktiv")})
    combined = combine_loop(loop[1], symbol) if loop else None
    if combined and combined["views"]:
        views.append({"bot": "loop", "name": NAMES["loop"], "text": "; ".join(
            f"{version}: {signal_text(view)}" for version, view in combined["views"].items())})
    plpc = float(position.get("unrealized_plpc") or 0)
    protected = any(o["symbol"] == symbol and o["side"] == "sell" and o.get("type") in {"stop", "stop_limit", "trailing_stop"}
                    for o in open_orders)
    bearish = forecast and views[0].get("bearish", 0) >= config.min_votes
    bullish = forecast and views[0].get("bullish", 0) >= config.min_votes
    if not protected and plpc <= -config.stop_loss:
        hint, tone = f"Minus {abs(plpc):.1%} ohne Stop-Loss: prüfe, ob du verkaufen willst", "sell"
    elif bearish:
        hint, tone = "Der Prediction-Bot wäre hier ausgestiegen (genug bärische Stimmen)", "sell"
    elif bullish:
        hint, tone = "Der Prediction-Bot sieht genug bullische Stimmen", "buy"
    else:
        hint, tone = "Keine klare Richtung; die Bots würden halten", "hold"
    return {"views": views, "hint": hint, "tone": tone, "protected": protected}
