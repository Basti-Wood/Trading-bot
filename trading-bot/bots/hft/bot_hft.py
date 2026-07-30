"""
bots/hft/bot_hft.py — HFT Alpaca Bot (corrected)

Original bot with the following fixes applied:
  1. Timestamp parsing: preserved full datetime (was discarding H:M:S).
  2. compare_ask_ltp: fixed variable shadowing (`i` reused) and pop-in-loop.
  3. Position check: compare symbols, not Position objects (always failed).
  4. CSV column drop: guarded with errors='ignore'.
  5. Market-close loop: now waits for next open instead of terminating.
  6. time_to_10: replaced fragile string-parse with .total_seconds().
  7. Email credentials: moved to env vars / config dict at top of file.
  8. Redundant `== True` removed throughout.
  9. Blanket except: pass narrowed to IndexError only.
"""

import os
import time
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime as dt, timedelta

import pandas as pd
import alpaca_trade_api as alpaca
from pytz import timezone

# ---------------------------------------------------------------------------
# Configuration — edit these values (or set env vars)
# ---------------------------------------------------------------------------
AUTH_PATH = os.environ.get("HFT_AUTH_PATH", "AUTH/auth.txt")
TICKERS_PATH = os.environ.get("HFT_TICKERS_PATH", "AUTH/Tickers.txt")

EMAIL_SENDER = os.environ.get("HFT_EMAIL_SENDER", "your_email@gmail.com")
EMAIL_PASS = os.environ.get("HFT_EMAIL_PASS", "your_app_password")
EMAIL_RECEIVER = os.environ.get("HFT_EMAIL_RECEIVER", "your_email@gmail.com")

