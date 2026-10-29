"""Off-the-shelf, CPU-only scikit-learn regressors with chronological validation."""
import os
import hashlib
import json
import uuid
import joblib
import numpy as np
import pandas as pd
import time
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from threadpoolctl import threadpool_limits

from .broker import Alpaca
from .features import FEATURES, FEATURE_SCHEMA, features
from .feedback import replay_dataset, resolve_outcomes
from .store import store_for_config, utcnow
from .universe import active_symbols

MODEL_NAMES = {"ridge": "Ridge regression", "extra_trees": "Extra Trees", "hist_gradient": "Histogram gradient boosting"}


def strategy_signature(config):
    settings = {"schema": 3, "ensemble": "two_of_three_median", "feature_schema": FEATURE_SCHEMA, "features": FEATURES,
                "threshold": config.threshold, "cost_bps": config.cost_bps,
                "stop_loss": config.effective_stop, "take_profit": config.effective_take_profit, "feed": config.feed,
                "feedback_weight": config.feedback_weight, "validation_bars": config.validation_bars}
    if config.intraday:
        settings.update(mode="daytrade", bar_minutes=5, horizon_bars=3, intraday_schema=1)
    if config.broker_name != "alpaca":
        settings.update(broker=config.broker_name, source=config.trading212_data_source,
                        validation_schema=2, validation_votes=config.trading212_min_votes,
                        min_trades=config.min_trades, max_drawdown=config.max_drawdown)
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]


def estimator(name):
    if name == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=100))
    if name == "extra_trees":
        return ExtraTreesRegressor(n_estimators=96, max_depth=6, min_samples_leaf=20, n_jobs=1, random_state=42)
    return HistGradientBoostingRegressor(max_iter=80, max_leaf_nodes=10, min_samples_leaf=25,
                                        learning_rate=.04, l2_regularization=10, early_stopping=False, random_state=42)


def fit_model(name, frame):
    model = estimator(name)
    weights = frame.sample_weight.to_numpy()
    if name == "ridge":
        model.fit(frame[FEATURES], frame.target, standardscaler__sample_weight=weights, ridge__sample_weight=weights)
    else:
        model.fit(frame[FEATURES], frame.target, sample_weight=weights)
    return model


def evaluate(frame, predictions, config):
    if config.intraday:
        # Evaluate non-overlapping opportunities; never compound simultaneous bets.
        selected, until = [], None
        for i, (at, row) in enumerate(frame.iterrows()):
            if until is None or at + pd.Timedelta(minutes=5) >= until:
                selected.append(i)
                until = row.label_end
        predictions = np.asarray(predictions)[selected]
        frame = frame.iloc[selected]
    active = np.asarray(predictions) > config.threshold
    gross = frame.target.to_numpy().copy()
    # Conservative same-bar ambiguity: the stop wins if both stop and target were touched.
    take = frame.high_next.to_numpy() >= frame.entry.to_numpy() * (1 + config.effective_take_profit)
    stop = frame.low_next.to_numpy() <= frame.entry.to_numpy() * (1 - config.effective_stop)
    gross[take] = config.effective_take_profit
    gross[stop] = -config.effective_stop
    net = np.where(active, gross - 2 * config.cost_bps / 10000, 0.)
    curve = np.r_[1., np.cumprod(1 + net)]
    dd = 1 - curve / np.maximum.accumulate(curve)
    risk_returns = net
    if config.intraday:
        risk_returns = pd.Series(1 + net, index=frame.index).groupby(frame.index.tz_convert("America/New_York").date).prod().to_numpy() - 1
    std = float(np.std(risk_returns))
    return {"net_return": float(curve[-1] - 1), "max_drawdown": float(dd.max()),
            "sharpe": float(np.mean(risk_returns) / std * np.sqrt(252)) if std > 1e-12 else 0.,
            "active_days": len(set(frame.index[active].tz_convert("America/New_York").date)) if config.intraday else int(active.sum()),
            "active_trades": int(active.sum()), "observations": len(frame),
            "benchmark_return": float(np.prod(1 + frame.target.to_numpy()) * (1 - 2 * config.cost_bps / 10000) - 1),
            "mae": float(np.mean(np.abs(np.asarray(predictions) - frame.target.to_numpy()))),
            "start": frame.index[0].isoformat(), "end": frame.index[-1].isoformat()}


def passes(metrics, config):
    return (metrics["active_days"] >= config.min_trades and metrics["net_return"] > 0
            and metrics["max_drawdown"] <= config.max_drawdown)


def consensus_predictions(predictions, config):
    values = np.asarray(predictions)
    votes = config.trading212_min_votes if config.broker_name == "trading212" else 2
    agrees = (values > config.threshold).sum(axis=0) >= votes
    return np.where(agrees, np.median(values, axis=0), 0.)


