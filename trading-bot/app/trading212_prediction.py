"""Three local daily models for the non-US book, with no Alpaca data dependency."""
import multiprocessing as mp
import threading
import time

import pandas as pd
import psutil
from threadpoolctl import threadpool_limits

from .feedback import capture_forecast
from .models import train_symbol, predict, strategy_signature
from .prediction_quality import forecast_quality
from .research import combine
from .store import store_for_config, utcnow


def train_frames_worker(config, frames):
    """Spawned CPU-only worker. Inputs are validated snapshots, not new broker requests."""
    store = store_for_config(config)
    failures = {}
    with threadpool_limits(limits=2):
        for symbol, bars in frames.items():
            try:
                store.set(f"training_attempt:{symbol}", time.time())
                store.set(f"training_attempt_signature:{symbol}",
                          strategy_signature(config) + ":" + str(bars.attrs.get("source_signature")))
                train_symbol(config, store, symbol, bars)
            except Exception as exc:
                failures[symbol] = str(exc)[:200]
                store.event("training", {"symbol": symbol, "message": failures[symbol]})
    store.set("training", {"status": "partial failure" if failures else "complete", "failed_symbols": list(failures),
        "errors": failures, "message": f"{len(frames) - len(failures)} / {len(frames)} stocks trained", "at": utcnow()})


class PredictionWorker:
    def __init__(self, config, store, loop_store):
        self.config, self.store, self.loop_store = config, store, loop_store
        self.frames = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.trainer = None
        self.training_started = 0
        self.store.set("entries_enabled", False)
        self.store.set("training", {"status": "waiting for non-US daily data"})

    def set_frames(self, frames):
        with self.lock:
            self.frames = {s: frame.copy(deep=True) for s, frame in frames.items()}
        if frames and (self.store.get("training") or {}).get("status") == "waiting for non-US daily data":
            cached = all((meta := self.store.get(f"model:{s}") or {}) and meta.get("input_signature") == frame.attrs.get("source_signature")
                         for s, frame in frames.items())
            self.store.set("training", {"status": "ready" if cached else "queued",
                "message": "Cached models loaded" if cached else "Daily data ready; waiting for the shared training slot", "at": utcnow()})

    def training_tick(self, allow_start=True):
        if self.trainer is not None:
            if self.trainer.is_alive():
                try:
                    rss = psutil.Process(self.trainer.pid).memory_info().rss / 1024 ** 2
                except psutil.NoSuchProcess:
                    return
                if rss > self.config.training_memory_mb or time.monotonic() - self.training_started > self.config.training_timeout:
                    self.trainer.terminate()
                    self.trainer.join(timeout=5)
                    self.store.set("training", {"status": "resource limit reached", "at": utcnow()})
                return
            self.trainer.join(timeout=1)
            if self.trainer.exitcode:
                self.store.set("training", {"status": "worker failed", "at": utcnow()})
            self.trainer = None
            # Publish forecasts immediately after fitting, rather than waiting another 30 minutes.
            self.loop_store.set("retest_requested", True)
        if not allow_start or self.stop.is_set() or not self.config.configured:
            return
        now = time.time()
        if now - self.store.get("last_training_attempt", 0) < 60:
            return
        with self.lock:
            frames = dict(self.frames)
        requested = self.store.get("train_requested", False)
        due = now - self.store.get("last_full_training", 0) > self.config.retrain_hours * 3600
        targets = {}
        for symbol, frame in frames.items():
            meta = self.store.get(f"model:{symbol}") or {}
            changed = (meta.get("strategy_signature") != strategy_signature(self.config)
                       or meta.get("input_signature") != frame.attrs.get("source_signature"))
            expired = not meta.get("created_at") or (pd.Timestamp.now(tz="UTC") - pd.Timestamp(meta["created_at"])).total_seconds() > self.config.model_max_age_hours * 3600
            attempt_signature = strategy_signature(self.config) + ":" + str(frame.attrs.get("source_signature"))
            new_settings = self.store.get(f"training_attempt_signature:{symbol}") != attempt_signature
            if (requested or due or changed or expired) and (requested or new_settings or now - self.store.get(f"training_attempt:{symbol}", 0) >= 3600):
                targets[symbol] = frame
        if not targets:
            return
        self.store.set("train_requested", False)
        self.store.set("last_training_attempt", now)
        if due:
            self.store.set("last_full_training", now)
        self.store.set("training", {"status": "starting", "symbols": list(targets), "at": utcnow()})
        self.trainer = mp.get_context("spawn").Process(target=train_frames_worker, args=(self.config, targets), daemon=True)
        self.trainer.start()
        self.training_started = time.monotonic()

    def forecast(self, symbol, bars):
        meta = self.store.get(f"model:{symbol}")
        if not meta:
            return
        forecast = predict(self.config, meta, bars)
        capture_forecast(self.config, self.store, forecast)
        self.store.set(f"prediction:{symbol}", forecast)
        self.store.set(f"prediction_error:{symbol}", None)

    def decision(self, symbol, now):
        forecast = self.store.get(f"prediction:{symbol}")
        if not forecast:
            error = ((self.store.get(f"prediction_error:{symbol}") or {}).get("message")
                     or (self.store.get("training") or {}).get("errors", {}).get(symbol))
            return {"symbol": symbol, "signal": "HOLD", "models": [], "expected_return": 0.,
                    "reason": error or "Waiting for this stock's first trained model"}
        quality = forecast_quality(self.config, self.store.get(f"model:{symbol}"), forecast, now)
        if not quality["ready"]:
            return {**forecast, "signal": "HOLD", "quality": quality, "reason": quality["reason"]}
        # No news source covers these listings, so the impossible news vote is left out.
        action, reason = combine({**forecast, "news": False}, None, now=now.to_pydatetime(), min_votes=self.config.trading212_min_votes)
        if not forecast.get("eligible") and (forecast.get("validation") or {}).get("reasons"):
            reason += "; " + "; ".join(forecast["validation"]["reasons"])
        return {**forecast, "signal": action, "quality": quality, "reason": reason,
                "votes": {m["name"]: m["signal"] for m in forecast["models"]}}

    def shutdown(self):
        self.stop.set()
        if self.trainer is not None:
            if self.trainer.is_alive():
                self.trainer.terminate()
            self.trainer.join(timeout=5)
            self.trainer = None
