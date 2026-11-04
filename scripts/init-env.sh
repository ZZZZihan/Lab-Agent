#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env && "${1:-}" != "--force" ]]; then
  echo ".env already exists"
  exit 0
fi
jwt="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
pg="$(python3 -c 'import secrets; print(secrets.token_hex(18))')"
cat > .env <<EOF
APP_ENV=dev
POSTGRES_PASSWORD=${pg}
DATABASE_URL=postgresql+asyncpg://labagent:${pg}@127.0.0.1:55432/labagent
REDIS_URL=redis://127.0.0.1:56379/0
JWT_SECRET=${jwt}
JWT_TTL_MINUTES=480
CORS_ORIGINS=http://127.0.0.1:43123,http://localhost:43123
LLM_PROVIDER=fake
LLM_MODEL=fake-grounded-v1
EMBEDDER=hashing
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_DIM=512
RERANKER=off
LOAD_TUNED_CONFIG=true
AUDIT_RETENTION_DAYS=180
UPLOAD_DIR=./data/uploads
OTEL_EXPORTER=none
RATE_LIMIT_ENABLED=true
COOKIE_SECURE=false
API_INTERNAL_URL=http://127.0.0.1:43124
NEXT_PUBLIC_DEMO_HINTS=true
EOF
echo "Wrote .env with a generated JWT secret and database password."
