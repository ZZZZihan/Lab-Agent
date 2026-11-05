#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

OFFLINE=false
FORCE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      OFFLINE=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--offline] [--force]" >&2
      exit 1
      ;;
  esac
done

if [[ -f .env ]]; then
  if [[ "$FORCE" != "true" ]]; then
    echo ".env already exists"
    exit 0
  fi
  umask 077
  backup=".env.bak.$(date +%Y%m%d%H%M%S)"
  cp -p .env "$backup"
  echo "Backed up existing .env to ${backup}"
fi

if [[ "$OFFLINE" != "true" ]]; then
  if [[ -z "${LLM_MODEL:-}" ]]; then
    cat >&2 <<'EOF'
Error: LLM_MODEL must be set in real mode.
Choose an available chat model on your Magpie gateway, for example:
  export LLM_MODEL=claude-sonnet-5-5
  # Other examples: gemini-3.8-flash, gemini-4-argon, grok-4.7, gpt-oss-120b
Or run with --offline for zero-dependency offline mode:
  ./scripts/init-env.sh --offline
EOF
    exit 1
  fi

  key_file="${LAB_AGENT_MAGPIE_KEY_FILE:-$HOME/.config/lab-agent/magpie.key}"
  if [[ ! -f "$key_file" || ! -s "$key_file" ]]; then
    cat >&2 <<EOF
Error: Magpie gateway key file not found or empty: ${key_file}
Create a dedicated Lab Agent key file first (never put the key in .env):
  mkdir -p ~/.config/lab-agent && chmod 700 ~/.config/lab-agent
  pbpaste > ~/.config/lab-agent/magpie.key && chmod 600 ~/.config/lab-agent/magpie.key
EOF
    exit 1
  fi

  key_mode="$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$key_file")"
  if [[ "$key_mode" != "600" ]]; then
    echo "Warning: ${key_file} permissions are ${key_mode} (recommended: 600). Run: chmod 600 \"${key_file}\"" >&2
  fi
fi

jwt="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
pg="$(python3 -c 'import secrets; print(secrets.token_hex(18))')"
redis_pw="$(python3 -c 'import secrets; print(secrets.token_hex(18))')"
proxy_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
audit_hmac_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

umask 077

if [[ "$OFFLINE" == "true" ]]; then
  cat > .env <<EOF
APP_ENV=dev
POSTGRES_PASSWORD=${pg}
DATABASE_URL=postgresql+asyncpg://labagent:${pg}@127.0.0.1:55432/labagent
REDIS_PASSWORD=${redis_pw}
REDIS_URL=redis://:${redis_pw}@127.0.0.1:56379/0
JWT_SECRET=${jwt}
JWT_TTL_MINUTES=480
INTERNAL_PROXY_SECRET=${proxy_secret}
AUDIT_HMAC_KEY=${audit_hmac_key}
CORS_ORIGINS=http://127.0.0.1:43123,http://localhost:43123
LLM_PROVIDER=fake
LLM_MODEL=fake-grounded-v1
EMBEDDER=hashing
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_DIM=512
EMBEDDING_STORAGE=vector
EMBEDDING_INDEX=auto
RERANKER=off
LOAD_TUNED_CONFIG=true
CALIBRATION_DIR=./data/calibrations
AUDIT_RETENTION_DAYS=180
UPLOAD_DIR=./data/uploads
OTEL_EXPORTER=none
RATE_LIMIT_ENABLED=true
COOKIE_SECURE=false
PUBLIC_ORIGIN=http://127.0.0.1:43123
API_INTERNAL_URL=http://127.0.0.1:43124
NEXT_PUBLIC_DEMO_HINTS=true
EOF
  echo "Wrote offline .env with generated secrets."
else
  ollama_bin="$(command -v ollama || true)"
  if [[ -z "$ollama_bin" && -x /opt/homebrew/bin/ollama ]]; then
    ollama_bin="/opt/homebrew/bin/ollama"
  fi
  embed_version="${EMBEDDING_MODEL_VERSION:-}"
  if [[ -z "$embed_version" && -n "$ollama_bin" ]]; then
    embed_version="$("$ollama_bin" list 2>/dev/null | awk '$1 == "lab-agent-qwen3-embed" || $1 == "lab-agent-qwen3-embed:latest" || $1 == "qwen3-embedding:8b" {print $2; exit}' || true)"
  fi

  {
    cat <<EOF
APP_ENV=dev
POSTGRES_PASSWORD=${pg}
DATABASE_URL=postgresql+asyncpg://labagent:${pg}@127.0.0.1:55432/labagent
REDIS_PASSWORD=${redis_pw}
REDIS_URL=redis://:${redis_pw}@127.0.0.1:56379/0
JWT_SECRET=${jwt}
JWT_TTL_MINUTES=480
INTERNAL_PROXY_SECRET=${proxy_secret}
AUDIT_HMAC_KEY=${audit_hmac_key}
CORS_ORIGINS=http://127.0.0.1:43123,http://localhost:43123
LLM_PROVIDER=openai_compatible
LLM_MODEL=${LLM_MODEL}
EOF
    if [[ -n "${LLM_BASE_URL:-}" ]]; then
      printf 'LLM_BASE_URL=%s\n' "${LLM_BASE_URL}"
    fi
    cat <<EOF
LLM_REDACT=true
EMBEDDER=openai_compatible
EMBEDDING_MODEL=lab-agent-qwen3-embed
EMBEDDING_MODEL_VERSION=${embed_version}
EMBEDDING_DIM=4096
EMBEDDING_NATIVE_DIM=4096
EMBEDDING_TRUNCATE=false
EMBEDDING_STORAGE=halfvec
EMBEDDING_INDEX=auto
EMBEDDING_QUERY_TEMPLATE=Instruct: {instruction}\nQuery:{query}
EMBEDDING_QUERY_INSTRUCTION=Given a question from a research lab member, retrieve the lab document passage that answers it
EMBEDDING_REDACT=false
EMBEDDING_BATCH_SIZE=16
EMBEDDING_TIMEOUT_SECONDS=120
RERANKER=off
LOAD_TUNED_CONFIG=true
CALIBRATION_DIR=./data/calibrations
AUDIT_RETENTION_DAYS=180
UPLOAD_DIR=./data/uploads
OTEL_EXPORTER=none
RATE_LIMIT_ENABLED=true
COOKIE_SECURE=false
PUBLIC_ORIGIN=http://127.0.0.1:43123
API_INTERNAL_URL=http://127.0.0.1:43124
NEXT_PUBLIC_DEMO_HINTS=true
EOF
  } > .env
  echo "Wrote real-mode .env with generated secrets (Magpie key stays in file)."
fi
