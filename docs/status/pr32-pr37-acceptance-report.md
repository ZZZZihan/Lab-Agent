# Proposal Cold-Start Import, Temporal Provenance Merge & Dossier/Reuse Basket (PR-32 to PR-39) Full-Stack E2E Acceptance Report

- **Date**: 2026-11-07
- **Base Commit (`main`)**: `0c9aa8a` (includes PR-32 through PR-38)
- **Verification Branch**: `test/pr-39-v22-full-e2e-verification`
- **Specifications Covered**:
  - `docs/specs/proposal-coldstart-spec.md` (Phase 1.5 Cold-Start Import Management, `/lab` Batch Copy/Export, Auto-Reindex & §9.2.D 30-Item Lab QA Eval + Phase 2 Temporal Provenance Merge Engine)
  - `docs/specs/proposal-coldstart-spec-v2.2-dossier-basket.md` (Spec v2.2 Person Dossier, Electronic Signature Extraction/CRUD & Cross-Tab Proposal Reuse Basket)

---

## 1. Wave 1–4 Merged Pull Requests Summary

| Wave | PR | GitHub PR | Merged Commit (`main`) | Scope & Key Deliverables | CI Checks | Codex Review |
| :--- | :--- | :--- | :--- | :--- | :---: | :---: |
| **Wave 1** | **PR-32** | `#53` | `dbec325` | Backend: ImportJob history/detail/reparse/delete APIs, roster re-download, `/lab` entity CRUD (`POST`/`DELETE`) with `sync_lab_profile_rag` auto-reindex, `/lab/format/batch`, `/lab/export` (`.docx` + `.json`), and 30-item Lab QA evaluation suite (`evals/datasets/lab_qa.jsonl`, `backend/tests/test_lab_qa_eval.py`). | 10 / 10 Pass | `clean` (0 issues) |
| **Wave 1** | **PR-33** | `#52` | `6e29524` | Frontend: Standalone `/change-password` route, `/admin/imports` history/detail/reparse/roster/delete UI, `/lab` multi-select batch copy, `.docx`/`.json` export, and admin create/delete entity actions. | 10 / 10 Pass | `clean` (0 issues) |
| **Wave 2** | **PR-34** | `#54` | `0d53fa4` | Backend: Alembic migration `20261110_0008_field_provenance`, `FieldProvenance` temporal priority engine (`effective_value()`, `manual` > `import(newer as_of_time)` > `import(older)`), `merge-preview`, `merge`, `revert`, `backfill-provenance`, and `restore-imported` APIs. | 10 / 10 Pass | `clean` (0 issues) |
| **Wave 2** | **PR-35** | `#55` | `7974cfd` | Frontend: `/admin/imports` temporal merge preview drawer (diff categorization: `new`, `upgrade`, `fill_blank`, `keep_newer`, `keep_manual`, `same_time_conflict`), force-adopt/conflict resolution, one-click `revert`, and `/lab` provenance badges (`手工修订` / `导入 · YYYY-MM` / `基准`) with field-level `恢复为导入值`. | 10 / 10 Pass | `clean` (0 issues) |
| **Wave 3** | **PR-36** | `#56` | `34d6932` | Backend (Spec v2.2): Alembic migration `20261110_0009_v22_lab_people_signature` (5 deferred signature columns + DB `CHECK` constraints), pure-Python PNG/JPEG/WebP validator (`validate_signature_image`), `.docx` 10-person signature extraction (`bind_docx_signatures`, `mc:AlternateContent` single-branch selection, 64-image / 32-MiB quota, `SIG_PI_CONFLICT`, `SIG_DUPLICATE_HASH`), reversible signature snapshot lineage (`prev_writer_job_id`), `GET /lab/people/{id}/dossier`, `GET/PUT/DELETE /lab/people/{id}/signature` (`ETag` / `If-Match` / `If-None-Match` / pre-parse 2.5 MiB stream cap), `POST /lab/format/batch` (mixed-kind), `POST /lab/people/signatures:zip`, and `POST /lab/export:selected`. | 10 / 10 Pass | `clean` (0 issues) |
| **Wave 3** | **PR-37** | `#57` | `5aa05f3` | Frontend (Spec v2.2): `/lab` Person Dossier drawer (`560px`, attribution badges for PI/Participant & First Author/Coauthor, ambiguous-name banner, one-click add-all-to-basket), Electronic Signature management card (thumbnail, `If-Match` optimistic concurrency upload/delete with 412 conflict recovery, single download), Cross-Tab Proposal Reuse Basket (`localStorage` sync across tabs, mixed-kind batch copy, signature ZIP download, selected `.docx` export), and `/admin/imports` Step 2 signature preview & `skip_signature_ids` selection. | 10 / 10 Pass | `clean` (0 issues) |
| **Wave 4** | **PR-38** | `#58` | `0c9aa8a` | Full-Stack Modular Decoupling (`<750` lines/file): Split backend oversized routers/services (`api/lab.py` -> `lab_dossier.py`, `lab_signatures.py`, `lab_basket.py`, `lab_schemas.py`; `api/admin_imports.py` -> `admin_imports_ops.py`, `admin_imports_helpers.py`; `services/provenance.py` -> `provenance_core.py`, `provenance_signature.py`, `provenance_merge.py`, `provenance_revert.py`), split frontend `/lab` & `/admin/imports` pages (`app/lab/_components/*`, `app/admin/imports/_components/*`, `lib/api-lab.ts`), and split v2.2 tests into `test_lab_v22_signatures.py`, `test_lab_v22_merge_revert.py`, and `test_lab_v22_dossier_basket.py`. | 10 / 10 Pass | `clean` (0 issues) |