def validation_report(metrics, wf_return, config):
    checks = {"trades": metrics["active_days"] >= config.min_trades,
              "return": metrics["net_return"] > 0,
              "drawdown": metrics["max_drawdown"] <= config.max_drawdown,
              "walk_forward": bool(wf_return > 0)}
    reasons = []
    if not checks["trades"]:
        reasons.append(f"Only {metrics['active_days']} validation trades; at least {config.min_trades} required")
    if not checks["return"]:
        reasons.append(f"Validation return after costs is {metrics['net_return']:.2%}; must be positive")
    if not checks["drawdown"]:
        reasons.append(f"Validation drawdown {metrics['max_drawdown']:.2%} exceeds {config.max_drawdown:.2%}")
    if not checks["walk_forward"]:
        reasons.append(f"Mean walk-forward return after costs is {wf_return:.2%}; must be positive")
    return {"passed": all(checks.values()), "checks": checks, "reasons": reasons,
            "metrics": metrics, "walk_forward_return": float(wf_return),
            "minimum_trades": config.min_trades, "maximum_drawdown": config.max_drawdown,
            "minimum_votes": config.trading212_min_votes if config.broker_name == "trading212" else 2}


def load_bundle(config, meta):
    # Only internally written checkpoints; never accept uploads/pickle from an API.
    return joblib.load(config.data_dir / "models" / meta["file"])


def train_symbol(config, store, symbol, bars):
    resolve_outcomes(config, store, symbol, bars)
    frame, feedback_revision = replay_dataset(config, store, symbol, bars)
    if len(frame) < 300:
        raise ValueError(f"{symbol}: need at least 350 complete bars; received {len(bars)}")
    holdout_size = 390 if config.intraday else config.validation_bars
    if len(frame) < holdout_size + 250:
        raise ValueError(f"{symbol}: insufficient bars for training and separate holdout")
    split = len(frame) - holdout_size
    holdout = frame.iloc[split:]
    train = frame.iloc[:split]
    train = train[train.label_end < holdout.index[0]]
    feedback = {"available_rows": int(frame.is_feedback.sum()), "train_rows": int(train.is_feedback.sum()),
                "validation_rows": int(holdout.is_feedback.sum()),
                "purged_rows": int(frame.is_feedback.sum() - train.is_feedback.sum() - holdout.is_feedback.sum()),
                "sample_weight": config.feedback_weight}
    key = f"model:{symbol}"
    old_meta = store.get(key)
    input_signature = bars.attrs.get("source_signature")
    if old_meta and old_meta.get("strategy_signature") != strategy_signature(config):
        old_meta = None
    if old_meta and input_signature and old_meta.get("input_signature") != input_signature:
        old_meta = None
    if (old_meta and old_meta["data_through"] == bars.index[-1].isoformat()
            and old_meta.get("feedback_revision") == feedback_revision):
        store.event("training", {"symbol": symbol, "message": "No new bars or resolved feedback; keeping current model"})
        return
    fitted, reports, predictions = {}, {}, []
    splits = list(TimeSeriesSplit(n_splits=3, gap=4 if config.intraday else 2).split(train))
    fold_predictions = [[] for _ in splits]
    for name in MODEL_NAMES:
        store.set("training", {"status": "running", "symbol": symbol, "model": MODEL_NAMES[name], "at": utcnow()})
        folds = []
        for fold_index, (tr, val) in enumerate(splits):
            model = fit_model(name, train.iloc[tr])
            values = model.predict(train.iloc[val][FEATURES])
            folds.append(evaluate(train.iloc[val], values, config))
            fold_predictions[fold_index].append(values)
        model = fit_model(name, train)
        pred = model.predict(holdout[FEATURES])
        fitted[name] = model
        predictions.append(pred)
        reports[name] = {"walk_forward": folds, "holdout": evaluate(holdout, pred, config)}
        store.event("training", {"symbol": symbol, "model": MODEL_NAMES[name],
            "message": f"Walk-forward and holdout evaluated; {feedback['train_rows']} resolved local examples used in fitting",
            **reports[name]["holdout"]})
    metrics = evaluate(holdout, consensus_predictions(predictions, config), config)
    ensemble_folds = [evaluate(train.iloc[val], consensus_predictions(values, config), config)
                      for (_, val), values in zip(splits, fold_predictions)]
    wf_return = (np.mean([f["net_return"] for f in ensemble_folds]) if config.broker_name == "trading212" else
                 np.mean([np.mean([f["net_return"] for f in report["walk_forward"]]) for report in reports.values()]))
    validation = validation_report(metrics, wf_return, config)
    eligible = passes(metrics, config) and wf_return > 0
    incumbent_metrics = None
    if old_meta and old_meta.get("eligible"):
        old = load_bundle(config, old_meta)
        if old["trained_through"] >= holdout.index[0].isoformat():
            eligible = False
        else:
            ps = np.asarray([m.predict(holdout[FEATURES]) for m in old["models"].values()])
            incumbent_metrics = evaluate(holdout, consensus_predictions(ps, config), config)
            eligible = eligible and metrics["net_return"] > incumbent_metrics["net_return"] + .001
    version = uuid.uuid4().hex[:12]
    file_prefix = f"daytrade-{symbol}" if config.intraday else symbol
    meta = {"version": version, "file": f"{file_prefix}-{version}.joblib", "symbol": symbol,
            "created_at": utcnow(), "data_through": bars.index[-1].isoformat(),
            "trained_through": train.label_end.max().isoformat(), "metrics": metrics,
            "reports": reports, "eligible": bool(eligible), "incumbent": incumbent_metrics,
            "validation": validation, "ensemble_walk_forward": ensemble_folds,
            "strategy_signature": strategy_signature(config), "feedback": feedback, "feedback_revision": feedback_revision}
    if input_signature:
        meta["input_signature"] = input_signature
    store.set(f"candidate:{symbol}", meta)
    if eligible or not old_meta:
        # Retain a research model even when the first candidate fails. It cannot trade.
        path = config.data_dir / "models"
        path.mkdir(exist_ok=True)
        target = path / meta["file"]
        joblib.dump({"models": fitted, "trained_through": meta["trained_through"]}, str(target) + ".tmp")
        os.replace(str(target) + ".tmp", target)
        store.set(key, meta)
        # Keep at most three versions per symbol, including the active model.
        for obsolete in sorted(path.glob(f"{file_prefix}-*.joblib"), key=lambda p: p.stat().st_mtime, reverse=True)[3:]:
            obsolete.unlink(missing_ok=True)
    store.event("training", {"symbol": symbol, "message": "Promoted validated candidate" if eligible else "Candidate failed promotion gate; retained incumbent or research model", "metrics": metrics, "feedback": feedback})


