#!/usr/bin/env bash
set -euo pipefail

cmd="${1:-all}"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in current folder."
  exit 1
fi

help() {
  cat <<EOF
Trading Bot Commands
  ./start.sh build      - Build all images
  ./start.sh evolver    - Start evolver only
  ./start.sh bots       - Start all trading bots
  ./start.sh dashboard  - Start dashboard only
  ./start.sh all        - Start evolver + bots + dashboard
  ./start.sh up         - Alias for all
  ./start.sh logs       - Follow all logs
  ./start.sh ps         - Show running services
  ./start.sh restart    - Restart all services
  ./start.sh stop       - Stop containers
  ./start.sh down       - Stop and remove containers
  ./start.sh clean      - Down plus remove volumes
EOF
}

case "$cmd" in
  help)
    help
    ;;
  build)
    docker compose build
    ;;
  evolver)
    docker compose up -d evolver
    ;;
  bots)
    docker compose up -d bot_funds bot_etf bot_stocks bot_crypto bot_politician
    ;;
  dashboard)
    docker compose up -d dashboard
    ;;
  all|up)
    docker compose up -d evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
    ;;
  logs)
    docker compose logs -f
    ;;
  ps)
    docker compose ps
    ;;
  restart)
    docker compose restart evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
    ;;
  stop)
    docker compose stop
    ;;
  down)
    docker compose down
    ;;
  clean)
    docker compose down -v
    ;;
  *)
    echo "Unknown command: $cmd"
    help
    exit 1
    ;;
esac
