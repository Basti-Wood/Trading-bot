# Trading Bot — Prompt 1/5: Overview & Architecture

## Project Goal
Build a multi-asset Python trading system with specialized bots per asset class.
Each bot uses a PyTorch LSTM as its primary prediction strategy, with an RSI+Trend fallback.
Optimal hyperparameters are discovered via a Genetic Algorithm (GA) that evolves 10,000 agents
over hundreds of generations. Live bots are monitored by a circuit breaker; underperforming
champions are automatically demoted and replaced by a background-evolved challenger.
Bots can go LONG and SHORT — profiting in both bull and bear markets.
The entire system runs in Docker — each bot and the evolver are isolated containers.

---

## Target Hardware
- **CPU:** Intel i5-9400 (6 cores, 6 threads — no hyperthreading)
- **GPU:** NVIDIA GTX 1060 6GB
- **RAM:** 80GB DDR4

### Hardware notes
- CPU is the GA bottleneck — use `multiprocessing.Pool(5)` everywhere (leave 1 core for OS + Docker)
- GPU is used for LSTM training only — inference runs on CPU (faster for single predictions)
- 80GB RAM means all historical data can be pre-loaded into memory at startup — zero disk I/O during GA runs
- GTX 1060 6GB VRAM is far more than needed — LSTM models are ~2–8MB

---

## Asset Classes & Bots
| Bot | Market | Hours | Shorting |
|---|---|---|---|
| `bot_crypto` | BTC, ETH, SOL etc. | 24/7 | ✅ | Important, Honeypot scams should be ignored
| `bot_stocks` | AAPL, MSFT, NVDA etc. | 09:30–16:00 ET | ✅ |
| `bot_etf` | SPY, QQQ, VTI etc. | 09:30–16:00 ET | ✅ |
| `bot_politician` | US stocks only | 09:30–16:00 ET | ✅ |
| `bot_funds` | VYM, JEPI, SCHD etc. | Any | ❌ Never |
| `evolver` | Runs GA for all asset classes | Always | — |
| `dashboard` | Unified monitoring UI | Always | — |

---

## Tech Stack
- **Language:** Python 3.10+
- **ML:** PyTorch (LSTM)
- **Broker:** Alpaca (`alpaca-py`) — margin account required for shorting
- **Market data:** `yfinance`, Alpaca OHLCV
- **Politician data:** Quiver Quantitative API (free tier)
- **Features:** `pandas`, `numpy`, `ta`
- **Parallelism:** `multiprocessing.Pool(5)` (GA), `threading` (background evolver)
- **Containerisation:** Docker + Docker Compose
- **GPU:** NVIDIA Container Toolkit (PyTorch CUDA in Docker)
- **Deps:** `torch`, `alpaca-py`, `yfinance`, `pandas`, `numpy`, `ta`, `scikit-learn`, `joblib`, `requests`

## Fractional Trading
Alpaca supports fractional shares on 5,000+ US equities — enabled by default on all accounts.
The risk manager always calculates position sizes as dollar amounts (`notional`), which maps
directly to Alpaca's notional order parameter. No share quantity math needed for longs.

| Order type | Fractional? | How |
|---|---|---|
| Long (fractionable asset) | ✅ Yes | Pass `notional=$100` — buys $100 exactly |
| Long (non-fractionable) | ⚠️ Whole shares | Round down: `qty = int(notional // price)` |
| Short | ❌ Never | Always whole shares — Alpaca hard limit |

Always check `client.get_asset(symbol).fractionable` before placing a fractional order.
Minimum notional: $1. Qty precision: up to 6 decimal places.

---

## Why Docker
Each bot and the evolver run in their own container:
- A crashed crypto bot does not affect the stocks bot
- The GA evolver (CPU intensive) is isolated — cannot starve live bots
- All containers share named volumes for champions, challengers, ledger, and archive
- Restart, update, or redeploy a single container without touching the others
- Identical behaviour on local machine, VPS, or cloud server

---

