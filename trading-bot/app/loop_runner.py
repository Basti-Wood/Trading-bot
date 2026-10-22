"""Isolated run of one trading-loop strategy version over one or more symbols.

  python -m app.loop_runner <strategien/name_vN.py> <folder with SYMBOL.csv files> <trading-loop folder>

Per symbol, two backtests with the stress test's own capital and costs: one exactly like
stresstest.py (its verdict decides whether the symbol may be traded) and one without closing
the final trade, whose pending orders are the strategy's instruction for the next open.
Prints one JSON line.
"""
import importlib.util
import json
import math
from pathlib import Path
import sys
import warnings


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


def run(module, data, stresstest, Backtest, symbol=""):
    costs = stresstest.KOSTEN
    test = Backtest(data, module.Strategie, cash=stresstest.STARTKAPITAL, commission=costs, finalize_trades=True).run()
    verdict = stresstest.urteil(test)
    live = Backtest(data, module.Strategie, cash=stresstest.STARTKAPITAL, commission=costs, finalize_trades=False).run()
    action, reason = signal(live._strategy)
    return {"signal": action, "reason": reason, "bars": len(data),
            "from": str(data.index[0].date()), "to": str(data.index[-1].date()),
            "test": {"gruen": verdict == "GRÜN", "urteil": verdict, "rendite": number(test["Return [%]"]),
                     "kaufen_halten": number(test["Buy & Hold Return [%]"]),
                     "max_drawdown": number(test["Max. Drawdown [%]"]), "trades": int(test["# Trades"])}}


def main(path, folder, loop_dir):
    sys.path.insert(0, loop_dir)
    warnings.filterwarnings("ignore")
    import pandas as pd
    from backtesting import Backtest
    import stresstest  # Same capital, costs and verdict as the stress test.

    spec = importlib.util.spec_from_file_location("loop_strategy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    results = {}
    for csv in sorted(Path(folder).glob("*.csv")):
        symbol = csv.stem
        try:
            results[symbol] = run(module, pd.read_csv(csv, index_col=0, parse_dates=True), stresstest, Backtest, symbol)
        except SystemExit:
            raise
        except Exception as exc:  # One unusable symbol must not hide the others.
            results[symbol] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    print(json.dumps({"symbol": module.SYMBOL, "symbols": results}))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Aufruf: python -m app.loop_runner <strategie.py> <kurs-ordner> <trading-loop-ordner>")
    main(*sys.argv[1:])
