"""Trading 212's official Invest API. No fallback routing or blind POST retries.

API contract: https://docs.trading212.com/_bundle/api.json
Daily candles/quotes: https://twelvedata.com/docs (explicit listing mappings).
"""
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit, quote

import pandas as pd
import requests

from .broker import BrokerError
from .loop import INSTRUMENT, STOCK


def number(value, *, nonnegative=False):
    if isinstance(value, bool):
        raise BrokerError("Invalid numeric broker data")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise BrokerError("Missing numeric broker data") from None
    if not math.isfinite(result) or nonnegative and result < 0:
        raise BrokerError("Invalid numeric broker data")
    return result


def currency(value):
    return "GBX" if value == "GBp" else str(value).upper()


# Broker exchange metadata, never ticker suffix, ISIN nationality, or currency,
# determines the route. Unknown exchanges fail closed until explicitly supported.
MARKETS = {
    "londonstockexchange": "XLON", "londonstockexchangeaim": "XLON", "lse": "XLON",
    "deutscheborsexetra": "XETR", "deutschebörsexetra": "XETR", "xetra": "XETR",
    "gettex": "GETT", "gettexgermany": "GETT",
    "euronextparis": "XPAR", "euronextamsterdam": "XAMS", "euronextbrussels": "XBRU",
    "euronextlisbon": "XLIS", "euronextmilan": "XMIL", "borsaitaliana": "XMIL",
    "bolsademadrid": "XMAD", "sixswissexchange": "XSWX", "swx": "XSWX",
    "wienerbörse": "XWBO", "wienerborse": "XWBO", "viennastockexchange": "XWBO",
    "torontostockexchange": "XTSE", "tsx": "XTSE", "tokyostockexchange": "XTKS",
}


def market_code(name):
    key = re.sub(r"[^a-zäöü]", "", str(name).lower())
    return MARKETS.get(key)


def mappings(config):
    path = config.trading212_instruments
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BrokerError("Trading 212 instrument mapping is not readable JSON") from None
    if not isinstance(data, dict) or len(data) > 50:
        raise BrokerError("Configure at most 50 Trading 212 instrument mappings")
    for ticker, spec in data.items():
        if (not INSTRUMENT.fullmatch(ticker) or not isinstance(spec, dict)
                or not all(isinstance(spec.get(k), str) and spec[k] for k in ("symbol", "mic", "currency", "isin"))
                or not re.fullmatch(r"[A-Z0-9]{4}", spec["mic"])
                or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", spec["isin"])
                or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,30}", spec["symbol"])):
            raise BrokerError("Each Trading 212 mapping needs ticker, symbol, MIC, currency and ISIN")
        if STOCK.fullmatch(ticker):
            # A bare US-style symbol (e.g. "SIE") would also enter Alpaca's US book. Trading 212's
            # own tickers carry an exchange suffix (e.g. "SIEd_EQ"); copy them from its metadata.
            raise BrokerError(f"Trading 212 mapping {ticker!r} looks like a US symbol; use the exact Trading 212 ticker (e.g. SIEd_EQ)")
    return data


