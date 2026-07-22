"""
shared/risk/manager.py — Risk Manager

Position sizes are always dollar/notional amounts — maps directly to Alpaca's
notional order parameter (fractional longs). Temperature drives thresholds,
sizing multipliers and stop widths (formulas from Prompt 2).
"""

from shared.evolution.dna import AgentDNA

SHORT_STOP_TIGHTEN = 0.6   # shorts get 40% tighter stops than longs


class RiskManager:
    def __init__(self, dna: AgentDNA, pool_config: dict):
        self.dna = dna
        self.pool_config = pool_config

        t = dna.temperature
        self.confidence_threshold = 0.80 - (t * 0.28)   # 0 -> 0.80, 1 -> 0.52
        self.position_size_mult = 0.5 + t                # 0 -> 0.5x, 1 -> 1.5x
        self.atr_multiplier = 3.0 - (t * 2.0)            # 0 -> 3.0 wide, 1 -> 1.0 tight

    def position_size_usd(self, portfolio_value: float) -> float:
        """Dollar amount to deploy — used directly as notional in orders."""
        return round(portfolio_value * self.dna.max_risk * self.position_size_mult, 2)

    def stop_loss(self, entry_price: float, side: str, atr: float) -> float:
        """
        LONG:  entry - atr * mult
        SHORT: entry + atr * mult * 0.6  (40% tighter — asymmetric risk)
        """
        if side in ("SELL", "SHORT"):
            return round(entry_price + atr * self.atr_multiplier * SHORT_STOP_TIGHTEN, 4)
        return round(entry_price - atr * self.atr_multiplier, 4)

    def short_exposure_ok(self, current_short_value: float,
                          portfolio_value: float) -> bool:
        """True if current shorts are below the pool's max short exposure cap."""
        cap = self.pool_config.get("max_short_exposure", self.dna.max_short_exposure)
        cap = min(cap, self.dna.max_short_exposure)
        return current_short_value < cap * portfolio_value
