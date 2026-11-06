#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

OFFLINE=false
LINUX_EMBED=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      OFFLINE=true
      shift
      ;;
    --linux-embed)
      LINUX_EMBED=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--offline] [--linux-embed]" >&2
      exit 1
      ;;
  esac
done

export LAB_AGENT_MAGPIE_KEY_FILE="${LAB_AGENT_MAGPIE_KEY_FILE:-$HOME/.config/lab-agent/magpie.key}"

COMPOSE_FILES=(-f docker-compose.yml)
if [[ "$OFFLINE" == "true" ]]; then
  COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.offline.yml)
elif [[ "$LINUX_EMBED" == "true" ]]; then
  COMPOSE_FILES=(--profile linux-embed -f docker-compose.yml)
fi

COMPOSE_STARTED=false
SMOKE_LOG=""

cleanup_on_exit() {
  local exit_code=$?
  if [[ -n "$SMOKE_LOG" && -f "$SMOKE_LOG" ]]; then
    rm -f "$SMOKE_LOG"
  fi
  if [[ $exit_code -ne 0 && "$COMPOSE_STARTED" == "true" ]]; then
    echo "--- Compose failed (exit ${exit_code}); tailing last 100 lines of api logs ---" >&2
    docker compose "${COMPOSE_FILES[@]}" logs --tail=100 api >&2 || true
  fi
  exit "$exit_code"
}
trap cleanup_on_exit EXIT

env_val() {
  local key="$1"
  if [[ ! -f .env ]]; then
    return 0
  fi
  python3 - "$key" <<'PY'
import sys
from pathlib import Path

target = sys.argv[1]
val = ""
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == target:
        val = v.strip().strip("'").strip('"')
print(val)
PY
}

