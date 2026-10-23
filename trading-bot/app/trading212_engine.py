"""Independent non-US trading-loop book using the same per-stock version ranking."""
from dataclasses import replace
import hashlib
import json
import math
import threading
import time

import pandas as pd

from . import loop
from .broker import BrokerError
from .execution import TERMINAL
from .store import Store, StrategyStore, utcnow
from .trading212 import Trading212, market_data, mappings, number


# Entries run during the whole regular session: from ENTRY_DELAY after the open until CLOSE_BUFFER
# before the close. A buy that has not filled after BUY_TIMEOUT is canceled and may be tried again.
ENTRY_DELAY = pd.Timedelta(minutes=5)
CLOSE_BUFFER = pd.Timedelta(minutes=30)
BUY_TIMEOUT = pd.Timedelta(minutes=15)


def entry_window(session, now):
    opening, close = session["opening"], session["close"]
    return bool(session["open"] and opening is not None and close is not None
                and now - opening >= ENTRY_DELAY and close - now >= CLOSE_BUFFER)


def context(config):
    directory = config.data_dir / "trading212" / ("paper" if config.trading212_paper else "live")
    cfg = replace(config, data_dir=directory, trading_mode="loop", coordinated=True, history_days=loop.HISTORY_DAYS)
    return cfg, StrategyStore(Store(directory), "loop")


