#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Set Alpaca/FreeLLM keys and DASHBOARD_PASSWORD, then run again."
  exit 1
fi
case "${1:-start}" in
  start) docker compose up -d --build ;;
  stop) docker compose stop ;;
  logs) docker compose logs -f ;;
  ps) docker compose ps ;;
  *) echo "Usage: ./start.sh [start|stop|logs|ps]"; exit 1 ;;
esac
