from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import numpy as np
import pandas as pd

from app.config import Config
from app.store import Store, utcnow
from app.broker import Alpaca, BrokerError
from app.features import dataset, features, FEATURES
from app.models import train_symbol, predict, evaluate, load_bundle
from app.research import Research, combine
from app.execution import Executor
from app.engine import Engine
from app.web import create_app
from app.lock import InstanceLock


def bars(count=430):
    idx = pd.date_range("2023-01-02", periods=count, freq="B", tz="UTC")
    close = 100 * np.exp(np.arange(count) * .006 + np.sin(np.arange(count)) * .0001)
    return pd.DataFrame({"open": close * .999, "high": close * 1.005, "low": close * .995,
                         "close": close, "volume": 100000 + np.arange(count) * 10}, index=idx)


def models(*signals):
    """Per-model ballots; one signal is repeated for all three prediction models."""
    signals = signals * 3 if len(signals) == 1 else signals
    return [{"id": f"m{i}", "name": f"Model {i}", "signal": s} for i, s in enumerate(signals)]


def book(fired=True, bar_at="2026-10-15T13:44:00Z", strategy="vwap_pullback", stop=99.5, target=101., exit="target"):
    """A stored day-trading playbook result whose last one-minute bar closed at 13:45."""
    signals = [{"strategy": strategy, "name": "Test setup", "entry": 100., "stop": stop, "target": target,
                "exit": exit, "reason": "test"}] if fired else []
    return {"signal": "BUY" if fired else "HOLD", "signals": signals, "bar_at": bar_at,
            "indicators": {"close": 100., "vwap": 99.8, "noise_upper": 99.7}}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=Path(self.temp.name), api_key="test-key", api_secret="test-secret", llm_key="llm-key", symbols=("SPY",), universe_mode="watchlist")
        self.store = Store(self.cfg.data_dir)


class ConfigTests(unittest.TestCase):
    def test_live_requires_explicit_gate(self):
        with self.assertRaises(ValueError): Config(paper=False)

    def test_resource_limits_and_nan_rejected(self):
        for options in ({"training_memory_mb": 9000}, {"capital": float("nan")}, {"max_bars": 100000}, {"symbols": ("SPY", "SPY")}):
            with self.assertRaises(ValueError): Config(**options)

    def test_network_host_requires_password(self):
        with self.assertRaises(ValueError): Config(host="0.0.0.0")


