"""Bridge to the trading-loop folder: tested strategy versions become a paper strategy.

A version trades either one fixed symbol or, with SYMBOL = "LISTE", the symbol list the other
bots use (the scanner shortlist or watchlist) plus the user's own holdings. Only versions whose
stress test is green, whose file is unchanged since that test and whose test used the current
risiko.py values may be activated. The bot then re-runs the same strategy per symbol in an
isolated subprocess: a symbol may be bought only if its own five-year test is green, and the
pending orders after the last bar are the signal.
"""
import ast
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from .store import utcnow

VERSION = re.compile(r"[a-z0-9_]{1,60}_v\d{1,3}")
STOCK = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
LIST = "LISTE"
RULES = {"STOP_LOSS_PROZENT": "stop_loss", "MAX_DRAWDOWN_PROZENT": "max_drawdown"}
BOT_ROOT = Path(__file__).resolve().parent.parent


def available(config):
    return bool(config.loop_dir and (config.loop_dir / "risiko.py").is_file())


def risk_rules(config):
    """Read risiko.py without executing it."""
    rules = {"stop_loss": None, "max_drawdown": None, "ok": False}
    if not available(config):
        return {**rules, "message": "Trading-Loop-Ordner nicht gefunden"}
    try:
        tree = ast.parse((config.loop_dir / "risiko.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return {**rules, "message": "risiko.py ist nicht lesbar"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in RULES:
                    rules[RULES[target.id]] = node.value.value
    for name, key in RULES.items():
        value = rules[key]
        if value is None:
            return {**rules, "message": f"{name} fehlt in risiko.py"}
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 100:
            return {**rules, "stop_loss": None, "max_drawdown": None,
                    "message": f"{name} in risiko.py muss eine Zahl zwischen 0 und 100 sein"}
    return {**rules, "ok": True, "message": f"Stop-Loss {rules['stop_loss']} % · max. Drawdown {rules['max_drawdown']} %"}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declared_symbol(path):
    try:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and any(isinstance(t, ast.Name) and t.id == "SYMBOL" for t in node.targets)):
                return str(node.value.value)
    except (OSError, SyntaxError, ValueError):
        pass
    return None


def strategy_path(config, version):
    """Resolve a version name to its file; names never become free-form paths."""
    if not isinstance(version, str) or not VERSION.fullmatch(version) or not available(config):
        return None
    path = config.loop_dir / "strategien" / f"{version}.py"
    return path if path.is_file() else None


def idea(version):
    return version.rpartition("_v")[0]


def version_info(config, path, rules=None):
    rules = rules or risk_rules(config)
    report_file = config.loop_dir / "berichte" / f"{path.stem}.json"
    try:
        report = json.loads(report_file.read_text(encoding="utf-8")) if report_file.is_file() else None
    except (OSError, ValueError):
        report = None
    sha = file_hash(path)
    symbol = (report or {}).get("symbol") or declared_symbol(path)
    name, _, number = path.stem.rpartition("_v")
    info = {"version": path.stem, "idea": name, "number": int(number), "symbol": symbol, "sha256": sha, "report": report}
    if not report:
        status, note = "ungetestet", "Noch kein Stresstest mit Bericht"
    elif report.get("sha256") != sha:
        status, note = "geaendert", "Datei nach dem Stresstest geändert; erst neu testen"
    elif report.get("ergebnis") != "gruen":
        status, note = "rot", report.get("urteil") or "Stresstest rot"
    elif report.get("risiko") != {"stop_loss": rules["stop_loss"], "max_drawdown": rules["max_drawdown"]}:
        status, note = "veraltet", "Getestet mit anderen Werten aus risiko.py; erst neu testen"
    else:
        status, note = "gruen", "Stresstest grün" + (f" ({report.get('urteil')})" if symbol == LIST else "")
    if status == "gruen" and not (symbol == LIST or symbol and STOCK.fullmatch(symbol)):
        status, note = "nicht_handelbar", "Der Bot handelt nur US-Aktien und ETFs, kein Krypto"
    return {**info, "status": status, "note": note, "tradable": status == "gruen" and rules["ok"]}


def catalogue(config):
    if not available(config):
        return []
    rules = risk_rules(config)
    folder = config.loop_dir / "strategien"
    files = sorted(folder.glob("*.py")) if folder.is_dir() else []
    versions = [version_info(config, p, rules) for p in files if VERSION.fullmatch(p.stem)]
    return sorted(versions, key=lambda v: (v["idea"], v["number"]))


def errors(config, limit=10):
    """Latest entries of fehler.db, opened read-only."""
    path = config.loop_dir / "fehler.db" if config.loop_dir else None
    if not path or not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT datum, strategie, version, problem, verlust_trades, aenderung, ergebnis"
                                      " FROM fehler ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    keys = ("datum", "strategie", "version", "problem", "verlust_trades", "aenderung", "ergebnis")
    return [dict(zip(keys, row)) for row in rows]


def active_versions(store):
    """{version: record}; older journals keyed the same records by symbol."""
    saved = store.get("loop_strategies") or {}
    return {record["version"]: record for record in saved.values()}


def bot_list(config, store, positions=None):
    """The list the other bots use plus the user's own holdings."""
    from .universe import watched_symbols
    shortlist = list(config.symbols) if config.universe_mode == "watchlist" else (store.get("universe") or {}).get("symbols", [])
    if positions is None:
        positions = (store.get("account") or {}).get("positions", [])
    return list(dict.fromkeys([*shortlist, *watched_symbols(config, store, positions)]))


def version_symbols(config, store, record, positions=None):
    """Symbols a version evaluates, including positions it still owns after they left the list."""
    symbols = bot_list(config, store, positions) if record["symbol"] == LIST else [record["symbol"]]
    owned = [s for s in store.owned_symbols() if owner_version(store, s) == record["version"]]
    return list(dict.fromkeys([*symbols, *owned]))


def owner_version(store, symbol):
    """The active version responsible for a loop-owned position (a newer version of the same idea takes over)."""
    owner = store.get(f"loop_owner:{symbol}")
    active = active_versions(store)
    if not owner or owner in active:
        return owner if owner in active else None
    successors = sorted((v for v in active if idea(v) == idea(owner)), key=lambda v: int(v.rpartition("_v")[2]))
    return successors[-1] if successors else None


def daily_frame(bars):
    """Bot bars (UTC, lowercase) to the backtesting.py layout used by stresstest.py."""
    frame = bars[["open", "high", "low", "close", "volume"]].rename(columns=str.capitalize)
    index = frame.index.tz_convert("America/New_York") if frame.index.tz is not None else frame.index
    frame.index = index.normalize().tz_localize(None) if index.tz is not None else index.normalize()
    return frame[~frame.index.duplicated(keep="last")]


def evaluate(config, record, frames):
    """Test and signal one version on {symbol: bars} in a subprocess; returns per-symbol results."""
    path = strategy_path(config, record["version"])
    if not path:
        raise ValueError(f"Strategie-Datei {record['version']} fehlt")
    if file_hash(path) != record["sha256"]:
        raise ValueError(f"{record['version']} wurde nach dem Aktivieren geändert; neu testen und neu aktivieren")
    with tempfile.TemporaryDirectory() as folder:
        for symbol, bars in frames.items():
            daily_frame(bars).to_csv(Path(folder) / f"{symbol}.csv")
        try:
            result = subprocess.run([sys.executable, "-m", "app.loop_runner", str(path), folder, str(config.loop_dir)],
                                    cwd=BOT_ROOT, capture_output=True, text=True, timeout=config.loop_timeout)
        except subprocess.TimeoutExpired:
            raise ValueError(f"{record['version']}: Berechnung dauerte länger als {config.loop_timeout} s")
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode or not lines:
        detail = (result.stderr.strip().splitlines() or ["unbekannter Fehler"])[-1]
        raise ValueError(f"{record['version']}: {detail[:200]}")
    output = json.loads(lines[-1])
    if output.get("symbol") != record["symbol"]:
        raise ValueError(f"{record['version']}: SYMBOL passt nicht mehr zur aktivierten Version")
    results = {}
    for symbol, item in output["symbols"].items():
        bars = frames.get(symbol)
        if bars is None:
            continue
        close = float(bars["close"].iloc[-1])
        results[symbol] = {**item, "bar_at": bars.index[-1].isoformat(), "target_session": bars.attrs.get("target_session"),
                           "last_close": close if math.isfinite(close) else None}
    return {"version": record["version"], "sha256": record["sha256"], "symbols": results, "at": utcnow()}


def combine(store, symbol):
    """One decision input per symbol from all active versions; the owning version decides its exit."""
    active = active_versions(store)
    base = {"symbol": symbol, "trading_mode": "loop", "models": [], "expected_return": 0., "eligible": True,
            "forecast_signal": "HOLD", "signal": "HOLD", "version": None, "sha256": None, "views": {}}
    views = {}
    for version, record in sorted(active.items()):
        results = store.get(f"loop_results:{version}") or {}
        item = (results.get("symbols") or {}).get(symbol)
        if item and results.get("sha256") == record["sha256"]:
            views[version] = item
    base["views"] = {v: {"signal": i.get("signal"), "test": i.get("test"), "error": i.get("error")} for v, i in views.items()}
    owner = owner_version(store, symbol) if store.get(f"owned:{symbol}", False) else None
    if store.get(f"owned:{symbol}", False):
        item = views.get(owner)
        if not owner:
            return {**base, "signal": "SELL", "forecast_signal": "SELL", "reason": "Keine aktive Version ist mehr für diese Position zuständig"}
        if not item or item.get("error"):
            return {**base, "version": owner, "reason": f"{owner}: warte auf ein aktuelles Signal"}
        action = "HOLD" if item["signal"] == "BUY" else item["signal"]
        return {**base, **_source(item), "version": owner, "sha256": active[owner]["sha256"],
                "signal": action, "forecast_signal": action, "reason": f"{owner}: {item['reason']}"}
    buys = [(v, i) for v, i in views.items() if i.get("signal") == "BUY"]
    green = [(v, i) for v, i in buys if (i.get("test") or {}).get("gruen")]
    if green:
        version, item = green[0]
        test = item["test"]
        return {**base, **_source(item), "version": version, "sha256": active[version]["sha256"], "signal": "BUY",
                "forecast_signal": "BUY", "reason": f"{version}: {item['reason']} (Stresstest {symbol} grün: "
                                                      f"{test['rendite']:+.1f} %, max. Drawdown {test['max_drawdown']:.1f} %)"}
    if buys:
        version, item = buys[0]
        return {**base, **_source(item), "version": version,
                "reason": f"{version}: Kaufsignal, aber Stresstest für {symbol} {item['test']['urteil'] if item.get('test') else 'fehlt'}"}
    if not views:
        return {**base, "reason": "Warte auf das erste Signal der aktiven Versionen"}
    version, item = sorted(views.items())[0]
    return {**base, **_source(item), "version": version, "signal": "SELL", "forecast_signal": "SELL",
            "reason": "Kein Kaufsignal: " + "; ".join(f"{v}: {i.get('reason') or i.get('error')}" for v, i in sorted(views.items()))}


def _source(item):
    return {k: item.get(k) for k in ("bar_at", "target_session", "last_close", "to")}
