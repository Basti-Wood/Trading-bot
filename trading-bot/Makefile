.PHONY: start stop logs test
start:
	docker compose up -d --build
stop:
	docker compose stop
logs:
	docker compose logs -f
test:
	python -m unittest discover -s tests -v
