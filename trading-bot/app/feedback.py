"""Replay immutable forecast inputs against observed future market outcomes."""
import hashlib
import json
import math
from datetime import datetime, time
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

from .features import FEATURES, FEATURE_SCHEMA, dataset
from .store import utcnow


def summary_key(config, symbol):
    return f"feedback:{config.feed}:{schema(config)}:{symbol}"


def schema(config):
    return 2 if config.intraday else FEATURE_SCHEMA


def capture_forecast(config, store, forecast):
    values = {name: float(forecast["features"][name]) for name in FEATURES}
    predictions = {item["id"]: float(item["expected_return"]) for item in forecast["models"]}
    predictions["ensemble"] = float(forecast["expected_return"])
    if not all(math.isfinite(v) for v in [*values.values(), *predictions.values()]):
        raise ValueError("Non-finite forecast cannot be saved for learning")
    captured = pd.Timestamp(forecast["at"])
    bar_at = pd.Timestamp(forecast["bar_at"])
    if captured.tzinfo is None or bar_at.tzinfo is None:
        raise ValueError("Learning snapshots require timezone-aware timestamps")
    # A forecast needs a completed input session, not an in-progress daily bar.
    if config.intraday and captured < bar_at + pd.Timedelta(minutes=5):
        raise ValueError("Forecast input bar is not complete")
    if not config.intraday and captured.tz_convert("America/New_York").date() <= bar_at.tz_convert("America/New_York").date():
        complete = pd.Timestamp(forecast.get("input_complete_at")) if forecast.get("input_complete_at") else None
        if (complete is None or complete.tzinfo is None or captured < complete
                or complete.tz_convert("America/New_York").date() != bar_at.tz_convert("America/New_York").date()
                or complete <= bar_at):
            raise ValueError("Forecast input session is not complete")
    inserted = store.capture_experience({
        "symbol": forecast["symbol"], "feed": config.feed, "feature_schema": schema(config),
        "bar_at": bar_at.tz_convert("UTC").isoformat(), "captured_at": captured.tz_convert("UTC").isoformat(),
        "model_version": forecast["model_version"], "features": values, "predictions": predictions})
    if inserted:
        update_summary(config, store, forecast["symbol"])
    return inserted


def resolve_outcomes(config, store, symbol, bars, now=None):
    """Resolve only complete target horizons, without replacing recorded inputs."""
    now = pd.Timestamp(now or utcnow())
    if config.intraday:
        return resolve_intraday(config, store, symbol, bars, now)
    zone = "America/New_York"
    local_today = now.tz_convert(zone).date()
    completed = bars[bars.index.tz_convert(zone).date < local_today]
    changed = 0
    for record in store.experience(symbol, config.feed, schema(config), status="pending"):
        at = pd.Timestamp(record["bar_at"])
        if at not in completed.index:
            if len(completed) and at < completed.index[0]:
                changed += store.resolve_experience(record["id"], "excluded", {"reason": "Input bar aged out before its outcome could be resolved"})
            continue
        index = completed.index.get_loc(at)
        if index + 2 >= len(completed):
            continue
        entry_bar, exit_bar = completed.iloc[index + 1], completed.iloc[index + 2]
        entry_day = completed.index[index + 1].tz_convert("America/New_York").date()
        entry_at = late = pd.Timestamp(datetime.combine(entry_day, time(9, 30), tzinfo=ZoneInfo("America/New_York")))
        if pd.Timestamp(record["captured_at"]) >= late:
            changed += store.resolve_experience(record["id"], "excluded", {"reason": "Prediction was recorded after its target session opened"})
            continue
        prices = [float(entry_bar.open), float(exit_bar.open), float(entry_bar.low), float(entry_bar.high)]
        if not all(math.isfinite(v) and v > 0 for v in prices):
            # Leave unresolved so a later valid provider response can repair the data.
            continue
        entry, exit_price, low, high = prices
        changed += store.resolve_experience(record["id"], "resolved", {
            "target": exit_price / entry - 1, "entry": entry, "exit": exit_price,
            "low_next": low, "high_next": high,
            "entry_at": entry_at.tz_convert("UTC").isoformat(),
            "label_end": completed.index[index + 2].isoformat(),
            "source": "Alpaca completed adjusted daily bars", "feed": config.feed})
    update_summary(config, store, symbol)
    return changed


