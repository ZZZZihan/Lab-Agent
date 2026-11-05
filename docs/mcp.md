# Lab Agent MCP Server (`app.mcp_server`)

Lab Agent exposes a Model Context Protocol (MCP) server over `stdio` (`python -m app.mcp_server`) so external MCP clients (such as Claude Desktop, Claude Code, or Cursor) can query the laboratory knowledge base and run grounded QA under the same document ACL and append-only audit guarantees as the web API.

---

## 1. MCP Client Configuration (`stdio`)

Ensure the Lab Agent stack (`postgres`, `redis`, `api`) is running via Docker Compose, then configure your MCP client (`claude_desktop_config.json` or `.mcp.json`) to launch `app.mcp_server` inside the `api` container with `MCP_ACTOR_EMAIL` set to a provisioned, active lab user:

```json
{
  "mcpServers": {
    "lab-agent": {
      "command": "docker",
      "args": [
        "compose",
        "-f",
        "/path/to/lab-agent/docker-compose.yml",
        "exec",
        "-T",
        "-e",
        "MCP_ACTOR_EMAIL=member@lab.local",
        "api",
        "python",
        "-m",
        "app.mcp_server"
      ]
    }
  }
}
```

For local host execution against the Compose database (when running from `backend/`):

```json
{
  "mcpServers": {
    "lab-agent-local": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/lab-agent/backend",
        "run",
        "python",
        "-m",
        "app.mcp_server"
      ],
      "env": {
        "MCP_ACTOR_EMAIL": "member@lab.local"
      }
    }
  }
}
```

---

## 2. Identity, ACL, and Audit Model

### Identity & Document ACL
- **Actor resolution**: On every tool invocation, `app.mcp_server` reads `MCP_ACTOR_EMAIL`, looks up the corresponding `User` row in PostgreSQL, verifies `user.is_active == True`, and loads the user's group memberships (`group_members`). If `MCP_ACTOR_EMAIL` is unset, unknown, or deactivated, the call fails closed with `RuntimeError`.
- **Shared ACL boundary**: All retrieval and document read operations (`search_knowledge_base`, `list_collections`, `get_document_excerpt`, `fetch_document`, `query_lab_projects`, `query_meetings`, and `answer_question`) pass the resolved `Principal(id=user.id, role=user.role, group_ids=group_ids)` into the same SQL (`acl_sql()`) and Python (`can_read()`) authorization checks used by the HTTP API:
  - `lab` documents are readable by all active users.
  - `restricted` documents require an explicit `read` grant in `document_acl` matching the user's `id`, `role`, or one of their `group_ids` (or ownership / `admin` role).
  - `private` documents are readable only by their `owner_id` or `admin`.

### Audit Logging (`channel="mcp"`)
- Every MCP tool call generates a per-invocation `request_id` (`mcp-<12-hex>`) and appends a hash-chained `AuditEvent` (`action="tool_call"`, `resource_type="mcp_tool"`, `resource_id=<tool_name>`) with:
  ```json
  {
    "tool": "<tool_name>",
    "status": "ok | refused | not_found | not_configured",
    "hit_count": 3,
    "channel": "mcp"
  }
  ```
- Underlying retrieval (`action="retrieval"`), unauthorized document read attempts (`action="permission_denied"`), refusals (`action="answer_refused"`), and LLM calls (`action="llm_call"`) triggered during tool execution are also recorded under the same `request_id` without logging raw prompts, queries, or document text.

### Limitations of `stdio` Transport
- **Process-bound identity**: Because `stdio` MCP servers do not carry per-request HTTP headers or bearer tokens, the acting user is fixed for the lifetime of the `app.mcp_server` process via `MCP_ACTOR_EMAIL`.
- **Single-user workstation scope**: Only launch `app.mcp_server` with an `MCP_ACTOR_EMAIL` matching the workstation owner's identity. Multi-tenant remote MCP access requires per-user Personal Access Tokens (`la_pat_...`) over streamable HTTP transport (tracked in Spec T4.2 / PR-29).
- **Singleton lifecycle**: `Settings`, `Embedder`, and `LLM` instances are lazily initialized once per `app.mcp_server` process and reused across tool calls so remote embedding/LLM HTTP clients are not recreated on every query.

---

## 3. Available MCP Tools