---

## 2. Stage 1–5 Full-Stack Verification Results on `main` (`0c9aa8a`)

### 2.1 Stage 1: Database Migration Chain (`0007 -> 0008 -> 0009`) & Schema Constraints

Verified on an isolated PostgreSQL (`pgvector:pg16`) database (`lab_test_pr39_mig`):
- **Single Head**: `uv run alembic heads` -> `20261110_0009 (head)` (exactly 1 head).
- **Reversible Upgrade/Downgrade Roundtrip**:
  `upgrade head (20261110_0009)` -> `downgrade -1 (20261110_0008)` -> `downgrade -1 (20261110_0007)` -> `upgrade head (20261110_0009)` completed with zero errors.
- **`lab_people` Signature `CHECK` Constraints Verified (`pg_constraint`)**:
  - `ck_lab_people_signature_all_or_none`
  - `ck_lab_people_signature_mime` (`image/png`, `image/jpeg`, `image/webp`)
  - `ck_lab_people_signature_sha256` (`^[0-9a-f]{64}$`)
  - `ck_lab_people_signature_size` (`1 .. 2097152` bytes)
  - `ck_lab_people_signature_source` (`docx_extract`, `manual_upload`)
- **`field_provenance` Indexes Verified (`pg_indexes`)**:
  - `field_provenance_pkey` (`id`)
  - `field_provenance_entity_field_idx` (`entity_type, entity_id, field_name`)
  - `field_provenance_job_idx` (`source_job_id`)

### 2.2 Stage 2 & Stage 5: Static Analysis, Full Backend Pytest (`183 / 183`) & Frontend Build Gates

| Gate | Command | Result |
| :--- | :--- | :--- |
| **Backend Ruff Lint** | `uv run ruff check app evals tests` | `All checks passed!` (`0` warnings/errors) |
| **Backend Mypy Typecheck** | `uv run mypy app evals` | `Success: no issues found in 64 source files` |
| **Backend Target Suites (10 modules)** | `uv run pytest -v tests/test_proposal_coldstart.py tests/test_lab_qa_eval.py tests/test_phase2_provenance.py tests/test_lab_v22_signatures.py tests/test_lab_v22_merge_revert.py tests/test_lab_v22_dossier_basket.py tests/test_lab_dossier_basket.py tests/test_full_e2e_flow.py tests/test_api_flows.py tests/test_lab_s10_s14_proxy_e2e.py` | `27 / 27 passed` |
| **Backend Remaining Suites** | `uv run pytest -v` (all remaining test modules) | `156 / 156 passed` (**Total: `183 / 183 passed`**) |
| **Frontend ESLint** | `npm run lint` | `0 warnings, 0 errors` |
| **Frontend TypeScript Check** | `npx tsc --noEmit` | `0 errors` |
| **Frontend Security & Contract Tests** | `npm test` (`node --test tests/security.test.mjs`) | `6 / 6 passed` (`519.1 ms`) |
| **Frontend Production Build** | `npm run build` (Next.js 16.4.5 Turbopack) | `15 / 15 routes built` (`/lab`, `/admin/imports`, `/change-password`, `/api/backend/[...path]`, etc.) |

### 2.3 Stage 3: Live S1–S14 End-to-End Verification via Web Proxy (`/api/session` + `/api/backend/*`) & ASGI Backend

Executed via `backend/tests/test_lab_s10_s14_proxy_e2e.py` (`1 passed in 18.54s`), routing browser-contract calls through the Next.js proxy semantics (`Origin` CSRF verification, `HttpOnly` `la_session` cookie -> `Authorization: Bearer`, `If-Match`/`If-None-Match` forwarding, `304` empty body, and `ETag`/`Content-Disposition`/`X-Signature-Count-*`/`X-Export-Warnings*`/`X-Content-Type-Options: nosniff`/`Content-Security-Policy: default-src 'none'; sandbox` response header forwarding):

