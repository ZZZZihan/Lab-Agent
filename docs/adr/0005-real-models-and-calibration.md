# ADR 0005 — Real models, embedding fingerprints, and per-fingerprint calibration

## Status

Accepted.

## Context

The initial vertical slice (`ADR 0001`–`0004`) ran entirely on an in-process 512-dimensional hashing embedder and an extractive `FakeLLM`. Moving to production-grade retrieval and generation on the author's Apple Silicon Mac Studio exposed several architectural requirements:

1. Remote HTTP embedding and LLM calls must be asynchronous (`async/await`) with connection reuse, bounded retries, and strict secret isolation.
2. Cosine similarity distributions vary drastically across embedding models, model digests, vector dimensions, storage precision (`vector` vs `halfvec`), PII redaction settings, and query instruction templates. Reusing a threshold tuned for `HashingEmbedder` (`0.2475`) on a neural embedder silently breaks refusal gating.
3. `qwen3-embedding:8b` produces 4096-dimensional vectors, exceeding pgvector's HNSW and IVFFlat index limits (`2000` dims for `vector`, `4000` dims for `halfvec`).
4. Token usage accounting on a singleton `LLM` instance suffered from data races under concurrent requests, and streamed answers lacked post-generation verification of `[n]` citations.
5. CI must remain 100% offline, deterministic, and free of external credentials or multi-gigabyte model weights.

## Decision

### 1. Async `Embedder` protocol (D1)

`Embedder` (`backend/app/rag/embedder.py`) is an `async` protocol:

```python
class Embedder(Protocol):
    name: str
    dim: int
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
    async def aclose(self) -> None: ...
```

- `OpenAICompatibleEmbedder` uses a shared `httpx.AsyncClient` with batching (`EMBEDDING_BATCH_SIZE=16`), response re-ordering by `index`, retries on transient network / `429` / `5xx` errors, optional Matryoshka (MRL) prefix truncation + L2 renormalization, and query instruction rendering.
- `BGEEmbedder` remains an optional host-only embedder (`uv sync --extra bge`) that offloads synchronous `SentenceTransformer.encode` calls via `asyncio.to_thread` on CPU without bundling PyTorch into the Docker image.

### 2. Embedding fingerprint & single active vector space (D2, D3)

Every vector space is uniquely identified by `Settings.embedding_fingerprint`:

```text
{embedder}|{model_id}|{dim}{storage_suffix}|redact={0|1}|qp={sha8(render_query(''))}|dp={sha8(document_prefix)}
```

- `model_id` is `{embedding_model}@{embedding_model_version[:12]}` when `EMBEDDING_MODEL_VERSION` is set (such as an Ollama model digest or Hugging Face revision), else `{embedding_model}`.
- `storage_suffix` is `"h"` for `halfvec` and `""` for `vector`.
- `sha8` is the first 8 hex characters of SHA-256 (`e3b0c442` for the empty string).
- The offline hashing baseline fingerprint is fixed at `hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442` (slug: `hashing-hashing-cjk-v1-512-redact-0-qp-e3b0c442-dp-e3b0c442`).
- A single-row metadata table `embedding_index_meta` tracks the active `fingerprint`, `dim`, `storage`, and `index_kind`. If the database state does not match the runtime configuration, `/api/v1/health` reports `embedding_index_ok: false`, and `python -m app.reindex --apply` alters the `chunks.embedding` column, rebuilds or drops the index, and marks documents `pending` for re-indexing.

### 3. High-dimensional vector storage & indexing (D13, D15)

