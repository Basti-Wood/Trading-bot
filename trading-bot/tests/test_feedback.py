from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch
import unittest
import numpy as np
import pandas as pd

from app.features import FEATURES, FEATURE_SCHEMA, features, dataset
from app.feedback import capture_forecast, resolve_outcomes, replay_dataset, summary_key
from app.models import train_symbol, load_bundle, fit_model, MODEL_NAMES, strategy_signature
from app.store import Store
from app.web import create_app
from app.engine import Engine
from test_system import TempCase, bars


def session_bars(count=430):
    result = bars(count)
    # Actual Alpaca daily timestamps represent New York sessions, not UTC dates.
    result.index = pd.date_range("2023-01-02", periods=count, freq="B", tz="America/New_York").tz_convert("UTC")
    return result


def snapshot(data, index, expected=.75):
    at = data.index[index]
    return {"symbol": "SPY", "at": (at.to_pydatetime() + timedelta(days=1, hours=1)).isoformat(),
            "bar_at": at.isoformat(), "model_version": "recorded-v1", "eligible": False,
            "features": {name: float(features(data).loc[at, name]) for name in FEATURES},
            "expected_return": expected, "forecast_signal": "HOLD", "threshold": .003,
            "last_close": float(data.close.iloc[index]), "horizon": "next open to following open",
            "models": [{"id": name, "expected_return": expected, "signal": "HOLD"} for name in MODEL_NAMES]}


