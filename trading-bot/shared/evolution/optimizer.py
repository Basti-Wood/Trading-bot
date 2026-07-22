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

WORKER_CORES = 5   # i5-9400: 6 cores total, leave 1 for OS + Docker

CHALLENGERS_DIR = os.environ.get("CHALLENGERS_DIR", "challengers")
LOGS_DIR = os.environ.get("LOGS_DIR", "logs")


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
        self.population = [AgentDNA.random() for _ in range(population_size)]
        self.best_ever: AgentDNA | None = None

    def evolve(self, generations: int = 500) -> AgentDNA:
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_path = os.path.join(LOGS_DIR, f"evolution_log_{self.asset_class}.csv")

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
