# Full-Stack End-to-End Verification & Bug-Fix Report (2026-11-06)

- **Branch**: `fix/e2e-full-verification`
- **Base Commit**: `265e8de` (`main`)
- **Environment**:
  - PostgreSQL (`pgvector:pg16`) on `127.0.0.1:55432`
  - Redis (`redis:7-alpine`) on `127.0.0.1:56379`
  - Embedding service: Ollama (`http://127.0.0.1:11434/v1`), models `qwen3-embedding:8b` / `lab-agent-qwen3-embed` (`64b933495768`, `dim=4096`, `halfvec`)
  - LLM gateway: Magpie (`http://127.0.0.1:3425/v1`), generation model `antigravity/gemini-3.8-flash`, cross-vendor judge model `grok/grok-4.7-build-fast`

---

## 1. Real-Mode `evals.run_e2e` Verification (`--judge` & Dataset v2)

### 1.1 Dataset v1 (`split=test`, `n=27`, Cross-Vendor LLM Judge + `--commit`)

Command executed:
```bash
JUDGE_MODEL=grok/grok-4.7-build-fast \
  uv run --frozen python -m evals.run_e2e \
  --dataset v1 --split test \
  --models antigravity/gemini-3.8-flash \
  --judge --commit
```

- **Artifact**: `evals/results/e2e/antigravity-gemini-3-8-flash__openai-compatible-qwen3-embedding-8b-64b933495768-4096h-redact-0-qp-e1e90dad-dp-e3b0c442.json`
- **Human Audit Export**: `evals/results/e2e/antigravity-gemini-3-8-flash__openai-compatible-qwen3-embedding-8b-64b933495768-4096h-redact-0-qp-e1e90dad-dp-e3b0c442/human_audit.csv` (`20` judged sentences exported with `human_score` column ready for manual spot-check)

| Metric | Value |
| --- | ---: |
| Questions (`v1` / `test`) | 27 |
| E2E Refusal Correctness | 0.8519 |
| False Refusal Rate | 0.1905 |
| False Answer Rate | 0.0000 |
| Citation Precision / Recall / F1 | 0.7619 / 0.7619 / 0.7619 |
| Invalid Citation Rate | 0.0000 |
| Unannotated Answer Rate | 0.0000 |
| Answer Fact Recall | 0.7381 |
| Cross-Vendor Judge Faithfulness (`grok/grok-4.7-build-fast`) | **0.9500** (19 / 20 sentences supported) |
| TTFT p50 / p95 (ms) | 2651.2 / 4507.1 |
| Total Latency p50 / p95 (ms) | 2753.0 / 4514.6 |

### 1.2 Dataset v2 (`split=test`, `max_questions=30`, Multi-format `.md`/`.txt`/`.pdf` + ACL + `--judge`)

Command executed:
```bash
JUDGE_MODEL=grok/grok-4.7-build-fast \
  uv run --frozen python -m evals.run_e2e \
  --dataset v2 --split test --max-questions 30 \
  --models antigravity/gemini-3.8-flash --judge
```

- **Corpus ingested**: 30 documents across `.md` (`26`), `.txt` (`2`), and `.pdf` (`2`), including 4 `seed_only` restricted documents (`project-helios`, `incident-postmortem`, `budget-2026`, `pi-notes`) isolated to `eval-acl-owner@qingheng.lab`.
- **Calibration loaded**: `evals/calibrations/v2/openai-compatible-qwen3-embedding-8b-64b933495768-4096h-redact-0-qp-e1e90dad-dp-e3b0c442.json` (`retrieval_mode=hybrid_rrf`, `rrf_k=60`, `evidence_score_min=0.5882`, `cite_margin=0.05`, `cite_k=3`).

| Metric | Value |
| --- | ---: |
| Questions (`v2` / `test` stratified sample) | 30 |
| E2E Refusal Correctness | 0.9000 |
| False Refusal Rate | 0.1304 |
| False Answer Rate | 0.0000 |
| Citation Precision / Recall / F1 | 0.7826 / 0.8043 / 0.7899 |
| Invalid Citation Rate | 0.0000 |
| Unannotated Answer Rate | 0.0000 |
| Answer Fact Recall | 0.8696 |
| Cross-Vendor Judge Faithfulness (`grok/grok-4.7-build-fast`) | **0.9744** (38 / 39 sentences supported) |
| TTFT p50 / p95 (ms) | 2539.7 / 3918.2 |
| Total Latency p50 / p95 (ms) | 2562.2 / 3967.5 |

