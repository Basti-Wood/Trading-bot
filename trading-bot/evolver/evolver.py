"""
evolver/evolver.py — Evolution Entry Point

Phase 1: GA (CPU, ~2.8h/asset) finds best DNA using fast rule-based backtest.
Phase 2: Train ONE LSTM (GPU, ~30-60 min/asset) using the winning DNA.
Asset classes run sequentially — not enough cores for parallel.
"""

import json
import os
import sys

import torch
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.data.fetcher import preload_all_data
from shared.evolution.optimizer import GeneticOptimizer
from shared.strategy.ml_model import train_lstm

CHAMPIONS_DIR = os.environ.get("CHAMPIONS_DIR", "champions")
POPULATION = int(os.environ.get("GA_POPULATION", 10_000))
GENERATIONS = int(os.environ.get("GA_GENERATIONS", 500))
EVOLVE_CLASSES = ["crypto", "stocks", "etf"]   # politician + funds are rule-based


def main() -> None:
    # Pre-load ALL historical data into RAM at startup (80GB — trivial)
    print("Pre-loading historical data into RAM...")
    all_data = preload_all_data(years=3)
    print("All data loaded. Starting evolution.")

    os.makedirs(CHAMPIONS_DIR, exist_ok=True)

    for asset_class in EVOLVE_CLASSES:
        data = all_data.get(asset_class)
        if not data:
            print(f"[evolver] No data for {asset_class} — skipping.")
            continue

        print(f"\n{'=' * 50}")
        print(f"Phase 1: Evolving {asset_class} "
              f"({POPULATION} agents, {GENERATIONS} gen, 5 cores)")
        print(f"{'=' * 50}")

        # Phase 1: GA finds best DNA using fast rule-based backtest (CPU)
        optimizer = GeneticOptimizer(
            asset_class=asset_class,
            population_size=POPULATION,
            survivor_rate=0.10,
            historical_data=data,
        )
        best_dna = optimizer.evolve(generations=GENERATIONS)

        print(f"\nPhase 2: Training LSTM for {asset_class} on "
              f"{'GPU' if torch.cuda.is_available() else 'CPU'}...")
        print(f"DNA: hidden={best_dna.hidden_size} layers={best_dna.num_layers} "
              f"seq={best_dna.seq_len} temp={best_dna.temperature:.2f}")

        # Phase 2: Train ONE LSTM using winning DNA
        model = train_lstm(best_dna, data, asset_class=asset_class)
        torch.save(model.state_dict(),
                   os.path.join(CHAMPIONS_DIR, f"{asset_class}_model.pt"))

        with open(os.path.join(CHAMPIONS_DIR, f"{asset_class}_champion.json"), "w") as f:
            json.dump(asdict(best_dna), f, indent=2)

        print(f"{asset_class} champion saved | Sharpe: {best_dna.last_sharpe:.3f}")

    print("\nAll asset classes evolved. Start trading bots now.")


if __name__ == "__main__":
    main()