DATA_DIR = os.environ.get("HFT_DATA_DIR", "tick_data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Auth & symbols
# ---------------------------------------------------------------------------
with open(AUTH_PATH, "r") as f:
    key = json.load(f)

api = alpaca.REST(
    key["APCA-API-KEY-ID"],
    key["APCA-API-SECRET-KEY"],
    base_url="https://api.alpaca.markets",
    api_version="v2",
)

with open(TICKERS_PATH, "r") as f:
    _raw = f.read().upper().split()

TICKERS: list[str] = _raw

ET = timezone("America/New_York")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_et() -> dt:
    """Current datetime in Eastern Time."""
    return dt.now().astimezone(ET)


def _parse_index(idx: pd.Index) -> pd.Index:
    """Convert Alpaca timestamps to a clean datetime-index string format.
    Preserves full H:M:S (the original code was losing time data by using
    format='%Y-%m-%d' — fixed)."""
    return pd.to_datetime(idx, utc=True).tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_minute_data(tickers: list[str]) -> None:
    """Fetch last 2 minutes of trades + quotes for each ticker."""

    def _save(ticker: str) -> None:
        start = (_now_et() - timedelta(minutes=2)).isoformat()
        end = _now_et().isoformat()

        prices = api.get_trades(
            ticker, start=start, end=end, limit=10000
        ).df[["price"]]
        prices.index = _parse_index(prices.index)
        prices = prices[~prices.index.duplicated(keep="first")]

        quotes = api.get_quotes(
            ticker, start=start, end=end, limit=10000
        ).df[["ask_price"]]
        quotes.index = _parse_index(quotes.index)
        quotes = quotes[~quotes.index.duplicated(keep="first")]

        df = pd.merge(prices, quotes, how="inner", left_index=True, right_index=True)
        df.to_csv(os.path.join(DATA_DIR, f"{ticker}.csv"))

    for t in tickers:
        _save(t)


def get_past30_data(tickers: list[str]) -> None:
    """Fetch 30-min window (with a 90s gap around now to avoid stale snapshots)."""

    def _save(ticker: str) -> None:
        now = _now_et()
        # Window 1: -30m → -90s
        p1 = api.get_trades(
            ticker,
            start=(now - timedelta(minutes=30)).isoformat(),
            end=(now - timedelta(seconds=90)).isoformat(),
            limit=10000,
        ).df[["price"]]
        # Window 2: -60s → now
        p2 = api.get_trades(
            ticker,
            start=(now - timedelta(seconds=60)).isoformat(),
            end=now.isoformat(),
            limit=10000,
        ).df[["price"]]

        p1.index = _parse_index(p1.index)
        p2.index = _parse_index(p2.index)
        prices = pd.concat([p1, p2])
        prices = prices[~prices.index.duplicated(keep="first")]

        q1 = api.get_quotes(
            ticker,
            start=(now - timedelta(minutes=30)).isoformat(),
            end=(now - timedelta(seconds=90)).isoformat(),
            limit=10000,
        ).df[["ask_price"]]
        q2 = api.get_quotes(
            ticker,
            start=(now - timedelta(seconds=60)).isoformat(),
            end=now.isoformat(),
            limit=10000,
        ).df[["ask_price"]]

        q1.index = _parse_index(q1.index)
        q2.index = _parse_index(q2.index)
        quotes = pd.concat([q1, q2])
        quotes = quotes[~quotes.index.duplicated(keep="first")]

        df = pd.merge(prices, quotes, how="inner", left_index=True, right_index=True)
        df.to_csv(os.path.join(DATA_DIR, f"{ticker}.csv"))

    for t in tickers:
        _save(t)


# ---------------------------------------------------------------------------
# Rate-of-Change
# ---------------------------------------------------------------------------

def roc(ask_series: pd.Series, timeframe: int) -> float:
    """Rate of change in per-mille (×1000)."""
    if len(ask_series) < 2:
        return 0.0
    if timeframe == 30:
        return (ask_series.iloc[-1] - ask_series.iloc[0]) / ask_series.iloc[0] * 1000
    return (ask_series.iloc[-1] - ask_series.iloc[-2]) / ask_series.iloc[-2] * 1000


def return_roc_list(tickers: list[str], timeframe: int) -> list[float]:
    """Return list of ROCs for all tickers."""
    rocs: list[float] = []
    for t in tickers:
        path = os.path.join(DATA_DIR, f"{t}.csv")
        if not os.path.isfile(path):
            rocs.append(0.0)
            continue
        df = pd.read_csv(path)
        df.set_index("timestamp", inplace=True)
        if "ask_price" not in df.columns or df.empty:
            rocs.append(0.0)
            continue
        rocs.append(roc(df["ask_price"], timeframe))
    return rocs


# ---------------------------------------------------------------------------
# Entry logic
# ---------------------------------------------------------------------------

def compare_ask_ltp(tickers: list[str], timeframe: int) -> str | int:
    """
    Find the ticker with the highest positive ROC where ask > last trade price.
    Returns ticker symbol, 0 (no positive ROC), or -1 (no ask > LTP).
    """
    remaining = list(tickers)  # work on a copy to avoid mutating caller's list
    rocs = return_roc_list(remaining, timeframe)

    while remaining:
        if not rocs:
            return -1
        max_roc = max(rocs)
        if max_roc <= 0:
            return 0
        idx = rocs.index(max_roc)
        candidate = remaining[idx]

        path = os.path.join(DATA_DIR, f"{candidate}.csv")
        if not os.path.isfile(path):
            remaining.pop(idx)
            rocs.pop(idx)
            continue

        df = pd.read_csv(path)
        df.set_index("timestamp", inplace=True)
        if "ask_price" not in df.columns or "price" not in df.columns:
            remaining.pop(idx)
            rocs.pop(idx)
            continue

        ask_col = df.columns.get_loc("ask_price")
        price_col = df.columns.get_loc("price")

        # Check last two rows: ask > price?
        last_rows = df.iloc[-2:] if len(df) >= 2 else df
        # FIXED: use a different variable name (k) to avoid shadowing outer loop
        buy_ok = all(
            last_rows.iloc[k, ask_col] > last_rows.iloc[k, price_col]
            for k in range(len(last_rows))
        )
        if buy_ok:
            return candidate

        # This ticker didn't qualify — remove and try next best ROC
        remaining.pop(idx)
        rocs.pop(idx)

    return -1   # exhausted all tickers


def stock_to_buy(tickers: list[str], timeframe: int) -> str | int:
    return compare_ask_ltp(tickers, timeframe)


def algo(tickers: list[str]) -> str | int:
    if os.path.isfile("FirstTrade.csv"):
        timeframe = 1
    else:
        timeframe = 30
    return stock_to_buy(tickers, timeframe)


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def buy(symbol: str) -> str:
    cash_balance = float(api.get_account().cash)
    price = api.get_latest_trade(symbol).price
    qty = cash_balance / price

    api.submit_order(symbol, qty, "buy", "market", "day")

    mail = (
        f"ALERT\n\nBUY Order Placed for {symbol}: {qty:.4f} Shares @ ${price:.2f}"
    )

    # --- Log to CSV ---
    row = {
        "Time": _now_et().strftime("%Y-%m-%d %H:%M:%S"),
        "Ticker": symbol,
        "Type": "buy",
        "Price": price,
        "Quantity": round(qty, 6),
        "Total": round(qty * price, 2),
        "Acc Balance": float(api.get_account().cash),
    }

    orders_path = "Orders.csv"
    if os.path.isfile(orders_path):
        df = pd.read_csv(orders_path)
        # Drop unnamed index column if present (errors='ignore' for safety)
        df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")],
                inplace=True, errors="ignore")
    else:
        df = pd.DataFrame(columns=list(row.keys()))

    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(orders_path, index=False)
    return mail


