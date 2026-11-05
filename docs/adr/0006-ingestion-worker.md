# ADR 0006 — In-process async ingestion worker and idempotent seed upsert

## Status

Accepted.

## Context

Three limitations in the initial ingestion pipeline blocked real-model usage with `qwen3-embedding:8b` and multi-format lab documents:

1. **Synchronous upload embedding**: `POST /api/v1/documents` previously chunked and embedded documents synchronously inside the HTTP request handler. With an 8B embedding model, multi-page PDFs or long notes could exceed HTTP timeouts.
2. **Markdown-only chunking & unsplit long paragraphs**: Uploads only accepted pasted text split on `## ` headings. Plain text without `## ` collapsed into a single chunk, PDFs were not supported in the UI, and single oversized paragraphs were returned unsplit.
3. **Destructive seed reloads**: `python -m app.seed` called `replace_source`, deleting and recreating all `seed` documents with fresh UUIDs on every container restart. This broke `/documents/{id}` citation links in existing conversations and forced the 8B embedder to re-embed the entire seed corpus on every boot.

## Decision

### 1. Multi-format extraction and adaptive chunking

- **Extraction (`backend/app/rag/extract.py`)**:
  - `extract_upload(filename, payload, max_chars)` accepts `.md`/`.markdown` (`markdown`), `.txt` (`text`), and `.pdf` (`pdf`), rejecting unsupported extensions with `415 unsupported_type`.
  - PDF uploads must start with `%PDF`; encrypted PDFs fail with `422 pdf_encrypted`; image-only/scanned PDFs with fewer than 20 non-whitespace characters across all pages fail with `422 pdf_no_text`. Pages are joined with form-feed (`\f`).
  - Text payloads decode strictly as `utf-8-sig` with fallback to `gb18030`, or fail with `422 bad_encoding`. Documents exceeding `MAX_DOCUMENT_CHARS` (default `300,000`) fail with `413 document_too_long`.
- **Chunking (`backend/app/rag/chunker.py`)**:
  - `chunk_markdown` preserves its exact section-splitting behavior on normal `## ` sections (keeping the v1 evaluation corpus chunks unchanged) and adds a sentence-boundary (`。！？；\n`) plus hard-window fallback in `_split_long` when a single paragraph exceeds `max_chars`.
  - `chunk_plain(text, max_chars=800, overlap=80)` packs paragraphs/sentences into overlapping windows with headings `第 {i} 段`.
  - `chunk_pdf(text, max_chars=800, overlap=80)` splits on `\f` and chunks each page with headings `第 {p} 页` (or `第 {p} 页（{k}）` when a page spans multiple chunks).

### 2. In-process async ingestion worker (D6)

- **Schema (`20261106_0003_ingest_status`)**: `documents` tracks `source_format` (`markdown` | `text` | `pdf`), `original_filename`, `content_sha256`, `error_code`, `attempts`, `updated_at`, and `status` (`pending` | `processing` | `ready` | `failed`), indexed by `documents_pending_idx` on `(created_at) WHERE status = 'pending' AND deleted_at IS NULL`.
- **Upload lifecycle**:
  - `POST /api/v1/documents` enforces per-user upload rate limits (`UPLOAD_RATE_LIMIT=30` per `3600` seconds), extracts text, creates the `Document` row with `status='pending'`, writes a `document_upload` audit event (`format`, `bytes`, `chars`, `grants`), and immediately returns `202 Accepted` with `{"id", "status": "pending"}`.
  - On FastAPI startup (`lifespan`), when `INGEST_WORKER_ENABLED=true`, `recover_processing()` resets any interrupted `processing` rows back to `pending` and spawns `run_worker`.
  - `process_next_pending` claims one row with `SELECT id FROM documents WHERE status = 'pending' AND deleted_at IS NULL ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`, sets `status='processing'` and increments `attempts`.
  - In a fresh session it runs `index_document` (chunking + async embedding + chunk insertion), sets `status='ready'`, clears `error_code`, and writes a `document_indexed` audit event (`{"chunks": n, "ms": t}`).
  - On failure, if `attempts < INGEST_MAX_ATTEMPTS` (default `3`), the document returns to `pending` for retry; otherwise it transitions to `failed` with `error_code` set and writes a `document_index_failed` audit event.
- **Visibility of non-ready documents**: `GET /api/v1/documents` exposes `pending`, `processing`, and `failed` documents only to their owner (`d.status = 'ready' OR d.owner_id = CAST(:uid AS uuid)`), masks `error_code` to `null` for non-owner/non-admin callers, and drives 2-second status polling in the frontend documents page.

### 3. Idempotent seed upsert by `content_sha256` (D11)

- `upsert_source` (`backend/app/rag/ingest.py`) matches existing documents by `(doc_key, source)` and compares `content_sha256`:
  - **Unchanged content**: updates `title`, `collection`, `visibility`, and ACL grants in place while preserving `documents.id` and existing `chunks` without calling the embedder.
  - **Changed content**: keeps `documents.id`, replaces its chunks, and re-embeds in place.
  - **New `doc_key`**: creates and indexes the document.
  - **Removed `doc_key`**: hard-deletes obsolete seed documents no longer present in the seed directories.
- The evaluation harness (`evals/run_eval.py`) continues to use `replace_source` on `source='eval'` so benchmark runs remain hermetic.

## Consequences

File and text uploads return `202 Accepted` sub-second regardless of embedding model size, transient Ollama hiccups recover automatically via worker retries, container restarts no longer re-embed unchanged seed documents or break historical citation links, and the worker loop can later be extracted into a standalone container process (`python -m app.ingest_worker`) without changing the database schema or HTTP contract.
