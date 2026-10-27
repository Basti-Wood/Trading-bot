"""Regressions for real API schedules, stale provider caches and worker starvation."""
from dataclasses import replace
import json
import threading
import time
from unittest.mock import Mock, patch

import pandas as pd
import requests

from app.broker import BrokerError
from app.trading212 import Trading212, YahooData, MarketData
from app.trading212_engine import Trading212Engine
from app.web import create_app
from test_broker_routing import yahoo_chart
from test_system import bars
from test_trading212 import Fixture, EngineFixture, instrument, remote, TICKER, SPEC, NOW


class SessionRecoveryTests(Fixture):
    def broker(self, day, opening="07:00", closing="15:30"):
        broker = Trading212(self.cfg)
        item = instrument()
        item["schedule"]["timeEvents"] = [
            {"date": f"{day}T{opening}:00Z", "type": "OPEN"},
            {"date": f"{day}T{closing}:00Z", "type": "CLOSE"}]
        broker.instrument = Mock(return_value=item)
        return broker

    def test_today_only_metadata_has_previous_completed_session(self):
        session = self.broker("2026-10-23").session_for(TICKER, NOW)
        self.assertTrue(session["open"])
        self.assertEqual(session["prior_close"], pd.Timestamp("2026-10-22T15:30Z"))

    def test_monday_after_dst_uses_fridays_close_and_new_opening_hour(self):
        session = self.broker("2026-10-26", "08:00", "16:30").session_for(TICKER, pd.Timestamp("2026-10-26T08:20Z"))
        self.assertTrue(session["open"])
        self.assertEqual(session["prior_close"], pd.Timestamp("2026-10-23T15:30Z"))
        self.assertEqual(session["next_open"], pd.Timestamp("2026-10-27T08:00Z"))

    def test_easter_holidays_are_not_treated_as_missing_daily_candles(self):
        session = self.broker("2026-04-07").session_for(TICKER, pd.Timestamp("2026-04-07T08:00Z"))
        self.assertEqual(session["prior_close"], pd.Timestamp("2026-04-02T15:30Z"))

    def test_reference_calendar_never_opens_an_empty_or_stale_broker_schedule(self):
        broker = self.broker("2026-10-23")
        self.assertFalse(broker.session_for(TICKER, pd.Timestamp("2026-10-26T10:00Z"))["open"])
        broker.instrument.return_value["schedule"]["timeEvents"] = []
        self.assertFalse(broker.session_for(TICKER, NOW)["open"])


