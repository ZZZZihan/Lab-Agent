# ADR 0001 — Stack and the deviations that are intentional

## Status

Accepted.

## Context

The rewrite targets a current Python and Next.js stack: FastAPI, SQLAlchemy 2, PostgreSQL 16 with pgvector, Redis, LangGraph, and an OpenAI-compatible model client. Two items in the original brief do not fit the images and libraries we can run without extra services.

## Decision

- Lexical retrieval is Okapi BM25 in Python over jieba tokens stored on each chunk. PostgreSQL `simple` full-text search is only a prefilter when the visible corpus is larger than the dense pool. The official `pgvector/pgvector:pg16` image does not ship ParadeDB `pg_search` or `zhparser`, and compiling either into the image would make the local stack harder to reproduce than the ranking quality is worth at this size. `ts_rank_cd` is not the ranker.
- The default embedder is a 512-dimensional hashing embedder (jieba tokens plus CJK character bigrams, L2-normalized). `BAAI/bge-small-zh-v1.5` is the same width and can be selected with `EMBEDDER=bge`. `bge-m3` is 1024 dimensions and would need a new column, so it is out of scope. CI does not download weights.
- The cross-encoder reranker is not loaded by default. A CPU run of `bge-reranker-base` on a small candidate pool was both slow and prone to refusing answerable questions. Lexical rerank remains available as an ablation, and it is labeled as such.
- The MCP SDK in this release exposes `mcp.server.mcpserver.MCPServer`. `mcp.server.fastmcp` is no longer importable, so the server uses `MCPServer`.
- The web app is Next.js App Router with shadcn/ui. `cacheComponents` is off because every page depends on the session cookie.

## Consequences

Hybrid numbers in the committed baseline are RRF over this BM25 implementation plus the hashing embedder. They are not a neural hybrid result. A later move to `pg_search` or `bge-m3` needs a migration and a new baseline, not a silent swap.
