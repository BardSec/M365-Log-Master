#!/bin/sh
set -e

if [ "${DEMO_MODE:-true}" = "true" ]; then
  echo "[entrypoint] Demo mode – skipping Alembic migrations."
else
  echo "[entrypoint] Running Alembic migrations..."
  alembic upgrade head
fi

echo "[entrypoint] Starting Gunicorn..."
exec gunicorn \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "app:create_app()"