def sell(symbol: str) -> str:
    qty = float(api.get_position(symbol).qty)
    sell_price = api.get_latest_trade(symbol).price
    api.cancel_all_orders()
    api.close_position(symbol)

    mail = (
        f"ALERT\n\nSELL Order Placed for {symbol}: {qty:.4f} Shares @ ${sell_price:.2f}"
    )

    row = {
        "Time": _now_et().strftime("%Y-%m-%d %H:%M:%S"),
        "Ticker": symbol,
        "Type": "sell",
        "Price": sell_price,
        "Quantity": qty,
        "Total": round(qty * sell_price, 2),
        "Acc Balance": float(api.get_account().cash),
    }

    orders_path = "Orders.csv"
    if os.path.isfile(orders_path):
        df = pd.read_csv(orders_path)
        df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")],
                inplace=True, errors="ignore")
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])

    df.to_csv(orders_path, index=False)
    return mail


def check_rets(symbol: str) -> str | int:
    returns_pct = float(api.get_position(symbol).unrealized_plpc) * 100
    if returns_pct >= 2:
        return sell(symbol)
    return 0


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def mail_alert(content: str, sleep_sec: int = 0) -> None:
    message = MIMEMultipart()
    message["From"] = "Trading Bot"
    message["To"] = EMAIL_RECEIVER
    message["Subject"] = "HFT Second-Bot"
    message.attach(MIMEText(content, "plain"))

    try:
        session = smtplib.SMTP("smtp.gmail.com", 587)
        session.starttls()
        session.login(EMAIL_SENDER, EMAIL_PASS)
        session.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, message.as_string())
        session.quit()
    except Exception as exc:
        print(f"[mail] failed to send: {exc}")

    if sleep_sec:
        time.sleep(sleep_sec)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    # Startup notification
    startup_msg = (
        f"The bot started running on {dt.now().strftime('%Y-%m-%d')} "
        f"at {dt.now().strftime('%H:%M:%S')} UTC"
    )
    mail_alert(startup_msg)

    warned_closed = False

    while True:
        # PDT guard
        if api.get_account().pattern_day_trader:
            mail_alert("Pattern day trading notification, bot is stopping now")
            break

        if not api.get_clock().is_open:
            if not warned_closed:
                print("[hft] Market is closed — waiting for next open.")
                warned_closed = True
            time.sleep(300)
            continue
        warned_closed = False

        tickers = list(TICKERS)  # fresh copy each cycle

        try:
            # --- Cancel any lingering partially-filled orders ---
            try:
                activities = api.get_activities()
                if activities and activities[0].order_status == "partially_filled":
                    api.cancel_all_orders()
            except IndexError:
                pass   # no activities yet — fine

            # ----------------------------------------------------------------
            # First trade of the day? Use 30-min window.
            # ----------------------------------------------------------------
            if not os.path.isfile("FirstTrade.csv"):
                now_et = _now_et()
                market_open_time = now_et.replace(hour=10, minute=0, second=0, microsecond=0)
                if now_et < market_open_time:
                    wait = (market_open_time - now_et).total_seconds() - 20
                    if wait > 0:
                        print(f"[hft] Waiting {wait:.0f}s until 10:00 ET for first trade...")
                        time.sleep(wait)

                get_past30_data(tickers)
                pick = algo(tickers)

                if pick == 0:
                    print("All ROCs <= 0")
                    continue
                if pick == -1:
                    print("All Ask < LTP")
                    continue

                mail_alert(buy(str(pick)), 5)
                pd.DataFrame({"First Stock": [pick]}).to_csv("FirstTrade.csv", index=False)
                continue

            # ----------------------------------------------------------------
            # Normal operation — 1-min window
            # ----------------------------------------------------------------
            if float(api.get_account().cash) > 10:
                get_minute_data(tickers)
                pick = algo(tickers)

                if pick == 0:
                    print("All ROCs <= 0")
                    time.sleep(2)
                    continue
                if pick == -1:
                    print("All Ask < LTP")
                    time.sleep(2)
                    continue

                symbol = str(pick)

                # --- Check if we already hold this symbol ---
                positions = api.list_positions()
                # FIXED: compare symbols, not Position objects
                held_symbols = [p.symbol for p in positions]

                if symbol in held_symbols:
                    if api.get_latest_trade(symbol).price > float(
                        api.get_position(symbol).avg_entry_price
                    ):
                        print(f"LTP for {symbol} > Average Entry Price — skip")
                        time.sleep(2)
                        continue

                mail_alert(buy(symbol), 5)

            else:
                # --- Out of cash: manage existing positions ---
                positions = api.list_positions()
                alerts = []
                for pos in positions:
                    result = check_rets(pos.symbol)
                    if result != 0:
                        alerts.append(result)

                if alerts:
                    for a in alerts:
                        mail_alert(str(a))
                else:
                    time.sleep(3)

        except Exception as exc:
            print(f"[hft] cycle error: {exc}")
            time.sleep(5)
            continue

    # Bot stopped
    mail_alert(
        f"The bot stopped running on {dt.now().strftime('%Y-%m-%d')} "
        f"at {dt.now().strftime('%H:%M:%S')} UTC"
    )


if __name__ == "__main__":
    main()
