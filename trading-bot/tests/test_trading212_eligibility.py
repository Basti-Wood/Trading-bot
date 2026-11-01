"""Listing discovery and validation must match the strategy that can actually trade."""
from dataclasses import replace
import time
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from app.features import dataset
from app.models import train_symbol, strategy_signature
from app.trading212 import resolve_mappings, Trading212
from app.trading212_engine import context
from app.web import create_app
from test_system import bars
from test_trading212 import Fixture, EngineFixture, instrument, SPEC, TICKER, NOW


class KnownOutcomes:
    """Deterministic votes on held-out rows, so validation policy can be checked independently of fitting."""
    def __init__(self, targets, correct=True):
        self.targets, self.correct = targets, correct

    def predict(self, x):
        positive = self.targets.reindex(x.index).to_numpy() > 0
        return np.where(positive == self.correct, .01, -.01)


def alternating_bars():
    frame = bars(430)
    # Small gains and larger losses: averaging unrelated individual strategies fails,
    # while a consensus that trades only the gains remains profitable after costs.
    price = 100 * np.cumprod(1 + np.resize([.006, -.04], len(frame)))
    frame["open"] = frame["close"] = price
    frame["high"], frame["low"] = price * 1.001, price * .999
    frame.attrs["source_signature"] = "verified-listing"
    return frame


class ListingResolutionTests(Fixture):
    def test_absent_ticker_resolves_only_to_one_matching_listing(self):
        actual = {**instrument(), "ticker": "SIEMENSd_EQ"}
        result = resolve_mappings({TICKER: SPEC}, {actual["ticker"]: actual})
        self.assertEqual(result["specs"], {"SIEMENSd_EQ": SPEC})
        self.assertEqual(result["aliases"], {TICKER: "SIEMENSd_EQ"})
        self.assertEqual(result["errors"], {})

    def test_exact_ticker_takes_precedence_over_other_matching_listings(self):
        result = resolve_mappings({TICKER: SPEC}, {TICKER: instrument(), "ALTd_EQ": {**instrument(), "ticker": "ALTd_EQ"}})
        self.assertEqual(result["specs"], {TICKER: SPEC})
        self.assertEqual(result["aliases"], {})

    def test_us_wrong_currency_type_isin_or_exchange_cannot_resolve_a_missing_ticker(self):
        cases = [
            {"ticker": "SIE_US_EQ"}, {"currencyCode": "USD"}, {"type": "CFD"}, {"isin": "DE0007164600"},
            {"schedule": {"mic": "XNYS", "exchange": "NYSE"}},
            {"schedule": {"mic": "XETR", "exchange": "unknown", "mic_source": "mapping"}},
        ]
        for changes in cases:
            item = {**instrument(), "ticker": "OTHERd_EQ", **changes}
            result = resolve_mappings({TICKER: SPEC}, {item["ticker"]: item})
            self.assertEqual(result["specs"], {}, changes)
            self.assertIn(TICKER, result["errors"])

    def test_ambiguous_candidates_and_existing_ownership_require_review(self):
        items = {name: {**instrument(), "ticker": name} for name in ("NEWd_EQ", "ALTd_EQ")}
        result = resolve_mappings({TICKER: SPEC}, items)
        self.assertEqual(result["specs"], {})
        self.assertIn("2 matching", result["errors"][TICKER])
        result = resolve_mappings({TICKER: SPEC}, {"NEWd_EQ": items["NEWd_EQ"]}, {TICKER})
        self.assertEqual(result["specs"], {})
        self.assertIn("reconcile existing", result["errors"][TICKER])

    def test_missing_listings_do_not_remove_valid_stocks(self):
        result = resolve_mappings({TICKER: SPEC, "MISSINGd_EQ": {**SPEC, "isin": "DE0007100000"}}, {TICKER: instrument()})
        self.assertEqual(result["specs"], {TICKER: SPEC})
        self.assertEqual(list(result["errors"]), ["MISSINGd_EQ"])

    def test_a_duplicate_alias_does_not_replace_a_directly_configured_ticker(self):
        actual = "NEWd_EQ"
        result = resolve_mappings({TICKER: SPEC, actual: SPEC}, {actual: {**instrument(), "ticker": actual}})
        self.assertEqual(result["specs"], {actual: SPEC})
        self.assertEqual(result["aliases"], {})
        self.assertEqual(list(result["errors"]), [TICKER])

    def test_us_exchanges_are_excluded_instead_of_reported_as_unknown(self):
        broker = Trading212(self.cfg)
        exchanges = [{"name": name, "workingSchedules": [{"id": i}]} for i, name in enumerate(
            ("NYSE", "NASDAQ", "OTC Markets", "London Stock Exchange NON-ISA", "Unrecognised exchange"))]
        broker.cached = Mock(side_effect=lambda path, _: [] if path.endswith("instruments") else exchanges)
        broker.metadata()
        self.assertEqual(broker.unknown_exchanges, ["Unrecognised exchange"])

    def test_an_unknown_name_cannot_make_a_us_mic_trade_on_trading212(self):
        spec = {**SPEC, "mic": "XNYS"}
        item = {**instrument(), "schedule": {"mic": "XNYS", "exchange": "Unrecognised exchange"}}
        self.assertEqual(resolve_mappings({TICKER: spec}, {TICKER: item})["specs"], {})
        broker = Trading212(self.cfg)
        broker.metadata = Mock(return_value={TICKER: item})
        broker.request = Mock()
        with self.assertRaisesRegex(Exception, "verified non-US"):
            broker.submit(TICKER, "buy", 1, kind="market")
        broker.request.assert_not_called()


