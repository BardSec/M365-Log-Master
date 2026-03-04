#!/bin/sh
set -e

echo "[entrypoint] Running Alembic migrations..."
alembic upgrade head

echo "[entrypoint] Starting Gunicorn..."
exec gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "app:create_app()"
