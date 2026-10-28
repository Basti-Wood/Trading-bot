"""Self-tuning trading-loop (walk-forward), loop candle charts and forecast-versus-reality charts."""
from dataclasses import replace
import importlib
import json
import sys
from unittest.mock import Mock, patch
import unittest

import numpy as np
import pandas as pd

from app import loop
from app.feedback import schema
from app.web import create_app
from test_loop import PortfolioCase, REAL_STRESSTEST, add_version, make_loop_dir
from test_system import TempCase, bars

TUNED = '''"""tuned: buys every PAUSE bars, holds HALTEN bars."""
from backtesting import Strategy
import risiko

SYMBOL = "{symbol}"
PAUSE = 5
HALTEN = 3
PARAMETER = {{"HALTEN": [2, 3, 8], "PAUSE": [5, 10]}}


class Strategie(Strategy):
    def init(self):
        self.start = None

    def next(self):
        n = len(self.data)
        if not self.position and n % PAUSE == 0:
            self.buy(size=.95, sl=risiko.stop_preis(self.data.Close[-1]))
            self.start = n
        elif self.position and n - self.start >= HALTEN:
            self.position.close()
'''


def history(days=1300, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-06-01", periods=days, tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(.0006, .01, days)))
    return pd.DataFrame({"open": close * .999, "high": close * 1.006, "low": close * .994, "close": close,
                         "volume": 1e6}, index=idx)


def daily_csv(frame):
    return loop.daily_frame(frame)


@unittest.skipUnless(REAL_STRESSTEST.is_file(), "needs trading-loop/stresstest.py")
class TuningTests(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        self.cfg = replace(self.cfg, loop_dir=self.folder)
        sys.path.insert(0, str(self.folder))
        self.addCleanup(self.unload)
        self.stresstest = importlib.import_module("stresstest")

    def unload(self):
        sys.path.remove(str(self.folder))
        for name in ("stresstest", "risiko"):
            sys.modules.pop(name, None)

    def strategy(self, name="tuned_v1", symbol="AAA", text=TUNED):
        path = self.folder / "strategien" / f"{name}.py"
        path.write_text(text.format(symbol=symbol), encoding="utf-8")
        return path

    def test_walk_forward_picks_values_on_the_past_and_judges_unseen_years(self):
        module = self.stresstest.lade_strategie(self.strategy())
        entry, _, _ = self.stresstest.pruefe(module, daily_csv(history()))
        self.assertEqual(entry["methode"], "walk_forward")
        self.assertIn(entry["parameter"]["HALTEN"], [2, 3, 8])
        self.assertEqual(entry["standard"], {"HALTEN": 3, "PAUSE": 5})
        test = entry["test"]
        self.assertEqual(len(test["jahre"]), self.stresstest.TEST_JAHRE)
        # Each test year starts after the data its values were chosen on.
        self.assertGreater(test["jahre"][1]["von"], test["jahre"][0]["bis"])
        self.assertEqual((entry["rendite"], entry["trades"]), (test["rendite"], test["trades"]))
        self.assertEqual(entry["urteil"], self.stresstest.urteil_werte(test["rendite"], test["max_drawdown"], test["trades"],
                                                                       self.stresstest.MIN_TRADES_TEST))
        # The module is unchanged afterwards and the strategy file is never rewritten.
        self.assertEqual((module.HALTEN, module.PAUSE), (3, 5))
        json.dumps(entry, allow_nan=False)

    def test_without_parameter_the_five_year_verdict_stays(self):
        text = TUNED.replace('PARAMETER = {{"HALTEN": [2, 3, 8], "PAUSE": [5, 10]}}\n', "")
        module = self.stresstest.lade_strategie(self.strategy(text=text))
        entry, _, stats = self.stresstest.pruefe(module, daily_csv(history()))
        self.assertEqual((entry["methode"], entry["parameter"]), ("fuenf_jahre", {}))
        self.assertEqual(entry["urteil"], self.stresstest.urteil(stats))

    def test_invalid_parameter_lists_are_refused(self):
        for bad in ('{{"NOPE": [1]}}', '{{"HALTEN": []}}', '{{"HALTEN": [True]}}', '{{"SYMBOL": [1]}}'):
            module = self.stresstest.lade_strategie(self.strategy(text=TUNED.replace('{{"HALTEN": [2, 3, 8], "PAUSE": [5, 10]}}', bad)))
            with self.assertRaises(SystemExit, msg=bad):
                self.stresstest.parameter_raster(module)

    def test_large_grids_are_thinned_but_keep_the_defaults(self):
        module = self.stresstest.lade_strategie(self.strategy())
        module.PARAMETER = {"HALTEN": list(range(2, 12)), "PAUSE": list(range(3, 13))}
        standard, combos = self.stresstest.parameter_raster(module)
        self.assertLessEqual(len(combos), self.stresstest.MAX_KOMBINATIONEN + 1)
        self.assertIn(standard, combos)

    def test_bot_reuses_the_reports_tuned_values_and_retunes_when_stale(self):
        path = self.strategy()
        frame = history()
        last = daily_csv(frame).index[-1]
        entry = {"methode": "walk_forward", "parameter": {"HALTEN": 8, "PAUSE": 10}, "standard": {"HALTEN": 3, "PAUSE": 5},
                 "angepasst": True, "ergebnis": "gruen", "urteil": "GRÜN", "rendite": 4.2, "kaufen_halten": 9.,
                 "max_drawdown": -3.1, "trades": 12, "bis": f"{last:%Y-%m-%d}",
                 "test": {"von": "2024-01-01", "bis": f"{last:%Y-%m-%d}", "jahre": []}}
        report = {"strategie": "tuned_v1", "symbol": "AAA", **entry, "risiko": {"stop_loss": 5, "max_drawdown": 20},
                  "sha256": loop.file_hash(path)}
        (self.folder / "berichte" / "tuned_v1.json").write_text(json.dumps(report), encoding="utf-8")
        record = {"version": "tuned_v1", "symbol": "AAA", "sha256": loop.file_hash(path)}
        result = loop.evaluate(self.cfg, record, {"AAA": frame})["symbols"]["AAA"]
        self.assertEqual((result["test"]["quelle"], result["test"]["parameter"], result["test"]["gruen"]),
                         ("bericht", {"HALTEN": 8, "PAUSE": 10}, True))
        chart = result["chart"]
        self.assertEqual(len(chart["bars"]), 130)
        self.assertTrue(all(t["entry_at"] >= chart["bars"][0]["at"] or t["exit_at"] for t in chart["trades"]))
        self.assertTrue(chart["trades"])
        # An outdated report (or one for other risk values) is not trusted: the bot tunes the stock itself.
        report["bis"] = "2020-01-01"
        (self.folder / "berichte" / "tuned_v1.json").write_text(json.dumps(report), encoding="utf-8")
        result = loop.evaluate(self.cfg, record, {"AAA": frame})["symbols"]["AAA"]
        self.assertEqual((result["test"]["quelle"], result["test"]["methode"]), ("neu", "walk_forward"))


class LoopChartTests(PortfolioCase):
    def test_chart_endpoint_serves_the_deciding_version_and_state_stays_small(self):
        self.activate()
        results = self.loop.store.get("loop_results:trend_v2")
        chart = {"bars": [{"at": "2026-10-14", "open": 1, "high": 2, "low": .5, "close": 1.5}],
                 "indicators": [{"name": "EMA 20", "overlay": True, "values": [1.2]}], "trades": []}
        results["symbols"]["AAA"]["chart"] = chart
        self.loop.store.set("loop_results:trend_v2", results)
        client = create_app(self.cfg, self.store).test_client()
        data = client.get("/api/loop/chart/AAA").get_json()
        self.assertEqual((data["version"], data["deciding"], data["chart"]), ("trend_v2", True, chart))
        pending = client.get("/api/loop/chart/ZZZ")  # Not computed yet: a normal answer, not a 404 error.
        self.assertEqual((pending.status_code, pending.get_json()["pending"]), (200, True))
        self.assertEqual(client.get("/api/loop/chart/a%20b").status_code, 400)
        state = client.get("/api/state").get_json()
        rows = [r for v in state["loop"]["running"] for r in v["rows"]]
        self.assertTrue(rows and all("chart" not in (r["result"] or {}) for r in rows))

    def test_dashboard_retest_includes_edited_files_but_schedule_does_not(self):
        add_version(self.folder, "trend_v1")
        edited = add_version(self.folder, "edit_v1")
        edited.write_text(edited.read_text(encoding="utf-8") + "\n# PARAMETER ergänzt\n", encoding="utf-8")
        self.assertEqual(loop.retest_targets(self.cfg), ["trend_v1"])
        self.assertEqual(loop.retest_targets(self.cfg, include_changed=True), ["edit_v1", "trend_v1"])


class ForecastChartTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode="daily")
        self.frame = bars(120)
        self.now = self.frame.index[-1] + pd.Timedelta(days=1, hours=16)

    def capture(self, i, status, predictions, outcome=None):
        record = {"symbol": "SPY", "feed": self.cfg.feed, "feature_schema": schema(self.cfg),
                  "bar_at": self.frame.index[i].isoformat(), "captured_at": (self.frame.index[i] + pd.Timedelta(hours=22)).isoformat(),
                  "model_version": "m1", "features": {}, "predictions": predictions}
        self.store.capture_experience(record)
        if outcome:
            row = self.store.experience("SPY", self.cfg.feed, schema(self.cfg), status="pending")[0]
            self.store.resolve_experience(row["id"], status, outcome)

    def test_each_models_recorded_forecast_and_the_outcome_in_price_terms(self):
        entry = float(self.frame.open.iloc[101])
        self.capture(100, "resolved", {"ridge": .01, "extra_trees": -.02, "ensemble": .0},
                     {"target": .03, "entry": entry, "label_end": self.frame.index[102].isoformat(),
                      "entry_at": self.frame.index[101].isoformat()})
        self.capture(119, "pending", {"ridge": .02, "ensemble": .01})
        self.capture(5, "pending", {"ridge": .5})  # Outside the 90-session window: not drawn.
        self.store.reserve_order("cid", {"symbol": "SPY", "side": "buy", "filled_qty": "3", "filled_avg_price": "101.5",
                                         "filled_at": (self.frame.index[110] + pd.Timedelta(hours=15)).isoformat()}, strategy="daily")
        self.store.reserve_order("other", {"symbol": "QQQ", "side": "buy", "filled_qty": "3", "filled_avg_price": "5"}, strategy="daily")
        broker = Mock()
        broker.multi_bars.return_value = {"SPY": self.frame}
        client = create_app(self.cfg, self.store, broker_factory=lambda cfg: broker).test_client()
        with patch("app.web.pd.Timestamp.now", return_value=self.now):
            data = client.get("/api/predictions/SPY?strategy=daily").get_json()
            self.assertEqual(len(data["bars"]), 90)
            done, open_ = data["forecasts"]
            self.assertEqual(done["status"], "resolved")
            self.assertAlmostEqual(done["models"]["ridge"]["price"], entry * 1.01)
            self.assertAlmostEqual(done["models"]["extra_trees"]["price"], entry * .98)
            self.assertAlmostEqual(done["actual"], entry * 1.03)
            self.assertEqual(pd.Timestamp(done["target_at"]), self.frame.index[102])
            self.assertEqual(open_["reference"], float(self.frame.close.iloc[119]))
            self.assertIsNone(open_["target_at"])
            self.assertEqual([(f["side"], f["price"]) for f in data["fills"]], [("buy", 101.5)])
            json.dumps(data, allow_nan=False)
            # Cached for a while: no second broker request.
            client.get("/api/predictions/SPY?strategy=daily")
        self.assertEqual(broker.multi_bars.call_count, 1)
        self.assertEqual(client.get("/api/predictions/QQQ?strategy=daily").status_code, 404)
        self.assertEqual(client.get("/api/predictions/SPY?strategy=nope").status_code, 400)


if __name__ == "__main__":
    unittest.main()