class NativeTickerExecutionTests(EngineFixture):
    def test_orders_and_ownership_use_the_verified_native_ticker(self):
        actual = "SIEMENSd_EQ"
        item = {**instrument(), "ticker": actual}
        self.broker.metadata.return_value = {actual: item}
        self.broker.instrument.return_value = item
        result = self.engine.store.get("loop_results:trend_v1")
        result["symbols"][actual] = result["symbols"].pop(TICKER)
        self.engine.store.set("loop_results:trend_v1", result)
        self.engine.cycle(NOW)
        self.assertEqual(self.broker.submit.call_args.args[0], actual)
        self.assertEqual(self.engine.store.orders()[0]["symbol"], actual)
        self.assertEqual(self.engine.store.owner(actual), "loop")
        self.assertIsNone(self.engine.store.owner(TICKER))
        self.assertEqual(self.engine.store.get("mapping_resolution")["aliases"], {TICKER: actual})
        self.assertEqual(self.store.orders(), [])

    def test_a_renamed_holding_is_not_automatically_adopted_after_a_flat_observation(self):
        actual = "SIEMENSd_EQ"
        self.filled_position()
        self.broker.positions.return_value[0]["symbol"] = actual
        self.broker.metadata.return_value = {actual: {**instrument(), "ticker": actual}}
        self.engine.store.set(f"flat_observed:{TICKER}", time.time() - 31)
        self.engine.cycle(NOW)
        resolution = self.engine.store.get("mapping_resolution")
        self.assertEqual(resolution["specs"], {})
        self.assertIn("reconcile existing", resolution["errors"][TICKER])
        self.assertIsNone(self.engine.store.owner(actual))
        self.broker.submit.assert_not_called()


class ConsensusValidationTests(Fixture):
    def fitted(self, correct=(True, True, False), votes=2, old=None):
        cfg, store = context(replace(self.cfg, trading212_min_votes=votes), "daily")
        frame = alternating_bars()
        if old:
            store.set(f"model:{TICKER}", {"version": "research", "eligible": False,
                "strategy_signature": strategy_signature(cfg), "input_signature": "verified-listing",
                "data_through": "2020-01-01T00:00:00+00:00", **old})
        target = dataset(frame).target
        names = {"ridge": correct[0], "extra_trees": correct[1], "hist_gradient": correct[2]}
        with patch("app.models.fit_model", side_effect=lambda name, data: KnownOutcomes(target, names[name])):
            train_symbol(cfg, store, TICKER, frame)
        return cfg, store, store.get(f"model:{TICKER}")

    def test_consensus_is_judged_on_its_own_walk_forward_trades(self):
        _, _, meta = self.fitted()
        individual = np.mean([f["net_return"] for report in meta["reports"].values() for f in report["walk_forward"]])
        self.assertLess(individual, 0)  # The previous, unrelated gate rejected this strategy.
        self.assertTrue(all(f["net_return"] > 0 for f in meta["ensemble_walk_forward"]))
        self.assertTrue(meta["eligible"])
        self.assertTrue(meta["validation"]["passed"])

    def test_three_vote_setting_is_also_enforced_in_validation(self):
        _, _, meta = self.fitted(votes=3)
        self.assertEqual(meta["metrics"]["active_trades"], 0)
        self.assertFalse(meta["eligible"])
        self.assertFalse(meta["validation"]["checks"]["trades"])
        self.assertIn("Only 0 validation trades", meta["validation"]["reasons"][0])

    def test_unprofitable_consensus_remains_blocked(self):
        _, _, meta = self.fitted(correct=(False, False, True))
        self.assertFalse(meta["eligible"])
        self.assertFalse(meta["validation"]["checks"]["return"])
        self.assertFalse(meta["validation"]["checks"]["walk_forward"])

    def test_unvalidated_incumbent_cannot_veto_a_valid_candidate(self):
        with patch("app.models.load_bundle", side_effect=AssertionError("Research models are not trading incumbents")):
            _, _, meta = self.fitted(old={"file": "old-research.joblib", "trained_through": "2099-01-01"})
        self.assertTrue(meta["eligible"])
        self.assertNotEqual(meta["version"], "research")

    def test_vote_and_risk_settings_invalidate_old_trading212_validation(self):
        cfg, _ = context(self.cfg, "daily")
        for changes in ({"trading212_min_votes": 3}, {"min_trades": 8}, {"max_drawdown": .1}):
            self.assertNotEqual(strategy_signature(cfg), strategy_signature(replace(cfg, **changes)))


