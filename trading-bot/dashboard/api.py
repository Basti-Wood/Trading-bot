"""
dashboard/api.py — Lightweight JSON API for the Trading Bot Dashboard

Replaces the Flask monolith. Serves JSON endpoints that the static
index.html dashboard calls via fetch(). Runs under supervisord alongside Nginx.
No HTML rendering, no auth — Nginx handles auth_basic if needed.
"""

import json
import csv
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, "/app")


ASSET_CLASSES = ["crypto", "stocks", "etf", "politician", "funds"]

# ---------------------------------------------------------------------------
# Data helpers — read from shared Docker volumes
# ---------------------------------------------------------------------------

def _read_json(path: str):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_csv(path: str) -> list[dict]:
    try:
        with open(path, "r", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, default=str).encode("utf-8")


# ---------------------------------------------------------------------------
# Route handlers — each returns (status, body_bytes, content_type)
# ---------------------------------------------------------------------------

def handle_status() -> tuple:
    """GET /api/status — all bots' champion/challenger summary."""
    status = {}
    for asset in ASSET_CLASSES:
        champion = _read_json(f"champions/{asset}_champion.json")
        challenger = _read_json(f"challengers/{asset}_challenger.json")
        status[asset] = {
            "champion_active": champion is not None,
            "champion_sharpe": champion.get("last_sharpe", 0) if champion else 0,
            "champion_temp": champion.get("temperature", 0) if champion else 0,
            "champion_short": champion.get("short_enabled", False) if champion else False,
            "challenger_ready": challenger is not None,
            "challenger_sharpe": challenger.get("last_sharpe", 0) if challenger else 0,
            "challenger_temp": challenger.get("temperature", 0) if challenger else 0,
            "challenger_short": challenger.get("short_enabled", False) if challenger else False,
            "sharpe_delta": (
                round(challenger.get("last_sharpe", 0) - champion.get("last_sharpe", 0), 3)
                if champion and challenger else 0.0
            ),
            "generation": champion.get("generation", 0) if champion else 0,
        }
    return 200, _json_bytes(status), "application/json"


def handle_champions() -> tuple:
    """GET /api/champions"""
    champs = {}
    for asset in ASSET_CLASSES:
        champs[asset] = _read_json(f"champions/{asset}_champion.json")
    return 200, _json_bytes(champs), "application/json"


def handle_challengers() -> tuple:
    """GET /api/challengers"""
    challs = {}
    for asset in ASSET_CLASSES:
        challs[asset] = _read_json(f"challengers/{asset}_challenger.json")
    return 200, _json_bytes(challs), "application/json"


def handle_leaderboard() -> tuple:
    """GET /api/leaderboard"""
    entries = []
    for asset in ASSET_CLASSES:
        champion = _read_json(f"champions/{asset}_champion.json")
        challenger = _read_json(f"challengers/{asset}_challenger.json")
        if champion:
            entries.append({
                "asset_class": asset, "role": "👑 Champion",
                "sharpe": champion.get("last_sharpe", 0),
                "temperature": champion.get("temperature", 0),
                "hidden_size": champion.get("hidden_size", 0),
                "num_layers": champion.get("num_layers", 0),
                "seq_len": champion.get("seq_len", 0),
                "short_enabled": champion.get("short_enabled", False),
                "generation": champion.get("generation", 0),
                "is_champion": True,
            })
        if challenger:
            entries.append({
                "asset_class": asset, "role": "🥊 Challenger",
                "sharpe": challenger.get("last_sharpe", 0),
                "temperature": challenger.get("temperature", 0),
                "hidden_size": challenger.get("hidden_size", 0),
                "num_layers": challenger.get("num_layers", 0),
                "seq_len": challenger.get("seq_len", 0),
                "short_enabled": challenger.get("short_enabled", False),
                "generation": challenger.get("generation", 0),
                "is_champion": False,
            })
    entries.sort(key=lambda e: e["sharpe"], reverse=True)
    for i, entry in enumerate(entries):
        entry["rank"] = i + 1
    return 200, _json_bytes(entries), "application/json"