```text
+-----------------------+--------+-----------+--------------------------------------------------------+
| Step                  | Result | Time (ms) | Detail                                                 |
+-----------------------+--------+-----------+--------------------------------------------------------+
| S1 health             | PASS   |     142.9 | fp=hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|... |
| S2 login              | PASS   |     837.2 | 4 accounts logged in                                   |
| S3 viewer upload      | PASS   |     141.1 | status=403                                             |
| S4 member upload      | PASS   |     479.4 | status=ready                                           |
| S5 cited QA           | PASS   |     382.2 | chars=48                                               |
| S6 refusal            | PASS   |     374.7 | reason=low_evidence_score                              |
| S7 ACL isolation      | PASS   |     776.6 | group=helios zn=404 lk=200                             |
| S8 audit trail        | PASS   |      97.4 | events=42 actions=6                                    |
| S9 cleanup            | PASS   |     150.0 | deleted temporary document                             |
| S10 coldstart+10sig   | PASS   |    2877.9 | saved=9, skipped=1, 3in1_login+chg_pw=ok               |
| S11 temporal+revert   | PASS   |    4222.1 | 2024->2025->2027 merge + revert(A) + restore-import=ok |
| S12 person dossier    | PASS   |     340.7 | sql_queries=4<=4, defer_sig=True, ambig=True           |
| S13 signature CRUD    | PASS   |     912.9 | GET=200(nosniff+CSP), 304, 412, 413, PUT=200, DEL=200  |
| S14 reuse basket      | PASS   |    1416.4 | batch=200(404 missing), zip_inc=2/miss=1, docx_warn=1  |
+-----------------------+--------+-----------+--------------------------------------------------------+
```

### 2.4 Stage 4: 30-Item Lab QA Eval (`test_lab_qa_eval.py`) & GPU Retrieval Regression Gate (`evals.run_eval --check`)

1. **Spec §9.2.D 30-Item Lab QA Evaluation (`uv run pytest -v -s tests/test_lab_qa_eval.py`)**:
   - **Questions**: `30` (`27` answerable across `pi_lookup`, `member_specialty`, `status_enum`, `achievement_kind`, `semantic_match`, `format_reuse` + `3` unanswerable refusals)
   - **`hit@5` (Citation Hit Rate)**: **`1.0000`** (`27 / 27`, threshold `>= 0.85`)
   - **`fact_recall`**: **`1.0000`** (`27 / 27`, threshold `>= 0.80`)
   - **`refusal_accuracy`**: **`1.0000`** (`3 / 3`, threshold `>= 0.90`)
   - **`pii_leak_rate`**: **`0.0000`** (`0` PII or forbidden-fact leaks, threshold `== 0.0`)

2. **GPU Retrieval & Report Regression Gate (`flock ~/.lab-agent/locks/gpu.lock uv run python -m evals.run_eval --check && uv run python -m evals.report --check`)**:
   - **Embedder**: `qwen3-embedding:8b` (`selected_mode: "hybrid_rrf"`, `13` eval documents ingested)
   - **`recall_at_5`**: **`1.0000`** (`ci_low: 1.0`, `ci_high: 1.0`, `n=21`)
   - **`recall_at_1`**: **`0.8095`**
   - **`mrr`**: **`0.9524`** (`ci_low: 0.881`, `ci_high: 1.0`)
   - **`multi_chunk_recall_at_5`**: **`1.0000`**
   - **`citation_f1_when_answered`**: **`0.8889`**
   - **`query_embedding_latency_ms` (p50 / p95)**: **`110.27 ms / 126.15 ms`**
   - **`search_latency_ms` (p50 / p95)**: **`133.73 ms / 150.52 ms`**

---

## 3. Issues Discovered & Fixed During PR-39 Verification

1. **`frontend/app/api/backend/[...path]/route.ts` — Security & Caching Header Forwarding on Binary Endpoints**:
   - *Symptom*: Backend `GET /api/v1/lab/people/{id}/signature` sets `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, and `Cache-Control: private, no-Cache, must-revalidate`, while `/api/backend/[...path]` previously hardcoded `Cache-Control: no-cache` and omitted `x-content-type-options` and `content-security-policy` from its forwarded header allowlist.
   - *Fix*: Added `x-content-type-options` and `content-security-policy` to the forwarded response header list in `frontend/app/api/backend/[...path]/route.ts` and preserved upstream `cache-control` when present (falling back to `"no-cache"`).
