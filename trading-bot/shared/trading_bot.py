"""
shared/trading_bot.py — Common Trading Bot Loop (Prompt 2)

Used by bot_crypto, bot_stocks, bot_etf. Every cycle:
  1. Reload dashboard controls (kill switches / strategy toggles)
  2. For each symbol: engineer features -> ML predict -> fallback -> trade
  3. Check short positions (3% force-close), short_hold_days expiry
  4. Circuit breaker: DEMOTE -> promote challenger, PAUSE -> sleep

Shorting only happens when ALL of these agree:
  dna.short_enabled (GA) AND dashboard global switch AND per-asset switch.
"""

import csv
import json
import os
import time
from datetime import datetime

from shared.data.fetcher import get_recent_candles, POOL_CONFIG
from shared.data.preprocessor import engineer_features, compute_atr
from shared.evolution.dna import AgentDNA
from shared.strategy.ml_model import MLPredictor
from shared.strategy.fallback import FallbackStrategy
from shared.risk.manager import RiskManager
from shared.risk.position_tracker import PositionTracker
from shared.monitoring.circuit_breaker import (
    CircuitBreaker, check_short_positions, promote_challenger)
from shared.monitoring import controls as ctl
from shared.monitoring.ledger import PerformanceLedger, BotRecord
from shared.broker import alpaca as broker

CHAMPIONS_DIR = os.environ.get("CHAMPIONS_DIR", "champions")
LOGS_DIR = os.environ.get("LOGS_DIR", "logs")
CYCLE_SECONDS = 60


