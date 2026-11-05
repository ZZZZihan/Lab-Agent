#!/usr/bin/env bash
# Automate Qwen3-Embedding-8B v1 ablations (Spec T3.2 / PR-22: E1..E7).
# Contains no secrets or credentials; calibration and retrieval eval do not call the LLM.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

ALL_GROUPS=(E1 E2 E3 E4 E5 E6 E7)
LAB_INSTRUCTION="Given a question from a research lab member, retrieve the lab document passage that answers it"
WEB_INSTRUCTION="Given a web search query, retrieve relevant passages that answer the query"
INSTRUCT_TEMPLATE='Instruct: {instruction}\nQuery:{query}'
PLAIN_TEMPLATE='{query}'

BASE_MODEL="${ABLAT_BASE_MODEL:-qwen3-embedding:8b}"
Q8_MODEL="${ABLAT_Q8_MODEL:-qwen3-embedding:8b-q8_0}"

usage() {
  cat <<'EOF'
Usage: ./scripts/run_embedding_ablation.sh [--dry-run] [--all] [E1 E2 E3 E4 E5 E6 E7 ...]

Options:
  --all       Run all ablation groups E1..E7 (default when no group arguments are given)
  --dry-run   Check environment (Ollama, DB, models) and print resolved env vars and commands without executing
  -h, --help  Show this help message and exit

Groups:
  E1  MVP default: Qwen3-8B, 4096-dim, halfvec, exact search (auto -> none), D13 lab instruction
  E2  Same as E1, generic web search query instruction
  E3  Same as E1, no instruction ({query})
  E4  MRL 2048-dim, halfvec, HNSW, lab instruction
  E5  MRL 1024-dim, vector, HNSW, lab instruction
  E6  4096-dim, vector (fp32), exact search (auto -> none), lab instruction
  E7  Optional q8_0 tag (qwen3-embedding:8b-q8_0), 4096-dim, halfvec, exact search, lab instruction
EOF
}

DRY_RUN=false
SELECTED_GROUPS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --all)
      SELECTED_GROUPS=("${ALL_GROUPS[@]}")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    [Ee][1-7])
      group_upper="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
      already=false
      for existing in "${SELECTED_GROUPS[@]:-}"; do
        if [[ "$existing" == "$group_upper" ]]; then
          already=true
          break
        fi
      done
      if [[ "$already" == "false" ]]; then
        SELECTED_GROUPS+=("$group_upper")
      fi
      shift
      ;;
    *)
      echo "Error: Unknown argument or ablation group: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ${#SELECTED_GROUPS[@]} -eq 0 ]]; then
  SELECTED_GROUPS=("${ALL_GROUPS[@]}")
fi

env_file_val() {
  local key="$1"
  if [[ ! -f "$REPO_ROOT/.env" ]]; then
    return 0
  fi
  python3 - "$REPO_ROOT/.env" "$key" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
target = sys.argv[2]
val = ""
for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == target:
        val = v.strip().strip("'").strip('"')
print(val)
PY
}

RESOLVED_BASE_URL="${EMBEDDING_BASE_URL:-}"
if [[ -z "$RESOLVED_BASE_URL" ]]; then
  RESOLVED_BASE_URL="$(env_file_val EMBEDDING_BASE_URL)"
fi
if [[ -z "$RESOLVED_BASE_URL" ]]; then
  RESOLVED_BASE_URL="http://127.0.0.1:11434/v1"
fi

# 1. Check Ollama reachability at EMBEDDING_BASE_URL
models_url="${RESOLVED_BASE_URL%/}/models"
echo "==> Checking Ollama embedding endpoint (${models_url})..."
if ! curl -fsS -o /dev/null --max-time 5 "$models_url"; then
  echo "Error: Ollama is not reachable at ${RESOLVED_BASE_URL} (checked ${models_url})." >&2
  exit 1
fi

