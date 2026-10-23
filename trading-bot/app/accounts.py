"""Separate balances and cash-flow-adjusted returns; combined in one display currency.

Returns start with the first complete funding-history snapshot, not account inception.
The combined return is sum(P&L converted at current reference FX) / sum(net capital
converted at the same FX). It is capital-weighted, not an average of percentages,
and deliberately excludes FX investment performance. No FX conversion is executed.
"""
import hashlib
import math
import threading

import pandas as pd
import requests

from .broker import Alpaca, BrokerError
from .store import utcnow
from .trading212 import Trading212, number


class Accounts:
    def __init__(self, config, store, alpaca=None, trading212=None):
        self.config, self.store = config, store
        self.alpaca = alpaca or Alpaca(config)
        self.trading212 = trading212 or Trading212(config)
        self.session = requests.Session()
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="broker-balances", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)

    def rate(self, source, target, now):
        if source == target:
            return {"rate": 1., "date": str(now.date()), "source": source, "target": target}
        key = f"display_fx:{source}:{target}"
        cached = self.store.get(key)
        if cached and cached.get("checked_day") == str(now.date()):
            return cached
        try:
            response = self.session.get("https://api.frankfurter.dev/v1/latest", params={"base": source, "symbols": target}, timeout=(5, 15))
            response.raise_for_status()
            result = response.json()
            rate = number(result["rates"][target], nonnegative=True)
            day = pd.Timestamp(result["date"], tz="UTC")
            if (str(result.get("base", "")).upper() != source
                    or not 0 <= (now.normalize() - day).days <= 7 or rate <= 0):
                raise ValueError()
            cached = {"rate": rate, "date": str(day.date()), "source": source, "target": target, "checked_day": str(now.date())}
            self.store.set(key, cached)
            return cached
        except (requests.RequestException, ValueError, KeyError, BrokerError):
            if cached and 0 <= (now.date() - pd.Timestamp(cached["date"]).date()).days <= 7:
                return cached
            raise BrokerError("Display FX unavailable; combined balance cannot be calculated") from None

    def observe(self, name, broker, now):
        raw = broker.account()
        code = raw.get("currency", "USD")
        equity, cash = number(raw["equity"], nonnegative=True), number(raw["cash"])
        mode = ("paper" if self.config.paper else "live") if name == "alpaca" else raw["mode"]
        scope = hashlib.sha256(f"{broker.base}:{raw['id']}:{code}".encode()).hexdigest()
        key = "performance_baseline:" + name
        baseline = self.store.get(key)
        if baseline and baseline["scope"] != scope:
            # An account/mode/currency switch must not inherit another account's performance.
            baseline = None
        flow_start = baseline["flow_start"] if baseline else (now.normalize() - pd.Timedelta(days=2)).isoformat()
        snapshot = {"broker": name, "equity": equity, "cash": cash, "currency": code, "mode": mode,
                    "at": now.isoformat(), "status": "connected", "return_fraction": None, "pnl": None,
                    "net_capital": None, "since": baseline["at"] if baseline else None}
        try:
            flows = broker.cash_flows(flow_start)
            if any(row["currency"] != code for row in flows):
                raise BrokerError("Funding in a different currency needs a valued adjustment")
            flow_total = sum(number(row["amount"]) for row in flows)
            if baseline is None:
                baseline = {"scope": scope, "at": now.isoformat(), "equity": equity,
                            "flow_start": flow_start, "flow_total": flow_total}
                self.store.set(key, baseline)
            capital = baseline["equity"] + flow_total - baseline["flow_total"]
            pnl = equity - capital
            snapshot.update(net_capital=capital, pnl=pnl, return_fraction=pnl / capital if capital > 0 else None,
                            since=baseline["at"])
        except (BrokerError, KeyError, TypeError, ValueError):
            snapshot["return_error"] = "Complete funding history required; deposits and withdrawals must not appear as profit"
        self.store.set("broker_balance:" + name, snapshot)
        return snapshot

    def tick(self, now=None):
        now = now or pd.Timestamp.now(tz="UTC")
        targets = [("alpaca", self.alpaca, self.config.configured)]
        if self.config.trading212_enabled:
            targets.append(("trading212", self.trading212, self.config.trading212_configured))
        for name, broker, configured in targets:
            try:
                if not configured:
                    raise BrokerError("API credentials missing")
                self.observe(name, broker, now)
            except Exception:
                previous = self.store.get("broker_balance:" + name) or {}
                self.store.set("broker_balance:" + name, {**previous, "broker": name, "status": "unavailable",
                    "message": "Account unavailable; check API credentials and permissions", "checked_at": now.isoformat()})
        rates = {}
        for name, _, _ in targets:
            row = self.store.get("broker_balance:" + name) or {}
            if row.get("currency"):
                try:
                    rates[row["currency"]] = self.rate(row["currency"], self.config.reporting_currency, now)
                except BrokerError:
                    pass
        self.store.set("display_fx", rates)
        overview = overview_state(self.config, self.store, now)
        if overview["combined"].get("equity") is not None:
            self.store.event("combined_equity", {"equity": overview["combined"]["equity"],
                "currency": self.config.reporting_currency})

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # Cached snapshots age visibly; account telemetry cannot stop risk engines.
            self.stop.wait(60)


