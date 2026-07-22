.PHONY: help check-env build evolver bots dashboard all up stop down logs ps restart clean

SERVICES_BOTS=bot_funds bot_etf bot_stocks bot_crypto bot_politician
SERVICES_ALL=evolver $(SERVICES_BOTS) dashboard

help:
	@echo "Trading Bot Commands"
	@echo "  make build      - Build all images"
	@echo "  make evolver    - Start evolver only"
	@echo "  make bots       - Start all trading bots (no dashboard)"
	@echo "  make dashboard  - Start dashboard only"
	@echo "  make all        - Start evolver + bots + dashboard"
	@echo "  make up         - Alias for 'make all'"
	@echo "  make logs       - Follow all logs"
	@echo "  make ps         - Show running services"
	@echo "  make restart    - Restart all services"
	@echo "  make stop       - Stop containers"
	@echo "  make down       - Stop and remove containers"
	@echo "  make clean      - Down + remove volumes"

check-env:
	@if [ ! -f .env ]; then \
		echo "ERROR: .env not found"; \
		exit 1; \
	fi

build: check-env
	docker compose build

evolver: check-env
	docker compose up -d evolver

bots: check-env
	docker compose up -d $(SERVICES_BOTS)

dashboard: check-env
	docker compose up -d dashboard

all: check-env
	docker compose up -d $(SERVICES_ALL)

up: all

logs:
	docker compose logs -f

ps:
	docker compose ps

restart:
	docker compose restart $(SERVICES_ALL)

stop:
	docker compose stop

down:
	docker compose down

clean:
	docker compose down -v