class DiagnosticDetailTests(EngineFixture):
    def test_failed_computations_are_reported_separately_from_red_tests(self):
        self.engine.store.set("loop_strategies", self.engine.source.get("loop_strategies"))
        self.engine.store.set("loop_results:trend_v1", {})
        self.engine.store.set("loop_error:trend_v1", {"message": "worker timed out"})
        client = create_app(self.cfg, self.store).test_client()
        state = client.get("/api/state").get_json()["trading212"]
        tests = state["loop"]["tests"]
        self.assertEqual(tests[0]["status"], "error")
        self.assertEqual(tests[0]["tested"], 0)
        self.assertEqual(tests[0]["error"], "worker timed out")
        step = next(s for s in state["diagnosis"] if s["step"] == "Trading-Loop")
        self.assertIn("worker timed out", step["detail"])

    def test_no_qualified_model_or_order_is_a_waiting_state(self):
        cfg, daily = context(self.cfg, "daily")
        daily.set("stocks", [{"symbol": TICKER, "signal": "HOLD"}])
        daily.set(f"model:{TICKER}", {"eligible": False, "validation": {"reasons": ["Only 0 validation trades; at least 5 required"]}})
        state = create_app(self.cfg, self.store).test_client().get("/api/state").get_json()["trading212"]
        steps = {s["step"]: s for s in state["diagnosis"]}
        self.assertIsNone(steps["Prognose-Modelle"]["ok"])
        self.assertIn("Only 0 validation trades", steps["Prognose-Modelle"]["detail"])
        self.assertIsNone(steps["Orders"]["ok"])

    def test_a_failed_training_worker_remains_an_error(self):
        _, daily = context(self.cfg, "daily")
        daily.set("training", {"status": "worker failed", "message": "training process exited"})
        state = create_app(self.cfg, self.store).test_client().get("/api/state").get_json()["trading212"]
        step = next(s for s in state["diagnosis"] if s["step"] == "Prognose-Modelle")
        self.assertIs(step["ok"], False)
        self.assertIn("training process exited", step["detail"])


class ValidationRefreshTests(EngineFixture):
    def setup_attempt(self, signature):
        worker = self.engine.predictions
        frame = bars()
        frame.attrs["source_signature"] = "listing"
        worker.set_frames({TICKER: frame})
        worker.store.set(f"model:{TICKER}", {"strategy_signature": "old-schema", "input_signature": "listing",
            "created_at": pd.Timestamp.now(tz="UTC").isoformat(), "data_through": frame.index[-1].isoformat()})
        worker.store.set("last_full_training", time.time())
        worker.store.set(f"training_attempt:{TICKER}", time.time())
        worker.store.set(f"training_attempt_signature:{TICKER}", signature)
        return worker

    def test_new_validation_settings_retrain_without_waiting_for_an_old_failure_backoff(self):
        worker = self.setup_attempt("old-schema:listing")
        with patch("app.trading212_prediction.mp.get_context") as spawn:
            worker.training_tick()
            spawn.return_value.Process.return_value.start.assert_called_once()
        worker.trainer = None

    def test_a_failure_under_the_same_settings_keeps_its_backoff(self):
        worker = self.setup_attempt(strategy_signature(self.engine.daily_cfg) + ":listing")
        with patch("app.trading212_prediction.mp.get_context") as spawn:
            worker.training_tick()
            spawn.assert_not_called()
