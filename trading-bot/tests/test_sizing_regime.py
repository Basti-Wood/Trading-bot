"""Percentage sizing, market posture, losing-streak cooldown and the stress-test quality score."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

from app import regime, sizing, streak
from app.allocation import allocation, validate
from app.config import Config
from app.store import Store, StrategyStore
from app.web import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trading-loop"))
import qualitaet  # noqa: E402

SETTINGS = {"position_fraction": .10, "gross_fraction": .50, "risk_per_trade": .01, "heat": .06, "quality_scaling": True}


def candles(start, step, days=260):
    index = pd.date_range("2025-01-01", periods=days, freq="B", tz="UTC")
    close = start * np.cumprod(np.full(days, 1 + step))
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1e6}, index=index)


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=Path(self.temp.name), api_key="k", api_secret="s", symbols=("SPY",),
                          universe_mode="watchlist", trading_mode="both")
        self.store = Store(self.cfg.data_dir)


class SizingTests(Case):
    def size(self, **changes):
        args = dict(capital=100000, share=.5, stop=.05, gross_all=0, gross_bot=0, open_risk_bot=0,
                    cash=100000, settings=SETTINGS, test=None, ceiling=1.)
        args.update(changes)
        return sizing.budget(**args)

    def test_percent_of_the_bot_budget_per_stock(self):
        plan = self.size()
        self.assertEqual(plan["bot_budget"], 50000)
        self.assertAlmostEqual(plan["amount"], 5000)
        self.assertEqual(plan["binding"], "Einsatz pro Aktie")

    def test_a_wide_stop_shrinks_the_position_to_the_risk_budget(self):
        plan = self.size(stop=.20)  # 1 % of 50 000 = 500 risk; 500 / 20 % = 2 500
        self.assertAlmostEqual(plan["amount"], 2500)
        self.assertEqual(plan["binding"], "Risiko pro Trade")

    def test_portfolio_heat_limits_new_risk(self):
        plan = self.size(open_risk_bot=2900)  # 3 000 allowed in total, 100 left / 5 % = 2 000
        self.assertAlmostEqual(plan["amount"], 2000)
        self.assertEqual(plan["binding"], "Offenes Risiko (Portfolio-Hitze)")

    def test_market_posture_caps_the_invested_share(self):
        plan = self.size(gross_bot=10000, gross_all=10000, ceiling=.3)  # 50 000 × 50 % × 30 % = 7 500 − 10 000
        self.assertEqual(plan["amount"], 0)
        self.assertEqual(plan["binding"], "Höchstens investiert (dieser Bot)")

    def test_quality_and_half_kelly_from_the_test(self):
        strong = {"qualitaet": {"punkte": 80}, "statistik": {"anzahl": 60, "gewinnquote": 55, "avg_gewinn": 4, "avg_verlust": 2}}
        self.assertAlmostEqual(self.size(test=strong)["amount"], 6250)  # 10 % × 1.25
        weak = {"qualitaet": {"punkte": 10}, "statistik": {}}
        self.assertAlmostEqual(self.size(test=weak)["amount"], 3000)  # 10 % × 0.6
        modest = {"statistik": {"anzahl": 40, "gewinnquote": 60, "avg_gewinn": 1, "avg_verlust": 1.2}}
        plan = self.size(test=modest, stop=.20)  # half Kelly 5 % risk ÷ 20 % stop = 25 % → not binding
        self.assertNotEqual(plan["binding"], "Halbes Kelly aus dem Test")
        small = {"statistik": {"anzahl": 40, "gewinnquote": 51, "avg_gewinn": 1, "avg_verlust": 1}}
        plan = self.size(test=small)  # half Kelly 1 % risk ÷ 5 % stop = 20 % × 50 000 = 10 000 → still above 5 000
        self.assertAlmostEqual(plan["limits"]["Halbes Kelly aus dem Test"], 10000)
        losing = {"statistik": {"anzahl": 40, "gewinnquote": 30, "avg_gewinn": 2, "avg_verlust": 2}}
        plan = self.size(test=losing)
        self.assertEqual(plan["amount"], 0)
        self.assertEqual(plan["binding"], "Halbes Kelly aus dem Test")
        off = self.size(test=losing, settings={**SETTINGS, "quality_scaling": False})
        self.assertAlmostEqual(off["amount"], 5000)

    def test_dashboard_settings_are_validated_and_kept(self):
        body = {"capital_fraction": .8, "capital_cap": 0, "weights": {"daily": 50, "loop": 50},
                "sizing": {"position_fraction": 8, "gross_fraction": 60, "risk_per_trade": .5, "heat": 5, "quality_scaling": False}}
        cleaned = validate(body)
        self.assertAlmostEqual(cleaned["sizing"]["gross_fraction"], .6)
        self.store.set("allocation", cleaned)
        current = sizing.settings(self.cfg, StrategyStore(self.store, "loop"))
        self.assertAlmostEqual(current["position_fraction"], .08)
        self.assertFalse(current["quality_scaling"])
        self.assertIn("sizing", allocation(self.cfg, self.store))
        with self.assertRaisesRegex(ValueError, "Einsatz pro Aktie"):
            validate({**body, "sizing": {**body["sizing"], "position_fraction": 70}})
        with self.assertRaisesRegex(ValueError, "nicht größer"):
            validate({**body, "sizing": {**body["sizing"], "position_fraction": 15, "gross_fraction": 10}})

    def test_saving_without_sizing_keeps_the_saved_sizing(self):
        client = create_app(self.cfg, self.store).test_client()
        headers = {"X-Bot-Control": "dashboard"}
        body = {"capital_fraction": 1, "capital_cap": 0, "weights": {"daily": 50, "loop": 50}}
        sized = {**body, "sizing": {"position_fraction": 5, "gross_fraction": 40, "risk_per_trade": 1, "heat": 6}}
        self.assertEqual(client.post("/api/allocation", json=sized, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/allocation", json=body, headers=headers).status_code, 200)
        self.assertAlmostEqual(sizing.settings(self.cfg, self.store)["position_fraction"], .05)


class RegimeTests(Case):
    def test_uptrend_with_broad_participation_is_full_exposure(self):
        stocks = {f"S{i}": candles(50, .001) for i in range(6)}
        result = regime.posture(self.cfg, candles(400, .001), stocks, "SPY")
        self.assertEqual(result["state"], "angriff")
        self.assertEqual(result["ceiling"], 1.)

    def test_downtrend_with_narrow_participation_is_defensive(self):
        stocks = {f"S{i}": candles(50, -.002) for i in range(6)}
        result = regime.posture(self.cfg, candles(400, -.001), stocks, "SPY")
        self.assertEqual(result["state"], "defensive")
        self.assertEqual(result["ceiling"], self.cfg.regime_defensive)

    def test_mixed_signals_are_cautious_and_stale_postures_expire(self):
        stocks = {f"S{i}": candles(50, -.002) for i in range(6)}
        result = regime.posture(self.cfg, candles(400, .001), stocks, "SPY")
        self.assertEqual(result["state"], "vorsicht")
        regime.save(self.store, "alpaca", result)
        self.assertEqual(regime.ceiling(StrategyStore(self.store, "loop"), "alpaca"), self.cfg.regime_caution)
        self.store.set("market_regime:alpaca", {**result, "at": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(regime.ceiling(self.store, "alpaca"), 1.)

    def test_no_data_means_no_restriction(self):
        result = regime.posture(self.cfg, None, {}, "SPY")
        self.assertEqual(result["ceiling"], 1.)


class RegimeRetryTests(Case):
    def test_a_failing_data_request_is_not_retried_every_cycle_by_any_bot(self):
        from unittest.mock import patch
        from app.broker import BrokerError
        from app.engine import Engine
        engines = [Engine(replace(self.cfg, trading_mode=mode, coordinated=True), StrategyStore(self.store, mode))
                   for mode in ("daily", "loop")]
        with patch("app.engine.Alpaca.bars", side_effect=BrokerError("HTTP 429")) as bars:
            for _ in range(3):
                for engine in engines:
                    engine.regime_tick(["AAA"])
                    if engine.regime_thread:
                        engine.regime_thread.join(5)
        self.assertEqual(bars.call_count, 1)
        self.assertIsNone(self.store.get("market_regime:alpaca"))


class StreakTests(Case):
    def close(self, book, symbol, pl):
        streak.remember(book, {"symbol": symbol, "unrealized_pl": pl, "market_value": 1000 + pl, "unrealized_plpc": pl / 1000})
        return streak.closed(book, symbol)

    def test_three_losses_in_a_row_pause_only_that_bot(self):
        cfg = replace(self.cfg, cooldown_losses=3, cooldown_hours=24)
        loop, daily = StrategyStore(self.store, "loop"), StrategyStore(self.store, "daily")
        for symbol in ("A", "B"):
            self.close(loop, symbol, -10)
        self.assertFalse(streak.status(cfg, loop)["active"])
        self.close(loop, "C", -5)
        state = streak.status(cfg, loop)
        self.assertTrue(state["active"])
        self.assertIn("3 Verlust-Trades", state["reason"])
        self.assertFalse(streak.status(cfg, daily)["active"])
        self.assertTrue(streak.status(cfg, loop, pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=25))["active"] is False)

    def test_a_win_ends_the_streak_and_results_are_recorded_once(self):
        cfg = replace(self.cfg, cooldown_losses=2)
        book = StrategyStore(self.store, "loop")
        self.close(book, "A", -10)
        self.close(book, "B", 20)
        self.close(book, "C", -10)
        self.assertFalse(streak.status(cfg, book)["active"])
        self.assertIsNone(streak.closed(book, "C"))
        self.assertEqual(len(book.history("trade_result")), 3)

    def test_zero_disables_the_cooldown(self):
        cfg = replace(self.cfg, cooldown_losses=0)
        book = StrategyStore(self.store, "loop")
        for symbol in "ABCDE":
            self.close(book, symbol, -1)
        self.assertFalse(streak.status(cfg, book)["active"])


class QualityTests(unittest.TestCase):
    def test_trade_statistics(self):
        trades = pd.DataFrame({"ReturnPct": [.04, .02, -.01, -.03]})
        stats = qualitaet.trade_kennzahlen(trades)
        self.assertEqual(stats["gewinnquote"], 50)
        self.assertEqual(stats["avg_gewinn"], 3)
        self.assertEqual(stats["avg_verlust"], 2)
        self.assertEqual(stats["profitfaktor"], 1.5)
        self.assertEqual(stats["erwartung"], .5)

    def test_score_rewards_samples_edge_and_surviving_double_costs(self):
        stats = {"anzahl": 120, "erwartung": 1.0, "profitfaktor": 2.0}
        strong = qualitaet.bewerten(stats, -12, 5, 2, 15.)
        self.assertGreaterEqual(strong["punkte"], 70)
        self.assertEqual(strong["stufe"], "stark")
        fragile = qualitaet.bewerten({**stats, "anzahl": 12}, -12, 5, 2, -1.)
        self.assertLess(fragile["punkte"], strong["punkte"] - 30)
        self.assertEqual(fragile["bereiche"]["ausfuehrung"], 0)


if __name__ == "__main__":
    unittest.main()
