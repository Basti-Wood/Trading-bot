"""Forecast versus reality for the dashboard: candles, each model's recorded price forecast and the outcome.

Only immutable experience records are drawn (feedback.capture_forecast), so a forecast is shown
exactly as the model made it before the outcome was known. A model's predicted price is the
reference price times (1 + its predicted return):

* daily: reference is the next session's open (the label's entry), target is the open one
  session later; an unresolved forecast uses the latest completed close as an indicative reference.
* intraday: reference is the close of the input five-minute bar, target the close 15 minutes later.

Chart code never places orders; fills come from the order journal.
"""
import math
import pandas as pd

from .feedback import schema
from .models import MODEL_NAMES

NY = "America/New_York"
DAILY_SESSIONS = 90
NAMES = {**MODEL_NAMES, "ensemble": "Ensemble (Median)"}


def finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def candle_rows(frame):
    return [{"at": at.isoformat(), **{k: finite(row[k]) for k in ("open", "high", "low", "close")}}
            for at, row in frame.iterrows()]


def window(frame, intraday):
    """Daily: the latest sessions. Intraday: the latest regular session's five-minute bars."""
    if frame is None or frame.empty:
        return frame
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    if not intraday:
        return frame.tail(DAILY_SESSIONS)
    local = frame.index.tz_convert(NY)
    minutes = local.hour * 60 + local.minute
    regular = frame[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]
    if regular.empty:
        return regular
    day = regular.index.tz_convert(NY).date
    return regular[day == day[-1]]


def points(config, store, symbol, bars):
    """Recorded forecasts inside the candle window, oldest first."""
    if bars is None or bars.empty:
        return []
    intraday = config.intraday
    first = bars.index[0]
    out = []
    for record in reversed(store.experience(symbol, config.feed, schema(config))):
        if record["status"] == "excluded":
            continue
        bar_at = pd.Timestamp(record["bar_at"])
        if bar_at < first:
            continue
        item = {"bar_at": record["bar_at"], "status": record["status"], "model_version": record["model_version"]}
        if record["status"] == "resolved":
            outcome = record["outcome"]
            reference, actual = finite(outcome.get("entry")), finite(outcome.get("target"))
            if reference is None or actual is None:
                continue
            end = pd.Timestamp(outcome["label_end"])
            # The candle that holds the target price: intraday the last bar of the horizon, daily the exit session.
            target_at = end - pd.Timedelta(minutes=5) if intraday else end
            item.update(reference_at=record["bar_at"] if intraday else outcome.get("entry_at"), target_at=target_at.isoformat(),
                        actual=reference * (1 + actual), actual_return=actual, reference_kind="entry")
        else:
            if bar_at not in bars.index:
                continue
            reference = finite(bars.loc[bar_at, "close"])
            if reference is None:
                continue
            item.update(reference_at=record["bar_at"], reference_kind="close",
                        target_at=(bar_at + pd.Timedelta(minutes=15)).isoformat() if intraday else None)
        item["reference"] = reference
        item["models"] = {name: {"return": value, "price": reference * (1 + value)}
                          for name, value in record["predictions"].items() if finite(value) is not None}
        out.append(item)
    return out


def fills(store, symbol, strategy, bars):
    """Confirmed fills of this strategy in the chart window."""
    if bars is None or bars.empty:
        return []
    first = bars.index[0]
    out = []
    for order in store.orders(500):
        price, qty = finite(order.get("filled_avg_price")), finite(order.get("filled_qty"))
        if order.get("symbol") != symbol or (order.get("strategy") or "daily") != strategy or not price or not qty:
            continue
        at = pd.Timestamp(order.get("filled_at") or order["at"])
        if at.tzinfo is None:
            at = at.tz_localize("UTC")
        if at >= first:
            out.append({"at": at.isoformat(), "side": order.get("side"), "price": price, "qty": qty})
    return sorted(out, key=lambda o: o["at"])


def payload(config, store, symbol, frame, strategy, now):
    bars = window(frame, config.intraday)
    if bars is None:
        bars = pd.DataFrame(columns=["open", "high", "low", "close"])
    return {"symbol": symbol, "strategy": strategy, "timeframe": "5Min" if config.intraday else "1Day",
            "horizon": "15 Minuten" if config.intraday else "nächste Eröffnung bis zur Eröffnung danach",
            "at": pd.Timestamp(now).isoformat(), "bars": candle_rows(bars), "names": NAMES,
            "forecasts": points(config, store, symbol, bars), "fills": fills(store, symbol, strategy, bars)}
