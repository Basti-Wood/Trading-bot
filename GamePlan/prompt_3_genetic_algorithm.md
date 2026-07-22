# Trading Bot — Prompt 3/5: Genetic Algorithm & Evolution

## Target Hardware Context
- CPU: i5-9400 — 6 cores, no hyperthreading → always use Pool(5)
- RAM: 80GB → pre-load ALL historical data into memory at startup
- GPU: GTX 1060 6GB → used for LSTM training only, not GA

## Two-Phase Evolution (critical design decision)

Training 10,000 LSTMs from scratch per generation is infeasible on any hardware.
Instead the system uses two clearly separated phases:

```
Phase 1 — GA (CPU, ~2.8 hours per asset class):
  Evolve strategy params using fast rule-based backtest
  No LSTM involved — just RSI/MA/momentum rules driven by DNA params
  10,000 agents × 500 generations → finds optimal DNA

Phase 2 — LSTM training (GPU, ~30–60 min per asset class):
  Take the winning DNA from Phase 1
  Train ONE LSTM using those exact architecture params
  Save model weights to champions/<asset>_model.pt
```

The GA discovers the best hyperparameters. The LSTM is trained once using them.

---

## Performance Profile on i5-9400

```
Population : 10,000 agents
Workers    : Pool(5)  ← never Pool(6), always leave 1 core for OS + Docker
Batch size : 10,000 / 5 = 2,000 batches per generation
Time/gen   : 2,000 × ~10ms = ~20 seconds
500 gens   : ~2.8 hours per asset class
3 classes  : ~8.4 hours total (run sequentially — not enough cores for parallel)
RAM used   : ~150MB of historical data out of 80GB — pre-load all at startup
```

---

## AgentDNA (`shared/evolution/dna.py`)

```python
from dataclasses import dataclass, asdict, field
import random, copy

@dataclass
class AgentDNA:
    # LSTM architecture (used in Phase 2 training)
    hidden_size        : int   = 128     # choices: [64, 128, 256]
    num_layers         : int   = 2       # range: 1–3
    seq_len            : int   = 30      # range: 10–60
    dropout            : float = 0.2     # range: 0.1–0.5

    # Strategy / personality (used in Phase 1 rule-based backtest)
    temperature        : float = 0.5     # range: 0.0–1.0

    # Risk (used in both phases)
    max_risk           : float = 0.02    # range: 0.01–0.05
    atr_mult           : float = 2.0     # range: 1.0–4.0

    # Shorting behaviour
    short_enabled      : bool  = True    # GA decides if shorting helps per asset
    max_short_exposure : float = 0.5     # range: 0.1–0.8
    short_hold_days    : int   = 3       # range: 1–10

    # Metadata (not evolved — set at runtime)
    last_sharpe        : float = 0.0
    generation         : int   = 0

    @staticmethod
    def random() -> "AgentDNA":
        return AgentDNA(
            hidden_size        = random.choice([64, 128, 256]),
            num_layers         = random.randint(1, 3),
            seq_len            = random.randint(10, 60),
            dropout            = round(random.uniform(0.1, 0.5), 2),
            temperature        = round(random.uniform(0.0, 1.0), 2),
            max_risk           = round(random.uniform(0.01, 0.05), 3),
            atr_mult           = round(random.uniform(1.0, 4.0), 2),
            short_enabled      = random.choice([True, False]),
            max_short_exposure = round(random.uniform(0.1, 0.8), 2),
            short_hold_days    = random.randint(1, 10),
        )

    def mutate(self, rate: float = 0.2) -> "AgentDNA":
        child = copy.deepcopy(self)
        if random.random() < rate:
            child.hidden_size        = random.choice([64, 128, 256])
        if random.random() < rate:
            child.temperature        = max(0.0, min(1.0,
                child.temperature + random.uniform(-0.1, 0.1)))
        if random.random() < rate:
            child.max_risk           = max(0.01, min(0.05,
                child.max_risk + random.uniform(-0.005, 0.005)))
        if random.random() < rate:
            child.seq_len            = max(10, min(60,
                child.seq_len + random.randint(-5, 5)))
        if random.random() < rate:
            child.short_enabled      = not child.short_enabled
        if random.random() < rate:
            child.max_short_exposure = max(0.1, min(0.8,
                child.max_short_exposure + random.uniform(-0.05, 0.05)))
        if random.random() < rate:
            child.atr_mult           = max(1.0, min(4.0,
                child.atr_mult + random.uniform(-0.2, 0.2)))
        return child

    def crossover(self, other: "AgentDNA") -> "AgentDNA":
        child = AgentDNA()
        for f in asdict(self):
            if f in ("last_sharpe", "generation"):
                continue
            setattr(child, f, getattr(self if random.random() < 0.5 else other, f))
        return child
```

