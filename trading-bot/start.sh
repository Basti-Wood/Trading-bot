#!/usr/bin/env bash
set -euo pipefail

cmd="${1:-}"

# ============================================================================
# Default (no args): build everything and launch detached
# ============================================================================
if [[ -z "$cmd" ]]; then
  if [[ ! -f .env ]]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill in your keys."
    exit 1
  fi

  echo ""
  echo "[1/3] Building base image (trading-bot-base)..."
  docker build -f Dockerfile.base -t trading-bot-base .

  echo ""
  echo "[2/3] Building all services..."
  docker compose build

  echo ""
  echo "[3/3] Starting all services detached..."
  docker compose up -d evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard

  echo ""
  echo "All services running. Dashboard at http://localhost:1337"
  echo "Use './start.sh ps' to see status, './start.sh logs' to follow logs."
  exit 0
fi

# ============================================================================
# Commands
# ============================================================================

help() {
  echo ""
  echo "Trading Bot — start.sh"
  echo ""
  echo "  ./start.sh              Build everything & start detached (default)"
  echo "  ./start.sh ps           Show running services"
  echo "  ./start.sh logs         Follow all logs"
  echo "  ./start.sh restart      Restart all services"
  echo "  ./start.sh stop         Stop containers"
  echo "  ./start.sh down         Stop & remove containers"
  echo "  ./start.sh clean        Down + remove volumes"
  echo "  ./start.sh fclean       Full clean: down, remove volumes & base image"
  echo ""
}

case "$cmd" in
  help)
    help
    ;;
  ps)
    docker compose ps
    ;;
  logs)
    docker compose logs -f
    ;;
  stop)
    docker compose stop
    ;;
  down)
    docker compose down
    ;;
  restart)
    docker compose restart evolver bot_funds bot_etf bot_stocks bot_crypto bot_politician dashboard
    ;;
  clean)
    docker compose down -v
    ;;
  fclean)
    echo "Removing containers, volumes, AND base image..."
    docker compose down -v
    docker rmi trading-bot-base 2>/dev/null || true
    echo "Done."
    ;;
  *)
    echo "Unknown command: $cmd"
    help
    exit 1
    ;;
esac