def resolve_intraday(config, store, symbol, bars, now):
    completed = bars[bars.index + pd.Timedelta(minutes=5) <= now]
    frame = dataset(completed, intraday=True)
    changed = 0
    for record in store.experience(symbol, config.feed, schema(config), status="pending"):
        at = pd.Timestamp(record["bar_at"])
        if at not in frame.index:
            if len(completed) and completed.index[-1] >= at + pd.Timedelta(minutes=20):
                changed += store.resolve_experience(record["id"], "excluded", {"reason": "Missing contiguous same-session horizon"})
            continue
        row = frame.loc[at]
        # Accept only forecasts observed promptly after input close; future returns
        # remain diagnostic close-to-close labels, not actual execution prices.
        delay = (pd.Timestamp(record["captured_at"]) - (at + pd.Timedelta(minutes=5))).total_seconds()
        if not 0 <= delay <= 90:
            changed += store.resolve_experience(record["id"], "excluded", {"reason": "Forecast arrived too late for its intraday horizon"})
            continue
        changed += store.resolve_experience(record["id"], "resolved", {
            **{k: float(row[k]) for k in ("target", "entry", "low_next", "high_next")},
            "label_end": row.label_end.isoformat(), "source": "Alpaca completed five-minute bars", "feed": config.feed})
    update_summary(config, store, symbol)
    return changed


def update_summary(config, store, symbol):
    records = store.experience(symbol, config.feed, schema(config))
    resolved = [r for r in records if r["status"] == "resolved"]
    scores = {}
    for record in resolved:
        actual = record["outcome"]["target"]
        for name, prediction in record["predictions"].items():
            score = scores.setdefault(name, {"observations": 0, "absolute_error": 0., "direction_matches": 0})
            score["observations"] += 1
            score["absolute_error"] += abs(prediction - actual)
            score["direction_matches"] += int(np.sign(prediction) == np.sign(actual))
    scores = {name: {"observations": s["observations"], "mae": s["absolute_error"] / s["observations"],
                     "direction_accuracy": s["direction_matches"] / s["observations"]} for name, s in scores.items()}
    result = {"captured": len(records), "pending": sum(r["status"] == "pending" for r in records),
              "resolved": len(resolved), "excluded": sum(r["status"] == "excluded" for r in records),
              "scores": scores, "at": utcnow(), "latest": [
                  {"bar_at": r["bar_at"], "model_version": r["model_version"],
                   "expected_return": r["predictions"]["ensemble"], "actual_return": r["outcome"]["target"]}
                  for r in resolved[:5]]}
    store.set(summary_key(config, symbol), result)
    return result


def replay_dataset(config, store, symbol, bars):
    """Replace matching historical rows with frozen experiences, never append copies.

    Predictions are used only for forward scorekeeping; labels are market returns.
    Chronological splitting happens later, so recent outcomes remain out of fitting.
    """
    frame = dataset(bars, intraday=config.intraday)
    frame["sample_weight"] = 1.
    frame["is_feedback"] = False
    replayed = []
    for record in store.experience(symbol, config.feed, schema(config), status="resolved"):
        at = pd.Timestamp(record["bar_at"])
        if at not in frame.index:
            continue
        outcome = record["outcome"]
        # A provider history gap must not silently change a forecast's time horizon.
        if pd.Timestamp(outcome["label_end"]) != frame.loc[at, "label_end"]:
            continue
        frame.loc[at, FEATURES] = [record["features"][name] for name in FEATURES]
        for column in ("target", "entry", "low_next", "high_next"):
            frame.loc[at, column] = outcome[column]
        frame.loc[at, "sample_weight"] = config.feedback_weight
        frame.loc[at, "is_feedback"] = True
        replayed.append({"bar_at": record["bar_at"], "features": record["features"], "outcome": outcome})
    revision = hashlib.sha256(json.dumps(replayed, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
    return frame, revision
