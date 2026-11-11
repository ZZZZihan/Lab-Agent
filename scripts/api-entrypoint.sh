#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data/uploads /app/data/calibrations /run/lab-agent-secrets
  chown -R 10001:10001 /app/data/uploads /app/data/calibrations /run/lab-agent-secrets
  chmod 0700 /run/lab-agent-secrets
  if [ -f /run/secrets/magpie_key ]; then
    cp /run/secrets/magpie_key /run/lab-agent-secrets/magpie_key
    chown 10001:10001 /run/lab-agent-secrets/magpie_key
    chmod 0400 /run/lab-agent-secrets/magpie_key
  fi
  if [ "${1:-}" = "--init-only" ]; then
    exit 0
  fi
  exec setpriv --reuid=10001 --regid=10001 --clear-groups "$0" "$@"
fi

if [ -n "${LLM_API_KEY_FILE:-}" ] && [ ! -r "${LLM_API_KEY_FILE}" ] && [ -r /run/lab-agent-secrets/magpie_key ]; then
  export LLM_API_KEY_FILE=/run/lab-agent-secrets/magpie_key
fi

cd /opt/lab-agent/backend
alembic upgrade head
if [ "${APP_ENV:-dev}" = "prod" ]; then
  echo "APP_ENV=prod: skipping demo seed"
  python -m app.reindex --init-empty
else
  python -m app.reindex --apply --if-needed
  python -m app.seed
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