def handle_evolution(asset_class: str) -> tuple:
    """GET /api/evolution/<asset_class>"""
    if asset_class not in ASSET_CLASSES:
        return 404, _json_bytes({"error": f"Unknown: {asset_class}"}), "application/json"
    rows = _read_csv(f"logs/evolution_log_{asset_class}.csv")
    return 200, _json_bytes(rows[-100:] if len(rows) > 100 else rows), "application/json"


def handle_ledger() -> tuple:
    """GET /api/ledger"""
    rows = _read_csv("logs/ledger.csv")
    return 200, _json_bytes(rows[-50:] if len(rows) > 50 else rows), "application/json"


def handle_trades(asset_class: str) -> tuple:
    """GET /api/trades/<asset_class>"""
    if asset_class not in ASSET_CLASSES:
        return 404, _json_bytes({"error": f"Unknown: {asset_class}"}), "application/json"
    trades = _read_csv(f"logs/trades_{asset_class}.csv")
    return 200, _json_bytes({"trades": trades, "prices": {}}), "application/json"


def handle_controls_get() -> tuple:
    """GET /api/controls"""
    try:
        from shared.monitoring import controls as ctl
        return 200, _json_bytes(ctl.load_controls()), "application/json"
    except Exception:
        controls_path = os.environ.get("CONTROLS_PATH",
                        os.path.join(os.environ.get("LOGS_DIR", "logs"), "controls.json"))
        data = _read_json(controls_path)
        if data:
            return 200, _json_bytes(data), "application/json"
        return 200, _json_bytes({"global_trading": True, "global_shorting": False,
                "per_asset": {a: {"trading_enabled": True, "shorting_enabled": False,
                "strategies": {"ml": True, "fallback": True, "politician": True},
                "auto_promote": True} for a in ASSET_CLASSES}}), "application/json"


def handle_controls_set(body: dict) -> tuple:
    """POST /api/controls"""
    try:
        from shared.monitoring import controls as ctl
        path = body.get("path")
        value = body.get("value")
        if not isinstance(path, list) or not path:
            return 400, _json_bytes({"error": "invalid path"}), "application/json"
        updated = ctl.set_control(path, value)
        return 200, _json_bytes(updated), "application/json"
    except Exception as e:
        return 500, _json_bytes({"error": str(e)}), "application/json"


def handle_promote(asset_class: str) -> tuple:
    """POST /api/promote/<asset_class>"""
    if asset_class not in ASSET_CLASSES:
        return 404, _json_bytes({"error": f"Unknown: {asset_class}"}), "application/json"
    try:
        from shared.monitoring.circuit_breaker import promote_challenger
        ok = promote_challenger(asset_class)
        return 200, _json_bytes({"promoted": ok, "asset_class": asset_class}), "application/json"
    except Exception as e:
        return 500, _json_bytes({"error": str(e)}), "application/json"


def handle_prediction(asset_class: str) -> tuple:
    """GET /api/prediction/<asset_class> — last model prediction state."""
    if asset_class not in ASSET_CLASSES:
        return 404, _json_bytes({"error": f"Unknown: {asset_class}"}), "application/json"
    pred = _read_json(f"logs/predictions_{asset_class}.json")
    if not pred:
        return 200, _json_bytes({"empty": True, "asset_class": asset_class}), "application/json"
    return 200, _json_bytes(pred), "application/json"


# ---------------------------------------------------------------------------
# Network architecture by asset class
# ---------------------------------------------------------------------------

# Feature counts per asset class (from shared/data/preprocessor.py)
FEATURE_COUNTS = {
    "crypto":     7,   # BASE_FEATURES
    "stocks":    10,   # BASE_FEATURES + 3 politician features
    "etf":        7,
    "politician": 7,
    "funds":      7,
}

# Feature names for tooltips
FEATURE_NAMES = [
    "return_1", "return_5", "return_20",
    "rsi", "volatility", "ma_cross", "vol_ratio",
    "pol_net_sentiment", "pol_signal_age", "pol_buy_count",
]