class TradingBot:
    def __init__(self, asset_class: str, symbols: list):
        self.asset_class = asset_class
        self.symbols = symbols
        self.pool_config = POOL_CONFIG[asset_class]
        self.tracker = PositionTracker()
        self.breaker = CircuitBreaker()
        self.ledger = PerformanceLedger()
        self.fallback = FallbackStrategy()
        self.dna: AgentDNA | None = None
        self.ml: MLPredictor | None = None
        self.pol_api_key = os.environ.get("QUIVER_API_KEY")
        self._warned_no_broker = False
        self._warned_bad_equity = False
        self._warned_no_champion = False
        self._trades_path = os.path.join(LOGS_DIR, f"trades_{self.asset_class}.csv")
        self._load_champion()

    # ------------------------------------------------------------------

    def _log_trade(self, symbol: str, side: str, price: float,
                   notional_usd: float = 0.0, qty: float = 0.0,
                   confidence: float = 0.0) -> None:
        """Append a trade record to the CSV log (shared volume)."""
        os.makedirs(LOGS_DIR, exist_ok=True)
        write_header = not os.path.exists(self._trades_path)
        with open(self._trades_path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["timestamp", "asset_class", "symbol", "side",
                            "price", "notional_usd", "qty", "confidence"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"),
                self.asset_class, symbol, side,
                round(price, 4), round(notional_usd, 2), round(qty, 6),
                round(confidence, 4),
            ])

    # ------------------------------------------------------------------

    def _write_prediction_state(self, symbol: str, signal: str,
                                 confidence: float) -> None:
        """Persist last prediction for dashboard network visualization."""
        if not self.ml:
            return
        state = {
            "asset_class": self.asset_class,
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "signal": signal,
            "confidence": round(confidence, 4),
            "prob_up": round(self.ml.last_proba_up, 4),
            "prob_down": round(self.ml.last_proba_down, 4),
            "features": self.ml.last_features,
            "dna": {
                "hidden_size": self.dna.hidden_size,
                "num_layers": self.dna.num_layers,
                "seq_len": self.dna.seq_len,
                "dropout": self.dna.dropout,
                "temperature": self.dna.temperature,
            },
        }
        os.makedirs(LOGS_DIR, exist_ok=True)
        path = os.path.join(LOGS_DIR, f"predictions_{self.asset_class}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)

    def _load_champion(self) -> None:
        """Load champion DNA + LSTM weights from the shared volume."""
        dna_path = os.path.join(CHAMPIONS_DIR, f"{self.asset_class}_champion.json")
        model_path = os.path.join(CHAMPIONS_DIR, f"{self.asset_class}_model.pt")
        try:
            with open(dna_path) as f:
                self.dna = AgentDNA.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            if not self._warned_no_champion:
                print(f"[{self.asset_class}] No champion DNA yet — waiting for evolver.")
                self._warned_no_champion = True
            self.dna = None
            return

        self._warned_no_champion = False

        self.risk = RiskManager(self.dna, self.pool_config)
        self.ml = MLPredictor(self.dna, self.asset_class)
        if os.path.exists(model_path):
            try:
                self.ml.load(model_path)
            except Exception as exc:
                print(f"[{self.asset_class}] Model load failed: {exc} — fallback only.")
        print(f"[{self.asset_class}] Champion loaded | "
              f"temp={self.dna.temperature:.2f} short={self.dna.short_enabled}")

    def _demote(self) -> None:
        """Circuit breaker tripped: close everything, promote challenger."""
        print(f"[{self.asset_class}] CIRCUIT BREAKER TRIPPED — demoting champion.")
        self._close_all()
        self.ledger.log(BotRecord(
            asset_class=self.asset_class,
            generation=self.dna.generation if self.dna else 0,
            promoted_at=datetime.now(),
            demoted_at=datetime.now(),
            sharpe_live=self.dna.last_sharpe if self.dna else 0.0,
            reason_demoted="5% drawdown",
        ))
        if ctl.auto_promote_allowed(self.asset_class):
            if promote_challenger(self.asset_class):
                self._load_champion()
                self.breaker.reset()
        else:
            print(f"[{self.asset_class}] auto_promote disabled — trading halted "
                  f"until manual promotion from dashboard.")
            self.dna = None   # halt until new champion appears

    def _close_all(self) -> None:
        for ticker in list(self.tracker.positions):
            pos = self.tracker.positions[ticker]
            closed = (broker.close_long(ticker, pos.qty)
                      if pos.side == "LONG"
                      else broker.close_short(ticker, pos.qty))
            if closed:
                self.tracker.close(ticker)
            else:
                print(f"[{self.asset_class}] close {ticker} failed; position retained")

    # ------------------------------------------------------------------

    def _handle_symbol(self, symbol: str, controls: dict,
                       portfolio_value: float, prices: dict) -> None:
        df = get_recent_candles(symbol, days=self.dna.seq_len + 90)
        if df.empty or len(df) < 55:
            return
        feats = engineer_features(
            df, asset_class=self.asset_class, ticker=symbol,
            pol_api_key=self.pol_api_key if self.asset_class == "stocks" else None)
        if feats.empty:
            return
        row = feats.iloc[-1].to_dict()
        prices[symbol] = row["close"]

        # --- signal: ML first, fallback second (both toggleable) ---
        signal, conf = "HOLD", 0.0
        if ctl.strategy_enabled(self.asset_class, "ml", controls) and self.ml:
            self.ml.push(row)
            signal, conf = self.ml.predict()
            # Write prediction state for dashboard network visualization
            self._write_prediction_state(symbol, signal, conf)
        if signal == "HOLD" and ctl.strategy_enabled(self.asset_class, "fallback", controls):
            signal, conf = self.fallback.predict(row)
        if signal == "HOLD":
            return

        atr = compute_atr(df)
        sl = self.risk.stop_loss(row["close"], signal, atr)
        notional_usd = self.risk.position_size_usd(portfolio_value)

        if signal == "BUY":
            if self.tracker.is_short(symbol):
                qty = self.tracker.positions[symbol].qty
                if broker.close_short(symbol, qty):
                    self.tracker.close(symbol)
                    self._log_trade(symbol, "CLOSE_SHORT", row["close"], qty=qty)
            if self.tracker.is_flat(symbol):
                frac = broker.is_fractionable(symbol)
                if broker.open_long(symbol, notional_usd, sl, row["close"]):
                    self.tracker.open_long(symbol, notional_usd, row["close"],
                                           sl, fractional=frac)
                    self._log_trade(symbol, "LONG", row["close"],
                                    notional_usd=notional_usd, confidence=conf)
                    print(f"[{self.asset_class}] LONG {symbol} "
                          f"${notional_usd:.2f} @ {row['close']:.2f} conf={conf:.2f}")

        elif signal == "SELL":
            if self.tracker.is_long(symbol):
                qty = self.tracker.positions[symbol].qty
                if broker.close_long(symbol, qty):
                    self.tracker.close(symbol)
                    self._log_trade(symbol, "CLOSE_LONG", row["close"], qty=qty)
            # Short only if DNA wants it AND dashboard kill switches allow it
            if (self.tracker.is_flat(symbol)
                    and self.dna.short_enabled
                    and ctl.shorting_allowed(self.asset_class, controls)):
                short_val = self.tracker.all_short_value(prices)
                if self.risk.short_exposure_ok(short_val, portfolio_value):
                    qty = broker.open_short(symbol, notional_usd, sl, row["close"])
                    if qty >= 1:
                        self.tracker.open_short(symbol, qty, row["close"], sl)
                        self._log_trade(symbol, "SHORT", row["close"],
                                        notional_usd=notional_usd, qty=qty,
                                        confidence=conf)
                        print(f"[{self.asset_class}] SHORT {symbol} "
                              f"{qty} sh @ {row['close']:.2f} conf={conf:.2f}")
                    else:
                        print(f"[{self.asset_class}] short {symbol} skipped — "
                              f"rounds to 0 whole shares")

    # ------------------------------------------------------------------

    def run(self) -> None:
        print(f"[{self.asset_class}] Bot starting.")
        while True:
            try:
                self._cycle()
            except Exception as exc:
                print(f"[{self.asset_class}] cycle error: {exc}")
            time.sleep(CYCLE_SECONDS)

    def _cycle(self) -> None:
        controls = ctl.load_controls()

        if not broker.is_configured():
            if not self._warned_no_broker:
                reason = broker.unavailable_reason()
                print(f"[{self.asset_class}] Alpaca credentials not configured. "
                      f"Bot is idling until .env is set."
                      f"{f' Reason: {reason}' if reason else ''}")
                self._warned_no_broker = True
            return
        self._warned_no_broker = False

        if self.dna is None:
            self._load_champion()
            if self.dna is None:
                return

        if not ctl.trading_allowed(self.asset_class, controls):
            return   # paused from dashboard — hold existing positions

        portfolio_value = broker.get_portfolio_value()
        if portfolio_value <= 0:
            if not self._warned_bad_equity:
                reason = broker.unavailable_reason()
                msg = (f"[{self.asset_class}] Broker equity is non-positive "
                       f"({portfolio_value:.2f}); skipping cycle.")
                if reason:
                    msg += f" Reason: {reason}"
                print(msg)
                self._warned_bad_equity = True
            return
        self._warned_bad_equity = False
        prices: dict = {}

        for symbol in self.symbols:
            try:
                self._handle_symbol(symbol, controls, portfolio_value, prices)
            except Exception as exc:
                print(f"[{self.asset_class}] {symbol} error: {exc}")

        # Shorts: force-close >3% losers + expired holds (checked EVERY cycle)
        for ticker in check_short_positions(self.tracker, prices,
                                            self.breaker.max_short_loss):
            price = prices.get(ticker, 0)
            qty = self.tracker.positions[ticker].qty
            if broker.close_short(ticker, qty):
                self._log_trade(ticker, "FORCE_CLOSE_SHORT", price, qty=qty)
                print(f"[{self.asset_class}] FORCE-CLOSED short {ticker} (>3% loss)")
                self.tracker.close(ticker)
            else:
                print(f"[{self.asset_class}] force-close {ticker} failed; will retry")
        for ticker in self.tracker.shorts_older_than(self.dna.short_hold_days):
            price = prices.get(ticker, 0)
            qty = self.tracker.positions[ticker].qty
            if broker.close_short(ticker, qty):
                self._log_trade(ticker, "CLOSE_SHORT", price, qty=qty)
                print(f"[{self.asset_class}] Closed aged short {ticker}")
                self.tracker.close(ticker)
            else:
                print(f"[{self.asset_class}] aged-short close {ticker} failed; will retry")

        # Emergency: if global shorting was killed from dashboard, flatten shorts
        if not ctl.shorting_allowed(self.asset_class, controls):
            for ticker in [t for t, p in self.tracker.positions.items()
                           if p.side == "SHORT"]:
                price = prices.get(ticker, 0)
                qty = self.tracker.positions[ticker].qty
                if broker.close_short(ticker, qty):
                    self._log_trade(ticker, "KILL_CLOSE_SHORT", price, qty=qty)
                    print(f"[{self.asset_class}] Kill switch closed short {ticker}")
                    self.tracker.close(ticker)
                else:
                    print(f"[{self.asset_class}] kill-switch close {ticker} failed; will retry")

        status = self.breaker.update(broker.get_portfolio_value())
        if status == "DEMOTE":
            self._demote()
        elif status == "PAUSE":
            print(f"[{self.asset_class}] Daily loss limit hit — pausing 1h.")
            time.sleep(3600)
