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
from test_loop import PortfolioCase, make_loop_dir, add_version
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


class ResumeAllTests(Fixture):
    def test_start_all_also_switches_trading212_on_and_says_what_is_missing(self):
        cfg = replace(self.cfg, trading212_data_source="twelvedata", twelvedata_key="")
        self.store.set("account", {"equity": 1000., "loss_halt": False})
        client = create_app(cfg, self.store).test_client()
        response = client.post("/api/control", json={"action": "resume"}, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertIn("TWELVEDATA_API_KEY", response.get_json()["warning"])
        for mode in ("daily", "daytrade", "loop", "loop_day"):
            self.assertTrue(StrategyStore(self.store, mode).get("entries_enabled"), mode)
        self.assertTrue(context(cfg)[1].get("entries_enabled"))
        state = client.get("/api/state").get_json()["trading212"]
        self.assertIn("TWELVEDATA_API_KEY", state["ready_problem"])
        self.assertEqual((state["mappings"]["count"], state["mappings"]["error"]), (1, None))

    def test_real_blockers_keep_trading212_paused_but_never_the_alpaca_bots(self):
        cfg = replace(self.cfg, trading212_key="")
        self.store.set("account", {"equity": 1000., "loss_halt": False})
        client = create_app(cfg, self.store).test_client()
        response = client.post("/api/control", json={"action": "resume"}, headers=HEADERS)
        self.assertIn("TRADING212_API_KEY", response.get_json()["warning"])
        self.assertTrue(StrategyStore(self.store, "daily").get("entries_enabled"))
        self.assertFalse(context(cfg)[1].get("entries_enabled"))
        own = client.post("/api/trading212/control", json={"action": "resume"}, headers=HEADERS)
        self.assertEqual(own.status_code, 409)

    def test_the_dashboard_says_whether_the_instrument_file_loaded(self):
        self.mapping.write_text("{broken")
        state = create_app(self.cfg, self.store).test_client().get("/api/state").get_json()["trading212"]
        self.assertEqual(state["mappings"]["count"], 0)
        self.assertIn("not readable JSON", state["mappings"]["error"])
        self.mapping.unlink()
        state = create_app(self.cfg, self.store).test_client().get("/api/state").get_json()["trading212"]
        self.assertEqual(state["mappings"]["error"], "file not found")


class EngineErrorMessageTests(PortfolioCase):
    def test_a_failing_bot_is_named_with_its_error_and_the_others_keep_running(self):
        self.loop_day.cycle = Mock(side_effect=BrokerError("Alpaca HTTP 403 for GET /v2/stocks/bars"))
        self.portfolio.cycle()
        message = self.store.get("portfolio_engine")["message"]
        self.assertIn("Intraday-Loop: Alpaca HTTP 403 for GET /v2/stocks/bars", message)
        self.assertIn("other bots keep running", message)
        self.assertEqual(self.loop_day.store.get("engine")["status"], "error")
        self.assertNotEqual((self.daily.store.get("engine") or {}).get("status"), "error")


class ShippedInstrumentListTests(unittest.TestCase):
    def test_shipped_trading212_list_is_valid_and_euro_only(self):
        path = Path(__file__).resolve().parents[1] / "config" / "trading212-instruments.json"
        data = mappings(Config(trading212_instruments=path))
        self.assertGreaterEqual(len(data), 10)

        def isin_check(isin):
            digits = "".join(str(int(c, 36)) for c in isin[:-1])
            total = sum(n // 10 + n % 10 for n in (int(d) * (2 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits))))
            return (10 - total % 10) % 10 == int(isin[-1])

        for ticker, spec in data.items():
            self.assertTrue(isin_check(spec["isin"]), ticker)
            # Same currency as a EUR account: no FX needed with TRADING212_ALLOW_FX=false.
            self.assertEqual(spec["currency"], "EUR", ticker)
            self.assertIn(spec["mic"], {"XETR", "XAMS", "XPAR"}, ticker)


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
        cfg = Config(readonly_token="k" * 40, trading212_key="k" * 40)
        self.assertEqual(cfg.readonly_token, "")
        self.assertIn("TRADING212_API_KEY", cfg.readonly_problem)
        self.assertNotIn("k" * 40, cfg.readonly_problem)

    def test_quotes_around_env_values_are_removed(self):
        env = {"READONLY_API_TOKEN": '"' + "z" * 40 + '"', "TRADING212_API_KEY": "'abc'", "TRADING212_API_SECRET": " def ",
               "TRADING212_ENABLED": "false", "TRADING_MODE": "both"}
        with patch.dict(os.environ, env):
            cfg = Config.from_env()
        self.assertEqual((cfg.readonly_token, cfg.readonly_problem), ("z" * 40, ""))
        self.assertEqual((cfg.trading212_key, cfg.trading212_secret), ("abc", "def"))


if __name__ == "__main__":
    unittest.main()


class LegacyJournalTests(unittest.TestCase):
    def test_old_timestamp_research_attempts_do_not_stop_the_prediction_bot(self):
        import tempfile
        from app.engine import Engine
        from app.store import Store
        with tempfile.TemporaryDirectory() as folder:
            cfg = Config(data_dir=Path(folder), api_key="k", api_secret="s", llm_key="llm", symbols=("SPY",), universe_mode="watchlist")
            store = Store(cfg.data_dir)
            store.set("research_attempt:SPY", 1760000000.0)  # Written by the first bot version.
            engine = Engine(cfg, store)
            with patch("app.engine.Research") as research:
                research.return_value.analyze_all.return_value = {"SPY": {"status": "ready"}}
                engine.research_tick([{"symbol": "SPY", "bar_at": "2026-10-23T04:00:00+00:00"}])
                engine.research_thread.join(5)
            attempt = store.get("research_attempt:SPY")
            self.assertEqual((attempt["tries"], attempt["done"]), (1, True))


def yahoo_chart(days=1100, exchange="GER", currency="EUR", kind="EQUITY", price_at=None, split_day=None):
    """A Yahoo Finance chart response shaped like the real one (session-start timestamps, adjclose)."""
    import pandas as pd
    index = pd.bdate_range(end="2026-10-23", periods=days, tz="Europe/Berlin") + pd.Timedelta(hours=9)
    close = [100. + i * .01 for i in range(days)]
    adj = [c * (.5 if split_day is not None and i < split_day else 1.) for i, c in enumerate(close)]
    stamps = [int(t.timestamp()) for t in index]
    meta = {"currency": currency, "symbol": "SIE.DE", "exchangeName": exchange, "fullExchangeName": "XETRA",
            "instrumentType": kind, "exchangeTimezoneName": "Europe/Berlin", "regularMarketPrice": 214.4,
            "regularMarketTime": int((price_at or index[-1]).timestamp())}
    quote = {"open": close, "high": [c * 1.01 for c in close], "low": [c * .99 for c in close], "close": close,
             "volume": [1000] * days}
    return {"chart": {"result": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [quote], "adjclose": [{"adjclose": adj}]}}],
                      "error": None}}