# 2. Check ollama CLI and inspect available models
OLLAMA_BIN="$(command -v ollama || true)"
if [[ -z "$OLLAMA_BIN" && -x /opt/homebrew/bin/ollama ]]; then
  OLLAMA_BIN="/opt/homebrew/bin/ollama"
fi
if [[ -z "$OLLAMA_BIN" ]]; then
  echo "Error: 'ollama' CLI not found in PATH or /opt/homebrew/bin/ollama." >&2
  exit 1
fi

OLLAMA_LIST_OUT="$("$OLLAMA_BIN" list 2>/dev/null || true)"
if [[ -z "$OLLAMA_LIST_OUT" ]]; then
  echo "Error: Failed to retrieve model list from 'ollama list'." >&2
  exit 1
fi

model_digest() {
  local target="$1"
  printf '%s\n' "$OLLAMA_LIST_OUT" | awk -v m="$target" 'NR>1 && ($1 == m || $1 == m ":latest") {print $2; exit}'
}

BASE_DIGEST="$(model_digest "$BASE_MODEL")"
Q8_DIGEST="$(model_digest "$Q8_MODEL")"

needs_base=false
for g in "${SELECTED_GROUPS[@]}"; do
  if [[ "$g" != "E7" ]]; then
    needs_base=true
    break
  fi
done

if [[ "$needs_base" == "true" && -z "$BASE_DIGEST" ]]; then
  echo "Error: Base embedding model '${BASE_MODEL}' not found in 'ollama list'. Pull it first:" >&2
  echo "  ollama pull ${BASE_MODEL}" >&2
  exit 1
fi

# 3. Check PostgreSQL database reachability
echo "==> Checking database connectivity..."
if ! (
  cd "$REPO_ROOT/backend"
  unset LLM_CAPTURE EVIDENCE_SCORE_MIN RARE_COVERAGE_MIN CITE_MARGIN CITE_K RRF_K RETRIEVAL_MODE
  export LLM_PROVIDER=fake
  export LLM_MODEL=fake-grounded-v1
  export EMBEDDER=hashing
  export EMBEDDING_DIM=512
  export EMBEDDING_STORAGE=vector
  uv run --frozen python -W ignore::UserWarning - <<'PY'
import asyncio
from sqlalchemy import text
from app.db import dispose_engine, get_sessionmaker

async def _check_db() -> None:
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            await session.execute(text("SELECT 1"))
    finally:
        await dispose_engine()

asyncio.run(_check_db())
PY
); then
  echo "Error: Database is not reachable. Check DATABASE_URL / PostgreSQL service." >&2
  exit 1
fi