def overview_state(config, store, now=None):
    now = now or pd.Timestamp.now(tz="UTC")
    names = ["alpaca", "trading212"] if config.trading212_enabled else ["alpaca"]
    rows, rates = {}, store.get("display_fx", {})
    for name in ["alpaca", "trading212"]:
        row = dict(store.get("broker_balance:" + name) or {})
        if name not in names:
            rows[name] = {"broker": name, "status": "disabled", "equity": None, "return_fraction": None}
            continue
        fresh = (row.get("status") == "connected" and row.get("at")
                 and 0 <= (now - pd.Timestamp(row["at"])).total_seconds() <= 180)
        row.update(broker=name, stale=not bool(fresh), status=row.get("status", "waiting"))
        if not fresh:
            row["return_fraction"] = None
        rows[name] = row
    combined = {"equity": None, "return_fraction": None, "currency": config.reporting_currency,
                "status": "waiting", "message": "Waiting for fresh balances from all enabled brokers"}
    selected = [rows[name] for name in names]
    if all(not r["stale"] and r.get("equity") is not None for r in selected):
        if len({r["mode"] for r in selected}) != 1:
            combined.update(status="mixed_modes", message="Demo and live balances are shown separately and are not added together")
        else:
            converted = []
            for row in selected:
                fx = rates.get(row["currency"])
                if row["currency"] == config.reporting_currency:
                    fx = {"rate": 1., "date": str(now.date())}
                if (not fx or not 0 <= (now.date() - pd.Timestamp(fx["date"]).date()).days <= 7
                        or not isinstance(fx.get("rate"), (int, float)) or not math.isfinite(fx["rate"]) or fx["rate"] <= 0):
                    combined.update(status="missing_fx", message="Waiting for display exchange rates; currencies are not added without conversion")
                    break
                converted.append((row, fx))
            else:
                total = sum(r["equity"] * fx["rate"] for r, fx in converted)
                complete = all(r.get("net_capital") is not None and r.get("pnl") is not None for r, _ in converted)
                capital = sum(r["net_capital"] * fx["rate"] for r, fx in converted) if complete else None
                pnl = sum(r["pnl"] * fx["rate"] for r, fx in converted) if complete else None
                combined.update(equity=total, pnl=pnl, net_capital=capital,
                    return_fraction=pnl / capital if capital is not None and capital > 0 else None,
                    status="connected", mode=selected[0]["mode"], message="Reference FX for display only; no currency conversion is executed",
                    fx_dates=sorted({fx["date"] for _, fx in converted}))
    return {"combined": combined, "alpaca": rows["alpaca"], "trading212": rows["trading212"],
            "return_basis": "Cash-flow-adjusted P&L / net capital since tracking began; combined at current reference FX, excluding FX performance"}
