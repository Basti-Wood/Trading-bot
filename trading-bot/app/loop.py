"""Tested stock strategies: activate versions, rank their verdicts per stock.

Versions on daily candles run in the Trading-Loop bot ("loop"); versions that declare an
intraday ZEITRAHMEN (5Min, 15Min, 30Min, 1Hour) run in the Intraday-Loop bot ("loop_day"),
which signals after every completed bar and is flat before each close.

Activation validates file hashes and risk settings. Each stock's own backtest decides
entry eligibility; a list-wide result never gates a stock. The best green version
by return, then lowest absolute drawdown supplies the entry signal. An open position
keeps its owning version's exits until that version is deactivated.
"""
import ast
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from .store import utcnow

VERSION = re.compile(r"[a-z0-9_]{1,60}_v\d{1,3}")
STOCK = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
INSTRUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
LIST = "LISTE"
RULES = {"STOP_LOSS_PROZENT": "stop_loss", "MAX_DRAWDOWN_PROZENT": "max_drawdown"}
BOT_ROOT = Path(__file__).resolve().parent.parent
# stresstest.py loads 5 years up to yesterday; the bot's per-symbol tests use the same window.
HISTORY_DAYS = 5 * 365 + 3
# New York time: yesterday's daily candle is final and the market is not open yet.
RETEST_HOUR = 8
RETEST_TIMEOUT = 1200
# Re-run on schedule: tested versions whose file is unchanged (edited files may be work in progress).
RETESTABLE = ("bereit", "gruen", "rot", "veraltet")
# Intraday versions: bar length in minutes. stresstest.py loads about 60 sessions of these bars.
TIMEFRAMES = {"5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60}
DAILY = "1Day"
INTRADAY_HISTORY_DAYS = 90
# Intraday versions are re-tuned this often during the session, besides the morning run.
INTRADAY_RETEST_MINUTES = 60


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


def declared(path, name):
    """A top-level string/number constant of a strategy file, read without executing it."""
    try:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)):
                return str(node.value.value)
    except (OSError, SyntaxError, ValueError):
        pass
    return None


def declared_symbol(path):
    return declared(path, "SYMBOL")


def bot_for(timeframe):
    """Which bot trades a version: intraday candles go to the Intraday-Loop."""
    return "loop_day" if timeframe in TIMEFRAMES else "loop"


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
    if not isinstance(report, dict):
        report = None
    sha = file_hash(path)
    symbol = (report or {}).get("symbol") or declared_symbol(path)
    timeframe = declared(path, "ZEITRAHMEN") or DAILY
    name, _, number = path.stem.rpartition("_v")
    info = {"version": path.stem, "idea": name, "number": int(number), "symbol": symbol, "sha256": sha, "report": report,
            "timeframe": timeframe}
    if not report:
        status, note = "ungetestet", "Noch kein Stresstest mit Bericht"
    elif report.get("sha256") != sha:
        status, note = "geaendert", "Datei nach dem Stresstest geändert; erst neu testen"
    elif report.get("risiko") != {"stop_loss": rules["stop_loss"], "max_drawdown": rules["max_drawdown"]}:
        status, note = "veraltet", "Getestet mit anderen Werten aus risiko.py; erst neu testen"
    else:
        status, note = "bereit", "Getestet; jede Aktie entscheidet einzeln"
    if not (symbol == LIST or isinstance(symbol, str) and INSTRUMENT.fullmatch(symbol)):
        status, note = "nicht_handelbar", "Nur Aktien und ETFs mit gültiger Instrument-ID werden unterstützt"
    elif timeframe != DAILY and timeframe not in TIMEFRAMES:
        status, note = "nicht_handelbar", f"ZEITRAHMEN muss {', '.join(TIMEFRAMES)} sein (oder fehlen für Tageskerzen)"
    return {**info, "status": status, "note": note, "tradable": status == "bereit" and rules["ok"], "bot": bot_for(timeframe)}


def catalogue(config):
    if not available(config):
        return []
    rules = risk_rules(config)
    folder = config.loop_dir / "strategien"
    files = sorted(folder.glob("*.py")) if folder.is_dir() else []
    versions = [version_info(config, p, rules) for p in files if VERSION.fullmatch(p.stem)]
    return sorted(versions, key=lambda v: (v["idea"], v["number"]))


def retest_targets(config, version=None, include_changed=False, bot=None):
    """One named version (any status), or every version due for a re-run.

    The scheduled run skips edited files (they may be work in progress); "Alle neu testen" in the
    dashboard includes them, so a strategy that gained PARAMETER can be tuned with one click.
    """
    if version is not None:
        path = strategy_path(config, version)
        return [path.stem] if path else []
    due = (*RETESTABLE, "geaendert") if include_changed else RETESTABLE
    return [v["version"] for v in catalogue(config) if v["status"] in due and (bot is None or v["bot"] == bot)]


def loop_python(config):
    """stresstest.py needs alpaca-py, which only the trading-loop's own .venv has."""
    for name in ("Scripts/python.exe", "bin/python"):
        path = config.loop_dir / ".venv" / name
        if path.is_file():
            return str(path)
    return sys.executable


