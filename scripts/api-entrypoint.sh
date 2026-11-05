#!/bin/sh
set -eu
cd /opt/lab-agent/backend
alembic upgrade head
if [ "${APP_ENV:-dev}" = "prod" ]; then
  echo "APP_ENV=prod: skipping demo seed"
  python -m app.reindex --check
else
  python -m app.seed
  python -m app.reindex --apply --if-needed
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
