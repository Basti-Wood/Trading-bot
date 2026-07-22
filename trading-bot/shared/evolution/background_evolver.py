"""
shared/evolution/background_evolver.py — Background Challenger Evolution

Runs GA continuously in a daemon thread after initial evolution completes.
Keeps a fresh challenger ready for the circuit breaker at all times.
"""

import threading
import json
import os
from dataclasses import asdict

from shared.evolution.dna import AgentDNA
from shared.evolution.optimizer import GeneticOptimizer, CHALLENGERS_DIR


class BackgroundEvolver:
    def __init__(self, asset_class: str, historical_data,
                 population_size: int = 10_000):
        self.asset_class = asset_class
        self.data = historical_data          # already in RAM — passed by reference
        self.population_size = population_size
        self._lock = threading.Lock()
        self._challenger: AgentDNA | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        # Reuse same optimizer — population carries over between weekly batches
        optimizer = GeneticOptimizer(
            self.asset_class,
            population_size=self.population_size,
            historical_data=self.data,
        )
        while not self._stop_event.is_set():
            best = optimizer.evolve(generations=50)   # ~28 min per weekly batch
            with self._lock:
                self._challenger = best
                self._save(best)

    def get_challenger(self) -> "AgentDNA | None":
        with self._lock:
            return self._challenger

    def _save(self, dna: AgentDNA) -> None:
        os.makedirs(CHALLENGERS_DIR, exist_ok=True)
        path = os.path.join(CHALLENGERS_DIR, f"{self.asset_class}_challenger.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(dna), f, indent=2)
        os.replace(tmp, path)
        print(f"[{self.asset_class}] New challenger | "
              f"Sharpe: {dna.last_sharpe:.3f} | "
              f"temp: {dna.temperature:.2f} | "
              f"short: {dna.short_enabled}")