class ModelTests(TempCase):
    def test_future_prices_cannot_change_past_features(self):
        original = bars()
        changed = original.copy()
        changed.iloc[300:, :4] *= 4
        pd.testing.assert_frame_equal(features(original).loc[:original.index[299]], features(changed).loc[:original.index[299]])

    def test_targets_start_after_signal_and_drop_unknown_labels(self):
        b = bars()
        frame = dataset(b)
        i = 100
        self.assertAlmostEqual(frame.loc[b.index[i], "target"], b.open.iloc[i+2] / b.open.iloc[i+1] - 1)
        self.assertNotIn(b.index[-1], frame.index)
        self.assertNotIn(b.index[-2], frame.index)
        self.assertEqual(frame.loc[b.index[i], "label_end"], b.index[i+2])

    def test_real_training_checkpoint_and_inference(self):
        b = bars()
        train_symbol(self.cfg, self.store, "SPY", b)
        meta = self.store.get("model:SPY")
        self.assertLess(meta["trained_through"], meta["metrics"]["start"])
        result = predict(self.cfg, meta, b)
        self.assertEqual(len(result["models"]), 3)
        self.assertEqual(result["bar_at"], b.index[-1].isoformat())
        self.assertTrue(np.isfinite(result["expected_return"]))
        bundle = load_bundle(self.cfg, meta)
        frame = dataset(b)
        cutoff = frame.index[len(frame) - self.cfg.validation_bars]
        training = frame[frame.label_end < cutoff]
        np.testing.assert_allclose(bundle["models"]["ridge"].named_steps["standardscaler"].mean_, training[FEATURES].mean())
        train_symbol(self.cfg, self.store, "SPY", b)
        self.assertEqual(self.store.get("model:SPY")["version"], meta["version"])

    def test_costs_stop_and_ambiguous_intrabar_paths(self):
        frame = dataset(bars()).iloc[:10].copy()
        frame["target"] = .01
        frame["low_next"] = frame.entry * .90
        frame["high_next"] = frame.entry * 1.20
        result = evaluate(frame, np.ones(10), self.cfg)
        self.assertLess(result["net_return"], 0)
        self.assertEqual(evaluate(frame, np.zeros(10), self.cfg)["net_return"], 0)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.f = {"eligible": True, "forecast_signal": "BUY", "bar_at": "bar", "models": models("BUY")}
        self.two = {**self.f, "models": models("BUY", "BUY", "HOLD")}
        self.r = {"status": "ready", "action": "BUY", "bar_at": "bar", "at": utcnow()}

    def test_three_of_four_votes_trade(self):
        # All three models agree: a neutral or opposing LLM cannot block the trade.
        for news in ("BUY", "HOLD", "SELL"):
            self.assertEqual(combine(self.f, {**self.r, "action": news})[0], "BUY")
        # Two models need the LLM as the third vote.
        self.assertEqual(combine(self.two, self.r)[0], "BUY")
        self.assertEqual(combine(self.two, {**self.r, "action": "HOLD"})[0], "HOLD")
        self.assertEqual(combine(self.two, {**self.r, "action": "SELL"})[0], "HOLD")
        self.assertEqual(combine({**self.f, "eligible": False}, self.r)[0], "HOLD")
        self.assertEqual(combine({**self.f, "models": models("SELL")}, {**self.r, "action": "HOLD"})[0], "SELL")
        self.assertEqual(combine(self.two, {**self.r, "action": "HOLD"}, min_votes=2)[0], "BUY")

    def test_missing_stale_or_misaligned_news_abstains(self):
        # The once-per-day opinion stays valid for its daily bar, including over a weekend.
        self.assertEqual(combine(self.two, {**self.r, "at": (datetime.now(timezone.utc)-timedelta(days=3)).isoformat()})[0], "BUY")
        for r in (None, {**self.r, "status": "unavailable"}, {**self.r, "bar_at": "other"},
                  {**self.r, "at": (datetime.now(timezone.utc)-timedelta(days=5)).isoformat()}):
            self.assertEqual(combine(self.f, r)[0], "BUY")
            self.assertEqual(combine(self.two, r)[0], "HOLD")

    def test_fabricated_citations_invalid_actions_and_nonfinite_confidence(self):
        valid = {"action": "BUY", "confidence": .8, "rationale": "Positive earnings", "source_ids": [1]}
        for override in ({"source_ids": [42]}, {"confidence": float("nan")}, {"action": "BUY NOW"}, {"rationale": None}):
            with self.assertRaises(ValueError): Research.validate(json.dumps({**valid, **override}), [{"id": 1}])
        self.assertEqual(Research.validate(json.dumps({**valid, "source_ids": []}), [{"id": 1}])["action"], "HOLD")
        self.assertEqual(Research.validate(json.dumps({**valid, "confidence": .5}), [{"id": 1}])["action"], "HOLD")

    def test_proxy_uses_auto_and_records_returned_model(self):
        cfg = Config(llm_key="secret")
        research = Research(cfg, Mock())
        research.news = Mock(return_value=([{"id":1}], []))
        response = Mock(ok=True)
        response.json.return_value = {"model":"routed-model", "choices":[{"message":{"content":json.dumps({"action":"BUY","confidence":.8,"rationale":"Evidence summary","source_ids":[1]})}}]}
        research.http.post = Mock(return_value=response)
        result = research.analyze("SPY", {"last_close": 100, "horizon":"one day", "bar_at":"bar"})
        args = research.http.post.call_args
        self.assertEqual(args.args[0], "https://ai.bastiwood.com/v1/chat/completions")
        self.assertEqual(args.kwargs["json"]["model"], "auto")
        self.assertNotIn("tools", args.kwargs["json"])
        self.assertEqual(result["opinions"][0]["model"], "routed-model")