# Resolve group configuration into CFG_* variables
resolve_group() {
  local group="$1"
  CFG_GROUP="$group"
  CFG_EMBEDDER="openai_compatible"
  CFG_BASE_URL="$RESOLVED_BASE_URL"
  CFG_NATIVE_DIM="4096"
  CFG_DOC_PREFIX=""
  CFG_REDACT="false"

  case "$group" in
    E1)
      CFG_DESC="MVP default (Qwen3-8B, 4096-dim, halfvec, exact search, D13 lab instruction)"
      CFG_MODEL="$BASE_MODEL"
      CFG_VERSION="$BASE_DIGEST"
      CFG_DIM="4096"
      CFG_TRUNCATE="false"
      CFG_STORAGE="halfvec"
      CFG_INDEX="auto"
      CFG_QUERY_TEMPLATE="$INSTRUCT_TEMPLATE"
      CFG_QUERY_INSTRUCTION="$LAB_INSTRUCTION"
      ;;
    E2)
      CFG_DESC="Generic web search instruction (4096-dim, halfvec, exact search)"
      CFG_MODEL="$BASE_MODEL"
      CFG_VERSION="$BASE_DIGEST"
      CFG_DIM="4096"
      CFG_TRUNCATE="false"
      CFG_STORAGE="halfvec"
      CFG_INDEX="auto"
      CFG_QUERY_TEMPLATE="$INSTRUCT_TEMPLATE"
      CFG_QUERY_INSTRUCTION="$WEB_INSTRUCTION"
      ;;
    E3)
      CFG_DESC="No instruction (4096-dim, halfvec, exact search)"
      CFG_MODEL="$BASE_MODEL"
      CFG_VERSION="$BASE_DIGEST"
      CFG_DIM="4096"
      CFG_TRUNCATE="false"
      CFG_STORAGE="halfvec"
      CFG_INDEX="auto"
      CFG_QUERY_TEMPLATE="$PLAIN_TEMPLATE"
      CFG_QUERY_INSTRUCTION=""
      ;;
    E4)
      CFG_DESC="MRL 2048-dim, halfvec, HNSW, lab instruction"
      CFG_MODEL="$BASE_MODEL"
      CFG_VERSION="$BASE_DIGEST"
      CFG_DIM="2048"
      CFG_TRUNCATE="true"
      CFG_STORAGE="halfvec"
      CFG_INDEX="hnsw"
      CFG_QUERY_TEMPLATE="$INSTRUCT_TEMPLATE"
      CFG_QUERY_INSTRUCTION="$LAB_INSTRUCTION"
      ;;
    E5)
      CFG_DESC="MRL 1024-dim, vector, HNSW, lab instruction"
      CFG_MODEL="$BASE_MODEL"
      CFG_VERSION="$BASE_DIGEST"
      CFG_DIM="1024"
      CFG_TRUNCATE="true"
      CFG_STORAGE="vector"
      CFG_INDEX="hnsw"
      CFG_QUERY_TEMPLATE="$INSTRUCT_TEMPLATE"
      CFG_QUERY_INSTRUCTION="$LAB_INSTRUCTION"
      ;;
    E6)
      CFG_DESC="4096-dim, vector fp32, exact search, lab instruction"
      CFG_MODEL="$BASE_MODEL"
      CFG_VERSION="$BASE_DIGEST"
      CFG_DIM="4096"
      CFG_TRUNCATE="false"
      CFG_STORAGE="vector"
      CFG_INDEX="auto"
      CFG_QUERY_TEMPLATE="$INSTRUCT_TEMPLATE"
      CFG_QUERY_INSTRUCTION="$LAB_INSTRUCTION"
      ;;
    E7)
      CFG_DESC="Optional q8_0 tag (${Q8_MODEL}, 4096-dim, halfvec, exact search, lab instruction)"
      CFG_MODEL="$Q8_MODEL"
      CFG_VERSION="$Q8_DIGEST"
      CFG_DIM="4096"
      CFG_TRUNCATE="false"
      CFG_STORAGE="halfvec"
      CFG_INDEX="auto"
      CFG_QUERY_TEMPLATE="$INSTRUCT_TEMPLATE"
      CFG_QUERY_INSTRUCTION="$LAB_INSTRUCTION"
      ;;
    *)
      echo "Error: Unknown group $group" >&2
      exit 1
      ;;
  esac

  # Compute fingerprint, slug, and resolved_embedding_index via app.config.Settings
  local meta_out
  meta_out="$(
    cd "$REPO_ROOT/backend"
    unset LLM_CAPTURE EVIDENCE_SCORE_MIN RARE_COVERAGE_MIN CITE_MARGIN CITE_K RRF_K RETRIEVAL_MODE
    export LLM_PROVIDER=fake
    export LLM_MODEL=fake-grounded-v1
    export EMBEDDER="$CFG_EMBEDDER"
    export EMBEDDING_BASE_URL="$CFG_BASE_URL"
    export EMBEDDING_MODEL="$CFG_MODEL"
    export EMBEDDING_MODEL_VERSION="$CFG_VERSION"
    export EMBEDDING_DIM="$CFG_DIM"
    export EMBEDDING_NATIVE_DIM="$CFG_NATIVE_DIM"
    export EMBEDDING_TRUNCATE="$CFG_TRUNCATE"
    export EMBEDDING_STORAGE="$CFG_STORAGE"
    export EMBEDDING_INDEX="$CFG_INDEX"
    export EMBEDDING_QUERY_TEMPLATE="$CFG_QUERY_TEMPLATE"
    export EMBEDDING_QUERY_INSTRUCTION="$CFG_QUERY_INSTRUCTION"
    export EMBEDDING_DOCUMENT_PREFIX="$CFG_DOC_PREFIX"
    export EMBEDDING_REDACT="$CFG_REDACT"
    uv run --frozen python -W ignore::UserWarning - <<'PY'
