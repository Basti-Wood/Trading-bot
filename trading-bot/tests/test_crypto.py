"""Crypto bots: coin safety rules, Alpaca crypto data and orders, 24/7 engine behaviour."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock
import unittest

import numpy as np
import pandas as pd

from app import crypto, loop
from app.broker import Alpaca
from app.config import Config
from app.execution import decimal, price_step
from app.features import FEATURES
from app.feedback import capture_forecast, resolve_outcomes
from app.models import strategy_signature
from app.portfolio import PortfolioEngine
from app.store import Store
from app.web import create_app
from test_loop_manual import add_version, make_loop_dir
from test_system import TempCase, models

NOW = pd.Timestamp("2026-10-16T00:30:00Z")  # Half an hour into the UTC day: inside the entry window.
ASSET = {"symbol": "BTC/USD", "tradable": True, "status": "active", "fractionable": True,
         "price_increment": "0.01", "min_trade_increment": "0.0001", "min_order_size": "0.0001"}


def coin(days=900, growth=.001, wiggle=.03, volume=100., end=NOW):
    """Completed UTC daily candles ending yesterday; about $10,000 traded per day."""
    index = pd.date_range(end=end.floor("D") - pd.Timedelta(days=1), periods=days, freq="D", tz="UTC")
    n = np.arange(days)
    close = 100 * np.exp(n * growth + np.sin(n / 3) * wiggle)
    frame = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * .99, "close": close, "volume": volume}, index=index)
    frame.attrs.update(crypto=True, target_session=(index[-1] + pd.Timedelta(days=1)).date().isoformat())
    return frame


class CoinRuleTests(unittest.TestCase):
    cfg = Config()

    def reason(self, symbol="BTC/USD", frame=None, cfg=None):
        return crypto.check(cfg or self.cfg, symbol, coin() if frame is None else frame, NOW)[0]

    def test_established_liquid_coin_passes(self):
        reason, metrics = crypto.check(self.cfg, "BTC/USD", coin(), NOW)
        self.assertIsNone(reason)
        self.assertGreater(metrics["dollar_volume"], self.cfg.crypto_min_dollar_volume)
        self.assertGreaterEqual(metrics["history_days"], 730)

    def test_pegged_meme_and_excluded_coins_are_never_allowed(self):
        self.assertIn("Stablecoin", self.reason("USDC/USD"))
        self.assertIn("Gold", self.reason("PAXG/USD"))
        self.assertIn("Meme", self.reason("DOGE/USD"))
        self.assertIn("CRYPTO_EXCLUDE", self.reason("XRP/USD", cfg=Config(crypto_exclude=("XRP",))))
        # An unknown stablecoin is caught by its flat price.
        self.assertIn("bewegt sich kaum", self.reason("NEWUSD/USD", coin(growth=0, wiggle=.001)))

    def test_young_thin_stale_or_pumped_coins_are_blocked(self):
        self.assertIn("Kurshistorie", self.reason(frame=coin(days=200)))
        self.assertIn("Zu wenig Handel", self.reason(frame=coin(volume=1)))
        self.assertIn("Keine aktuellen Kurse", self.reason(frame=coin(end=NOW - pd.Timedelta(days=10))))
        jump = coin()
        jump.iloc[-5:, :4] *= 1.6
        self.assertIn("Kurssprung", self.reason(frame=jump))
        pump = coin()
        # Steady 3 % daily gains: no single jump, but more than doubled in 30 days.
        pump.iloc[-30:, :4] = pump.iloc[-30:, :4].mul(np.exp(np.arange(1, 31) * .03), axis=0)
        self.assertIn("Pump-Verdacht", self.reason(frame=pump))

    def test_scan_keeps_only_passing_coins_and_fails_closed(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name))
        broker = Mock()
        broker.crypto_assets.return_value = [{"symbol": s, "tradable": True, "status": "active"} for s in
                                             ("BTC/USD", "ETH/USD", "USDT/USD", "DOGE/USD", "NEW/USD")] + [
                                             {"symbol": "BTC/USDT", "tradable": True, "status": "active"},
                                             {"symbol": "OLD/USD", "tradable": False, "status": "active"}]
        broker.crypto_daily.return_value = {"BTC/USD": coin(volume=1000), "ETH/USD": coin(), "USDT/USD": coin(),
                                            "DOGE/USD": coin(), "NEW/USD": coin(days=100)}
        crypto.scan(self.cfg, store, broker)
        universe = store.get("crypto_universe")
        self.assertEqual(universe["symbols"], ["BTC/USD", "ETH/USD"])  # Most traded first.
        self.assertEqual(sorted(r["symbol"] for r in universe["candidates"]), ["BTC/USD", "DOGE/USD", "ETH/USD", "NEW/USD", "USDT/USD"])
        self.assertTrue(crypto.ready(store))
        self.assertAlmostEqual(crypto.position_cap(self.cfg, store, "BTC/USD"),
                               next(r for r in universe["candidates"] if r["symbol"] == "BTC/USD")["dollar_volume"] * .10)
        self.assertEqual(crypto.position_cap(self.cfg, store, "DOGE/USD"), 0)
        broker.crypto_daily.side_effect = RuntimeError("down")
        crypto.scan(self.cfg, store, broker)
        self.assertEqual(store.get("crypto_universe")["symbols"], ["BTC/USD", "ETH/USD"])
        self.assertFalse(crypto.ready(store))

    def test_entry_window_and_utc_sessions(self):
        self.assertTrue(crypto.entry_window(NOW))
        self.assertFalse(crypto.entry_window(NOW + pd.Timedelta(hours=3)))
        self.assertEqual(crypto.session(pd.Timestamp("2026-10-16T23:30:00-04:00")), "2026-10-17")


class CryptoBrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = Alpaca(Config(api_key="k", api_secret="s", max_bars=2000))
        self.broker.request = Mock()

    def test_daily_candles_drop_the_unfinished_utc_day(self):
        days = pd.date_range("2026-10-13", periods=4, freq="D", tz="UTC")
        rows = [{"t": d.isoformat(), "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 3} for d in days]
        self.broker.request.return_value = {"bars": {"BTC/USD": rows}, "next_page_token": None}
        frame = self.broker.bars("BTC/USD", now=pd.Timestamp("2026-10-16T13:00:00Z").to_pydatetime())
        self.assertEqual(list(frame.index.strftime("%m-%d")), ["10-13", "10-14", "10-15"])
        self.assertEqual((frame.attrs["target_session"], frame.attrs["crypto"]), ("2026-10-16", True))
        self.assertIn("/v1beta3/crypto/us/bars", self.broker.request.call_args.args[1])

    def test_crypto_positions_use_the_trading_symbol(self):
        self.broker.request.return_value = [{"symbol": "BTCUSD", "asset_class": "crypto", "qty": "1"},
                                            {"symbol": "AAPL", "asset_class": "us_equity", "qty": "2"}]
        self.assertEqual([p["symbol"] for p in self.broker.positions()], ["BTC/USD", "AAPL"])

    def test_wide_or_stale_crypto_quotes_are_refused(self):
        fresh = pd.Timestamp.now(tz="UTC").isoformat()
        self.broker.request.return_value = {"quotes": {"BTC/USD": {"t": fresh, "bp": 99.5, "ap": 100}}}
        self.assertEqual(self.broker.quote("BTC/USD"), (99.5, 100.))
        self.broker.request.return_value = {"quotes": {"BTC/USD": {"t": fresh, "bp": 95, "ap": 100}}}
        with self.assertRaises(Exception):
            self.broker.quote("BTC/USD")

    def test_decimal_follows_the_coin_increment(self):
        self.assertEqual(decimal(4.99, .0001), "4.9900")
        self.assertEqual(decimal(65000.4, 1), "65000")
        self.assertEqual(decimal(.000012345, .000000001), "0.000012345")
        fine = {"price_increment": "0.000000001"}
        self.assertEqual((price_step(fine, 82000.), price_step(fine, .00002)), (.01, 1e-9))


class CryptoCase(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        self.cfg = replace(self.cfg, trading_mode="both", loop_dir=self.folder, crypto_enabled=True)
        self.store.set("allocation", {"capital_fraction": 1, "capital_cap": 0,
                                      "weights": {"daily": 50, "daytrade": 0, "loop": 0, "crypto": 50, "crypto_loop": 0}})
        self.portfolio = PortfolioEngine(self.cfg, self.store)
        self.engines = {e.config.trading_mode: e for e in self.portfolio.engines}
        self.crypto = self.engines["crypto"]
        self.broker = Mock(base="paper")
        self.broker.account.return_value = {"status": "ACTIVE", "equity": "10000", "last_equity": "10000",
                                            "cash": "10000", "buying_power": "10000"}
        # Saturday: the stock market is closed, coins trade.
        self.broker.clock.return_value = {"timestamp": NOW.isoformat(), "is_open": False,
                                          "next_open": "2026-10-19T13:30:00Z", "next_close": "2026-10-19T20:00:00Z"}
        self.broker.positions.return_value = []
        self.broker.open_orders.return_value = []
        self.broker.quote.return_value = (99.9, 100.)
        self.broker.asset.return_value = ASSET
        self.broker.submit.return_value = {"id": "accepted", "status": "new", "filled_qty": "0"}
        self.broker.order.return_value = {"id": "accepted", "status": "filled", "filled_qty": "4"}
        self.portfolio.broker = self.broker
        for engine in self.portfolio.engines:
            engine.broker = engine.executor.broker = self.broker
            engine.scan_tick = engine.forecast_tick = engine.research_tick = engine.playbook_tick = Mock()
            engine.store.set("entries_enabled", True)
        self.coins(dollar_volume=1000000)

    def coins(self, dollar_volume):
        self.store.set("crypto_universe", {"symbols": ["BTC/USD"], "scanned_at": time.time(),
                                           "candidates": [{"symbol": "BTC/USD", "ok": True, "dollar_volume": dollar_volume},
                                                          {"symbol": "DOGE/USD", "ok": False, "dollar_volume": 9e9}]})
        self.store.set("crypto_scan_status", {"status": "ready"})

    def forecast(self, signal="BUY", session="2026-10-16"):
        meta = {"version": "m1", "strategy_signature": strategy_signature(self.crypto.config), "eligible": True,
                "data_through": "2026-10-15T00:00:00+00:00"}
        self.crypto.store.set("model:BTC/USD", meta)
        self.crypto.store.set("prediction:BTC/USD", {
            "symbol": "BTC/USD", "model_version": "m1", "bar_at": "2026-10-15T00:00:00+00:00", "target_session": session,
            "models": models(signal), "eligible": True, "forecast_signal": signal, "expected_return": .02,
            "trading_mode": "crypto", "last_close": 100.})

    def held(self, **extra):
        self.crypto.store.set("owned:BTC/USD", True)
        position = {"symbol": "BTC/USD", "asset_class": "crypto", "qty": "4.99", "qty_available": "4.99",
                    "avg_entry_price": "100", "market_value": "499", "unrealized_plpc": "0", **extra}
        self.broker.positions.return_value = [position]
        return position


class CryptoEngineTests(CryptoCase):
    def test_both_crypto_bots_run_with_crypto_costs_and_window(self):
        self.assertEqual(list(self.engines), ["daily", "daytrade", "loop", "crypto", "crypto_loop"])
        self.assertEqual((self.crypto.config.cost_bps, self.engines["crypto_loop"].config.history_days), (25, loop.HISTORY_DAYS))
        self.assertEqual(self.engines["daily"].config.cost_bps, 10)

    def test_entry_is_an_ioc_limit_at_the_ask_inside_the_utc_window(self):
        self.forecast()
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload["symbol"], payload["side"], payload["type"], payload["time_in_force"]), ("BTC/USD", "buy", "limit", "ioc"))
        self.assertEqual((payload["limit_price"], payload["qty"]), ("100.20", "4.9900"))  # 50 % of $10,000, 10 % per position.
        self.assertNotIn("order_class", payload)
        self.assertNotIn("/", payload["client_order_id"])
        self.assertEqual(self.crypto.store.get("unbracketed:BTC/USD"), {"stop": .05, "target": .10})
        self.assertEqual(self.store.owner("BTC/USD"), "crypto")

    def test_no_entry_outside_the_window_for_another_day_or_for_failed_coins(self):
        self.forecast()
        self.broker.clock.return_value = {**self.broker.clock.return_value, "timestamp": (NOW + pd.Timedelta(hours=4)).isoformat()}
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("entry window", self.crypto.store.get("forecast:BTC/USD")["reason"])
        self.broker.clock.return_value = {**self.broker.clock.return_value, "timestamp": NOW.isoformat()}
        self.forecast(session="2026-10-15")
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("Krypto-Tag", self.crypto.store.get("forecast:BTC/USD")["reason"])
        self.assertIn("Sicherheitsregeln", self.crypto.executor.execute("DOGE/USD", "BUY", "2026-10-16", self.broker.account(), [], []))
        self.store.set("crypto_scan_status", {"status": "unavailable"})
        self.forecast()
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()

    def test_position_size_is_capped_by_the_coins_daily_volume(self):
        self.coins(dollar_volume=2000)  # 10 % of $2,000 = $200.
        self.forecast()
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]["qty"], "1.9960")

    def test_held_coins_get_a_broker_stop_once(self):
        self.held()
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload["side"], payload["type"], payload["time_in_force"]), ("sell", "stop_limit", "gtc"))
        self.assertEqual((payload["stop_price"], payload["limit_price"], payload["qty"]), ("95.00", "92.15", "4.9900"))
        self.broker.open_orders.return_value = [{"symbol": "BTC/USD", "side": "sell", "type": "stop_limit", "id": "stop-1",
                                                 "client_order_id": payload["client_order_id"]}]
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_count, 1)

    def test_strategy_exit_cancels_the_broker_stop_then_sells_at_market(self):
        self.held()
        self.forecast("SELL")
        self.broker.open_orders.return_value = [{"symbol": "BTC/USD", "side": "sell", "type": "stop_limit", "id": "stop-1",
                                                 "client_order_id": "ensemble-x-crypto-stop"}]
        self.portfolio.cycle()
        self.broker.cancel.assert_called_once_with("stop-1")
        self.broker.submit.assert_not_called()
        self.broker.open_orders.return_value = []
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload["side"], payload["type"], payload["time_in_force"]), ("sell", "market", "gtc"))

    def test_bot_managed_stop_sells_around_the_clock(self):
        self.held(unrealized_plpc="-0.06")
        self.crypto.store.set("unbracketed:BTC/USD", {"stop": .05, "target": .10})
        self.broker.clock.return_value = {**self.broker.clock.return_value, "timestamp": "2026-10-17T03:00:00Z"}
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload["side"], payload["type"]), ("sell", "market"))


class CryptoLoopTests(CryptoCase):
    def test_coin_versions_activate_in_the_crypto_trading_loop(self):
        client = create_app(self.cfg, self.store).test_client()
        headers = {"X-Bot-Control": "dashboard"}
        add_version(self.folder, "coins_v1", symbol="KRYPTO")
        add_version(self.folder, "trend_v1")
        for version in ("coins_v1", "trend_v1"):
            self.assertEqual(client.post("/api/loop", json={"action": "activate", "version": version}, headers=headers).status_code, 200)
        self.assertEqual(list(loop.active_versions(self.engines["crypto_loop"].store)), ["coins_v1"])
        self.assertEqual(list(loop.active_versions(self.engines["loop"].store)), ["trend_v1"])
        record = loop.active_versions(self.engines["crypto_loop"].store)["coins_v1"]
        self.assertEqual(loop.version_symbols(self.engines["crypto_loop"].config, self.engines["crypto_loop"].store, record), ["BTC/USD"])
        state = client.get("/api/state").get_json()
        self.assertEqual(sorted((r["version"], r["bot"]) for r in state["loop"]["running"]),
                         [("coins_v1", "crypto_loop"), ("trend_v1", "loop")])
        self.assertEqual(state["crypto"]["universe"]["symbols"], ["BTC/USD"])
        self.assertEqual(client.post("/api/loop", json={"action": "deactivate", "version": "coins_v1"}, headers=headers).status_code, 200)
        self.assertEqual(loop.active_versions(self.engines["crypto_loop"].store), {})

    def test_allocation_accepts_crypto_weights(self):
        client = create_app(self.cfg, self.store).test_client()
        body = {"capital_fraction": 1, "capital_cap": 0, "weights": {"daily": 20, "daytrade": 20, "loop": 20, "crypto": 20, "crypto_loop": 20}}
        self.assertEqual(client.post("/api/allocation", json=body, headers={"X-Bot-Control": "dashboard"}).status_code, 200)
        cards = {s["id"]: s for s in client.get("/api/state").get_json()["strategies"]}
        self.assertAlmostEqual(cards["crypto_loop"]["capital_fraction"], .2)

    def test_coin_backtests_pay_the_crypto_fee(self):
        frame = coin(430)
        results = {}
        for name, symbol in (("stock_v1", "AAA"), ("btc_v1", "BTC/USD")):
            path = add_version(self.folder, name, symbol=symbol, buy_at=100, sell_at=300)
            record = {"version": name, "symbol": symbol, "sha256": loop.file_hash(path)}
            out = loop.evaluate(self.cfg, record, {symbol: frame})["symbols"][symbol]
            results[symbol] = out["test"]["rendite"]
        self.assertLess(results["BTC/USD"], results["AAA"])


class CryptoFeedbackTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode="crypto", coordinated=True)
        self.bars = coin(10, end=pd.Timestamp("2026-10-19T01:00:00Z"))

    def capture(self, bar, captured):
        forecast = {"symbol": "BTC/USD", "at": captured.isoformat(), "bar_at": bar.isoformat(), "model_version": "m1",
                    "features": {name: .01 for name in FEATURES}, "models": [{"id": "ridge", "expected_return": .01}],
                    "expected_return": .01}
        return capture_forecast(self.cfg, self.store, forecast)

    def test_utc_day_must_be_complete_and_late_forecasts_are_excluded(self):
        bar, late_bar = self.bars.index[-4], self.bars.index[-5]
        with self.assertRaisesRegex(ValueError, "UTC day"):
            self.capture(bar, bar + pd.Timedelta(hours=12))
        self.assertTrue(self.capture(bar, bar + pd.Timedelta(days=1, minutes=10)))
        self.assertTrue(self.capture(late_bar, late_bar + pd.Timedelta(days=1, hours=3)))
        resolve_outcomes(self.cfg, self.store, "BTC/USD", self.bars, now=pd.Timestamp("2026-10-19T01:00:00Z"))
        records = {r["bar_at"]: r for r in self.store.experience("BTC/USD", self.cfg.feed, 1)}
        resolved = records[bar.isoformat()]
        self.assertEqual(resolved["status"], "resolved")
        index = self.bars.index.get_loc(bar)
        self.assertAlmostEqual(resolved["outcome"]["target"], self.bars.open.iloc[index + 2] / self.bars.open.iloc[index + 1] - 1)
        self.assertEqual(records[late_bar.isoformat()]["status"], "excluded")


if __name__ == "__main__":
    unittest.main()
