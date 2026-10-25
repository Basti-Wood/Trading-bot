"""Parallel non-US daily predictions and strategy-file loop, sharing one account snapshot."""
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
from .trading212_prediction import PredictionWorker


# Entries run during the whole regular session: from ENTRY_DELAY after the open until CLOSE_BUFFER
# before the close. A buy that has not filled after BUY_TIMEOUT is canceled and may be tried again.
ENTRY_DELAY = pd.Timedelta(minutes=5)
CLOSE_BUFFER = pd.Timedelta(minutes=30)
BUY_TIMEOUT = pd.Timedelta(minutes=15)


def entry_window(session, now):
    opening, close = session["opening"], session["close"]
    return bool(session["open"] and opening is not None and close is not None
                and now - opening >= ENTRY_DELAY and close - now >= CLOSE_BUFFER)


def context(config, strategy="loop"):
    directory = config.data_dir / "trading212" / ("paper" if config.trading212_paper else "live")
    cfg = replace(config, data_dir=directory, trading_mode=strategy, coordinated=True, history_days=loop.HISTORY_DAYS,
                  broker_name="trading212", feed="trading212-" + config.trading212_data_source)
    return cfg, StrategyStore(Store(directory), strategy)


def source_signature(config, spec):
    return hashlib.sha256(json.dumps({"source": config.trading212_data_source, "listing": spec}, sort_keys=True).encode()).hexdigest()


