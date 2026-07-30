"""
evolver/evolver.py — Continuous Evolution Engine (24/7)

Phase 1: GA (CPU) finds best DNA using fast rule-based backtest.
Phase 2: Train ONE LSTM (GPU) using the winning DNA.

Runs continuously — when markets are closed the evolver ramps up
generations and retrains LSTM models. During market hours it runs
lighter evolution cycles so the bots get CPU priority.

Asset classes run sequentially (ThreadPool + mp.Pool = fork deadlock).
Use GA_WORKER_CORES to control per-asset parallelism (default 5 cores).

Env vars (all optional, defaults shown):
  EVOLVER_CONTINUOUS=true        # keep evolving forever
  EVOLVER_INTERVAL_HOURS=6       # hours between full evolution cycles
  OFF_HOURS_GENERATIONS=500      # generations when market is closed
  MARKET_HOURS_GENERATIONS=50    # generations when market is open
  OFF_HOURS_LSTM=true            # retrain LSTM models during off-hours
"""

import json
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

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

# Continuous mode settings
CONTINUOUS = os.environ.get("EVOLVER_CONTINUOUS", "true").lower() != "false"
INTERVAL_HOURS = float(os.environ.get("EVOLVER_INTERVAL_HOURS", "6"))
OFF_HOURS_GEN = int(os.environ.get("OFF_HOURS_GENERATIONS", 500))
MARKET_HOURS_GEN = int(os.environ.get("MARKET_HOURS_GENERATIONS", 50))
OFF_HOURS_LSTM = os.environ.get("OFF_HOURS_LSTM", "true").lower() != "false"

ET = ZoneInfo("America/New_York")


def _us_market_open() -> bool:
    """True during NYSE regular trading hours (09:30–16:00 ET, Mon–Fri)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:   # Sat/Sun
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def _evolve_asset(asset_class: str, data, generations: int,
                  train_model: bool) -> None:
    """Run one full GA phase + optional LSTM training for a single asset."""
    print(f"\n{'=' * 50}")
    print(f"Phase 1: Evolving {asset_class} "
          f"({POPULATION} agents, {generations} gen, 5 cores)")
    print(f"{'=' * 50}")

    optimizer = GeneticOptimizer(
        asset_class=asset_class,
        population_size=POPULATION,
        survivor_rate=0.10,
        historical_data=data,
    )
    best_dna = optimizer.evolve(generations=generations)

    # Always save champion DNA (allows fallback-only trading without LSTM)
    champion_path = os.path.join(CHAMPIONS_DIR, f"{asset_class}_champion.json")
    model_path = os.path.join(CHAMPIONS_DIR, f"{asset_class}_model.pt")
    with open(champion_path, "w") as f:
        json.dump(asdict(best_dna), f, indent=2)
    print(f"{asset_class} champion DNA saved | Sharpe: {best_dna.last_sharpe:.3f}")

    if not train_model:
        print(f"[{asset_class}] Skipping LSTM training (market-hours mode).")
        return

    print(f"\nPhase 2: Training LSTM for {asset_class} on "
          f"{'GPU' if torch.cuda.is_available() else 'CPU'}...")
    print(f"DNA: hidden={best_dna.hidden_size} layers={best_dna.num_layers} "
          f"seq={best_dna.seq_len} temp={best_dna.temperature:.2f}")

    try:
        model = train_lstm(best_dna, data, asset_class=asset_class)
        torch.save(model.state_dict(), model_path)
        print(f"[{asset_class}] LSTM model saved.")
    except Exception as exc:
        if os.path.exists(model_path):
            os.remove(model_path)
        print(f"[evolver] LSTM training failed for {asset_class}: {exc}")
        print(f"[evolver] Keeping DNA-only champion for {asset_class} "
              f"(bot will run fallback strategy until next successful training).")


def main() -> None:
    print("Pre-loading historical data into RAM (this may take a few minutes)...")
    all_data = preload_all_data(years=3)
    print("All data loaded. Starting evolution engine.")

    os.makedirs(CHAMPIONS_DIR, exist_ok=True)

    # ---- First run: full evolution for all asset classes ----
    print("\n" + "=" * 60)
    print("INITIAL FULL EVOLUTION — all asset classes, max generations")
    print("=" * 60)

    for asset_class in EVOLVE_CLASSES:
        data = all_data.get(asset_class)
        if not data:
            print(f"[evolver] No data for {asset_class} — skipping.")
            continue
        _evolve_asset(asset_class, data, generations=GENERATIONS, train_model=True)

    print("\nAll asset classes evolved. Entering continuous mode.\n")

    if not CONTINUOUS:
        print("[evolver] EVOLVER_CONTINUOUS=false — exiting after initial run.")
        return

    # ---- Continuous loop ----
    cycle = 0
    while True:
        cycle += 1
        market_open = _us_market_open()
        generations = MARKET_HOURS_GEN if market_open else OFF_HOURS_GEN
        train_lstm_now = not market_open and OFF_HOURS_LSTM

        status = "MARKET OPEN — light evolution" if market_open else "MARKET CLOSED — full training"
        print(f"\n{'#' * 60}")
        print(f"CYCLE {cycle} | {status}")
        print(f"Generations: {generations} | LSTM training: {train_lstm_now}")
        print(f"Next cycle in ~{INTERVAL_HOURS}h")
        print(f"{'#' * 60}")

        for asset_class in EVOLVE_CLASSES:
            data = all_data.get(asset_class)
            if not data:
                continue
            try:
                _evolve_asset(asset_class, data,
                              generations=generations,
                              train_model=train_lstm_now)
            except Exception as exc:
                print(f"[evolver] {asset_class} cycle failed: {exc}")
                continue

        # Sleep until next cycle
        sleep_sec = INTERVAL_HOURS * 3600
        print(f"\n[evolver] Sleeping {INTERVAL_HOURS}h until next cycle...")
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