from app.config import get_settings, reset_settings_cache
reset_settings_cache()
s = get_settings()
print(s.embedding_fingerprint)
print(s.embedding_slug)
print(s.resolved_embedding_index)
PY
  )"
  CFG_FINGERPRINT="$(printf '%s\n' "$meta_out" | sed -n '1p')"
  CFG_SLUG="$(printf '%s\n' "$meta_out" | sed -n '2p')"
  CFG_RESOLVED_INDEX="$(printf '%s\n' "$meta_out" | sed -n '3p')"
}

print_group_config() {
  cat <<EOF
============================================================
Group ${CFG_GROUP}: ${CFG_DESC}
------------------------------------------------------------
EMBEDDER=${CFG_EMBEDDER}
EMBEDDING_BASE_URL=${CFG_BASE_URL}
EMBEDDING_MODEL=${CFG_MODEL}
EMBEDDING_MODEL_VERSION=${CFG_VERSION}
EMBEDDING_DIM=${CFG_DIM}
EMBEDDING_NATIVE_DIM=${CFG_NATIVE_DIM}
EMBEDDING_TRUNCATE=${CFG_TRUNCATE}
EMBEDDING_STORAGE=${CFG_STORAGE}
EMBEDDING_INDEX=${CFG_INDEX}
RESOLVED_EMBEDDING_INDEX=${CFG_RESOLVED_INDEX}
EMBEDDING_QUERY_TEMPLATE=${CFG_QUERY_TEMPLATE}
EMBEDDING_QUERY_INSTRUCTION=${CFG_QUERY_INSTRUCTION}
EMBEDDING_DOCUMENT_PREFIX=${CFG_DOC_PREFIX}
EMBEDDING_REDACT=${CFG_REDACT}
EMBEDDING_FINGERPRINT=${CFG_FINGERPRINT}
EMBEDDING_SLUG=${CFG_SLUG}
Commands:
  python -m app.reindex --apply
  python -m evals.run_eval --tune --commit
EOF
}

EXECUTED_SLUGS=()
EXECUTED_GROUPS=()
CORPUS_PREPOPULATED="false"

