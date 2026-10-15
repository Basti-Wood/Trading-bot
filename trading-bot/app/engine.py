from datetime import datetime, timezone
import hashlib
import multiprocessing as mp
import threading
import time
from zoneinfo import ZoneInfo
from contextlib import nullcontext
import psutil
import pandas as pd

from .broker import Alpaca
from .execution import Executor
from .decisions import position_context, position_decision
from .models import train_worker, predict, strategy_signature
from .feedback import capture_forecast, resolve_outcomes
from .research import Research, combine
from .store import utcnow
from .universe import active_symbols, monitored_symbols, scan, entries_ready


class Engine:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.broker = Alpaca(config)
        self.executor = Executor(config, store, self.broker)
        self.stop = threading.Event()
        self.trainer = None
        self.training_started = 0
        self.research_thread = None
        self.research_lock = None
        self.thread = None
        self.monitor = None
        self.cached = {}
        self.forecast_attempts = {}
        self.scan_thread = None
        self.forecast_thread = None
        self.last_scan = 0
        previous_mode = store.get("trading_mode", "daily")
        if not config.coordinated and store.get("parallel_enabled", False):
            raise ValueError("This journal contains parallel strategies; keep TRADING_MODE=both to monitor both")
        if not config.coordinated and previous_mode != config.trading_mode and (store.get("account_scope") or store.orders(1)):
            raise ValueError("Strategy changed: use a separate DATA_DIR for daily and daytrade modes")
        store.set("trading_mode", config.trading_mode)
        scope = hashlib.sha256((self.broker.base + config.api_key).encode()).hexdigest()
        previous_scope = store.get("account_scope")
        if previous_scope and previous_scope != scope:
            raise ValueError("Alpaca account/mode changed: use a separate DATA_DIR to preserve reconciliation history")
        if config.configured:
            store.set("account_scope", scope)
        # A restart always requires deliberate re-enabling of new entries.
        store.set("entries_enabled", False)

    def start(self):
        self.monitor = threading.Thread(target=self.monitor_training, name="training-watchdog", daemon=True)
        self.monitor.start()
        self.thread = threading.Thread(target=self.run, name="trading-engine", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.monitor:
            self.monitor.join(timeout=3)
        if self.trainer and self.trainer.is_alive():
            self.trainer.terminate()
            self.trainer.join(timeout=5)
        if self.thread:
            self.thread.join(timeout=3)

    def monitor_training(self):
        while not self.stop.is_set():
            try:
                self.training_tick()
            except Exception as exc:
                self.store.set("training", {"status": "failed", "message": str(exc)[:240], "at": utcnow()})
            self.stop.wait(2)

    def training_tick(self, allow_start=True):
        if self.trainer:
            if self.trainer.is_alive():
                try:
                    rss = psutil.Process(self.trainer.pid).memory_info().rss / 1024**2
                except psutil.NoSuchProcess:
                    return
                if rss > self.config.training_memory_mb or time.monotonic() - self.training_started > self.config.training_timeout:
                    self.trainer.terminate()
                    self.trainer.join(timeout=5)
                    self.store.set("training", {"status": "resource limit reached", "at": utcnow()})
                return
            self.trainer.join(timeout=1)
            if self.trainer.exitcode and self.store.get("training", {}).get("status") == "running":
                self.store.set("training", {"status": "worker failed", "at": utcnow()})
            self.trainer = None
        if not allow_start:
            return
        last = self.store.get("last_training_attempt", 0)
        requested = self.store.get("train_requested", False)
        # Daily scheduled retraining happens outside regular market hours.
        local = datetime.now(ZoneInfo("America/New_York"))
        off_hours = local.weekday() >= 5 or not (9 <= local.hour < 17)
        due = time.time() - self.store.get("last_full_training", 0) > self.config.retrain_hours * 3600
        symbols = self.store.get("monitor_symbols", active_symbols(self.config, self.store))
        missing = [s for s in symbols if (not self.store.get(f"model:{s}") or self.store.get(f"model:{s}").get("strategy_signature") != strategy_signature(self.config))
                   and time.time() - self.store.get(f"training_attempt:{s}", 0) > 3600]
        if self.config.configured and symbols and (requested or missing or due and off_hours) and time.time() - last > 60:
            self.store.set("train_requested", False)
            self.store.set("last_training_attempt", time.time())
            self.store.set("training", {"status": "starting", "at": utcnow()})
            targets = symbols if requested or due and off_hours else missing
            if requested or due and off_hours:
                self.store.set("last_full_training", time.time())
            self.trainer = mp.get_context("spawn").Process(target=train_worker, args=(self.config, targets), daemon=True)
            self.trainer.start()
            self.training_started = time.monotonic()

    def research_tick(self, forecasts):
        if self.research_thread and self.research_thread.is_alive():
            return
        due = []
        for f in forecasts:
            last = self.store.get(f"research_attempt:{f['symbol']}", 0)
            if time.time() - last > (300 if self.config.intraday else 1800):
                due.append(f)
        if not due or not self.config.llm_key:
            if not self.config.llm_key:
                self.store.set("research_status", {"status": "news disabled; prediction models decide"})
            return
        due.sort(key=lambda f: (not self.store.get(f"owned:{f['symbol']}", False), f["forecast_signal"] != "BUY", -f["expected_return"]))
        if self.config.intraday:
            due = due[:5]
        def work():
            research = Research(self.config, self.store)
            for forecast in due:
                if self.stop.is_set():
                    break
                symbol = forecast["symbol"]
                self.store.set(f"research_attempt:{symbol}", time.time())
                try:
                    with self.research_lock if self.research_lock is not None else nullcontext():
                        if self.stop.is_set():
                            break
                        research.analyze(symbol, forecast)
                except Exception as exc:
                    # requests exceptions can contain URLs; never log headers or raw payloads.
                    message = str(exc)[:240] if isinstance(exc, ValueError) else "News/FreeLLM connection failed"
                    self.store.set(f"research:{symbol}", {"status": "unavailable", "at": utcnow(), "message": message})
                    self.store.event("research", {"symbol": symbol, "status": "unavailable", "message": message})
            self.store.set("research_status", {"status": "idle", "at": utcnow()})
        self.research_thread = threading.Thread(target=work, name="news-research", daemon=True)
        self.research_thread.start()

    def scan_tick(self):
        if self.config.coordinated and self.config.intraday:
            return
        if self.config.universe_mode != "market" or self.scan_thread and self.scan_thread.is_alive():
            return
        if time.time() - self.last_scan < self.config.scan_interval_minutes * 60:
            return
        self.last_scan = time.time()
        def work():
            try:
                scan(self.config, self.store, Alpaca(self.config), self.stop)
            except Exception:
                self.store.set("scan_status", {"status": "unavailable", "message": "Market scan failed; will retry", "at": utcnow()})
        self.scan_thread = threading.Thread(target=work, name="market-scanner", daemon=True)
        self.scan_thread.start()

    def forecast_tick(self, symbols, day):
        if self.forecast_thread and self.forecast_thread.is_alive():
            return
        # Refresh daily inputs after session close as well as before the open.
        slot = int(time.time() // (300 if self.config.intraday else 1800))
        # Bound the in-memory cache even as discovery rotates through new stocks.
        self.cached = {s: v for s, v in self.cached.items() if s in symbols}
        self.forecast_attempts = {s: v for s, v in self.forecast_attempts.items() if s in symbols}
        def work():
            broker = Alpaca(self.config)
            for symbol in symbols:
                if self.stop.is_set():
                    return
                meta = self.store.get(f"model:{symbol}")
                if not meta or meta.get("strategy_signature") != strategy_signature(self.config):
                    continue
                key = (slot, meta["version"])
                if self.cached.get(symbol) == key:
                    continue
                if time.time() - self.forecast_attempts.get(symbol, 0) < (30 if self.config.intraday else 60):
                    continue
                self.forecast_attempts[symbol] = time.time()
                try:
                    bars = broker.bars(symbol)
                    resolve_outcomes(self.config, self.store, symbol, bars)
                    forecast = predict(self.config, meta, bars)
                    capture_forecast(self.config, self.store, forecast)
                    self.store.set(f"prediction:{symbol}", forecast)
                    expected = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta(minutes=5)
                    if not self.config.intraday or pd.Timestamp(forecast["bar_at"]) == expected:
                        self.cached[symbol] = key
                except Exception as exc:
                    self.store.event("error", {"symbol": symbol, "message": str(exc)[:240]})
        self.forecast_thread = threading.Thread(target=work, name="forecasts", daemon=True)
        self.forecast_thread.start()

    def forecast_decision(self, forecast, now):
        meta = self.store.get(f"model:{forecast['symbol']}")
        if not meta or meta.get("strategy_signature") != strategy_signature(self.config) or forecast["model_version"] != meta["version"]:
            return "HOLD", "Settings or model changed; awaiting a current forecast"
        age = (now - pd.Timestamp(forecast["bar_at"])).total_seconds()
        model_age = (now - pd.Timestamp(meta["data_through"])).total_seconds()
        if not (300 <= age <= 390 if self.config.intraday else 0 <= age <= 7 * 86400) or not 0 <= model_age <= (3 if self.config.intraday else 14) * 86400:
            return "HOLD", "Market data or model is too old for this horizon"
        return combine(forecast, self.store.get(f"research:{forecast['symbol']}"))

    def queue_session(self, clock, now):
        if self.config.intraday or not self.config.queue_daily_orders or clock["is_open"]:
            return None
        try:
            opening = pd.Timestamp(clock["next_open"])
            if opening.tzinfo is not None and 0 < (opening - now).total_seconds() <= 7 * 86400:
                return opening.tz_convert("America/New_York").date().isoformat()
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def cycle(self, snapshot=None, phase="all", allow_action=True):
        if not self.config.configured:
            self.store.set("engine", {"status": "setup", "message": "Add Alpaca credentials to .env", "at": utcnow()})
            return False
        if snapshot is None:
            account, clock = self.broker.account(), self.broker.clock()
            positions, orders = self.broker.positions(), self.broker.open_orders()
            reconciled = self.executor.reconcile()
        else:
            account, clock, positions, orders, reconciled = snapshot
        equity = float(account["equity"])
        prior = float(account.get("last_equity") or equity)
        loss_breach = prior > 0 and equity <= prior * (1 - self.config.daily_loss)
        day = pd.Timestamp(clock["timestamp"]).tz_convert("America/New_York").date().isoformat()
        if loss_breach:
            self.store.set("loss_halt_day", day)
            self.store.set("entries_enabled", False)
        loss_breach = loss_breach or self.store.get("loss_halt_day") == day
        self.store.set("account", {"equity": equity, "cash": float(account["cash"]), "buying_power": float(account["buying_power"]),
            "daily_pnl": equity - prior, "positions": [{k: p.get(k) for k in ("symbol", "qty", "market_value", "unrealized_pl", "unrealized_plpc", "avg_entry_price")} for p in positions],
            "market_open": clock["is_open"], "loss_halt": loss_breach, "at": utcnow()})
        if time.time() - self.store.get("equity_sample", 0) >= 900:
            self.store.event("equity", {"equity": equity, "daily_pnl": equity - prior})
            self.store.set("equity_sample", time.time())
        now = pd.Timestamp(clock["timestamp"])
        local = now.tz_convert("America/New_York")
        queue_session = self.queue_session(clock, now)
        entry_window = local.hour == 9 and 35 <= local.minute <= 59
        flatten = False
        if self.config.intraday:
            close = pd.Timestamp(clock["next_close"]) if clock.get("next_close") else None
            valid_close = close is not None and close.tzinfo is not None and close.tz_convert("America/New_York").date() == local.date()
            remaining = (close - now).total_seconds() / 60 if valid_close else 0
            flatten = bool(clock["is_open"] and (not valid_close or remaining <= self.config.daytrade_close_minutes))
            entry_window = (clock["is_open"] and (local.hour, local.minute) >= (9, 40)
                            and remaining > self.config.daytrade_close_minutes + self.config.daytrade_hold_minutes)
        acted = not allow_action
        symbols = monitored_symbols(self.config, self.store, positions, orders)
        self.store.set("monitor_symbols", symbols)
        if phase != "decisions":
            for order in orders:
                if acted or order["side"] != "buy" or not order.get("client_order_id", "").startswith("ensemble-"):
                    continue
                if self.config.coordinated and not self.store.owns_order(order):
                    continue
                record = self.store.order(order.get("client_order_id", "")) or {}
                queued = record.get("queued_for_open", False) and not self.config.intraday
                keep_queued, invalid_queue = False, False
                if queued:
                    target = record.get("session")
                    keep_queued = (self.config.queue_daily_orders and
                                   (queue_session is not None and target == queue_session
                                    or clock["is_open"] and target == day and local.hour == 9))
                    prediction = self.store.get(f"prediction:{order['symbol']}")
                    invalid_queue = (not prediction or prediction.get("target_session") != target
                                     or self.forecast_decision(prediction, now)[0] != "BUY"
                                     or order["symbol"] not in active_symbols(self.config, self.store)
                                     or not entries_ready(self.config, self.store))
                expired = False
                if self.config.intraday:
                    submitted = order.get("submitted_at") or order.get("created_at")
                    expired = not submitted or (now - pd.Timestamp(submitted)).total_seconds() > 60
                if (not self.store.get("entries_enabled", False) or loss_breach
                        or not (keep_queued if queued else entry_window) or expired or invalid_queue):
                    self.broker.cancel(order["id"])
                    acted = True
            unresolved = self.store.unresolved_orders()
            # Risk exits run before predictions and news, including rotated-out holdings.
            for symbol in symbols:
                pos = next((p for p in positions if p["symbol"] == symbol), None)
                pending = any(o["symbol"] == symbol for o in [*orders, *unresolved])
                owned = self.store.get(f"owned:{symbol}", False)
                if not pos and not pending:
                    if owned and self.config.coordinated:
                        observed = self.store.get(f"flat_observed:{symbol}")
                        if observed is None:
                            self.store.set(f"flat_observed:{symbol}", time.time())
                            continue
                        if time.time() - observed < 15:
                            continue
                    if owned and self.config.intraday:
                        self.store.set(f"flat_since:{symbol}", time.time())
                    self.store.set(f"owned:{symbol}", False)
                    self.store.set(f"exit_pending:{symbol}", False)
                    self.store.set(f"exit_reason:{symbol}", None)
                    self.store.set(f"exit_status:{symbol}", None)
                elif self.config.coordinated:
                    self.store.set(f"flat_observed:{symbol}", None)
                exit_reason = (self.store.get(f"exit_reason:{symbol}") or "Completing the previously requested exit") if self.store.get(f"exit_pending:{symbol}", False) else None
                if pos and float(pos["qty"]) > 0 and owned:
                    pnl_fraction = float(pos.get("unrealized_plpc") or 0)
                    if loss_breach:
                        exit_reason = "Account daily-loss limit reached"
                    elif pnl_fraction <= -self.config.effective_stop:
                        exit_reason = "Position stop-loss reached"
                    elif flatten:
                        exit_reason = "Day-trading exit before market close"
                    if self.config.intraday:
                        started = self.store.get(f"daytrade_position:{symbol}", {}).get("started_at")
                        # Unknown holding age after recovery is an exit, never a new timer.
                        if not exit_reason and (not started or (now - pd.Timestamp(started)).total_seconds() >= self.config.daytrade_hold_minutes * 60):
                            exit_reason = "Day-trading holding period ended or holding age is unknown"
                if exit_reason and pos and float(pos["qty"]) > 0 and owned:
                    # Keep the exit visible even if another symbol used this snapshot.
                    self.store.set(f"exit_pending:{symbol}", True)
                    self.store.set(f"exit_reason:{symbol}", exit_reason)
                    if clock["is_open"] and not acted:
                        status = self.executor.execute(symbol, "SELL", day, account, positions, orders, loss_breach=loss_breach, force_exit=True)
                        self.store.set(f"exit_status:{symbol}", status)
                        self.store.event("decision", {"symbol": symbol, "action": "SELL", "reason": exit_reason + "; " + status, "risk_exit": True})
                        acted = bool(self.executor.changed)
        if phase == "risk":
            return acted
        forecasts = []
        for symbol in symbols:
            prediction = self.store.get(f"prediction:{symbol}")
            if prediction:
                forecasts.append(dict(prediction))
        # Consider sales first, then the highest predicted returns. A held stock,
        # exhausted allocation or duplicate intent must not starve other candidates.
        forecasts.sort(key=lambda f: (f["forecast_signal"] != "SELL", -f["expected_return"], f["symbol"]))
        candidates = set(active_symbols(self.config, self.store))
        for forecast in forecasts:
            symbol = forecast["symbol"]
            try:
                research = self.store.get(f"research:{symbol}")
                action, reason = self.forecast_decision(forecast, now)
                position = position_context(self.config, self.store, symbol, positions)
                action, reason = position_decision(action, reason, position)
                queued_buy = action == "BUY" and queue_session is not None
                exiting = position["owned"] and self.store.get(f"exit_pending:{symbol}", False)
                if exiting:
                    action = "SELL"
                    reason = (self.store.get(f"exit_reason:{symbol}") or "Completing the previously requested exit") + "; " + (self.store.get(f"exit_status:{symbol}") or "Exit pending")
                    if not clock["is_open"]:
                        reason = "Market closed; exit pending. " + reason
                elif not clock["is_open"] and not queued_buy:
                    reason = "Market closed; " + reason
                elif not reconciled:
                    reason = "Unresolved order outcome; new decisions blocked"
                elif action == "BUY" and (symbol not in candidates or not entries_ready(self.config, self.store)):
                    reason = "Waiting for a complete, fresh market scan and candidate selection"
                elif queued_buy and forecast.get("target_session") != queue_session:
                    reason = "Waiting for a completed daily forecast targeting the next market session"
                elif action == "BUY" and not entry_window and not queued_buy:
                    reason = "Outside the strategy entry window"
                elif not acted and action in {"BUY", "SELL"}:
                    if action == "SELL":
                        self.store.set(f"exit_pending:{symbol}", True)
                        self.store.set(f"exit_reason:{symbol}", reason)
                    queue_args = {"queued_for_open": True, "reference_price": forecast.get("last_close")} if queued_buy else {}
                    status = self.executor.execute(symbol, action, queue_session if queued_buy else day, account, positions, orders,
                                                   loss_breach=loss_breach, **queue_args)
                    if action == "SELL":
                        self.store.set(f"exit_status:{symbol}", status)
                    reason += "; " + status
                    acted = bool(self.executor.changed)
                forecast.update({"action": action, "reason": reason, "research": research,
                                 "position": position, "decision_at": utcnow()})
                previous = self.store.get(f"forecast:{symbol}", {})
                if previous.get("action") != action or previous.get("reason") != reason:
                    self.store.event("decision", {"symbol": symbol, "action": action, "reason": reason})
                self.store.set(f"forecast:{symbol}", forecast)
            except Exception as exc:
                self.store.event("error", {"symbol": symbol, "message": str(exc)[:240]})
        # Network-heavy scanning, forecasting and LLM calls cannot stall risk polling.
        self.scan_tick()
        self.forecast_tick(symbols, day)
        self.research_tick(forecasts)
        self.store.set("engine", {"status": "running", "message": f"Monitoring {len(symbols)} symbols in {self.config.trading_mode} mode", "at": utcnow()})

        return acted

    def run(self):
        next_cycle = 0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_cycle:
                    self.cycle()
                    next_cycle = time.monotonic() + self.config.poll_seconds
            except Exception as exc:
                self.store.set("engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
                next_cycle = time.monotonic() + self.config.poll_seconds
            self.stop.wait(2)
