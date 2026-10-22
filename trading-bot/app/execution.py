"""One durable intent per symbol/side/session, reconciled against broker state."""
from datetime import datetime, timezone
import hashlib
import math
import time
from .allocation import BOTS, bot_capital, bot_share
from .broker import BrokerError
from .store import utcnow

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}
FAILED = {"rejected", "canceled", "expired"}


class Executor:
    def __init__(self, config, store, broker):
        self.config, self.store, self.broker = config, store, broker
        self.changed = False

    def reconcile(self):
        blocked = False
        for local in self.store.unresolved_orders():
            try:
                remote = self.broker.order(local["client_id"])
                self.store.update_order(local["client_id"], {**local, "status": remote["status"],
                    "broker_id": remote["id"], "filled_qty": remote.get("filled_qty"), "filled_avg_price": remote.get("filled_avg_price"),
                    "filled_at": remote.get("filled_at")})
            except BrokerError:
                # A 404 after a timed-out POST is ambiguous: do not retry or discard it.
                blocked = True
        return not blocked

    def send(self, payload, session=None, queued_for_open=False, plan=None):
        cid = payload["client_order_id"]
        record = {"symbol": payload["symbol"], "side": payload["side"], "qty": payload["qty"],
                  "status": "pending_submission", "mode": "paper" if self.config.paper else "live", "session": session,
                  "strategy": self.config.trading_mode, "queued_for_open": queued_for_open,
                  "limit_price": payload.get("limit_price")}
        if not self.store.reserve_order(cid, record):
            return "An order was already attempted for this symbol and side today"
        self.changed = True
        if payload["side"] == "buy":
            # Persist ownership before POST: an accepted order followed by a crash
            # must still receive stop/time exits when its position appears.
            self.store.set(f"owned:{payload['symbol']}", True)
        if payload["side"] == "buy" and self.config.intraday:
            # The strategy's own stop/target/exit rule travels with the position.
            self.store.set(f"daytrade_position:{payload['symbol']}", {**(plan or {}), "entry_id": cid, "started_at": utcnow()})
        try:
            remote = self.broker.submit(payload)
        except BrokerError as exc:
            # Definite validation/auth errors are terminal. Timeouts/5xx remain uncertain.
            status = "rejected" if exc.status in {400, 401, 403, 422} else "unknown"
            self.store.update_order(cid, {**record, "status": status})
            return f"Order {status}; {exc}"
        self.store.update_order(cid, {**record, "status": remote["status"], "broker_id": remote["id"],
                                     "filled_qty": remote.get("filled_qty"), "filled_avg_price": remote.get("filled_avg_price"),
                                     "filled_at": remote.get("filled_at")})
        queued = f"; queued for regular session {session}" if queued_for_open else ""
        return f"Alpaca order {remote['status']} (acceptance is not a fill){queued}"

    def execute(self, symbol, action, session, account, positions, orders, *, loss_breach=False, force_exit=False,
                queued_for_open=False, reference_price=None, stop_fraction=None, target_fraction=None, plan=None):
        cfg = self.config
        self.changed = False
        if queued_for_open and (cfg.same_day or not cfg.queue_daily_orders or action != "BUY"):
            return "Only daily buy orders can be queued for the next regular session"
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
        if cfg.coordinated:
            owner = self.store.owner(symbol)
            if owner and owner != cfg.trading_mode:
                return f"Symbol is owned by the {owner} strategy"
        closing = qty > 0 and owned and (action == "SELL" or loss_breach or force_exit)
        if qty > 0 and not owned:
            return "Existing position was not opened by this bot; leaving it under account control"
        if closing:
            if symbol_orders:
                # Cancel bracket exits first, then wait for broker confirmation on the next cycle.
                for order in symbol_orders:
                    if order.get("type") in {"stop", "stop_limit", "limit"} and order.get("status") != "pending_cancel":
                        self.broker.cancel(order["id"])
                        self.changed = True
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
            if cfg.coordinated and owned:
                return "Waiting for confirmed flat position before releasing strategy ownership"
            if any(o["side"] == "buy" for o in self.store.unresolved_orders()):
                return "An entry outcome is unresolved; waiting for reconciliation"
            if cfg.loop and not stop_fraction:
                return "Stop-Loss aus risiko.py fehlt; Einstieg blockiert"
            if cfg.loop_intraday and len(self.store.session_entries(symbol, session)) >= cfg.daytrade_max_entries:
                return "Intraday-Loop: Einstiegslimit für diese Aktie heute erreicht"
            if cfg.intraday:
                counted = [p for p in positions if not cfg.coordinated or self.store.get(f"owned:{p['symbol']}", False)]
                if len([p for p in counted if float(p["qty"]) != 0]) >= cfg.daytrade_max_positions:
                    return "Day-trading position limit reached"
                if len(self.store.session_entries(symbol, session)) >= cfg.daytrade_max_entries:
                    return "Day-trading entry limit reached for this symbol today"
                flat_since = self.store.get(f"flat_since:{symbol}", 0)
                if time.time() - flat_since < cfg.daytrade_cooldown_minutes * 60:
                    return "Waiting for the day-trading cooldown after the last position closed"
            asset = self.broker.asset(symbol)
            if (not asset.get("tradable") or asset.get("status") != "active"
                    or asset.get("class", "us_equity") != "us_equity"):
                return "Asset is not a tradable US stock or ETF"
            price = (reference_price if queued_for_open else self.broker.quote(symbol)[1] if cfg.same_day
                     else self.broker.latest_price(symbol))
            if not isinstance(price, (int, float)) or not math.isfinite(price) or not 0 < price < 1000000:
                return "Invalid reference price; entry blocked"
            capital = bot_capital(cfg, self.store, float(account["equity"]))
            strategy_capital = capital * bot_share(cfg, self.store)
            if strategy_capital <= 0:
                return "No budget: this bot's weight is 0%"
            # In parallel mode the user's own holdings live outside the bots' budget.
            gross = sum(abs(float(p["market_value"])) for p in positions
                        if not cfg.coordinated or self.store.owner(p["symbol"]) in BOTS)
            strategy_gross = sum(abs(float(p["market_value"])) for p in positions if self.store.get(f"owned:{p['symbol']}", False))
            # If any entry is pending, wait for a fresh account snapshot before allocating again.
            if any(o["side"] == "buy" for o in orders):
                return "Pending buy order; waiting for updated exposure"
            budget = min(strategy_capital * cfg.position_fraction, capital * cfg.gross_fraction - gross,
                         strategy_capital * cfg.gross_fraction - strategy_gross,
                         float(account["cash"]), float(account["buying_power"]))
            if cfg.same_day:
                # Broker-reported capacity, when supplied, can only lower our cash cap.
                for field in ("daytrading_buying_power", "non_marginable_buying_power"):
                    if account.get(field) is not None:
                        budget = min(budget, float(account[field]))
            limit = math.ceil(price * (1.0005 if cfg.same_day else 1.002) * 100) / 100
            qty = math.floor(budget / limit)
            fractional = qty < 1
            if fractional:
                # A share pricier than the budget is bought in fractions; Alpaca accepts
                # fractional day limit orders but not fractional brackets.
                if queued_for_open:
                    return "Allocation cannot fund one whole share; a fractional entry waits for the regular session"
                if not asset.get("fractionable"):
                    return "Allocation cannot fund one whole share and this stock cannot be bought in fractions"
                qty = math.floor(budget / limit * 10000) / 10000
                if qty * limit < 1:
                    return "Allocation is below Alpaca's $1 minimum order"
            side = "buy"
            if cfg.loop:
                # Trading-loop strategies exit by their own rules; risiko.py sets the only stop.
                stop, target = stop_fraction, None
            else:
                # A caller-supplied stop may only tighten the configured loss limit.
                stop = min(stop_fraction, cfg.effective_stop) if stop_fraction else cfg.effective_stop
                target = target_fraction or cfg.effective_take_profit
            payload = {"symbol": symbol, "side": side, "qty": format(qty, "f").rstrip("0").rstrip("."), "type": "limit",
                       "limit_price": f"{limit:.2f}", "time_in_force": "day", "extended_hours": False}
            # Without broker-side legs, the engine enforces these levels on every poll.
            guard = {"stop": stop, "target": target} if fractional else None
            if not fractional and target is None:
                payload.update({"order_class": "oto", "stop_loss": {"stop_price": f"{price * (1 - stop):.2f}"}})
            elif not fractional:
                payload.update({"order_class": "bracket",
                                "stop_loss": {"stop_price": f"{price * (1 - stop):.2f}"},
                                "take_profit": {"limit_price": f"{price * (1 + target):.2f}"}})
        # Include account/mode to prevent cross-account journal collisions, without exposing keys.
        scope = self.scope()
        base_id = f"ensemble-{scope}-{session}-{symbol}-{side}"
        if cfg.loop:
            # Distinct from daily IDs so both bots can trade a symbol on the same day.
            base_id = f"ensemble-{scope}-loop-{session}-{symbol}-{side}"
        if cfg.loop_intraday:
            # Several round trips per day: number each entry; its exit carries the same number.
            entries = len(self.store.session_entries(symbol, session))
            base_id = f"ensemble-{scope}-lday-{session.replace('-', '')}-{symbol}-{side[0]}{entries + 1 if side == 'buy' else max(1, entries)}"
        if cfg.intraday:
            if side == "buy":
                suffix = str(len(self.store.session_entries(symbol, session)) + 1)
            else:
                entry_id = self.store.get(f"daytrade_position:{symbol}", {}).get("entry_id", symbol)
                suffix = hashlib.sha256(entry_id.encode()).hexdigest()[:6]
            base_id = f"ensemble-{scope}-{session.replace('-', '')}-{symbol}-{side[0]}{suffix}"
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
        result = self.send(payload, session, queued_for_open=queued_for_open, plan=plan if side == "buy" else None)
        if side == "buy" and self.changed:
            self.store.set(f"unbracketed:{symbol}", guard)
            if guard:
                result += "; fractional shares, stop/take-profit managed by the bot"
        return result

    def scope(self):
        return hashlib.sha256((self.broker.base + self.config.api_key).encode()).hexdigest()[:8]
