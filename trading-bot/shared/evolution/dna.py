"""
shared/evolution/dna.py — Agent DNA

Encodes all evolvable hyperparameters for a trading agent.
GA evolves 10,000 agents over 500 generations to discover optimal DNA.
"""

from dataclasses import dataclass, asdict
import random
import copy


@dataclass
class AgentDNA:
    """
    Agent DNA — all evolvable parameters.

    LSTM architecture (used in Phase 2 training):
        hidden_size, num_layers, seq_len, dropout

    Strategy / personality (used in Phase 1 rule-based backtest):
        temperature

    Risk (used in both phases):
        max_risk, atr_mult

    Shorting behaviour:
        short_enabled, max_short_exposure, short_hold_days

    Metadata (not evolved — set at runtime):
        last_sharpe, generation
    """

    # LSTM architecture
    hidden_size: int = 128       # choices: [64, 128, 256]
    num_layers: int = 2          # range: 1–3
    seq_len: int = 30            # range: 10–60
    dropout: float = 0.2         # range: 0.1–0.5

    # Strategy / personality
    temperature: float = 0.5     # range: 0.0–1.0

    # Risk
    max_risk: float = 0.02       # range: 0.01–0.05  (max % of portfolio per trade)
    atr_mult: float = 2.0        # range: 1.0–4.0    (ATR multiplier for stop-loss)

    # Shorting behaviour
    short_enabled: bool = True           # GA decides if shorting helps per asset
    max_short_exposure: float = 0.5      # range: 0.1–0.8  (max fraction of pool in shorts)
    short_hold_days: int = 3             # range: 1–10     (auto-close shorts older than this)

    # Metadata (not evolved — set at runtime)
    last_sharpe: float = 0.0
    generation: int = 0

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @staticmethod
    def random() -> "AgentDNA":
        """Generate a completely random DNA for initialising a population."""
        return AgentDNA(
            hidden_size=random.choice([64, 128, 256]),
            num_layers=random.randint(1, 3),
            seq_len=random.randint(10, 60),
            dropout=round(random.uniform(0.1, 0.5), 2),
            temperature=round(random.uniform(0.0, 1.0), 2),
            max_risk=round(random.uniform(0.01, 0.05), 3),
            atr_mult=round(random.uniform(1.0, 4.0), 2),
            short_enabled=random.choice([True, False]),
            max_short_exposure=round(random.uniform(0.1, 0.8), 2),
            short_hold_days=random.randint(1, 10),
        )

    # ------------------------------------------------------------------
    # Genetic operators
    # ------------------------------------------------------------------

    def mutate(self, rate: float = 0.2) -> "AgentDNA":
        """
        Mutate this DNA with per-gene probability `rate`.
        Returns a new mutated copy — does not modify self.
        """
        child = copy.deepcopy(self)

        if random.random() < rate:
            child.hidden_size = random.choice([64, 128, 256])

        if random.random() < rate:
            child.num_layers = random.randint(1, 3)

        if random.random() < rate:
            child.seq_len = max(10, min(60,
                child.seq_len + random.randint(-5, 5)))

        if random.random() < rate:
            child.dropout = round(max(0.1, min(0.5,
                child.dropout + random.uniform(-0.1, 0.1))), 2)

        if random.random() < rate:
            child.temperature = round(max(0.0, min(1.0,
                child.temperature + random.uniform(-0.1, 0.1))), 2)

        if random.random() < rate:
            child.max_risk = round(max(0.01, min(0.05,
                child.max_risk + random.uniform(-0.005, 0.005))), 3)

        if random.random() < rate:
            child.atr_mult = round(max(1.0, min(4.0,
                child.atr_mult + random.uniform(-0.2, 0.2))), 2)

        if random.random() < rate:
            child.short_enabled = not child.short_enabled

        if random.random() < rate:
            child.max_short_exposure = round(max(0.1, min(0.8,
                child.max_short_exposure + random.uniform(-0.05, 0.05))), 2)

        if random.random() < rate:
            child.short_hold_days = max(1, min(10,
                child.short_hold_days + random.randint(-2, 2)))

        # Reset fitness — mutated DNA hasn't been evaluated yet
        child.last_sharpe = 0.0

        return child

    def crossover(self, other: "AgentDNA") -> "AgentDNA":
        """
        Uniform crossover: each gene comes from either parent with 50% probability.
        Returns a new child DNA.
        """
        child = AgentDNA()
        for field_name in asdict(self):
            if field_name in ("last_sharpe", "generation"):
                continue
            parent = self if random.random() < 0.5 else other
            setattr(child, field_name, getattr(parent, field_name))
        return child

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Convert DNA to a JSON-serializable dict."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "AgentDNA":
        """Reconstruct DNA from a dict (e.g. loaded from JSON)."""
        return AgentDNA(**data)
