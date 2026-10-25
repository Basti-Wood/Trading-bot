"""Broker routing, ambiguous execution, isolated cash, and real-money reporting contracts."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

from app import loop
from app.accounts import Accounts, overview_state
from app.broker import Alpaca, BrokerError
from app.config import Config
from app.store import Store, StrategyStore
from app.trading212 import Trading212, MarketData, mappings
from app.trading212_engine import Trading212Engine, context
from app.web import create_app
from test_loop import make_loop_dir, add_version


NOW = pd.Timestamp("2026-10-23T07:20:00Z")
TICKER = "SIEd_EQ"
SPEC = {"symbol": "SIE", "mic": "XETR", "currency": "EUR", "isin": "DE0007236101"}


def instrument(ticker=TICKER):
    return {"ticker": ticker, "name": "Siemens", "type": "STOCK", "currencyCode": "EUR",
            "isin": SPEC["isin"], "workingScheduleId": 12, "maxOpenQuantity": 10000,
            "schedule": {"id": 12, "mic": "XETR", "exchange": "Xetra", "timeEvents": [
                {"date": "2026-10-22T07:00:00Z", "type": "OPEN"},
                {"date": "2026-10-22T15:30:00Z", "type": "CLOSE"},
                {"date": "2026-10-23T07:00:00Z", "type": "OPEN"},
                {"date": "2026-10-23T15:30:00Z", "type": "CLOSE"}]}}


def remote(oid=1, ticker=TICKER, side="BUY", status="NEW", qty=3, filled=0, kind="LIMIT"):
    return {"id": oid, "ticker": ticker, "instrument": {"ticker": ticker}, "side": side,
            "status": status, "quantity": qty, "filledQuantity": filled, "type": kind, "stopPrice": 95,
            "limitPrice": 100, "createdAt": NOW.isoformat()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.mapping = directory / "instruments.json"
        self.mapping.write_text(json.dumps({TICKER: SPEC}))
        self.cfg = Config(data_dir=directory / "data", api_key="alpaca-key", api_secret="alpaca-secret",
            trading_mode="both", universe_mode="watchlist", symbols=("SPY",), trading212_enabled=True,
            trading212_key="t212-key", trading212_secret="t212-secret", twelvedata_key="data-key",
            trading212_instruments=self.mapping)
        self.store = Store(self.cfg.data_dir)


class ApiTests(Fixture):
    def adapter(self, exchange="Xetra", kind="STOCK"):
        broker = Trading212(self.cfg)
        i = instrument()
        broker.cached = Mock(side_effect=lambda path, _: (
            [{k: v for k, v in i.items() if k != "schedule"} | {"type": kind}] if path.endswith("instruments") else
            [{"id": 1, "name": exchange, "workingSchedules": [i["schedule"]]}]))
        return broker

    def test_us_exchange_is_rejected_even_when_ticker_and_currency_look_european(self):
        for exchange in ("NYSE", "NASDAQ", "UNKNOWN"):
            with self.subTest(exchange=exchange):
                broker = self.adapter(exchange)
                broker.request = Mock()
                with self.assertRaises(BrokerError):
                    broker.submit(TICKER, "buy", 3, kind="market")
                broker.request.assert_not_called()

    def test_only_non_us_stocks_and_etfs_are_accepted(self):
        broker = self.adapter(kind="CRYPTO")
        with self.assertRaises(BrokerError):
            broker.instrument(TICKER)
        broker = self.adapter()
        self.assertEqual(broker.instrument(TICKER)["schedule"]["mic"], "XETR")

    def test_sell_and_broker_stop_follow_current_official_payload_schema(self):
        broker = self.adapter()
        broker.request = Mock(return_value=remote(side="SELL", qty=-3, kind="STOP"))
        broker.submit(TICKER, "sell", 3, kind="stop", price=95)
        self.assertEqual(broker.request.call_args.kwargs["json"],
                         {"ticker": TICKER, "quantity": -3., "stopPrice": 95, "timeValidity": "GOOD_TILL_CANCEL"})
        self.assertEqual(broker.base, "https://demo.trading212.com/api/v0")

    def test_live_orders_require_the_secondary_broker_gate(self):
        broker = self.adapter()
        broker.config = replace(self.cfg, trading212_paper=False)
        broker.request = Mock()
        with self.assertRaises(BrokerError):
            broker.submit(TICKER, "buy", 1, kind="market")
        broker.request.assert_not_called()

    def test_timeout_is_not_retried_and_does_not_disclose_credentials(self):
        broker = Trading212(self.cfg)
        broker.session.request = Mock(side_effect=requests.Timeout("t212-secret in url"))
        with self.assertRaises(BrokerError) as raised:
            broker.request("POST", "/equity/orders/market", json={})
        self.assertNotIn("t212-secret", str(raised.exception))
        self.assertEqual(broker.session.request.call_count, 1)

    def test_pending_404_is_reconciled_through_history_not_called_a_fill(self):
        broker = self.adapter()
        broker.request = Mock(side_effect=[BrokerError("not pending", 404),
            {"items": [{"order": remote(status="FILLED", filled=3)}], "nextPagePath": None}])
        self.assertEqual(broker.order("1", TICKER)["filled_qty"], 3)
        broker.request = Mock(side_effect=[BrokerError("not pending", 404), {"items": [], "nextPagePath": None}])
        with self.assertRaises(BrokerError):
            broker.order("1", TICKER)

    def test_history_pagination_never_follows_an_external_url_with_auth(self):
        broker = self.adapter()
        broker.request = Mock(return_value={"items": [], "nextPagePath": "https://example.com/collect"})
        with self.assertRaises(BrokerError):
            list(broker.pages("/equity/history/orders"))
        self.assertEqual(broker.request.call_count, 1)

    def test_dated_exchange_events_respect_breaks_and_closed_days(self):
        broker = self.adapter()
        self.assertTrue(broker.session_for(TICKER, NOW)["open"])
        self.assertFalse(broker.session_for(TICKER, NOW + pd.Timedelta(days=1))["open"])
        i = instrument()
        i["schedule"]["timeEvents"].extend([
            {"date": "2026-10-23T07:10:00Z", "type": "BREAK_START"},
            {"date": "2026-10-23T08:00:00Z", "type": "BREAK_END"}])
        broker.instrument = Mock(return_value=i)
        self.assertFalse(broker.session_for(TICKER, NOW)["open"])

    def test_alpaca_rechecks_listing_at_the_post_boundary(self):
        broker = Alpaca(self.cfg)
        broker.asset = Mock(return_value={"class": "us_equity", "exchange": "XETRA"})
        broker.request = Mock()
        with self.assertRaises(BrokerError):
            broker.submit({"symbol": "SAP", "side": "buy"})
        broker.request.assert_not_called()
        with self.assertRaises(BrokerError):
            broker.submit({"symbol": TICKER, "side": "buy"})
        broker.asset.return_value = {"class": "us_equity", "exchange": "NYSE"}
        broker.submit({"symbol": "SAP", "side": "buy"})
        broker.request.assert_called_once()


class DataTests(Fixture):
    def test_listing_currency_and_isin_must_match(self):
        meta = {"symbol": "SIE", "mic_code": "XETR", "currency": "EUR"}
        MarketData.verify(meta, SPEC, instrument())
        for changed in ({"mic_code": "XNYS"}, {"currency": "USD"}, {"symbol": "OTHER"}):
            with self.assertRaises(BrokerError):
                MarketData.verify({**meta, **changed}, SPEC, instrument())
        with self.assertRaises(BrokerError):
            MarketData.verify(meta, {**SPEC, "isin": "US0000000000"}, instrument())

    def test_daily_bars_use_exchange_dates_and_exclude_todays_unfinished_bar(self):
        data = MarketData(self.cfg)
        dates = pd.bdate_range(end="2026-10-23", periods=1100)
        data.request = Mock(return_value={"meta": {"symbol": "SIE", "mic_code": "XETR", "currency": "EUR",
            "exchange_timezone": "Europe/Berlin"}, "values": [{"datetime": str(d.date()), "open": "100",
            "high": "102", "low": "99", "close": "101", "volume": "2000"} for d in dates]})
        frame = data.bars(instrument(), SPEC, NOW)
        self.assertEqual(str(frame.index[-1].tz_convert("Europe/Berlin").date()), "2026-10-22")
        self.assertEqual(str(loop.daily_frame(frame).index[-1].date()), "2026-10-22")
        self.assertEqual(len(frame), 1099)

    def test_delayed_quotes_cannot_price_an_entry(self):
        data = MarketData(self.cfg)
        result = {"symbol": "SIE", "mic_code": "XETR", "currency": "EUR", "timestamp": NOW.timestamp(),
                  "close": "100", "is_market_open": True}
        data.request = Mock(return_value=result)
        self.assertEqual(data.quote(instrument(), SPEC, NOW), 100)
        data.request.return_value = {**result, "timestamp": (NOW - pd.Timedelta(minutes=15)).timestamp()}
        with self.assertRaises(BrokerError):
            data.quote(instrument(), SPEC, NOW)

    def test_fx_is_opt_in_and_pence_are_scaled_once(self):
        data = MarketData(self.cfg)
        self.assertEqual(data.execution_fx("EUR", "EUR", NOW), 1)
        self.assertEqual(data.execution_fx("GBX", "GBP", NOW), .01)
        with self.assertRaises(BrokerError):
            data.execution_fx("GBP", "EUR", NOW)


class EngineFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        add_version(self.folder, "trend_v1", symbol="LISTE")
        self.cfg = replace(self.cfg, loop_dir=self.folder)
        self.broker, self.data = Mock(spec=Trading212), Mock(spec=MarketData)
        self.broker.base = "https://demo.trading212.com/api/v0"
        self.broker.account.return_value = {"id": "212", "equity": 10000, "cash": 3000, "currency": "EUR", "mode": "paper"}
        self.broker.positions.return_value = []
        self.broker.open_orders.return_value = []
        self.broker.metadata.return_value = {TICKER: instrument()}
        self.broker.instrument.return_value = instrument()
        self.broker.session_for.return_value = {"open": True, "opening": pd.Timestamp("2026-10-23T07:00Z"),
            "close": pd.Timestamp("2026-10-23T15:30Z"), "prior_close": pd.Timestamp("2026-10-22T15:30Z")}
        self.broker.submit.return_value = Trading212.normalize_order(remote())
        self.broker.order.return_value = Trading212.normalize_order(remote())
        self.data.quote.return_value = 100
        self.data.execution_fx.return_value = 1
        self.data.verify.side_effect = MarketData.verify
        self.engine = Trading212Engine(self.cfg, self.store, self.broker, self.data)
        self.engine.signals_tick = Mock()
        record = {"version": "trend_v1", "symbol": "LISTE", "sha256": loop.file_hash(self.folder / "strategien/trend_v1.py")}
        StrategyStore(self.store, "loop").set("loop_strategies", {"trend_v1": record})
        self.engine.store.set("loop_results:trend_v1", {"sha256": record["sha256"], "symbols": {TICKER: {
            "signal": "BUY", "reason": "next open", "bar_at": "2026-10-21T22:00Z", "target_session": "2026-10-23",
            "market_timezone": "Europe/Berlin", "last_close": 100,
            "test": {"gruen": True, "rendite": 15., "max_drawdown": -5., "trades": 20}}}})
        self.engine.store.set("entries_enabled", True)

    def filled_position(self, qty=3, actual=None):
        self.engine.store.reserve_order("filled-entry", {"symbol": TICKER, "side": "buy", "type": "limit",
            "qty": qty, "filled_qty": qty, "status": "filled", "broker": "trading212"})
        self.engine.store.set(f"owned:{TICKER}", True)
        self.engine.store.set(f"loop_owner:{TICKER}", "trend_v1")
        self.broker.positions.return_value = [{"symbol": TICKER, "qty": actual or qty, "available_qty": actual or qty,
            "avg_entry_price": 100, "current_price": 101, "market_value": 101 * (actual or qty),
            "unrealized_pl": qty, "currency": "EUR", "instrument_currency": "EUR"}]


class EngineTests(EngineFixture):
    def test_entry_uses_only_secondary_cash_and_keeps_alpaca_journal_untouched(self):
        self.store.set("account", {"equity": 1000000, "cash": 1000000})
        self.engine.cycle(NOW)
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args[:2], (TICKER, "buy"))
        self.assertLessEqual(self.broker.submit.call_args.args[2] * 101, 1000)
        self.assertEqual(self.store.orders(), [])
        self.assertEqual(self.store.get("account")["cash"], 1000000)
        self.assertEqual(len(self.engine.store.orders()), 1)

    def test_no_other_account_can_cover_a_secondary_cash_shortfall(self):
        self.store.set("account", {"equity": 1000000, "cash": 1000000})
        self.broker.account.return_value["cash"] = 80
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()

    def test_non_us_route_failure_blocks_a_green_signal(self):
        self.broker.instrument.side_effect = BrokerError("US listings belong to Alpaca", 422)
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.assertIn("Alpaca", self.engine.store.get(f"forecast:{TICKER}")["reason"])

    def test_timeout_is_durable_and_no_entry_is_replayed_after_restart(self):
        self.broker.submit.side_effect = BrokerError("timeout")
        self.engine.cycle(NOW)
        self.assertEqual(self.engine.store.orders()[0]["status"], "unknown")
        restarted = Trading212Engine(self.cfg, self.store, self.broker, self.data)
        restarted.signals_tick = Mock()
        restarted.store.set("entries_enabled", True)
        restarted.cycle(NOW)
        self.assertEqual(self.broker.submit.call_count, 1)
        self.assertEqual(restarted.store.get("engine")["status"], "blocked")

    def test_external_holdings_are_never_adopted_or_sold(self):
        self.broker.positions.return_value = [{"symbol": TICKER, "qty": 10}]
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.assertFalse(self.engine.store.get(f"owned:{TICKER}", False))

    def test_paused_entries_still_protect_only_confirmed_bot_shares(self):
        self.filled_position(qty=3, actual=8)
        self.engine.store.set("entries_enabled", False)
        self.engine.cycle(NOW)
        call = self.broker.submit.call_args
        self.assertEqual(call.args, (TICKER, "sell", 3.))
        self.assertEqual(call.kwargs, {"kind": "stop", "price": 95.})

    def test_rejected_broker_stop_requests_an_exit_not_another_entry(self):
        self.filled_position()
        self.broker.submit.side_effect = BrokerError("stop rejected", 422)
        self.engine.cycle(NOW)
        self.broker.submit.side_effect = None
        self.broker.submit.return_value = Trading212.normalize_order(remote(side="SELL", kind="MARKET"))
        self.engine.cycle(NOW)
        self.assertEqual(self.broker.submit.call_args.kwargs["kind"], "market")
        self.assertEqual(self.broker.submit.call_args.args[1], "sell")

    def test_closed_or_missing_session_blocks_entry(self):
        self.broker.session_for.return_value["open"] = False
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.data.quote.assert_not_called()

    def test_account_change_cannot_reuse_another_accounts_ownership(self):
        self.engine.cycle(NOW)
        self.broker.account.return_value["id"] = "different"
        with self.assertRaises(BrokerError):
            self.engine.cycle(NOW)
        self.assertEqual(self.broker.submit.call_count, 1)

    def test_pending_buy_is_canceled_when_paused_but_external_orders_are_not(self):
        self.engine.send(TICKER, "buy", 3, "limit", "pending", price=100, version="trend_v1")
        self.broker.open_orders.return_value = [Trading212.normalize_order(remote()), Trading212.normalize_order(remote(oid=42))]
        self.engine.store.set("entries_enabled", False)
        self.engine.cycle(NOW)
        self.broker.cancel.assert_called_once_with("1")


class AccountTests(Fixture):
    def snapshot(self, equity=10000, currency="USD", mode="paper", capital=None):
        capital = equity if capital is None else capital
        return {"equity": equity, "cash": 100, "currency": currency, "mode": mode, "at": NOW.isoformat(),
                "status": "connected", "net_capital": capital, "pnl": equity - capital,
                "return_fraction": (equity - capital) / capital, "since": NOW.isoformat()}

    def test_combined_is_converted_and_capital_weighted_not_averaged(self):
        self.store.set("broker_balance:alpaca", self.snapshot(1100, capital=1000))
        self.store.set("broker_balance:trading212", self.snapshot(2100, "EUR", capital=2000))
        self.store.set("display_fx", {"USD": {"rate": .9, "date": "2026-10-23"}})
        result = overview_state(self.cfg, self.store, NOW)
        self.assertAlmostEqual(result["combined"]["equity"], 3090)
        self.assertAlmostEqual(result["combined"]["return_fraction"], 190 / 2900)
        self.assertNotAlmostEqual(result["combined"]["return_fraction"], (.1 + .05) / 2)

    def test_missing_fx_or_stale_account_does_not_fabricate_a_total(self):
        self.store.set("broker_balance:alpaca", self.snapshot())
        self.store.set("broker_balance:trading212", self.snapshot(currency="EUR"))
        self.assertIsNone(overview_state(self.cfg, self.store, NOW)["combined"]["equity"])
        self.store.set("display_fx", {"USD": {"rate": .9, "date": "2026-10-23"}})
        self.assertIsNone(overview_state(self.cfg, self.store, NOW + pd.Timedelta(minutes=4))["combined"]["equity"])

    def test_demo_and_live_money_are_never_added(self):
        self.store.set("broker_balance:alpaca", self.snapshot(currency="EUR"))
        self.store.set("broker_balance:trading212", self.snapshot(currency="EUR", mode="live"))
        result = overview_state(self.cfg, self.store, NOW)
        self.assertEqual(result["combined"]["status"], "mixed_modes")
        self.assertIsNone(result["combined"]["equity"])

    def test_deposits_and_withdrawals_are_not_profit(self):
        broker = Mock(base="paper-alpaca")
        broker.account.return_value = {"id": "1", "equity": 10000, "cash": 3000, "currency": "USD"}
        broker.cash_flows.return_value = [{"amount": 1000, "currency": "USD"}]
        accounts = Accounts(self.cfg, self.store, alpaca=broker)
        accounts.observe("alpaca", broker, NOW)
        broker.account.return_value["equity"] = 12100
        broker.cash_flows.return_value = [{"amount": 1000, "currency": "USD"}, {"amount": 2000, "currency": "USD"}]
        result = accounts.observe("alpaca", broker, NOW + pd.Timedelta(minutes=1))
        self.assertAlmostEqual(result["return_fraction"], 100 / 12000)
        broker.account.return_value["equity"] = 10080
        broker.cash_flows.return_value.append({"amount": -2000, "currency": "USD"})
        self.assertAlmostEqual(accounts.observe("alpaca", broker, NOW + pd.Timedelta(minutes=2))["return_fraction"], 80 / 10000)

    def test_missing_funding_history_keeps_balance_but_hides_return(self):
        broker = Mock(base="paper-alpaca")
        broker.account.return_value = {"id": "1", "equity": 10000, "cash": 3000, "currency": "USD"}
        broker.cash_flows.side_effect = BrokerError("missing permission", 403)
        result = Accounts(self.cfg, self.store, alpaca=broker).observe("alpaca", broker, NOW)
        self.assertEqual(result["equity"], 10000)
        self.assertIsNone(result["return_fraction"])


class WebTests(Fixture):
    def test_dashboard_exposes_separate_balances_and_redacts_all_api_keys(self):
        state = create_app(self.cfg, self.store).test_client().get("/api/state").get_json()
        self.assertEqual(set(state["money"]) & {"combined", "alpaca", "trading212"}, {"combined", "alpaca", "trading212"})
        for secret in ("alpaca-key", "alpaca-secret", "t212-key", "t212-secret", "data-key"):
            self.assertNotIn(secret, json.dumps(state))

    def test_pause_all_reaches_both_independent_books(self):
        secondary = context(self.cfg)[1]
        secondary.set("entries_enabled", True)
        StrategyStore(self.store, "loop").set("entries_enabled", True)
        client = create_app(self.cfg, self.store).test_client()
        response = client.post("/api/control", json={"action": "pause"}, headers={"X-Bot-Control": "dashboard"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(secondary.get("entries_enabled"))
        self.assertFalse(StrategyStore(self.store, "loop").get("entries_enabled"))

    def test_secondary_live_resume_is_blocked_without_its_live_gate(self):
        cfg = replace(self.cfg, trading212_paper=False)
        client = create_app(cfg, self.store).test_client()
        response = client.post("/api/trading212/control", json={"action": "resume"}, headers={"X-Bot-Control": "dashboard"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("TRADING212_ALLOW_LIVE_TRADING", response.get_json()["error"])

    def test_primary_and_secondary_ledger_rows_keep_their_broker(self):
        self.store.reserve_order("a", {"symbol": "SPY", "side": "buy", "status": "filled"})
        context(self.cfg)[1].reserve_order("b", {"symbol": TICKER, "broker": "trading212", "side": "buy", "status": "filled"})
        rows = create_app(self.cfg, self.store).test_client().get("/api/state").get_json()["orders"]
        self.assertEqual({r["broker"] for r in rows}, {"alpaca", "trading212"})
