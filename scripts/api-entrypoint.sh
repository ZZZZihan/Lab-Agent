#!/bin/sh
set -eu
cd /opt/yanzheng/backend
alembic upgrade head
if [ "${APP_ENV:-dev}" = "prod" ]; then
  echo "APP_ENV=prod: skipping demo seed"
else
  python -m app.seed
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
