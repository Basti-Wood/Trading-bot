"""Coordinate three stock strategies under shared account/order limits."""
from dataclasses import replace
import threading

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
            "message": "Strategy error; check decision logs" if failed else
                       "Prediction, day trading and trading-loop running together; shared account limits",
            "at": utcnow()})

    def run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.store.set("portfolio_engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
            self.stop.wait(self.config.poll_seconds)
