"""
dashboard/dashboard.py — Unified Monitoring Dashboard

Flask-based web UI at http://localhost:8080
Shows all bots' status, champion DNA, challenger comparison,
circuit breaker state, evolution progress, and ledger history.

Access Control:
  - Password-protected with a persistent cookie (30-day expiry).
  - Set DASHBOARD_PASSWORD in .env. No username — just the password.
  - Cookie keeps you logged in; use /logout to clear it.
"""

from flask import Flask, request, abort, jsonify, make_response, redirect, url_for
from functools import wraps
import os
import json
import hashlib
import secrets
import csv
import sys

sys.path.insert(0, "/app")

# ---------------------------------------------------------------------------
# App & config
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Secret key for signing cookies — generated on first start, persisted in env
app.secret_key = os.getenv(
    "DASHBOARD_SECRET",
    secrets.token_hex(32),
)

# The one password (set in .env)
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
# Store a SHA-256 hash for comparison (never store plaintext in memory longer than needed)
_PASSWORD_HASH = hashlib.sha256(DASHBOARD_PASSWORD.encode()).hexdigest() if DASHBOARD_PASSWORD else None

COOKIE_NAME = "dashboard_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _make_token() -> str:
    """Create a signed login token (HMAC of password hash + secret)."""
    payload = f"{_PASSWORD_HASH}:{app.secret_key}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _check_token(token: str) -> bool:
    """Validate a login token."""
    if not _PASSWORD_HASH:
        return False
    return secrets.compare_digest(token, _make_token())


