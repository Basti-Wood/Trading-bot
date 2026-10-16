import hmac
from pathlib import Path
import psutil
from flask import Flask, jsonify, request, send_from_directory
from . import loop, manual
from .allocation import BOTS, NAMES, allocation, bot_capital, bot_share, validate
from .broker import Alpaca, BrokerError
from .store import utcnow
from .feedback import summary_key
from .universe import active_symbols
from .portfolio import strategy_contexts
from .decisions import position_context

MAX_LOOP_STRATEGIES = 10


def create_app(config, store, broker_factory=Alpaca):
    contexts = {cfg.trading_mode: (cfg, state) for cfg, state in strategy_contexts(config, store)}
    parallel = config.trading_mode == "both"

    def loop_state(equity):
        if "loop" not in contexts:
            return None
        cfg, state = contexts["loop"]
        active = loop.active_versions(state)
        versions = loop.catalogue(cfg)
        for version in versions:
            version["active"] = version["version"] in active
        running = []
        for name, record in sorted(active.items()):
            results = state.get(f"loop_results:{name}") or {}
            fresh = results.get("sha256") == record["sha256"]
            rows = []
            for symbol in loop.version_symbols(cfg, state, record):
                item = (results.get("symbols") or {}).get(symbol) if fresh else None
                decision = state.get(f"forecast:{symbol}") or {}
                rows.append({"symbol": symbol, "result": item, "action": decision.get("action"),
                             "reason": decision.get("reason"), "position": decision.get("position"),
                             "own": store.owner(symbol) in (None, "manual") and bool((decision.get("position") or {}).get("qty"))})
            running.append({"version": name, "symbol": record["symbol"], "activated_at": record.get("activated_at"),
                            "computed_at": results.get("at") if fresh else None, "error": state.get(f"loop_error:{name}"),
                            "green": sum(1 for r in rows if ((r["result"] or {}).get("test") or {}).get("gruen")), "rows": rows})
        peak = state.get("equity_peak")
        return {"available": loop.available(cfg), "folder": str(cfg.loop_dir or ""), "rules": loop.risk_rules(cfg),
                "versions": versions, "running": running, "errors": loop.errors(cfg), "halt": state.get("halt"),
                "peak": peak, "drawdown": (peak - equity) / peak * 100 if peak and equity else None,
                "entries_enabled": state.get("entries_enabled", False)}

    def manual_state(account):
        if not parallel or not account:
            return None
        open_orders = store.get("open_orders", [])
        positions = []
        for p in account.get("positions", []):
            owner = store.owner(p["symbol"])
            if owner in BOTS:
                continue
            positions.append({**p, "owner": owner or "external",
                              "outlook": manual.outlook(config, contexts, p, open_orders),
                              "orders": [o for o in open_orders if o["symbol"] == p["symbol"]]})
        return {"positions": positions, "orders": [o for o in open_orders if store.owner(o["symbol"]) not in BOTS],
                "cash": account.get("cash"), "history": store.history("manual", 15)}
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
                            "model": state_store.get(f"model:{symbol}"), "candidate": state_store.get(f"candidate:{symbol}"),
                            "research": state_store.get(f"research:{symbol}"),
                            "feedback": state_store.get(summary_key(cfg, symbol), {"captured": 0, "pending": 0, "resolved": 0, "excluded": 0, "scores": {}, "latest": []})})
        process = psutil.Process()
        rss = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
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
            loop=loop_state(equity), manual=manual_state(account),
            at=utcnow(), mode="paper" if cfg.paper else "live", trading_mode=cfg.trading_mode, configured=cfg.configured,
            universe=state_store.get("universe", {"symbols": active_symbols(cfg, state_store), "candidates": []}),
            scan_status=state_store.get("scan_status", {"status": "waiting" if cfg.universe_mode == "market" else "watchlist"}),
            entries_enabled=state_store.get("entries_enabled", False), engine=state_store.get("engine", {"status": "starting"}),
            account=account, training=state_store.get("training", {"status": "waiting for data"}),
            research_status=state_store.get("research_status", {"status": "waiting"}),
            symbols=symbols,
            orders=store.orders(50), events=state_store.history("decision", 30), errors=state_store.history("error", 10),
            training_history=state_store.history("training", 25), equity=store.history("equity", 100),
            resources={"process_memory_mb": round(rss / 1024**2, 1), "host_cpu_percent": psutil.cpu_percent(),
                       "training_memory_mb": cfg.training_memory_mb, "cpu_threads": 2, "gpu_memory_mb": 0},
            settings={"capital": cfg.sizing_capital(equity), "capital_fraction": cfg.capital_fraction, "capital_cap": cfg.capital,
                "strategy_capital": cfg.sizing_capital(equity) * cfg.strategy_fraction, "position_fraction": cfg.position_fraction, "gross_fraction": cfg.gross_fraction,
                "daytrade_strategies": cfg.daytrade_strategies,
                "daily_loss": cfg.daily_loss, "stop_loss": cfg.effective_stop, "feed": cfg.feed,
                "queue_daily_orders": cfg.queue_daily_orders,
                "retrain_hours": cfg.retrain_hours, "validation_bars": 390 if cfg.intraday else cfg.validation_bars,
                "universe_mode": cfg.universe_mode, "scan_candidates": cfg.scan_candidates,
                "poll_seconds": config.poll_seconds, "daytrade_max_positions": cfg.daytrade_max_positions,
                "daytrade_hold_minutes": cfg.daytrade_hold_minutes, "daytrade_close_minutes": cfg.daytrade_close_minutes,
                "cost_bps": cfg.cost_bps, "llm_url": cfg.llm_url, "min_votes": cfg.min_votes,
                "llm_models": cfg.llm_models, "llm_configured": bool(cfg.llm_key), "web_search": bool(cfg.brave_key)})

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
            if selected == "loop" and contexts["loop"][1].get("halt"):
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
        active = loop.active_versions(state)
        if action == "activate":
            path = loop.strategy_path(cfg, version)
            if not path:
                return jsonify(error="Diese Strategie-Version gibt es nicht"), 404
            info = loop.version_info(cfg, path)
            if not info["tradable"]:
                return jsonify(error=f"{version} kann nicht aktiviert werden: {info['note'] if info['status'] != 'gruen' else loop.risk_rules(cfg)['message']}"), 409
            # A newer version of the same idea replaces the older one and takes over its positions.
            replaced = [v for v in active if loop.idea(v) == info["idea"] and v != version]
            for old in replaced:
                del active[old]
            if version not in active and len(active) >= MAX_LOOP_STRATEGIES:
                return jsonify(error=f"Höchstens {MAX_LOOP_STRATEGIES} Strategien gleichzeitig"), 409
            active[version] = {"version": version, "symbol": info["symbol"], "sha256": info["sha256"], "activated_at": utcnow()}
            target = "die Aktienliste der Bots plus deine eigenen Aktien" if info["symbol"] == loop.LIST else info["symbol"]
            message = f"{version} handelt {target} im Paper-Modus" + (f" (ersetzt {', '.join(replaced)})" if replaced else "")
            if info["symbol"] == loop.LIST:
                message += ". Jede Aktie wird vor dem Kauf einzeln getestet; deine eigenen Aktien bekommen nur Signale"
            else:
                owner = store.owner(info["symbol"])
                if owner not in (None, "loop"):
                    message += f". {info['symbol']} gehört gerade dem {NAMES.get(owner, owner)}; gekauft wird erst, wenn die Aktie frei ist"
        elif action == "deactivate":
            if version not in active:
                return jsonify(error="Diese Version ist nicht aktiv"), 404
            del active[version]
            message = f"{version} abgeschaltet; ihre offenen Positionen werden geschlossen"
        elif action == "reset_peak":
            state.set("halt", None)
            state.set("equity_peak", None)
            store.event("control", {"action": "loop_reset_peak", "strategy": "loop"})
            return jsonify(ok=True, message="Höchststand zurückgesetzt. Einstiege bleiben pausiert, bis du sie einschaltest.")
        else:
            return jsonify(error="Unknown action"), 400
        state.set("loop_strategies", active)
        store.event("control", {"action": f"loop_{action}", "strategy": "loop", "version": version})
        return jsonify(ok=True, message=message)

    @app.post("/api/manual/order")
    def manual_order():
        body = body_or_error()
        if not parallel:
            return jsonify(error="Selber traden gibt es nur mit TRADING_MODE=both"), 409
        if not config.configured:
            return jsonify(error="Configure Alpaca keys first"), 409
        if body is None:
            return jsonify(error="Expected a JSON object"), 400
        try:
            return jsonify(ok=True, message=manual.place(config, store, broker_factory(config), body))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except BrokerError as exc:
            return jsonify(error=f"Alpaca nicht erreichbar oder Symbol unbekannt ({exc})"), 502

    @app.post("/api/manual/cancel")
    def manual_cancel():
        body = body_or_error()
        if not parallel or not config.configured:
            return jsonify(error="Selber traden ist nicht eingerichtet"), 409
        if body is None:
            return jsonify(error="Expected a JSON object"), 400
        try:
            return jsonify(ok=True, message=manual.cancel(store, broker_factory(config), body.get("order_id")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except BrokerError as exc:
            return jsonify(error=f"Alpaca nicht erreichbar ({exc})"), 502

    return app
