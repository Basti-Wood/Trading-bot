"""Crypto on Alpaca: which coins the crypto bots may buy, and when.

Alpaca lists only coins it holds in custody, so smart-contract honeypots (tokens whose contract
blocks selling) cannot reach the bot. What remains are coins that are pegged rather than traded,
meme coins that follow hype, coins too young or too thinly traded to judge, and pumps. Every coin
must pass every rule below, re-checked hourly from Alpaca's own prices. A coin that fails can
still be sold, never bought.
"""
import re
import time

import pandas as pd

from .store import utcnow

PAIR = re.compile(r"[A-Z0-9]{2,15}/USD")
STABLECOINS = {"USDC", "USDT", "USDG", "DAI", "PYUSD", "TUSD", "USDP", "FDUSD", "BUSD", "GUSD", "USDE", "USDS", "RLUSD"}
ASSET_BACKED = {"PAXG", "XAUT"}
MEME_COINS = {"DOGE", "SHIB", "PEPE", "BONK", "WIF", "TRUMP", "FLOKI", "MEME", "POPCAT", "MOG", "BRETT", "PENGU",
              "FARTCOIN", "MELANIA", "TURBO", "NEIRO", "SPX", "MEW", "BOME", "PNUT"}
WINDOW = 90
# A traded coin moves; a 90-day price band this narrow means a peg to the dollar or another asset.
PEG_VARIATION = .02
SCAN_SECONDS = 3600
# Like the stock bots' window after the open: buy only right after the UTC daily candle closes.
ENTRY_HOURS = 2
# The broker stop sells at most this far below its trigger, so a fast drop still fills.
STOP_LIMIT_GAP = .03


def is_crypto(symbol):
    return isinstance(symbol, str) and bool(PAIR.fullmatch(symbol))


def session(now):
    """A crypto "session" is the UTC day of Alpaca's daily candle."""
    return pd.Timestamp(now).tz_convert("UTC").date().isoformat()


def entry_window(now):
    return pd.Timestamp(now).tz_convert("UTC").hour < ENTRY_HOURS


def check(config, symbol, bars, now):
    """(None, metrics) if the coin may be bought, else (reason, metrics)."""
    base = symbol.split("/")[0]
    if base in STABLECOINS:
        return "Stablecoin: an den Dollar gebunden, kein Handelsobjekt", {}
    if base in ASSET_BACKED:
        return "An Gold gebunden (Token auf einen Vermögenswert)", {}
    if base in MEME_COINS:
        return "Meme-Coin: der Kurs folgt Hype, nicht Nutzen", {}
    if base in config.crypto_exclude:
        return "Ausgeschlossen über CRYPTO_EXCLUDE", {}
    if bars is None or len(bars) < 31:
        return "Zu wenig Kursdaten bei Alpaca", {}
    close, now = bars.close, pd.Timestamp(now)
    recent = close.tail(WINDOW)
    metrics = {"price": float(close.iloc[-1]), "history_days": int((now - bars.index[0]).days),
               "dollar_volume": float((close * bars.volume).tail(30).median()),
               "max_day_move": float(close.pct_change().tail(WINDOW).abs().max()),
               "month_return": float(close.iloc[-1] / close.iloc[-31] - 1)}
    if (now - bars.index[-1]).days > 3:
        return "Keine aktuellen Kurse", metrics
    if recent.std() / recent.mean() < PEG_VARIATION:
        return "Kurs bewegt sich kaum: vermutlich an einen Wert gebunden", metrics
    if metrics["history_days"] < config.crypto_min_history_days:
        return f"Erst {metrics['history_days']} Tage Kurshistorie bei Alpaca (mindestens {config.crypto_min_history_days})", metrics
    if metrics["dollar_volume"] < config.crypto_min_dollar_volume:
        return (f"Zu wenig Handel: im Mittel {metrics['dollar_volume']:,.0f} USD pro Tag bei Alpaca "
                f"(mindestens {config.crypto_min_dollar_volume:,.0f})"), metrics
    if metrics["max_day_move"] > config.crypto_max_day_move:
        return (f"Kurssprung von {metrics['max_day_move']:.0%} an einem Tag in den letzten {WINDOW} Tagen "
                f"(Pump- oder Crash-Gefahr, erlaubt {config.crypto_max_day_move:.0%})"), metrics
    if metrics["month_return"] > config.crypto_max_month_gain:
        return f"Kurs in 30 Tagen um {metrics['month_return']:+.0%} gestiegen (Pump-Verdacht)", metrics
    return None, metrics


def scan(config, store, broker, stop=None):
    """Check every Alpaca coin against the rules; the passing ones form the crypto bots' list."""
    now = pd.Timestamp.now(tz="UTC")
    try:
        symbols = sorted(a["symbol"] for a in broker.crypto_assets()
                         if a.get("tradable") and a.get("status") == "active" and is_crypto(a.get("symbol")))
        frames = broker.crypto_daily(symbols, now - pd.Timedelta(days=config.crypto_min_history_days + 40), now) if symbols else {}
    except Exception:
        # Fail closed: keep the previous list visible, block new crypto entries until a scan completes.
        store.set("crypto_scan_status", {"status": "unavailable", "message": "Krypto-Prüfung fehlgeschlagen; neue Käufe pausiert",
                                         "at": utcnow()})
        return
    if stop is not None and stop.is_set():
        return
    rows = []
    for symbol in symbols:
        reason, metrics = check(config, symbol, frames.get(symbol), now)
        rows.append({"symbol": symbol, "ok": reason is None, "reason": reason or "Alle Regeln erfüllt", **metrics})
    passed = sorted((r for r in rows if r["ok"]), key=lambda r: -r["dollar_volume"])
    store.set("crypto_universe", {"symbols": [r["symbol"] for r in passed], "candidates": rows, "total": len(symbols),
                                  "at": utcnow(), "scanned_at": time.time()})
    store.set("crypto_scan_status", {"status": "ready", "at": utcnow()})


def symbols(store):
    return list((store.get("crypto_universe") or {}).get("symbols", []))


def ready(store):
    """New crypto entries need a complete, recent safety check."""
    state = store.get("crypto_universe") or {}
    return ((store.get("crypto_scan_status") or {}).get("status") == "ready"
            and 0 <= time.time() - state.get("scanned_at", 0) <= 3 * SCAN_SECONDS)


def position_cap(config, store, symbol):
    """At most this share of the coin's typical daily dollar volume, so one order cannot move a thin market."""
    row = next((r for r in (store.get("crypto_universe") or {}).get("candidates", []) if r["symbol"] == symbol and r["ok"]), None)
    return row["dollar_volume"] * config.crypto_volume_share if row else 0.
