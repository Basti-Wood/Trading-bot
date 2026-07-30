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
    def __init__(self, error=None, fail_on_call=None, fractionable=True):
        self.error = error
        self.fail_on_call = fail_on_call
        self.fractionable = fractionable
        self.orders = []

    def submit_order(self, order):
        if self.fail_on_call is not None and len(self.orders) == self.fail_on_call:
            raise RuntimeError("simulated submit failure")
        if self.error:
            raise self.error
        self.orders.append(order)

    def get_asset(self, _symbol):
        return types.SimpleNamespace(fractionable=self.fractionable)


class BrokerBaseTests(unittest.TestCase):
    def setUp(self):
        broker._client = None
        broker._broker_unavailable_reason = None


class CloseOrderTests(BrokerBaseTests):
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


class OpenOrderTests(BrokerBaseTests):
    def test_open_long_fractional_uses_fractional_stop_qty(self):
        client = _Client(fractionable=True)

        with patch.object(broker, "get_client", return_value=client), \
                patch.object(broker, "is_fractionable", return_value=True):
            opened = broker.open_long("AAPL", 25.0, 170.0, 200.0)

        self.assertTrue(opened)
        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[1].qty, "0.125")

    def test_open_long_still_reports_success_when_stop_fails(self):
        client = _Client(fail_on_call=1, fractionable=True)

        with patch.object(broker, "get_client", return_value=client), \
                patch.object(broker, "is_fractionable", return_value=True):
            opened = broker.open_long("AAPL", 25.0, 170.0, 200.0)

        self.assertTrue(opened)
        self.assertEqual(len(client.orders), 1)
        self.assertIn("Long stop placement failed", broker.unavailable_reason())

    def test_open_short_returns_zero_on_submission_failure(self):
        client = _Client(error=RuntimeError("broker unavailable"))

        with patch.object(broker, "get_client", return_value=client):
            qty = broker.open_short("AAPL", 300.0, 210.0, 200.0)

        self.assertEqual(qty, 0)
        self.assertIn("Short entry failed", broker.unavailable_reason())


class ConfigurationTests(BrokerBaseTests):
    def test_is_configured_rejects_env_example_placeholder_values(self):
        with patch.dict("os.environ", {
            "ALPACA_API_KEY": "your_alpaca_key_here",
            "ALPACA_SECRET_KEY": "your_alpaca_secret_here",
        }, clear=False):
            self.assertFalse(broker.is_configured())


if __name__ == "__main__":
    unittest.main()