for group in "${SELECTED_GROUPS[@]}"; do
  if [[ "$group" == "E7" && -z "$Q8_DIGEST" ]]; then
    echo "==> [E7] Skipping optional group E7: model '${Q8_MODEL}' not found in 'ollama list'."
    continue
  fi

  resolve_group "$group"
  print_group_config

  if [[ "$DRY_RUN" == "true" ]]; then
    continue
  fi

  if [[ "$CORPUS_PREPOPULATED" == "false" ]]; then
    echo "==> Pre-populating evaluation corpus..."
    (
      cd "$REPO_ROOT/backend"
      unset LLM_CAPTURE EVIDENCE_SCORE_MIN RARE_COVERAGE_MIN CITE_MARGIN CITE_K RRF_K RETRIEVAL_MODE
      export LLM_PROVIDER=fake
      export LLM_MODEL=fake-grounded-v1
      export EMBEDDER="$CFG_EMBEDDER"
      export EMBEDDING_BASE_URL="$CFG_BASE_URL"
      export EMBEDDING_MODEL="$CFG_MODEL"
      export EMBEDDING_MODEL_VERSION="$CFG_VERSION"
      export EMBEDDING_DIM="$CFG_DIM"
      export EMBEDDING_NATIVE_DIM="$CFG_NATIVE_DIM"
      export EMBEDDING_TRUNCATE="$CFG_TRUNCATE"
      export EMBEDDING_STORAGE="$CFG_STORAGE"
      export EMBEDDING_INDEX="$CFG_INDEX"
      export EMBEDDING_QUERY_TEMPLATE="$CFG_QUERY_TEMPLATE"
      export EMBEDDING_QUERY_INSTRUCTION="$CFG_QUERY_INSTRUCTION"
      export EMBEDDING_DOCUMENT_PREFIX="$CFG_DOC_PREFIX"
      export EMBEDDING_REDACT="$CFG_REDACT"
      uv run --frozen python -W ignore::UserWarning - <<'PY'
import asyncio
from app.config import get_settings, reset_settings_cache
from app.db import dispose_engine
from evals.run_eval import _prepare

async def _prepopulate() -> None:
    reset_settings_cache()
    try:
        await _prepare(get_settings())
    finally:
        await dispose_engine()

asyncio.run(_prepopulate())
PY
    )
    CORPUS_PREPOPULATED="true"
  fi

  echo "==> [${CFG_GROUP}] Running reindex (python -m app.reindex --apply)..."
  reindex_start="$(python3 -c 'import time; print(f"{time.perf_counter():.6f}")')"
  (
    cd "$REPO_ROOT/backend"
    unset LLM_CAPTURE EVIDENCE_SCORE_MIN RARE_COVERAGE_MIN CITE_MARGIN CITE_K RRF_K RETRIEVAL_MODE
    export LLM_PROVIDER=fake
    export LLM_MODEL=fake-grounded-v1
    export EMBEDDER="$CFG_EMBEDDER"
    export EMBEDDING_BASE_URL="$CFG_BASE_URL"
    export EMBEDDING_MODEL="$CFG_MODEL"
    export EMBEDDING_MODEL_VERSION="$CFG_VERSION"
    export EMBEDDING_DIM="$CFG_DIM"
    export EMBEDDING_NATIVE_DIM="$CFG_NATIVE_DIM"
    export EMBEDDING_TRUNCATE="$CFG_TRUNCATE"
    export EMBEDDING_STORAGE="$CFG_STORAGE"
    export EMBEDDING_INDEX="$CFG_INDEX"
    export EMBEDDING_QUERY_TEMPLATE="$CFG_QUERY_TEMPLATE"
    export EMBEDDING_QUERY_INSTRUCTION="$CFG_QUERY_INSTRUCTION"
    export EMBEDDING_DOCUMENT_PREFIX="$CFG_DOC_PREFIX"
    export EMBEDDING_REDACT="$CFG_REDACT"
    uv run --frozen python -m app.reindex --apply
  )
  reindex_end="$(python3 -c 'import time; print(f"{time.perf_counter():.6f}")')"
  reindex_duration_s="$(python3 -c "print(f'{float(${reindex_end}) - float(${reindex_start}):.4f}')")"

  echo "==> [${CFG_GROUP}] Running evaluation (python -m evals.run_eval --tune --commit)..."
  (
    cd "$REPO_ROOT/backend"
    unset LLM_CAPTURE EVIDENCE_SCORE_MIN RARE_COVERAGE_MIN CITE_MARGIN CITE_K RRF_K RETRIEVAL_MODE
    export LLM_PROVIDER=fake
    export LLM_MODEL=fake-grounded-v1
    export EMBEDDER="$CFG_EMBEDDER"
    export EMBEDDING_BASE_URL="$CFG_BASE_URL"
    export EMBEDDING_MODEL="$CFG_MODEL"
    export EMBEDDING_MODEL_VERSION="$CFG_VERSION"
    export EMBEDDING_DIM="$CFG_DIM"
    export EMBEDDING_NATIVE_DIM="$CFG_NATIVE_DIM"
    export EMBEDDING_TRUNCATE="$CFG_TRUNCATE"
    export EMBEDDING_STORAGE="$CFG_STORAGE"
    export EMBEDDING_INDEX="$CFG_INDEX"
    export EMBEDDING_QUERY_TEMPLATE="$CFG_QUERY_TEMPLATE"
    export EMBEDDING_QUERY_INSTRUCTION="$CFG_QUERY_INSTRUCTION"
    export EMBEDDING_DOCUMENT_PREFIX="$CFG_DOC_PREFIX"
    export EMBEDDING_REDACT="$CFG_REDACT"
    export EVAL_REINDEX_DURATION_S="$reindex_duration_s"
    uv run --frozen python -m evals.run_eval --tune --commit
  )

  result_file="$REPO_ROOT/evals/results/v1/${CFG_SLUG}.json"
  python3 - "$CFG_GROUP" "$reindex_duration_s" "$result_file" <<'PY'
