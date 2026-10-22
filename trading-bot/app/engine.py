from datetime import datetime, timezone
import hashlib
import multiprocessing as mp
import threading
import time
from zoneinfo import ZoneInfo
from contextlib import nullcontext
import psutil
import pandas as pd

from .broker import Alpaca
from .candles import snapshot
from .execution import Executor
from .decisions import position_context, position_decision
from .models import train_worker, predict, strategy_signature
from .prediction_quality import forecast_quality
from .feedback import capture_forecast, resolve_outcomes
from .daytrade import TREND_EXITS, baseline, best, bracket, exit_signal, fresh, playbook, session_open
from . import loop
from .research import Research, ballots, combine
from .store import utcnow
from .universe import active_symbols, monitored_symbols, scan, entries_ready, watched_symbols


class Engine:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.broker = Alpaca(config)
        self.executor = Executor(config, store, self.broker)
        self.stop = threading.Event()
        self.trainer = None
        self.training_started = 0
        self.research_thread = None
        self.research_lock = None
        self.thread = None
        self.monitor = None
        self.cached = {}
        self.forecast_attempts = {}
        self.scan_thread = None
        self.forecast_thread = None
        self.retest_thread = None
        self.last_scan = 0
        self.playbook_thread = None
        self.last_playbook = None
        self.sessions = set()
        self.market_open = False
        self.rules = None
        previous_mode = store.get("trading_mode", "daily")
        if not config.coordinated and store.get("parallel_enabled", False):
            raise ValueError("This journal contains parallel strategies; keep TRADING_MODE=both to monitor both")
        if not config.coordinated and previous_mode != config.trading_mode and (store.get("account_scope") or store.orders(1)):
            raise ValueError("Strategy changed: use a separate DATA_DIR for daily and daytrade modes")
        store.set("trading_mode", config.trading_mode)
        scope = hashlib.sha256((self.broker.base + config.api_key).encode()).hexdigest()
        previous_scope = store.get("account_scope")
        if previous_scope and previous_scope != scope:
            raise ValueError("Alpaca account/mode changed: use a separate DATA_DIR to preserve reconciliation history")
        if config.configured:
            store.set("account_scope", scope)
        # A restart always requires deliberate re-enabling of new entries.
        store.set("entries_enabled", False)
        retest = store.get("loop_retest") or {}
        if retest.get("status") == "running":
            store.set("loop_retest", {**retest, "status": "abgebrochen", "at": utcnow()})

    def start(self):
        self.monitor = threading.Thread(target=self.monitor_training, name="training-watchdog", daemon=True)
        self.monitor.start()
        self.thread = threading.Thread(target=self.run, name="trading-engine", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.monitor:
            self.monitor.join(timeout=3)
        if self.trainer and self.trainer.is_alive():
            self.trainer.terminate()
            self.trainer.join(timeout=5)
        if self.thread:
            self.thread.join(timeout=3)

    def monitor_training(self):
        while not self.stop.is_set():
            try:
                self.training_tick()
            except Exception as exc:
                self.store.set("training", {"status": "failed", "message": str(exc)[:240], "at": utcnow()})
            self.stop.wait(2)

    def training_tick(self, allow_start=True):
        if self.config.loop:
            return  # Trading-loop strategies are tested in the trading-loop folder, not trained here.
        if self.trainer:
            if self.trainer.is_alive():
                try:
                    rss = psutil.Process(self.trainer.pid).memory_info().rss / 1024**2
                except psutil.NoSuchProcess:
                    return
                if rss > self.config.training_memory_mb or time.monotonic() - self.training_started > self.config.training_timeout:
                    self.trainer.terminate()
                    self.trainer.join(timeout=5)
                    self.store.set("training", {"status": "resource limit reached", "at": utcnow()})
                return
            self.trainer.join(timeout=1)
            if self.trainer.exitcode and self.store.get("training", {}).get("status") == "running":
                self.store.set("training", {"status": "worker failed", "at": utcnow()})
            self.trainer = None
        if not allow_start:
            return
        last = self.store.get("last_training_attempt", 0)
        requested = self.store.get("train_requested", False)
        # Daily scheduled retraining happens outside regular market hours.
        local = datetime.now(ZoneInfo("America/New_York"))
        off_hours = local.weekday() >= 5 or not (9 <= local.hour < 17)
        due = time.time() - self.store.get("last_full_training", 0) > self.config.retrain_hours * 3600
        symbols = self.store.get("monitor_symbols", active_symbols(self.config, self.store))
        missing = [s for s in symbols if (not self.store.get(f"model:{s}") or self.store.get(f"model:{s}").get("strategy_signature") != strategy_signature(self.config))
                   and time.time() - self.store.get(f"training_attempt:{s}", 0) > 3600]
        if self.config.configured and symbols and (requested or missing or due and off_hours) and time.time() - last > 60:
            self.store.set("train_requested", False)
            self.store.set("last_training_attempt", time.time())
            self.store.set("training", {"status": "starting", "at": utcnow()})
            targets = symbols if requested or due and off_hours else missing
            if requested or due and off_hours:
                self.store.set("last_full_training", time.time())
            self.trainer = mp.get_context("spawn").Process(target=train_worker, args=(self.config, targets), daemon=True)
            self.trainer.start()
            self.training_started = time.monotonic()

    def research_tick(self, forecasts):
        if self.config.intraday or self.config.loop:
            # Intraday entries use models and the playbook; daily news has its own horizon.
            status = ("not used by the trading-loop; tested strategy rules decide" if self.config.loop
                      else "not used for day trading; prediction models and setups decide")
            if self.store.get("research_status", {}).get("status") != status:
                self.store.set("research_status", {"status": status})
            return
        if self.research_thread and self.research_thread.is_alive():
            return
        if not self.config.llm_key:
            self.store.set("research_status", {"status": "news disabled; prediction models decide"})
            return
        if not forecasts:
            return
        from .research import news_day
        day = news_day()
        due = []
        for forecast in forecasts:
            symbol = forecast["symbol"]
            attempt = self.store.get(f"research_attempt:{symbol}") or {}
            key = f"{day}:{forecast['bar_at']}"
            if attempt.get("key") != key:
                attempt = {"key": key, "tries": 0, "at": 0, "done": False}
            if attempt.get("done") or attempt["tries"] >= 3 or time.time() - attempt["at"] < 3600:
                continue
            self.store.set(f"research_attempt:{symbol}", {**attempt, "tries": attempt["tries"] + 1, "at": time.time()})
            due.append(forecast)
        if not due:
            return
        def work():
            try:
                with self.research_lock if self.research_lock is not None else nullcontext():
                    if self.stop.is_set():
                        return
                    results = Research(self.config, self.store).analyze_all(due)
                for symbol, result in results.items():
                    attempt = self.store.get(f"research_attempt:{symbol}") or {}
                    self.store.set(f"research_attempt:{symbol}", {**attempt, "done": result.get("status") == "ready"})
            except Exception:
                self.store.event("research", {"status": "unavailable", "message": "News/FreeLLM connection failed"})
            finally:
                self.store.set("research_status", {"status": "idle", "at": utcnow()})
        self.research_thread = threading.Thread(target=work, name="news-research", daemon=True)
        self.research_thread.start()

    def scan_tick(self):
        if self.config.coordinated and self.config.trading_mode != "daily" or self.config.loop:
            return
        if self.config.universe_mode != "market" or self.scan_thread and self.scan_thread.is_alive():
            return
        if time.time() - self.last_scan < self.config.scan_interval_minutes * 60:
            return
        self.last_scan = time.time()
        def work():
            try:
                scan(self.config, self.store, Alpaca(self.config), self.stop)
            except Exception:
                self.store.set("scan_status", {"status": "unavailable", "message": "Market scan failed; will retry", "at": utcnow()})
        self.scan_thread = threading.Thread(target=work, name="market-scanner", daemon=True)
        self.scan_thread.start()


    def forecast_tick(self, symbols, day):
        if self.config.loop:
            self.retest_tick()
        if self.forecast_thread and self.forecast_thread.is_alive():
            return
        if self.config.loop:
            return self.loop_tick()
        # Refresh daily inputs after session close as well as before the open.
        slot = int(time.time() // (300 if self.config.intraday else 1800))
        # Bound the in-memory cache even as discovery rotates through new stocks.
        self.cached = {s: v for s, v in self.cached.items() if s in symbols}
        self.forecast_attempts = {s: v for s, v in self.forecast_attempts.items() if s in symbols}
        def work():
            broker = Alpaca(self.config)
            for symbol in symbols:
                if self.stop.is_set():
                    return
                meta = self.store.get(f"model:{symbol}")
                if not meta or meta.get("strategy_signature") != strategy_signature(self.config):
                    continue
                key = (slot, meta["version"])
                if self.cached.get(symbol) == key:
                    continue
                if time.time() - self.forecast_attempts.get(symbol, 0) < (30 if self.config.intraday else 60):
                    continue
                self.forecast_attempts[symbol] = time.time()
                try:
                    bars = broker.bars(symbol)
                    resolve_outcomes(self.config, self.store, symbol, bars)
                    forecast = predict(self.config, meta, bars)
                    capture_forecast(self.config, self.store, forecast)
                    self.store.set(f"prediction:{symbol}", forecast)
                    expected = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta(minutes=5)
                    if not self.config.intraday or pd.Timestamp(forecast["bar_at"]) == expected:
                        self.cached[symbol] = key
                except Exception as exc:
                    self.store.event("error", {"symbol": symbol, "message": str(exc)[:240]})
        self.forecast_thread = threading.Thread(target=work, name="forecasts", daemon=True)
        self.forecast_thread.start()

    def loop_tick(self):
        """Test and signal each activated version on its symbols: daily versions at most every 30 minutes,
        intraday versions once per completed bar while the market is open."""
        if self.config.loop_intraday:
            return self.intraday_loop_tick()
        slot = int(time.time() // 1800)
        versions = loop.active_versions(self.store)
        plans = {v: loop.version_symbols(self.config, self.store, r) for v, r in versions.items()}
        self.cached = {v: k for v, k in self.cached.items() if v in versions}
        def work():
            broker, frames = Alpaca(self.config), {}
            for version, record in versions.items():
                key = (slot, record["sha256"], tuple(plans[version]))
                if self.stop.is_set():
                    return
                if self.cached.get(version) == key or time.time() - self.forecast_attempts.get(version, 0) < 60:
                    continue
                self.forecast_attempts[version] = time.time()
                for symbol in plans[version]:
                    if symbol not in frames:
                        try:
                            frames[symbol] = broker.bars(symbol)
                        except Exception as exc:
                            frames[symbol] = None
                            self.store.event("error", {"symbol": symbol, "message": "Trading-Loop: " + str(exc)[:200]})
                try:
                    usable = {s: frames[s] for s in plans[version] if frames.get(s) is not None}
                    self.store.set(f"loop_results:{version}", loop.evaluate(self.config, record, usable))
                    self.cached[version] = key
                    self.store.set(f"loop_error:{version}", None)
                except Exception as exc:
                    self.store.set(f"loop_error:{version}", {"message": str(exc)[:240], "at": utcnow()})
                    self.store.event("error", {"message": str(exc)[:240]})
        self.forecast_thread = threading.Thread(target=work, name="trading-loop-signals", daemon=True)
        self.forecast_thread.start()

    def intraday_loop_tick(self):
        """Intraday-Loop: one bar request per timeframe, then each version on its symbols after every new bar."""
        if not self.market_open:
            return
        now = pd.Timestamp.now(tz="UTC")
        versions = {v: r for v, r in loop.active_versions(self.store).items() if r.get("timeframe") in loop.TIMEFRAMES}
        plans = {v: loop.version_symbols(self.config, self.store, r) for v, r in versions.items()}
        self.cached = {v: k for v, k in self.cached.items() if v in versions}
        due = {}
        for version, record in versions.items():
            minutes = loop.TIMEFRAMES[record["timeframe"]]
            # The bar that completed last; wait 20 seconds so the provider has published it.
            key = ((now - pd.Timedelta(seconds=20)).floor(f"{minutes}min"), record["sha256"], tuple(plans[version]))
            if self.cached.get(version) != key and time.time() - self.forecast_attempts.get(version, 0) >= 30:
                due[version] = key
        if not due:
            return
        def work():
            broker = Alpaca(self.config)
            frames = {}
            start = now - pd.Timedelta(days=loop.INTRADAY_HISTORY_DAYS)
            for timeframe in {versions[v]["timeframe"] for v in due}:
                symbols = sorted({s for v in due if versions[v]["timeframe"] == timeframe for s in plans[v]})
                try:
                    data = broker.multi_bars(symbols, timeframe, start, end=now)
                    minutes = loop.TIMEFRAMES[timeframe]
                    # Completed bars only.
                    frames[timeframe] = {s: f[f.index + pd.Timedelta(minutes=minutes) <= now] for s, f in data.items()}
                except Exception as exc:
                    frames[timeframe] = {}
                    self.store.event("error", {"message": f"Intraday-Loop {timeframe}: " + str(exc)[:200]})
            for version, key in due.items():
                if self.stop.is_set():
                    return
                self.forecast_attempts[version] = time.time()
                record = versions[version]
                usable = {s: f for s, f in frames.get(record["timeframe"], {}).items() if s in plans[version] and len(f)}
                try:
                    self.store.set(f"loop_results:{version}", loop.evaluate(self.config, record, usable))
                    self.cached[version] = key
                    self.store.set(f"loop_error:{version}", None)
                except Exception as exc:
                    self.store.set(f"loop_error:{version}", {"message": str(exc)[:240], "at": utcnow()})
                    self.store.event("error", {"message": str(exc)[:240]})
        self.forecast_thread = threading.Thread(target=work, name="intraday-loop-signals", daemon=True)
        self.forecast_thread.start()

    def retest_tick(self, now=None):
        """Re-run stresstest.py when the dashboard asks and once a day before the open; the Intraday-Loop
        also re-tunes its versions every INTRADAY_RETEST_MINUTES while the market is open."""
        if self.retest_thread and self.retest_thread.is_alive() or not loop.available(self.config):
            return
        request = self.store.get("loop_retest_requested")
        today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
        daily = today.hour >= loop.RETEST_HOUR and self.store.get("loop_retest_day") != today.date().isoformat()
        hourly = (self.config.loop_intraday and self.market_open
                  and time.time() - self.store.get("loop_retest_last", 0) >= loop.INTRADAY_RETEST_MINUTES * 60)
        if not (request or daily or hourly):
            return
        if request:
            self.store.set("loop_retest_requested", None)
        elif daily:
            self.store.set("loop_retest_day", today.date().isoformat())
        self.store.set("loop_retest_last", time.time())
        versions = loop.retest_targets(self.config, (request or {}).get("version"), include_changed=bool(request),
                                       bot=None if (request or {}).get("version") else self.config.trading_mode)
        if not versions:
            return
        status = {"status": "running", "trigger": "manuell" if request else "täglich" if daily else "stündlich", "versions": versions,
                  "done": {}, "started_at": utcnow()}
        self.store.set("loop_retest", status)
        def work():
            try:
                for version in versions:
                    if self.stop.is_set():
                        return
                    self.store.set("loop_retest", {**status, "current": version})
                    status["done"][version] = loop.retest(self.config, version)
                self.store.set("loop_retest", {**status, "status": "fertig", "at": utcnow()})
            except Exception as exc:
                self.store.set("loop_retest", {**status, "status": "fehler", "message": str(exc)[:240], "at": utcnow()})
        self.retest_thread = threading.Thread(target=work, name="trading-loop-retest", daemon=True)
        self.retest_thread.start()

    def prediction(self, symbol):
        return loop.combine(self.store, symbol, self.config) if self.config.loop else self.store.get(f"prediction:{symbol}")

    def loop_risk(self, equity, owned_any):
        """risiko.py: stop per trade and the maximum drawdown from the account's peak."""
        rules = loop.risk_rules(self.config)
        if rules["ok"]:
            self.store.set("risk_rules", rules)
        # Invalid edits never remove protection from open positions: keep the last valid stop.
        self.rules = rules if rules["ok"] else {**(self.store.get("risk_rules") or rules), "ok": False, "message": rules["message"]}
        if not (self.store.get("loop_strategies") or owned_any):
            self.store.set("equity_peak", None)
            return self.store.get("halt")
        peak = max(float(self.store.get("equity_peak") or 0), equity)
        self.store.set("equity_peak", peak)
        drawdown = (peak - equity) / peak * 100 if peak > 0 else 0.
        limit = self.rules.get("max_drawdown")
        if limit and drawdown >= limit and not self.store.get("halt"):
            self.store.set("halt", {"at": utcnow(), "drawdown": round(drawdown, 2), "limit": limit, "peak": peak})
            self.store.set("entries_enabled", False)
            self.store.event("decision", {"symbol": "ALLE", "action": "SELL", "risk_exit": True,
                                          "reason": f"Maximaler Drawdown aus risiko.py erreicht ({drawdown:.1f} % ≥ {limit} %); Trading-Loop gestoppt"})
        return self.store.get("halt")

    def stop_limit(self):
        if self.config.loop:
            stop = (self.rules or {}).get("stop_loss")
            return stop / 100 if stop else self.config.stop_loss
        return self.config.effective_stop

    def loop_decision(self, forecast, now):
        signal, reason = forecast["signal"], forecast.get("reason", "")
        if signal != "BUY":
            return signal, reason
        minutes = loop.TIMEFRAMES.get(forecast.get("timeframe"))
        if self.store.get("halt"):
            return "HOLD", "Drawdown-Stopp aus risiko.py aktiv; keine neuen Käufe"
        if not (self.rules or {}).get("ok"):
            return "HOLD", "risiko.py ist unvollständig; keine neuen Käufe"
        if minutes:
            # Intraday: a buy is valid only for the bar right after its signal bar, like the backtest's next open.
            try:
                age = (now - pd.Timestamp(forecast["bar_at"])).total_seconds() - minutes * 60
            except (KeyError, TypeError, ValueError):
                age = None
            if age is None or not 0 <= age <= minutes * 60 + 60:
                return "HOLD", "Kaufsignal gehört nicht zur zuletzt abgeschlossenen Kerze"
            return signal, reason
        # A buy signal is valid only for the session right after its bar, like the backtest's next open.
        if forecast.get("target_session") not in self.sessions:
            return "HOLD", "Kaufsignal gilt nicht für die aktuelle Handelssitzung"
        return signal, reason

    def stale_forecast(self, forecast, now):
        meta = self.store.get(f"model:{forecast['symbol']}")
        return forecast_quality(self.config, meta, forecast, now)["reason"]

    def forecast_decision(self, forecast, now):
        if self.config.intraday:
            return self.daytrade_decision(forecast, now)
        if self.config.loop:
            return self.loop_decision(forecast, now)
        stale = self.stale_forecast(forecast, now)
        if stale:
            return "HOLD", stale
        signal, reason = combine(forecast, self.store.get(f"research:{forecast['symbol']}"), min_votes=self.config.min_votes)
        return signal, reason

    def daytrade_decision(self, forecast, now):
        """Rule-based setups enter; prediction models only veto entries or call exits."""
        votes = [] if self.stale_forecast(forecast, now) else [m.get("signal") for m in forecast.get("models", [])]
        buys, sells = votes.count("BUY"), votes.count("SELL")
        if votes and sells >= min(3, self.config.min_votes):
            return "SELL", f"{sells} of 3 prediction models are bearish"
        book = forecast.get("playbook")
        if not book or not fresh(book, now):
            return "HOLD", "Waiting for fresh one-minute bars for the day-trading playbook"
        if not book["signals"]:
            return "HOLD", "No day-trading setup is firing" + (f" ({book['note']})" if book.get("note") else "")
        names = " + ".join(s["name"] for s in book["signals"])
        if sells >= 2:
            return "HOLD", f"{names} fired, but {sells} prediction models are bearish (veto)"
        models = f"models {buys} bullish / {sells} bearish" if votes else "prediction models abstain"
        return "BUY", f"{names} fired: {best(book)['reason']}; {models}"

    def playbook_tick(self, symbols, now, is_open, session_close=None):
        """Fetch one-minute bars for every symbol in one request and evaluate the playbook."""
        if not self.config.intraday or not is_open or self.playbook_thread and self.playbook_thread.is_alive():
            return
        slot = now.floor("30s")
        if self.last_playbook == slot or not symbols:
            return
        self.last_playbook = slot
        def work():
            try:
                broker = Alpaca(self.config)
                today = now.tz_convert("America/New_York").date()
                opening = session_open(today)
                missing = [s for s in symbols if (self.store.get(f"playbook_base:{s}") or {}).get("day") != str(today)]
                if missing:
                    # Once per day: daily ATR/previous close and 14 sessions of opening volume and noise.
                    daily = broker.multi_bars(missing, "1Day", now - pd.Timedelta(days=40))
                    five = broker.multi_bars(missing, "5Min", now - pd.Timedelta(days=25), end=opening)
                    for symbol in missing:
                        try:
                            base = baseline(daily.get(symbol), five.get(symbol), today)
                        except ValueError as exc:
                            base = {"day": str(today), "error": str(exc)[:200]}
                        self.store.set(f"playbook_base:{symbol}", base)
                minute = broker.multi_bars(symbols, "1Min", opening, end=now)
                closing = pd.Timestamp(session_close) if session_close else None
                for symbol in symbols:
                    frame = minute.get(symbol)
                    if frame is not None:
                        frame = frame[(frame.index + pd.Timedelta(minutes=1) <= now)
                                      & (frame.index.tz_convert("America/New_York").date == today)]
                        if closing is not None:
                            frame = frame[frame.index + pd.Timedelta(minutes=1) <= closing]
                    try:
                        book = playbook(frame, self.store.get(f"playbook_base:{symbol}"), now, self.config)
                    except (ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
                        book = {"signal": "HOLD", "signals": [], "at": now.isoformat(), "bar_at": None, "note": str(exc)[:200]}
                    self.store.set(f"playbook:{symbol}", book)
                    base = self.store.get(f"playbook_base:{symbol}")
                    if not base or base.get("day") != str(today):
                        base = None
                    data = snapshot(symbol, frame, base, now, self.store.get(f"candles:{symbol}"), book)
                    data["session_close"] = closing.isoformat() if closing is not None else None
                    self.store.set(f"candles:{symbol}", data)
            except Exception as exc:
                self.store.event("error", {"message": "Day-trading playbook data failed: " + str(exc)[:200]})
        self.playbook_thread = threading.Thread(target=work, name="daytrade-playbook", daemon=True)
        self.playbook_thread.start()

    def daytrade_exit(self, symbol, pos, pnl, now):
        """Each setup's own exit; positions without a setup keep the time-based rules."""
        cfg = self.config
        record = self.store.get(f"daytrade_position:{symbol}") or {}
        peak = max(float(record.get("peak", 0)), pnl)
        if peak > record.get("peak", 0):
            self.store.set(f"daytrade_position:{symbol}", {**record, "peak": peak})
        started = record.get("started_at")
        # Unknown holding age after recovery is an exit, never a new timer.
        if not started:
            return "Day-trading holding period unknown; exiting"
        held = (now - pd.Timestamp(started)).total_seconds() / 60
        if record.get("exit"):
            try:
                price = float(pos.get("current_price") or 0) or float(pos["avg_entry_price"]) * (1 + pnl)
            except (KeyError, TypeError, ValueError):
                price = None
            book = self.store.get(f"playbook:{symbol}") or {}
            reason = exit_signal(record, price, book.get("indicators", {}) if fresh(book, now) else {})
            if reason or record["exit"] in TREND_EXITS:
                # Trend setups ride until their trail, stop or the pre-close flatten.
                return reason
            if peak >= cfg.daytrade_trail_trigger and pnl <= peak - cfg.daytrade_trail_gap:
                return f"Trailing profit stop: {pnl:.2%} after a {peak:.2%} peak"
            if held >= cfg.daytrade_run_minutes:
                return f"{record.get('name', 'Setup')} time stop after {cfg.daytrade_run_minutes} minutes"
            return None
        if peak >= cfg.daytrade_trail_trigger and pnl <= peak - cfg.daytrade_trail_gap:
            return f"Trailing profit stop: {pnl:.2%} after a {peak:.2%} peak"
        if held < cfg.daytrade_hold_minutes:
            return None
        current = {**(self.store.get(f"prediction:{symbol}") or {"symbol": symbol}),
                   "playbook": self.store.get(f"playbook:{symbol}")}
        if pnl > 0 and held < cfg.daytrade_run_minutes and self.daytrade_decision(current, now)[0] == "BUY":
            return None
        return "Day-trading holding period ended" + ("; winner extension used up" if pnl > 0 and held >= cfg.daytrade_run_minutes else "")

    def queue_session(self, clock, now):
        if self.config.same_day or not self.config.queue_daily_orders or clock["is_open"]:
            return None
        try:
            opening = pd.Timestamp(clock["next_open"])
            if opening.tzinfo is not None and 0 < (opening - now).total_seconds() <= 7 * 86400:
                return opening.tz_convert("America/New_York").date().isoformat()
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def cycle(self, snapshot=None, phase="all", allow_action=True):
        if not self.config.configured:
            self.store.set("engine", {"status": "setup", "message": "Add Alpaca credentials to .env", "at": utcnow()})
            return False
        if snapshot is None:
            account, clock = self.broker.account(), self.broker.clock()
            positions, orders = self.broker.positions(), self.broker.open_orders()
            reconciled = self.executor.reconcile()
        else:
            account, clock, positions, orders, reconciled = snapshot
        equity = float(account["equity"])
        prior = float(account.get("last_equity") or equity)
        loss_breach = prior > 0 and equity <= prior * (1 - self.config.daily_loss)
        day = pd.Timestamp(clock["timestamp"]).tz_convert("America/New_York").date().isoformat()
        if loss_breach:
            self.store.set("loss_halt_day", day)
            self.store.set("entries_enabled", False)
        loss_breach = loss_breach or self.store.get("loss_halt_day") == day
        self.store.set("account", {"equity": equity, "cash": float(account["cash"]), "buying_power": float(account["buying_power"]),
            "daily_pnl": equity - prior, "positions": [{k: p.get(k, "us_equity" if k == "asset_class" else None) for k in ("symbol", "asset_class", "qty", "market_value", "unrealized_pl", "unrealized_plpc", "avg_entry_price")} for p in positions],
            "market_open": clock["is_open"], "loss_halt": loss_breach, "at": utcnow()})
        if time.time() - self.store.get("equity_sample", 0) >= 900:
            self.store.event("equity", {"equity": equity, "daily_pnl": equity - prior})
            self.store.set("equity_sample", time.time())
        now = pd.Timestamp(clock["timestamp"])
        local = now.tz_convert("America/New_York")
        queue_session = self.queue_session(clock, now)
        self.sessions = {s for s in (day if clock["is_open"] else None, queue_session) if s}
        self.market_open = bool(clock["is_open"])
        entry_window = local.hour == 9 and 35 <= local.minute <= 59
        flatten = False
        if self.config.same_day:
            close = pd.Timestamp(clock["next_close"]) if clock.get("next_close") else None
            valid_close = close is not None and close.tzinfo is not None and close.tz_convert("America/New_York").date() == local.date()
            remaining = (close - now).total_seconds() / 60 if valid_close else 0
            flatten = bool(clock["is_open"] and (not valid_close or remaining <= self.config.daytrade_close_minutes))
            # The opening-range breakout needs the first five minutes only.
            entry_window = (clock["is_open"] and (local.hour, local.minute) >= (9, 35)
                            and remaining > self.config.daytrade_close_minutes + 5)
        acted = not allow_action
        symbols = monitored_symbols(self.config, self.store, positions, orders)
        self.store.set("monitor_symbols", symbols)
        halt = None
        if self.config.loop:
            halt = self.loop_risk(equity, any(self.store.get(f"owned:{p['symbol']}", False) for p in positions))
        if phase != "decisions":
            for order in orders:
                if acted or order["side"] != "buy" or not order.get("client_order_id", "").startswith("ensemble-"):
                    continue
                if self.config.coordinated and not self.store.owns_order(order):
                    continue
                record = self.store.order(order.get("client_order_id", "")) or {}
                queued = record.get("queued_for_open", False) and not self.config.same_day
                keep_queued, invalid_queue = False, False
                if queued:
                    target = record.get("session")
                    keep_queued = (self.config.queue_daily_orders and
                                   (queue_session is not None and target == queue_session
                                    or clock["is_open"] and target == day and local.hour == 9))
                    prediction = self.prediction(order["symbol"])
                    invalid_queue = (not prediction or prediction.get("target_session") != target
                                     or self.forecast_decision(prediction, now)[0] != "BUY"
                                     or order["symbol"] not in active_symbols(self.config, self.store)
                                     or not entries_ready(self.config, self.store))
                expired = False
                if self.config.same_day:
                    submitted = order.get("submitted_at") or order.get("created_at")
                    expired = not submitted or (now - pd.Timestamp(submitted)).total_seconds() > 60
                if (not self.store.get("entries_enabled", False) or loss_breach
                        or not (keep_queued if queued else entry_window) or expired or invalid_queue):
                    self.broker.cancel(order["id"])
                    acted = True
            unresolved = self.store.unresolved_orders()
            # Risk exits run before predictions and news, including rotated-out holdings.
            for symbol in symbols:
                pos = next((p for p in positions if p["symbol"] == symbol), None)
                pending = any(o["symbol"] == symbol for o in [*orders, *unresolved])
                owned = self.store.get(f"owned:{symbol}", False)
                if not pos and not pending:
                    if owned and self.config.coordinated:
                        observed = self.store.get(f"flat_observed:{symbol}")
                        if observed is None:
                            self.store.set(f"flat_observed:{symbol}", time.time())
                            continue
                        if time.time() - observed < 15:
                            continue
                    if owned and self.config.intraday:
                        self.store.set(f"flat_since:{symbol}", time.time())
                    self.store.set(f"owned:{symbol}", False)
                    self.store.set(f"exit_pending:{symbol}", False)
                    self.store.set(f"exit_reason:{symbol}", None)
                    self.store.set(f"exit_status:{symbol}", None)
                    if owned:
                        self.store.set(f"unbracketed:{symbol}", None)
                elif self.config.coordinated:
                    self.store.set(f"flat_observed:{symbol}", None)
                exit_reason = (self.store.get(f"exit_reason:{symbol}") or "Completing the previously requested exit") if self.store.get(f"exit_pending:{symbol}", False) else None
                if pos and float(pos["qty"]) > 0 and owned:
                    pnl_fraction = float(pos.get("unrealized_plpc") or 0)
                    if loss_breach:
                        exit_reason = "Account daily-loss limit reached"
                    elif pnl_fraction <= -self.stop_limit():
                        exit_reason = "Position stop-loss reached" + (" (risiko.py)" if self.config.loop else "")
                    elif flatten:
                        exit_reason = ("Intraday-Loop: flat vor Börsenschluss" if self.config.loop_intraday
                                       else "Day-trading exit before market close")
                    elif halt:
                        exit_reason = "Maximaler Drawdown aus risiko.py erreicht; Position wird geschlossen"
                    elif self.config.loop and not loop.owner_version(self.store, symbol):
                        exit_reason = "Trading-Loop-Strategie abgeschaltet; Position wird geschlossen"
                    guard = self.store.get(f"unbracketed:{symbol}")
                    if guard and not exit_reason:
                        # Fractional entries carry no broker bracket; enforce its levels here.
                        if pnl_fraction <= -guard["stop"]:
                            exit_reason = "Stop-loss reached (bot-managed for fractional shares)"
                        elif not self.config.intraday and guard.get("target") and pnl_fraction >= guard["target"]:
                            exit_reason = "Take-profit reached (bot-managed for fractional shares)"
                    if self.config.intraday and not exit_reason:
                        exit_reason = self.daytrade_exit(symbol, pos, pnl_fraction, now)
                if exit_reason and pos and float(pos["qty"]) > 0 and owned:
                    # Keep the exit visible even if another symbol used this snapshot.
                    self.store.set(f"exit_pending:{symbol}", True)
                    self.store.set(f"exit_reason:{symbol}", exit_reason)
                    if clock["is_open"] and not acted:
                        status = self.executor.execute(symbol, "SELL", day, account, positions, orders, loss_breach=loss_breach, force_exit=True)
                        self.store.set(f"exit_status:{symbol}", status)
                        self.store.event("decision", {"symbol": symbol, "action": "SELL", "reason": exit_reason + "; " + status, "risk_exit": True})
                        acted = bool(self.executor.changed)
        if phase == "risk":
            return acted
        forecasts = []
        for symbol in symbols:
            prediction = self.prediction(symbol)
            if self.config.intraday:
                # Day trading needs no trained model: the playbook alone can qualify an entry.
                book = self.store.get(f"playbook:{symbol}")
                if prediction or book:
                    item = dict(prediction or {"symbol": symbol, "models": [], "expected_return": 0., "forecast_signal": "HOLD",
                                               "trading_mode": self.config.trading_mode, "eligible": False})
                    item["playbook"] = book
                    item["rank"] = len((book or {}).get("signals") or []) if book and fresh(book, now) else 0
                    forecasts.append(item)
            elif prediction:
                forecasts.append(dict(prediction))
        # Consider sales first, then the strongest setups or highest predicted returns. A held
        # stock, exhausted allocation or duplicate intent must not starve other candidates.
        forecasts.sort(key=lambda f: (f["forecast_signal"] != "SELL", -f.get("rank", 0), -f["expected_return"], f["symbol"]))
        candidates = set(active_symbols(self.config, self.store))
        for forecast in forecasts:
            symbol = forecast["symbol"]
            try:
                research = self.store.get(f"research:{symbol}")
                action, reason = self.forecast_decision(forecast, now)
                position = position_context(self.config, self.store, symbol, positions)
                action, reason = position_decision(action, reason, position)
                queued_buy = action == "BUY" and queue_session is not None
                exiting = position["owned"] and self.store.get(f"exit_pending:{symbol}", False)
                if exiting:
                    action = "SELL"
                    reason = (self.store.get(f"exit_reason:{symbol}") or "Completing the previously requested exit") + "; " + (self.store.get(f"exit_status:{symbol}") or "Exit pending")
                    if not clock["is_open"]:
                        reason = "Market closed; exit pending. " + reason
                elif not clock["is_open"] and not queued_buy:
                    reason = "Market closed; " + reason
                elif not reconciled:
                    reason = "Unresolved order outcome; new decisions blocked"
                elif action == "BUY" and (symbol not in candidates or not entries_ready(self.config, self.store)):
                    reason = "Waiting for a complete, fresh market scan and candidate selection"
                elif queued_buy and forecast.get("target_session") != queue_session:
                    reason = "Waiting for a completed daily forecast targeting the next market session"
                elif action == "BUY" and not entry_window and not queued_buy:
                    reason = "Outside the strategy entry window"
                elif not acted and action in {"BUY", "SELL"}:
                    if action == "SELL":
                        self.store.set(f"exit_pending:{symbol}", True)
                        self.store.set(f"exit_reason:{symbol}", reason)
                    queue_args = {"queued_for_open": True, "reference_price": forecast.get("last_close")} if queued_buy else {}
                    if self.config.intraday and action == "BUY":
                        plan = best(forecast["playbook"])
                        stop_fraction, target_fraction = bracket(self.config, plan)
                        queue_args.update(stop_fraction=stop_fraction, target_fraction=target_fraction, plan=plan)
                    if self.config.loop and action == "BUY":
                        queue_args.update(stop_fraction=self.stop_limit() if self.rules.get("ok") else None)
                        # Recorded before the order: the buying version decides this position's exit.
                        self.store.set(f"loop_owner:{symbol}", forecast["version"])
                    status = self.executor.execute(symbol, action, queue_session if queued_buy else day, account, positions, orders,
                                                   loss_breach=loss_breach, **queue_args)
                    if action == "SELL":
                        self.store.set(f"exit_status:{symbol}", status)
                    reason += "; " + status
                    acted = bool(self.executor.changed)
                votes = {forecast.get("version", "Trading-Loop"): forecast.get("signal")} if self.config.loop else ballots(forecast, research)
                forecast.update({"action": action, "reason": reason, "research": research,
                                 "votes": votes,
                                 "position": position, "decision_at": utcnow()})
                previous = self.store.get(f"forecast:{symbol}", {})
                if previous.get("action") != action or previous.get("reason") != reason:
                    self.store.event("decision", {"symbol": symbol, "action": action, "reason": reason})
                self.store.set(f"forecast:{symbol}", forecast)
            except Exception as exc:
                self.store.event("error", {"symbol": symbol, "message": str(exc)[:240]})
        # Network-heavy scanning, forecasting and LLM calls cannot stall risk polling.
        self.scan_tick()
        # Day trading also evaluates its setups for the user's own holdings (shown, never traded).
        watched = watched_symbols(self.config, self.store, positions) if self.config.intraday else []
        self.playbook_tick(list(dict.fromkeys([*symbols, *watched])), now, clock["is_open"], clock.get("next_close"))
        self.forecast_tick(symbols, day)
        self.research_tick(forecasts)
        self.store.set("engine", {"status": "running", "message": f"Monitoring {len(symbols)} symbols in {self.config.trading_mode} mode", "at": utcnow()})

        return acted

    def run(self):
        next_cycle = 0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_cycle:
                    self.cycle()
                    next_cycle = time.monotonic() + self.config.poll_seconds
            except Exception as exc:
                self.store.set("engine", {"status": "error", "message": str(exc)[:240], "at": utcnow()})
                next_cycle = time.monotonic() + self.config.poll_seconds
            self.stop.wait(2)
