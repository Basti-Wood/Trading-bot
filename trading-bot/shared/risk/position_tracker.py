"""
shared/risk/position_tracker.py — Position Tracker

In-memory record of open positions per bot.
Longs may be fractional (notional based); shorts are always whole shares.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Position:
    ticker: str
    side: str                 # "LONG" or "SHORT"
    notional_usd: float       # dollar value at entry (fractional longs)
    qty: float                # share quantity (shorts must be whole shares)
    entry: float              # entry price
    stop_loss: float
    fractional: bool = False  # True if this is a fractional position
    opened_at: datetime = field(default_factory=datetime.now)


class PositionTracker:
    def __init__(self):
        self.positions: dict = {}   # ticker -> Position

    # -- state queries ---------------------------------------------------

    def is_long(self, ticker: str) -> bool:
        pos = self.positions.get(ticker)
        return pos is not None and pos.side == "LONG"

    def is_short(self, ticker: str) -> bool:
        pos = self.positions.get(ticker)
        return pos is not None and pos.side == "SHORT"

    def is_flat(self, ticker: str) -> bool:
        return ticker not in self.positions

    # -- mutations ---------------------------------------------------------

    def open_long(self, ticker: str, notional_usd: float, entry: float,
                  stop_loss: float, fractional: bool = False) -> None:
        qty = notional_usd / entry if entry > 0 else 0.0
        self.positions[ticker] = Position(
            ticker=ticker, side="LONG", notional_usd=notional_usd,
            qty=round(qty, 6), entry=entry, stop_loss=stop_loss,
            fractional=fractional,
        )

    def open_short(self, ticker: str, qty: float, entry: float,
                   stop_loss: float) -> None:
        whole_qty = int(qty)   # shorts are ALWAYS whole shares
        self.positions[ticker] = Position(
            ticker=ticker, side="SHORT", notional_usd=whole_qty * entry,
            qty=whole_qty, entry=entry, stop_loss=stop_loss, fractional=False,
        )

    def close(self, ticker: str) -> None:
        self.positions.pop(ticker, None)

    # -- aggregates ---------------------------------------------------------

    def all_short_value(self, current_prices: dict) -> float:
        """Current market value of all open shorts."""
        total = 0.0
        for ticker, pos in self.positions.items():
            if pos.side != "SHORT":
                continue
            price = current_prices.get(ticker, pos.entry)
            total += pos.qty * price
        return total

    def shorts_older_than(self, days: int) -> list:
        """Tickers of shorts held longer than `days` — for auto-close."""
        now = datetime.now()
        return [t for t, p in self.positions.items()
                if p.side == "SHORT" and (now - p.opened_at).days >= days]
