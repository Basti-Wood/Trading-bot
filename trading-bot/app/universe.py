"""Bounded, batched discovery; expensive forecasts run only on the shortlist."""
import math
import re
import time
import pandas as pd
from .store import utcnow


def active_symbols(config, store):
    if config.universe_mode == "watchlist":
        return list(config.symbols)
    return store.get("universe", {}).get("symbols", [])


def monitored_symbols(config, store, positions=(), orders=()):
    # A rotated-out position must keep its stops, time exits and news coverage.
    held = [p["symbol"] for p in positions if store.get(f"owned:{p['symbol']}", False)]
    pending = [o["symbol"] for o in orders if o.get("client_order_id", "").startswith("ensemble-")
               and (not config.coordinated or store.owns_order(o))]
    registered = store.owned_symbols() if config.coordinated else []
    return list(dict.fromkeys([*held, *pending, *registered, *active_symbols(config, store)]))


def screen(config, symbol, snapshot, now):
    try:
        bar = snapshot.get("prevDailyBar") or {}
        today = snapshot.get("dailyBar") or {}
        price = float((snapshot.get("latestTrade") or {}).get("p") or today.get("c") or bar["c"])
        previous = float(bar["c"])
        dollar_volume = previous * float(bar["v"])
        age = (now - pd.Timestamp(bar["t"])).total_seconds()
        if not all(math.isfinite(x) and x > 0 for x in (price, previous, dollar_volume)):
            return None
        if not 0 <= age <= 7 * 86400 or price < config.scan_min_price or dollar_volume < config.scan_min_dollar_volume:
            return None
        change = price / previous - 1
        relative_volume = min(5., max(0., float(today.get("v", 0)) / float(bar["v"])))
        score = math.log10(dollar_volume) + min(abs(change), .20) * 10 + relative_volume
        if not math.isfinite(score):
            return None
        return {"symbol": symbol, "price": price, "dollar_volume": dollar_volume,
                "change": change, "relative_volume": relative_volume, "score": score}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def scan(config, store, broker, stop):
    assets = broker.assets()
    symbols = sorted({a["symbol"] for a in assets if a.get("tradable") and a.get("status") == "active"
                      and a.get("class") == "us_equity" and a.get("exchange") in {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "NYSEARCA"}
                      and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", a.get("symbol", ""))})
    ranked, covered, failures = [], 0, 0
    now = pd.Timestamp.now(tz="UTC")
    for offset in range(0, len(symbols), 100):
        if stop.is_set():
            return
        batch = symbols[offset:offset + 100]
        try:
            snapshots = broker.snapshots(batch)
            for symbol in batch:
                if symbol not in snapshots:
                    continue
                covered += 1
                candidate = screen(config, symbol, snapshots[symbol], now)
                if candidate:
                    ranked.append(candidate)
        except Exception:
            failures += 1
        store.set("scan_status", {"status": "scanning", "checked": min(offset + 100, len(symbols)),
                                  "total": len(symbols), "at": utcnow()})
        # Leave capacity for prices, orders, training and news on shared API limits.
        if stop.wait(1):
            return
    ranked.sort(key=lambda row: (-row["score"], row["symbol"]))
    selected = ranked[:config.scan_candidates]
    # Fail closed: retain the visible previous selection but block its new entries.
    if failures or not covered:
        store.set("scan_status", {"status": "unavailable", "message": "Incomplete scan; new market-universe entries paused",
                                  "failed_batches": failures, "at": utcnow()})
        return
    store.set("universe", {"symbols": [row["symbol"] for row in selected], "candidates": selected,
                           "total_assets": len(symbols), "covered": covered, "qualified": len(ranked),
                           "feed": config.feed, "at": utcnow(), "scanned_at": time.time()})
    store.set("scan_status", {"status": "ready", "at": utcnow()})


def entries_ready(config, store):
    if config.universe_mode == "watchlist":
        return True
    state = store.get("universe", {})
    return (store.get("scan_status", {}).get("status") in {"ready", "scanning"}
            and state.get("feed") == config.feed
            and 0 <= time.time() - state.get("scanned_at", 0) <= config.scan_interval_minutes * 120)