class ExperienceTests(TempCase):
    def test_first_snapshot_survives_restart_and_model_replacement(self):
        b = session_bars()
        original = snapshot(b, 100)
        self.assertTrue(capture_forecast(self.cfg, self.store, original))
        replacement = {**original, "model_version": "new-v2", "expected_return": -.25,
                       "features": {**original["features"], "return_1d": .99}}
        reopened = Store(self.cfg.data_dir)
        self.assertFalse(capture_forecast(self.cfg, reopened, replacement))
        rows = reopened.experience("SPY", "iex", FEATURE_SCHEMA)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model_version"], "recorded-v1")
        self.assertEqual(rows[0]["predictions"]["ensemble"], .75)
        self.assertEqual(rows[0]["features"], original["features"])

    def test_label_waits_for_two_completed_future_sessions_and_is_not_a_guess(self):
        b = session_bars()
        capture_forecast(self.cfg, self.store, snapshot(b, 100))
        now = b.index[-1].to_pydatetime() + timedelta(days=1)
        self.assertEqual(resolve_outcomes(self.cfg, self.store, "SPY", b.iloc[:102], now=now), 0)
        # Even if a provider accidentally includes today's bar, it cannot be a label.
        self.assertEqual(resolve_outcomes(self.cfg, self.store, "SPY", b.iloc[:103], now=b.index[102].to_pydatetime()+timedelta(hours=12)), 0)
        self.assertEqual(resolve_outcomes(self.cfg, self.store, "SPY", b, now=now), 1)
        expected = b.open.iloc[102] / b.open.iloc[101] - 1
        row = self.store.experience("SPY", "iex", FEATURE_SCHEMA)[0]
        self.assertAlmostEqual(row["outcome"]["target"], expected)
        self.assertNotEqual(row["outcome"]["target"], .75)
        summary = self.store.get(summary_key(self.cfg, "SPY"))
        self.assertAlmostEqual(summary["scores"]["ridge"]["mae"], abs(.75 - expected))
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["scores"]["ensemble"]["observations"], 1)
        revised = b.copy()
        revised.loc[b.index[102], "open"] *= 2
        self.assertEqual(resolve_outcomes(self.cfg, self.store, "SPY", revised, now=now), 0)
        self.assertAlmostEqual(self.store.experience("SPY", "iex", FEATURE_SCHEMA)[0]["outcome"]["target"], expected)

    def test_late_predictions_are_not_forward_evidence_or_training_examples(self):
        b = session_bars()
        record = snapshot(b, 100)
        record["at"] = (b.index[101].to_pydatetime() + timedelta(hours=12)).isoformat()
        capture_forecast(self.cfg, self.store, record)
        resolve_outcomes(self.cfg, self.store, "SPY", b)
        row = self.store.experience("SPY", "iex", FEATURE_SCHEMA)[0]
        self.assertEqual(row["status"], "excluded")
        self.assertIn("opened", row["outcome"]["reason"])
        replay, _ = replay_dataset(self.cfg, self.store, "SPY", b)
        self.assertEqual(int(replay.is_feedback.sum()), 0)

    def test_feed_and_feature_schema_cannot_mix(self):
        b = session_bars()
        record = snapshot(b, 100)
        capture_forecast(self.cfg, self.store, record)
        sip = replace(self.cfg, feed="sip")
        self.assertEqual(resolve_outcomes(sip, self.store, "SPY", b), 0)
        self.assertEqual(len(self.store.experience("SPY", "iex", FEATURE_SCHEMA, status="pending")), 1)
        self.assertEqual(self.store.experience("SPY", "iex", FEATURE_SCHEMA + 1), [])
        self.assertTrue(capture_forecast(sip, self.store, record))

    def test_replay_preserves_inputs_and_replaces_instead_of_duplicating_history(self):
        b = session_bars()
        record = snapshot(b, 100)
        capture_forecast(self.cfg, self.store, record)
        resolve_outcomes(self.cfg, self.store, "SPY", b)
        revised = b.copy()
        revised.loc[b.index[100], "close"] *= 1.25
        frame, revision = replay_dataset(self.cfg, self.store, "SPY", revised)
        self.assertEqual(len(frame), len(dataset(revised)))
        self.assertTrue(frame.index.is_unique)
        self.assertEqual(frame.loc[b.index[100], "sample_weight"], 2.)
        self.assertAlmostEqual(frame.loc[b.index[100], "return_1d"], record["features"]["return_1d"])
        self.assertNotAlmostEqual(frame.loc[b.index[100], "return_1d"], dataset(revised).loc[b.index[100], "return_1d"])
        self.assertEqual(int(frame.is_feedback.sum()), 1)
        self.assertEqual(replay_dataset(self.cfg, self.store, "SPY", revised)[1], revision)
        # Old experiences cannot silently enlarge the bounded fitting window.
        cropped, _ = replay_dataset(self.cfg, self.store, "SPY", b.tail(300))
        self.assertEqual(int(cropped.is_feedback.sum()), 0)

    def test_invalid_snapshot_and_same_day_input_are_rejected(self):
        b = session_bars()
        original = snapshot(b, 100)
        for changed in ({"expected_return": float("nan")}, {"at": b.index[100].isoformat()},
                        {"at": "2023-01-01T12:00:00"}):
            with self.assertRaises(ValueError):
                capture_forecast(self.cfg, self.store, {**original, **changed})
        self.assertEqual(len(self.store.experience("SPY", "iex", FEATURE_SCHEMA)), 0)

    def test_changed_history_horizon_is_not_silently_relabelled(self):
        b = session_bars()
        capture_forecast(self.cfg, self.store, snapshot(b, 100))
        resolve_outcomes(self.cfg, self.store, "SPY", b)
        frame, _ = replay_dataset(self.cfg, self.store, "SPY", b.drop(b.index[101]))
        self.assertEqual(int(frame.is_feedback.sum()), 0)


