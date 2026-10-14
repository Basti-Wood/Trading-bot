"""One durable intent per symbol/side/session, reconciled against broker state."""
from datetime import datetime, timezone
import hashlib
import math
from .broker import BrokerError
from .store import utcnow

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}


class Executor:
    def __init__(self, config, store, broker):
        self.config, self.store, self.broker = config, store, broker

    def reconcile(self):
        blocked = False
        for local in self.store.unresolved_orders():
            try:
                remote = self.broker.order(local["client_id"])
                self.store.update_order(local["client_id"], {**local, "status": remote["status"],
                    "broker_id": remote["id"], "filled_qty": remote.get("filled_qty"), "filled_avg_price": remote.get("filled_avg_price")})
            except BrokerError:
                # A 404 after a timed-out POST is ambiguous: do not retry or discard it.
                blocked = True
        return not blocked

    def send(self, payload):
        cid = payload["client_order_id"]
        record = {"symbol": payload["symbol"], "side": payload["side"], "qty": payload["qty"],
                  "status": "pending_submission", "mode": "paper" if self.config.paper else "live"}
        if not self.store.reserve_order(cid, record):
            return "An order was already attempted for this symbol and side today"
        try:
            remote = self.broker.submit(payload)
        except BrokerError as exc:
            # Definite validation/auth errors are terminal. Timeouts/5xx remain uncertain.
            status = "rejected" if exc.status in {400, 401, 403, 422} else "unknown"
            self.store.update_order(cid, {**record, "status": status})
            return f"Order {status}; {exc}"
        self.store.update_order(cid, {**record, "status": remote["status"], "broker_id": remote["id"],
                                     "filled_qty": remote.get("filled_qty"), "filled_avg_price": remote.get("filled_avg_price")})
        return f"Alpaca order {remote['status']} (acceptance is not a fill)"

    def execute(self, symbol, action, session, account, positions, orders, *, loss_breach=False, force_exit=False):
        cfg = self.config
        if not cfg.paper and not cfg.allow_live:
            return "Live mode is not enabled"
        if account.get("trading_blocked") or account.get("account_blocked") or account.get("status") != "ACTIVE":
            return "Alpaca account is blocked or inactive"
        position = next((p for p in positions if p["symbol"] == symbol), None)
        qty = float(position["qty"]) if position else 0
        if qty < 0:
            return "Existing short position requires manual review; bot is long-only"
        symbol_orders = [o for o in orders if o["symbol"] == symbol]
        owned = self.store.get(f"owned:{symbol}", False)
        closing = qty > 0 and owned and (action == "SELL" or loss_breach or force_exit)
        if qty > 0 and not owned:
            return "Existing position was not opened by this bot; leaving it under account control"
        if closing:
            if symbol_orders:
                # Cancel bracket exits first, then wait for broker confirmation on the next cycle.
                for order in symbol_orders:
                    if order.get("type") in {"stop", "stop_limit", "limit"}:
                        self.broker.cancel(order["id"])
                return "Waiting for existing exit orders to cancel before closing"
            side = "sell"
            payload = {"symbol": symbol, "side": side, "qty": format(qty, ".9f").rstrip("0").rstrip("."),
                       "type": "market", "time_in_force": "day"}
        else:
            if action != "BUY":
                return "No entry requested"
            if loss_breach:
                return "Daily loss limit reached; new entries blocked"
            if not self.store.get("entries_enabled", False):
                return "Entries are paused"
            if qty > 0 or symbol_orders:
                return "Position or pending order already exists"
            asset = self.broker.asset(symbol)
            if not asset.get("tradable") or asset.get("status") != "active":
                return "Asset is not tradable"
            price = self.broker.latest_price(symbol)
            capital = min(cfg.capital, float(account["equity"]))
            gross = sum(abs(float(p["market_value"])) for p in positions)
            # If any entry is pending, wait for a fresh account snapshot before allocating again.
            if any(o["side"] == "buy" for o in orders):
                return "Pending buy order; waiting for updated exposure"
            budget = min(capital * cfg.position_fraction, capital * cfg.gross_fraction - gross,
                         float(account["cash"]), float(account["buying_power"]))
            limit = math.ceil(price * 1.002 * 100) / 100
            qty = math.floor(budget / limit)
            if qty < 1:
                return "Allocation cannot fund one whole share within exposure limits"
            side = "buy"
            payload = {"symbol": symbol, "side": side, "qty": str(qty), "type": "limit", "limit_price": f"{limit:.2f}",
                       "time_in_force": "day", "order_class": "bracket",
                       "stop_loss": {"stop_price": f"{price * (1 - cfg.stop_loss):.2f}"},
                       "take_profit": {"limit_price": f"{price * (1 + cfg.take_profit):.2f}"}}
        # Include account/mode to prevent cross-account journal collisions, without exposing keys.
        scope = hashlib.sha256((self.broker.base + cfg.api_key).encode()).hexdigest()[:8]
        base_id = f"ensemble-{scope}-{session}-{symbol}-{side}"
        payload["client_order_id"] = base_id
        if side == "sell":
            # Fresh broker position quantities permit a bounded retry only after a
            # definite rejection/cancellation. Never retry an ambiguous/accepted exit.
            for attempt in range(3):
                cid = base_id + (f"-r{attempt}" if attempt else "")
                prior = self.store.order(cid)
                if not prior or prior.get("status") not in {"rejected", "canceled", "expired"}:
                    payload["client_order_id"] = cid
                    break
            else:
                return "Exit retry limit reached; inspect this position and its protection in Alpaca"
        result = self.send(payload)
        if side == "buy":
            self.store.set(f"owned:{symbol}", True)
        return result
