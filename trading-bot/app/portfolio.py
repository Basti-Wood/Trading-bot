"""Run the daily, intraday and trading-loop strategies under one account/order coordinator.

The user's own (manual) trades share the account but sit outside the bots' budget.
"""
from dataclasses import replace
import threading
import time

from . import loop
from .allocation import bot_share
from .engine import Engine
from .store import StrategyStore, utcnow

STRATEGIES = ("daily", "daytrade", "loop")


def strategy_contexts(config, store):
    if config.trading_mode != "both":
        return [(config, store)]
    contexts = []
    for mode in STRATEGIES:
        # strategy_fraction is the startup weight; sizing reads the live dashboard weights.
        cfg = replace(config, trading_mode=mode, coordinated=True)
        if mode == "loop":
            # Per-symbol tests must see the stress test's window, not the prediction bot's training history.
            cfg = replace(cfg, history_days=loop.HISTORY_DAYS)
        contexts.append((replace(cfg, strategy_fraction=max(.05, bot_share(cfg, store, mode))), StrategyStore(store, mode)))
    return contexts


def release_manual(store, positions, orders):
    """Hand a fully sold manual symbol back to the bots after two flat observations."""
    for symbol in store.owned_symbols("manual"):
        busy = (any(p["symbol"] == symbol and float(p.get("qty") or 0) for p in positions)
                or any(o["symbol"] == symbol for o in orders)
                or any(o["symbol"] == symbol and o.get("strategy") == "manual" for o in store.unresolved_orders()))
        if busy:
            store.set(f"manual_flat:{symbol}", None)
            continue
        seen = store.get(f"manual_flat:{symbol}")
        if seen is None:
            store.set(f"manual_flat:{symbol}", time.time())
        elif time.time() - seen >= 15:
            store.set_owned(symbol, "manual", False)
            store.set(f"manual_flat:{symbol}", None)


class PortfolioEngine:
    def __init__(self, config, store):
        if config.trading_mode != "both":
            raise ValueError("PortfolioEngine requires TRADING_MODE=both")
        # The existing daily namespace/journal stays intact; intraday state gets
        # its own namespace in the same database. Never reinterpret a standalone
        # daytrade journal as a daily book.
        if store.get("trading_mode", "daily") != "daily":
            raise ValueError("Use your daily DATA_DIR for parallel mode; this directory is a standalone daytrade journal")
        self.config, self.store = config, store
        self.engines = [Engine(cfg, state) for cfg, state in strategy_contexts(config, store)]
        self.broker = self.engines[0].broker
        self.research_lock = threading.Lock()
        for engine in self.engines:
            engine.research_lock = self.research_lock
        self.stop = threading.Event()
        self.thread = self.monitor = None
        self.turn = self.training_turn = 0
        store.set("parallel_enabled", True)

    def start(self):
        self.monitor = threading.Thread(target=self.monitor_training, name="portfolio-training", daemon=True)
        self.thread = threading.Thread(target=self.run, name="portfolio-engine", daemon=True)
        self.monitor.start()
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        for engine in self.engines:
            engine.stop.set()
        if self.monitor:
            self.monitor.join(timeout=3)
        if self.thread:
            self.thread.join(timeout=3)
        for engine in self.engines:
            engine.shutdown()

    def training_tick(self):
        # Watchdog every existing worker, then admit at most one training process.
        for engine in self.engines:
            engine.training_tick(allow_start=False)
        if any(e.trainer is not None for e in self.engines):
            return
        sequence = self.engines[self.training_turn:] + self.engines[:self.training_turn]
        for engine in sequence:
            engine.training_tick()
            if engine.trainer is not None:
                self.training_turn = (self.engines.index(engine) + 1) % len(self.engines)
                break

    def monitor_training(self):
        while not self.stop.is_set():
            try:
                self.training_tick()
            except Exception as exc:
                self.store.event("error", {"message": str(exc)[:240], "component": "portfolio training"})
            self.stop.wait(2)

    def cycle(self):
        if not self.config.configured:
            for engine in self.engines:
                engine.cycle()
            self.store.set("portfolio_engine", {"status": "setup", "message": "Configure Alpaca", "at": utcnow()})
            return
        reconciled = self.engines[0].executor.reconcile()
        account, clock = self.broker.account(), self.broker.clock()
        positions, orders = self.broker.positions(), self.broker.open_orders()
        snapshot = (account, clock, positions, orders, reconciled)
        # Open orders that are not bot entries, for the dashboard's "trade yourself" view.
        self.store.set("open_orders", [{k: o.get(k) for k in ("id", "client_order_id", "symbol", "side", "type", "qty",
                                        "notional", "limit_price", "stop_price", "status", "submitted_at")}
                                       for o in orders if not o.get("client_order_id", "").startswith("ensemble-")][:100])
        release_manual(self.store, positions, orders)
        sequence = self.engines[self.turn:] + self.engines[:self.turn]
        self.turn = (self.turn + 1) % len(self.engines)
        acted, failed = False, []
        # Check BOTH books' risk before either may enter. Once an order changes,
        # neither strategy may reuse the old account snapshot for another order.
        for phase in ("risk", "decisions"):
            for engine in sequence:
                if self.stop.is_set():
                    return
                try:
                    acted = engine.cycle(snapshot=snapshot, phase=phase, allow_action=not acted) or acted
                except Exception as exc:
                    acted = True  # An interrupted mutation may have reached Alpaca.
                    failed.append(engine.config.trading_mode)
                    engine.store.set("engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
                    engine.store.event("error", {"message": str(exc)[:240]})
        self.store.set("portfolio_engine", {"status": "error" if failed else "running",
            "message": "Strategy error; check decision logs" if failed else "Prediction, day trading and trading-loop running together; shared account limits",
            "at": utcnow()})

    def run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.store.set("portfolio_engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
            self.stop.wait(self.config.poll_seconds)
