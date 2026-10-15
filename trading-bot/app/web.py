import hmac
from pathlib import Path
import psutil
from flask import Flask, jsonify, request, send_from_directory
from .store import utcnow
from .feedback import summary_key
from .universe import active_symbols
from .portfolio import strategy_contexts
from .decisions import position_context


def create_app(config, store):
    contexts = {cfg.trading_mode: (cfg, state) for cfg, state in strategy_contexts(config, store)}
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
        if account and config.trading_mode == "both":
            account = {**account, "positions": [{**p, "strategy": store.owner(p["symbol"]) or "external"} for p in account.get("positions", [])]}
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
            strategies=[{"id": c.trading_mode, "entries_enabled": st.get("entries_enabled", False),
                         "capital_fraction": c.strategy_fraction, "capital_ceiling": c.capital * c.strategy_fraction,
                         "engine": st.get("engine", {"status": "starting"}),
                         "training": st.get("training", {"status": "waiting"})} for c, st in contexts.values()],
            any_entries_enabled=any(st.get("entries_enabled", False) for _, st in contexts.values()),
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
            settings={"capital": cfg.capital, "strategy_capital": cfg.capital * cfg.strategy_fraction, "position_fraction": cfg.position_fraction, "gross_fraction": cfg.gross_fraction,
                "daily_loss": cfg.daily_loss, "stop_loss": cfg.effective_stop, "feed": cfg.feed,
                "retrain_hours": cfg.retrain_hours, "validation_bars": 390 if cfg.intraday else cfg.validation_bars,
                "universe_mode": cfg.universe_mode, "scan_candidates": cfg.scan_candidates,
                "poll_seconds": config.poll_seconds, "daytrade_max_positions": cfg.daytrade_max_positions,
                "daytrade_hold_minutes": cfg.daytrade_hold_minutes, "daytrade_close_minutes": cfg.daytrade_close_minutes,
                "cost_bps": cfg.cost_bps, "llm_url": cfg.llm_url,
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
            if not config.configured or not config.llm_key:
                return jsonify(error="Configure Alpaca and FreeLLM keys first"), 409
            if not store.get("account"):
                return jsonify(error="Wait for a successful Alpaca account connection"), 409
            if store.get("account", {}).get("loss_halt"):
                return jsonify(error="Daily loss halt is active"), 409
            for _, state in targets:
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

    return app