class Trading212Engine:
    def __init__(self, config, root, broker=None, data=None):
        self.config, self.root = config, root
        self.cfg, self.store = context(config)
        self.daily_cfg, self.daily = context(config, "daily")
        self.predictions = PredictionWorker(self.daily_cfg, self.daily, self.store)
        self.books = {"loop": self.store, "daily": self.daily}
        self.source = StrategyStore(root, "loop")
        self.broker, self.data = broker or Trading212(config), data or market_data(config)
        self.stop = threading.Event()
        self.thread = self.signals_thread = None
        self.last_signals = 0
        self.last_metadata = 0
        self.turn = 0
        self.store.set("entries_enabled", False)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="trading212-loop", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        self.predictions.shutdown()
        if self.thread:
            self.thread.join(timeout=3)

    def scope(self, account):
        scope = hashlib.sha256((self.broker.base + account["id"]).encode()).hexdigest()
        previous = self.store.get("account_scope")
        if previous and previous != scope:
            raise BrokerError("Trading 212 account changed; use a separate DATA_DIR")
        self.store.set("account_scope", scope)

    def send(self, symbol, side, qty, kind, intent, *, price=None, version=None, strategy="loop"):
        book = self.books[strategy]
        generation = book.get(f"position_generation:{symbol}", "legacy") if side == "sell" else "entry"
        suffix = "" if strategy == "loop" else ":daily"
        cid = "t212-" + hashlib.sha256(f"{symbol}:{side}:{kind}:{intent}:{generation}{suffix}".encode()).hexdigest()[:32]
        record = {"broker": "trading212", "symbol": symbol, "side": side, "qty": qty,
                  "strategy": strategy,
                  "type": kind, "status": "pending_submission", "filled_qty": 0, "created_at": utcnow(),
                  "position_generation": cid if side == "buy" else generation,
                  "mode": "paper" if self.config.trading212_paper else "live", "version": version,
                  "limit_price": price if kind == "limit" else None, "stop_price": price if kind == "stop" else None}
        record["currency"] = self.broker.instrument(symbol)["currencyCode"]
        if not book.reserve_order(cid, record):
            return False
        if side == "buy":
            book.set(f"owned:{symbol}", True)
            book.set(f"loop_owner:{symbol}", version)
            book.set(f"position_generation:{symbol}", cid)
            book.set(f"exit_reason:{symbol}", None)
        try:
            remote = self.broker.submit(symbol, side, qty, kind=kind, price=price)
            record.update(status=remote["status"], broker_id=remote["id"], filled_qty=remote["filled_qty"])
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            record["status"] = "rejected" if isinstance(exc, BrokerError) and exc.status in {400, 401, 403, 422} else "unknown"
            if record["status"] == "rejected" and side == "buy":
                book.set(f"owned:{symbol}", False)
            book.event("error", {"symbol": symbol, "message": f"Trading 212 {kind} order {record['status']}; inspect broker before retrying"})
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

    def owned_quantity(self, symbol, strategy="loop"):
        # Count confirmed fills, not the entire account holding. Manual additions stay external.
        with self.store.db() as db:
            rows = db.execute("SELECT body FROM orders WHERE json_extract(body,'$.symbol')=?", (symbol,)).fetchall()
        generation = self.books[strategy].get(f"position_generation:{symbol}", "legacy")
        records = [json.loads(row[0]) for row in rows]
        total = sum(number(r.get("filled_qty", 0)) * (1 if r["side"] == "buy" else -1)
                    for r in records if r.get("strategy", "loop") == strategy
                    and r.get("position_generation", "legacy") == generation)
        return max(0., total)

    def close(self, position, orders, reason, strategy="loop"):
        book = self.books[strategy]
        symbol = position["symbol"]
        owned = self.owned_quantity(symbol, strategy)
        # Persist before cancellation so a forecast change cannot abandon an exit
        # after removing the broker-held stop.
        book.set(f"exit_reason:{symbol}", reason)
        own_orders = {r.get("broker_id") for r in self.store.unresolved_orders()
                      if r["symbol"] == symbol and r.get("strategy", "loop") == strategy}
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
        book.set(f"exit_reason:{symbol}", reason)
        # One intent per currently observed owned quantity. Unknown outcomes never resubmit.
        return self.send(symbol, "sell", qty, "market", f"exit:{book.get(f'loop_owner:{symbol}')}:{owned}", strategy=strategy)

    def protect(self, position, orders, stop_fraction, strategy="loop"):
        book = self.books[strategy]
        symbol = position["symbol"]
        qty = min(position["qty"], self.owned_quantity(symbol, strategy))
        if qty <= 0 or position["avg_entry_price"] <= 0:
            return False, False
        price = math.ceil(position["avg_entry_price"] * (1 - stop_fraction) * 100) / 100
        own_ids = {r.get("broker_id") for r in self.store.unresolved_orders()
                   if r["symbol"] == symbol and r.get("strategy", "loop") == strategy}
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
        # A confirmed canceled/filled predecessor permits a *new* protection intent. Unknown
        # outcomes stay unresolved and never get this retry path.
        previous = sum(1 for o in self.store.orders(1000) if o["symbol"] == symbol and o.get("type") == "stop"
                       and o.get("strategy", "loop") == strategy and o["status"] in TERMINAL
                       and o.get("position_generation", "legacy") == book.get(f"position_generation:{symbol}", "legacy"))
        attempted = self.send(symbol, "sell", qty, "stop", f"protect:{book.get(f'loop_owner:{symbol}')}:{qty}:{price}:{previous}",
                              price=price, strategy=strategy)
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
                    frame = self.data.bars(instrument, spec, now).copy(deep=True)
                    session = self.broker.session_for(ticker, now)
                    zone = frame.attrs["market_timezone"]
                    prior = session["prior_close"]
                    last_day = frame.index[-1].tz_convert(zone).date()
                    opening = session["opening"]
                    if prior is not None and (opening is None or opening <= prior):
                        opening = session.get("next_open")
                    valid = (prior is not None and prior.tz_convert(zone).date() == last_day
                             and opening is not None and opening > prior
                             and (now - frame.index[-1]).total_seconds() < 8 * 86400)
                    frame.attrs["target_session"] = str(opening.tz_convert(zone).date()) if valid else None
                    frame.attrs["source_signature"] = source_signature(self.config, spec)
                    frame.attrs["input_complete_at"] = prior.isoformat() if prior is not None else None
                    frames[ticker] = frame
                    try:
                        self.predictions.forecast(ticker, frame)
                    except Exception as exc:
                        self.daily.set(f"prediction_error:{ticker}", {"message": str(exc)[:200], "at": utcnow()})
                except Exception as exc:
                    errors[ticker] = str(exc)[:200]
                    self.daily.set(f"prediction_error:{ticker}", {"message": errors[ticker], "at": utcnow()})
            self.predictions.set_frames(frames)
            for version, record in records.items():
                if self.stop.is_set():
                    return
                symbols = list(specs) if record["symbol"] == loop.LIST else [record["symbol"]]
                usable = {s: frames[s] for s in symbols if s in frames}
                try:
                    if usable:
                        if record.get("timeframe", loop.DAILY) != loop.DAILY:
                            raise BrokerError("Trading 212 daily data cannot evaluate an intraday strategy")
                        result = loop.evaluate(self.cfg, record, usable)
                        for symbol, item in result["symbols"].items():
                            item["source_signature"] = frames[symbol].attrs["source_signature"]
                        self.store.set(f"loop_results:{version}", result)
                        self.store.set(f"loop_error:{version}", None)
                except Exception as exc:
                    self.store.set(f"loop_error:{version}", {"message": str(exc)[:200], "at": utcnow()})
            self.store.set("data_errors", errors)
            self.daily.set("data_errors", errors)
        self.signals_thread = threading.Thread(target=work, name="trading212-stock-tests", daemon=True)
        self.signals_thread.start()

    def strategy_share(self, strategy):
        share = self.config.trading212_prediction_share
        return share if strategy == "daily" else 1 - share

    def _risk(self, account, positions, orders, reconciled, now, rules):
        """Check every owned position before either book can spend a snapshot."""
        acted, protected = False, True
        # A halt in one strategy does not silently enable or disable the other.
        for mode, book in self.books.items():
            peak = max(account["equity"], book.get("equity_peak") or 0)
            book.set("equity_peak", peak)
            limit = rules.get("max_drawdown") if mode == "loop" else self.config.max_drawdown * 100
            drawdown = (peak - account["equity"]) / peak * 100 if peak else 0
            if limit and drawdown >= limit:
                book.set("halt", {"at": utcnow(), "drawdown": drawdown, "limit": limit, "peak": peak})
                book.set("entries_enabled", False)
        day = str(now.date())
        reference = self.store.root.get("broker_daily_reference") or {}
        if reference.get("day") != day:
            reference = {"day": day, "equity": account["equity"]}
            self.store.root.set("broker_daily_reference", reference)
        loss = reference["equity"] > 0 and account["equity"] <= reference["equity"] * (1 - self.config.daily_loss)
        if loss:
            self.store.root.set("broker_daily_loss_day", day)
            for book in self.books.values():
                book.set("entries_enabled", False)
        loss = loss or self.store.root.get("broker_daily_loss_day") == day
        self.store.root.set("broker_loss_halt", bool(loss))
        pending = {o.get("broker_id"): o for o in self.store.unresolved_orders()}
        for order in orders:
            record = pending.get(order["id"])
            if not record or order["side"] != "buy" or order["status"] == "pending_cancel":
                continue
            mode = record.get("strategy", "loop")
            book = self.books.get(mode)
            if book is None:
                continue
            session = self.broker.session_for(order["symbol"], now)
            created = pd.Timestamp(order["created_at"]) if order.get("created_at") else None
            expired = (not entry_window(session, now) or created is None or created.tzinfo is None
                       or now - created >= BUY_TIMEOUT)
            if not book.get("entries_enabled", False) or book.get("halt") or loss or expired:
                self.broker.cancel(order["id"])
                acted = True
                break
        records = self.store.orders(1000)
        for position in positions:
            symbol = position["symbol"]
            mode = self.store.owner(symbol)
            book = self.books.get(mode)
            if book is None:
                continue
            if not self.owned_quantity(symbol, mode):
                protected = False
                continue
            session = self.broker.session_for(symbol, now)
            stop = rules.get("stop_loss", self.config.stop_loss * 100) / 100 if mode == "loop" else self.config.stop_loss
            stopped = position["current_price"] <= position["avg_entry_price"] * (1 - stop)
            failed = any(o["symbol"] == symbol and o.get("type") == "stop" and o["status"] == "rejected"
                         and o.get("strategy", "loop") == mode and o.get("position_generation", "legacy") ==
                         book.get(f"position_generation:{symbol}", "legacy") for o in records)
            reason = book.get(f"exit_reason:{symbol}")
            if failed:
                reason = "Broker stop protection rejected; closing this strategy's filled shares"
            elif loss:
                reason = "Trading 212 account daily-loss limit reached"
            elif book.get("halt"):
                reason = "Account drawdown limit reached"
            elif stopped:
                reason = "Position stop-loss reached"
            elif mode == "loop" and not loop.owner_version(book, symbol):
                reason = "Trading-loop strategy deactivated"
            elif mode == "daily" and position["current_price"] >= position["avg_entry_price"] * (1 + self.config.take_profit):
                reason = "Prediction position take-profit reached"
            if reason:
                book.set(f"exit_reason:{symbol}", reason)
                if session["open"] and not acted and reconciled:
                    acted = self.close(position, orders, reason, mode)
                protected = False
            elif not acted and reconciled:
                safe, changed = self.protect(position, orders, stop, mode)
                acted = acted or changed
                protected = protected and safe
        held = {p["symbol"] for p in positions if p["qty"] > 0}
        pending_symbols = {o["symbol"] for o in self.store.unresolved_orders()}
        for mode, book in self.books.items():
            for symbol in book.owned_symbols():
                if symbol in pending_symbols:
                    continue
                if symbol not in held or self.owned_quantity(symbol, mode) == 0:
                    if symbol not in held and self.owned_quantity(symbol, mode) > 0:
                        observed = book.get(f"flat_observed:{symbol}")
                        if observed is None:
                            book.set(f"flat_observed:{symbol}", time.time())
                            protected = False
                            continue
                        if time.time() - observed < 30:
                            protected = False
                            continue
                    book.set(f"owned:{symbol}", False)
                    book.set(f"exit_reason:{symbol}", None)
                    book.set(f"flat_observed:{symbol}", None)
                else:
                    book.set(f"flat_observed:{symbol}", None)
        return acted, protected

    def _decision(self, mode, symbol, choice, specs, account, positions, orders, now, *, ready, acted, rules):
        book = self.books[mode]
        position = next((p for p in positions if p["symbol"] == symbol and p["qty"] > 0), None)
        owned = book.get(f"owned:{symbol}", False)
        action, reason = "WAIT", choice.get("reason", "Waiting for a signal")
        try:
            if position and not owned:
                reason = f"Position belongs to {self.store.owner(symbol) or 'the account owner'}; signals only"
            elif book.get(f"exit_reason:{symbol}") and position and owned:
                action, reason = "SELL", book.get(f"exit_reason:{symbol}")
            elif symbol not in specs:
                reason = "Map this listing to matching daily data before trading"
            else:
                spec = specs[symbol]
                instrument = self.broker.instrument(symbol)
                self.data.verify({"symbol": spec["symbol"], "mic_code": spec["mic"], "currency": spec["currency"]}, spec, instrument)
                session = self.broker.session_for(symbol, now)
                opening = session["opening"]
                target = choice.get("target_session")
                zone = choice.get("market_timezone", "UTC")
                valid_day = target is not None and opening is not None and str(opening.tz_convert(zone).date()) == target
                signature = choice.get("source_signature")
                source_ok = signature is None or signature == source_signature(self.config, spec)
                signal = choice.get("signal", "HOLD")
                if position and owned:
                    action = "HOLD"
                    if signal == "SELL" and valid_day and source_ok:
                        action = "SELL"
                        if not acted and ready and session["open"]:
                            acted = self.close(position, orders, reason, mode)
                elif signal == "BUY":
                    if not book.get("entries_enabled", False):
                        reason = f"Trading 212 {mode} entries are paused"
                    elif book.get("halt") or self.store.root.get("broker_loss_halt") or mode == "loop" and not rules["ok"]:
                        reason = "This strategy's risk rules block new entries"
                    elif not source_ok or book.get("data_errors", {}).get(symbol):
                        reason = book.get("data_errors", {}).get(symbol) or "Listing/data mapping changed; waiting for a fresh evaluation"
                    elif not ready:
                        reason = "Waiting for reconciliation and broker stop protection"
                    elif any(o["side"] == "buy" for o in [*orders, *self.store.unresolved_orders()]):
                        reason = "A buy is still pending; waiting for a fresh position snapshot"
                    elif self.store.owner(symbol) not in {None, mode}:
                        reason = "Another Trading 212 strategy reserved this symbol"
                    elif not session["open"]:
                        reason = "This listing's exchange is closed"
                    elif not valid_day:
                        reason = "Waiting for a signal from the latest completed exchange session"
                    elif not entry_window(session, now):
                        reason = "Outside this listing's regular-session entry window"
                    elif self.strategy_share(mode) <= 0:
                        reason = "This strategy has a zero budget share"
                    elif not acted:
                        price = self.data.quote(instrument, spec, pd.Timestamp.now(tz="UTC"))
                        fx = self.data.execution_fx(instrument["currencyCode"], account["currency"], pd.Timestamp.now(tz="UTC"))
                        capital = account["equity"] * self.config.trading212_capital_fraction
                        if self.config.trading212_capital_cap:
                            capital = min(capital, self.config.trading212_capital_cap)
                        own_capital = capital * self.strategy_share(mode)
                        gross = sum(abs(p["market_value"]) for p in positions)
                        own_gross = sum(abs(p["market_value"]) for p in positions if self.store.owner(p["symbol"]) == mode)
                        budget = min(own_capital * self.config.position_fraction, own_capital * self.config.gross_fraction - own_gross,
                                     capital * self.config.gross_fraction - gross, account["cash"])
                        qty = math.floor(max(0, budget) / (price * fx * 1.01))
                        if instrument.get("maxOpenQuantity") is not None:
                            qty = min(qty, math.floor(number(instrument["maxOpenQuantity"], nonnegative=True)))
                        if qty > 0:
                            slot = now.floor("15min").isoformat()
                            changed = self.send(symbol, "buy", qty, "limit", f"entry:{target}:{choice.get('bar_at')}:{slot}",
                                price=round(price, 2), version=choice.get("version") or choice.get("model_version"), strategy=mode)
                            if changed:
                                action, acted = "BUY", True
                                reason += "; order intent journaled; acceptance is not a fill"
                            else:
                                reason = "An order was already attempted for this signal and time slot"
                        else:
                            reason = "This account/strategy's cash or exposure limit leaves no whole-share budget"
                elif position and owned:
                    action = "HOLD"
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            reason = str(exc)[:240]
        forecast = {**choice, "action": action, "reason": reason, "decision_at": utcnow()}
        book.set(f"forecast:{symbol}", forecast)
        row = {"broker": "trading212", "strategy": mode, "symbol": symbol, "version": choice.get("version"),
               "test": choice.get("test"), "signal": choice.get("signal"), "action": action, "reason": reason,
               "views": choice.get("views", {}), "models": choice.get("models", []),
               "expected_return": choice.get("expected_return"), "eligible": choice.get("eligible"),
               "quality": choice.get("quality"), "owner": loop.owner_version(book, symbol) if mode == "loop" and owned else None}
        return row, acted

    def cycle(self, now=None):
        now = now or pd.Timestamp.now(tz="UTC")
        if not self.config.trading212_configured:
            self.store.set("engine", {"status": "setup", "message": "Add Trading 212 API key and secret", "at": utcnow()})
            return
        account = self.broker.account()
        self.scope(account)
        positions, orders = self.broker.positions(), self.broker.open_orders()
        self.store.set("account", {**{k: v for k, v in account.items() if k != "id"}, "positions": positions,
                                  "broker": "trading212", "at": utcnow()})
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
        acted, protected = self._risk(account, positions, orders, reconciled, now, safe_rules)
        specs = mappings(self.config)
        symbols = sorted(set(specs) | {p["symbol"] for p in positions})
        modes = ["loop", "daily"] if self.turn % 2 == 0 else ["daily", "loop"]
        self.turn += 1
        plans = []
        for mode in modes:
            self.books[mode].set("monitor_symbols", symbols)
            for symbol in symbols:
                choice = loop.combine(self.store, symbol, self.cfg) if mode == "loop" else self.predictions.decision(symbol, now)
                plans.append((mode, symbol, choice))
        # Discretionary sales also get priority over either strategy's new entries.
        plans.sort(key=lambda p: p[2].get("signal") != "SELL")
        rows = {mode: [] for mode in modes}
        for mode, symbol, choice in plans:
            row, acted = self._decision(mode, symbol, choice, specs, account, positions, orders, now,
                                       ready=reconciled and protected, acted=acted, rules=rules)
            rows[mode].append(row)
        for mode, book in self.books.items():
            book.set("stocks", rows[mode])
        # Background work remains independent of order changes and risk polling.
        self.signals_tick(now, specs)
        self.store.set("engine", {"status": "running" if reconciled else "blocked",
            "message": ("Parallel daily predictions and daily strategy loop on non-US listings" if specs else
                        "Add Trading 212 instrument mappings; balances remain available") if reconciled else
                       "Unresolved order outcome; no automatic retry", "at": utcnow()})

    def run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.store.set("engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
            self.stop.wait(15)