---

## Fitness Function (`shared/backtest/engine.py`)

Phase 1 uses a fast rule-based backtest — no LSTM, no GPU.
DNA params drive the strategy rules directly.

```python
def run_backtest(dna: AgentDNA, historical_data: pd.DataFrame) -> list[float]:
    """
    Fast rule-based backtest — CPU only, no LSTM.
    Uses dna.temperature to derive confidence_threshold, position_size_mult, atr_mult.
    Uses dna.short_enabled / max_short_exposure / short_hold_days for short logic.
    Uses RSI + MA cross as the signal source (same as fallback strategy).
    Simulates transaction cost of 0.001 per trade.
    Returns list of daily % returns — never uses future data.
    """

def fitness(dna: AgentDNA, historical_data: pd.DataFrame) -> float:
    returns = run_backtest(dna, historical_data)
    if len(returns) == 0 or np.std(returns) == 0:
        return -999.0
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
    dna.last_sharpe = sharpe
    return float(sharpe)
```

---

## GeneticOptimizer (`shared/evolution/optimizer.py`)

```python
import multiprocessing as mp
import numpy as np, csv, json, os, random
from dataclasses import asdict

WORKER_CORES = 5   # i5-9400: 6 cores total, leave 1 for OS + Docker

class GeneticOptimizer:
    def __init__(self,
                 asset_class     : str,
                 population_size : int   = 10_000,
                 survivor_rate   : float = 0.10,
                 historical_data          = None):
        self.asset_class  = asset_class
        self.pop_size     = population_size
        self.keep         = int(population_size * survivor_rate)   # 1,000 survivors
        self.data         = historical_data    # pre-loaded into RAM — no disk I/O
        self.population   = [AgentDNA.random() for _ in range(population_size)]
        self.best_ever    = None

    def evolve(self, generations: int = 500) -> AgentDNA:
        log_path = f"logs/evolution_log_{self.asset_class}.csv"

        for gen in range(generations):
            # Parallel fitness evaluation — Pool(5) for i5-9400
            with mp.Pool(processes=WORKER_CORES) as pool:
                scores = pool.starmap(
                    fitness,
                    [(dna, self.data) for dna in self.population]
                )

            # Rank descending by Sharpe
            ranked    = sorted(zip(scores, self.population),
                               key=lambda x: x[0], reverse=True)
            survivors = [dna for _, dna in ranked[:self.keep]]
            best_score, best_dna = ranked[0]

            # Track all-time best — checkpoint immediately on improvement
            if self.best_ever is None or best_score > self.best_ever.last_sharpe:
                self.best_ever = best_dna
                self._checkpoint(best_dna)

            # Log generation stats
            avg_score = float(np.mean(scores))
            print(f"[{self.asset_class}] Gen {gen+1:>4}/{generations} | "
                  f"Sharpe best: {best_score:.3f} avg: {avg_score:.3f} | "
                  f"temp: {best_dna.temperature:.2f} | "
                  f"short: {best_dna.short_enabled} | "
                  f"seq_len: {best_dna.seq_len}")
            self._log_csv(log_path, gen+1, best_score, avg_score, best_dna)

            # Breed next generation
            next_gen = list(survivors)    # elites survive unchanged
            while len(next_gen) < self.pop_size:
                a, b = random.choices(survivors, k=2)
                next_gen.append(a.crossover(b).mutate())
            self.population = next_gen

        return self.best_ever

    def _checkpoint(self, dna: AgentDNA):
        """Atomic write — safe even if process is killed mid-write."""
        path = f"challengers/{self.asset_class}_challenger.json"
        tmp  = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(dna), f, indent=2)
        os.replace(tmp, path)   # atomic on Linux (Docker runs Linux)

    def _log_csv(self, path, gen, best, avg, dna):
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["generation", "best_sharpe", "avg_sharpe",
                             "temperature", "short_enabled", "hidden_size",
                             "seq_len", "max_risk", "atr_mult"])
            w.writerow([gen, round(best, 4), round(avg, 4),
                        dna.temperature, dna.short_enabled,
                        dna.hidden_size, dna.seq_len,
                        dna.max_risk, dna.atr_mult])
```

