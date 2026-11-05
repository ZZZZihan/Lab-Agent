# Security

Lab Agent is a lab knowledge agent. The controls below are part of the product, not a checklist pasted on top.

## Reporting

Report a vulnerability privately to the repository owner. Do not open a public issue that includes exploit detail, tokens, or document text.

## Secrets

- Configuration comes from the environment or a key file. `JWT_SECRET` shorter than 32 characters, a placeholder, or a low-diversity string aborts startup.
- A non-local LLM provider other than Ollama (`openai_compatible`, `openai`, `deepseek`, `vllm`) refuses to start without a resolved key from `LLM_API_KEY` or `LLM_API_KEY_FILE`.
- `LLM_CAPTURE` is rejected unless `APP_ENV=test`. It exists so tests can prove redaction. It must stay off in any shared environment.
- Demo passwords in the seed script are labeled local fixtures (`LabAgent-Demo-2026-*`). `python -m app.seed` and the API entrypoint both refuse to load them when `APP_ENV=prod`. The login form prefills them only when `NEXT_PUBLIC_DEMO_HINTS=true`, which the Docker image leaves off unless a demo build opts in. Do not reuse these passwords.

## Access control

Retrieval SQL and direct document reads use the same rule: admins see everything, owners see their own documents, `lab` is visible to every signed-in user, `restricted` also allows explicit grants, and `private` ignores grants. A document the caller cannot read returns 404, including when it exists. Search omits those chunks instead of filtering them after ranking.

## Audit

`audit_events` is append-only. Updates are rejected. Deletes run only through the retention job, which sets a transaction-local flag the trigger recognizes. The log records action, actor id, resource id, request id, a hash of the client address, model name, and token counts. It does not record questions, answers, prompts, document text, or email addresses. Default retention is 180 days and can be set between 1 and 3650 days.

## Data sent to a model

- LLM requests are sent to the local Magpie gateway (`LLM_PROVIDER=openai_compatible`), which forwards them to the upstream provider for the selected chat model.
- Only the user's question and the cited evidence chunks are sent. The lab has confirmed (on **2026-11-04**) that outbound transmission of lab data to model providers is permitted. By default (`LLM_REDACT=true`), email addresses, mainland mobile numbers, and 18-digit resident identity numbers in the question and cited chunks are replaced with redaction tokens as defense in depth; this can be turned off with `LLM_REDACT=false` (recorded in the `llm_call` audit event with `"redact": false` and `redaction_count: 0`).
- Lab Agent uses a dedicated Magpie gateway key so its token usage and costs are tracked separately in Magpie.
- The gateway key is read from `LLM_API_KEY` or `LLM_API_KEY_FILE` (such as `~/.config/lab-agent/magpie.key` or `/run/secrets/magpie_key`) and is never written to the repository, `.env`, logs, audit events, or `/health` responses.
- Citations shown to an authorized user keep the original excerpt, because that user can already open the document.

## 数据流向 (Data flow)

### 1. LLM（大语言模型）

- **流向**：用户问题 + 检索选中的引用块（`LLM_REDACT=true` 时先脱敏邮箱、大陆手机号与 18 位身份证号）→ 本机 Magpie 网关（宿主机 `http://127.0.0.1:3425/v1`，容器内 `http://host.docker.internal:3425/v1`）→ 所选对话模型的上游模型提供方。
- **拒答不调用 LLM**：检索决策分数低于按嵌入指纹校准的阈值（`evidence_score_min`）或无命中时，系统直接返回拒答，**不调用大模型**，没有任何文本外发。
- **外发合规确认**：实验室已于 **2026-11-04** 确认允许将实验室数据外发至模型提供方。PII 脱敏（`LLM_REDACT=true`）作为默认开启的纵深防御保留；若显式设 `LLM_REDACT=false` 则发送原文，并在 `llm_call` 审计事件中记录 `"redact": false` 与 `"redaction_count": 0`。

### 2. 嵌入（Embeddings）