class Trading212:
    def __init__(self, config):
        self.config = config
        self.base = ("https://demo.trading212.com" if config.trading212_paper else
                     "https://live.trading212.com") + "/api/v0"
        self.session = requests.Session()
        self.session.auth = (config.trading212_key, config.trading212_secret)
        self._cache = {}
        self._lock = threading.RLock()

    def request(self, method, path, **kwargs):
        if not self.config.trading212_configured:
            raise BrokerError("Configure Trading 212 API key and secret")
        if not path.startswith("/equity/") or urlsplit(path).netloc:
            raise BrokerError("Invalid Trading 212 API path")
        try:
            response = self.session.request(method, self.base + path, timeout=(5, 25), **kwargs)
            if not response.ok:
                raise BrokerError(f"Trading 212 HTTP {response.status_code} for {method} {path.split('?')[0]}", response.status_code)
            return response.json() if response.content else None
        except (requests.RequestException, ValueError):
            # Do not expose response bodies, auth headers or a request exception's URL.
            raise BrokerError("Trading 212 request failed; reconcile any submitted order before retrying") from None

    def cached(self, path, seconds):
        with self._lock:
            saved = self._cache.get(path)
            if saved and time.monotonic() - saved[0] < seconds:
                return saved[1]
            result = self.request("GET", path)
            self._cache[path] = (time.monotonic(), result)
            return result

    def account(self):
        data = self.cached("/equity/account/summary", 5)
        code = data.get("currency", "")
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise BrokerError("Trading 212 account currency is missing")
        return {"equity": number(data.get("totalValue"), nonnegative=True),
                "cash": number(data.get("cash", {}).get("availableToTrade"), nonnegative=True),
                "currency": code, "id": str(data["id"]),
                "mode": "paper" if self.config.trading212_paper else "live"}

    def metadata(self):
        instruments = self.cached("/equity/metadata/instruments", 600)
        exchanges = self.cached("/equity/metadata/exchanges", 600)
        schedules = {s["id"]: {**s, "exchange": ex["name"], "mic": market_code(ex["name"])}
                     for ex in exchanges for s in ex.get("workingSchedules", [])}
        return {i["ticker"]: {**i, "schedule": schedules.get(i.get("workingScheduleId"))}
                for i in instruments if INSTRUMENT.fullmatch(i.get("ticker", ""))}

    def instrument(self, ticker):
        item = self.metadata().get(ticker)
        schedule = (item or {}).get("schedule") or {}
        if not item or item.get("type") not in {"STOCK", "ETF"} or not schedule.get("mic"):
            raise BrokerError("Trading 212 route requires a verified non-US stock/ETF exchange", 422)
        return item

    def positions(self):
        rows = self.cached("/equity/positions", 2)
        result = []
        for row in rows:
            instrument, wallet = row["instrument"], row["walletImpact"]
            result.append({"symbol": instrument["ticker"], "qty": number(row["quantity"], nonnegative=True),
                           "available_qty": number(row["quantityAvailableForTrading"], nonnegative=True),
                           "avg_entry_price": number(row["averagePricePaid"], nonnegative=True),
                           "current_price": number(row["currentPrice"], nonnegative=True),
                           "market_value": number(wallet["currentValue"], nonnegative=True),
                           "unrealized_pl": number(wallet["unrealizedProfitLoss"]),
                           "currency": wallet["currency"], "instrument_currency": instrument["currency"],
                           "asset_class": "equity", "broker": "trading212"})
        return result

    @staticmethod
    def normalize_order(row):
        symbol = row.get("ticker") or row.get("instrument", {}).get("ticker")
        status = str(row["status"]).lower()
        return {"id": str(row["id"]), "symbol": symbol, "side": row["side"].lower(),
                "qty": abs(number(row["quantity"])), "filled_qty": abs(number(row.get("filledQuantity", 0))),
                "status": {"cancelled": "canceled", "cancelling": "pending_cancel"}.get(status, status),
                "type": row["type"].lower(), "limit_price": row.get("limitPrice"), "stop_price": row.get("stopPrice"),
                "created_at": row.get("createdAt")}

    def open_orders(self):
        return [self.normalize_order(row) for row in self.cached("/equity/orders", 5)]

    def pages(self, path, params=None):
        seen = set()
        for _ in range(100):
            page = self.request("GET", path, params=params)
            yield page.get("items", [])
            next_path = page.get("nextPagePath")
            if not next_path:
                return
            if not next_path.startswith("/api/v0/equity/history/") or next_path in seen:
                raise BrokerError("Invalid or repeated Trading 212 pagination path")
            seen.add(next_path)
            path, params = next_path.removeprefix("/api/v0"), None
            # History endpoints permit 20 requests/minute. Avoid bursts on large accounts.
            time.sleep(3.05)
        raise BrokerError("Trading 212 history is incomplete")

    def order(self, order_id, ticker):
        try:
            return self.normalize_order(self.request("GET", "/equity/orders/" + quote(str(order_id), safe="")))
        except BrokerError as exc:
            if exc.status != 404:
                raise
        # The single-order endpoint returns *pending* orders only. A 404 is not a fill.
        for page in self.pages("/equity/history/orders", {"ticker": ticker, "limit": 50}):
            matches = [item["order"] for item in page if str(item.get("order", {}).get("id")) == str(order_id)]
            if matches:
                return self.normalize_order(max(matches, key=lambda x: abs(number(x.get("filledQuantity", 0)))))
        raise BrokerError("Order absent from pending orders and history; outcome unresolved")

    def cash_flows(self, since):
        rows = []
        for page in self.pages("/equity/history/transactions", {"time": since, "limit": 50}):
            for row in page:
                if row.get("type") == "TRANSFER":
                    raise BrokerError("Transfers need a valued performance adjustment")
                if row.get("type") in {"DEPOSIT", "WITHDRAW"}:
                    amount = abs(number(row["amount"])) * (-1 if row["type"] == "WITHDRAW" else 1)
                    rows.append({"at": row["dateTime"], "amount": amount, "currency": row["currency"]})
        return rows

    def submit(self, ticker, side, qty, *, kind, price=None):
        self.instrument(ticker)  # Recheck routing immediately before every POST, including sells/stops.
        if not self.config.trading212_paper and not self.config.trading212_allow_live:
            raise BrokerError("Trading 212 live execution is disabled", 403)
        qty = number(qty, nonnegative=True)
        if side not in {"buy", "sell"} or qty <= 0 or kind not in {"limit", "market", "stop"}:
            raise BrokerError("Invalid Trading 212 order", 422)
        payload = {"ticker": ticker, "quantity": qty if side == "buy" else -qty}
        if kind == "market":
            payload["extendedHours"] = False
        else:
            if number(price, nonnegative=True) <= 0:
                raise BrokerError("Invalid Trading 212 order price", 422)
            payload.update({"stopPrice" if kind == "stop" else "limitPrice": price,
                            "timeValidity": "GOOD_TILL_CANCEL" if kind == "stop" else "DAY"})
        result = self.request("POST", "/equity/orders/" + kind, json=payload)
        with self._lock:
            self._cache.pop("/equity/orders", None)
        return self.normalize_order(result)

    def cancel(self, order_id):
        result = self.request("DELETE", "/equity/orders/" + quote(str(order_id), safe=""))
        with self._lock:
            self._cache.pop("/equity/orders", None)
        return result

    def session_for(self, ticker, now):
        """Use dated broker events, including breaks/holidays; never guess opening hours."""
        schedule = self.instrument(ticker)["schedule"]
        events = sorted((pd.Timestamp(e["date"]), e["type"]) for e in schedule.get("timeEvents", []))
        if any(at.tzinfo is None for at, _ in events):
            raise BrokerError("Exchange schedule has no timezone")
        relevant = [(at, kind) for at, kind in events if kind in {"OPEN", "CLOSE", "BREAK_START", "BREAK_END"}]
        past = [(at, kind) for at, kind in relevant if at <= now]
        last = past[-1] if past else None
        close = next((at for at, kind in relevant if at > now and kind == "CLOSE"), None)
        opening = next((at for at, kind in reversed(past) if kind == "OPEN"), None)
        is_open = bool(last and last[1] in {"OPEN", "BREAK_END"} and opening is not None
                       and now - opening < pd.Timedelta(hours=24) and close is not None
                       and close - now < pd.Timedelta(hours=24))
        prior_close = next((at for at, kind in reversed(past) if kind == "CLOSE"), None)
        return {"open": is_open, "opening": opening, "close": close, "prior_close": prior_close}