class Trading212Engine:
    def __init__(self, config, root, broker=None, data=None):
        self.config, self.root = config, root
        self.cfg, self.store = context(config)
        self.source = StrategyStore(root, "loop")
        self.broker, self.data = broker or Trading212(config), data or market_data(config)
        self.stop = threading.Event()
        self.thread = self.signals_thread = None
        self.last_signals = 0
        self.last_metadata = 0
        self.store.set("entries_enabled", False)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="trading212-loop", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)

    def scope(self, account):
        scope = hashlib.sha256((self.broker.base + account["id"]).encode()).hexdigest()
        previous = self.store.get("account_scope")
        if previous and previous != scope:
            raise BrokerError("Trading 212 account changed; use a separate DATA_DIR")
        self.store.set("account_scope", scope)

    def send(self, symbol, side, qty, kind, intent, *, price=None, version=None):
        generation = self.store.get(f"position_generation:{symbol}", "legacy") if side == "sell" else "entry"
        cid = "t212-" + hashlib.sha256(f"{symbol}:{side}:{kind}:{intent}:{generation}".encode()).hexdigest()[:32]
        record = {"broker": "trading212", "symbol": symbol, "side": side, "qty": qty,
                  "type": kind, "status": "pending_submission", "filled_qty": 0, "created_at": utcnow(),
                  "position_generation": cid if side == "buy" else generation,
                  "mode": "paper" if self.config.trading212_paper else "live", "version": version,
                  "limit_price": price if kind == "limit" else None, "stop_price": price if kind == "stop" else None}
        record["currency"] = self.broker.instrument(symbol)["currencyCode"]
        if not self.store.reserve_order(cid, record):
            return False
        if side == "buy":
            self.store.set(f"owned:{symbol}", True)
            self.store.set(f"loop_owner:{symbol}", version)
            self.store.set(f"position_generation:{symbol}", cid)
            self.store.set(f"exit_reason:{symbol}", None)
        try:
            remote = self.broker.submit(symbol, side, qty, kind=kind, price=price)
            record.update(status=remote["status"], broker_id=remote["id"], filled_qty=remote["filled_qty"])
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            record["status"] = "rejected" if isinstance(exc, BrokerError) and exc.status in {400, 401, 403, 422} else "unknown"
            if record["status"] == "rejected" and side == "buy":
                self.store.set(f"owned:{symbol}", False)
            self.store.event("error", {"symbol": symbol, "message": f"Trading 212 {kind} order {record['status']}; inspect broker before retrying"})
        self.store.update_order(cid, record)
        return True

    def reconcile(self, orders):
        by_id = {o["id"]: o for o in orders}
        complete, fetched = True, False
        for local in self.store.unresolved_orders():
            oid = local.get("broker_id")
            if not oid:
                complete = False  # No client order IDs: matching by quantity/time would be unsafe.
                continue
            try:
                remote = by_id.get(oid)
                if remote is None:
                    if fetched:
                        complete = False
                        continue
                    remote, fetched = self.broker.order(oid, local["symbol"]), True
                if remote["symbol"] != local["symbol"] or remote["side"] != local["side"]:
                    raise BrokerError("Trading 212 order reconciliation mismatch")
                self.store.update_order(local["client_id"], {**local, "status": remote["status"],
                                        "filled_qty": remote["filled_qty"]})
            except BrokerError:
                complete = False
        return complete

    def owned_quantity(self, symbol):
        # Count confirmed fills, not the entire account holding. Manual additions stay external.
        with self.store.db() as db:
            rows = db.execute("SELECT body FROM orders WHERE json_extract(body,'$.symbol')=?", (symbol,)).fetchall()
        generation = self.store.get(f"position_generation:{symbol}", "legacy")
        records = [json.loads(row[0]) for row in rows]
        total = sum(number(r.get("filled_qty", 0)) * (1 if r["side"] == "buy" else -1)
                    for r in records if r.get("position_generation", "legacy") == generation)
        return max(0., total)

    def close(self, position, orders, reason):
        symbol = position["symbol"]
        owned = self.owned_quantity(symbol)
        own_orders = {r.get("broker_id") for r in self.store.unresolved_orders() if r["symbol"] == symbol}
        outstanding = [o for o in orders if o["symbol"] == symbol]
        if outstanding:
            for order in outstanding:
                if order["id"] in own_orders and order["status"] != "pending_cancel":
                    self.broker.cancel(order["id"])
                    return True
            return False  # Wait for confirmed cancellation; never cancel a user's order.
        qty = min(position["qty"], position["available_qty"], owned)
        if qty <= 0:
            return False
        self.store.set(f"exit_reason:{symbol}", reason)
        # One intent per currently observed owned quantity. Unknown outcomes never resubmit.
        return self.send(symbol, "sell", qty, "market", f"exit:{self.store.get(f'loop_owner:{symbol}')}:{owned}")

    def protect(self, position, orders, stop_fraction):
        symbol = position["symbol"]
        qty = min(position["qty"], self.owned_quantity(symbol))
        if qty <= 0 or position["avg_entry_price"] <= 0:
            return False, False
        price = math.ceil(position["avg_entry_price"] * (1 - stop_fraction) * 100) / 100
        own_ids = {r.get("broker_id") for r in self.store.unresolved_orders() if r["symbol"] == symbol}
        own_stops = [o for o in orders if o["id"] in own_ids and o["symbol"] == symbol and o["type"] == "stop"]
        if any(o["status"] != "pending_cancel" and abs(o["qty"] - o["filled_qty"] - qty) < 1e-8
               and number(o.get("stop_price")) >= price for o in own_stops):
            return True, False
        if own_stops:
            stop = next((o for o in own_stops if o["status"] != "pending_cancel"), None)
            if stop:
                self.broker.cancel(stop["id"])
                return False, True
            return False, False
        if position["available_qty"] + 1e-8 < qty:
            return False, False
        # A failed/ambiguous protection attempt cannot repeat the same POST.
        attempted = self.send(symbol, "sell", qty, "stop", f"protect:{self.store.get(f'loop_owner:{symbol}')}:{qty}:{price}", price=price)
        return False, attempted

    def signals_tick(self, now, specs):
        forced = self.store.get("retest_requested", False)
        if self.signals_thread and self.signals_thread.is_alive() or not forced and self.last_signals and time.monotonic() - self.last_signals < 1800:
            return
        self.store.set("retest_requested", False)
        self.last_signals = time.monotonic()
        records = loop.active_versions(self.store)

        def work():
            frames, errors = {}, {}
            for ticker, spec in specs.items():
                if self.stop.is_set():
                    return
                try:
                    instrument = self.broker.instrument(ticker)
                    frame = self.data.bars(instrument, spec, now)
                    session = self.broker.session_for(ticker, now)
                    zone = frame.attrs["market_timezone"]
                    prior = session["prior_close"]
                    last_day = frame.index[-1].tz_convert(zone).date()
                    opening = session["opening"]
                    valid = (prior is not None and prior.tz_convert(zone).date() == last_day
                             and opening is not None and opening > prior
                             and (now - frame.index[-1]).total_seconds() < 8 * 86400)
                    frame.attrs["target_session"] = str(opening.tz_convert(zone).date()) if valid else None
                    frames[ticker] = frame
                except Exception as exc:
                    errors[ticker] = str(exc)[:200]
            for version, record in records.items():
                if self.stop.is_set():
                    return
                symbols = list(specs) if record["symbol"] == loop.LIST else [record["symbol"]]
                usable = {s: frames[s] for s in symbols if s in frames}
                try:
                    if usable:
                        self.store.set(f"loop_results:{version}", loop.evaluate(self.cfg, record, usable))
                        self.store.set(f"loop_error:{version}", None)
                except Exception as exc:
                    self.store.set(f"loop_error:{version}", {"message": str(exc)[:200], "at": utcnow()})
            self.store.set("data_errors", errors)
        self.signals_thread = threading.Thread(target=work, name="trading212-stock-tests", daemon=True)
        self.signals_thread.start()

    def cycle(self, now=None):
        now = now or pd.Timestamp.now(tz="UTC")
        if not self.config.trading212_configured:
            self.store.set("engine", {"status": "setup", "message": "Add Trading 212 API key and secret", "at": utcnow()})
            return
        account = self.broker.account()
        self.scope(account)
        positions, orders = self.broker.positions(), self.broker.open_orders()
        self.store.set("account", {**{k: v for k, v in account.items() if k != "id"},
                                  "positions": positions, "broker": "trading212", "at": utcnow()})
        self.store.set("loop_strategies", loop.active_versions(self.source))
        if not self.last_metadata or time.monotonic() - self.last_metadata >= 600:
            instruments = self.broker.metadata()
            self.store.set("instruments", [{"ticker": i["ticker"], "name": i.get("name", i["ticker"]),
                "currency": i["currencyCode"], "isin": i.get("isin"), "mic": i["schedule"]["mic"],
                "exchange": i["schedule"]["exchange"]} for i in instruments.values()
                if i.get("type") in {"STOCK", "ETF"} and (i.get("schedule") or {}).get("mic")])
            self.last_metadata = time.monotonic()
        reconciled = self.reconcile(orders)
        rules = loop.risk_rules(self.config)
        if rules["ok"]:
            self.store.set("risk_rules", rules)
        safe_rules = rules if rules["ok"] else self.store.get("risk_rules") or {}
        peak = max(account["equity"], self.store.get("equity_peak") or 0)
        self.store.set("equity_peak", peak)
        drawdown = (peak - account["equity"]) / peak * 100 if peak else 0
        if safe_rules.get("max_drawdown") and drawdown >= safe_rules["max_drawdown"]:
            self.store.set("halt", {"at": utcnow(), "drawdown": drawdown, "limit": safe_rules["max_drawdown"], "peak": peak})
            self.store.set("entries_enabled", False)

        # Risk/protection comes before candle downloads, strategy subprocesses and new entries.
        acted, protected = False, True
        own_pending = {o.get("broker_id"): o for o in self.store.unresolved_orders()}
        for order in orders:
            record = own_pending.get(order["id"])
            if not record or order["side"] != "buy" or order["status"] == "pending_cancel":
                continue
            session = self.broker.session_for(order["symbol"], now)
            try:
                age = now - pd.Timestamp(order.get("created_at"))
            except (TypeError, ValueError):
                age = None
            expired = not entry_window(session, now) or age is None or age >= BUY_TIMEOUT
            if not self.store.get("entries_enabled", False) or self.store.get("halt") or expired:
                self.broker.cancel(order["id"])
                acted = True
                break
        for position in positions:
            symbol = position["symbol"]
            if not self.store.get(f"owned:{symbol}", False):
                continue
            if not self.owned_quantity(symbol):
                protected = False
                continue
            session = self.broker.session_for(symbol, now)
            stop = safe_rules.get("stop_loss", self.config.stop_loss * 100) / 100
            stopped = position["current_price"] <= position["avg_entry_price"] * (1 - stop)
            exit_reason = self.store.get(f"exit_reason:{symbol}")
            failed_protection = any(o["symbol"] == symbol and o.get("type") == "stop" and o["status"] == "rejected"
                                    and o.get("position_generation", "legacy") == self.store.get(f"position_generation:{symbol}", "legacy")
                                    for o in self.store.orders(200))
            if failed_protection:
                exit_reason = "Broker stop protection was rejected; closing the bot's shares"
            if self.store.get("halt") or not loop.owner_version(self.store, symbol) or stopped or exit_reason:
                reason = exit_reason or ("Drawdown stop" if self.store.get("halt") else "Stop loss or strategy deactivated")
                self.store.set(f"exit_reason:{symbol}", reason)
                if session["open"] and not acted and reconciled:
                    acted = self.close(position, orders, reason)
                protected = False
            elif not acted and reconciled:
                safe, acted = self.protect(position, orders, stop)
                protected = protected and safe
        held = {p["symbol"] for p in positions if p["qty"] > 0}
        pending = {o["symbol"] for o in self.store.unresolved_orders()}
        for symbol in self.store.owned_symbols():
            if symbol not in pending and (symbol not in held or self.owned_quantity(symbol) == 0):
                # A filled order can become visible before its position. Require two
                # separated flat observations before releasing that position's owner.
                if symbol not in held and self.owned_quantity(symbol) > 0:
                    observed = self.store.get(f"flat_observed:{symbol}")
                    if observed is None:
                        self.store.set(f"flat_observed:{symbol}", time.time())
                        protected = False
                        continue
                    if time.time() - observed < 30:
                        protected = False
                        continue
                self.store.set(f"owned:{symbol}", False)
                self.store.set(f"exit_reason:{symbol}", None)
                self.store.set(f"flat_observed:{symbol}", None)
            elif symbol in held:
                self.store.set(f"flat_observed:{symbol}", None)

        specs = mappings(self.config)
        symbols = sorted(set(specs) | {p["symbol"] for p in positions})
        self.store.set("monitor_symbols", symbols)
        errors = self.store.get("data_errors", {})
        rows = []
        for symbol in symbols:
            choice = loop.combine(self.store, symbol, self.cfg)
            action, reason = "WAIT", errors.get(symbol) or choice["reason"]
            position = next((p for p in positions if p["symbol"] == symbol), None)
            owned = self.store.get(f"owned:{symbol}", False)
            if position and not owned:
                reason = "External account holding; signals only"
            elif symbol not in specs:
                reason = "Map this instrument to matching daily market data before trading"
            else:
                try:
                    instrument = self.broker.instrument(symbol)
                    spec = specs[symbol]
                    self.data.verify({"symbol": spec["symbol"], "mic_code": spec["mic"], "currency": spec["currency"]}, spec, instrument)
                    session = self.broker.session_for(symbol, now)
                    opening = session["opening"]
                    # Sessions are identified from the strategy's exchange-local candle date.
                    target = choice.get("target_session")
                    zone = choice.get("market_timezone", "UTC")
                    valid_day = target is not None and opening is not None and str(opening.tz_convert(zone).date()) == target
                    if choice["signal"] == "SELL" and position and owned:
                        action = "SELL"
                        if not acted and reconciled and session["open"] and valid_day:
                            acted = self.close(position, orders, choice["reason"])
                    elif position and owned:
                        action = "HOLD"
                    elif choice["signal"] == "BUY":
                        if not self.store.get("entries_enabled", False):
                            reason = "Trading 212 entries are paused"
                        elif not rules["ok"] or self.store.get("halt"):
                            reason = "Trading-loop risk rules block new entries"
                        elif not reconciled or not protected:
                            reason = "Waiting for order reconciliation and broker stop protection"
                        elif any(o["side"] == "buy" for o in [*orders, *self.store.unresolved_orders()]):
                            reason = "A buy is still pending; waiting for a fresh filled-position snapshot"
                        elif not session["open"]:
                            reason = "Exchange closed; this listing trades again at its next session"
                        elif not valid_day:
                            reason = "Waiting for the signal from the latest completed daily candle"
                        elif not entry_window(session, now):
                            reason = "Outside the entry window (5 minutes after the open until 30 minutes before the close)"
                        elif not acted and symbol not in pending:
                            price = self.data.quote(instrument, spec, pd.Timestamp.now(tz="UTC"))
                            fx = self.data.execution_fx(instrument["currencyCode"], account["currency"], pd.Timestamp.now(tz="UTC"))
                            capital = account["equity"] * self.config.trading212_capital_fraction
                            if self.config.trading212_capital_cap:
                                capital = min(capital, self.config.trading212_capital_cap)
                            # Include all account positions in gross exposure, including external ones.
                            gross = sum(abs(p["market_value"]) for p in positions)
                            budget = min(capital * self.config.position_fraction,
                                         capital * self.config.gross_fraction - gross, account["cash"])
                            # Whole shares, no borrowing; reserve extra cash for FX/fees/slippage.
                            qty = math.floor(max(0, budget) / (price * fx * 1.01))
                            max_qty = instrument.get("maxOpenQuantity")
                            if max_qty is not None:
                                qty = min(qty, math.floor(number(max_qty, nonnegative=True)))
                            if qty > 0:
                                action = "BUY"
                                # One intent per signal and 15-minute slot: an expired, unfilled buy may retry later.
                                slot = now.floor("15min").isoformat()
                                acted = self.send(symbol, "buy", qty, "limit", f"entry:{target}:{choice.get('bar_at')}:{slot}",
                                                  price=round(price, 2), version=choice["version"])
                                reason = choice["reason"] + "; order intent journaled; acceptance is not a fill"
                            else:
                                reason = "Trading 212 cash or exposure limit leaves no whole-share budget"
                except (BrokerError, KeyError, ValueError) as exc:
                    reason = str(exc)[:240]
            self.store.set(f"forecast:{symbol}", {**choice, "action": action, "reason": reason, "decision_at": utcnow()})
            rows.append({"broker": "trading212", "symbol": symbol, "version": choice.get("version"),
                         "test": choice.get("test"), "signal": choice.get("signal"), "action": action,
                         "reason": reason, "views": choice.get("views", {}),
                         "owner": loop.owner_version(self.store, symbol) if owned else None})
        self.store.set("stocks", rows)
        if not acted:
            self.signals_tick(now, specs)
        self.store.set("engine", {"status": "running" if reconciled else "blocked",
            "message": ("Monitoring non-US listings" if specs else "Add Trading 212 instrument mappings; balances remain available")
                       if reconciled else "Unresolved order outcome; no automatic retry", "at": utcnow()})

    def run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.store.set("engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
            self.stop.wait(15)