class ExecutionTests(TempCase):
    def setUp(self):
        super().setUp()
        self.broker = Mock(base="https://paper-api.alpaca.markets")
        self.broker.latest_price.return_value = 100
        self.broker.asset.return_value = {"tradable": True, "status": "active"}
        self.broker.submit.return_value = {"id":"broker1", "status":"new", "filled_qty":"0"}
        self.executor = Executor(self.cfg, self.store, self.broker)
        self.account = {"status":"ACTIVE", "equity":"10000", "cash":"10000", "buying_power":"20000"}
        self.store.set("entries_enabled", True)

    def execute(self, action="BUY", positions=None, orders=None, **kwargs):
        return self.executor.execute("SPY", action, "2026-10-14", self.account, positions or [], orders or [], **kwargs)

    def test_whole_share_bracket_single_atomic_order_and_no_duplicates(self):
        self.execute()
        self.execute()
        self.broker.submit.assert_called_once()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual(payload["order_class"], "bracket")
        self.assertEqual(payload["qty"], "9")
        self.assertIn("stop_loss", payload)
        self.assertEqual(self.store.orders()[0]["status"], "new")

    def test_timeout_is_persisted_and_never_blindly_retried(self):
        self.broker.submit.side_effect = BrokerError("timeout")
        self.execute()
        self.execute()
        self.broker.submit.assert_called_once()
        self.assertEqual(self.store.orders()[0]["status"], "unknown")
        self.broker.order.side_effect = BrokerError("not found", 404)
        self.assertFalse(self.executor.reconcile())
        self.assertEqual(self.store.orders()[0]["status"], "unknown")

    def test_reconciliation_uses_actual_fill(self):
        self.execute()
        self.broker.order.return_value={"id":"broker1", "status":"filled", "filled_qty":"9", "filled_avg_price":"100.10"}
        self.assertTrue(self.executor.reconcile())
        self.assertEqual(self.store.orders()[0]["filled_avg_price"], "100.10")

    def test_paused_loss_halt_and_pending_orders_block_new_buy(self):
        self.store.set("entries_enabled", False)
        self.execute()
        self.store.set("entries_enabled", True)
        self.execute(loss_breach=True)
        self.execute(orders=[{"symbol":"QQQ", "side":"buy"}])
        self.broker.submit.assert_not_called()

    def test_total_account_exposure_counts_even_unwatched_positions(self):
        self.execute(positions=[{"symbol":"AAPL", "qty":"40", "market_value":"4000"}])
        self.broker.submit.assert_not_called()

    def test_no_short_and_no_touching_unowned_positions(self):
        self.execute("SELL")
        self.execute("SELL", positions=[{"symbol":"SPY", "qty":"3", "market_value":"300"}])
        self.broker.submit.assert_not_called()

    def test_exit_waits_for_cancellation_and_works_while_paused(self):
        self.store.set("entries_enabled", False)
        self.store.set("owned:SPY", True)
        position = [{"symbol":"SPY", "qty":"3", "market_value":"300"}]
        self.execute("SELL", position, [{"symbol":"SPY", "side":"sell", "type":"stop", "id":"stop1"}])
        self.broker.cancel.assert_called_once_with("stop1")
        self.broker.submit.assert_not_called()
        self.execute("SELL", position)
        self.assertEqual(self.broker.submit.call_args.args[0]["qty"], "3")

    def test_rejected_exit_retries_but_uncertain_exit_does_not(self):
        self.store.set("owned:SPY", True)
        position=[{"symbol":"SPY", "qty":"3", "market_value":"300"}]
        self.broker.submit.side_effect=[BrokerError("rejected",422),BrokerError("timeout")]
        self.execute("SELL",position)
        self.execute("SELL",position)
        self.execute("SELL",position)
        self.assertEqual(self.broker.submit.call_count,2)
        self.assertEqual({o['status'] for o in self.store.orders()},{'rejected','unknown'})


class BrokerTests(unittest.TestCase):
    def test_paginated_bars_exclude_unfinished_day(self):
        broker=Alpaca(Config(api_key="k",api_secret="s"))
        broker.calendar = Mock(return_value=[{'date': '2026-10-13', 'close': '16:00'}, {'date': '2026-10-14', 'close': '16:00'}])
        row=lambda day: {"t":f"2026-10-{day}T04:00:00Z", "o":100,"h":101,"l":99,"c":100,"v":1000}
        broker.request=Mock(side_effect=[{"bars":[row("12")],"next_page_token":"next"},{"bars":[row("13"),row("14")]}])
        result=broker.bars("SPY",datetime(2026,10,14,15,tzinfo=timezone.utc))
        self.assertEqual(len(result),2)
        self.assertEqual(result.index[-1].day,13)

    def test_stale_price_rejected(self):
        broker=Alpaca(Config())
        broker.request=Mock(return_value={"trade":{"p":100,"t":"2020-01-01T00:00:00Z"}})
        with self.assertRaises(BrokerError): broker.latest_price("SPY")