def handle_network(asset_class: str) -> tuple:
    """
    GET /api/network/<asset_class>

    Returns the full LSTM network topology for both champion and challenger
    so the frontend can render an SVG architecture diagram from real DNA.
    """
    if asset_class not in ASSET_CLASSES:
        return 404, _json_bytes({"error": f"Unknown: {asset_class}"}), "application/json"

    n_features = FEATURE_COUNTS.get(asset_class, 7)
    feature_names = FEATURE_NAMES[:n_features]

    result = {
        "asset_class": asset_class,
        "n_features": n_features,
        "feature_names": feature_names,
        "champion": _build_network_dict(asset_class, "champion", n_features),
        "challenger": _build_network_dict(asset_class, "challenger", n_features),
    }
    return 200, _json_bytes(result), "application/json"


def _build_network_dict(asset_class: str, role: str, n_features: int) -> dict | None:
    """Read champion/challenger DNA and build the network topology dict."""
    dna = _read_json(f"{role}s/{asset_class}_{role}.json")
    if not dna:
        return None

    hidden = dna.get("hidden_size", 128)
    layers = dna.get("num_layers", 2)
    seq = dna.get("seq_len", 30)
    dropout = dna.get("dropout", 0.2)

    # Architecture: LSTM(input=n_features, hidden=hidden, layers) -> Linear(hidden→64) -> ReLU -> Dropout -> Linear(64→2)
    return {
        "hidden_size": hidden,
        "num_layers": layers,
        "seq_len": seq,
        "dropout": dropout,
        "sharpe": dna.get("last_sharpe", 0),
        "generation": dna.get("generation", 0),
        "temperature": dna.get("temperature", 0),
        # Layer-by-layer topology for rendering
        "topology": [
            {"type": "input",    "size": n_features, "label": f"Features ({n_features})"},
            *[{"type": "lstm",   "size": hidden,      "label": f"LSTM Layer {i+1}",
               "hidden": hidden, "seq_len": seq} for i in range(layers)],
            {"type": "linear",   "size": 64,          "label": "Dense (hidden→64)"},
            {"type": "relu",     "size": 64,          "label": "ReLU"},
            {"type": "dropout",  "size": 64,          "label": f"Dropout({dropout})"},
            {"type": "linear",   "size": 2,           "label": "Output (64→2)"},
        ],
    }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

ROUTES = [
    ("/api/status",         "GET",  lambda _m, _b: handle_status()),
    ("/api/champions",      "GET",  lambda _m, _b: handle_champions()),
    ("/api/challengers",    "GET",  lambda _m, _b: handle_challengers()),
    ("/api/leaderboard",    "GET",  lambda _m, _b: handle_leaderboard()),
    ("/api/ledger",         "GET",  lambda _m, _b: handle_ledger()),
    ("/api/controls",       "GET",  lambda _m, _b: handle_controls_get()),
    ("/api/controls",       "POST", lambda _m, b: handle_controls_set(b)),
    ("/api/health",         "GET",  lambda _m, _b: (200, b'{"status":"ok"}', "application/json")),
]


class APIHandler(BaseHTTPRequestHandler):

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        path = urlparse(self.path).path

        # Exact-match routes
        for route_path, method, handler in ROUTES:
            if method != "GET":
                continue
            if path == route_path:
                status, body, ct = handler("GET", None)
                self._send(status, body, ct)
                return

        # Parameterized routes
        for prefix, handler_fn in [
            ("/api/evolution/",   handle_evolution),
            ("/api/trades/",      handle_trades),
            ("/api/network/",     handle_network),
            ("/api/prediction/",  handle_prediction),
        ]:
            if path.startswith(prefix):
                asset = path[len(prefix):]
                status, body, ct = handler_fn(asset)
                self._send(status, body, ct)
                return

        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        for route_path, method, handler in ROUTES:
            if method != "POST":
                continue
            if path == route_path:
                status, resp_body, ct = handler("POST", body)
                self._send(status, resp_body, ct)
                return

        # POST /api/promote/<asset_class>
        if path.startswith("/api/promote/"):
            asset = path[len("/api/promote/"):]
            status, resp_body, ct = handle_promote(asset)
            self._send(status, resp_body, ct)
            return

        self._send(404, b'{"error":"not found"}')

    def do_OPTIONS(self):
        self._send(200, b"{}")

    def log_message(self, format, *args):
        pass  # silence request logs — Nginx handles access logging


def main():
    port = int(os.environ.get("API_PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), APIHandler)
    print(f"[api] Listening on 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