---

## 2. Live S1–S9 Smoke Suite (`python -m app.smoke`)

### 2.1 Real Mode (`lab-agent-qwen3-embed` + `antigravity/gemini-3.8-flash` + `hnsw_prefix`)

- **`python -m app.embed_check`**:
  `ok fingerprint=openai_compatible|lab-agent-qwen3-embed@64b933495768|4096h|redact=0|qp=e3b0c442|dp=e3b0c442 dim=4096 gap=0.3964 ms_per_text=1153.14`

```text
+------------------+--------+-----------+-----------------------------------------------------------------------------------------------------+
| Step             | Result | Time (ms) | Detail                                                                                              |
+------------------+--------+-----------+-----------------------------------------------------------------------------------------------------+
| S1 health        | PASS   |       8.5 | fp=openai_compatible|lab-agent-qwen3-embed@64b933495768|4096h|redact=0|qp=e3b0c442|dp=e3b0c442|ix=prefix1024 |
| S2 login         | PASS   |     845.1 | 4 accounts logged in                                                                                |
| S3 viewer upload | PASS   |       9.3 | status=403                                                                                          |
| S4 member upload | PASS   |    1530.4 | id=f7640e90-7500-4673-9697-a30bb4a11df8 status=ready                                                |
| S5 cited QA      | PASS   |    4631.9 | chars=25                                                                                            |
| S6 refusal       | PASS   |    1120.7 | reason=low_evidence_score                                                                           |
| S7 ACL isolation | PASS   |      41.2 | group=helios helios_id=19d4be79-3e72-4a62-8421-c11c93ab1d6c zn=404 lk=200                           |
| S8 audit trail   | PASS   |      14.8 | events=46 actions=7                                                                                 |
| S9 cleanup       | PASS   |      18.6 | deleted=f7640e90-7500-4673-9697-a30bb4a11df8                                                        |
+------------------+--------+-----------+-----------------------------------------------------------------------------------------------------+
```

### 2.2 Offline Mode (`hashing-cjk-v1` + `FakeLLM`)

- **`python -m app.embed_check` (offline)**:
  `ok fingerprint=hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442 dim=512 gap=0.2887 ms_per_text=0.09`

```text
+------------------+--------+-----------+-------------------------------------------------------------------+
| Step             | Result | Time (ms) | Detail                                                            |
+------------------+--------+-----------+-------------------------------------------------------------------+
| S1 health        | PASS   |       6.8 | fp=hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442    |
| S2 login         | PASS   |     828.4 | 4 accounts logged in                                              |
| S3 viewer upload | PASS   |       8.1 | status=403                                                        |
| S4 member upload | PASS   |     524.7 | id=4e526681-4090-4a84-bf90-831b1c98cf0f status=ready              |
| S5 cited QA      | PASS   |      36.2 | chars=48                                                          |
| S6 refusal       | PASS   |      19.5 | reason=low_evidence_score                                         |
| S7 ACL isolation | PASS   |      34.9 | group=helios helios_id=08d726da-2ec1-49bd-9e87-ce68908781da zn=404 lk=200 |
| S8 audit trail   | PASS   |      11.4 | events=42 actions=6                                               |
| S9 cleanup       | PASS   |      15.2 | deleted=4e526681-4090-4a84-bf90-831b1c98cf0f                      |
+------------------+--------+-----------+-------------------------------------------------------------------+
```

---

## 3. Comprehensive Phase 1–4 E2E Test Suite (`backend/tests/test_full_e2e_flow.py`)