class WebTests(TempCase):
    def test_auth_and_cross_origin_controls_and_secret_redaction(self):
        app=create_app(replace(self.cfg,password="secret-password"),self.store)
        client=app.test_client()
        self.assertEqual(client.get("/api/state").status_code,401)
        auth={"Authorization":"Basic "+base64.b64encode(b"admin:secret-password").decode()}
        response=client.get("/api/state",headers=auth)
        self.assertEqual(response.status_code,200)
        for secret in ("test-key","test-secret","llm-key","secret-password"):
            self.assertNotIn(secret,response.get_data(as_text=True))
        self.assertEqual(client.post("/api/control",json={"action":"resume"},headers=auth).status_code,403)
        auth["X-Bot-Control"]="dashboard"
        self.assertEqual(client.post("/api/control",json={"action":"pause"},headers={**auth,"Origin":"https://evil.example"}).status_code,403)
        self.store.set("account", {"loss_halt":False})
        self.assertEqual(client.post("/api/control",json={"action":"resume"},headers={**auth,"Origin":"https://trade.bastiwood.com"}).status_code,200)
        self.assertEqual(client.post("/api/control",json={"action":"resume"},headers=auth).status_code,200)
        self.assertTrue(self.store.get("entries_enabled"))

    def test_dashboard_assets_and_validation(self):
        client=create_app(self.cfg,self.store).test_client()
        for path in ("/","/assets/app.js","/assets/style.css","/healthz"):
            with client.get(path) as response:
                self.assertEqual(response.status_code,200)
        headers={"X-Bot-Control":"dashboard"}
        self.assertEqual(client.post("/api/control",json=[],headers=headers).status_code,400)
        self.assertEqual(client.post("/api/control",json={"action":"unknown"},headers=headers).status_code,400)

    def test_daily_halt_cannot_be_resumed(self):
        self.store.set("account",{"loss_halt":True})
        response=create_app(self.cfg,self.store).test_client().post("/api/control",json={"action":"resume"},headers={"X-Bot-Control":"dashboard"})
        self.assertEqual(response.status_code,409)


class EngineTests(TempCase):
    def test_restart_pauses_entries_and_account_switch_requires_new_directory(self):
        self.store.set("entries_enabled",True)
        Engine(self.cfg,self.store)
        self.assertFalse(self.store.get("entries_enabled"))
        with self.assertRaises(ValueError): Engine(replace(self.cfg,api_key="different-account"),self.store)

    def test_risk_exit_without_any_model_or_llm(self):
        engine=Engine(self.cfg,self.store)
        engine.broker=Mock()
        engine.executor=Mock()
        engine.executor.reconcile.return_value=True
        engine.executor.execute.return_value="exit submitted"
        engine.broker.account.return_value={"equity":"9500","last_equity":"10000","cash":"9000","buying_power":"9000"}
        engine.broker.clock.return_value={"timestamp":"2026-10-14T14:00:00Z","is_open":True}
        engine.broker.positions.return_value=[{"symbol":"SPY","qty":"5","unrealized_plpc":"-.06"}]
        engine.broker.open_orders.return_value=[]
        self.store.set("owned:SPY",True)
        engine.cycle()
        self.assertTrue(self.store.get("account")["loss_halt"])
        self.assertEqual(engine.executor.execute.call_args.args[1],"SELL")

    def test_single_instance_lock(self):
        path=self.cfg.data_dir/"engine.lock"
        first=InstanceLock(path)
        try:
            with self.assertRaises(RuntimeError): InstanceLock(path)
        finally:
            first.close()

    def test_training_memory_limit_terminates_worker(self):
        engine=Engine(self.cfg,self.store)
        engine.trainer=Mock(pid=123)
        engine.trainer.is_alive.return_value=True
        with patch('app.engine.psutil.Process') as process:
            process.return_value.memory_info.return_value.rss=5*1024**3
            engine.training_tick()
        engine.trainer.terminate.assert_called_once()
        self.assertEqual(self.store.get('training')['status'],'resource limit reached')

    def test_training_timeout_terminates_worker(self):
        engine=Engine(self.cfg,self.store)
        engine.trainer=Mock(pid=123)
        engine.trainer.is_alive.return_value=True
        engine.training_started=0
        with patch('app.engine.psutil.Process') as process, patch('app.engine.time.monotonic',return_value=2000):
            process.return_value.memory_info.return_value.rss=100*1024**2
            engine.training_tick()
        engine.trainer.terminate.assert_called_once()


if __name__ == "__main__": unittest.main()
