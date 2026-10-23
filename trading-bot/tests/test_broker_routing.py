"""Routing between Alpaca (US listings) and Trading 212 (non-US listings), and guards against merge losses."""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from app import loop
from app.broker import Alpaca, BrokerError
from app.config import Config
from app.store import StrategyStore
from app.trading212 import mappings
from app.trading212_engine import context
from app.web import create_app
from test_loop import make_loop_dir, add_version
from test_trading212 import Fixture, TICKER, SPEC

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
HEADERS = {"X-Bot-Control": "dashboard"}


class MappingAndOrderRoutingTests(Fixture):
    def test_a_us_style_mapping_key_is_refused_so_alpaca_never_sees_it(self):
        self.mapping.write_text(json.dumps({"SIE": SPEC}))
        with self.assertRaisesRegex(BrokerError, "looks like a US symbol"):
            mappings(self.cfg)
        self.mapping.write_text(json.dumps({TICKER: SPEC}))
        self.assertEqual(list(mappings(self.cfg)), [TICKER])

    def test_trading212_tickers_never_enter_the_alpaca_books(self):
        folder = make_loop_dir(self.temp.name)
        cfg = replace(self.cfg, loop_dir=folder)
        state = StrategyStore(self.store, "loop")
        record = {"version": "x_v1", "symbol": TICKER, "sha256": "0"}
        self.assertEqual(loop.version_symbols(cfg, state, record), [])
        self.assertEqual(loop.version_symbols(cfg, state, {**record, "symbol": "SPY"}), ["SPY"])

    def test_alpaca_refuses_otc_at_the_order_boundary(self):
        broker = Alpaca(self.cfg)
        broker.asset = Mock(return_value={"class": "us_equity", "exchange": "OTC"})
        broker.request = Mock()
        with self.assertRaisesRegex(BrokerError, "verified US stock listing"):
            broker.submit({"symbol": "ABCDF", "side": "buy"})
        broker.request.assert_not_called()


class DashboardRoutingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        self.cfg = replace(self.cfg, loop_dir=self.folder)
        self.secondary = context(self.cfg)[1]
        sha = loop.file_hash(add_version(self.folder, "trend_v2", symbol="LISTE"))
        results = {"version": "trend_v2", "sha256": sha, "symbols": {TICKER: {
            "signal": "BUY", "reason": "test", "test": {"gruen": True, "urteil": "GRÜN", "rendite": 5., "max_drawdown": -3., "trades": 12},
            "chart": {"bars": [{"at": "2026-10-22", "open": 1, "high": 2, "low": .5, "close": 1.5}], "indicators": [], "trades": []}}}}
        self.secondary.set("loop_strategies", {"trend_v2": {"version": "trend_v2", "symbol": "LISTE", "sha256": sha}})
        self.secondary.set("loop_results:trend_v2", results)
        self.secondary.set("stocks", [{"broker": "trading212", "symbol": TICKER, "version": "trend_v2", "signal": "BUY",
                                       "action": "WAIT", "reason": "Trading 212 entries are paused", "test": results["symbols"][TICKER]["test"]}])
        self.client = create_app(self.cfg, self.store).test_client()

    def test_trading212_rows_are_tagged_and_their_chart_comes_from_the_trading212_journal(self):
        rows = self.client.get("/api/state").get_json()["loop"]["stocks"]
        row = next(r for r in rows if r["symbol"] == TICKER)
        self.assertEqual((row["broker"], row["bot"]), ("trading212", "trading212"))
        chart = self.client.get(f"/api/loop/chart/{TICKER}?bot=trading212").get_json()
        self.assertEqual((chart["bot"], chart["version"], chart["currency"]), ("trading212", "trend_v2", "EUR"))
        # The Alpaca book only accepts US symbols.
        self.assertEqual(self.client.get(f"/api/loop/chart/{TICKER}?bot=loop").status_code, 400)

    def test_retest_all_reaches_alpaca_and_trading212(self):
        response = self.client.post("/api/loop", json={"action": "retest"}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(StrategyStore(self.store, "loop").get("loop_retest_requested"))
        self.assertTrue(self.secondary.get("retest_requested"))

    def test_readonly_api_covers_both_brokers(self):
        cfg = replace(self.cfg, readonly_token="t" * 40)
        self.secondary.reserve_order("b", {"symbol": TICKER, "broker": "trading212", "side": "buy", "status": "filled"})
        self.store.reserve_order("a", {"symbol": "SPY", "side": "buy", "status": "filled"})
        client = create_app(cfg, self.store).test_client()
        bearer = {"Authorization": "Bearer " + "t" * 40}
        summary = client.get("/api/readonly/summary", headers=bearer).get_json()
        self.assertTrue(summary["trading212"]["enabled"])
        self.assertEqual(summary["trading212"]["decisions"][0]["symbol"], TICKER)
        self.assertIn("combined", summary["money"])
        stock = client.get(f"/api/readonly/stocks/{TICKER}", headers=bearer).get_json()
        self.assertEqual(stock["bots"]["trading212"]["loop"]["version"], "trend_v2")
        trades = client.get("/api/readonly/trades?days=1", headers=bearer).get_json()["orders"]
        self.assertEqual({o["broker"] for o in trades}, {"alpaca", "trading212"})
        body = json.dumps(summary)
        for secret in ("alpaca-secret", "t212-key", "t212-secret", "data-key"):
            self.assertNotIn(secret, body)


class MergeGuardTests(unittest.TestCase):
    def test_every_element_the_dashboard_script_uses_exists_in_the_page(self):
        script = (DASHBOARD / "app.js").read_text(encoding="utf-8")
        page = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        used = set(re.findall(r"\$\('([a-z0-9-]+)'\)", script))
        # Ids composed at runtime from bot names (alloc-<bot>, out-<bot>, <broker>-money …).
        used = {u for u in used if not u.endswith("-")}
        missing = sorted(u for u in used if f'id="{u}"' not in page)
        self.assertEqual(missing, [])
        for function in ("candleSvg", "renderForecast", "renderLoopChart", "renderReadonly", "renderMoney", "renderTrading212"):
            self.assertIn(f"function {function}(", script)

    def test_env_settings_reach_the_config(self):
        env = {"READONLY_API_TOKEN": "z" * 40, "READONLY_API_DAILY_LIMIT": "7", "TRADING_LOOP_TIMEOUT_SECONDS": "901",
               "TRADING212_ENABLED": "false", "TRADING_MODE": "both"}
        with patch.dict(os.environ, env):
            cfg = Config.from_env()
        self.assertEqual((cfg.readonly_token, cfg.readonly_daily_limit, cfg.loop_timeout), ("z" * 40, 7, 901))
        self.assertEqual(Config().loop_timeout, 900)

    def test_secrets_may_not_double_as_the_readonly_token(self):
        with self.assertRaises(ValueError):
            Config(readonly_token="k" * 40, trading212_key="k" * 40)


if __name__ == "__main__":
    unittest.main()