set_env_val() {
  local key="$1"
  local value="$2"
  python3 - "$key" "$value" <<'PY'
import sys
from pathlib import Path

key, value = sys.argv[1], sys.argv[2]
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
updated = False
out: list[str] = []
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        k, _ = stripped.split("=", 1)
        if k.strip() == key:
            out.append(f"{key}={value}")
            updated = True
            continue
    out.append(line)
if not updated:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

# 1. Verify Docker Compose v2+
if ! docker compose version >/dev/null 2>&1; then
  echo "Error: 'docker compose' (Compose v2+) is required." >&2
  exit 1
fi
compose_ver="$(docker compose version --short 2>/dev/null || true)"
compose_major="${compose_ver#v}"
compose_major="${compose_major%%.*}"
if [[ -z "$compose_major" || ! "$compose_major" =~ ^[0-9]+$ || "$compose_major" -lt 2 ]]; then
  echo "Error: Docker Compose v2+ is required (found: ${compose_ver:-unknown})." >&2
  exit 1
fi

# 2. Initialize or validate .env
if [[ ! -f .env ]]; then
  if [[ "$OFFLINE" == "true" ]]; then
    ./scripts/init-env.sh --offline
  elif [[ "$LINUX_EMBED" == "true" ]]; then
    ./scripts/init-env.sh --linux-embed
  else
    ./scripts/init-env.sh
  fi
else
  if [[ "$LINUX_EMBED" == "true" ]]; then
    set_env_val EMBEDDING_BASE_URL "http://embedder:11434/v1"
  elif [[ "$OFFLINE" != "true" && "$(env_val EMBEDDING_BASE_URL)" == "http://embedder:11434/v1" ]]; then
    LINUX_EMBED=true
    COMPOSE_FILES=(--profile linux-embed -f docker-compose.yml)
  fi
  if [[ -n "$(env_val LLM_API_KEY)" ]]; then
    echo "Warning: LLM_API_KEY is set in .env; 建议改用密钥文件 (${LAB_AGENT_MAGPIE_KEY_FILE})." >&2
  fi
  if [[ -z "$(env_val REDIS_PASSWORD)" ]]; then
    new_redis_pw="$(python3 -c 'import secrets; print(secrets.token_hex(18))')"
    set_env_val REDIS_PASSWORD "$new_redis_pw"
    set_env_val REDIS_URL "redis://:${new_redis_pw}@127.0.0.1:56379/0"
  fi
  if [[ "$OFFLINE" != "true" ]]; then
    cfg_llm_provider="$(env_val LLM_PROVIDER)"
    cfg_embedder="$(env_val EMBEDDER)"
    if [[ -z "$cfg_llm_provider" || "$cfg_llm_provider" == "fake" || -z "$cfg_embedder" || "$cfg_embedder" == "hashing" || ! -s "$LAB_AGENT_MAGPIE_KEY_FILE" ]]; then
      cat >&2 <<EOF
Error: Real mode requires a non-fake LLM_PROVIDER, a non-hashing EMBEDDER in .env,
and a non-empty Magpie key file at ${LAB_AGENT_MAGPIE_KEY_FILE}.
Current .env has LLM_PROVIDER='${cfg_llm_provider:-}' EMBEDDER='${cfg_embedder:-}'.
Either:
  1. Run in offline mode: ./scripts/demo.sh --offline
  2. Or prepare Magpie key + LLM_MODEL and regenerate .env:
     export LLM_MODEL=claude-sonnet-5-5
     ./scripts/init-env.sh --force
EOF
      exit 1
    fi
  fi
fi

# 3. Pre-check Magpie and Ollama on host (real mode only)
if [[ "$OFFLINE" != "true" ]]; then
  magpie_probe_base="${LLM_BASE_URL_HOST:-http://127.0.0.1:3425/v1}"
  magpie_probe_url="${magpie_probe_base%/}/models"
  echo "==> Checking host Magpie gateway (${magpie_probe_url})..."
  if ! sed 's/^/Authorization: Bearer /' "$LAB_AGENT_MAGPIE_KEY_FILE" | curl -fsS -o /dev/null --max-time 10 -H @- "$magpie_probe_url"; then
    cat >&2 <<'EOF'
Error: Could not reach Magpie gateway /v1/models on the host.
Please verify:
  1. Magpie gateway is running on http://127.0.0.1:3425/v1 and your key file is valid.
  2. If container or host routing requires a LAN or Tailscale address (see D12 in
     docs/specs/mvp-and-roadmap.md), set LLM_BASE_URL (and optionally LLM_BASE_URL_HOST)
     in your local .env and retry.
EOF
    exit 1
  fi

  # 3a. Pre-check Ollama (containerized linux-embed or host)
  if [[ "$LINUX_EMBED" == "true" ]]; then
    echo "==> Starting containerized Ollama (profile: linux-embed) and ensuring lab-agent-qwen3-embed..."
    COMPOSE_STARTED=true
    docker compose "${COMPOSE_FILES[@]}" up -d --wait --wait-timeout 600 embedder
    docker compose "${COMPOSE_FILES[@]}" run --rm embedder-init
    model_id="$(docker compose "${COMPOSE_FILES[@]}" exec -T embedder ollama list | awk '$1 == "lab-agent-qwen3-embed" || $1 == "lab-agent-qwen3-embed:latest" {print $2; exit}')"
    if [[ -z "$model_id" ]]; then
      echo "Error: Failed to read digest for lab-agent-qwen3-embed from containerized 'ollama list'." >&2
      exit 1
    fi

    current_ver="$(env_val EMBEDDING_MODEL_VERSION)"
    current_model="$(env_val EMBEDDING_MODEL)"
    if [[ "$current_model" != "lab-agent-qwen3-embed" ]]; then
      set_env_val EMBEDDING_MODEL "lab-agent-qwen3-embed"
    fi
    if [[ "$current_ver" != "$model_id" ]]; then
      echo "Notice: Updating EMBEDDING_MODEL_VERSION in .env (${current_ver:-none} -> ${model_id}); index rebuild and recalibration will run if needed."
      set_env_val EMBEDDING_MODEL_VERSION "$model_id"
    fi

    echo "==> Warming up lab-agent-qwen3-embed on containerized Ollama..."
    ollama_port="${OLLAMA_CONTAINER_PORT:-$(env_val OLLAMA_CONTAINER_PORT)}"
    ollama_port="${ollama_port:-11435}"
    curl -fsS -o /dev/null --max-time 120 "http://127.0.0.1:${ollama_port}/v1/embeddings" \
      -H 'Content-Type: application/json' \
      -d '{"model":"lab-agent-qwen3-embed","input":["预热"]}'
  else
    echo "==> Checking host Ollama service (http://127.0.0.1:11434)..."
    if ! curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:11434/api/version; then
      echo "Error: Ollama is not reachable at http://127.0.0.1:11434. Start it first:" >&2
      echo "  brew services start ollama" >&2
      exit 1
    fi

    ollama_bin="$(command -v ollama || true)"
    if [[ -z "$ollama_bin" && -x /opt/homebrew/bin/ollama ]]; then
      ollama_bin="/opt/homebrew/bin/ollama"
    fi
    if [[ -z "$ollama_bin" ]]; then
      echo "Error: 'ollama' CLI not found in PATH or /opt/homebrew/bin/ollama." >&2
      exit 1
    fi

    if ! "$ollama_bin" list | awk 'NR>1 {print $1}' | grep -Eq '^qwen3-embedding:8b$'; then
      echo "Error: qwen3-embedding:8b is not pulled in Ollama. Pull it first:" >&2
      echo "  ollama pull qwen3-embedding:8b" >&2
      exit 1
    fi

    echo "==> Ensuring derived Ollama model lab-agent-qwen3-embed (num_ctx=8192)..."
    "$ollama_bin" create lab-agent-qwen3-embed -f docker/ollama/Modelfile.qwen3-embedding >/dev/null
    model_id="$("$ollama_bin" list | awk '$1 == "lab-agent-qwen3-embed" || $1 == "lab-agent-qwen3-embed:latest" {print $2; exit}')"
    if [[ -z "$model_id" ]]; then
      echo "Error: Failed to read digest for lab-agent-qwen3-embed from 'ollama list'." >&2
      exit 1
    fi

    current_ver="$(env_val EMBEDDING_MODEL_VERSION)"
    current_model="$(env_val EMBEDDING_MODEL)"
    if [[ "$current_model" != "lab-agent-qwen3-embed" ]]; then
      set_env_val EMBEDDING_MODEL "lab-agent-qwen3-embed"
    fi
    if [[ "$current_ver" != "$model_id" ]]; then
      echo "Notice: Updating EMBEDDING_MODEL_VERSION in .env (${current_ver:-none} -> ${model_id}); index rebuild and recalibration will run if needed."
      set_env_val EMBEDDING_MODEL_VERSION "$model_id"
    fi

    echo "==> Warming up lab-agent-qwen3-embed on host Ollama..."
    curl -fsS -o /dev/null --max-time 120 http://127.0.0.1:11434/v1/embeddings \
      -H 'Content-Type: application/json' \
      -d '{"model":"lab-agent-qwen3-embed","input":["预热"]}'
  fi
fi

# 4. Build and start containers
echo "==> Starting Docker Compose stack..."
COMPOSE_STARTED=true
docker compose "${COMPOSE_FILES[@]}" up -d --build --wait --wait-timeout 600

# 5. Container-side LLM and embedding self-checks (real mode only)
if [[ "$OFFLINE" != "true" ]]; then
  echo "==> Running container LLM connectivity check (app.llm_check)..."
  if ! docker compose "${COMPOSE_FILES[@]}" exec -T api python -m app.llm_check; then
    echo "Error: Container LLM check failed. Check LLM_BASE_URL (default http://host.docker.internal:3425/v1), LLM_MODEL, and ${LAB_AGENT_MAGPIE_KEY_FILE}." >&2
    exit 1
  fi

  echo "==> Running container embedding check (app.embed_check)..."
  if ! docker compose "${COMPOSE_FILES[@]}" exec -T api python -m app.embed_check; then
    echo "Error: Container embedding check failed. Check EMBEDDING_BASE_URL (default http://host.docker.internal:11434/v1) and host Ollama." >&2
    if [[ "$(uname -s)" == "Linux" && "$LINUX_EMBED" != "true" ]]; then
      cat >&2 <<'EOF'
Hint (Linux Docker bridge): Host Ollama defaults to binding 127.0.0.1:11434, which is not
reachable from inside the 'api' container via host.docker.internal:11434.
To fix this on Linux, either:
  1. Use the containerized Ollama profile: ./scripts/demo.sh --linux-embed
  2. Or bind host Ollama to 0.0.0.0 (set OLLAMA_HOST=0.0.0.0:11434 in systemd) and restart ollama.
EOF
    fi
    exit 1
  fi
fi

# 6. Check calibration status and tune on dev split if needed
echo "==> Checking calibration status via /api/v1/health..."
health_json="$(curl -fsS http://127.0.0.1:43124/api/v1/health)"
is_calibrated="$(python3 -c 'import json,sys; print(str(bool(json.loads(sys.argv[1]).get("calibrated"))).lower())' "$health_json")"
if [[ "$is_calibrated" != "true" ]]; then
  echo "==> Current embedder fingerprint is not calibrated yet; tuning on dev split..."
  docker compose "${COMPOSE_FILES[@]}" exec -T api python -m evals.run_eval --tune
  echo "==> Restarting api to load new calibration..."
  docker compose "${COMPOSE_FILES[@]}" restart api
  docker compose "${COMPOSE_FILES[@]}" up -d --wait --wait-timeout 600
fi

# 7. Run end-to-end smoke test
echo "==> Running end-to-end smoke suite (app.smoke)..."
SMOKE_LOG="$(mktemp)"
smoke_args=(--web http://web:3000 --api http://127.0.0.1:8000)
if [[ "$OFFLINE" == "true" ]]; then
  smoke_args+=(--offline)
fi
docker compose "${COMPOSE_FILES[@]}" exec -T api python -m app.smoke "${smoke_args[@]}" | tee "$SMOKE_LOG"

# Verify Magpie secret never appears in smoke output or api container logs
if [[ "$OFFLINE" != "true" && -s "$LAB_AGENT_MAGPIE_KEY_FILE" ]]; then
  if { cat "$SMOKE_LOG"; docker compose "${COMPOSE_FILES[@]}" logs --no-color api 2>&1; } | grep -F -q -f "$LAB_AGENT_MAGPIE_KEY_FILE"; then
    echo "Error: Secret value from key file was detected in smoke output or api logs!" >&2
    exit 1
  fi
fi

# 8. Print summary (never print secrets)
final_health="$(curl -fsS http://127.0.0.1:43124/api/v1/health)"
llm_model_out="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("llm_model",""))' "$final_health")"
fp_out="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("embedding_fingerprint",""))' "$final_health")"

cat <<EOF

============================================================
Lab Agent is ready!
  Web UI:                http://127.0.0.1:43123
  API:                   http://127.0.0.1:43124
  LLM Model:             ${llm_model_out}
  Embedding Fingerprint: ${fp_out}

Demo Accounts:
  - 沈昭 (admin):  admin@lab.local
  - 林可 (member): member@lab.local
  - 周宁 (member): zhou@lab.local
  - 王敏 (viewer): viewer@lab.local
============================================================
EOF