### `search_knowledge_base`
Hybrid dense + BM25 RRF search over all ready, non-deleted documents the actor is authorized to read.

- **Arguments**:
  - `query` (`str`, required): Search query.
  - `k` (`int`, optional, default `8`): Maximum number of chunks to return.
- **Returns** (JSON string):
  ```json
  {
    "hits": [
      {
        "chunk_id": "uuid",
        "document_id": "uuid",
        "title": "文档标题",
        "heading": "章节标题",
        "excerpt": "最多 280 字符的段落摘录……",
        "score": 0.7812
      }
    ]
  }
  ```

### `list_collections`
List document collections (`knowledge`, `project`, `meeting`, `paper_note`) and the count of ready, ACL-visible documents in each collection for the current actor.

- **Arguments**: None
- **Returns** (JSON string):
  ```json
  {
    "status": "ok",
    "collections": [
      {"collection": "knowledge", "document_count": 8},
      {"collection": "project", "document_count": 3},
      {"collection": "meeting", "document_count": 2},
      {"collection": "paper_note", "document_count": 1}
    ],
    "total_documents": 14
  }
  ```

### `get_document_excerpt`
Fetch a bounded text excerpt of one document by `document_id` if the actor has read permission. Unauthorized or missing documents return `{"status": "not_found"}` (and log `permission_denied` when the document exists but is forbidden by ACL).

- **Arguments**:
  - `document_id` (`str`, required): Document UUID.
  - `max_chars` (`int`, optional, default `6000`, clamped to `1..6000`): Maximum excerpt length.
- **Returns** (JSON string):
  ```json
  {
    "status": "ok",
    "document_id": "uuid",
    "title": "文档标题",
    "collection": "knowledge",
    "visibility": "lab",
    "text": "文档正文摘录……",
    "excerpt": "文档正文摘录……"
  }
  ```

### `fetch_document`
Fetch a document by `document_id` (up to 6,000 characters) if the actor can read it, or `{"status": "not_found"}`.

- **Arguments**:
  - `document_id` (`str`, required): Document UUID.
- **Returns** (JSON string):
  ```json
  {
    "document_id": "uuid",
    "title": "文档标题",
    "collection": "knowledge",
    "visibility": "lab",
    "text": "文档正文（最多 6000 字符）……"
  }
  ```

### `query_lab_projects`
Search ACL-visible documents within the `project` collection.

- **Arguments**:
  - `query` (`str`, required): Search query.
  - `k` (`int`, optional, default `8`): Maximum number of chunks to return.
- **Returns** (JSON string):
  ```json
  {
    "status": "ok",
    "hits": [
      {"title": "项目文档", "heading": "里程碑", "excerpt": "……"}
    ]
  }
  ```

### `query_meetings`
Search ACL-visible documents within the `meeting` collection.

- **Arguments**:
  - `query` (`str`, required): Search query.
  - `k` (`int`, optional, default `8`): Maximum number of chunks to return.
- **Returns** (JSON string):
  ```json
  {
    "status": "ok",
    "hits": [
      {"title": "组会纪要", "heading": "决议", "excerpt": "……"}
    ]
  }
  ```

### `search_papers`
External paper search interface. In the MVP (`PAPER_SEARCH_PROVIDER=unconfigured`), returns `status: "not_configured"`.

- **Arguments**:
  - `query` (`str`, required): Paper search query.
- **Returns** (JSON string):
  ```json
  {
    "status": "not_configured",
    "provider": null,
    "papers": []
  }
  ```

### `answer_question`
Run the full LangGraph evidence-gated QA pipeline (`run_agent`) in-process as the MCP actor, preserving refusal semantics (`no_hits`, `low_score`, `low_rare_coverage`, `model_declined`) and citation verification.

- **Arguments**:
  - `question` (`str`, required): Natural-language question.
- **Returns** (JSON string):
  - When answered (`refused: false`):
    ```json
    {
      "refused": false,
      "reason": null,
      "answer": "根据实验室文档中可核对的段落，回答如下。\n[1] ……",
      "citations": [
        {
          "title": "文档标题",
          "heading": "章节标题",
          "document_id": "uuid",
          "excerpt": "被引用的原文段落摘录……"
        }
      ]
    }
    ```
  - When refused (`refused: true`):
    ```json
    {
      "refused": true,
      "reason": "low_score",
      "answer": "",
      "citations": []
    }
    ```
