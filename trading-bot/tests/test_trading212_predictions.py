from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from app.broker import BrokerError
from app.models import strategy_signature
from app.portfolio import PortfolioEngine
from app.store import StrategyStore
from app.trading212 import Trading212, YahooData
from app.trading212_engine import context, source_signature, Trading212Engine
from app.web import create_app
from test_system import bars
from test_trading212 import EngineFixture, Fixture, NOW, TICKER, SPEC, remote, instrument
from test_broker_routing import yahoo_chart


class PredictionExecutionTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.engine.store.set("entries_enabled", False)
        self.engine.daily.set("entries_enabled", True)
        self.forecast()

    def forecast(self, signals=("BUY", "BUY", "BUY"), eligible=True):
        signature = source_signature(self.cfg, SPEC)
        self.engine.daily.set(f"model:{TICKER}", {"version": "m1", "strategy_signature": strategy_signature(self.engine.daily_cfg),
            "created_at": (NOW - pd.Timedelta(hours=1)).isoformat(), "data_through": "2026-10-21T22:00:00Z", "input_signature": signature})
        self.engine.daily.set(f"prediction:{TICKER}", {"symbol": TICKER, "model_version": "m1", "eligible": eligible,
            "expected_return": .01, "forecast_signal": signals[0], "bar_at": "2026-10-21T22:00:00Z",
            "target_session": "2026-10-23", "market_timezone": "Europe/Berlin", "source_signature": signature,
            "trading_mode": "daily", "models": [{"id": f"m{i}", "name": f"Model {i}", "signal": s, "expected_return": .01}
                                                for i, s in enumerate(signals)]})

    def daily_position(self, qty=3, actual=3):
        self.engine.daily.reserve_order("daily-entry", {"symbol": TICKER, "side": "buy", "type": "limit", "qty": qty,
            "filled_qty": qty, "status": "filled", "position_generation": "legacy"})
        self.engine.daily.set(f"owned:{TICKER}", True)
        self.broker.positions.return_value = [{"symbol": TICKER, "qty": actual, "available_qty": actual,
            "avg_entry_price": 100, "current_price": 101, "market_value": actual * 101, "unrealized_pl": actual,
            "currency": "EUR", "instrument_currency": "EUR"}]

    def test_three_models_can_buy_without_any_active_loop_version(self):
        self.engine.source.set("loop_strategies", {})
        self.engine.cycle(NOW)
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args, (TICKER, "buy", 4))
        self.assertEqual(self.engine.store.owner(TICKER), "daily")
        self.assertEqual(self.engine.store.orders()[0]["strategy"], "daily")
        self.assertEqual(self.store.orders(), [])

    def test_alpaca_setup_and_us_market_state_do_not_gate_european_predictions(self):
        self.engine.config = replace(self.cfg, api_key="", api_secret="")
        self.store.set("account", {"market_open": True, "cash": 999999})
        self.engine.cycle(NOW)
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args[0], TICKER)
        self.assertEqual(self.store.get("account")["cash"], 999999)

    def test_two_bullish_models_do_not_bypass_the_three_vote_setting(self):
        self.forecast(("BUY", "BUY", "HOLD"))
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()

    def test_unvalidated_models_are_visible_but_cannot_buy(self):
        self.forecast(eligible=False)
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.assertIn("validation", self.engine.daily.get(f"forecast:{TICKER}")["reason"])

    def test_expired_models_cannot_buy(self):
        meta = self.engine.daily.get(f"model:{TICKER}")
        self.engine.daily.set(f"model:{TICKER}", {**meta, "created_at": (NOW - pd.Timedelta(days=8)).isoformat()})
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.assertEqual(self.engine.daily.get("stocks")[0]["quality"]["status"], "model_expired")

    def test_mapping_changes_invalidate_existing_forecasts(self):
        self.mapping.write_text(json.dumps({TICKER: {**SPEC, "yahoo": "OTHER.DE"}}))
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.assertIn("mapping changed", self.engine.daily.get(f"forecast:{TICKER}")["reason"])

    def test_each_book_has_a_separate_capital_share(self):
        self.engine.config = replace(self.cfg, trading212_prediction_share=.2)
        self.engine.cycle(NOW)
        self.assertEqual(self.broker.submit.call_args.args[2], 1)
        self.assertAlmostEqual(self.engine.strategy_share("loop"), .8)

    def test_closed_european_exchange_never_creates_a_buy(self):
        self.broker.session_for.return_value["open"] = False
        self.engine.cycle(NOW)
        self.broker.submit.assert_not_called()
        self.data.quote.assert_not_called()

    def test_paused_daily_book_still_protects_only_its_confirmed_shares(self):
        self.daily_position(qty=3, actual=8)
        self.engine.daily.set("entries_enabled", False)
        self.engine.cycle(NOW)
        self.assertEqual(self.broker.submit.call_args.args, (TICKER, "sell", 3.))
        self.assertEqual(self.broker.submit.call_args.kwargs["kind"], "stop")
        self.assertFalse(self.engine.store.get(f"owned:{TICKER}", False))

    def test_loop_cannot_adopt_or_sell_a_prediction_position(self):
        self.daily_position()
        self.engine.store.set("entries_enabled", True)
        self.engine.cycle(NOW)
        self.assertEqual(self.engine.store.owner(TICKER), "daily")
        loop_row = self.engine.store.get(f"forecast:{TICKER}")
        self.assertEqual(loop_row["action"], "WAIT")
        self.assertIn("daily", loop_row["reason"])

    def test_daily_exit_survives_a_forecast_change_while_stop_is_canceled(self):
        self.daily_position()
        self.forecast(("SELL", "SELL", "SELL"), eligible=False)
        self.engine.daily.reserve_order("daily-stop", {"symbol": TICKER, "side": "sell", "type": "stop", "qty": 3,
            "filled_qty": 0, "status": "new", "broker_id": "stop", "position_generation": "legacy"})
        stop = Trading212.normalize_order(remote(oid="stop", side="SELL", kind="STOP"))
        self.broker.open_orders.return_value = [stop]
        self.engine.cycle(NOW)
        self.broker.cancel.assert_called_once_with("stop")
        self.broker.submit.assert_not_called()
        self.forecast()  # Now bullish, but the initiated exit must complete.
        self.broker.open_orders.return_value = []
        self.broker.order.return_value = {**stop, "status": "canceled"}
        self.engine.cycle(NOW)
        self.assertEqual(self.broker.submit.call_args.args, (TICKER, "sell", 3.))
        self.assertEqual(self.broker.submit.call_args.kwargs["kind"], "market")

    def test_loop_and_predictions_get_turns_without_duplicate_symbol_ownership(self):
        self.engine.store.set("entries_enabled", True)
        self.broker.submit.side_effect = [BrokerError("definite rejection", 422), Trading212.normalize_order(remote())]
        self.engine.cycle(NOW)
        self.assertIsNone(self.engine.store.owner(TICKER))
        self.engine.cycle(NOW)
        self.assertEqual(self.engine.store.owner(TICKER), "daily")
        self.assertEqual(self.broker.submit.call_count, 2)


