from datetime import datetime, timezone
import hashlib
import multiprocessing as mp
import threading
import time
from zoneinfo import ZoneInfo
import psutil
import pandas as pd

from .broker import Alpaca
from .execution import Executor
from .models import train_worker, predict, strategy_signature
from .feedback import capture_forecast, resolve_outcomes
from .research import Research, combine
from .store import utcnow


class Engine:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.broker = Alpaca(config)
        self.executor = Executor(config, store, self.broker)
        self.stop = threading.Event()
        self.trainer = None
        self.training_started = 0
        self.research_thread = None
        self.thread = None
        self.monitor = None
        self.cached = {}
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

    def training_tick(self):
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
        last = self.store.get("last_training_attempt", 0)
        requested = self.store.get("train_requested", False)
        # Daily scheduled retraining happens outside regular market hours.
        local = datetime.now(ZoneInfo("America/New_York"))
        off_hours = local.weekday() >= 5 or not (9 <= local.hour < 17)
        due = time.time() - last > self.config.retrain_hours * 3600
        first = any(not self.store.get(f"model:{s}") or self.store.get(f"model:{s}").get("strategy_signature") != strategy_signature(self.config) for s in self.config.symbols)
        if self.config.configured and (requested or due and (off_hours or first)) and time.time() - last > 60:
            self.store.set("train_requested", False)
            self.store.set("last_training_attempt", time.time())
            self.store.set("training", {"status": "starting", "at": utcnow()})
            self.trainer = mp.get_context("spawn").Process(target=train_worker, args=(self.config,), daemon=True)
            self.trainer.start()
            self.training_started = time.monotonic()

    def research_tick(self, forecasts):
        if self.research_thread and self.research_thread.is_alive():
            return
        due = []
        for f in forecasts:
            last = self.store.get(f"research_attempt:{f['symbol']}", 0)
            if time.time() - last > 1800:
                due.append(f)
        if not due or not self.config.llm_key:
            if not self.config.llm_key:
                self.store.set("research_status", {"status": "waiting for FREELLM_API_KEY"})
            return
        def work():
            research = Research(self.config, self.store)
            for forecast in due:
                if self.stop.is_set():
                    break
                symbol = forecast["symbol"]
                self.store.set(f"research_attempt:{symbol}", time.time())
                try:
                    research.analyze(symbol, forecast)
                except Exception as exc:
                    # requests exceptions can contain URLs; never log headers or raw payloads.
                    message = str(exc)[:240] if isinstance(exc, ValueError) else "News/FreeLLM connection failed"
                    self.store.set(f"research:{symbol}", {"status": "unavailable", "at": utcnow(), "message": message})
                    self.store.event("research", {"symbol": symbol, "status": "unavailable", "message": message})
            self.store.set("research_status", {"status": "idle", "at": utcnow()})
        self.research_thread = threading.Thread(target=work, name="news-research", daemon=True)
        self.research_thread.start()

    def cycle(self):
        if not self.config.configured:
            self.store.set("engine", {"status": "setup", "message": "Add Alpaca and FreeLLM credentials to .env", "at": utcnow()})
            return
        account, clock = self.broker.account(), self.broker.clock()
        positions, orders = self.broker.positions(), self.broker.open_orders()
        reconciled = self.executor.reconcile()
        equity = float(account["equity"])
        prior = float(account.get("last_equity") or equity)
        loss_breach = prior > 0 and equity <= prior * (1 - self.config.daily_loss)
        day = pd.Timestamp(clock["timestamp"]).tz_convert("America/New_York").date().isoformat()
        if loss_breach:
            self.store.set("loss_halt_day", day)
            self.store.set("entries_enabled", False)
        loss_breach = loss_breach or self.store.get("loss_halt_day") == day
        self.store.set("account", {"equity": equity, "cash": float(account["cash"]), "buying_power": float(account["buying_power"]),
            "daily_pnl": equity - prior, "positions": [{k: p.get(k) for k in ("symbol", "qty", "market_value", "unrealized_pl", "avg_entry_price")} for p in positions],
            "market_open": clock["is_open"], "loss_halt": loss_breach, "at": utcnow()})
        if time.time() - self.store.get("equity_sample", 0) >= 900:
            self.store.event("equity", {"equity": equity, "daily_pnl": equity - prior})
            self.store.set("equity_sample", time.time())
        local = pd.Timestamp(clock["timestamp"]).tz_convert("America/New_York")
        entry_window = local.hour == 9 and 35 <= local.minute <= 59
        # Pausing entries or leaving the entry window cancels this bot's unfilled buys.
        if not self.store.get("entries_enabled", False) or loss_breach or not entry_window:
            for order in orders:
                if order["side"] == "buy" and order.get("client_order_id", "").startswith("ensemble-"):
                    self.broker.cancel(order["id"])
        forecasts = []
        # One order-changing action per cycle; refresh all broker state before another.
        acted = False
        for symbol in self.config.symbols:
            pos = next((p for p in positions if p["symbol"] == symbol), None)
            if not pos and not any(o["symbol"] == symbol for o in orders):
                self.store.set(f"owned:{symbol}", False)
            force_exit = bool(self.store.get(f"exit_pending:{symbol}", False))
            if pos and self.store.get(f"owned:{symbol}", False):
                pnl_fraction = float(pos.get("unrealized_plpc") or 0)
                force_exit = force_exit or pnl_fraction <= -self.config.stop_loss or loss_breach
            if force_exit and not pos:
                self.store.set(f"exit_pending:{symbol}", False)
                force_exit = False
            if force_exit and clock["is_open"] and not acted:
                self.store.set(f"exit_pending:{symbol}", True)
                reason = self.executor.execute(symbol, "SELL", day, account, positions, orders, loss_breach=loss_breach, force_exit=True)
                self.store.event("decision", {"symbol": symbol, "action": "SELL", "reason": reason, "risk_exit": True})
                acted = True
            meta = self.store.get(f"model:{symbol}")
            if not meta:
                continue
            try:
                cached = self.cached.get(symbol)
                if not cached or cached["day"] != day or cached["version"] != meta["version"]:
                    bars = self.broker.bars(symbol)
                    resolve_outcomes(self.config, self.store, symbol, bars)
                    cached = {"day": day, "version": meta["version"], "bars": bars, "forecast": predict(self.config, meta, bars)}
                    capture_forecast(self.config, self.store, cached["forecast"])
                    self.cached[symbol] = cached
                forecast = dict(cached["forecast"])
                research = self.store.get(f"research:{symbol}")
                action, reason = combine(forecast, research)
                # Seven calendar days allows long weekends while refusing stale feeds/models.
                age = (pd.Timestamp.now(tz="UTC") - cached["bars"].index[-1]).total_seconds()
                model_age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(meta["data_through"])).days
                if meta.get("strategy_signature") != strategy_signature(self.config):
                    action, reason = "HOLD", "Settings changed; retraining is required before using this model"
                elif age > 7 * 86400 or model_age > 14:
                    action, reason = "HOLD", "Market data or model is too old"
                elif not clock["is_open"]:
                    reason = "Market closed; " + reason
                elif not reconciled:
                    reason = "Unresolved order outcome; new decisions blocked"
                elif not acted and action in {"BUY", "SELL"} and (action == "SELL" or entry_window):
                    if action == "SELL" and pos and self.store.get(f"owned:{symbol}", False):
                        self.store.set(f"exit_pending:{symbol}", True)
                    reason = self.executor.execute(symbol, action, day, account, positions, orders, loss_breach=loss_breach)
                    acted = True
                elif action == "BUY" and not entry_window:
                    reason = "Buy window is 09:35-09:59 New York time"
                forecast.update({"action": action, "reason": reason, "research": research})
                forecasts.append(forecast)
                previous = self.store.get(f"forecast:{symbol}", {})
                if previous.get("action") != action or previous.get("reason") != reason:
                    self.store.event("decision", {"symbol": symbol, "action": action, "reason": reason})
                self.store.set(f"forecast:{symbol}", forecast)
            except Exception as exc:
                self.store.event("error", {"symbol": symbol, "message": str(exc)[:240]})
        self.research_tick(forecasts)
        self.store.set("engine", {"status": "running", "message": "Monitoring market, models and risk limits", "at": utcnow()})

    def run(self):
        next_cycle = 0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_cycle:
                    self.cycle()
                    next_cycle = time.monotonic() + 60
            except Exception as exc:
                self.store.set("engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
                next_cycle = time.monotonic() + 60
            self.stop.wait(2)