## Project Structure
```
trading-bot/
├── shared/                           # Shared Python modules (mounted into all containers)
│   ├── data/
│   │   ├── fetcher.py
│   │   ├── preprocessor.py
│   │   └── politician_trades.py
│   ├── strategy/
│   │   ├── ml_model.py
│   │   └── fallback.py
│   ├── risk/
│   │   ├── manager.py
│   │   └── position_tracker.py
│   ├── broker/
│   │   └── alpaca.py
│   ├── backtest/
│   │   └── engine.py
│   ├── evolution/
│   │   ├── dna.py
│   │   ├── optimizer.py
│   │   └── background_evolver.py
│   └── monitoring/
│       ├── circuit_breaker.py
│       └── ledger.py
│
├── bots/
│   ├── crypto/
│   │   ├── bot_crypto.py
│   │   └── Dockerfile
│   ├── stocks/
│   │   ├── bot_stocks.py
│   │   └── Dockerfile
│   ├── etf/
│   │   ├── bot_etf.py
│   │   └── Dockerfile
│   ├── politician/
│   │   ├── bot_politician.py
│   │   └── Dockerfile
│   └── funds/
│       ├── bot_funds.py
│       └── Dockerfile
│
├── evolver/
│   ├── evolver.py
│   └── Dockerfile
│
├── dashboard/
│   ├── dashboard.py
│   └── Dockerfile
│
├── data/                             # Named volume — historical OHLCV CSVs
├── champions/                        # Named volume — active champion DNA per asset class
├── challengers/                      # Named volume — latest GA challenger DNA
├── archive/                          # Named volume — demoted champion records
├── logs/                             # Named volume — evolution CSVs + ledger
│
├── docker-compose.yml
├── .env                              # API keys — never commit this
└── requirements.txt
```

---

## Dockerfile (shared base)

```dockerfile
# Dockerfile.base
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared/ is mounted as a volume at runtime — not baked in
# Update shared code without rebuilding images
```

```dockerfile
# bots/crypto/Dockerfile
FROM trading-bot-base
COPY bots/crypto/bot_crypto.py .
CMD ["python", "bot_crypto.py"]
```

```dockerfile
# evolver/Dockerfile — GPU-enabled
FROM pytorch/pytorch:2.2.0-cuda11.8-cudnn8-runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY evolver/evolver.py .
CMD ["python", "evolver.py"]
```

---

## docker-compose.yml

```yaml
version: "3.9"

volumes:
  shared_data:
  champions:
  challengers:
  archive:
  logs:

x-common-env: &common-env
  env_file: .env

x-bot-volumes: &bot-volumes
  - ./shared:/app/shared
  - champions:/app/champions
  - challengers:/app/challengers
  - logs:/app/logs
  - archive:/app/archive

services:

  bot_crypto:
    build: bots/crypto
    container_name: bot_crypto
    restart: unless-stopped
    <<: *common-env
    volumes: *bot-volumes
    deploy:
      resources:
        limits:
          cpus: "0.2"
          memory: 512M

  bot_stocks:
    build: bots/stocks
    container_name: bot_stocks
    restart: unless-stopped
    <<: *common-env
    volumes: *bot-volumes
    deploy:
      resources:
        limits:
          cpus: "0.2"
          memory: 512M

  bot_etf:
    build: bots/etf
    container_name: bot_etf
    restart: unless-stopped
    <<: *common-env
    volumes: *bot-volumes
    deploy:
      resources:
        limits:
          cpus: "0.2"
          memory: 256M

  bot_politician:
    build: bots/politician
    container_name: bot_politician
    restart: unless-stopped
    <<: *common-env
    volumes: *bot-volumes
    deploy:
      resources:
        limits:
          cpus: "0.2"
          memory: 256M

  bot_funds:
    build: bots/funds
    container_name: bot_funds
    restart: unless-stopped
    <<: *common-env
    volumes: *bot-volumes
    deploy:
      resources:
        limits:
          cpus: "0.1"
          memory: 128M

  evolver:
    build: evolver
    container_name: evolver
    restart: unless-stopped
    <<: *common-env
    volumes:
      - ./shared:/app/shared
      - shared_data:/app/data
      - champions:/app/champions
      - challengers:/app/challengers
      - logs:/app/logs
    deploy:
      resources:
        limits:
          cpus: "5"        # 5 of 6 cores — leave 1 for OS + Docker
          memory: 60G      # pre-load all historical data into RAM
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  dashboard:
    build: dashboard
    container_name: dashboard
    restart: unless-stopped
    <<: *common-env
    ports:
      - "8080:8080"
    volumes:
      - ./shared:/app/shared
      - champions:/app/champions
      - challengers:/app/challengers
      - logs:/app/logs
      - archive:/app/archive
    deploy:
      resources:
        limits:
          cpus: "0.1"
          memory: 256M

# Total allocated: ~6 CPUs, ~62GB RAM
# Leaves ~18GB headroom for OS + desktop
```