- **流向**：文档分块原文（入库与校准时）与渲染了 instruction 模板的查询文本（检索时，默认 `EMBEDDING_REDACT=false`）→ 宿主机上的 Ollama 服务（运行 `qwen3-embedding:8b`，宿主机 `http://127.0.0.1:11434/v1`，容器内 `http://host.docker.internal:11434/v1`）。
- **本机边界与网络限制（R2d）**：嵌入计算完全在 Mac Studio 本机（Apple Silicon Metal）处理，**不经过任何第三方外部服务**。Ollama 自身没有鉴权机制，必须保持默认仅监听本机回环地址 `127.0.0.1:11434`（容器经 Docker Desktop / OrbStack 的 `host.docker.internal` 访问），严禁设置 `OLLAMA_HOST=0.0.0.0` 将其暴露到局域网；若确需跨机访问，必须由宿主机防火墙严格限制来源。Lab Agent 仅调用 `/v1/embeddings`，不调用模型拉取或删除接口。离线模式（`EMBEDDER=hashing`）完全在 API 进程内计算，无网络请求。

### 3. 密钥来源、优先级与不落盘承诺

- **解析顺序（`read_secret`）**：
  1. 环境变量优先：当 `LLM_API_KEY`（或 `EMBEDDING_API_KEY`）非空时优先采用；
  2. 密钥文件次之：读取 `LLM_API_KEY_FILE`（或 `EMBEDDING_API_KEY_FILE`）指向的文件。宿主机默认路径为 `${LAB_AGENT_MAGPIE_KEY_FILE:-~/.config/lab-agent/magpie.key}`（权限要求 `600`），Compose 通过 Docker secret 只读挂载到容器内 `/run/secrets/magpie_key`。
- **独立客户端密钥**：Lab Agent 使用独立的 Magpie 网关密钥，便于在 Magpie 后台单独统计用量并在需要时独立吊销。
- **安全承诺**：真实密钥**绝不写入代码仓库、`.env`、命令行参数、结构化日志、审计表（`audit_events`）、异常输出或 `/api/v1/health` 响应**（`/health` 仅返回不含路径与查询参数的 `llm_base_host` / `embedding_base_host`）。

### 4. 相关环境变量

