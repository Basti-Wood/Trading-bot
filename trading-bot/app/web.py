import hmac
from pathlib import Path
import psutil
from flask import Flask, jsonify, request, send_from_directory
from .store import utcnow
from .feedback import summary_key


def create_app(config, store):
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
            if origin and origin != request.host_url.rstrip("/"):
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
        process = psutil.Process()
        rss = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        return jsonify(
            at=utcnow(), mode="paper" if config.paper else "live", configured=config.configured,
            entries_enabled=store.get("entries_enabled", False), engine=store.get("engine", {"status": "starting"}),
            account=store.get("account"), training=store.get("training", {"status": "waiting for data"}),
            research_status=store.get("research_status", {"status": "waiting"}),
            symbols=[{"symbol": s, "forecast": store.get(f"forecast:{s}"), "model": store.get(f"model:{s}"),
                      "candidate": store.get(f"candidate:{s}"), "research": store.get(f"research:{s}"),
                      "feedback": store.get(summary_key(config, s), {"captured": 0, "pending": 0, "resolved": 0, "excluded": 0, "scores": {}, "latest": []})} for s in config.symbols],
            orders=store.orders(50), events=store.history("decision", 30), errors=store.history("error", 10),
            training_history=store.history("training", 25), equity=store.history("equity", 100),
            resources={"process_memory_mb": round(rss / 1024**2, 1), "host_cpu_percent": psutil.cpu_percent(),
                       "training_memory_mb": config.training_memory_mb, "cpu_threads": 2, "gpu_memory_mb": 0},
            settings={"capital": config.capital, "position_fraction": config.position_fraction, "gross_fraction": config.gross_fraction,
                "daily_loss": config.daily_loss, "stop_loss": config.stop_loss, "feed": config.feed,
                "retrain_hours": config.retrain_hours, "validation_bars": config.validation_bars,
                "cost_bps": config.cost_bps, "llm_url": config.llm_url,
                "llm_models": config.llm_models, "llm_configured": bool(config.llm_key), "web_search": bool(config.brave_key)})

    @app.post("/api/control")
    def control():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Expected a JSON object"), 400
        action = body.get("action")
        if action == "pause":
            store.set("entries_enabled", False)
        elif action == "resume":
            if not config.configured or not config.llm_key:
                return jsonify(error="Configure Alpaca and FreeLLM keys first"), 409
            if not store.get("account"):
                return jsonify(error="Wait for a successful Alpaca account connection"), 409
            if store.get("account", {}).get("loss_halt"):
                return jsonify(error="Daily loss halt is active"), 409
            store.set("entries_enabled", True)
        elif action == "train":
            if not config.configured:
                return jsonify(error="Alpaca credentials are required for training data"), 409
            store.set("train_requested", True)
        else:
            return jsonify(error="Unknown action"), 400
        store.event("control", {"action": action})
        return jsonify(ok=True)

    return app
