"""Independent broker workers: one broker's outage cannot stop the other's exits."""
from .accounts import Accounts
from .engine import Engine
from .portfolio import PortfolioEngine
from .trading212_engine import Trading212Engine


class MultiBrokerEngine:
    def __init__(self, config, store):
        self.primary = PortfolioEngine(config, store) if config.trading_mode == "both" else Engine(config, store)
        self.secondary = Trading212Engine(config, store) if config.trading212_enabled else None
        if self.secondary:
            self.primary.training_peers.append(self.secondary.predictions)
        self.accounts = Accounts(config, store, trading212=self.secondary.broker if self.secondary else None)

    def start(self):
        self.primary.start()
        if self.secondary:
            self.secondary.start()
        self.accounts.start()

    def shutdown(self):
        self.primary.shutdown()
        if self.secondary:
            self.secondary.shutdown()
        self.accounts.shutdown()