| 类别 | 变量名 | 说明 |
| --- | --- | --- |
| **LLM** | `LLM_PROVIDER` | 模型提供方：`fake`（默认离线）或 `openai_compatible`（Magpie 网关），兼容 `openai` / `deepseek` / `ollama` / `vllm` |
| **LLM** | `LLM_BASE_URL` | 网关地址（容器内默认 `http://host.docker.internal:3425/v1`，宿主机 `http://127.0.0.1:3425/v1`） |
| **LLM** | `LLM_API_KEY` / `LLM_API_KEY_FILE` | 网关密钥（环境变量优先于密钥文件；Compose 中挂载 `/run/secrets/magpie_key`） |
| **LLM** | `LAB_AGENT_MAGPIE_KEY_FILE` | 宿主机上供 Compose 挂载的 Magpie 密钥文件路径（默认 `~/.config/lab-agent/magpie.key`） |
| **LLM** | `LLM_MODEL` | 对话模型名（`openai_compatible` 时必填，如 `claude-sonnet-5-5`、`gemini-3.8-flash`） |
| **LLM** | `LLM_REDACT` | 外发给 LLM 前是否做 PII 脱敏（默认 `true`） |
| **LLM** | `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` / `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | 超时（默认 `60`）、首 token 前重试次数（默认 `2`）、采样温度（`0.1`）、最大输出 token（`800`） |
| **LLM** | `LLM_CAPTURE` | 仅允许在 `APP_ENV=test` 下开启，用于测试捕获外发提示词 |
| **嵌入** | `EMBEDDER` | 嵌入器类型：`hashing`（默认离线）、`openai_compatible`（宿主机 Ollama）、`bge`（宿主机可选） |
| **嵌入** | `EMBEDDING_BASE_URL` | 嵌入服务地址（容器内默认 `http://host.docker.internal:11434/v1`，宿主机 `http://127.0.0.1:11434/v1`） |
| **嵌入** | `EMBEDDING_API_KEY` / `EMBEDDING_API_KEY_FILE` | 远程嵌入密钥（本机 Ollama 不需要，留空） |
| **嵌入** | `EMBEDDING_MODEL` / `EMBEDDING_MODEL_VERSION` | 嵌入模型名与版本/digest（`APP_ENV=prod` 且非 hashing 时必填，写入嵌入指纹） |
| **嵌入** | `EMBEDDING_DIM` / `EMBEDDING_NATIVE_DIM` / `EMBEDDING_TRUNCATE` | 存储维度（`64–4096`，真实模式 `4096`）、原生维度与 MRL 截断开关 |
| **嵌入** | `EMBEDDING_STORAGE` / `EMBEDDING_INDEX` | 向量列存储类型（`vector` / `halfvec`）与索引策略（`auto` / `hnsw` / `none`） |
| **嵌入** | `EMBEDDING_QUERY_TEMPLATE` / `EMBEDDING_QUERY_INSTRUCTION` / `EMBEDDING_DOCUMENT_PREFIX` | 查询指令模板、指令正文与文档前缀（计入嵌入指纹） |
| **嵌入** | `EMBEDDING_REDACT` | 嵌入前是否做 PII 脱敏（默认 `false`） |
| **嵌入** | `EMBEDDING_SEND_DIMENSIONS` / `EMBEDDING_BATCH_SIZE` / `EMBEDDING_TIMEOUT_SECONDS` / `EMBEDDING_MAX_RETRIES` | 请求体是否传 `dimensions`、批大小（`16`）、超时（`120`）、重试次数（`3`） |
| **嵌入** | `OLLAMA_HOST` | 宿主机 Ollama 监听配置（保持不设置，即仅监听 `127.0.0.1:11434`） |
| **校准与检索** | `LOAD_TUNED_CONFIG` / `CALIBRATION_DIR` | 是否加载按指纹绑定的校准文件（默认 `true`）及运行时校准目录（`./data/calibrations`） |
| **校准与检索** | `RETRIEVAL_MODE` / `EVIDENCE_SCORE_MIN` / `CITE_MARGIN` / `CITE_K` / `RARE_COVERAGE_MIN` | 检索模式、拒答余弦阈值、引用分数容差与数量上限（显式环境变量优先于校准文件） |
| **校准与检索** | `RERANKER` / `RERANKER_MODEL` | 模型重排开关（MVP 固定 `off`） |
| **安全与审计** | `APP_ENV` / `JWT_SECRET` / `JWT_TTL_MINUTES` | 运行环境（`dev`/`test`/`prod`）、强校验 JWT 密钥（`SecretStr`）与会话有效期 |
| **安全与审计** | `INTERNAL_PROXY_SECRET` | Next.js 代理透传 `X-Client-IP` 的共享 HMAC 密钥（`APP_ENV=prod` 时必填） |
| **安全与审计** | `RATE_LIMIT_ENABLED` / `LOGIN_IP_RATE_LIMIT` / `LOGIN_IP_RATE_WINDOW_SECONDS` / `LOGIN_ACCOUNT_RATE_LIMIT` / `LOGIN_ACCOUNT_RATE_WINDOW_SECONDS` | 登录双桶限流（按 IP 默认 30 次/60 秒，按账号默认 10 次/900 秒） |
| **安全与审计** | `CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW_SECONDS` / `UPLOAD_RATE_LIMIT` / `UPLOAD_RATE_WINDOW_SECONDS` | 问答与文档上传按用户限流 |
| **安全与审计** | `AUDIT_RETENTION_DAYS` | 审计保留天数（`1–3650`，默认 `180`） |
| **入库** | `MAX_UPLOAD_BYTES` / `MAX_DOCUMENT_CHARS` / `INGEST_WORKER_ENABLED` / `INGEST_MAX_ATTEMPTS` / `INGEST_POLL_SECONDS` | 上传字节与字符上限、进程内异步入库 worker 开关与重试参数 |

## Grounding

The model is called only after a decision score clears a threshold that was fit on the development split. The score is cosine similarity, or the reranker score when a rerank mode is selected. Rank-fusion scores are not thresholds. If the check fails, the API returns a refusal and does not call the model.

## Dependencies and history

Continuous integration runs gitleaks on full history, `pip-audit`, and `npm audit --audit-level=high`, plus the test suite. The default embedder is a local hashing embedder so CI never downloads weights.