class PredictionPipelineTests(EngineFixture):
    def test_cached_models_leave_waiting_status_after_data_arrives_on_restart(self):
        frame = bars(430)
        frame.attrs["source_signature"] = "same-listing"
        self.engine.daily.set(f"model:{TICKER}", {"input_signature": "same-listing"})
        self.engine.predictions.set_frames({TICKER: frame})
        self.assertEqual(self.engine.daily.get("training")["status"], "ready")

    def test_spawned_worker_trains_and_forecasts_non_us_data_without_alpaca(self):
        frame = bars(430)
        frame.index = pd.bdate_range(end="2026-10-22", periods=len(frame), tz="Europe/Berlin").tz_convert("UTC")
        frame.attrs.update(market_timezone="Europe/Berlin", target_session="2026-10-23",
                           input_complete_at="2026-10-22T15:30:00Z", source_signature=source_signature(self.cfg, SPEC))
        bad = pd.DataFrame(columns=frame.columns, index=pd.DatetimeIndex([], tz="UTC"))
        worker = self.engine.predictions
        worker.set_frames({TICKER: frame, "BADd_EQ": bad})
        try:
            worker.training_tick()
            self.assertIsNotNone(worker.trainer)
            worker.trainer.join(timeout=45)
            self.assertFalse(worker.trainer.is_alive(), "Training worker exceeded the test timeout")
            self.assertEqual(worker.trainer.exitcode, 0)
            worker.training_tick(allow_start=False)
            self.assertTrue(self.engine.store.get("retest_requested"))
            status = self.engine.daily.get("training")
            self.assertEqual(status["status"], "partial failure")
            self.assertEqual(status["failed_symbols"], ["BADd_EQ"])
            self.assertIsNone(self.store.get(f"model:{TICKER}"))
            self.assertEqual(self.engine.daily_cfg.broker_name, "trading212")
            self.assertEqual(self.engine.daily_cfg.feed, "trading212-yahoo")
            worker.forecast(TICKER, frame)
            forecast = self.engine.daily.get(f"prediction:{TICKER}")
            self.assertEqual(len(forecast["models"]), 3)
            self.assertEqual(forecast["target_session"], "2026-10-23")
            self.assertEqual(forecast["market_timezone"], "Europe/Berlin")
            self.assertEqual(self.engine.daily.experience(TICKER, "trading212-yahoo", 1)[0]["model_version"], forecast["model_version"])
        finally:
            worker.shutdown()

    def test_loop_failure_does_not_prevent_prediction_data_from_being_ready(self):
        frame = bars(1100)
        frame.index = pd.bdate_range(end="2026-10-22", periods=len(frame), tz="Europe/Berlin").tz_convert("UTC")
        frame.attrs["market_timezone"] = "Europe/Berlin"
        self.data.bars.return_value = frame
        self.engine.store.set("loop_strategies", self.engine.source.get("loop_strategies"))
        with patch("app.trading212_engine.loop.evaluate", side_effect=BrokerError("one bad strategy")):
            Trading212Engine.signals_tick(self.engine, NOW, {TICKER: SPEC})
            self.engine.signals_thread.join(timeout=5)
            self.engine.loop_thread.join(timeout=5)
        self.assertIn(TICKER, self.engine.predictions.frames)
        self.assertEqual(self.engine.predictions.frames[TICKER].attrs["target_session"], "2026-10-23")
        self.assertIn("one bad strategy", self.engine.store.get("loop_error:trend_v1")["message"])