class ReplayTrainingTests(TempCase):
    def test_all_estimators_receive_explicit_feedback_weights(self):
        frame = dataset(session_bars()).iloc[:50].copy()
        frame["sample_weight"] = 1.
        frame.iloc[0, frame.columns.get_loc("sample_weight")] = 2.
        for name in MODEL_NAMES:
            fitted = Mock()
            with patch("app.models.estimator", return_value=fitted):
                fit_model(name, frame)
            options = fitted.fit.call_args.kwargs
            keys = ("standardscaler__sample_weight", "ridge__sample_weight") if name == "ridge" else ("sample_weight",)
            for key in keys:
                np.testing.assert_array_equal(options[key], frame.sample_weight)

    def test_real_training_uses_replay_but_not_recent_validation_or_purged_examples(self):
        b = session_bars()
        base = dataset(b)
        holdout_start = len(base) - self.cfg.validation_bars
        indices = [100, b.index.get_loc(base.index[holdout_start - 1]),
                   b.index.get_loc(base.index[holdout_start + 1])]
        records = []
        for i in indices:
            record = snapshot(b, i)
            record["features"]["volume_ratio"] = 10. if i == 100 else 1000.
            capture_forecast(self.cfg, self.store, record)
            records.append(record)
        train_symbol(self.cfg, self.store, "SPY", b)
        meta = self.store.get("model:SPY")
        self.assertEqual(meta["feedback"]["available_rows"], 3)
        self.assertEqual(meta["feedback"]["train_rows"], 1)
        self.assertEqual(meta["feedback"]["validation_rows"], 1)
        self.assertEqual(meta["feedback"]["purged_rows"], 1)
        self.assertEqual(meta["metrics"]["observations"], 30)
        frame, _ = replay_dataset(self.cfg, self.store, "SPY", b)
        train = frame[frame.label_end < pd.Timestamp(meta["metrics"]["start"])]
        expected_mean = np.average(train[FEATURES].to_numpy(), axis=0, weights=train.sample_weight)
        fitted = load_bundle(self.cfg, meta)["models"]["ridge"]
        np.testing.assert_allclose(fitted.named_steps["standardscaler"].mean_, expected_mean)
        self.assertLess(meta["trained_through"], meta["metrics"]["start"])

    def test_new_resolved_feedback_retrains_even_when_latest_market_bar_is_unchanged(self):
        b = session_bars()
        train_symbol(self.cfg, self.store, "SPY", b)
        old = self.store.get("candidate:SPY")
        capture_forecast(self.cfg, self.store, snapshot(b, 100))
        with patch("app.models.fit_model", wraps=fit_model) as fitting:
            train_symbol(self.cfg, self.store, "SPY", b)
        self.assertEqual(fitting.call_count, 12)
        new = self.store.get("candidate:SPY")
        self.assertNotEqual(new["feedback_revision"], old["feedback_revision"])
        self.assertEqual(new["feedback"]["train_rows"], 1)

    def test_feedback_settings_invalidate_old_validation(self):
        self.assertNotEqual(strategy_signature(self.cfg), strategy_signature(replace(self.cfg, feedback_weight=3)))
        self.assertNotEqual(strategy_signature(self.cfg), strategy_signature(replace(self.cfg, validation_bars=60)))
        for changed in ({"feedback_weight": 0}, {"feedback_weight": 4}, {"validation_bars": 1}):
            with self.assertRaises(ValueError): replace(self.cfg, **changed)


class FeedbackIntegrationTests(TempCase):
    def test_dashboard_exposes_outcomes_without_full_feature_replay(self):
        b = session_bars()
        capture_forecast(self.cfg, self.store, snapshot(b, 100))
        resolve_outcomes(self.cfg, self.store, "SPY", b)
        client = create_app(self.cfg, self.store).test_client()
        response = client.get("/api/state").get_json()
        feedback = response["symbols"][0]["feedback"]
        self.assertEqual(feedback["resolved"], 1)
        self.assertIn("ridge", feedback["scores"])
        self.assertNotIn("features", feedback["latest"][0])
        self.assertEqual(response["settings"]["validation_bars"], 30)
        with client.get("/") as page:
            self.assertIn('id="feedback-progress"', page.get_data(as_text=True))

    def test_engine_captures_hold_forecast_while_entries_are_paused(self):
        b = session_bars()
        forecast = snapshot(b, len(b) - 1, expected=0.)
        engine = Engine(self.cfg, self.store)
        engine.broker = Mock()
        engine.executor = Mock()
        engine.executor.reconcile.return_value = True
        engine.broker.account.return_value = {"equity":"10000", "last_equity":"10000", "cash":"10000", "buying_power":"10000"}
        engine.broker.clock.return_value = {"timestamp":forecast["at"], "is_open":False}
        engine.broker.positions.return_value = []
        engine.broker.open_orders.return_value = []
        engine.broker.bars.return_value = b
        engine.research_tick = Mock()
        self.store.set("model:SPY", {"version":"recorded-v1", "data_through":b.index[-1].isoformat(),
                                     "strategy_signature":strategy_signature(self.cfg)})
        with patch("app.engine.predict", return_value=forecast):
            engine.cycle()
            engine.cycle()
        self.assertFalse(self.store.get("entries_enabled"))
        self.assertEqual(len(self.store.experience("SPY", "iex", FEATURE_SCHEMA)), 1)
        engine.executor.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