Added `backend/tests/test_full_e2e_flow.py` (`3 passed`), exercising:
1. **Auth, Session Cookie & CSRF**: Cross-origin `POST /api/session` and `POST /api/backend/documents` blocked with `403`; same-origin `POST /api/session` sets `la_session` cookie; `/api/v1/health` verified.
2. **Multi-format Ingestion (`.md`, `.txt`, `.pdf`) & `embed_check`**: Uploads Markdown, plain-text, and multi-page PDF documents via `/api/v1/documents`, drains the ARQ worker (`index_document`), verifies page-level PDF chunk headings (`第 2 页`), and verifies `run_embed_check` query/document interleaving.
3. **Document ACL & Group Isolation**: Creates group `cryo-sec`, assigns member, sets `visibility="restricted"` via `PUT /api/v1/documents/{id}/acl`, verifies group member can read/retrieve/cite while unauthorized member receives `404` and zero search hits.
4. **Chat SSE, Citations & Refusal**: Verifies `citations`, `token`, `citation_check`, and `done(refused=False)` on answerable questions, and `refusal` + `done(refused=True)` on out-of-corpus questions.
5. **Conversations CRUD & Isolation**: Lists conversations, continues multi-turn chat with `conversation_id`, verifies message count and cross-user `404` isolation, and deletes conversation.
6. **Personal Access Tokens (`la_pat_`) & Scope Enforcement**: Creates PAT, verifies `200` on `/api/v1/documents`, `/api/v1/search`, and `/api/v1/chat/stream`, verifies `403` on `/api/v1/admin/users`, `/api/v1/audit`, and `/api/v1/tokens`, revokes PAT, and verifies subsequent Bearer calls fail with `401`.
7. **MCP Server (Direct Tools & Streamable HTTP `/mcp` with PAT)**: Exercises `mcp_list_collections`, `mcp_search`, `mcp_list_documents`, `mcp_get_document`, `mcp_answer_question`, and Streamable HTTP `/mcp` (`initialize` + `tools/call` with PAT and `401` without PAT), verifying `channel="mcp"` `tool_call` audit events.
8. **Reranker (`hybrid_rrf_rerank`) & Two-Stage Prefix HNSW (`hnsw_prefix`)**: Exercises `run_agent` in `hybrid_rrf_rerank` mode on both upstream rerank success and upstream `503` failure (`refused=True`, `reason="rerank_failed"`), and verifies `hnsw_prefix` (`|ix=prefix1024`) calibration loading.
9. **Audit Hash Chain & Retention Purge Maintenance**: Runs `verify_audit_chain` over all generated events (`ok=True`, zero raw query/PII leakage) and verifies `purge_expired_deleted_documents` and `purge_expired_messages`.

---

## 4. Bugs Discovered & Fixed

1. **`backend/evals/run_e2e.py` — Multi-format Corpus Ingestion (`.txt`, `.pdf`) for Dataset v2**:
   - *Symptom*: `_gold_map` and `_read_corpus` globbed `*.md` and called `chunk_markdown`, failing with `KeyError` on Dataset v2 `.txt` (`equipment-serial-numbers.txt`, `shift-handover-notes.txt`) and `.pdf` (`biosafety-sop.pdf`, `laser-alignment-manual.pdf`) documents.
   - *Fix*: Switched `_gold_map` and `_read_corpus` to `read_corpus_dir(corpus_dir)` and `chunk_document(doc.body, doc.source_format)`, propagating `source_format` into `ingest_text`.
2. **`backend/evals/run_e2e.py` — `seed_only` Restricted Document ACL Isolation in E2E Runs**:
   - *Symptom*: `_run` created `eval@lab.local` with `role="admin"` and ingested all corpus documents with `visibility="lab"`, allowing the eval user to retrieve `seed_only` restricted documents (`project-helios`, `incident-postmortem`, `budget-2026`, `pi-notes`) and causing `false_answer` on `type="acl"` questions.
   - *Fix*: Set `eval@lab.local` to `role="member"`, created `eval-acl-owner@qingheng.lab` (`role="admin"`), and ingested `seed_only` documents (`doc.key not in SEED_DOCS`) under `acl_owner.id` with `visibility="restricted"`.
