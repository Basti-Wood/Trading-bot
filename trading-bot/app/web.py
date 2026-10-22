import hmac
import pandas as pd
from pathlib import Path
import psutil
from flask import Flask, jsonify, request, send_from_directory
from . import loop
from .candles import snapshot, forecast_overlay
from .daytrade import NY, session_open
from .allocation import BOTS, NAMES, allocation, bot_capital, bot_share, validate
from .broker import Alpaca, BrokerError
from .store import utcnow
from .feedback import summary_key
from .prediction_quality import forecast_quality
from .prediction_chart import payload as prediction_payload
from .universe import active_symbols
from .portfolio import strategy_contexts
from .decisions import position_context

MAX_LOOP_STRATEGIES = 10
# About 90 sessions of daily candles for the forecast chart.
DAILY_LOOKBACK_DAYS = 140


def create_app(config, store, broker_factory=Alpaca):
    contexts = {cfg.trading_mode: (cfg, state) for cfg, state in strategy_contexts(config, store)}
    parallel = config.trading_mode == "both"

    def loop_bots():
        """The stock trading-loop dashboard context."""
        return [(mode, *contexts[mode]) for mode in ("loop",) if mode in contexts]

    def loop_state(equity):
        if "loop" not in contexts:
            return None
        cfg, state = contexts["loop"]
        versions = loop.catalogue(cfg)
        running, halts, peaks = [], [], []
        for version in versions:
            version["active"] = False
            version["bot_available"] = version["bot"] in contexts
        for mode, mode_cfg, mode_state in loop_bots():
            active = loop.active_versions(mode_state)
            for version in versions:
                version["active"] = version["active"] or version["version"] in active
            for name, record in sorted(active.items()):
                results = mode_state.get(f"loop_results:{name}") or {}
                fresh = results.get("sha256") == record["sha256"]
                rows = []
                for symbol in loop.version_symbols(mode_cfg, mode_state, record):
                    item = (results.get("symbols") or {}).get(symbol) if fresh else None
                    if item:
                        item = {k: v for k, v in item.items() if k != "chart"}  # Served by /api/loop/chart.
                    decision = mode_state.get(f"forecast:{symbol}") or {}
                    rows.append({"symbol": symbol, "result": item, "action": decision.get("action"),
                                 "reason": decision.get("reason"), "position": decision.get("position"),
                                 "own": store.owner(symbol) not in BOTS and bool((decision.get("position") or {}).get("qty"))})
                running.append({"version": name, "bot": mode, "symbol": record["symbol"], "activated_at": record.get("activated_at"),
                                "computed_at": results.get("at") if fresh else None, "error": mode_state.get(f"loop_error:{name}"),
                                "green": sum(1 for r in rows if ((r["result"] or {}).get("test") or {}).get("gruen")), "rows": rows})
            halts.append(mode_state.get("halt"))
            peaks.append(mode_state.get("equity_peak"))
        peak = max((p for p in peaks if p), default=None)
        return {"available": loop.available(cfg), "folder": str(cfg.loop_dir or ""), "rules": loop.risk_rules(cfg),
                "versions": versions, "running": running, "stocks": loop_stock_rows(cfg, state, running), "errors": loop.errors(cfg), "halt": next((h for h in halts if h), None),
                "peak": peak, "drawdown": (peak - equity) / peak * 100 if peak and equity else None,
                "entries_enabled": state.get("entries_enabled", False),
                "retest": state.get("loop_retest"), "retest_requested": bool(state.get("loop_retest_requested")),
                "retest_hour": loop.RETEST_HOUR}


    def loop_stock_rows(cfg, state, running):
        symbols = sorted({r["symbol"] for v in running for r in v["rows"]})
        rows = []
        for symbol in symbols:
            choice = loop.combine(state, symbol, cfg)
            decision = state.get(f"forecast:{symbol}") or {}
            rows.append({"symbol": symbol, "version": choice.get("version"), "test": choice.get("test"),
                         "signal": choice.get("strategy_signal", choice.get("signal")), "reason": choice.get("reason"),
                         "action": decision.get("action", "WAIT"), "views": choice.get("views", {}),
                         "owner": loop.owner_version(state, symbol) if state.get(f"owned:{symbol}", False) else None})
        return rows

    app = Flask(__name__, static_folder=None)
    root = Path(__file__).resolve().parent.parent / "dashboard"
    app.config["MAX_CONTENT_LENGTH"] = 4096
    if not config.password:
        app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", "[::1]"]

    @app.before_request
    def secure():
        if request.path == "/healthz":
            return None
        if config.password:
            auth = request.authorization
            if not auth or auth.username != "admin" or not hmac.compare_digest((auth.password or "").encode(), config.password.encode()):
                return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Trading dashboard"'})
        if request.method != "GET":
            if request.headers.get("X-Bot-Control") != "dashboard" or not request.is_json:
                return jsonify(error="Use authenticated same-origin dashboard controls"), 403
            origin = request.headers.get("Origin")
            # Behind an HTTPS reverse proxy host_url is the internal address, so also accept the configured public origins.
            if origin and origin.lower() not in {request.host_url.rstrip("/").lower(), *config.origins}:
                return jsonify(error="Cross-origin controls are blocked"), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return send_from_directory(root, "index.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(root, name)

    @app.get("/api/candles/<symbol>")
    def candles(symbol):
        if not loop.STOCK.fullmatch(symbol):
            return jsonify(error="Invalid stock symbol"), 400
        if "daytrade" not in contexts:
            return jsonify(error="Day trading is not enabled"), 409
        cfg, state = contexts["daytrade"]
        allowed = {*active_symbols(cfg, state), *state.get("monitor_symbols", [])}
        if symbol not in allowed:
            return jsonify(error="Stock is not monitored by the day-trading bot"), 404
        now = pd.Timestamp.now(tz="UTC")
        day = str(now.tz_convert(NY).date())
        data = state.get(f"candles:{symbol}")
        if not data or data.get("day") != day or (now - pd.Timestamp(data["at"])).total_seconds() > 60:
            if not cfg.configured:
                return jsonify(error="Configure Alpaca first"), 409
            try:
                broker = broker_factory(cfg)
                sessions = broker.calendar(day, day)
                session = next((r for r in sessions if r["date"] == day), None)
                opening = session_open(now.tz_convert(NY).date())
                closing = pd.Timestamp(f"{day} {session['close']}", tz=NY) if session else opening
                frames = broker.multi_bars([symbol], "1Min", opening, end=min(now, closing)) if session and now > opening else {}
                base = state.get(f"playbook_base:{symbol}")
                if not base or base.get("day") != day:
                    base = None
                data = snapshot(symbol, frames.get(symbol), base, now, previous=data)
                data["session_close"] = closing.isoformat() if session else None
                state.set(f"candles:{symbol}", data)
            except (BrokerError, ValueError, KeyError):
                return jsonify(error="Candle data unavailable; will retry"), 502
        prediction = state.get(f"prediction:{symbol}") or {}
        quality = forecast_quality(cfg, state.get(f"model:{symbol}"), prediction, now)
        return jsonify(**data, quality=quality,
                       forecast=forecast_overlay(prediction, now, data.get("session_close")) if quality["ready"] else None)

    @app.get("/api/predictions/<symbol>")
    def predictions(symbol):
        """Candles plus every model's recorded forecast and the observed outcome."""
        selected = request.args.get("strategy", "daily")
        if not loop.STOCK.fullmatch(symbol):
            return jsonify(error="Invalid stock symbol"), 400
        if selected not in contexts or selected == "loop":
            return jsonify(error="Forecast charts exist for the prediction and day-trading bots"), 400
        cfg, state = contexts[selected]
        if symbol not in {*active_symbols(cfg, state), *state.get("monitor_symbols", [])}:
            return jsonify(error="Stock is not monitored by this bot"), 404
        now = pd.Timestamp.now(tz="UTC")
        cached = state.get(f"prediction_bars:{symbol}")
        ttl = 120 if cfg.intraday else 1800
        if cached and (now - pd.Timestamp(cached["at"])).total_seconds() <= ttl:
            frame = pd.DataFrame(cached["bars"]).set_index("at") if cached["bars"] else None
            if frame is not None:
                frame.index = pd.to_datetime(frame.index, utc=True)
        else:
            if not cfg.configured:
                return jsonify(error="Configure Alpaca first"), 409
            try:
                start = now - (pd.Timedelta(days=5) if cfg.intraday else pd.Timedelta(days=DAILY_LOOKBACK_DAYS))
                frame = broker_factory(cfg).multi_bars([symbol], "5Min" if cfg.intraday else "1Day", start, end=now).get(symbol)
            except (BrokerError, ValueError, KeyError):
                return jsonify(error="Price data unavailable; will retry"), 502
            if frame is not None and cfg.intraday:
                frame = frame[frame.index + pd.Timedelta(minutes=5) <= now]  # completed bars only
            rows = [] if frame is None else [{"at": at.isoformat(), **{k: float(row[k]) for k in ("open", "high", "low", "close")}}
                                             for at, row in frame.iterrows()]
            state.set(f"prediction_bars:{symbol}", {"at": now.isoformat(), "bars": rows})
        return jsonify(prediction_payload(cfg, state, symbol, frame, selected, now))

    @app.get("/api/loop/chart/<symbol>")
    def loop_chart(symbol):
        if not loop.STOCK.fullmatch(symbol):
            return jsonify(error="Invalid stock symbol"), 400
        if "loop" not in contexts:
            return jsonify(error="Der Trading-Loop läuft nur mit TRADING_MODE=both"), 409
        cfg, state = contexts["loop"]
        choice = loop.combine(state, symbol, cfg)
        active = loop.active_versions(state)
        # The deciding version, else any active version that evaluated this stock.
        versions = [v for v in [choice.get("version"), *sorted(active)] if v in active]
        for version in dict.fromkeys(versions):
            results = state.get(f"loop_results:{version}") or {}
            item = (results.get("symbols") or {}).get(symbol)
            if results.get("sha256") == active[version]["sha256"] and item and item.get("chart"):
                decision = state.get(f"forecast:{symbol}") or {}
                return jsonify(symbol=symbol, version=version, deciding=version == choice.get("version"),
                               chart=item["chart"], test=item.get("test"), signal=item.get("signal"),
                               strategy_reason=item.get("reason"), reason=choice.get("reason"),
                               action=decision.get("action", "WAIT"), computed_at=results.get("at"))
        return jsonify(error="Noch keine Chartdaten: die aktiven Versionen haben diese Aktie noch nicht berechnet"), 404

    @app.get("/api/state")
    def state():
        selected = request.args.get("strategy", next(iter(contexts)))
        if selected not in contexts:
            return jsonify(error="Unknown strategy"), 400
        cfg, state_store = contexts[selected]
        account = state_store.get("account")
        equity = float((account or {}).get("equity") or 0)
        if account and parallel:
            account = {**account, "positions": [{**p, "strategy": store.owner(p["symbol"]) or "external"} for p in account.get("positions", [])]}
        budget = bot_capital(config, store, equity) if parallel else None
        symbols = []
        for symbol in dict.fromkeys([*active_symbols(cfg, state_store), *state_store.get("monitor_symbols", [])]):
            position = position_context(cfg, state_store, symbol, account.get("positions", [])) if account else None
            forecast = state_store.get(f"forecast:{symbol}")
            if forecast and "decision_at" not in forecast:
                # Old cached HOLDs did not distinguish a neutral outlook from ownership.
                forecast = {**forecast, "action": "WAIT", "reason": "Waiting for a fresh decision with current position data"}
            symbols.append({"symbol": symbol, "position": position, "forecast": forecast,
                            "quality": (forecast_quality(cfg, state_store.get(f"model:{symbol}"), forecast, pd.Timestamp.now(tz="UTC"))
                                        if forecast and not cfg.loop else None),
                            "model": state_store.get(f"model:{symbol}"), "candidate": state_store.get(f"candidate:{symbol}"),
                            "research": state_store.get(f"research:{symbol}"),
                            "feedback": state_store.get(summary_key(cfg, symbol), {"captured": 0, "pending": 0, "resolved": 0, "excluded": 0, "scores": {}, "latest": []})})
        rss, cpu, resources_available = 0, None, False
        try:
            process = psutil.Process()
            rss = process.memory_info().rss
            for child in process.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except psutil.Error:
                    pass
            cpu, resources_available = psutil.cpu_percent(), True
        except (psutil.Error, OSError):
            pass  # Resource telemetry may be unavailable in a restricted host.
        return jsonify(
            portfolio_mode=config.trading_mode,
            portfolio_engine=store.get("portfolio_engine"),
            strategies=[{"id": c.trading_mode, "name": NAMES.get(c.trading_mode, c.trading_mode),
                         "entries_enabled": st.get("entries_enabled", False),
                         "capital_fraction": bot_share(c, store), "weight": allocation(config, store)["weights"].get(c.trading_mode) if parallel else None,
                         "capital_ceiling": (budget if parallel else c.sizing_capital(equity)) * bot_share(c, store),
                         "gross_ceiling": (budget if parallel else c.sizing_capital(equity)) * bot_share(c, store) * c.gross_fraction,
                         "halt": st.get("halt") if c.loop else None,
                         "engine": st.get("engine", {"status": "starting"}),
                         "training": st.get("training", {"status": "waiting"})} for c, st in contexts.values()],
            any_entries_enabled=any(st.get("entries_enabled", False) for _, st in contexts.values()),
            allocation={**allocation(config, store), "budget": budget, "gross_fraction": config.gross_fraction,
                        "position_fraction": config.position_fraction} if parallel else None,
            loop=loop_state(equity), news_report=store.get("news_report"),
            at=utcnow(), mode="paper" if cfg.paper else "live", trading_mode=cfg.trading_mode, configured=cfg.configured,
            universe=state_store.get("universe", {"symbols": active_symbols(cfg, state_store), "candidates": []}),
            scan_status=state_store.get("scan_status", {"status": "waiting" if cfg.universe_mode == "market" else "watchlist"}),
            entries_enabled=state_store.get("entries_enabled", False), engine=state_store.get("engine", {"status": "starting"}),
            account=account, training=state_store.get("training", {"status": "waiting for data"}),
            research_status=state_store.get("research_status", {"status": "waiting"}),
            symbols=symbols,
            orders=store.orders(50), events=state_store.history("decision", 30), errors=state_store.history("error", 10),
            training_history=state_store.history("training", 25), equity=store.history("equity", 100),
            resources={"process_memory_mb": round(rss / 1024**2, 1), "host_cpu_percent": cpu, "available": resources_available,
                       "training_memory_mb": cfg.training_memory_mb, "cpu_threads": 2, "gpu_memory_mb": 0},
            settings={"capital": cfg.sizing_capital(equity), "capital_fraction": cfg.capital_fraction, "capital_cap": cfg.capital,
                "strategy_capital": cfg.sizing_capital(equity) * cfg.strategy_fraction, "position_fraction": cfg.position_fraction, "gross_fraction": cfg.gross_fraction,
                "daytrade_strategies": cfg.daytrade_strategies,
                "daily_loss": cfg.daily_loss, "stop_loss": cfg.effective_stop, "feed": cfg.feed,
                "queue_daily_orders": cfg.queue_daily_orders,
                "retrain_hours": cfg.retrain_hours, "model_max_age_hours": cfg.model_max_age_hours, "validation_bars": 390 if cfg.intraday else cfg.validation_bars,
                "universe_mode": cfg.universe_mode, "scan_candidates": cfg.scan_candidates,
                "poll_seconds": config.poll_seconds, "daytrade_max_positions": cfg.daytrade_max_positions,
                "daytrade_hold_minutes": cfg.daytrade_hold_minutes, "daytrade_close_minutes": cfg.daytrade_close_minutes,
                "cost_bps": cfg.cost_bps, "llm_url": cfg.llm_url, "min_votes": cfg.min_votes,
                "llm_models": cfg.llm_models, "llm_configured": bool(cfg.llm_key)})

    @app.post("/api/control")
    def control():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Expected a JSON object"), 400
        action = body.get("action")
        selected = body.get("strategy", "all")
        if not isinstance(selected, str) or selected not in {"all", *contexts}:
            return jsonify(error="Unknown strategy"), 400
        targets = list(contexts.values()) if selected == "all" else [contexts[selected]]
        if action == "pause":
            for _, state in targets:
                state.set("entries_enabled", False)
        elif action == "resume":
            if not config.configured:
                return jsonify(error="Configure Alpaca keys first"), 409
            if not store.get("account"):
                return jsonify(error="Wait for a successful Alpaca account connection"), 409
            if store.get("account", {}).get("loss_halt"):
                return jsonify(error="Daily loss halt is active"), 409
            if selected in ("loop",) and contexts[selected][1].get("halt"):
                return jsonify(error="Drawdown-Stopp aus risiko.py aktiv. Erst im Trading-Loop den Höchststand zurücksetzen."), 409
            for cfg, state in targets:
                # A drawdown-halted trading-loop stays paused when enabling all bots.
                if not (cfg.loop and state.get("halt")):
                    state.set("entries_enabled", True)
        elif action == "train":
            if not config.configured:
                return jsonify(error="Alpaca credentials are required for training data"), 409
            for _, state in targets:
                state.set("train_requested", True)
        else:
            return jsonify(error="Unknown action"), 400
        store.event("control", {"action": action, "strategy": selected})
        return jsonify(ok=True)

    def body_or_error():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else None

    @app.post("/api/allocation")
    def set_allocation():
        body = body_or_error()
        if not parallel:
            return jsonify(error="Budget und Gewichtung gibt es nur mit TRADING_MODE=both"), 409
        if body is None:
            return jsonify(error="Expected a JSON object"), 400
        try:
            cleaned = validate(body)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        store.set("allocation", {**cleaned, "at": utcnow()})
        store.event("control", {"action": "allocation", **cleaned})
        return jsonify(ok=True, allocation=allocation(config, store))

    @app.post("/api/loop")
    def loop_control():
        body = body_or_error()
        if "loop" not in contexts:
            return jsonify(error="Der Trading-Loop läuft nur mit TRADING_MODE=both"), 409
        if body is None:
            return jsonify(error="Expected a JSON object"), 400
        cfg, state = contexts["loop"]
        action, version = body.get("action"), body.get("version")
        bot = "loop"
        if action == "activate":
            path = loop.strategy_path(cfg, version)
            if not path:
                return jsonify(error="Diese Strategie-Version gibt es nicht"), 404
            info = loop.version_info(cfg, path)
            if not info["tradable"]:
                return jsonify(error=f"{version} kann nicht aktiviert werden: {info['note'] if info['status'] != 'bereit' else loop.risk_rules(cfg)['message']}"), 409
            bot = info["bot"]
            state = contexts[bot][1]
            active = loop.active_versions(state)
            if version not in active and len(active) >= MAX_LOOP_STRATEGIES:
                return jsonify(error=f"Höchstens {MAX_LOOP_STRATEGIES} Strategien gleichzeitig"), 409
            active[version] = {"version": version, "symbol": info["symbol"], "sha256": info["sha256"], "activated_at": utcnow()}
            target = "die Aktienliste der Bots plus externe Aktien" if info["symbol"] == loop.LIST else info["symbol"]
            message = f"{version} prüft {target} im Paper-Modus. Pro Aktie entscheidet die beste grüne aktive Version."
        elif action == "deactivate":
            bot = next((mode for mode, _, mode_state in loop_bots() if version in loop.active_versions(mode_state)), None)
            if not bot:
                return jsonify(error="Diese Version ist nicht aktiv"), 404
            state = contexts[bot][1]
            active = loop.active_versions(state)
            del active[version]
            message = f"{version} abgeschaltet; ihre offenen Positionen werden geschlossen"
        elif action == "reset_peak":
            for _, _, mode_state in loop_bots():
                mode_state.set("halt", None)
                mode_state.set("equity_peak", None)
            store.event("control", {"action": "loop_reset_peak", "strategy": "loop"})
            return jsonify(ok=True, message="Höchststand zurückgesetzt. Einstiege bleiben pausiert, bis du sie einschaltest.")
        elif action == "retest":
            if version is not None and not loop.strategy_path(cfg, version):
                return jsonify(error="Diese Strategie-Version gibt es nicht"), 404
            if state.get("loop_retest_requested") or (state.get("loop_retest") or {}).get("status") == "running":
                return jsonify(error="Es läuft schon ein Stresstest. Warte, bis er fertig ist."), 409
            if not loop.retest_targets(cfg, version, include_changed=True):
                return jsonify(error="Keine getestete Version zum Neutesten"), 409
            state.set("loop_retest_requested", {"version": version, "at": utcnow()})
            store.event("control", {"action": "loop_retest", "strategy": "loop", "version": version})
            return jsonify(ok=True, message=f"{version or 'Alle getesteten Versionen'}: Stresstest startet. Das dauert ein paar Minuten.")
        else:
            return jsonify(error="Unknown action"), 400
        state.set("loop_strategies", active)
        store.event("control", {"action": f"loop_{action}", "strategy": bot, "version": version})
        return jsonify(ok=True, message=message)


    return app