class MarketData:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self._next_request = 0.
        self._lock = threading.Lock()

    def request(self, path, params):
        if not self.config.twelvedata_key:
            raise BrokerError("Add TWELVEDATA_API_KEY for non-US stock candles and quotes")
        # At most 7 requests/minute, shared by signal workers and execution quotes.
        with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + 8.7
            try:
                response = self.session.get("https://api.twelvedata.com/" + path,
                    params={**params, "apikey": self.config.twelvedata_key}, timeout=(5, 25))
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError):
                raise BrokerError("Non-US market-data request failed") from None
        if data.get("status") == "error":
            raise BrokerError("Non-US data unavailable: check listing coverage and provider quota")
        return data

    @staticmethod
    def verify(meta, spec, instrument):
        if (meta.get("symbol") != spec["symbol"] or meta.get("mic_code") != spec["mic"]
                or currency(meta.get("currency")) != currency(spec["currency"])
                or spec["mic"] != instrument["schedule"]["mic"]
                or currency(spec["currency"]) != currency(instrument["currencyCode"])
                or spec["isin"] != instrument.get("isin")):
            raise BrokerError("Market-data listing, currency or ISIN does not match the broker instrument")

    def bars(self, instrument, spec, now):
        result = self.request("time_series", {"symbol": spec["symbol"], "mic_code": spec["mic"],
            "interval": "1day", "outputsize": 1500, "order": "asc", "adjust": "all"})
        meta = result.get("meta", {})
        self.verify(meta, spec, instrument)
        zone = meta.get("exchange_timezone")
        try:
            frame = pd.DataFrame(result["values"])
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("datetime"))).tz_localize(zone).tz_convert("UTC")
            frame = frame[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
            # Only complete sessions preceding the current exchange day enter the backtest.
            day = now.tz_convert(zone).date()
            frame = frame[frame.index.tz_convert(zone).date < day].tail(1300)
            valid = ((frame[["open", "high", "low", "close"]] > 0).all().all()
                     and (frame.high >= frame[["open", "close", "low"]].max(axis=1)).all()
                     and (frame.low <= frame[["open", "close", "high"]].min(axis=1)).all()
                     and (frame.volume >= 0).all() and not frame.isna().any().any()
                     and all(math.isfinite(v) for v in frame.to_numpy().flat))
            if len(frame) < 1000 or frame.index.has_duplicates or not valid:
                raise ValueError()
            frame.attrs["market_timezone"] = zone
            return frame
        except (KeyError, TypeError, ValueError):
            raise BrokerError("Need at least 1,000 valid completed daily candles with an exchange timezone") from None

    def quote(self, instrument, spec, now):
        result = self.request("quote", {"symbol": spec["symbol"], "mic_code": spec["mic"]})
        self.verify(result, spec, instrument)
        try:
            at = pd.Timestamp(number(result["timestamp"]), unit="s", tz="UTC")
            price = number(result["close"], nonnegative=True)
            if not -10 <= (now - at).total_seconds() <= 120 or price <= 0 or not result.get("is_market_open"):
                raise ValueError()
        except (KeyError, ValueError):
            raise BrokerError("Non-US quote is delayed, stale or outside the regular session") from None
        return price

    def execution_fx(self, source, target, now):
        source, scale = ("GBP", .01) if currency(source) == "GBX" else (currency(source), 1.)
        if source == target:
            return scale
        if not self.config.trading212_allow_fx:
            raise BrokerError("Foreign-currency entries disabled; TRADING212_ALLOW_FX is false")
        result = self.request("exchange_rate", {"symbol": source + "/" + target})
        if result.get("symbol") != source + "/" + target:
            raise BrokerError("Execution FX currency mismatch")
        at = pd.Timestamp(number(result.get("timestamp")), unit="s", tz="UTC")
        rate = number(result.get("rate"), nonnegative=True)
        if rate <= 0 or not -10 <= (now - at).total_seconds() <= 120:
            raise BrokerError("Execution FX rate is stale")
        return rate * scale
