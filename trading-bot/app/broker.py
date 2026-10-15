"""Alpaca REST boundary. Never retry an order submission blindly."""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import requests
import pandas as pd


class BrokerError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Alpaca:
    def __init__(self, config):
        self.config = config
        self.base = "https://paper-api.alpaca.markets" if config.paper else "https://api.alpaca.markets"
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": config.api_key, "APCA-API-SECRET-KEY": config.api_secret})

    def request(self, method, path, *, data=False, **kwargs):
        if not self.config.configured:
            raise BrokerError("Add Alpaca credentials to .env to connect")
        base = "https://data.alpaca.markets" if data else self.base
        try:
            response = self.session.request(method, base + path, timeout=(5, 25), **kwargs)
        except requests.RequestException as exc:
            raise BrokerError("Alpaca request failed; order outcome may need reconciliation") from exc
        if not response.ok:
            # Do not forward response bodies or headers containing account information.
            raise BrokerError(f"Alpaca returned HTTP {response.status_code} for {method} {path.split('?')[0]}", response.status_code)
        return response.json() if response.content else None

    def account(self):
        return self.request("GET", "/v2/account")

    def clock(self):
        return self.request("GET", "/v2/clock")

    def positions(self):
        return self.request("GET", "/v2/positions")

    def open_orders(self):
        orders = self.request("GET", "/v2/orders", params={"status": "open", "limit": 500, "nested": "false"})
        if len(orders) >= 500:
            raise BrokerError("Too many open account orders to reconcile safely")
        return orders

    def asset(self, symbol):
        return self.request("GET", f"/v2/assets/{quote(symbol, safe='')}")

    def assets(self):
        return self.request("GET", "/v2/assets", params={"status": "active", "asset_class": "us_equity"})

    def snapshots(self, symbols):
        return self.request("GET", "/v2/stocks/snapshots", data=True,
                            params={"symbols": ",".join(symbols), "feed": self.config.feed})

    def calendar(self, start, end):
        return self.request("GET", "/v2/calendar", params={"start": str(start), "end": str(end)})

    def quote(self, symbol):
        result = self.request("GET", f"/v2/stocks/{quote(symbol, safe='')}/quotes/latest", data=True,
                              params={"feed": self.config.feed})["quote"]
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(result["t"])).total_seconds()
        bid, ask = float(result["bp"]), float(result["ap"])
        if not -10 <= age <= 60 or not 0 < bid <= ask < 1000000 or (ask - bid) / ask > .003:
            raise BrokerError(f"Stale, crossed or wide quote for {symbol}")
        return bid, ask

    def order(self, client_id):
        return self.request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_id})

    def submit(self, payload):
        return self.request("POST", "/v2/orders", json=payload)

    def cancel(self, order_id):
        return self.request("DELETE", f"/v2/orders/{quote(order_id, safe='')}")

    def bars(self, symbol, now=None):
        now = now or datetime.now(timezone.utc)
        if self.config.intraday:
            return self.intraday_bars(symbol, now)
        # Exclude today's unfinished daily bar, including after-hours revisions.
        local_day = now.astimezone(__import__('zoneinfo').ZoneInfo("America/New_York")).date()
        params = {"timeframe": "1Day", "start": (now - timedelta(days=self.config.history_days)).isoformat(),
                  "end": (now - timedelta(minutes=16)).isoformat(), "adjustment": "all",
                  "feed": self.config.feed, "limit": 10000, "sort": "asc"}
        rows, tokens = [], set()
        for _ in range(10):
            result = self.request("GET", f"/v2/stocks/{quote(symbol, safe='')}/bars", data=True, params=params)
            rows.extend(result.get("bars") or [])
            token = result.get("next_page_token")
            if not token:
                break
            if token in tokens:
                raise BrokerError("Repeated market data pagination token")
            tokens.add(token)
            params["page_token"] = token
        else:
            raise BrokerError("Market data pagination limit exceeded")
        if not rows:
            raise BrokerError(f"No {self.config.feed.upper()} daily bars for {symbol}")
        frame = pd.DataFrame(rows).rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True)
        frame = frame.set_index("timestamp").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = frame[frame.index.tz_convert("America/New_York").date < local_day]
        frame = frame[["open", "high", "low", "close", "volume"]].astype(float).tail(self.config.max_bars)
        valid = (frame[["open", "high", "low", "close"]] > 0).all(axis=1) & (frame.volume >= 0)
        if not valid.all() or frame.isna().any().any():
            raise BrokerError(f"Invalid OHLCV data for {symbol}")
        return frame

    def intraday_bars(self, symbol, now):
        start = now - timedelta(days=60)
        params = {"timeframe": "5Min", "start": start.isoformat(), "end": now.isoformat(),
                  "adjustment": "all", "feed": self.config.feed, "limit": 10000, "sort": "desc"}
        rows, tokens = [], set()
        for _ in range(10):
            result = self.request("GET", f"/v2/stocks/{quote(symbol, safe='')}/bars", data=True, params=params)
            rows.extend(result.get("bars") or [])
            token = result.get("next_page_token")
            if not token:
                break
            if token in tokens:
                raise BrokerError("Repeated market data pagination token")
            tokens.add(token)
            params["page_token"] = token
        else:
            raise BrokerError("Intraday pagination limit exceeded")
        if not rows:
            raise BrokerError(f"No five-minute bars for {symbol}")
        frame = pd.DataFrame(rows).rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True)
        frame = frame.set_index("timestamp").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        # Calendar filtering includes half-days; extended-hours bars never enter fitting.
        sessions = self.calendar(start.date(), now.date())
        local = frame.index.tz_convert("America/New_York")
        keep = pd.Series(False, index=frame.index)
        for session in sessions:
            opening = pd.Timestamp(f"{session['date']} {session['open']}", tz="America/New_York")
            closing = pd.Timestamp(f"{session['date']} {session['close']}", tz="America/New_York")
            keep |= (local >= opening) & (local + pd.Timedelta(minutes=5) <= closing)
        keep &= frame.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)
        frame = frame.loc[keep, ["open", "high", "low", "close", "volume"]].astype(float).tail(self.config.max_bars)
        if frame.empty or not (frame[["open", "high", "low", "close"]] > 0).all().all() or not (frame.volume >= 0).all() or frame.isna().any().any():
            raise BrokerError(f"Invalid or missing regular-session bars for {symbol}")
        return frame

    def latest_price(self, symbol):
        result = self.request("GET", f"/v2/stocks/{quote(symbol, safe='')}/trades/latest", data=True, params={"feed": self.config.feed})
        trade = result["trade"]
        at = pd.Timestamp(trade["t"])
        age = (pd.Timestamp.now(tz="UTC") - at).total_seconds()
        price = float(trade["p"])
        if not 0 < price < 1000000 or not -10 <= age <= 120:
            raise BrokerError(f"Stale or invalid latest trade for {symbol}")
        return price
