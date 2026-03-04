.PHONY: up down build logs shell test migrate sync-now psql demo demo-down demo-logs

up:
	docker compose up --build -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f web

shell:
	docker compose exec web bash

test:
	docker compose exec web pytest tests/ -v

migrate:
	docker compose exec web alembic upgrade head

sync-now:
	curl -s -X POST http://localhost:$${PORT:-8080}/api/sync-now | python3 -m json.tool

psql:
	docker compose exec db psql -U m365user -d m365logs

dev:
	FLASK_ENV=development flask --app "app:create_app()" run --host 0.0.0.0 --port $${PORT:-8080} --debug

demo:
	docker compose -f docker-compose.demo.yml up --build -d

demo-down:
	docker compose -f docker-compose.demo.yml down

demo-logs:
	docker compose -f docker-compose.demo.yml logs -f web