def train_worker(config, symbols=None):
    store = store_for_config(config)
    try:
        broker = Alpaca(config)
        failures, reasons = [], {}
        targets = list(symbols if symbols is not None else active_symbols(config, store))
        with threadpool_limits(limits=2):
            for symbol in targets:
                try:
                    store.set(f"training_attempt:{symbol}", time.time())
                    train_symbol(config, store, symbol, broker.bars(symbol))
                except Exception as exc:
                    failures.append(symbol)
                    reasons[symbol] = str(exc)[:160]
                    store.event("training", {"symbol": symbol, "message": str(exc)[:300]})
        # Stocks that cannot be trained (e.g. too little history) are skipped; the others keep trading.
        message = (f"{len(failures)} of {len(targets)} stocks not trained, e.g. {failures[0]}: {reasons[failures[0]]}"
                   if failures else None)
        store.set("training", {"status": "partial failure" if failures else "complete", "failed_symbols": failures,
                               "message": message, "at": utcnow()})
    except Exception as exc:
        store.set("training", {"status": "failed", "message": str(exc)[:300], "at": utcnow()})


def predict(config, meta, bars):
    if config.broker_name == "trading212" and meta.get("input_signature") != bars.attrs.get("source_signature"):
        raise ValueError("Listing or data mapping changed; retrain this stock before using its model")
    row = features(bars).tail(1)
    if row.empty or row.index[-1] != bars.index[-1]:
        raise ValueError("Latest completed bar cannot produce valid features")
    bundle = load_bundle(config, meta)
    outputs = []
    with threadpool_limits(limits=2):
        for name, model in bundle["models"].items():
            value = float(model.predict(row[FEATURES])[0])
            # Local sensitivity, not a hidden reasoning trace or a calibrated probability.
            baseline = row.copy()
            impacts = []
            neutral = {"volume_ratio": 1., "rsi_14d": .5}
            for feature in FEATURES:
                changed = baseline.copy()
                changed[feature] = neutral.get(feature, 0.)
                impact = value - float(model.predict(changed[FEATURES])[0])
                impacts.append({"feature": feature, "value": float(row[feature].iloc[0]), "impact": impact})
            outputs.append({"id": name, "name": MODEL_NAMES[name], "expected_return": value,
                            "signal": "BUY" if value > config.threshold else "SELL" if value < -config.threshold else "HOLD",
                            "drivers": sorted(impacts, key=lambda d: abs(d["impact"]), reverse=True)[:3],
                            "validation": meta["reports"][name]["holdout"]})
    consensus = float(np.median([p["expected_return"] for p in outputs]))
    buys = sum(p["signal"] == "BUY" for p in outputs)
    sells = sum(p["signal"] == "SELL" for p in outputs)
    signal = "BUY" if buys >= 2 else "SELL" if sells >= 2 else "HOLD"
    return {"symbol": meta["symbol"], "at": utcnow(), "bar_at": row.index[-1].isoformat(),
            "features": {name: float(row[name].iloc[0]) for name in FEATURES},
            "model_version": meta["version"], "eligible": meta["eligible"], "models": outputs,
            "validation": meta.get("validation"),
            "forecast_signal": signal, "expected_return": consensus, "agreement": max(buys, sells, 3 - buys - sells),
            "ensemble_method": "median", "target_session": bars.attrs.get("target_session"),
            "input_complete_at": bars.attrs.get("input_complete_at"),
            "market_timezone": bars.attrs.get("market_timezone", "America/New_York"),
            "source_signature": bars.attrs.get("source_signature"),
            "last_close": float(bars.close.iloc[-1]), "threshold": config.threshold,
            "horizon": "next 15 minutes from completed five-minute close" if config.intraday
 else "next session open to the following session open",
            "trading_mode": config.trading_mode}