class ProviderCacheRecoveryTests(Fixture):
    def test_yahoo_connection_failure_uses_and_remembers_its_other_host(self):
        data = YahooData(self.cfg)
        response = Mock()
        response.json.return_value = yahoo_chart()
        data.session.get = Mock(side_effect=[requests.ReadTimeout(), response, response])
        with patch("app.trading212.time.sleep"):
            data.bars(instrument(), SPEC, NOW)
            data.chart("SIE.DE", {"range": "1d", "interval": "1m"})
        hosts = [call.args[0] for call in data.session.get.call_args_list]
        self.assertTrue(hosts[0].startswith(YahooData.BASE))
        self.assertTrue(all(h.startswith(YahooData.ALTERNATE) for h in hosts[1:]))

    def test_yahoo_rate_limit_backs_off_without_trying_another_host(self):
        data = YahooData(self.cfg)
        response = Mock(status_code=429)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        data.session.get = Mock(return_value=response)
        with self.assertRaisesRegex(BrokerError, "HTTP 429"):
            data.chart("SIE.DE", {})
        with self.assertRaisesRegex(BrokerError, "waiting"):
            data.chart("SIE.DE", {})
        self.assertEqual(data.session.get.call_count, 1)

    def test_yahoo_forbidden_response_is_not_bypassed_via_another_host(self):
        data = YahooData(self.cfg)
        response = Mock(status_code=403)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        data.session.get = Mock(return_value=response)
        with self.assertRaisesRegex(BrokerError, "HTTP 403"):
            data.chart("SIE.DE", {})
        self.assertEqual(data.session.get.call_count, 1)

    def test_alternate_host_must_still_return_the_exact_requested_listing(self):
        data = YahooData(self.cfg)
        response = Mock()
        body = yahoo_chart()
        body["chart"]["result"][0]["meta"]["symbol"] = "SAP.DE"
        response.json.return_value = body
        data.session.get = Mock(side_effect=[requests.ReadTimeout(), response])
        with patch("app.trading212.time.sleep"), self.assertRaisesRegex(BrokerError, "does not match"):
            data.bars(instrument(), SPEC, NOW)

    def test_yahoo_retries_a_cached_response_missing_the_previous_session(self):
        old, fresh = yahoo_chart(), yahoo_chart()
        result = old["chart"]["result"][0]
        result["timestamp"] = result["timestamp"][:-2]  # Wednesday; Thursday is missing.
        for key in result["indicators"]["quote"][0]:
            result["indicators"]["quote"][0][key] = result["indicators"]["quote"][0][key][:-2]
        result["indicators"]["adjclose"][0]["adjclose"] = result["indicators"]["adjclose"][0]["adjclose"][:-2]
        data = YahooData(self.cfg)
        data.chart = Mock(side_effect=[result, fresh["chart"]["result"][0]])
        self.assertEqual(str(data.bars(instrument(), SPEC, NOW).index[-1].tz_convert("Europe/Berlin").date()), "2026-10-21")
        corrected = data.bars(instrument(), SPEC, NOW + pd.Timedelta(minutes=1))
        self.assertEqual(str(corrected.index[-1].tz_convert("Europe/Berlin").date()), "2026-10-22")
        self.assertIs(data.bars(instrument(), SPEC, NOW + pd.Timedelta(minutes=2)), corrected)
        self.assertEqual(data.chart.call_count, 2)

    def test_twelvedata_also_refreshes_an_incomplete_day_cache(self):
        old, fresh = bars(1100), bars(1100)
        for frame, end in ((old, "2026-10-21"), (fresh, "2026-10-22")):
            frame.index = pd.bdate_range(end=end, periods=len(frame), tz="Europe/Berlin").tz_convert("UTC")
            frame.attrs["market_timezone"] = "Europe/Berlin"
        data = MarketData(self.cfg)
        data.download_bars = Mock(side_effect=[old, fresh])
        data.bars(instrument(), SPEC, NOW)
        self.assertIs(data.bars(instrument(), SPEC, NOW + pd.Timedelta(minutes=1)), fresh)
        self.assertEqual(data.download_bars.call_count, 2)


class WorkerRecoveryTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.frame = bars(1100)
        self.frame.index = pd.bdate_range(end="2026-10-22", periods=len(self.frame), tz="Europe/Berlin").tz_convert("UTC")
        self.frame.attrs["market_timezone"] = "Europe/Berlin"
        self.data.bars.return_value = self.frame
        self.engine.predictions.forecast = Mock()
        self.engine.store.set("loop_strategies", self.engine.source.get("loop_strategies"))

    def refresh(self):
        Trading212Engine.signals_tick(self.engine, NOW, {TICKER: SPEC})
        self.engine.signals_thread.join(timeout=5)
        self.assertFalse(self.engine.signals_thread.is_alive())

    def test_today_only_schedule_produces_actionable_predictions_and_loop_inputs(self):
        real = Trading212(self.cfg)
        item = instrument()
        item["schedule"]["timeEvents"] = item["schedule"]["timeEvents"][2:]
        real.instrument = Mock(return_value=item)
        self.broker.session_for.side_effect = real.session_for
        result = self.engine.store.get("loop_results:trend_v1")
        with patch("app.trading212_engine.loop.evaluate", return_value=result) as evaluate:
            self.refresh()
            self.engine.loop_thread.join(timeout=5)
        frame = self.engine.predictions.forecast.call_args.args[1]
        self.assertEqual(frame.attrs["target_session"], "2026-10-23")
        self.assertEqual(evaluate.call_args.args[2][TICKER].attrs["target_session"], "2026-10-23")
        self.engine.cycle(NOW)
        self.broker.submit.assert_called_once()

    def test_slow_loop_cannot_block_a_new_prediction_refresh(self):
        entered, release = threading.Event(), threading.Event()
        result = self.engine.store.get("loop_results:trend_v1")
        def evaluate(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release loop worker")
            return result
        with patch("app.trading212_engine.loop.evaluate", side_effect=evaluate):
            try:
                self.refresh()
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.engine.loop_thread.is_alive())
                self.engine.store.set("retest_requested", True)
                self.refresh()
                self.assertEqual(self.engine.predictions.forecast.call_count, 2)
                self.assertEqual(self.engine.store.get("data_status")["status"], "ready")
            finally:
                release.set()
                if self.engine.loop_thread:
                    self.engine.loop_thread.join(timeout=5)

    def test_failed_data_is_retried_after_one_minute_and_the_error_clears(self):
        self.engine.store.set("loop_strategies", {})
        self.data.bars.side_effect = [BrokerError("provider temporarily unavailable"), self.frame]
        self.refresh()
        self.assertIn(TICKER, self.engine.store.get("data_errors"))
        self.assertEqual(self.engine.store.get("data_status")["status"], "error")
        self.engine.last_signals = time.monotonic() - 61
        self.refresh()
        self.assertEqual(self.data.bars.call_count, 2)
        self.assertEqual(self.engine.store.get("data_errors"), {})
        self.assertEqual(self.engine.store.get("data_status")["status"], "ready")

    def test_data_error_is_visible_even_without_a_buy_signal(self):
        self.engine.store.set("loop_results:trend_v1", {})
        for book in self.engine.books.values():
            book.set("data_errors", {TICKER: "missing Thursday candle"})
        self.engine.cycle(NOW)
        for book in self.engine.books.values():
            self.assertEqual(book.get(f"forecast:{TICKER}")["reason"], "missing Thursday candle")
        self.broker.submit.assert_not_called()

    def test_explicit_rate_limit_rejection_does_not_permanently_freeze_the_book(self):
        self.broker.submit.side_effect = [BrokerError("rate limited", 429), Trading212.normalize_order(remote())]
        self.engine.cycle(NOW)
        self.assertEqual(self.engine.store.orders()[0]["status"], "rejected")
        self.assertEqual(self.engine.store.orders()[0]["error"], "rate limited")
        self.assertEqual(self.engine.store.unresolved_orders(), [])
        self.assertIsNone(self.engine.store.owner(TICKER))
        self.engine.cycle(NOW + pd.Timedelta(minutes=16))
        self.assertEqual(self.broker.submit.call_count, 2)


class DiagnosticTests(Fixture):
    def test_status_and_download_show_the_failure_without_keys_or_balances(self):
        from app.trading212_engine import context
        cfg = replace(self.cfg, password="dashboard-secret")
        secondary = context(cfg)[1]
        secondary.set("engine", {"status": "running"})
        secondary.set("account", {"at": pd.Timestamp.now(tz="UTC").isoformat(), "id": "private-account", "equity": 123456789})
        secondary.set("data_errors", {TICKER: "provider unavailable"})
        client = create_app(cfg, self.store).test_client()
        self.assertEqual(client.get("/api/trading212/diagnostics").status_code, 401)
        response = client.get("/api/trading212/diagnostics", auth=("admin", "dashboard-secret"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        report = response.get_json()
        self.assertEqual(report["errors"][TICKER], "provider unavailable")
        self.assertIn("daily data is blocked", report["ready_problem"])
        text = json.dumps(report)
        for secret in (cfg.password, cfg.api_key, cfg.api_secret, cfg.trading212_key, cfg.trading212_secret,
                       "private-account", "123456789"):
            self.assertNotIn(secret, text)

    def test_authentication_error_identifies_the_account_environment(self):
        broker = Trading212(self.cfg)
        broker.session.request = Mock(return_value=Mock(ok=False, status_code=401))
        with self.assertRaisesRegex(BrokerError, "selected demo/live Invest account"):
            broker.account()