class SharedTrainingTests(unittest.TestCase):
    def coordinator(self):
        coordinator = PortfolioEngine.__new__(PortfolioEngine)
        coordinator.engines = [Mock(trainer=None)]
        coordinator.training_peers = [Mock(trainer=None)]
        coordinator.training_turn = 0
        return coordinator

    def test_an_active_alpaca_trainer_prevents_a_second_training_process(self):
        coordinator = self.coordinator()
        coordinator.engines[0].trainer = object()
        coordinator.training_tick()
        coordinator.training_peers[0].training_tick.assert_called_once_with(allow_start=False)

    def test_an_alpaca_worker_error_does_not_starve_trading212_training(self):
        coordinator = self.coordinator()
        coordinator.engines[0].training_tick.side_effect = ValueError("old journal value")
        peer = coordinator.training_peers[0]
        def tick(allow_start=True):
            if allow_start:
                peer.trainer = object()
        peer.training_tick.side_effect = tick
        coordinator.training_tick()
        self.assertIsNotNone(peer.trainer)
        self.assertEqual(coordinator.training_turn, 0)


class PredictionControlTests(Fixture):
    def client(self):
        cfg = replace(self.cfg, api_key="", api_secret="")
        app = create_app(cfg, self.store)
        return cfg, app.test_client()

    def test_prediction_switch_is_independent_of_alpaca_and_loop_activation(self):
        cfg, client = self.client()
        response = client.post("/api/trading212/control", json={"action": "resume", "strategy": "daily"},
                               headers={"X-Bot-Control": "dashboard"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(context(cfg, "daily")[1].get("entries_enabled"))
        self.assertFalse(context(cfg)[1].get("entries_enabled", False))

    def test_top_control_can_enable_available_trading212_when_alpaca_is_unconfigured(self):
        cfg, client = self.client()
        response = client.post("/api/control", json={"action": "resume"}, headers={"X-Bot-Control": "dashboard"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(context(cfg, "daily")[1].get("entries_enabled"))
        self.assertFalse(self.store.get("entries_enabled", False))

    def test_pause_all_reaches_daily_and_loop_without_disabling_protection(self):
        cfg, client = self.client()
        for mode in ("daily", "loop"):
            context(cfg, mode)[1].set("entries_enabled", True)
        response = client.post("/api/control", json={"action": "pause"}, headers={"X-Bot-Control": "dashboard"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(not context(cfg, mode)[1].get("entries_enabled") for mode in ("daily", "loop")))

    def test_state_reports_training_votes_and_independent_budgets(self):
        _, client = self.client()
        state = client.get("/api/state").get_json()["trading212"]
        self.assertEqual(state["prediction"]["share"], .5)
        self.assertEqual(state["loop"]["share"], .5)
        self.assertEqual(state["prediction"]["min_votes"], 3)
        self.assertIn("training", state["prediction"])
        self.assertIn("parallel", state["execution_policy"])


class SourceIdentityTests(Fixture):
    def test_same_exchange_and_currency_cannot_mask_the_wrong_stock(self):
        data = YahooData(self.cfg)
        body = yahoo_chart()
        body["chart"]["result"][0]["meta"]["symbol"] = "SAP.DE"
        data.chart = Mock(return_value=body["chart"]["result"][0])
        with self.assertRaisesRegex(BrokerError, "symbol"):
            data.quote(instrument(), SPEC, pd.Timestamp("2026-10-23T12:00Z"))

    def test_an_unknown_instrument_type_is_not_accepted_as_equity(self):
        data = YahooData(self.cfg)
        body = yahoo_chart(kind="MUTUALFUND")
        data.chart = Mock(return_value=body["chart"]["result"][0])
        with self.assertRaises(BrokerError):
            data.bars(instrument(), SPEC, pd.Timestamp("2026-10-23T12:00Z"))