def require_auth(f):
    """
    Decorator: redirect to /login if not authenticated.
    Checks for a valid dashboard_token cookie.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # If no password is configured, allow all (dev mode)
        if _PASSWORD_HASH is None:
            return f(*args, **kwargs)

        token = request.cookies.get(COOKIE_NAME)
        if token and _check_token(token):
            return f(*args, **kwargs)

        # For API routes, return 401 JSON instead of redirect
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized — log in at /login"}), 401

        return redirect(url_for("login", next=request.path))

    return decorated


# ---------------------------------------------------------------------------
# Data helpers — read from shared volumes
# ---------------------------------------------------------------------------

ASSET_CLASSES = ["crypto", "stocks", "etf", "politician", "funds"]


def _read_json(path: str) -> dict | None:
    """Safely read a JSON file. Returns None if missing or malformed."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_csv(path: str) -> list[dict]:
    """Safely read a CSV file into a list of dicts."""
    try:
        with open(path, "r", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """Password-only login page. Sets a persistent cookie on success."""
    error = None
    next_url = request.args.get("next", "/")

    if request.method == "POST":
        pw = request.form.get("password", "")
        pw_hash = hashlib.sha256(pw.encode()).hexdigest()

        if _PASSWORD_HASH and secrets.compare_digest(pw_hash, _PASSWORD_HASH):
            resp = make_response(redirect(next_url))
            resp.set_cookie(
                COOKIE_NAME,
                _make_token(),
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="Lax",
            )
            return resp
        else:
            error = "Wrong password"

    # If already logged in, skip to dashboard
    token = request.cookies.get(COOKIE_NAME)
    if token and _check_token(token):
        return redirect(next_url)

    return _render_login(error)


@app.route("/logout")
def logout():
    """Clear the auth cookie and return to login."""
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# Routes — Web UI
# ---------------------------------------------------------------------------

@app.route("/")
@require_auth
def index():
    """Main dashboard HTML page."""
    return _render_dashboard()


@app.route("/health")
def health():
    """Health-check endpoint — no auth required (for Docker healthcheck)."""
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Routes — JSON API
# ---------------------------------------------------------------------------

@app.route("/api/status")
@require_auth
def api_status():
    """All bots' current status: champion DNA, challenger comparison, Sharpe delta."""
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
    return jsonify(status)


@app.route("/api/champions")
@require_auth
def api_champions():
    """Full champion DNA for each asset class."""
    champions = {}
    for asset in ASSET_CLASSES:
        champions[asset] = _read_json(f"champions/{asset}_champion.json")
    return jsonify(champions)


@app.route("/api/challengers")
@require_auth
def api_challengers():
    """Full challenger DNA for each asset class."""
    challengers = {}
    for asset in ASSET_CLASSES:
        challengers[asset] = _read_json(f"challengers/{asset}_challenger.json")
    return jsonify(challengers)


@app.route("/api/evolution/<asset_class>")
@require_auth
def api_evolution(asset_class: str):
    """Evolution log CSV for an asset class (last 100 rows)."""
    if asset_class not in ASSET_CLASSES:
        return jsonify({"error": f"Unknown asset class: {asset_class}"}), 404

    rows = _read_csv(f"logs/evolution_log_{asset_class}.csv")
    return jsonify(rows[-100:] if len(rows) > 100 else rows)


@app.route("/api/ledger")
@require_auth
def api_ledger():
    """Recent performance ledger entries."""
    rows = _read_csv("logs/ledger.csv")
    return jsonify(rows[-50:] if len(rows) > 50 else rows)


@app.route("/api/compare/<asset_class>")
@require_auth
def api_compare(asset_class: str):
    """Head-to-head champion vs challenger comparison for one asset class."""
    if asset_class not in ASSET_CLASSES:
        return jsonify({"error": f"Unknown asset class: {asset_class}"}), 404

    champion = _read_json(f"champions/{asset_class}_champion.json")
    challenger = _read_json(f"challengers/{asset_class}_challenger.json")

    return jsonify({
        "asset_class": asset_class,
        "champion": champion,
        "challenger": challenger,
        "verdict": _verdict(champion, challenger),
    })


def _verdict(champion: dict | None, challenger: dict | None) -> str:
    """Human-readable verdict on champion vs challenger."""
    if not champion:
        return "No champion yet — run evolver first"
    if not challenger:
        return "No challenger yet — background evolver still working"

    delta = challenger.get("last_sharpe", 0) - champion.get("last_sharpe", 0)
    if delta > 0.2:
        return f"🔺 Challenger BETTER by {delta:.3f} Sharpe — promote recommended"
    elif delta > 0:
        return f"📈 Challenger slightly ahead (+{delta:.3f}). Holding champion for now."
    elif delta > -0.1:
        return f"✅ Champion holding strong ({abs(delta):.3f} ahead of challenger)"
    else:
        return f"🏆 Champion dominant ({abs(delta):.3f} ahead). No contest."


@app.route("/api/leaderboard")
@require_auth
def api_leaderboard():
    """
    Unified leaderboard: all champions + challengers ranked by Sharpe ratio.
    Returns a sorted list of contestants across all asset classes.
    """
    entries = []
    for asset in ASSET_CLASSES:
        champion = _read_json(f"champions/{asset}_champion.json")
        challenger = _read_json(f"challengers/{asset}_challenger.json")

        if champion:
            entries.append({
                "rank": 0,  # filled after sort
                "asset_class": asset,
                "role": "👑 Champion",
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
                "rank": 0,
                "asset_class": asset,
                "role": "🥊 Challenger",
                "sharpe": challenger.get("last_sharpe", 0),
                "temperature": challenger.get("temperature", 0),
                "hidden_size": challenger.get("hidden_size", 0),
                "num_layers": challenger.get("num_layers", 0),
                "seq_len": challenger.get("seq_len", 0),
                "short_enabled": challenger.get("short_enabled", False),
                "generation": challenger.get("generation", 0),
                "is_champion": False,
            })

    # Sort by Sharpe descending
    entries.sort(key=lambda e: e["sharpe"], reverse=True)
    for i, entry in enumerate(entries):
        entry["rank"] = i + 1

    return jsonify(entries)


# ---------------------------------------------------------------------------
# Routes — Strategy Controls (kill switches)
# ---------------------------------------------------------------------------

from shared.monitoring import controls as ctl


@app.route("/api/controls", methods=["GET"])
@require_auth
def api_controls_get():
    """Current control panel state (kill switches, strategy toggles)."""
    return jsonify(ctl.load_controls())


@app.route("/api/controls", methods=["POST"])
@require_auth
def api_controls_set():
    """
    Update one control. Body: {"path": ["per_asset","crypto","shorting_enabled"],
    "value": true}. Shorting has unlimited loss potential — the global and
    per-asset short switches both default to OFF and must be enabled here.
    """
    body = request.get_json(silent=True) or {}
    path = body.get("path")
    value = body.get("value")
    if (not isinstance(path, list) or not path
            or not all(isinstance(p, str) for p in path)):
        return jsonify({"error": "invalid path"}), 400

    # Validate the path exists in the schema (whitelist — no arbitrary keys)
    node = ctl.DEFAULT_CONTROLS
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return jsonify({"error": f"unknown control: {'/'.join(path)}"}), 400
        node = node[key]
    if not isinstance(value, bool):
        return jsonify({"error": "value must be boolean"}), 400

    updated = ctl.set_control(path, value)
    return jsonify(updated)


@app.route("/api/promote/<asset_class>", methods=["POST"])
@require_auth
def api_promote(asset_class: str):
    """Manually promote the current challenger to champion."""
    if asset_class not in ASSET_CLASSES:
        abort(404)
    from shared.monitoring.circuit_breaker import promote_challenger
    ok = promote_challenger(asset_class)
    return jsonify({"promoted": ok, "asset_class": asset_class})


@app.route("/api/prediction/<asset_class>")
@require_auth
def api_prediction(asset_class: str):
    """Last model prediction state for network visualization."""
    if asset_class not in ASSET_CLASSES:
        abort(404)
    pred = _read_json(f"logs/predictions_{asset_class}.json")
    if not pred:
        return jsonify({"empty": True, "asset_class": asset_class})
    return jsonify(pred)


@app.route("/api/trades/<asset_class>")
@require_auth
def api_trades(asset_class: str):
    """
    Return the trade log for an asset class as JSON.
    Includes latest price data for chart context (last 90 days per symbol).
    """
    if asset_class not in ASSET_CLASSES:
        abort(404)
    trades = _read_csv(f"logs/trades_{asset_class}.csv")
    # Attach recent price data for chart context
    price_data = {}
    try:
        from shared.data.fetcher import ASSET_CLASS_CONFIG, get_recent_candles
        symbols = ASSET_CLASS_CONFIG.get(asset_class, {}).get("symbols", [])
        for sym in symbols[:10]:  # limit to avoid timeout
            df = get_recent_candles(sym, days=90)
            if df is not None and not df.empty:
                price_data[sym] = [
                    {"date": str(idx.date()), "close": round(float(row.close), 2)}
                    for idx, row in df.iterrows()
                ]
    except Exception:
        price_data = {}
    return jsonify({"trades": trades, "prices": price_data})


# ---------------------------------------------------------------------------
# HTML templates (inline — no external static files)
# ---------------------------------------------------------------------------

LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot — Login</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9;
          --green:#3fb950; --red:#f85149; --blue:#58a6ff; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; }
  .login-box { background:var(--card); border:1px solid var(--border); border-radius:12px;
               padding:40px; width:100%; max-width:380px; text-align:center; }
  h1 { font-size:1.4rem; margin-bottom:4px; }
  .sub { color:#8b949e; font-size:0.82rem; margin-bottom:28px; }
  input { width:100%; padding:10px 14px; background:var(--bg); color:var(--text);
          border:1px solid var(--border); border-radius:6px; font-size:0.95rem;
          margin-bottom:16px; outline:none; }
  input:focus { border-color:var(--blue); }
  button { width:100%; padding:10px; background:var(--green); color:#000;
           border:none; border-radius:6px; font-size:0.95rem; font-weight:600;
           cursor:pointer; }
  button:hover { opacity:0.9; }
  .error { color:var(--red); font-size:0.82rem; margin-bottom:12px; }
  .hint { margin-top:20px; color:#484f58; font-size:0.72rem; }
</style>
</head>
<body>
<div class="login-box">
  <h1>🤖 Trading Bot</h1>
  <p class="sub">Enter password to access the dashboard</p>
  {error_html}
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Log In</button>
  </form>
  <p class="hint">You'll stay logged in for 30 days · <code>DASHBOARD_PASSWORD</code> in .env</p>
</div>
</body>
</html>"""


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Trading Bot — Dashboard</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9;
          --green:#3fb950; --red:#f85149; --yellow:#d2991d; --blue:#58a6ff;
          --purple:#bc8cff; --cyan:#39d2c0; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         padding:20px; line-height:1.5; }
  h1 { font-size:1.6rem; margin-bottom:4px; }
  h2 { font-size:1.2rem; margin:24px 0 12px; }
  .sub { color:#8b949e; font-size:0.85rem; margin-bottom:8px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; position:relative; }
  .card h3 { font-size:1.05rem; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
  .badge { font-size:0.7rem; padding:2px 8px; border-radius:10px; font-weight:600; }
  .badge-live { background:#1b3a1b; color:var(--green); }
  .badge-off { background:#3a1b1b; color:var(--red); }
  .badge-short { background:#1b2a3a; color:var(--blue); }
  table { width:100%; border-collapse:collapse; font-size:0.82rem; }
  td { padding:4px 8px; border-bottom:1px solid var(--border); }
  td:last-child { text-align:right; font-weight:600; }
  .green{color:var(--green)} .red{color:var(--red)} .yellow{color:var(--yellow)}
  .verdict{ margin-top:12px; padding:8px 12px; border-radius:6px; font-size:0.82rem;
             background:#1b2a1b; border:1px solid #2a4a2a; }
  .loading{text-align:center;padding:40px;color:#8b949e}
  .error{color:var(--red)}
  .bar-wrap{background:var(--border);border-radius:4px;height:8px;margin:4px 0 12px;overflow:hidden}
  .bar-fill{background:var(--green);height:100%;border-radius:4px;transition:width .5s}
  .bar-fill.warn{background:var(--yellow)} .bar-fill.bad{background:var(--red)}
  .nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
  .logout{color:var(--red);font-size:.8rem;text-decoration:none}
  .logout:hover{text-decoration:underline}
  .tabs{display:flex;gap:4px;margin-bottom:20px}
  .tab{padding:6px 16px;border:1px solid var(--border);border-radius:6px 6px 0 0;cursor:pointer;
       background:var(--card);color:var(--text);font-size:.82rem;border-bottom:none}
  .tab.active{background:var(--blue);color:#000;border-color:var(--blue)}
  .tab-content{display:none}
  .tab-content.active{display:block}
  .leaderboard-table{width:100%;border-collapse:collapse;font-size:.84rem}
  .leaderboard-table th{text-align:left;padding:8px 10px;border-bottom:2px solid var(--border);
      color:#8b949e;font-weight:600;font-size:.75rem;text-transform:uppercase}
  .leaderboard-table td{padding:8px 10px;border-bottom:1px solid var(--border)}
  .rank-1{color:#ffd700} .rank-2{color:#c0c0c0} .rank-3{color:#cd7f32}
  .ctl-global{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:14px}
  .ctl-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
  .ctl-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px}
  .ctl-card h3{margin:0 0 8px;font-size:.95rem;text-transform:capitalize}
  .ctl-row{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #21262d}
  .ctl-row:last-child{border-bottom:none}
  .ctl-hint{color:#8b949e;font-size:.75rem;line-height:1.3}
  .switch{position:relative;display:inline-block;width:46px;height:24px;flex:0 0 46px}
  .switch input{opacity:0;width:0;height:0}
  .slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#6e7681;transition:.2s;border-radius:24px}
  .slider:before{position:absolute;content:"";height:18px;width:18px;left:3px;bottom:3px;background:#fff;transition:.2s;border-radius:50%}
  .switch input:checked + .slider{background:var(--green)}
  .switch.danger input:checked + .slider{background:var(--red)}
  .switch input:checked + .slider:before{transform:translateX(22px)}
  .promote-btn{margin-top:10px;width:100%;padding:7px 10px;background:var(--blue);color:#000;border:none;border-radius:6px;font-weight:700;font-size:.78rem;cursor:pointer}
  .promote-btn:hover{opacity:.9}
  .net-btn{display:inline-block;margin-top:8px;padding:4px 12px;background:var(--purple);color:#000;
           border:none;border-radius:4px;font-size:.75rem;font-weight:600;cursor:pointer}
  .net-btn:hover{opacity:.9}
  /* Modal overlay for network viz */
  .modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
      background:rgba(0,0,0,.85);z-index:1000;justify-content:center;align-items:center}
  .modal-overlay.active{display:flex}
  .modal-box{background:var(--card);border:1px solid var(--border);border-radius:12px;
      padding:24px;max-width:900px;width:95%;max-height:90vh;overflow:auto;position:relative}
  .modal-close{position:absolute;top:12px;right:16px;background:none;border:none;
      color:var(--text);font-size:1.4rem;cursor:pointer}
  .modal-title{font-size:1.1rem;margin-bottom:4px}
  .modal-sub{color:#8b949e;font-size:.78rem;margin-bottom:16px}
  .net-legend{display:flex;gap:16px;font-size:.72rem;color:#8b949e;margin-top:8px;flex-wrap:wrap}
  .net-legend span{display:flex;align-items:center;gap:4px}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
  .dot-in{background:var(--cyan)} .dot-lstm{background:var(--purple)}
  .dot-dense{background:var(--yellow)} .dot-out{background:var(--green)}
  footer{margin-top:32px;text-align:center;color:#484f58;font-size:.75rem}
  @keyframes pulse{0%,100%{opacity:.4} 50%{opacity:1}}
  @keyframes flow{0%{stroke-dashoffset:24} 100%{stroke-dashoffset:0}}
  .neuron-pulse{animation:pulse 1.5s ease-in-out infinite}
</style>
</head>
<body>
<div class="nav">
  <div>
    <h1>🤖 Trading Bot Dashboard</h1>
    <p class="sub" style="margin-bottom:0">Auto-refreshes every 30s · <span id="lastUpdate">loading...</span></p>
  </div>
  <a class="logout" href="/logout">Log out</a>
</div>

<div class="tabs">
  <div class="tab active" data-tab="bots" onclick="switchTab('bots')">🤖 Bots</div>
  <div class="tab" data-tab="leaderboard" onclick="switchTab('leaderboard')">🏆 Leaderboard</div>
  <div class="tab" data-tab="trades" onclick="switchTab('trades')">📈 Trades</div>
  <div class="tab" data-tab="controls" onclick="switchTab('controls')">🎛️ Controls</div>
</div>

<div class="tab-content active" id="tab-bots">
  <div class="grid" id="grid"><div class="loading">Loading bot data...</div></div>
</div>

<div class="tab-content" id="tab-leaderboard">
  <h2>🏆 Contender Rankings</h2>
  <p class="sub">All champions & challengers ranked by Sharpe ratio</p>
  <div id="leaderboard"><div class="loading">Loading leaderboard...</div></div>
</div>

<div class="tab-content" id="tab-trades">
  <h2>📈 Trade History</h2>
  <p class="sub">Every buy, sell, short, and close — select an asset class to see where the AI traded.
     <span style="display:inline-block;margin-left:16px">🔵 Buy/Long &nbsp; 🔴 Sell/Short &nbsp; ⚫ Close</span></p>
  <div style="margin-bottom:12px">
    <label for="tradeAsset" style="color:#8b949e;font-size:.82rem">Asset class: </label>
    <select id="tradeAsset" onchange="loadTrades()"
      style="background:var(--card);color:var(--text);border:1px solid var(--border);padding:4px 10px;border-radius:4px">
      <option value="crypto">Crypto</option><option value="stocks">Stocks</option>
      <option value="etf">ETF</option><option value="politician">Politician</option>
      <option value="funds">Funds</option>
    </select>
  </div>
  <div id="trades-chart-container" style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;overflow-x:auto">
    <div id="tradesChart" style="min-width:600px"><div class="loading">Select an asset class above</div></div>
  </div>
  <div id="trades-table" style="margin-top:12px"></div>
</div>

<div class="tab-content" id="tab-controls">
  <h2>🎛️ Strategy Controls</h2>
  <p class="sub">Kill switches &amp; strategy toggles — bots re-read these every cycle.
     ⚠️ Shorting has <b>unlimited</b> loss potential (a stock can rise forever) and is OFF
     by default. A short only opens when the GA's DNA wants it <i>and</i> both switches
     below are on. Disabling shorting also force-closes open shorts next cycle.</p>
  <div id="controls"><div class="loading">Loading controls...</div></div>
</div>

<!-- Network visualization modal -->
<div class="modal-overlay" id="netModal">
  <div class="modal-box">
    <button class="modal-close" onclick="closeNetwork()">&times;</button>
    <div class="modal-title" id="netTitle">🧠 Neural Network</div>
    <div class="modal-sub" id="netSub"></div>
    <svg id="netSvg" width="100%" viewBox="0 0 800 400"></svg>
    <div class="net-legend">
      <span><span class="dot dot-in"></span> Input features</span>
      <span><span class="dot dot-lstm"></span> LSTM cells</span>
      <span><span class="dot dot-dense"></span> Dense (ReLU)</span>
      <span><span class="dot dot-out"></span> Output (UP/DOWN)</span>
      <span style="margin-left:8px">🔵 Animated = data flowing</span>
    </div>
  </div>
</div>

<footer>Password-protected · Cookie valid for 30 days · v2.0</footer>

<script>
// ======================================================================
// State
// ======================================================================
let currentTab = 'bots';
let statusData = {};
let leaderboardData = [];
let champData = {};   // full champion DNA per asset
let netAnimId = null;

// ======================================================================
// Tab switching
// ======================================================================
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${tab}"]`).classList.add('active');
  document.getElementById(`tab-${tab}`).classList.add('active');
  if (tab === 'leaderboard') loadLeaderboard();
  if (tab === 'trades') loadTrades();
  if (tab === 'controls') loadControls();
}

// ======================================================================
// Data loading
// ======================================================================
async function loadAll() {
  try {
    const [sr, cr] = await Promise.all([
      fetch('/api/status'),
      fetch('/api/champions')
    ]);
    if (sr.status === 401) { window.location = '/login'; return; }
    statusData = await sr.json();
    champData = await cr.json();
    renderBots();
    if (currentTab === 'leaderboard') loadLeaderboard();
    if (currentTab === 'controls') loadControls();
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('grid').innerHTML =
      '<div class="loading error">Failed to load — check if bots are running</div>';
  }
}

async function loadLeaderboard() {
  try {
    const r = await fetch('/api/leaderboard');
    leaderboardData = await r.json();
    renderLeaderboard();
  } catch(e) {
    document.getElementById('leaderboard').innerHTML =
      '<div class="loading error">Failed to load leaderboard</div>';
  }
}

// ======================================================================
// Bot cards
// ======================================================================
function renderBots() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  const labels = { crypto:'₿ Crypto', stocks:'📈 Stocks', etf:'📊 ETFs', politician:'🏛️ Politician', funds:'💰 Funds' };

  for (const [asset, s] of Object.entries(statusData)) {
    const hasChamp = s.champion_active;
    const hasChall = s.challenger_ready;
    const sc = s.champion_sharpe >= 1 ? 'green' : s.champion_sharpe >= 0 ? 'yellow' : 'red';
    const delta = s.sharpe_delta;
    const dc = delta > 0.2 ? 'green' : delta > 0 ? 'yellow' : delta > -0.1 ? '' : 'red';
    const ds = delta >= 0 ? '+' : '';
    const bp = Math.min(Math.max((s.champion_sharpe/3)*100,0),100);
    const bc = s.champion_sharpe >= 1.5 ? '' : s.champion_sharpe >= 0.5 ? 'warn' : 'bad';

    grid.innerHTML += `
<div class="card" id="card-${asset}">
  <h3>${labels[asset]||asset}
    <span class="badge ${hasChamp?'badge-live':'badge-off'}">${hasChamp?'LIVE':'OFF'}</span>
    ${s.champion_short?'<span class="badge badge-short">SHORT</span>':''}
  </h3>
  <table>
    <tr><td>Champion Sharpe</td><td class="${sc}">${s.champion_sharpe.toFixed(3)}</td></tr>
    <tr><td>Temperature</td><td>${s.champion_temp.toFixed(2)}</td></tr>
    <tr><td>Generation</td><td>${s.generation}</td></tr>
    <tr><td>Challenger Sharpe</td><td>${hasChall?s.challenger_sharpe.toFixed(3):'—'}</td></tr>
    <tr><td>Challenger Temp</td><td>${hasChall?s.challenger_temp.toFixed(2):'—'}</td></tr>
    <tr><td>Champ vs Chall</td><td class="${dc}">${hasChall?ds+delta.toFixed(3):'—'}</td></tr>
  </table>
  <div class="bar-wrap"><div class="bar-fill ${bc}" style="width:${bp}%"></div></div>
  ${hasChamp&&hasChall?`<div class="verdict">${getVerdict(asset,delta)}</div>`:''}
  ${asset!=='funds'&&asset!=='politician'&&hasChamp?
    `<button class="net-btn" onclick="showNetwork('${asset}')">🧠 View Network</button>`:''}
</div>`;
  }
}

function getVerdict(asset, delta) {
  const n = {crypto:'Crypto',stocks:'Stocks',etf:'ETF',politician:'Politician'};
  if (delta > 0.2) return `🔺 ${n[asset]||asset} challenger is <b>better</b> — consider promoting`;
  if (delta > 0)   return `📈 Challenger slightly ahead. Monitoring...`;
  if (delta > -0.1) return `✅ Champion holding strong. No action needed.`;
  return `🏆 Champion dominant. Challenger far behind.`;
}

// ======================================================================
// Leaderboard
// ======================================================================
function renderLeaderboard() {
  const lb = document.getElementById('leaderboard');
  if (!leaderboardData.length) { lb.innerHTML = '<p style="color:#8b949e">No contenders yet — run the evolver first.</p>'; return; }

  let html = `<table class="leaderboard-table">
    <thead><tr><th>#</th><th>Asset</th><th>Role</th><th>Sharpe</th><th>Temp</th><th>Hidden</th><th>Layers</th><th>Seq</th><th>Short</th><th>Gen</th></tr></thead><tbody>`;

  for (const e of leaderboardData) {
    const rc = e.rank===1?'rank-1':e.rank===2?'rank-2':e.rank===3?'rank-3':'';
    const sc = e.sharpe>=1?'green':e.sharpe>=0?'yellow':'red';
    html += `<tr>
      <td class="${rc}"><b>${e.rank}</b></td>
      <td>${e.asset_class}</td>
      <td>${e.role}</td>
      <td class="${sc}">${e.sharpe.toFixed(3)}</td>
      <td>${e.temperature.toFixed(2)}</td>
      <td>${e.hidden_size}</td>
      <td>${e.num_layers}</td>
      <td>${e.seq_len}</td>
      <td>${e.short_enabled?'✅':'❌'}</td>
      <td>${e.generation}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  lb.innerHTML = html;
}

// ======================================================================
// Trade History with price chart
// ======================================================================
let tradesData = null;

async function loadTrades() {
  const asset = document.getElementById('tradeAsset').value;
  try {
    const r = await fetch(`/api/trades/${asset}`);
    tradesData = await r.json();
    renderTradesChart(asset);
    renderTradesTable();
  } catch(e) {
    document.getElementById('tradesChart').innerHTML =
      '<div class="loading error">Failed to load trades</div>';
  }
}

function renderTradesChart(asset) {
  const container = document.getElementById('tradesChart');
  const td = tradesData;
  if (!td || !td.trades || !td.trades.length) {
    container.innerHTML = '<p style="color:#8b949e;padding:20px;text-align:center">No trades recorded yet for ' + asset + ' — bots will log trades as they run.</p>';
    return;
  }

  const prices = td.prices || {};
  const trades = td.trades;
  // Pick the most-traded symbol for the price line
  const symCount = {};
  trades.forEach(t => { const s = t.symbol; symCount[s] = (symCount[s]||0)+1; });
  const topSym = Object.entries(symCount).sort((a,b)=>b[1]-a[1])[0]?.[0];
  const priceSeries = prices[topSym] || [];

  const w = 760, h = 300, padL = 60, padR = 20, padT = 20, padB = 40;
  const plotW = w - padL - padR, plotH = h - padT - padB;

  // Build SVG
  let svg = `<svg width="100%" viewBox="0 0 ${w} ${h}" style="max-width:100%">
    <rect x="0" y="0" width="${w}" height="${h}" fill="#0d1117" rx="6"/>`;

  // Price line
  if (priceSeries.length > 1) {
    const closes = priceSeries.map(p => p.close);
    const minP = Math.min(...closes) * 0.995, maxP = Math.max(...closes) * 1.005;
    const range = maxP - minP || 1;
    let pathD = '';
    priceSeries.forEach((p, i) => {
      const x = padL + (i / (priceSeries.length - 1)) * plotW;
      const y = padT + plotH - ((p.close - minP) / range) * plotH;
      pathD += (i === 0 ? 'M' : 'L') + `${x.toFixed(1)},${y.toFixed(1)} `;
    });
    svg += `<path d="${pathD}" fill="none" stroke="#58a6ff" stroke-width="1.5" opacity="0.7"/>`;

    // Gridlines
    for (let i = 0; i <= 4; i++) {
      const y = padT + (plotH * i / 4);
      const val = maxP - (range * i / 4);
      svg += `<line x1="${padL}" y1="${y}" x2="${w-padR}" y2="${y}" stroke="#21262d" stroke-width="0.5"/>`;
      svg += `<text x="${padL-6}" y="${y+4}" text-anchor="end" fill="#8b949e" font-size="9">$${val.toFixed(0)}</text>`;
    }

    // Trade markers on price line
    trades.forEach(t => {
      const p = parseFloat(t.price);
      if (!p) return;
      // find closest x on the price line by date
      let idx = priceSeries.findIndex(ps => ps.date >= (t.timestamp||'').slice(0,10));
      if (idx < 0) idx = priceSeries.length - 1;
      const x = padL + (idx / Math.max(1, priceSeries.length - 1)) * plotW;
      const y = padT + plotH - ((p - minP) / range) * plotH;
      const side = (t.side||'').toUpperCase();
      const color = side.includes('LONG') || side === 'BUY' ? '#3fb950'
                  : side.includes('SHORT') || side === 'SELL' ? '#f85149'
                  : side.includes('CLOSE') || side.includes('FORCE') || side.includes('KILL') ? '#8b949e'
                  : '#d2991d';
      const size = side.includes('FORCE') || side.includes('KILL') ? 5 : 4;
      svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${size}" fill="${color}" opacity="0.85">
        <title>${t.side} ${t.symbol} @ $${p} — ${t.timestamp||''}</title></circle>`;
    });
  }

  svg += `<text x="${w/2}" y="${h-8}" text-anchor="middle" fill="#484f58" font-size="9">${topSym||''} price + trades (hover dots for details)</text>`;
  svg += '</svg>';
  container.innerHTML = svg;
}

function renderTradesTable() {
  const el = document.getElementById('trades-table');
  const trades = tradesData?.trades || [];
  if (!trades.length) { el.innerHTML = ''; return; }
  const recent = [...trades].reverse().slice(0, 50);  // last 50
  let html = `<h3 style="font-size:.95rem;margin:12px 0 8px">Recent trades</h3>
    <table class="leaderboard-table"><thead><tr>
      <th>Time</th><th>Symbol</th><th>Side</th><th>Price</th><th>Notional</th><th>Qty</th><th>Conf</th>
    </tr></thead><tbody>`;
  for (const t of recent) {
    const sc = (t.side||'').includes('LONG')||t.side==='BUY'?'green':
               (t.side||'').includes('SHORT')||t.side==='SELL'?'red':'';
    html += `<tr>
      <td style="font-size:.72rem">${(t.timestamp||'').replace('T',' ')}</td>
      <td><b>${t.symbol}</b></td>
      <td class="${sc}">${t.side}</td>
      <td>$${parseFloat(t.price||0).toFixed(2)}</td>
      <td>$${parseFloat(t.notional_usd||0).toFixed(0)}</td>
      <td>${parseFloat(t.qty||0).toFixed(4)}</td>
      <td>${parseFloat(t.confidence||0).toFixed(2)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

// ======================================================================
// Strategy Controls (kill switches)
// ======================================================================
let controlsData = null;

async function loadControls() {
  try {
    const r = await fetch('/api/controls');
    controlsData = await r.json();
    renderControls();
  } catch(e) {
    document.getElementById('controls').innerHTML =
      '<div class="loading error">Failed to load controls</div>';
  }
}

function toggleHtml(path, value, danger=false) {
  const id = 'sw-' + path.join('-');
  return `<label class="switch ${danger?'danger':''}">
    <input type="checkbox" id="${id}" ${value?'checked':''}
      onchange='setControl(${JSON.stringify(path)}, this.checked)'>
    <span class="slider"></span></label>`;
}

async function setControl(path, value) {
  try {
    const r = await fetch('/api/controls', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({path, value})
    });
    controlsData = await r.json();
    renderControls();
  } catch(e) { alert('Failed to update control'); loadControls(); }
}

async function promote(asset) {
  if (!confirm(`Promote the ${asset} challenger to champion?`)) return;
  const r = await fetch(`/api/promote/${asset}`, {method:'POST'});
  const res = await r.json();
  alert(res.promoted ? `${asset} challenger promoted! 🏆`
                     : `No challenger available for ${asset}.`);
}

function renderControls() {
  const c = controlsData;
  if (!c) return;
  const strategyLabels = {ml:'🧠 ML (LSTM)', fallback:'📐 Fallback (RSI+Trend)', politician:'🏛️ Politician signal'};

  let html = `<div class="ctl-global">
    <div class="ctl-row">
      <div><b>🌍 Global trading</b><br><span class="ctl-hint">Master switch — off halts ALL new trades on every bot</span></div>
      ${toggleHtml(['global_trading'], c.global_trading)}
    </div>
    <div class="ctl-row">
      <div><b>⚠️ Global shorting</b><br><span class="ctl-hint">Unlimited-loss kill switch — off closes all shorts &amp; blocks new ones everywhere</span></div>
      ${toggleHtml(['global_shorting'], c.global_shorting, true)}
    </div>
  </div><div class="ctl-grid">`;

  for (const [asset, cfg] of Object.entries(c.per_asset)) {
    html += `<div class="ctl-card">
      <h3>${asset}</h3>
      <div class="ctl-row"><span>Trading</span>${toggleHtml(['per_asset',asset,'trading_enabled'], cfg.trading_enabled)}</div>`;
    if (asset !== 'funds') {
      html += `<div class="ctl-row"><span>Shorting ⚠️</span>${toggleHtml(['per_asset',asset,'shorting_enabled'], cfg.shorting_enabled, true)}</div>`;
    } else {
      html += `<div class="ctl-row"><span>Shorting</span><span class="ctl-hint">never (by design)</span></div>`;
    }
    for (const [strat, on] of Object.entries(cfg.strategies)) {
      html += `<div class="ctl-row"><span>${strategyLabels[strat]||strat}</span>${toggleHtml(['per_asset',asset,'strategies',strat], on)}</div>`;
    }
    html += `<div class="ctl-row"><span>Auto-promote challengers</span>${toggleHtml(['per_asset',asset,'auto_promote'], cfg.auto_promote)}</div>`;
    if (asset !== 'funds') {
      html += `<button class="promote-btn" onclick="promote('${asset}')">🏆 Promote challenger now</button>`;
    }
    html += `</div>`;
  }
  html += '</div>';
  document.getElementById('controls').innerHTML = html;
}

// ======================================================================
// Animated Neural Network Visualization
// ======================================================================

const NEURON_COLORS = {
  input:  '#39d2c0',  // cyan
  lstm:   '#bc8cff',  // purple
  dense:  '#d2991d',  // yellow
  output: '#3fb950',  // green
};

let netState = { particles: [], tick: 0 };

function showNetwork(asset) {
  const dna = champData[asset];
  if (!dna) return;

  const modal = document.getElementById('netModal');
  document.getElementById('netTitle').textContent =
    `🧠 ${asset.toUpperCase()} — LSTM Neural Network`;
  document.getElementById('netSub').textContent =
    `Hidden: ${dna.hidden_size} · Layers: ${dna.num_layers} · Seq: ${dna.seq_len} · Dropout: ${dna.dropout}`;
  modal.classList.add('active');

  // Build SVG
  const svg = document.getElementById('netSvg');
  const w = 800, h = 400;

  // Layer config: [name, neuron_count, x_position, color]
  const inputSize = asset === 'stocks' ? 10 : 7;
  const layers = [
    { name:'Input', count:inputSize, color:NEURON_COLORS.input },
    ...Array.from({length:dna.num_layers}, (_,i)=>({
      name:`LSTM ${i+1}`, count:Math.min(dna.hidden_size, 40), color:NEURON_COLORS.lstm
    })),
    { name:'Dense', count:16, color:NEURON_COLORS.dense },
    { name:'Output', count:2, color:NEURON_COLORS.output },
  ];

  const nLayers = layers.length;
  const layerGap = w / (nLayers + 1);

  // Assign x positions
  layers.forEach((l, i) => { l.x = layerGap * (i + 1); });

  // Build SVG HTML
  let svgHtml = '<defs>' +
    '<filter id="glow"><feGaussianBlur stdDeviation="2.5" result="blur"/>' +
    '<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>' +
    '</defs>';

  // Draw connections between layers (with ids for animation)
  let connId = 0;
  for (let li = 0; li < layers.length - 1; li++) {
    const from = layers[li], to = layers[li+1];
    const fromYs = neuronYs(from.count, h);
    const toYs = neuronYs(to.count, h);
    for (let fi = 0; fi < from.count; fi++) {
      for (let ti = 0; ti < to.count; ti++) {
        const y1 = fromYs[fi], y2 = toYs[ti];
        svgHtml += `<line x1="${from.x}" y1="${y1}" x2="${to.x}" y2="${y2}"
          id="conn-${connId}" stroke="${from.color}" stroke-width="0.4" opacity="0.2"/>`;
        connId++;
      }
    }
  }

  // Draw layer labels
  for (const l of layers) {
    svgHtml += `<text x="${l.x}" y="28" text-anchor="middle" fill="#8b949e" font-size="10">${l.name}</text>`;
    if (l.name.startsWith('LSTM')) {
      svgHtml += `<text x="${l.x}" y="42" text-anchor="middle" fill="#8b949e" font-size="8">${l.count} cells</text>`;
    }
  }

  // Draw neurons
  for (const l of layers) {
    const ys = neuronYs(l.count, h);
    for (let ni = 0; ni < l.count; ni++) {
      svgHtml += `<circle cx="${l.x}" cy="${ys[ni]}" r="7" fill="${l.color}" opacity="0.5"
        class="neuron-pulse" id="n-${l.name}-${ni}"/>`;
    }
  }

  // Output labels
  const outYs = neuronYs(2, h);
  svgHtml += `<text x="${layers[nLayers-1].x + 18}" y="${outYs[0] + 4}" fill="${NEURON_COLORS.output}" font-size="12" font-weight="600">UP 📈</text>`;
  svgHtml += `<text x="${layers[nLayers-1].x + 18}" y="${outYs[1] + 4}" fill="#f85149" font-size="12" font-weight="600">DOWN 📉</text>`;

  svg.innerHTML = svgHtml;

  // Start animation
  netState = {
    layers, connId, tick: 0, asset,
    particles: [],
    signal: Math.random() > 0.5 ? 1 : 0,  // pretend prediction
    svgEl: svg,
  };
  if (netAnimId) cancelAnimationFrame(netAnimId);
  animateNetwork();
}

function neuronYs(count, height) {
  const top = 70, bottom = height - 30;
  if (count === 1) return [(top + bottom) / 2];
  const gap = (bottom - top) / (count - 1);
  return Array.from({length: count}, (_, i) => top + gap * i);
}

function animateNetwork() {
  const ns = netState;
  if (!ns.layers) { netAnimId = null; return; }

  ns.tick++;

  // Spawn new particles periodically
  if (ns.tick % 3 === 0) {
    const fromLayer = ns.layers[Math.floor(Math.random() * (ns.layers.length - 1))];
    const toLayer = ns.layers[ns.layers.indexOf(fromLayer) + 1];
    const fromYs = neuronYs(fromLayer.count, 400);
    const toYs = neuronYs(toLayer.count, 400);
    ns.particles.push({
      x1: fromLayer.x, y1: fromYs[Math.floor(Math.random() * fromLayer.count)],
      x2: toLayer.x,   y2: toYs[Math.floor(Math.random() * toLayer.count)],
      progress: 0, speed: 0.01 + Math.random() * 0.03,
      color: fromLayer.color,
    });
  }

  // Cap particles
  if (ns.particles.length > 200) ns.particles = ns.particles.slice(-150);

  // Remove old particles from SVG
  const svg = ns.svgEl;
  svg.querySelectorAll('.particle').forEach(p => p.remove());

  // Update & draw particles
  const alive = [];
  for (const p of ns.particles) {
    p.progress += p.speed;
    if (p.progress >= 1) {
      // Flash destination neuron
      if (p.progress < 1.1) {
        const cx = p.x2, cy = p.y2;
        const flash = document.createElementNS('http://www.w3.org/2000/svg','circle');
        flash.setAttribute('cx', cx); flash.setAttribute('cy', cy);
        flash.setAttribute('r', '10'); flash.setAttribute('fill', p.color);
        flash.setAttribute('opacity', '0.8'); flash.setAttribute('class','particle');
        flash.setAttribute('filter','url(#glow)');
        svg.appendChild(flash);
        setTimeout(() => flash.remove(), 300);
      }
      continue; // drop
    }

    const cx = p.x1 + (p.x2 - p.x1) * p.progress;
    const cy = p.y1 + (p.y2 - p.y1) * p.progress;
    const circ = document.createElementNS('http://www.w3.org/2000/svg','circle');
    circ.setAttribute('cx', cx); circ.setAttribute('cy', cy);
    circ.setAttribute('r', '3'); circ.setAttribute('fill', p.color);
    circ.setAttribute('opacity', '0.8'); circ.setAttribute('class','particle');
    circ.setAttribute('filter','url(#glow)');
    svg.appendChild(circ);
    alive.push(p);
  }
  ns.particles = alive;

  // Pulse output neurons based on signal
  const outLayer = ns.layers[ns.layers.length - 1];
  const outYs = neuronYs(outLayer.count, 400);
  svg.querySelectorAll(`circle[id^="n-Output-"]`).forEach((c, i) => {
    const active = i === ns.signal;
    c.setAttribute('opacity', active ? '1' : '0.3');
    c.setAttribute('r', active ? '10' : '6');
    if (active) c.setAttribute('filter', 'url(#glow)');
    else c.removeAttribute('filter');
  });

  // Periodically flip the "prediction"
  if (ns.tick % 120 === 0) ns.signal = ns.signal === 0 ? 1 : 0;

  netAnimId = requestAnimationFrame(animateNetwork);
}

function closeNetwork() {
  if (netAnimId) cancelAnimationFrame(netAnimId);
  netAnimId = null;
  netState = { particles:[], tick:0 };
  document.getElementById('netModal').classList.remove('active');
}

// Close modal on Escape
document.addEventListener('keydown', e => { if (e.key==='Escape') closeNetwork(); });

// ======================================================================
// Init
// ======================================================================
loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def _render_login(error: str | None = None) -> str:
    """Return the login page HTML."""
    error_html = f'<p class="error">{error}</p>' if error else ""
    return LOGIN_TEMPLATE.replace("{error_html}", error_html)


def _render_dashboard() -> str:
    """Return the dashboard HTML."""
    return DASHBOARD_TEMPLATE
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("[Dashboard] Starting on http://0.0.0.0:1337")
    if _PASSWORD_HASH:
        print(f"[Dashboard] Password-protected — set via DASHBOARD_PASSWORD in .env")
    else:
        print("[Dashboard] ⚠️  No DASHBOARD_PASSWORD set — all access allowed!")

    app.run(host="0.0.0.0", port=1337, debug=False)


if __name__ == "__main__":
    main()