def retest(config, version):
    """Run stresstest.py for one version, exactly as the agents do; returns its verdict line."""
    path = strategy_path(config, version)
    if not path:
        return "Fehler: Strategie-Datei fehlt"
    # liste.py reads the stock list from this bot's database, wherever DATA_DIR points.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TRADING_BOT_DB": str((config.data_dir / "bot.sqlite3").resolve())}
    try:
        result = subprocess.run([loop_python(config), "stresstest.py", str(path)], cwd=config.loop_dir, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RETEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"Fehler: Stresstest dauerte länger als {RETEST_TIMEOUT // 60} min"
    verdict = next((l for l in reversed(result.stdout.splitlines()) if l.startswith("ERGEBNIS:")), None)
    if result.returncode == 0 and verdict:
        return verdict.removeprefix("ERGEBNIS:").strip()
    detail = (result.stderr.strip().splitlines() or result.stdout.strip().splitlines() or ["unbekannter Fehler"])[-1]
    return f"Fehler: {detail[:200]}"


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
    if record["symbol"] == LIST:
        symbols = bot_list(config, store, positions)
    else:
        symbols = [record["symbol"]]
    owned = [s for s in store.owned_symbols() if owner_version(store, s) == record["version"]]
    # This context belongs to Alpaca; Trading 212 evaluates its own verified IDs.
    return [s for s in dict.fromkeys([*symbols, *owned]) if STOCK.fullmatch(s)]


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
    zone = bars.attrs.get("market_timezone", "America/New_York")
    frame = bars[["open", "high", "low", "close", "volume"]].rename(columns=str.capitalize)
    index = frame.index.tz_convert(zone) if frame.index.tz is not None else frame.index
    frame.index = index.normalize().tz_localize(None) if index.tz is not None else index.normalize()
    return frame[~frame.index.duplicated(keep="last")]


def intraday_frame(bars, minutes):
    """Completed regular-session bars in New York wall time, as stresstest.py loads them."""
    frame = bars[["open", "high", "low", "close", "volume"]].rename(columns=str.capitalize)
    local = frame.index.tz_convert("America/New_York")
    start = local.hour * 60 + local.minute
    keep = (start >= 9 * 60 + 30) & (start + minutes <= 16 * 60)
    frame = frame[keep]
    frame.index = local[keep].tz_localize(None)
    return frame[~frame.index.duplicated(keep="last")]


def evaluate(config, record, frames):
    """Test and signal one version on {symbol: bars} in a subprocess; returns per-symbol results."""
    path = strategy_path(config, record["version"])
    if not path:
        raise ValueError(f"Strategie-Datei {record['version']} fehlt")
    if file_hash(path) != record["sha256"]:
        raise ValueError(f"{record['version']} wurde nach dem Aktivieren geändert; neu testen und neu aktivieren")
    with tempfile.TemporaryDirectory() as folder:
        minutes = TIMEFRAMES.get(record.get("timeframe"))
        converted = {s: intraday_frame(b, minutes) if minutes else daily_frame(b) for s, b in frames.items()}
        for symbol, frame in converted.items():
            if len(frame):
                frame.to_csv(Path(folder) / f"{symbol}.csv")
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
        bar_at = bars.index[-1]
        if minutes:
            # The last completed regular-session bar the strategy saw (bot frames may hold extended hours).
            bar_at = converted[symbol].index[-1].tz_localize("America/New_York").tz_convert("UTC")
            close = float(converted[symbol]["Close"].iloc[-1])
        results[symbol] = {**item, "bar_at": bar_at.isoformat(), "target_session": bars.attrs.get("target_session"),
                           "timeframe": record.get("timeframe", DAILY),
                           "market_timezone": bars.attrs.get("market_timezone", "America/New_York"),
                           "last_close": close if math.isfinite(close) else None}
    return {"version": record["version"], "sha256": record["sha256"], "symbols": results, "at": utcnow()}


def combine(store, symbol, config=None):
    """One decision input per symbol from all active versions; the owning version decides its exit."""
    active = active_versions(store)
    base = {"symbol": symbol, "trading_mode": getattr(store, "strategy", "loop"), "models": [], "expected_return": 0., "eligible": True,
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
                "signal": action, "forecast_signal": action, "test": item.get("test"), "reason": f"{owner}: {item['reason']}"}
    if config is not None:
        valid = {v["version"]: v["sha256"] for v in catalogue(config) if v["tradable"]}
        views = {v: i for v, i in views.items() if valid.get(v) == active[v]["sha256"]}
        if not risk_rules(config)["ok"]:
            return {**base, "reason": "risiko.py ist unvollständig; keine neuen Käufe"}
    green = [(v, i) for v, i in views.items() if green_test(i.get("test")) and not i.get("error")]
    if green:
        version, item = min(green, key=ranking)
        test = item["test"]
        action = "BUY" if item.get("signal") == "BUY" else "HOLD"
        return {**base, **_source(item), "version": version, "sha256": active[version]["sha256"],
                "signal": action, "forecast_signal": action, "expected_return": test["rendite"] / 100,
                "test": test, "strategy_signal": item.get("signal"),
                "reason": f"{version}: beste grüne Version für {symbol} ({test['rendite']:+.1f} %, "
                          f"max. Drawdown {test['max_drawdown']:.1f} %); {item.get('reason', item.get('signal'))}"}
    return {**base, "reason": f"Kein grüner Einzeltest für {symbol}" if views else "Warte auf das erste Signal der aktiven Versionen"}


def green_test(test):
    return (isinstance(test, dict) and test.get("gruen") is True
            and all(isinstance(test.get(k), (int, float)) and not isinstance(test[k], bool)
                    and math.isfinite(test[k]) for k in ("rendite", "max_drawdown")))


def ranking(entry):
    version, item = entry
    test = item["test"]
    return (-test["rendite"], abs(test["max_drawdown"]), version)


def _source(item):
    return {k: item.get(k) for k in ("bar_at", "target_session", "last_close", "to", "timeframe", "market_timezone")}