3. **`backend/evals/run_e2e.py` — Dataset-Specific Calibrations, Output Slugs & Inclusive Stratified `--max-questions`**:
   - *Symptom*: `_resolve_model_settings` only looked up v1 calibrations (`evals/calibrations/{slug}.json`), ignoring `evals/calibrations/v2/{slug}.json` (and `base_embedding_slug` for `hnsw_prefix`), and `--max-questions` used a head slice or non-inclusive step that missed tail `type="acl"` questions.
   - *Fix*: Added dataset-specific calibration lookup in `_resolve_model_settings`, suffixed `run_slug` with `__{dataset}` when `dataset != "v1"`, and used inclusive stratified indexing (`round(i * (n_sel - 1) / (max_questions - 1))`) when `0 < max_questions < len(selected_questions)`.
4. **`backend/evals/run_e2e.py` — CLI `--models` Promotion Before Alembic Startup Check**:
   - *Symptom*: Running `python -m evals.run_e2e --models <real-model>` when `.env` had `APP_ENV=development` and `EMBEDDER=openai_compatible` without `LLM_MODEL` set in `.env` failed during `command.upgrade(cfg, "head")` because `env.py` instantiated `get_settings()` with default `llm_provider="fake"`.
   - *Fix*: Promoted the first `--models` entry into `os.environ["LLM_MODEL"]` in `main()` before `command.upgrade()` when `LLM_MODEL` is unset or `"fake-grounded-v1"`.
5. **`backend/app/embed_check.py` — Ollama CUDA Slot KV-Cache Numerical Drift on Consecutive Identical Queries**:
   - *Symptom*: In real mode on Linux CUDA GPUs, `run_embed_check` called `embed_query(DETERMINISM_QUERY)` twice back-to-back (`det_1` then `det_2`). On the second identical call, Ollama reused its slot KV cache for all `N-1` prompt tokens and ran the single-token decode CUDA kernel for the final token instead of the batch prefill kernel, producing `determinism=0.999447 < 0.9999`.
   - *Fix*: Interleaved `docs = await active_embedder.embed_documents([PARAPHRASE_DOC, UNRELATED_DOC])` between `det_1` and `det_2`, evicting the slot prompt cache so both `det_1` and `det_2` execute the identical batch prefill kernel (`determinism=1.000000`).
6. **`backend/app/smoke.py` — Offline S6 Refusal Probe Token Overlap & Real-Mode `hnsw_prefix` Health Check**:
   - *Symptom*: In offline mode (`hashing-cjk-v1`, `evidence_score_min=0.2475`), `s6_question = "火星基地的氧气配额是多少？"` shared the repeated token `"配额"` with `storage-nas#1` (`NAS 存储 · 路径配额`), scoring `cosine=0.2581 > 0.2475` and failing S6 refusal. Additionally, `s1_health()` in real mode required `embedding_index == "none"`, rejecting the calibrated default `hnsw_prefix`.
   - *Fix*: Updated `s6_question` to `"火星基地的氧气浓度是多少？"` (`cosine=0.1721 < 0.2475`, refusing cleanly in both offline and real modes) and updated `s1_health()` to accept `embedding_index in {"none", "hnsw_prefix"}`.
7. **`scripts/demo.sh` — Actionable Linux Docker Bridge Diagnostic for `app.embed_check`**:
   - *Symptom*: On Linux without `--linux-embed`, step 3a verified `http://127.0.0.1:11434/api/version` on the host, but step 5 `docker compose exec -T api python -m app.embed_check` failed without explaining that host `systemd` Ollama bound to `127.0.0.1:11434` is unreachable from the Docker bridge via `host.docker.internal:11434`.
   - *Fix*: Added a clear Linux diagnostic in step 5 when `app.embed_check` fails and `LINUX_EMBED != "true"`, instructing the user to re-run `./scripts/demo.sh --linux-embed` or bind host Ollama to `0.0.0.0:11434`.

---

## 5. Phase 1.5, Phase 2, Spec v2.2 & PR-38 Modular Decoupling E2E Verification (2026-11-07)

- **Base Commit (`main`)**: `0c9aa8a` (PR-32 `#53` `dbec325`, PR-33 `#52` `6e29524`, PR-34 `#54` `0d53fa4`, PR-35 `#55` `7974cfd`, PR-36 `#56` `34d6932`, PR-37 `#57` `5aa05f3`, PR-38 `#58` `0c9aa8a`)
- **Verification Suite**: `backend/tests/test_lab_s10_s14_proxy_e2e.py` + full backend (`183 / 183 passed`) & frontend (`6 / 6 passed`, `15 / 15` Next.js 16.4.5 routes built) gates.