---

## Background Evolver (`shared/evolution/background_evolver.py`)

Runs GA continuously in a daemon thread after initial evolution completes.
Keeps a fresh challenger ready for the circuit breaker at all times.

```python
import threading, json, os
from dataclasses import asdict

class BackgroundEvolver:
    def __init__(self, asset_class: str, historical_data):
        self.asset_class = asset_class
        self.data        = historical_data   # already in RAM — passed by reference
        self._lock       = threading.Lock()
        self._challenger = None

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        # Reuse same optimizer — population carries over between weekly batches
        optimizer = GeneticOptimizer(
            self.asset_class,
            population_size = 10_000,
            historical_data = self.data
        )
        while True:
            best = optimizer.evolve(generations=50)   # ~28 min per weekly batch
            with self._lock:
                self._challenger = best
                self._save(best)

    def get_challenger(self) -> "AgentDNA | None":
        with self._lock:
            return self._challenger

    def _save(self, dna: AgentDNA):
        path = f"challengers/{self.asset_class}_challenger.json"
        tmp  = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(dna), f, indent=2)
        os.replace(tmp, path)
        print(f"✅ [{self.asset_class}] New challenger | "
              f"Sharpe: {dna.last_sharpe:.3f} | "
              f"temp: {dna.temperature:.2f} | "
              f"short: {dna.short_enabled}")
```

---

## Evolver Entry Point (`evolver/evolver.py`)

```python
from shared.data.fetcher import fetch_historical
from shared.evolution.optimizer import GeneticOptimizer
from shared.strategy.ml_model import train_lstm
import json, torch
from dataclasses import asdict

# Pre-load ALL historical data into RAM at startup
# 80GB RAM makes this trivial — ~150MB total
print("Pre-loading historical data into RAM...")
ALL_DATA = {
    "crypto" : fetch_historical("BTC/USD", years=3),
    "stocks" : fetch_historical(["AAPL", "MSFT", "NVDA", "AMZN"], years=3),
    "etf"    : fetch_historical(["SPY", "QQQ", "VTI"], years=3),
}
print("All data loaded. Starting evolution.")

# Run asset classes sequentially — not enough cores to run in parallel
for asset_class, data in ALL_DATA.items():
    print(f"\n{'='*50}")
    print(f"Phase 1: Evolving {asset_class} (10k agents, 500 gen, 5 cores)")
    print(f"Expected time: ~2.8 hours")
    print(f"{'='*50}")

    # Phase 1: GA finds best DNA using fast rule-based backtest (CPU)
    optimizer = GeneticOptimizer(
        asset_class     = asset_class,
        population_size = 10_000,
        survivor_rate   = 0.10,
        historical_data = data,
    )
    best_dna = optimizer.evolve(generations=500)

    print(f"\nPhase 2: Training LSTM for {asset_class} on GPU...")
    print(f"DNA: hidden={best_dna.hidden_size} layers={best_dna.num_layers} "
          f"seq={best_dna.seq_len} temp={best_dna.temperature:.2f}")

    # Phase 2: Train ONE LSTM using winning DNA (GPU)
    model = train_lstm(best_dna, data)
    torch.save(model.state_dict(), f"champions/{asset_class}_model.pt")

    # Save champion DNA
    with open(f"champions/{asset_class}_champion.json", "w") as f:
        json.dump(asdict(best_dna), f, indent=2)

    print(f"✅ {asset_class} champion saved | Sharpe: {best_dna.last_sharpe:.3f}")

print("\n🏆 All asset classes evolved. Start trading bots now.")
```

---

## What Evolved DNA Tells You

After evolution completes, inspect `logs/evolution_log_<asset>.csv`:

| Pattern in log | Meaning |
|---|---|
| `temperature` converges high (>0.7) | Market rewards aggression for this asset |
| `temperature` converges low (<0.3) | Be conservative — aggression is punished |
| `short_enabled` evolves to False | Shorting hurts returns — disable it |
| `short_enabled` evolves to True | Shorting adds alpha — keep it |
| `seq_len` converges high (50+) | Long-range patterns matter |
| `seq_len` converges low (<15) | Short-term signals dominate |
| Sharpe plateau after ~200 gen | Evolution has converged — stop early |

Trust the GA's conclusions — they come from data, not intuition.
