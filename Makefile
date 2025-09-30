.PHONY: up down logs build

up:
	docker compose up -d --build
	docker compose ps

logs:
	docker compose logs -f --tail=200

down:
	docker compose down -v

build:
	docker compose build --no-cache