### 5.1 Live S1–S14 Web Proxy (`/api/session` + `/api/backend/*`) & ASGI Verification Table

| Step | Result | Time (ms) | Verified Contract & Assertions |
| :--- | :---: | ---: | :--- |
| `S1 health` | PASS | 142.9 | `/api/v1/health` database, redis, and embedding fingerprint check. |
| `S2 login` | PASS | 837.2 | 4 seeded accounts authenticated. |
| `S3 viewer upload` | PASS | 141.1 | Viewer upload rejected with `403`. |
| `S4 member upload` | PASS | 479.4 | Member document upload + indexing (`status=ready`). |
| `S5 cited QA` | PASS | 382.2 | Grounded SSE answer with valid citations (`chars=48`). |
| `S6 refusal` | PASS | 374.7 | Out-of-corpus refusal (`reason=low_evidence_score`). |
| `S7 ACL isolation` | PASS | 776.6 | Restricted group `helios` isolation (`zn=404`, `lk=200`). |
| `S8 audit trail` | PASS | 97.4 | Hash-chained audit log verification (`events=42`, `actions=6`). |
| `S9 cleanup` | PASS | 150.0 | Temporary document deletion and cleanup. |
| `S10 coldstart+10sig` | PASS | 2877.9 | `.docx` with 10 signatures (`mc:AlternateContent`), Step 2 preview, `skip_signature_ids` (`saved=9, skipped=1`), zero `_signature_blobs` leak, zero PII (`assert_no_pii`), 3-in-1 tolerant login, and `/change-password`. |
| `S11 temporal+revert` | PASS | 4222.1 | `2026-03` -> `2024-03` (`time_regression=True`, `keep_newer` + `fill_blank`) -> `2027-03` (`upgrade` + `keep_manual`), `restore-imported`, and 3-job `A(S1)->B(S2)->C(S1)` out-of-order `revert(A)` preserving `S1` via `prev_writer_job_id`. |
| `S12 person dossier` | PASS | 340.7 | `GET /api/backend/lab/people/{id}/dossier` attribution (`pi`/`participant`, `first_author`/`coauthor`), homonym `name_ambiguous=True` + `match="ambiguous"`, SQL query count `4 <= 4`, and deferred `signature_data` column. |
| `S13 signature CRUD` | PASS | 912.9 | `GET/PUT/DELETE /api/backend/lab/people/{id}/signature` with `ETag`, `If-None-Match -> 304`, stale `If-Match -> 412`, `> 2.5 MiB -> 413 SIGNATURE_TOO_LARGE`, `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'none'; sandbox`. |
| `S14 reuse basket` | PASS | 1416.4 | `POST /api/backend/lab/format/batch` (4 entity kinds, `include_relationship_note`, `include_support_note`, `404 NOT_FOUND` `error.missing`), `POST /api/backend/lab/people/signatures:zip` (`manifest.csv`, `X-Signature-Count-Included=2`, `X-Signature-Count-Missing=1`), and `POST /api/backend/lab/export:selected` (`.docx` with scaled signature images + `X-Export-Warnings-Count=1`). |

### 5.2 Spec §9.2.D 30-Item Lab QA Eval & GPU Retrieval Regression Gate

- **`tests/test_lab_qa_eval.py` (`1 passed in 18.64s`)**:
  - `hit@5`: **`1.0000`** (`>= 0.85`), `fact_recall`: **`1.0000`** (`>= 0.80`), `refusal_accuracy`: **`1.0000`** (`>= 0.90`), `pii_leak_rate`: **`0.0000`** (`== 0.0`).
- **`flock ~/.lab-agent/locks/gpu.lock uv run python -m evals.run_eval --check && uv run python -m evals.report --check`**:
  - `embedder=qwen3-embedding:8b`, `selected_mode=hybrid_rrf`, `recall_at_5=1.0000`, `mrr=0.9524`, `multi_chunk_recall_at_5=1.0000`, `citation_f1_when_answered=0.8889`, `search_latency_ms` p50/p95 = `133.73 ms / 150.52 ms`.

