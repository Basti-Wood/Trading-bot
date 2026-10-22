"""Trading-loop bot, dashboard budget/weights and stock selection."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import Mock, patch
import unittest

from app import loop
from app.allocation import bot_capital, bot_share, validate
from app.broker import BrokerError
from app.config import Config
from app.portfolio import PortfolioEngine
from app.web import create_app
from test_system import TempCase, bars

REAL_STRESSTEST = Path(__file__).resolve().parents[1] / "trading-loop" / "stresstest.py"
RISIKO = '''STOP_LOSS_PROZENT = {stop}
MAX_DRAWDOWN_PROZENT = {drawdown}


def pruefe():
    if STOP_LOSS_PROZENT is None or MAX_DRAWDOWN_PROZENT is None:
        raise SystemExit("STOP_LOSS_PROZENT fehlt.")


def stop_preis(einstieg, short=False):
    pruefe()
    faktor = STOP_LOSS_PROZENT / 100
    return round(einstieg * (1 + faktor) if short else einstieg * (1 - faktor), 2)
'''
STRATEGY = '''"""{name}: test strategy"""
from backtesting import Strategy
import risiko

SYMBOL = "{symbol}"


class Strategie(Strategy):
    def init(self):
        pass

    def next(self):
        n = len(self.data)
        # EINSTIEG
        if n == {buy_at} and not self.position:
            self.buy(size=.95, sl=risiko.stop_preis(self.data.Close[-1]))
        # AUSSTIEG
        if n == {sell_at} and self.position:
            self.position.close()
'''


def make_loop_dir(root, stop=5, drawdown=20):
    folder = Path(root) / "trading-loop"
    (folder / "strategien").mkdir(parents=True)
    (folder / "berichte").mkdir()
    (folder / "risiko.py").write_text(RISIKO.format(stop=stop, drawdown=drawdown), encoding="utf-8")
    if REAL_STRESSTEST.is_file():
        shutil.copy(REAL_STRESSTEST, folder / "stresstest.py")
    else:
        (folder / "stresstest.py").write_text("STARTKAPITAL = 100_000\nKOSTEN = 0.001\n", encoding="utf-8")
    return folder


def add_version(folder, name, symbol="AAA", buy_at=430, sell_at=0, result="gruen", stop=5, drawdown=20):
    path = folder / "strategien" / f"{name}.py"
    path.write_text(STRATEGY.format(name=name, symbol=symbol, buy_at=buy_at, sell_at=sell_at), encoding="utf-8")
    if result:
        report = {"strategie": name, "symbol": symbol, "rendite": 12.5, "kaufen_halten": 30.1, "max_drawdown": -9.8,
                  "trades": 14, "ergebnis": result, "urteil": "GRÜN" if result == "gruen" else "ROT (Rendite 0 % oder weniger)",
                  "risiko": {"stop_loss": stop, "max_drawdown": drawdown},
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (folder / "berichte" / f"{name}.json").write_text(json.dumps(report), encoding="utf-8")
    return path


class AllocationTests(TempCase):
    def test_validation_rejects_unsafe_values(self):
        good = {"capital_fraction": .5, "capital_cap": 0, "weights": {"daily": 50, "daytrade": 25, "loop": 25}}
        self.assertEqual(validate(good)["weights"]["loop"], 25)
        for bad in ({**good, "capital_fraction": 0}, {**good, "capital_fraction": 1.5}, {**good, "capital_cap": 50},
                    {**good, "weights": {"daily": 0, "daytrade": 0, "loop": 0}}, {**good, "weights": {"daily": -1, "daytrade": 1, "loop": 1}},
                    {**good, "capital_fraction": float("nan")}, {"weights": {}}):
            with self.assertRaises(ValueError):
                validate(bad)

    def test_weights_split_the_budget_and_default_keeps_previous_split(self):
        cfg = replace(self.cfg, trading_mode="daily", coordinated=True)
        self.assertAlmostEqual(bot_share(cfg, self.store, "daily"), .5)
        self.assertEqual(bot_share(cfg, self.store, "loop"), 0)
        self.store.set("allocation", {"capital_fraction": .5, "capital_cap": 3000, "weights": {"daily": 60, "daytrade": 20, "loop": 20}})
        self.assertEqual(bot_capital(cfg, self.store, 10000), 3000)
        self.assertAlmostEqual(bot_share(cfg, self.store, "loop"), .2)


class LoopFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = make_loop_dir(self.temp.name)
        self.cfg = Config(data_dir=Path(self.temp.name) / "data", api_key="k", api_secret="s",
                          symbols=("SPY",), universe_mode="watchlist", loop_dir=self.folder)

    def test_risk_rules_are_parsed_without_execution(self):
        self.assertEqual(loop.risk_rules(self.cfg)["stop_loss"], 5)
        (self.folder / "risiko.py").write_text("STOP_LOSS_PROZENT = None\nMAX_DRAWDOWN_PROZENT = 20\nimport os; os.remove('x')", encoding="utf-8")
        rules = loop.risk_rules(self.cfg)
        self.assertFalse(rules["ok"])
        self.assertIn("STOP_LOSS_PROZENT", rules["message"])
        (self.folder / "risiko.py").write_text("STOP_LOSS_PROZENT = True\nMAX_DRAWDOWN_PROZENT = 20\n", encoding="utf-8")
        self.assertFalse(loop.risk_rules(self.cfg)["ok"])

    def test_tested_unchanged_stock_versions_with_current_rules_are_activatable(self):
        add_version(self.folder, "trend_v1", result="rot")
        green = add_version(self.folder, "trend_v2")
        add_version(self.folder, "trend_v3", result=None)
        add_version(self.folder, "index_v1", symbol="^GSPC")
        add_version(self.folder, "alt_v1", stop=3)
        changed = add_version(self.folder, "edit_v1")
        changed.write_text(changed.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        catalogue = loop.catalogue(self.cfg)
        status = {v["version"]: (v["status"], v["tradable"]) for v in catalogue}
        self.assertEqual(status, {"trend_v1": ("bereit", True), "trend_v2": ("bereit", True), "trend_v3": ("ungetestet", False),
                                  "index_v1": ("nicht_handelbar", False),
                                  "alt_v1": ("veraltet", False), "edit_v1": ("geaendert", False)})
        self.assertEqual({v["version"]: v["bot"] for v in catalogue if v["tradable"]},
                         {"trend_v1": "loop", "trend_v2": "loop"})
        self.assertEqual(loop.strategy_path(self.cfg, "trend_v2"), green)
        for name in ("../risiko", "trend_v2.py", "TREND_V2", "..\\x_v1"):
            self.assertIsNone(loop.strategy_path(self.cfg, name))

    def test_signals_match_backtest_orders_at_the_last_bar(self):
        history = bars(430)
        history.attrs["target_session"] = "2026-10-16"
        cases = {"buy_v1": (430, 0, "BUY"), "hold_v1": (100, 0, "HOLD"), "exit_v1": (100, 430, "SELL"), "flat_v1": (100, 200, "SELL")}
        for name, (buy_at, sell_at, expected) in cases.items():
            path = add_version(self.folder, name, buy_at=buy_at, sell_at=sell_at)
            record = {"version": name, "symbol": "AAA", "sha256": loop.file_hash(path)}
            result = loop.evaluate(self.cfg, record, {"AAA": history})["symbols"]["AAA"]
            self.assertEqual(result["signal"], expected, name)
            self.assertEqual(result["target_session"], "2026-10-16")
            self.assertIn("gruen", result["test"])

    def test_list_version_tests_every_symbol_with_the_stress_test_verdict(self):
        path = add_version(self.folder, "liste_v1", symbol="LISTE", buy_at=100, sell_at=0)
        record = {"version": "liste_v1", "symbol": "LISTE", "sha256": loop.file_hash(path)}
        falling = bars(430)
        falling[["open", "high", "low", "close"]] = falling[["open", "high", "low", "close"]].values[::-1]
        result = loop.evaluate(self.cfg, record, {"UP": bars(430), "DOWN": falling})["symbols"]
        # One open trade only: fewer than 10 trades is red in the stress test, whatever the return.
        self.assertEqual({s: r["test"]["gruen"] for s, r in result.items()}, {"UP": False, "DOWN": False})
        self.assertIn("weniger als 10 Trades", result["UP"]["test"]["urteil"])
        self.assertGreater(result["UP"]["test"]["rendite"], 0)
        self.assertLess(result["DOWN"]["test"]["rendite"], 0)
        self.assertEqual(result["UP"]["signal"], "HOLD")

    def test_changed_file_or_symbol_is_refused(self):
        path = add_version(self.folder, "buy_v1")
        record = {"version": "buy_v1", "symbol": "AAA", "sha256": loop.file_hash(path)}
        path.write_text(path.read_text(encoding="utf-8").replace('"AAA"', '"BBB"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "geändert"):
            loop.evaluate(self.cfg, record, {"AAA": bars(430)})
        with self.assertRaisesRegex(ValueError, "SYMBOL"):
            loop.evaluate(self.cfg, {**record, "sha256": loop.file_hash(path)}, {"AAA": bars(430)})

    @unittest.skipUnless(REAL_STRESSTEST.is_file(), "needs trading-loop/stresstest.py")
    def test_list_summary_has_no_group_verdict(self):
        code = "import stresstest as s; print(s.listen_urteil(2, 21)); print(s.listen_urteil(3, 21)); print(s.listen_urteil(2, 2))"
        out = subprocess.run([sys.executable, "-c", code], cwd=self.folder, capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(out.stdout.splitlines(), ["EINZELTESTS (2 von 21 Aktien grün)",
                                                   "EINZELTESTS (3 von 21 Aktien grün)", "EINZELTESTS (2 von 2 Aktien grün)"], out.stderr)

    @unittest.skipUnless(REAL_STRESSTEST.is_file(), "needs trading-loop/stresstest.py")
    def test_retest_reruns_the_stress_test_and_rewrites_the_report(self):
        path = add_version(self.folder, "trend_v1", buy_at=100, result="gruen")
        add_version(self.folder, "trend_v2", result=None)
        changed = add_version(self.folder, "edit_v1")
        changed.write_text(changed.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        # Scheduled runs skip untested and edited files; a named version is always allowed.
        self.assertEqual(loop.retest_targets(self.cfg), ["trend_v1"])
        self.assertEqual(loop.retest_targets(self.cfg, "trend_v2"), ["trend_v2"])
        self.assertEqual(loop.retest_targets(self.cfg, "../risiko"), [])
        # Prices cached today: stresstest.py does not download.
        (self.folder / "daten").mkdir()
        loop.daily_frame(bars(430)).to_csv(self.folder / "daten" / "AAA_1Day.csv")
        self.assertEqual(loop.retest(self.cfg, "trend_v1"), "ROT (weniger als 10 Trades)")
        report = json.loads((self.folder / "berichte" / "trend_v1.json").read_text(encoding="utf-8"))
        self.assertEqual((report["ergebnis"], report["sha256"], report["trades"]), ("rot", loop.file_hash(path), 1))
        self.assertEqual(path.read_text(encoding="utf-8"), STRATEGY.format(name="trend_v1", symbol="AAA", buy_at=100, sell_at=0))
        self.assertTrue(loop.retest(self.cfg, "nope_v1").startswith("Fehler"))

    def test_error_database_is_read_only_and_ordered(self):
        db = sqlite3.connect(self.folder / "fehler.db")
        db.execute("CREATE TABLE fehler (id INTEGER PRIMARY KEY, datum TEXT, strategie TEXT, version INTEGER, problem TEXT,"
                   " verlust_trades INTEGER, aenderung TEXT, ergebnis TEXT)")
        db.executemany("INSERT INTO fehler (datum,strategie,version,problem,verlust_trades,aenderung,ergebnis) VALUES (?,?,?,?,?,?,?)",
                       [("2026-10-16T10:00", "trend", 1, "Fehlausbrüche", 9, "Volumenfilter", "rot"),
                        ("2026-10-16T11:00", "trend", 2, "Zu später Ausstieg", 4, "Trailing", "gruen")])
        db.commit()
        db.close()
        self.assertEqual([e["version"] for e in loop.errors(self.cfg)], [2, 1])


class PortfolioCase(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = make_loop_dir(self.temp.name)
        self.cfg = replace(self.cfg, trading_mode="both", symbols=("SPY",), loop_dir=self.folder)
        self.store.set("allocation", {"capital_fraction": 1, "capital_cap": 0, "weights": {"daily": 50, "daytrade": 0, "loop": 50}})
        self.portfolio = PortfolioEngine(self.cfg, self.store)
        self.daily, self.daytrade, self.loop = self.portfolio.engines
        self.broker = Mock(base="paper")
        self.broker.account.return_value = {"status": "ACTIVE", "equity": "10000", "last_equity": "10000",
                                            "cash": "10000", "buying_power": "10000"}
        self.broker.clock.return_value = {"timestamp": "2026-10-15T13:45:00Z", "next_close": "2026-10-15T20:00:00Z",
                                          "next_open": "2026-10-16T13:30:00Z", "is_open": True}
        self.broker.positions.return_value = []
        self.broker.open_orders.return_value = []
        self.broker.latest_price.return_value = 100
        self.broker.asset.return_value = {"tradable": True, "status": "active", "fractionable": True}
        self.broker.submit.return_value = {"id": "accepted", "status": "new", "filled_qty": "0"}
        self.broker.order.return_value = {"id": "accepted", "status": "filled", "filled_qty": "4"}
        self.portfolio.broker = self.broker
        for engine in self.portfolio.engines:
            engine.broker = engine.executor.broker = self.broker
            engine.scan_tick = engine.forecast_tick = engine.research_tick = engine.playbook_tick = Mock()
            engine.store.set("entries_enabled", True)

    def activate(self, symbol="AAA", signal="BUY", session="2026-10-15", version="trend_v2", green=True, results=None, keep=False):
        path = add_version(self.folder, version, symbol=symbol)
        sha = loop.file_hash(path)
        active = loop.active_versions(self.loop.store) if keep else {}
        self.loop.store.set("loop_strategies", {**active, version: {"version": version, "symbol": symbol, "sha256": sha}})
        item = lambda s: {"signal": signal, "reason": "test", "bar_at": "2026-10-14T04:00:00Z", "target_session": session,
                          "last_close": 100., "to": "2026-10-14",
                          "test": {"gruen": green, "urteil": "GRÜN" if green else "ROT (Drawdown über 20 %)",
                                   "rendite": 12., "max_drawdown": -8., "trades": 15}}
        symbols = results or [symbol]
        self.loop.store.set(f"loop_results:{version}", {"version": version, "sha256": sha, "symbols": {s: item(s) for s in symbols}})


class LoopEngineTests(PortfolioCase):
    def test_green_buy_signal_enters_with_risiko_stop_and_no_take_profit(self):
        self.activate()
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload["symbol"], payload["side"], payload["order_class"]), ("AAA", "buy", "oto"))
        self.assertEqual(payload["stop_loss"], {"stop_price": "95.00"})
        self.assertNotIn("take_profit", payload)
        self.assertIn("-loop-", payload["client_order_id"])
        self.assertEqual(payload["qty"], "4")  # 50% weight of $10,000, 10% per position.
        self.assertEqual(self.store.owner("AAA"), "loop")

    def test_stale_buy_signal_or_zero_weight_does_not_trade(self):
        self.activate(session="2026-10-14")
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("aktuelle Handelssitzung", self.loop.store.get("forecast:AAA")["reason"])
        self.activate()
        self.store.set("allocation", {"capital_fraction": 1, "capital_cap": 0, "weights": {"daily": 50, "daytrade": 0, "loop": 0}})
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("weight is 0", self.loop.store.get("forecast:AAA")["reason"])

    def test_incomplete_risiko_blocks_new_entries(self):
        self.activate()
        (self.folder / "risiko.py").write_text("STOP_LOSS_PROZENT = None\nMAX_DRAWDOWN_PROZENT = 20\n", encoding="utf-8")
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("risiko.py", self.loop.store.get("forecast:AAA")["reason"])

    def test_red_symbol_test_blocks_the_buy(self):
        self.activate(green=False)
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertIn("Kein grüner Einzeltest für AAA", self.loop.store.get("forecast:AAA")["reason"])

    def test_list_version_trades_the_bots_list_but_never_the_users_holdings(self):
        self.cfg = replace(self.cfg, symbols=("AAA", "BBB"))
        portfolio = PortfolioEngine(self.cfg, self.store)
        for engine in portfolio.engines:
            engine.broker = engine.executor.broker = self.broker
            engine.scan_tick = engine.forecast_tick = engine.research_tick = engine.playbook_tick = Mock()
            engine.store.set("entries_enabled", True)
        portfolio.broker = self.broker
        loop_engine = portfolio.engines[2]
        self.broker.positions.return_value = [{"symbol": "MINE", "qty": "5", "market_value": "500", "unrealized_plpc": "0"}]
        self.store.set("account", {"positions": self.broker.positions.return_value})
        self.loop = loop_engine
        self.activate(symbol="LISTE", results=["MINE"])
        self.assertEqual(loop.version_symbols(loop_engine.config, loop_engine.store, loop.active_versions(loop_engine.store)["trend_v2"]),
                         ["AAA", "BBB", "MINE"])
        portfolio.cycle()
        self.broker.submit.assert_not_called()
        self.assertEqual(loop_engine.store.get("forecast:MINE")["action"], "WAIT")
        self.activate(symbol="LISTE", results=["BBB", "MINE"])
        portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]["symbol"], "BBB")
        self.assertEqual(loop_engine.store.get("loop_owner:BBB"), "trend_v2")

    def test_buying_version_decides_the_exit_and_a_newer_version_takes_over(self):
        self.activate(signal="SELL", version="trend_v2")
        self.activate(signal="BUY", version="other_v1", keep=True)
        self.owned_position()
        self.loop.store.set("loop_owner:AAA", "other_v1")
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()  # other_v1 still holds; trend_v2's SELL is not its business.
        self.activate(signal="SELL", version="other_v2", keep=True)
        strategies = loop.active_versions(self.loop.store)
        del strategies["other_v1"]
        self.loop.store.set("loop_strategies", strategies)
        self.assertEqual(loop.owner_version(self.loop.store, "AAA"), "other_v2")
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]["side"], "sell")

    def owned_position(self, pnl=".01"):
        self.loop.store.set("owned:AAA", True)
        if not self.loop.store.get("loop_owner:AAA"):
            self.loop.store.set("loop_owner:AAA", "trend_v2")
        self.broker.positions.return_value = [{"symbol": "AAA", "qty": "4", "market_value": "400", "unrealized_plpc": pnl}]

    def test_risiko_stop_loss_and_strategy_exit_sell(self):
        self.activate(signal="HOLD")
        self.owned_position("-.051")
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]["side"], "sell")
        self.assertIn("risiko.py", self.loop.store.get("exit_reason:AAA"))

    def test_deactivated_strategy_closes_its_position(self):
        self.activate(signal="HOLD")
        self.owned_position()
        self.loop.store.set("loop_strategies", {})
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]["side"], "sell")
        self.assertIn("abgeschaltet", self.loop.store.get("exit_reason:AAA"))

    def test_max_drawdown_halts_loop_and_closes_positions(self):
        self.activate(signal="HOLD")
        self.owned_position()
        self.loop.store.set("equity_peak", 13000)  # 10,000 is 23% below the peak; limit 20%.
        self.portfolio.cycle()
        self.assertTrue(self.loop.store.get("halt"))
        self.assertFalse(self.loop.store.get("entries_enabled"))
        self.assertTrue(self.daily.store.get("entries_enabled"))
        self.assertEqual(self.broker.submit.call_args.args[0]["side"], "sell")
        client = create_app(self.cfg, self.store).test_client()
        headers = {"X-Bot-Control": "dashboard"}
        response = client.post("/api/control", json={"action": "resume", "strategy": "loop"}, headers=headers)
        self.assertEqual(response.status_code, 409)
        client.post("/api/control", json={"action": "resume"}, headers=headers)
        self.assertFalse(self.loop.store.get("entries_enabled"))
        self.assertEqual(client.post("/api/loop", json={"action": "reset_peak"}, headers=headers).status_code, 200)
        self.assertIsNone(self.loop.store.get("halt"))

    def test_stress_tests_rerun_once_a_day_before_the_open_and_on_request(self):
        self.assertEqual(self.loop.config.history_days, loop.HISTORY_DAYS)
        self.assertNotEqual(self.daily.config.history_days, loop.HISTORY_DAYS)
        add_version(self.folder, "trend_v1", result="rot")
        calls = []
        client = create_app(self.cfg, self.store).test_client()
        post = lambda **body: client.post("/api/loop", json={"action": "retest", **body}, headers={"X-Bot-Control": "dashboard"})
        with patch.object(loop, "retest", side_effect=lambda cfg, version: calls.append(version) or "GRÜN"):
            self.loop.retest_tick(datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc))  # 07:00 New York
            self.assertIsNone(self.loop.retest_thread)
            morning = datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc)
            self.loop.retest_tick(morning)
            self.loop.retest_thread.join(5)
            self.assertEqual(calls, ["trend_v1"])
            self.assertEqual(self.loop.store.get("loop_retest")["status"], "fertig")
            self.loop.retest_tick(morning)
            self.loop.retest_thread.join(5)
            self.assertEqual(calls, ["trend_v1"])  # Once per day.
            self.assertEqual(post(version="nope_v1").status_code, 404)
            self.assertEqual(post().status_code, 200)
            self.assertEqual(post().status_code, 409)
            self.loop.retest_tick(morning)
            self.loop.retest_thread.join(5)
            self.assertEqual(calls, ["trend_v1", "trend_v1"])
        state = client.get("/api/state").get_json()["loop"]
        self.assertEqual((state["retest"]["trigger"], state["retest"]["done"]), ("manuell", {"trend_v1": "GRÜN"}))

    def test_loop_never_trains_or_requests_news(self):
        self.loop.store.set("loop_strategies", {"AAA": {"version": "x_v1", "symbol": "AAA", "sha256": "0"}})
        self.loop.training_tick()
        self.assertIsNone(self.loop.trainer)


class DashboardApiTests(PortfolioCase):
    def setUp(self):
        super().setUp()
        self.client = create_app(self.cfg, self.store).test_client()
        self.headers = {"X-Bot-Control": "dashboard"}

    def test_allocation_endpoint_validates_and_changes_live_sizing(self):
        body = {"capital_fraction": .4, "capital_cap": 0, "weights": {"daily": 30, "daytrade": 30, "loop": 40}}
        self.assertEqual(self.client.post("/api/allocation", json={**body, "capital_fraction": 2}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/api/allocation", json=body, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/allocation", json=body).status_code, 403)
        self.store.set("account", {"equity": 10000, "positions": []})
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["allocation"]["budget"], 4000)
        loop_card = next(s for s in state["strategies"] if s["id"] == "loop")
        self.assertAlmostEqual(loop_card["capital_ceiling"], 1600)

    def test_loop_activation_allows_red_report_and_keeps_versions_of_same_idea(self):
        add_version(self.folder, "trend_v1", result="rot")
        add_version(self.folder, "trend_v2")
        post = lambda action, version: self.client.post("/api/loop", json={"action": action, "version": version}, headers=self.headers)
        self.assertEqual(post("activate", "trend_v1").status_code, 200)
        self.assertEqual(post("activate", "../../etc_v1").status_code, 404)
        self.assertEqual(post("activate", "trend_v2").status_code, 200)
        self.assertEqual(self.loop.store.get("loop_strategies")["trend_v2"]["symbol"], "AAA")
        state = self.client.get("/api/state").get_json()["loop"]
        self.assertEqual([v["active"] for v in state["versions"]], [True, True])
        self.assertEqual(state["running"][0]["rows"][0]["symbol"], "AAA")
        add_version(self.folder, "trend_v3")
        self.assertEqual(post("activate", "trend_v3").status_code, 200)
        self.assertEqual(list(self.loop.store.get("loop_strategies")), ["trend_v1", "trend_v2", "trend_v3"])
        post("deactivate", "trend_v3")
        add_version(self.folder, "liste_v1", symbol="LISTE")
        self.assertIn("Aktienliste", post("activate", "liste_v1").get_json()["message"])
        post("deactivate", "liste_v1")
        self.activate()
        self.assertEqual(post("deactivate", "trend_v2").status_code, 200)
        self.assertEqual(self.loop.store.get("loop_strategies"), {})

class PerStockSelectionTests(PortfolioCase):
    def score(self, version, symbol, *, profit=None, drawdown=None, green=None, signal=None):
        saved = self.loop.store.get(f'loop_results:{version}')
        item = saved['symbols'][symbol]
        for key, value in [('rendite', profit), ('max_drawdown', drawdown), ('gruen', green)]:
            if value is not None:
                item['test'][key] = value
        if signal is not None:
            item['signal'] = signal
        self.loop.store.set(f'loop_results:{version}', saved)

    def test_green_stock_can_buy_despite_old_red_list_report(self):
        self.activate(symbol='LISTE', results=['PAG', 'CCC'], version='list_v1')
        report_file = self.folder / 'berichte/list_v1.json'
        report = json.loads(report_file.read_text())
        report.update(ergebnis='rot', urteil='old group gate: only 1/20 green')
        report_file.write_text(json.dumps(report))
        self.cfg = replace(self.cfg, symbols=('PAG', 'CCC'))
        self.loop.config = replace(self.loop.config, symbols=('PAG', 'CCC'))
        self.score('list_v1', 'CCC', green=False)
        self.assertTrue(loop.version_info(self.cfg, self.folder / 'strategien/list_v1.py')['tradable'])
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_args.args[0]['symbol'], 'PAG')
        self.assertEqual(self.loop.store.get('loop_owner:PAG'), 'list_v1')

    def test_rank_is_per_stock_then_return_then_absolute_drawdown(self):
        self.activate(symbol='LISTE', results=['AAA', 'BBB'], version='trend_v1')
        self.activate(symbol='LISTE', results=['AAA', 'BBB'], version='trend_v2', keep=True)
        self.activate(symbol='LISTE', results=['AAA', 'BBB'], version='other_v1', keep=True)
        self.score('trend_v1', 'AAA', profit=20, drawdown=-12)
        self.score('trend_v2', 'AAA', profit=20, drawdown=-5)
        self.score('other_v1', 'AAA', profit=100, green=False)
        self.score('other_v1', 'BBB', profit=30)
        self.assertEqual(loop.combine(self.loop.store, 'AAA', self.loop.config)['version'], 'trend_v2')
        self.assertEqual(loop.combine(self.loop.store, 'BBB', self.loop.config)['version'], 'other_v1')

    def test_winner_without_buy_does_not_fall_back_to_lower_ranked_buy(self):
        self.activate(version='other_v1')
        self.activate(version='trend_v2', keep=True)
        self.score('trend_v2', 'AAA', profit=25, signal='HOLD')
        choice = loop.combine(self.loop.store, 'AAA', self.loop.config)
        self.assertEqual((choice['version'], choice['signal']), ('trend_v2', 'HOLD'))
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()

    def test_changed_file_or_risk_report_cannot_buy_from_cached_results(self):
        self.activate()
        path = self.folder / 'strategien/trend_v2.py'
        original = path.read_text()
        path.write_text(original+'\n# changed')
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()
        path.write_text(original)
        report_file = self.folder / 'berichte/trend_v2.json'
        report = json.loads(report_file.read_text())
        report['risiko']['stop_loss'] = 3
        report_file.write_text(json.dumps(report))
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()

    def test_nonfinite_green_metrics_cannot_win(self):
        self.activate(version='trend_v1')
        saved = self.loop.store.get('loop_results:trend_v1')
        saved['symbols']['AAA']['test']['rendite'] = None
        self.loop.store.set('loop_results:trend_v1', saved)
        self.assertEqual(loop.combine(self.loop.store, 'AAA')['signal'], 'HOLD')

    def test_removed_routes_and_modes_are_unavailable_and_stock_rows_are_exposed(self):
        self.activate()
        client = create_app(self.cfg, self.store).test_client()
        self.assertEqual(client.post('/api/manual/order', json={}, headers={'X-Bot-Control':'dashboard'}).status_code, 404)
        state = client.get('/api/state').get_json()
        self.assertEqual([s['id'] for s in state['strategies']], ['daily', 'daytrade', 'loop'])
        self.assertNotIn('manual', state)
        self.assertNotIn('crypto', state)
        row = state['loop']['stocks'][0]
        self.assertEqual((row['symbol'], row['version'], row['signal']), ('AAA', 'trend_v2', 'BUY'))
        self.assertIn('trend_v2', row['views'])


if __name__ == "__main__":
    unittest.main()
