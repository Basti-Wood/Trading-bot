"""
shared/broker/alpaca.py — Alpaca Broker Layer

Fractional LONGS via notional dollar amounts (min $1, 6-dp qty precision).
Shorts are ALWAYS whole shares — Alpaca hard limit, no fractional shorts.
Every short gets a stop-loss (BUY stop) placed immediately after opening.
"""

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

_client: TradingClient | None = None
_broker_unavailable_reason: str | None = None


def _invalid_value(v: str | None) -> bool:
    if not v:
        return True
    return v.strip().lower() in {
        "your_key_here",
        "your_secret_here",
        "changeme",
        "none",
        "null",
    }


def _credentials_configured() -> bool:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    return not _invalid_value(key) and not _invalid_value(secret)


def get_client() -> TradingClient:
    """Lazily construct the singleton TradingClient from env vars."""
    global _client, _broker_unavailable_reason
    if _client is None:
        if not _credentials_configured():
            _broker_unavailable_reason = (
                "ALPACA_API_KEY/ALPACA_SECRET_KEY not configured "
                "(still empty or placeholder values)."
            )
            raise RuntimeError(_broker_unavailable_reason)
        paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
        try:
            _client = TradingClient(
                os.environ["ALPACA_API_KEY"],
                os.environ["ALPACA_SECRET_KEY"],
                paper=paper,
            )
        except Exception as exc:
            _broker_unavailable_reason = f"Alpaca client init failed: {exc}"
            raise RuntimeError(_broker_unavailable_reason)
    return _client


def is_configured() -> bool:
    """True when Alpaca credentials look valid (not placeholders)."""
    return _credentials_configured()


def is_fractionable(symbol: str) -> bool:
    """Check the fractionable flag before placing a fractional order."""
    try:
        return bool(get_client().get_asset(symbol).fractionable)
    except Exception:
        return False


def open_long(symbol: str, notional_usd: float, stop_loss: float,
              current_price: float) -> bool:
    """
    Long order: notional for fractionable assets, whole shares otherwise.
    Places a GTC stop-loss SELL immediately after. Returns True if placed.
    """
    try:
        client = get_client()
    except Exception:
        return False
    if notional_usd < 1.0:
        return False

    if is_fractionable(symbol):
        client.submit_order(MarketOrderRequest(
            symbol=symbol,
            notional=str(round(notional_usd, 2)),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        approx_qty = max(1, round(notional_usd / current_price))
        client.submit_order(StopOrderRequest(
            symbol=symbol,
            qty=str(approx_qty),
            side=OrderSide.SELL,
            stop_price=str(round(stop_loss, 2)),
            time_in_force=TimeInForce.GTC,
        ))
        return True

    whole_shares = int(notional_usd // current_price)
    if whole_shares < 1:
        return False   # can't afford one share — skip
    client.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=str(whole_shares),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    ))
    client.submit_order(StopOrderRequest(
        symbol=symbol,
        qty=str(whole_shares),
        side=OrderSide.SELL,
        stop_price=str(round(stop_loss, 2)),
        time_in_force=TimeInForce.GTC,
    ))
    return True


def close_long(symbol: str, qty: float) -> bool:
    try:
        get_client().submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=str(round(qty, 6)),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception:
        return False
    return True


def open_short(symbol: str, notional_usd: float, stop_loss: float,
               current_price: float) -> int:
    """
    Short: convert notional to WHOLE shares (round down). If it rounds to 0,
    skip silently (caller logs it). Stop-loss BUY placed immediately.
    Returns the whole-share qty shorted (0 = skipped).
    """
    whole_shares = int(notional_usd // current_price)
    if whole_shares < 1:
        return 0   # position too small to short — skip entirely

    try:
        client = get_client()
    except Exception:
        return 0
    client.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=str(whole_shares),
        side=OrderSide.SELL,     # sell to open short
        time_in_force=TimeInForce.DAY,
    ))
    # Stop-loss for short: BUY order above entry to cap losses
    client.submit_order(StopOrderRequest(
        symbol=symbol,
        qty=str(whole_shares),
        side=OrderSide.BUY,      # buy to close short
        stop_price=str(round(stop_loss, 2)),
        time_in_force=TimeInForce.GTC,
    ))
    return whole_shares


def close_short(symbol: str, qty: float) -> bool:
    try:
        get_client().submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=str(int(qty)),       # always whole shares for shorts
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception:
        return False
    return True


def get_portfolio_value() -> float:
    """Current account equity."""
    try:
        return float(get_client().get_account().equity)
    except Exception:
        return 0.0


def get_positions() -> list:
    try:
        return get_client().get_all_positions()
    except Exception:
        return []
