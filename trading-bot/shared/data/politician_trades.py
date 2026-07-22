"""
shared/data/politician_trades.py — Politician Trading Signal

US politicians must disclose trades within 45 days (STOCK Act).
Data source: Quiver Quantitative free tier.
US equities only — not useful for crypto or EU assets.
"""

import requests
import pandas as pd
from datetime import datetime, timedelta

QUIVER_BASE = "https://api.quiverquant.com/beta"

POLITICIAN_WEIGHTS = {
    "House Financial Services Committee": 1.5,
    "Senate Banking Committee": 1.5,
    "House Intelligence Committee": 1.3,
    "Senate Intelligence Committee": 1.3,
    "default": 1.0,
}

DEFAULT_LOOKBACK_DAYS = 60
STALE_SIGNAL_DAYS = 45

# Simple in-process cache to respect the free-tier rate limit
_cache: dict = {}
_CACHE_TTL = timedelta(hours=6)


def get_politician_trades(ticker: str, api_key: str) -> pd.DataFrame:
    """
    GET /historical/congresstrading/{ticker}
    Columns: Date, Politician, Party, Chamber, Committee, Transaction, Amount, weight.
    Weighted by committee relevance, sorted by Date descending.
    """
    now = datetime.now()
    cached = _cache.get(ticker)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    url = f"{QUIVER_BASE}/historical/congresstrading/{ticker}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        print(f"[politician] fetch failed for {ticker}: {exc}")
        return pd.DataFrame(columns=["Date", "Politician", "Party", "Chamber",
                                     "Committee", "Transaction", "Amount", "weight"])

    df = pd.DataFrame(rows)
    if df.empty:
        _cache[ticker] = (now, df)
        return df

    # Normalise columns that exist in the Quiver payload
    if "TransactionDate" in df.columns and "Date" not in df.columns:
        df = df.rename(columns={"TransactionDate": "Date"})
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])

    committee = df.get("Committee", pd.Series("", index=df.index)).fillna("")
    df["weight"] = committee.map(
        lambda c: POLITICIAN_WEIGHTS.get(c, POLITICIAN_WEIGHTS["default"]))

    df = df.sort_values("Date", ascending=False).reset_index(drop=True)
    _cache[ticker] = (now, df)
    return df


def compute_politician_signal(ticker: str, api_key: str,
                              lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """
    Returns 3 ML-ready features:
      pol_net_sentiment : float  weighted (-1.0 all sells -> +1.0 all buys)
      pol_signal_age    : int    days since most recent trade (999 = none)
      pol_buy_count     : int    number of buy transactions in lookback window
    """
    df = get_politician_trades(ticker, api_key)
    if df.empty:
        return {"pol_net_sentiment": 0.0, "pol_signal_age": 999, "pol_buy_count": 0}

    cutoff = datetime.now() - timedelta(days=lookback_days)
    recent = df[df["Date"] >= cutoff]
    if recent.empty:
        return {"pol_net_sentiment": 0.0, "pol_signal_age": 999, "pol_buy_count": 0}

    tx = recent["Transaction"].astype(str)
    is_buy = tx.str.contains("Purchase", case=False)
    is_sell = tx.str.contains("Sale", case=False)

    buy_w = float(recent.loc[is_buy, "weight"].sum())
    sell_w = float(recent.loc[is_sell, "weight"].sum())
    total_w = buy_w + sell_w
    net = (buy_w - sell_w) / total_w if total_w > 0 else 0.0

    return {
        "pol_net_sentiment": round(float(net), 3),
        "pol_signal_age": int((datetime.now() - recent["Date"].max()).days),
        "pol_buy_count": int(is_buy.sum()),
    }


def is_strong_buy(sig: dict) -> bool:
    """Strong buy: net sentiment > 0.6, fresh (< 30 days)."""
    return sig["pol_net_sentiment"] > 0.6 and sig["pol_signal_age"] < 30


def is_strong_sell(sig: dict) -> bool:
    """Strong sell/short: net sentiment < -0.6, fresh (< 30 days)."""
    return sig["pol_net_sentiment"] < -0.6 and sig["pol_signal_age"] < 30


def is_stale(sig: dict) -> bool:
    """Signal older than STALE_SIGNAL_DAYS — close positions on it."""
    return sig["pol_signal_age"] > STALE_SIGNAL_DAYS
