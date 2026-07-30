"""
shared/evolution/optimizer.py — Genetic Optimizer (GA Phase 1)

Evolves 10,000 agents over hundreds of generations using Pool(5).
Fitness = Sharpe ratio from the fast rule-based backtest.
Checkpoints best DNA after every improvement (atomic write).
"""

import multiprocessing as mp
import numpy as np
import csv
import json
import os
import random
from dataclasses import asdict

from shared.evolution.dna import AgentDNA
from shared.backtest.engine import fitness

WORKER_CORES = int(os.environ.get("GA_WORKER_CORES", 5))  # GA fitness eval parallelism

CHAMPIONS_DIR = os.environ.get("CHAMPIONS_DIR", "champions")
CHALLENGERS_DIR = os.environ.get("CHALLENGERS_DIR", "challengers")
LOGS_DIR = os.environ.get("LOGS_DIR", "logs")

# Fraction of population seeded from previous champion (rest random)
SEED_FRACTION = 0.20   # 20% champion-derived, 80% fresh blood


class GeneticOptimizer:
    def __init__(self,
                 asset_class: str,
                 population_size: int = 10_000,
                 survivor_rate: float = 0.10,
                 historical_data=None):
        self.asset_class = asset_class
        self.pop_size = population_size
        self.keep = max(2, int(population_size * survivor_rate))
        self.data = historical_data          # pre-loaded into RAM — no disk I/O
        self.population = self._seed_population(population_size)
        self.best_ever: AgentDNA | None = None

    def _seed_population(self, size: int) -> list:
        """Warm-start: seed ~20% from previous champion, 80% random."""
        champion = self._load_champion_dna()
        if champion is None:
            print(f"[{self.asset_class}] No previous champion — fresh random population.")
            return [AgentDNA.random() for _ in range(size)]

        seeded = max(2, int(size * SEED_FRACTION))
        random_count = size - seeded
        print(f"[{self.asset_class}] Warm-start: {seeded} from champion "
              f"(Sharpe {champion.last_sharpe:.3f}) + {random_count} random agents.")

        pop = []
        # Champion itself survives unchanged
        pop.append(champion)
        # Slightly mutated champion variants
        for _ in range(seeded - 1):
            pop.append(champion.clone().mutate())
        # Fresh random blood
        for _ in range(random_count):
            pop.append(AgentDNA.random())
        return pop

    def _load_champion_dna(self) -> "AgentDNA | None":
        """Try loading the previously saved champion DNA from disk."""
        path = os.path.join(CHAMPIONS_DIR, f"{self.asset_class}_champion.json")
        try:
            with open(path) as f:
                return AgentDNA.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _diagnose_data(self) -> None:
        """Run one backtest in the main process to verify data integrity."""
        if not self.data:
            print(f"[{self.asset_class}] DIAGNOSE: self.data is empty/falsy!")
            return
        if isinstance(self.data, dict):
            syms = list(self.data.keys())
            sample_sym = syms[0]
            sample_df = self.data[sample_sym]
            print(f"[{self.asset_class}] DIAGNOSE: {len(syms)} symbols, "
                  f"sample='{sample_sym}' rows={len(sample_df)} "
                  f"cols={list(sample_df.columns)}")
            # Show a few close prices
            if "close" in sample_df.columns and len(sample_df) > 0:
                closes = sample_df["close"].dropna()
                print(f"[{self.asset_class}] DIAGNOSE: close range "
                      f"[{closes.min():.2f}, {closes.max():.2f}], "
                      f"first={closes.iloc[0]:.2f}, last={closes.iloc[-1]:.2f}")
            # Run a test backtest
            test_dna = AgentDNA.random()
            test_dna.temperature = 0.5  # neutral temp
            score = fitness(test_dna, self.data)
            print(f"[{self.asset_class}] DIAGNOSE: test fitness (temp=0.5) = {score:.4f}")
        else:
            print(f"[{self.asset_class}] DIAGNOSE: data type={type(self.data).__name__}, "
                  f"len={len(self.data) if hasattr(self.data, '__len__') else 'N/A'}")

    def evolve(self, generations: int = 500) -> AgentDNA:
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_path = os.path.join(LOGS_DIR, f"evolution_log_{self.asset_class}.csv")

        # ---- Quick diagnostic: run one backtest in the main process ----
        self._diagnose_data()

        for gen in range(generations):
            # Parallel fitness evaluation — Pool(5) for i5-9400
            with mp.Pool(processes=WORKER_CORES) as pool:
                scores = pool.starmap(
                    fitness,
                    [(dna, self.data) for dna in self.population]
                )

            # pool.starmap runs in child processes — dna.last_sharpe was set
            # in the child copy, so re-apply scores in the parent:
            for dna, score in zip(self.population, scores):
                dna.last_sharpe = score
                dna.generation = gen + 1

            # Rank descending by Sharpe
            ranked = sorted(zip(scores, self.population),
                            key=lambda x: x[0], reverse=True)
            survivors = [dna for _, dna in ranked[:self.keep]]
            best_score, best_dna = ranked[0]

            # Track all-time best — checkpoint immediately on improvement
            if self.best_ever is None or best_score > self.best_ever.last_sharpe:
                self.best_ever = best_dna
                self._checkpoint(best_dna)

            avg_score = float(np.mean(scores))
            short_state = best_dna.short_enabled
            print(f"[{self.asset_class}] Gen {gen + 1:>4}/{generations} | "
                  f"Sharpe best: {best_score:.3f} avg: {avg_score:.3f} | "
                  f"temp: {best_dna.temperature:.2f} | "
                  f"short: {short_state} | "
                  f"seq_len: {best_dna.seq_len}")
            self._log_csv(log_path, gen + 1, best_score, avg_score, best_dna)

            # Breed next generation — elites survive unchanged
            next_gen = list(survivors)
            while len(next_gen) < self.pop_size:
                a, b = random.choices(survivors, k=2)
                next_gen.append(a.crossover(b).mutate())
            self.population = next_gen

        return self.best_ever

    def _checkpoint(self, dna: AgentDNA) -> None:
        """Atomic write — safe even if process is killed mid-write."""
        os.makedirs(CHALLENGERS_DIR, exist_ok=True)
        path = os.path.join(CHALLENGERS_DIR, f"{self.asset_class}_challenger.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(dna), f, indent=2)
        os.replace(tmp, path)   # atomic on Linux (Docker runs Linux)

    def _log_csv(self, path: str, gen: int, best: float, avg: float,
                 dna: AgentDNA) -> None:
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