- **Local `qwen3-embedding:8b` via host Ollama (D13)**: In real mode, embeddings are served by Homebrew `ollama` on the Mac Studio host (`http://127.0.0.1:11434/v1` on host, `http://host.docker.internal:11434/v1` from containers) using `EMBEDDER=openai_compatible`. Queries are formatted with `Instruct: {instruction}\nQuery:{query}` while documents are embedded as raw text. Context length is pinned to `8192` via `docker/ollama/Modelfile.qwen3-embed`. `python -m app.embed_check` validates semantic separation and determinism.
- **Storage and indexing strategy (D15)**:
  - **MVP default**: `EMBEDDING_DIM=4096`, `EMBEDDING_STORAGE=halfvec`, `EMBEDDING_INDEX=auto` (which resolves to `none` whenever `dim` exceeds pgvector's HNSW limit of `2000` for `vector` or `4000` for `halfvec`). At lab scale ($10^3\text{–}10^4$ chunks), exact cosine scan over `halfvec(4096)` with SQL ACL filtering guarantees 100% recall without filtered-HNSW recall degradation and halves storage and scan I/O compared to `vector(4096)`.
  - **Ablation & downgrade path**: Client-side MRL truncation (`EMBEDDING_DIM=1024` or `2048` with `EMBEDDING_TRUNCATE=true`) L2-normalizes the prefix vector and builds an HNSW index under a distinct fingerprint.
  - **Scale-out path**: Two-stage retrieval using a functional HNSW index on `subvector(embedding, 1, 1024)::halfvec(1024)` for candidate generation followed by exact 4096-dim cosine reranking preserves full-dimension decision scores.
- **Reranker default (D14)**: `RERANKER=off` in the MVP. Retrieval mode is chosen on the dev split between `dense` and `hybrid_rrf`.

### 4. Per-fingerprint calibration & fail-closed gating (D5)

- Calibration files are stored by fingerprint slug in `${CALIBRATION_DIR}/<slug>.json` (runtime, checked first) and `evals/calibrations/<slug>.json` (committed). Evaluation reports live in `evals/results/v1/<slug>.json`.
- On startup, `Settings` loads the calibration file only if its internal `embedder_fingerprint` matches `self.embedding_fingerprint`.
- If no matching calibration file is found and `EVIDENCE_SCORE_MIN` is not explicitly set in the environment, Lab Agent fails closed with `evidence_score_min = 0.99`, sets `calibrated = False` on `/api/v1/health`, and renders a warning banner in the frontend shell.

### 5. Configurable PII redaction as defense in depth (D4)

- Outbound transmission of lab data to model providers was confirmed as permitted on **2026-11-04**.
- `LLM_REDACT` defaults to `true`: questions and cited chunks pass through `redact_text` (replacing email addresses, mainland mobile numbers, and 18-digit ID numbers) before leaving the process. Setting `LLM_REDACT=false` sends raw text and records `"redact": false` and `"redaction_count": 0` in the `llm_call` audit detail.
- `EMBEDDING_REDACT` defaults to `false`: embeddings are computed locally on the host's Ollama, and redacting chunks would alter vectors and prevent legitimate queries for contact details in lab notes.

### 6. Unified LLM gateway (Magpie), concurrency-safe usage, and citation check (D7, D8, D12)

- **Magpie gateway & secret isolation (D12)**: Real mode uses `LLM_PROVIDER=openai_compatible` pointing at the local Magpie gateway (`http://127.0.0.1:3425/v1` on host, `http://host.docker.internal:3425/v1` in Compose). Each client uses a dedicated Magpie key resolved via `read_secret` (`LLM_API_KEY` env var first, then `LLM_API_KEY_FILE` mounted at `/run/secrets/magpie_key` from `~/.config/lab-agent/magpie.key`). Secrets are wrapped in `SecretStr` and never appear in `.env`, logs, audit events, exception messages, or `/health` (which exposes only `llm_base_host`). `python -m app.llm_check` verifies gateway connectivity.
- **Per-call `Usage` (D7)**: `LLM.generate(question, evidence, usage)` populates a caller-supplied `Usage` dataclass instance per request, eliminating shared mutable state on `app.state.llm`. When an upstream stream omits usage, tokens are estimated as `chars / 2` with `usage.estimated = True`.
- **Prompt isolation & post-generation citation check (D8)**:
  - Evidence passages are formatted as `<evidence id="{i}" title="{title}" section="{heading}">\n{text}\n</evidence>` with literal `</evidence>` escaped as `<\/evidence>`.
  - The system prompt instructs the model to treat everything inside `<evidence>` strictly as data, cite `[n]` at the end of each sentence, or output `无法根据现有文档回答` (`MODEL_DECLINE`) if the evidence is insufficient.
  - After generation, `check_citations(answer, n)` parses `\[(\d{1,2})\]` into `used` (valid, deduplicated in first-appearance order), `invalid` (out-of-range), `uncited` (`len(used) == 0`), and `declined` (answer starts with `MODEL_DECLINE`).
  - If `used` is non-empty, persisted message citations are filtered to only the cited items; if `declined` is true, the response is converted into a `model_declined` refusal and audited under `answer_refused`.

### 7. SSE Event Contract (`POST /api/v1/chat/stream`)

| `type` | Fields | When emitted |
| --- | --- | --- |
| `status` | `phase` | Before each agent tool step begins |
| `tool` | `name`, `status` | After each agent tool step finishes |
| `retrieval` | `mode`, `coverage`, `elapsed_ms`, `hits[]`, `refused` | After retrieval and evidence grading |
| `citations` | `items[]`, `paper_status` | Before LLM streaming starts |
| `token` | `text` | For each streamed token chunk from the LLM |
| `citation_check` | `used[]`, `invalid[]`, `uncited` | After LLM generation completes and citations are verified |
| `refusal` | `reason` (`no_hits` \| `low_score` \| `low_coverage` \| `model_declined`), `score`, `cosine`, `coverage`, `threshold`, `neighbors[]`, `paper_status?` | When a question is refused at retrieval gating or by `MODEL_DECLINE` |
| `error` | `code` (`llm_unavailable` \| `embedding_unavailable` \| `agent_failed` \| ...) | When an upstream service or internal step fails |
| `done` | `conversation_id`, `message_id`, `refused`, `model`, `usage`, `error?` | Final event of the stream |

## Consequences

Switching between the offline CI baseline (`hashing` + `fake`) and the real Mac Studio stack (`qwen3-embedding:8b` on Ollama + Magpie chat models) requires zero code changes. Uncalibrated embedding spaces fail closed visibly rather than silently mis-thresholding, concurrent chat streams track token usage accurately without cross-request leakage, and UI citations reflect what the model actually cited.
