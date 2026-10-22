"""Intraday-Loop: trading-loop strategies on intraday candles, flat before every close."""
from dataclasses import replace
import importlib
import json
import sys
from unittest.mock import patch
import unittest

import numpy as np
import pandas as pd

from app import loop
from app.web import create_app
from test_loop import PortfolioCase, REAL_STRESSTEST, make_loop_dir
from test_system import TempCase

INTRADAY = '''"""intraday: buys at the first bar after N bars of a session, holds HALTEN bars."""
from backtesting import Strategy
import risiko

SYMBOL = "{symbol}"
ZEITRAHMEN = "{timeframe}"
HALTEN = 6
PARAMETER = {{"HALTEN": [3, 6, 200]}}


class Strategie(Strategy):
    def init(self):
        self.start = None

    def next(self):
        n = len(self.data)
        if not self.position and n % 7 == 0:
            self.buy(size=.95, sl=risiko.stop_preis(self.data.Close[-1]))
            self.start = n
        elif self.position and n - self.start >= HALTEN:
            self.position.close()
'''


def sessions(days=45, minutes=5, seed=4):
    rng = np.random.default_rng(seed)
    per_day = 390 // minutes
    index = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=30) + pd.Timedelta(minutes=minutes * k)
                              for d in pd.bdate_range("2026-08-03", periods=days) for k in range(per_day)])
    close = 50 * np.exp(np.cumsum(rng.normal(.00005, .002, len(index))))
    return pd.DataFrame({"Open": close, "High": close * 1.002, "Low": close * .998, "Close": close, "Volume": 1000.}, index=index)


def write(folder, name="intra_v1", symbol="AAA", timeframe="5Min"):
    path = folder / "strategien" / f"{name}.py"
    path.write_text(INTRADAY.format(symbol=symbol, timeframe=timeframe), encoding="utf-8")
    return path


def report(folder, path, symbol="AAA", stop=5, drawdown=20):
    data = {"strategie": path.stem, "symbol": symbol, "ergebnis": "gruen", "urteil": "GRÜN", "rendite": 1.,
            "risiko": {"stop_loss": stop, "max_drawdown": drawdown}, "sha256": loop.file_hash(path)}
    (folder / "berichte" / f"{path.stem}.json").write_text(json.dumps(data), encoding="utf-8")


