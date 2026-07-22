"""
shared/monitoring/ledger.py — Performance Ledger

Records every champion's lifecycle: promotion, demotion, performance.
Backs the dashboard leaderboard and demotion analysis.
"""

import csv
import os
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Optional

LOGS_DIR = os.environ.get("LOGS_DIR", "logs")


@dataclass
class BotRecord:
    asset_class: str
    generation: int
    promoted_at: datetime
    demoted_at: Optional[datetime] = None
    peak_value: float = 0.0
    final_value: float = 0.0
    max_drawdown: float = 0.0
    sharpe_live: float = 0.0
    total_trades: int = 0
    short_trades: int = 0
    reason_demoted: Optional[str] = None   # "5% drawdown" | "weekly review" | "manual"


class PerformanceLedger:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(LOGS_DIR, "ledger.csv")

    def log(self, record: BotRecord) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        write_header = not os.path.exists(self.path)
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow([fld.name for fld in fields(BotRecord)])
            w.writerow([getattr(record, fld.name) for fld in fields(BotRecord)])

    def _read_all(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, newline="") as f:
            return list(csv.DictReader(f))

    def average_lifespan(self, asset_class: str) -> float:
        """Average days a champion survives before demotion."""
        lifespans = []
        for row in self._read_all():
            if row["asset_class"] != asset_class or not row.get("demoted_at"):
                continue
            try:
                promoted = datetime.fromisoformat(row["promoted_at"])
                demoted = datetime.fromisoformat(row["demoted_at"])
                lifespans.append((demoted - promoted).total_seconds() / 86400)
            except (ValueError, KeyError):
                continue
        return round(sum(lifespans) / len(lifespans), 2) if lifespans else 0.0

    def demotion_reasons(self) -> dict:
        """Breakdown of why bots got demoted — useful for tuning thresholds."""
        reasons: dict = {}
        for row in self._read_all():
            reason = row.get("reason_demoted") or ""
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        return reasons

    def current_champion_stats(self, asset_class: str) -> Optional[dict]:
        """Most recent (non-demoted) record for an asset class, if any."""
        rows = [r for r in self._read_all() if r["asset_class"] == asset_class]
        if not rows:
            return None
        return rows[-1]