---

## .env
```env
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PAPER=true
QUIVER_API_KEY=your_quiver_key_here
```

---

## Common Docker Commands
```bash
# Build all images
docker compose build

# Start evolver first — let it run before bringing bots online
docker compose up -d evolver

# Watch evolver progress live
docker compose logs -f evolver

# Bring bots up once champion DNA exists
docker compose up -d bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard

# Restart one bot without touching others
docker compose restart bot_crypto

# Rebuild and restart after a code change
docker compose up -d --build bot_stocks

# Shell into a running container for debugging
docker compose exec bot_crypto bash

# Stop everything
docker compose down
```

---

## Deployment Sequence
```
1. docker compose up -d evolver
   → Runs GA for ~2.8 hours per asset class (10k agents, 500 gen, 5 cores)
   → Saves champion DNA to champions/ volume automatically

2. docker compose up -d bot_funds
   → Safest bot — start while evolver still runs other asset classes

3. docker compose up -d bot_etf bot_stocks
   → Once their champion DNA is ready

4. docker compose up -d bot_crypto bot_politician
   → Higher risk — start last after system is validated

5. docker compose up -d dashboard
   → Monitoring view at http://localhost:8080
```

---

## Capital Allocation
```python
POOL_CONFIG = {
    "funds"      : {"allocation": 0.40, "shorting": False, "isolated": True},
    "etf"        : {"allocation": 0.25, "shorting": True,  "max_short_exposure": 0.3},
    "stocks"     : {"allocation": 0.20, "shorting": True,  "max_short_exposure": 0.5},
    "crypto"     : {"allocation": 0.10, "shorting": True,  "max_short_exposure": 0.4},
    "politician" : {"allocation": 0.05, "shorting": True,  "max_short_exposure": 0.5},
}
```

---

## Key Rules (never violate)
- No lookahead bias — never use future data in training, backtesting, or evolution
- No shuffling — time-series data must always stay in chronological order
- Sharpe ratio is always the fitness metric — never raw profit
- Use `multiprocessing.Pool(5)` everywhere — never Pool(6), always leave 1 core free
- Pre-load all historical data into RAM at evolver startup — no disk I/O during GA
- Paper trade first — `ALPACA_PAPER=true` in .env before going live
- Funds bot has zero sell logic — it only buys and holds
- Checkpoint best DNA after every GA generation in case of crash
- Shorts always have a stop-loss order placed immediately after opening
- Fractional longs use `notional` dollar amounts — fractional shorts are NOT supported by Alpaca, always use whole share qty
- If a short position size rounds down to 0 whole shares, skip the trade silently and log it
- Never commit .env — API keys in environment variables only

---

## Build Priority Order
1.  `requirements.txt` + `Dockerfile.base`
2.  `shared/data/fetcher.py`
3.  `shared/data/preprocessor.py`
4.  `shared/data/politician_trades.py`
5.  `shared/backtest/engine.py`
6.  `shared/evolution/dna.py`
7.  `shared/evolution/optimizer.py`
8.  `evolver/evolver.py` + `evolver/Dockerfile`
9.  `shared/evolution/background_evolver.py`
10. `shared/monitoring/circuit_breaker.py`
11. `shared/monitoring/ledger.py`
12. `shared/strategy/ml_model.py`
13. `shared/risk/manager.py`
14. `shared/risk/position_tracker.py`
15. `shared/broker/alpaca.py`
16. `docker-compose.yml` + `.env`
17. `bots/crypto/` (bot + Dockerfile)
18. `bots/stocks/` (bot + Dockerfile)
19. `bots/etf/` (bot + Dockerfile)
20. `bots/politician/` (bot + Dockerfile)
21. `bots/funds/` (bot + Dockerfile)
22. `dashboard/` (bot + Dockerfile)
