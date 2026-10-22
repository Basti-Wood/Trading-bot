"""Isolated run of one trading-loop strategy version over one or more symbols.

  python -m app.loop_runner <strategien/name_vN.py> <folder with SYMBOL.csv files> <trading-loop folder>

Per symbol, the stress test's verdict and tuned values come from the version's current report
(same file hash and risiko.py values, at most REPORT_DAYS old); a stock the report does not cover
is tested and tuned here with stresstest.pruefe, exactly as the stress test does. A second run
with those values and without closing the final trade gives the pending orders: the strategy's
instruction for the next open. Chart data (recent candles, the strategy's own indicators and
trades) is included for the dashboard. Prints one JSON line.
"""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import warnings

import numpy as np
import pandas as pd

REPORT_DAYS = 7
CHART_BARS = 130


def signal(strategy):
    """Pending orders after the last bar are the strategy's instructions for the next open."""
    entries = [o for o in strategy.orders if not o.is_contingent and o.parent_trade is None]
    closes = [o for o in strategy.orders if not o.is_contingent and o.parent_trade is not None]
    size = strategy.position.size
    if size < 0 or any(o.size < 0 for o in entries):
        return "SELL", "Strategie will short gehen; der Bot handelt nur long und bleibt ohne Position"
    if size > 0 and closes:
        return "SELL", "Ausstiegssignal: die Strategie schließt ihre Position zur nächsten Eröffnung"
    if size > 0:
        return "HOLD", "Strategie ist investiert und hält"
    if entries:
        return "BUY", "Einstiegssignal: die Strategie kauft zur nächsten Eröffnung"
    return "SELL", "Kein Signal: die Strategie ist ohne Position"


def number(value):
    value = float(value)
    return round(value, 2) if math.isfinite(value) else None


def report_entries(path, loop_dir, stresstest):
    """{symbol: entry} from the version's report if it belongs to exactly this file and risiko.py."""
    try:
        report = json.loads((Path(loop_dir) / "berichte" / f"{Path(path).stem}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    risiko = getattr(stresstest, "risiko", None)
    if (not isinstance(report, dict) or report.get("sha256") != hashlib.sha256(Path(path).read_bytes()).hexdigest()
            or risiko is None or report.get("risiko") != {"stop_loss": risiko.STOP_LOSS_PROZENT,
                                                          "max_drawdown": risiko.MAX_DRAWDOWN_PROZENT}):
        return {}
    if isinstance(report.get("aktien"), dict):
        return report["aktien"]
    return {report.get("symbol"): report}


def usable(entry, data):
    """Only tuned entries (stresstest.pruefe) whose test reaches the current data are reused."""
    try:
        return (entry.get("methode") in {"walk_forward", "fuenf_jahre"} and entry.get("ergebnis") in {"gruen", "rot"}
                and isinstance(entry.get("parameter"), dict)
                and (data.index[-1] - pd.Timestamp(entry["bis"])).days <= REPORT_DAYS)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def finite(value):
    value = float(value)
    return round(value, 4) if math.isfinite(value) else None


def chart(live, data):
    """Recent daily candles with the strategy's own indicators and trades, for the dashboard."""
    strategy = live._strategy
    start = len(data) - min(CHART_BARS, len(data))
    days = [f"{d:%Y-%m-%d}" for d in data.index[start:]]
    bars = [{"at": day, **{k.lower(): finite(row[k]) for k in ("Open", "High", "Low", "Close")}}
            for day, (_, row) in zip(days, data.iloc[start:].iterrows())]
    indicators = []
    for indicator in getattr(strategy, "_indicators", [])[:8]:
        values = np.atleast_2d(np.asarray(indicator, dtype=float))
        names = indicator.name if isinstance(indicator.name, list) else [indicator.name] * len(values)
        overlay = indicator._opts.get("overlay")
        for name, row in zip(names, values[:4]):
            if len(row) != len(data):
                continue
            indicators.append({"name": str(name)[:40], "overlay": bool(overlay),
                               "values": [finite(v) for v in row[start:]]})
    trades = []
    closed = live["_trades"]
    for _, trade in closed.iterrows():
        if trade["ExitTime"] >= data.index[start]:
            trades.append({"entry_at": f"{trade['EntryTime']:%Y-%m-%d}", "entry": finite(trade["EntryPrice"]),
                           "exit_at": f"{trade['ExitTime']:%Y-%m-%d}", "exit": finite(trade["ExitPrice"]),
                           "return": finite(trade["ReturnPct"])})
    for trade in strategy.trades:  # Still open after the last bar.
        trades.append({"entry_at": f"{trade.entry_time:%Y-%m-%d}", "entry": finite(trade.entry_price),
                       "exit_at": None, "exit": None, "return": None})
    return {"bars": bars, "indicators": indicators, "trades": trades}


def run(module, data, stresstest, Backtest, symbol="", reports=None):
    costs = stresstest.KOSTEN
    entry = (reports or {}).get(symbol)
    if hasattr(stresstest, "pruefe"):
        source = "bericht" if usable(entry, data) else "neu"
        if source == "neu":
            entry, _, _ = stresstest.pruefe(module, data)
        values = entry["parameter"]
        with stresstest.parameter(module, values):
            live = Backtest(data, module.Strategie, cash=stresstest.STARTKAPITAL, commission=costs, finalize_trades=False).run()
        verdict = entry["urteil"]
        test = {k: entry.get(k) for k in ("rendite", "kaufen_halten", "max_drawdown", "trades")}
        test.update(methode=entry["methode"], parameter=values, standard=entry.get("standard", {}), quelle=source,
                    angepasst=bool(entry.get("angepasst")), zeitraum=entry.get("test", {}).get("von") and
                    f"{entry['test']['von']} bis {entry['test']['bis']}", jahre=entry.get("test", {}).get("jahre", []))
    else:  # An older stresstest.py without self-tuning.
        stats = Backtest(data, module.Strategie, cash=stresstest.STARTKAPITAL, commission=costs, finalize_trades=True).run()
        verdict = stresstest.urteil(stats)
        live = Backtest(data, module.Strategie, cash=stresstest.STARTKAPITAL, commission=costs, finalize_trades=False).run()
        test = {"rendite": number(stats["Return [%]"]), "kaufen_halten": number(stats["Buy & Hold Return [%]"]),
                "max_drawdown": number(stats["Max. Drawdown [%]"]), "trades": int(stats["# Trades"]), "parameter": {}}
    action, reason = signal(live._strategy)
    return {"signal": action, "reason": reason, "bars": len(data),
            "from": str(data.index[0].date()), "to": str(data.index[-1].date()),
            "test": {"gruen": verdict == "GRÜN", "urteil": verdict, **test}, "chart": chart(live, data)}


def main(path, folder, loop_dir):
    sys.path.insert(0, loop_dir)
    warnings.filterwarnings("ignore")
    from backtesting import Backtest
    import stresstest  # Same capital, costs and verdict as the stress test.

    spec = importlib.util.spec_from_file_location("loop_strategy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reports = report_entries(path, loop_dir, stresstest)
    results = {}
    for csv in sorted(Path(folder).glob("*.csv")):
        symbol = csv.stem
        try:
            results[symbol] = run(module, pd.read_csv(csv, index_col=0, parse_dates=True), stresstest, Backtest, symbol, reports)
        except SystemExit:
            raise
        except Exception as exc:  # One unusable symbol must not hide the others.
            results[symbol] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    print(json.dumps({"symbol": module.SYMBOL, "symbols": results}))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Aufruf: python -m app.loop_runner <strategie.py> <kurs-ordner> <trading-loop-ordner>")
    main(*sys.argv[1:])
