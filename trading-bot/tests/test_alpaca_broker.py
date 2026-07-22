import sys
import types
import unittest
from unittest.mock import patch


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


alpaca_module = types.ModuleType("alpaca")
trading_module = types.ModuleType("alpaca.trading")
client_module = types.ModuleType("alpaca.trading.client")
requests_module = types.ModuleType("alpaca.trading.requests")
enums_module = types.ModuleType("alpaca.trading.enums")
client_module.TradingClient = object
requests_module.MarketOrderRequest = _Request
requests_module.StopOrderRequest = _Request
enums_module.OrderSide = types.SimpleNamespace(BUY="buy", SELL="sell")
enums_module.TimeInForce = types.SimpleNamespace(DAY="day", GTC="gtc")

sys.modules.update({
    "alpaca": alpaca_module,
    "alpaca.trading": trading_module,
    "alpaca.trading.client": client_module,
    "alpaca.trading.requests": requests_module,
    "alpaca.trading.enums": enums_module,
})

from shared.broker import alpaca as broker


class _Client:
    def __init__(self, error=None):
        self.error = error
        self.orders = []

    def submit_order(self, order):
        if self.error:
            raise self.error
        self.orders.append(order)


class CloseOrderTests(unittest.TestCase):
    def test_close_long_reports_accepted_order(self):
        client = _Client()

        with patch.object(broker, "get_client", return_value=client):
            closed = broker.close_long("AAPL", 1.25)

        self.assertTrue(closed)
        self.assertEqual(client.orders[0].symbol, "AAPL")
        self.assertEqual(client.orders[0].qty, "1.25")
        self.assertEqual(client.orders[0].side, "sell")

    def test_close_short_reports_accepted_order(self):
        client = _Client()

        with patch.object(broker, "get_client", return_value=client):
            closed = broker.close_short("AAPL", 3)

        self.assertTrue(closed)
        self.assertEqual(client.orders[0].qty, "3")
        self.assertEqual(client.orders[0].side, "buy")

    def test_close_long_reports_submission_failure(self):
        client = _Client(RuntimeError("broker unavailable"))

        with patch.object(broker, "get_client", return_value=client):
            closed = broker.close_long("AAPL", 1.25)

        self.assertFalse(closed)

    def test_close_short_reports_submission_failure(self):
        client = _Client(RuntimeError("broker unavailable"))

        with patch.object(broker, "get_client", return_value=client):
            closed = broker.close_short("AAPL", 3)

        self.assertFalse(closed)


if __name__ == "__main__":
    unittest.main()