import json
import sys
from pathlib import Path

group = sys.argv[1]
reindex_s = float(sys.argv[2])
res_path = Path(sys.argv[3])
doc = json.loads(res_path.read_text(encoding="utf-8"))

selected = doc["selected_mode"]
dev_m = doc["modes"][selected]["dev"]
test_m = doc["modes"][selected]["test"]
q_lat = doc.get("query_embedding_latency_ms") or {}
s_lat = doc.get("search_latency_ms") or {}
table_bytes = int(doc.get("chunks_table_bytes") or 0)

def val(metric: object) -> float:
    if isinstance(metric, dict):
        return float(metric.get("value", 0.0))
    return float(metric or 0.0)

print(f"--- [{group}] Summary ({doc['embedder_fingerprint']}) ---")
print(f"  Reindex duration:        {reindex_s:.2f} s")
print(
    f"  Query embed latency ms:  mean={q_lat.get('mean', 0.0):.2f}  "
    f"p50={q_lat.get('p50', 0.0):.2f}  p95={q_lat.get('p95', 0.0):.2f}"
)
print(
    f"  Search latency ms (all): mean={s_lat.get('mean', 0.0):.2f}  "
    f"p50={s_lat.get('p50', 0.0):.2f}  p95={s_lat.get('p95', 0.0):.2f}"
)
print(
    f"  Test split latency ms:   mean={val(test_m.get('latency_ms_mean')):.2f}  "
    f"p50={val(test_m.get('latency_ms_p50')):.2f}  p95={val(test_m.get('latency_ms_p95')):.2f}"
)
print(f"  Chunks table size:       {table_bytes} bytes ({table_bytes / 1024.0:.1f} KiB)")
print(f"  Selected mode:           {selected}")
print(
    f"  Dev  metrics:            Recall@5={val(dev_m['recall_at_5']):.4f}  "
    f"MRR={val(dev_m['mrr']):.4f}  Refusal={val(dev_m['refusal_correctness']):.4f}"
)
print(
    f"  Test metrics:            Recall@5={val(test_m['recall_at_5']):.4f}  "
    f"MRR={val(test_m['mrr']):.4f}  CitationF1={val(test_m['citation_f1']):.4f}  "
    f"Refusal={val(test_m['refusal_correctness']):.4f}  FalseAnswer={val(test_m['false_answer_rate']):.4f}"
)
PY

  EXECUTED_GROUPS+=("$CFG_GROUP")
  EXECUTED_SLUGS+=("$CFG_SLUG")
done

if [[ "$DRY_RUN" == "true" ]]; then
  cat <<'EOF'
============================================================
Post-run report commands (dry-run):
  python -m evals.report
  python -m evals.report --check
============================================================
EOF
  exit 0
fi

if [[ ${#EXECUTED_GROUPS[@]} -gt 0 ]]; then
  echo "==> Regenerating evaluation tables (python -m evals.report)..."
  (
    cd "$REPO_ROOT/backend"
    uv run --frozen python -m evals.report
    uv run --frozen python -m evals.report --check
  )
  echo "==> Completed ablation groups: ${EXECUTED_GROUPS[*]}"
fi