@unittest.skipUnless(REAL_STRESSTEST.is_file(), "needs trading-loop/stresstest.py")
class IntradayStressTests(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        sys.path.insert(0, str(self.folder))
        self.addCleanup(self.unload)
        self.stresstest = importlib.import_module("stresstest")

    def unload(self):
        sys.path.remove(str(self.folder))
        for name in ("stresstest", "risiko"):
            sys.modules.pop(name, None)

    def test_positions_never_stay_open_overnight_and_the_last_sessions_decide(self):
        module = self.stresstest.lade_strategie(write(self.folder))
        prices = sessions()
        entry, _, stats = self.stresstest.pruefe(module, prices)
        trades = stats["_trades"]
        self.assertTrue(len(trades))
        # HALTEN=200 would hold across days; the session close still ends every trade the same day.
        self.assertTrue((trades.EntryTime.dt.date == trades.ExitTime.dt.date).all())
        self.assertLessEqual(trades.ExitTime.dt.time.max(), pd.Timestamp("15:55").time())
        self.assertEqual((entry["methode"], entry["zeitrahmen"]), ("walk_forward", "5Min"))
        days = prices.index.normalize().unique()
        self.assertEqual(entry["test"]["von"], f"{days[-self.stresstest.TEST_SITZUNGEN]:%Y-%m-%d}")
        self.assertEqual(entry["urteil"], self.stresstest.urteil_werte(entry["rendite"], entry["max_drawdown"], entry["trades"],
                                                                       self.stresstest.MIN_TRADES_INTRADAY))
        json.dumps(entry, allow_nan=False)

    def test_a_running_session_is_not_mistaken_for_its_close(self):
        index = pd.DatetimeIndex([pd.Timestamp("2026-10-20 15:40") + pd.Timedelta(minutes=5 * k) for k in range(4)]
                                 + [pd.Timestamp("2026-10-21 09:30") + pd.Timedelta(minutes=5 * k) for k in range(4)])
        self.assertEqual(list(self.stresstest.schluss_kerzen(index)), [False, True, True, True, False, False, False, False])
        # Half days end with their last bar.
        half = pd.DatetimeIndex([pd.Timestamp("2026-11-27 12:40") + pd.Timedelta(minutes=5 * k) for k in range(4)]
                                + [pd.Timestamp("2026-11-30 09:30")])
        self.assertEqual(list(self.stresstest.schluss_kerzen(half))[:4], [False, True, True, True])

    def test_too_little_intraday_history_is_refused(self):
        module = self.stresstest.lade_strategie(write(self.folder))
        with self.assertRaisesRegex(ValueError, "Handelstage"):
            self.stresstest.pruefe(module, sessions(days=30))


class IntradayRoutingTests(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        self.cfg = replace(self.cfg, loop_dir=self.folder)

    def test_timeframe_routes_versions_to_the_intraday_loop(self):
        for name, timeframe in (("intra_v1", "5Min"), ("hour_v1", "1Hour"), ("bad_v1", "2Min")):
            report(self.folder, write(self.folder, name, timeframe=timeframe))
        info = {v["version"]: v for v in loop.catalogue(self.cfg)}
        self.assertEqual((info["intra_v1"]["bot"], info["intra_v1"]["timeframe"], info["intra_v1"]["tradable"]), ("loop_day", "5Min", True))
        self.assertEqual(info["hour_v1"]["bot"], "loop_day")
        self.assertEqual((info["bad_v1"]["status"], info["bad_v1"]["tradable"]), ("nicht_handelbar", False))
        self.assertEqual(loop.retest_targets(self.cfg, bot="loop_day"), ["hour_v1", "intra_v1"])
        self.assertEqual(loop.retest_targets(self.cfg, bot="loop"), [])

    def test_intraday_frame_keeps_regular_session_in_new_york_time(self):
        index = pd.date_range("2026-10-21 13:00", "2026-10-21 20:30", freq="5min", tz="UTC")
        bars = pd.DataFrame({"open": 1., "high": 1., "low": 1., "close": 1., "volume": 1.}, index=index)
        frame = loop.intraday_frame(bars, 5)
        self.assertEqual((str(frame.index[0]), str(frame.index[-1])), ("2026-10-21 09:30:00", "2026-10-21 15:55:00"))


class IntradayEngineTests(PortfolioCase):
    def setUp(self):
        super().setUp()
        self.broker.quote.return_value = (99.95, 100.)
        self.engine = self.loop_day

    def activate_intraday(self, signal="BUY", bar_at="2026-10-15T13:35:00+00:00"):
        path = write(self.folder)
        report(self.folder, path)
        sha = loop.file_hash(path)
        self.engine.store.set("loop_strategies", {"intra_v1": {"version": "intra_v1", "symbol": "AAA", "sha256": sha, "timeframe": "5Min"}})
        self.engine.store.set("loop_results:intra_v1", {"version": "intra_v1", "sha256": sha, "symbols": {"AAA": {
            "signal": signal, "reason": "test", "bar_at": bar_at, "timeframe": "5Min", "last_close": 100., "target_session": None,
            "test": {"gruen": True, "urteil": "GRÜN", "rendite": 3., "max_drawdown": -2., "trades": 14}}}})
        self.store.set("allocation", {"capital_fraction": 1, "capital_cap": 0, "weights": {"daily": 0, "daytrade": 0, "loop": 0, "loop_day": 50}})

    def test_fresh_bar_signal_buys_with_risiko_stop_and_numbered_entry(self):
        self.activate_intraday()
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload["symbol"], payload["side"], payload["order_class"]), ("AAA", "buy", "oto"))
        self.assertEqual(payload["stop_loss"], {"stop_price": "95.00"})
        self.assertIn("-lday-20261015-AAA-b1", payload["client_order_id"])
        self.assertEqual(self.store.owner("AAA"), "loop_day")

    def test_signal_from_an_older_bar_does_not_buy(self):
        self.activate_intraday(bar_at="2026-10-15T13:20:00+00:00")
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("zuletzt abgeschlossenen Kerze", self.engine.store.get("forecast:AAA")["reason"])

    def test_positions_are_closed_before_the_market_close(self):
        self.activate_intraday(signal="HOLD")
        self.engine.store.set("owned:AAA", True)
        self.engine.store.set("loop_owner:AAA", "intra_v1")
        self.broker.positions.return_value = [{"symbol": "AAA", "qty": "4", "market_value": "400", "unrealized_plpc": ".01"}]
        self.broker.clock.return_value = {"timestamp": "2026-10-15T19:52:00Z", "next_close": "2026-10-15T20:00:00Z",
                                          "next_open": "2026-10-16T13:30:00Z", "is_open": True}
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]["side"], "sell")
        self.assertIn("Börsenschluss", self.engine.store.get("exit_reason:AAA"))

    def test_dashboard_activates_on_the_intraday_bot_and_retests_both(self):
        path = write(self.folder)
        report(self.folder, path)
        client = create_app(self.cfg, self.store).test_client()
        headers = {"X-Bot-Control": "dashboard"}
        response = client.post("/api/loop", json={"action": "activate", "version": "intra_v1"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(loop.active_versions(self.engine.store)["intra_v1"]["timeframe"], "5Min")
        self.assertEqual(loop.active_versions(self.loop.store), {})
        from test_loop import add_version
        add_version(self.folder, "trend_v1")
        self.assertEqual(client.post("/api/loop", json={"action": "retest"}, headers=headers).status_code, 200)
        self.assertTrue(self.engine.store.get("loop_retest_requested") and self.loop.store.get("loop_retest_requested"))
        state = client.get("/api/state").get_json()["loop"]
        self.assertEqual(set(state["bots"]), {"loop", "loop_day"})
        self.assertTrue(state["retest_requested"])

    @unittest.skipUnless(REAL_STRESSTEST.is_file(), "needs trading-loop/stresstest.py")
    def test_each_completed_bar_is_evaluated_once_with_one_request_per_timeframe(self):
        path = write(self.folder)
        self.engine.store.set("loop_strategies", {"intra_v1": {"version": "intra_v1", "symbol": "AAA",
                                                               "sha256": loop.file_hash(path), "timeframe": "5Min"}})
        frame = sessions(days=45).rename(columns=str.lower)
        frame.index = frame.index.tz_localize("America/New_York").tz_convert("UTC")
        now = frame.index[-1] + pd.Timedelta(minutes=5, seconds=30)
        self.engine.market_open = True
        with patch("app.engine.Alpaca") as alpaca, patch("app.engine.pd.Timestamp.now", return_value=now):
            alpaca.return_value.multi_bars.return_value = {"AAA": frame}
            self.engine.intraday_loop_tick()
            self.engine.forecast_thread.join(120)
            self.engine.intraday_loop_tick()  # Same bar: nothing new to do.
        self.assertEqual(alpaca.return_value.multi_bars.call_count, 1)
        self.assertEqual(alpaca.return_value.multi_bars.call_args.args[1], "5Min")
        item = self.engine.store.get("loop_results:intra_v1")["symbols"]["AAA"]
        self.assertEqual((item["timeframe"], pd.Timestamp(item["bar_at"])), ("5Min", frame.index[-1]))
        self.assertIn(item["signal"], {"BUY", "HOLD", "SELL"})
        self.assertEqual(item["test"]["zeitrahmen"], "5Min")
        self.assertIn("T15:55", item["chart"]["bars"][-1]["at"])

    def test_hourly_retune_only_while_open_and_only_its_own_versions(self):
        report(self.folder, write(self.folder))
        from test_loop import add_version
        add_version(self.folder, "trend_v1")
        self.engine.market_open = True
        self.engine.store.set("loop_retest_day", "2026-10-15")
        now = pd.Timestamp("2026-10-15T15:00:00Z").to_pydatetime()
        with patch("app.engine.loop.retest", return_value="GRÜN") as retest:
            self.engine.retest_tick(now=now)
            self.engine.retest_thread.join(5)
        self.assertEqual([c.args[1] for c in retest.call_args_list], ["intra_v1"])
        self.assertEqual(self.engine.store.get("loop_retest")["trigger"], "stündlich")
        self.engine.retest_thread = None
        with patch("app.engine.loop.retest") as again:
            self.engine.retest_tick(now=now)  # Within the hour: nothing.
        again.assert_not_called()


if __name__ == "__main__":
    unittest.main()