class YahooDataTests(Fixture):
    def setUp(self):
        super().setUp()
        import pandas as pd
        from test_trading212 import instrument
        from app.trading212 import YahooData, market_data
        self.instrument, self.pd = instrument(), pd
        self.now = pd.Timestamp("2026-10-23T12:00:00Z")  # Friday, Xetra open: today's candle is unfinished.
        self.data = market_data(self.cfg)
        self.assertIsInstance(self.data, YahooData)
        self.data._next_request = 0.

    def respond(self, body):
        response = Mock(status_code=200)
        response.json.return_value = body
        response.raise_for_status = Mock()
        self.data.session.get = Mock(return_value=response)
        self.data._lock = __import__("threading").Lock()
        return self.data.session.get

    def test_daily_candles_are_complete_adjusted_and_fetched_once_a_day(self):
        get = self.respond(yahoo_chart(split_day=10))
        with patch("app.trading212.time.sleep"):
            frame = self.data.bars(self.instrument, SPEC, self.now)
            again = self.data.bars(self.instrument, SPEC, self.now + self.pd.Timedelta(hours=2))
        self.assertIs(frame, again)
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith("/SIE.DE"))  # Mapping symbol + Xetra suffix.
        self.assertEqual(len(frame), 1099)  # Today's unfinished candle is excluded.
        self.assertEqual(str(frame.index[-1].tz_convert("Europe/Berlin").date()), "2026-10-22")
        self.assertAlmostEqual(frame.close.iloc[0], 50.)  # Scaled by the split adjustment factor.
        self.assertEqual(frame.attrs["market_timezone"], "Europe/Berlin")

    def test_wrong_exchange_currency_or_short_history_is_refused(self):
        for body, text in ((yahoo_chart(exchange="PAR"), "does not match"), (yahoo_chart(currency="USD"), "does not match"),
                           (yahoo_chart(days=500), "1,000")):
            self.data._bars = {}
            self.respond(body)
            with self.subTest(text=text), patch("app.trading212.time.sleep"):
                with self.assertRaisesRegex(BrokerError, text):
                    self.data.bars(self.instrument, SPEC, self.now)

    def test_prices_older_than_the_limit_never_size_an_order(self):
        self.respond(yahoo_chart(price_at=self.now - self.pd.Timedelta(minutes=10)))
        with patch("app.trading212.time.sleep"):
            self.assertEqual(self.data.quote(self.instrument, SPEC, self.now), 214.4)
            self.respond(yahoo_chart(price_at=self.now - self.pd.Timedelta(minutes=45)))
            with self.assertRaisesRegex(BrokerError, "older than"):
                self.data.quote(self.instrument, SPEC, self.now)


class SessionWindowTests(unittest.TestCase):
    def test_entries_run_all_session_but_not_right_at_the_open_or_close(self):
        import pandas as pd
        from app.trading212_engine import entry_window
        session = {"open": True, "opening": pd.Timestamp("2026-10-23T07:00:00Z"), "close": pd.Timestamp("2026-10-23T15:30:00Z")}
        at = lambda hhmm: pd.Timestamp(f"2026-10-23T{hhmm}:00Z")
        self.assertFalse(entry_window(session, at("07:03")))
        self.assertTrue(entry_window(session, at("07:05")))
        self.assertTrue(entry_window(session, at("12:00")))  # Previously only the first hour was allowed.
        self.assertTrue(entry_window(session, at("15:00")))
        self.assertFalse(entry_window(session, at("15:10")))
        self.assertFalse(entry_window({**session, "open": False}, at("12:00")))
