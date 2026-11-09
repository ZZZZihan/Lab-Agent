# Lab Agent · 清衡实验室科研智能与资产工作台

Lab Agent 是面向科研课题组与实验室构建的**权限隔离知识助手（Grounded RAG）与全生命周期科研资产中枢**。系统将数据库级细粒度访问控制（ACL）、开发集校准的拒答门控、项目申报书（`.doc` / `.docx`）冷启动解析、字段级时间线溯源合并、个人全息档案袋（含内嵌电子签名核验）以及防篡改哈希链审计统一在同一套工程底座中。

> **合成演示数据声明**：本仓库所有文档、评测集与下方界面截图均基于 100% 虚构的**「清衡实验室（Qingheng Lab）」**合成数据（可通过 `python -m app.seed --with-lab-demo` 一键复现），不包含任何真实科研机构、项目或个人敏感信息（PII）。

---

## 核心能力一览

| 模块 | 核心工程机制 |
| --- | --- |
| **零幻觉证据门控问答 (`/chat`)** | 检索阶段在 PostgreSQL + `pgvector` SQL 层内联下推 ACL 过滤；融合 `halfvec(4096)` 向量检索（支持 1024 维前缀 HNSW 两阶段精排）与 `jieba` BM25 倒排（Reciprocal Rank Fusion）。模型调用前必须通过已校准的余弦相似度与稀有词覆盖率双门控；回答强制校验 `[n]` 引用角标并自动挂载核验成员卡片。 |
| **申报书冷启动与三级账号开通 (`/admin/imports`)** | 支持上传历史或新编 `.doc`（OLE2 二进制）/ `.docx` 项目申报书，内存中物理剥离身份证号与手机号（零入库），自动抽取封面、团队成员、承担项目、代表性成果、支撑设备及内嵌电子签名，并支持按 **T1（学号/工作证号）→ T2（拼音登录名）→ T3（姓名）** 三级标识符一键开通全组账号。 |
| **字段级时间线溯源与多本子合并 (`FieldProvenance`)** | 每个实体字段独立维护基准日期（`as_of_time`）、来源类型（`proposal_import` / `manual`）与前序快照。跨年度导入多份申报书时自动识别**新增实体、字段演进更新、空白补齐、时间倒退忽略、人工修改保护与同时间冲突**，支持强制采纳裁决与一键事务级撤销合并（Revert）。 |
| **课题组资料库、全息档案袋与写本子素材篮 (`/lab`)** | 按姓名确定性归一化（NFKC、剥离括号标注与脚标、统一中点符）自动归因每位成员主持/参与的项目与一作/合作成果；提供内嵌电子签名安全预览与 ETag 缓存下载；跨标签页「写本子素材篮」支持自定义排序、一键导出标准申报书格式 `.docx`（附带成员签名表）或打包下载 `.zip` 电子签名包。 |
| **防篡改哈希链审计与 MCP 工具协议 (`/audit` & `/settings`)** | 审计表 `audit_events` 由数据库触发器强制只追加（Append-Only），每行通过 `prev_hash` 与 `row_hash`（SHA-256）构成防篡改哈希链，仅记录动作、资源 ID 与模型用量（零原文、零邮箱、零外发 PII）。内置 `la_pat_` 个人访问令牌（PAT）与标准 MCP（Model Context Protocol）服务端，供 Claude Code / Claude Desktop 直接安全调用。 |

---

## 真实页面展示（基于「清衡实验室」合成演示数据）

### 1. 动态科研研报与证据门控问答 (`/chat`)

回答紧扣当前用户有权访问的知识库分块，内联标注 `[1]`、`[2]` 引用角标，并在画布下方自动挂载经证据核验的课题组成员专长与签名状态卡片；右侧「智能探针与证据检索流（Inspector）」实时展示 Hybrid RRF 命中得分与原文证据胶囊。

![动态科研研报与证据门控问答](docs/assets/screenshots/01-chat-grounded-rag.png)

### 2. 校准拒答门控与 SQL 级权限边界 (`/chat`)

当提问超出知识库记载范围（如未登记的线下实体会议室）或涉及当前账号无权读取的受限文档（如未授权的采购合同）时，ACL 在 SQL 检索阶段直接物理隔离，证据门控在调用生成模型前拦截并返回可核查的「知识库证据边界备忘」，杜绝猜测性幻觉。

![校准拒答门控与权限边界](docs/assets/screenshots/02-chat-calibrated-refusal.png)

### 3. 课题组资料库与字段级人工保护溯源 (`/lab`)

集中管理课题组团队成员（8 人）、承担项目（5 项）、前期成果（5 篇/项）与设备支撑条件（3 台/套）。卡片内联展示每个字段的导入基准日期（如 `来源: 申报书 (2025-03-15)`）与管理员或本人手工修订状态（`来源: 人工修改`），并支持一键「恢复为导入值」。

![课题组资料库与字段级溯源](docs/assets/screenshots/03-lab-assets-hub.png)

### 4. 成员个人全息档案袋与内嵌电子签名 (`/lab`)

点击任意成员即可打开全息档案袋，集中展示从申报书中提取并经魔数/尺寸安全核验的**真实内嵌电子签名**（支持 PNG 下载、替换与删除）、一行式申报书标准简介、个人学术简历全文，以及按姓名确定性归因的关联承担项目与代表性成果。

![成员个人全息档案袋与电子签名](docs/assets/screenshots/04-lab-person-dossier.png)

### 5. 跨标签页「写本子素材篮」与一键申报书导出 (`/lab`)

在成员、项目、成果、设备任意标签页或全息档案袋中勾选条目即可加入底部常驻的「写本子素材篮」，支持上下调整导出顺序、选择签名包命名规则（`01_姓名.ext` / `姓名.ext` / `姓名_ID.ext`）、一键批量复制标准文本、打包下载 `.zip` 签名包或导出带成员签名表的 Word（`.docx`）文档。

![跨标签页写本子素材篮](docs/assets/screenshots/05-lab-reuse-basket.png)

### 6. 申报书导入管理与多本子时间线合并预览 (`/admin/imports`)

管理历次申报书导入任务（`provisioned` 已开号、`merged` 已合并、`parsed` 待开号/待合并）。点击「版本合并预览」即可按本子基准时间（`as_of_time`）自动比对字段级变化：高亮展示**新增独立实体、字段更新、空白字段补齐、时间倒退自动忽略、人工修改保护忽略**，并支持管理员勾选强制采纳或一键撤销历史合并。

![多本子版本合并预览与冲突裁决](docs/assets/screenshots/06-imports-temporal-merge.png)

### 7. 多格式知识库文档与细粒度 ACL 权限管理 (`/documents`)

支持上传与异步解析 Markdown（`.md` / `.txt`）、PDF（`.pdf`）与 Word（`.docx`）文档，以及由课题组资料库自动同步生成的结构化档案分块（`lab_profile`）。每篇文档支持 `lab`（全员可读）、`restricted`（指定成员或项目组如 `helios` 赫利俄斯组可读）与 `private`（仅上传者与管理员可读）三级访问控制。

![多格式知识库文档与细粒度权限](docs/assets/screenshots/07-documents-acl-ingestion.png)

### 8. 防篡改哈希链安全审计日志 (`/audit`)

所有敏感操作（登录、文档检索、模型调用、证据拒答、越权拦截、个人访问令牌签发、申报书导入合并、电子签名读取与打包导出）均实时写入只追加审计表，通过 `prev_hash` 与 `row_hash` 构成 SHA-256 哈希链，不记录任何提问原文、回答正文或个人隐私字段。

![防篡改哈希链安全审计日志](docs/assets/screenshots/08-audit-hash-chain.png)

---

## 合成演示账号与「清衡实验室」测试数据

下面的账号、学号/工号与密码仅属于本地合成演示。当 `APP_ENV=prod` 时，种子脚本会直接拒绝退出，绝不写入任何演示账号。登录框仅在 `NEXT_PUBLIC_DEMO_HINTS=true` 时显示快捷填充提示（生产镜像默认关闭）。

### 1. 基础角色演示账号（`python -m app.seed` 默认写入）

| 姓名 | 登录邮箱 | 角色与权限边界 | 演示密码 |
| --- | --- | --- | --- |
| **沈昭** | admin@lab.local | 管理员（`admin`），可访问全部文档、导入管理、用户/项目组与审计日志 | `LabAgent-Demo-2026-Admin` |
| **林可** | member@lab.local | 课题组成员（`member`），隶属「项目赫利俄斯组（`helios`）」，可读赫利俄斯受限测试记录 | `LabAgent-Demo-2026-Member` |
| **周宁** | zhou@lab.local | 课题组成员（`member`），获单独授权可读「采购审批」，不可读赫利俄斯项目文档 | `LabAgent-Demo-2026-Zhou` |
| **王敏** | viewer@lab.local | 访客（`viewer`），仅可读全组公开文档（`visibility=lab`），无上传与编辑权限 | `LabAgent-Demo-2026-Viewer` |

### 2. 合成课题组档案与三级登录演示（`python -m app.seed --with-lab-demo` 额外写入）

加上 `--with-lab-demo` 参数运行种子脚本时，系统会在不依赖任何外部文件的情况下，自动生成完整的清衡实验室科研资产库（8 名虚拟科研人员、7 枚合成手写体电子签名、5 项科研项目、5 项代表性成果、3 套支撑设备、3 条历次申报书导入任务与字段溯源记录、多格式文档及示例对话）：

```bash
cd backend && uv run python -m app.seed --with-lab-demo
```

除邮箱外，已通过冷启动开通账号的合成成员支持直接使用 **学号/工作证号（T1）、拼音登录名（T2）或姓名（T3）** 登录（初始演示密码见配置项 `IMPORT_DEFAULT_PASSWORD`）：

| 姓名 | 学号 / 工作证号 (T1) | 拼音登录名 (T2) | 职称 / 学位 | 合成研究专长 |
| --- | --- | --- | --- | --- |
| **陈予安** | `20260108` | `chenyuan` | 研究员 · 博士（课题负责人） | 极低温超导磁体、液氦闭环冷却、异构算力集群协同 |
| **陆星河** | `20260115` | `luxinghe` | 副研究员 · 博士（课题负责人） | 可信检索增强生成、证据门控校准、多模态科学文献解析 |
| **顾清辞** | `20260203` | `guqingci` | 高级工程师 · 硕士 | 高速 RDMA 网络互连、NVMe-oF 并行文件系统、容器化算力编排 |
| **苏景行** | `20260319` | `sujingxing` | 博士研究生 · 硕士 | 跨模态实验记录知识图谱、向量量化索引、科学文献表格抽取 |
| **许知微** | `20260422` | `xuzhiwei` | 硕士研究生 · 学士（待合并预览） | 低温测控时序异常检测、实验元数据清洗 |

界面默认中文，点击右上角 `EN` / `中文` 可随时切换双语界面。

### 本地运行

推荐使用仓库自带的 `./scripts/demo.sh` 一键启动并完成端到端冒烟验证（演示操作步骤见 [`docs/demo.md`](docs/demo.md)）。支持**真实模式**与**离线模式**两种运行方式：

#### 1. 真实模式（Magpie 网关 + 宿主机 Ollama 运行 Qwen3-Embedding-8B）

**环境与资源要求**：
- Apple Silicon Mac（建议 ≥16 GB 统一内存；预留至少 6 GB 可用磁盘空间用于存放 8B 嵌入模型权重与校准产物）；
- Docker（Compose v2）；
- 本机运行 Magpie 网关（OpenAI 兼容）；
- 宿主机通过 Homebrew 安装并运行 `ollama`。

**步骤 1：创建独立的 Magpie 密钥文件**
每个客户端应使用独立的 Magpie 网关密钥，以便在 Magpie 中单独统计用量并在需要时独立吊销。将 Lab Agent 专用密钥写入 `~/.config/lab-agent/magpie.key`（权限设为 `600`，**不要**写进 `.env` 或命令行参数）：

```bash
mkdir -p ~/.config/lab-agent && chmod 700 ~/.config/lab-agent
pbpaste > ~/.config/lab-agent/magpie.key && chmod 600 ~/.config/lab-agent/magpie.key
```

**步骤 2：启动宿主机 Ollama 并拉取 `qwen3-embedding:8b`**

```bash
brew services start ollama          # 或者在前台运行 `ollama serve`
ollama pull qwen3-embedding:8b      # Q4_K_M 量化约 5 GB，首次拉取需联网
```

- **为什么 Ollama 不放进 Docker 容器**：macOS 上的 Docker 容器无法调用 Apple Silicon 的 Metal GPU；若将 8B 嵌入模型放进容器内只能退回 CPU 推理，吞吐会慢一个数量级。
- **安全要求**：Ollama 本身没有鉴权机制，必须保持默认仅监听本机回环地址 `127.0.0.1:11434`（容器通过 `host.docker.internal:11434` 访问），**不要**设置 `OLLAMA_HOST=0.0.0.0` 或将其暴露到局域网。

**步骤 3：一键启动与自动冒烟**

```bash
export LLM_MODEL=claude-sonnet-5-5  # 任选一个 Magpie 上可用的对话模型
./scripts/demo.sh
```

- 容器内默认通过 `http://host.docker.internal:3425/v1` 访问本机 Magpie 网关（宿主机直连为 `http://127.0.0.1:3425/v1`）。如果容器连不上默认地址，请在本地 `.env` 里设置 `LLM_BASE_URL`，改成你的网关所在主机的地址。
- `demo.sh` 会依次预检 Magpie 与 Ollama、构建并启动容器、执行 `python -m app.llm_check` 与 `python -m app.embed_check`、在当前嵌入指纹未校准时自动在 dev 集上完成阈值校准，最后跑通 `python -m app.smoke` 端到端冒烟检查。
- **Linux 容器化 Ollama 模式（`--profile linux-embed`）**：在没有宿主机 Ollama 或在 Linux 服务器上部署时，可使用 Compose 的 `linux-embed` profile 在容器内运行 `ollama/ollama:0.32.14` 并自动拉取 `qwen3-embedding:8b`、构建 `lab-agent-qwen3-embed`（详见 [`docs/deploy.md`](docs/deploy.md)）：
  ```bash
  export LLM_MODEL=claude-sonnet-5-5
  ./scripts/init-env.sh --linux-embed && docker compose --profile linux-embed up -d --build --wait
  # 或直接运行端到端冒烟：./scripts/demo.sh --linux-embed
  ```

#### 2. 离线模式（无需密钥与模型权重，CI 默认模式）

只需要 Docker，不依赖 Magpie 或 Ollama：

```bash
./scripts/demo.sh --offline
```

离线模式使用 `EMBEDDER=hashing`（512 维特征哈希）+ `LLM_PROVIDER=fake`（抽取式回退）跑通同一套迁移、种子、阈值检查与端到端冒烟流程，CI 每次 PR 均执行该模式。

> **升级提示（命名统一后的破坏性变化）**：
> - 数据库用户名和库名已改为 `labagent`，旧的本地数据卷与 `.env` 不能继续使用。请执行 `docker compose down -v`，删除旧的 `.env`（或执行 `./scripts/init-env.sh --force`）后再重新启动。
> - 会话与语言 Cookie 已改名为 `la_session`、`la_locale`，已登录的用户需要重新登录一次。

浏览器打开 [http://127.0.0.1:43123](http://127.0.0.1:43123)。API 在 `43124`，Postgres 在 `55432`，Redis 在 `56379`。所有容器端口默认只绑定宿主机回环地址 `127.0.0.1`；需要局域网访问时，自行修改 `docker-compose.yml` 中 `web` 服务的 `ports` 映射（例如改为 `"43123:3000"`）。

不使用 `demo.sh` 或 Docker 时，先起 Postgres（带 pgvector）和 Redis，再：

```bash
cd backend && uv sync && uv run alembic upgrade head && uv run python -m app.reindex --apply --if-needed && uv run python -m app.seed --with-lab-demo
uv run uvicorn app.main:app --host 127.0.0.1 --port 43124
```

另一个终端：

```bash
cd frontend && npm ci && API_INTERNAL_URL=http://127.0.0.1:43124 npm run dev -- --port 43123
```

默认嵌入器是本地哈希嵌入（`EMBEDDER=hashing`），不下载模型，API 镜像也不打包 `torch` 或本地模型权重。真实模式下使用宿主机 Ollama 提供 OpenAI 兼容嵌入接口（`EMBEDDER=openai_compatible`，运行 `qwen3-embedding:8b`，4096 维 `halfvec` 精确余弦检索）。
- 如果要在宿主机上直接运行 API（不经过 Docker）并使用 Hugging Face `sentence-transformers` 加载本地 BGE 模型（如 `BAAI/bge-small-zh-v1.5`），需要先执行 `cd backend && uv sync --extra bge`，再设置 `EMBEDDER=bge`（可通过 `EMBEDDING_MODEL_VERSION` 固定 HF commit revision，通过 `EMBEDDING_QUERY_TEMPLATE` / `EMBEDDING_QUERY_INSTRUCTION` 和 `EMBEDDING_DOCUMENT_PREFIX` 配置查询指令模板与文档前缀）。
- 用 Ollama 运行 bge 类模型（例如 `bge-m3`）**不需要**启用 `EMBEDDER=bge` 或安装 `bge` 额外依赖，直接设置 `EMBEDDER=openai_compatible` 并指向 Ollama 的 `/v1` 端点即可。

默认回答器是抽取式本地回退。真实模式设置 `LLM_PROVIDER=openai_compatible`、通过 `LLM_API_KEY_FILE`（或 `LLM_API_KEY`）提供网关密钥并指定 `LLM_BASE_URL` 与 `LLM_MODEL` 后，拒绝门槛仍然在模型调用之前。

评测与阈值校准（按嵌入指纹 `embedding_fingerprint` 绑定）：

```bash
# 在开发集 (dev) 上拟合阈值，写入运行时目录 ${CALIBRATION_DIR}/<slug>.json
cd backend && uv run python -m evals.run_eval --tune

# 容器内校准当前嵌入模型（只调用嵌入服务，不调用 LLM），校准后重启 api 生效
docker compose exec api python -m evals.run_eval --tune && docker compose restart api

# 将校准文件与评测报告写入仓库 evals/calibrations/<slug>.json 与 evals/results/v1/<slug>.json（供提交）
cd backend && uv run python -m evals.run_eval --tune --commit

# 对照仓库内当前指纹对应的已提交基线（无基线时退出码为 3）
cd backend && uv run python -m evals.run_eval --check
```

若当前嵌入指纹尚未校准（`${CALIBRATION_DIR}/<slug>.json` 与 `evals/calibrations/<slug>.json` 均不存在或指纹不匹配，且未显式设置 `EVIDENCE_SCORE_MIN`），系统会失败关闭（拒答阈值回退为 `0.99`），`/api/v1/health` 返回 `calibrated: false`，前端顶部显示黄色未校准提示横幅。

## Architecture

```mermaid
flowchart LR
  web["Next.js 16 / React 19 UI\n(/chat · /lab · /admin/imports · /audit)"] --> api["FastAPI Backend"]
  api --> importer["Proposal Cold-Start Importer\n(.doc/.docx · PII Strip · Signatures)"]
  importer --> provenance["Temporal Field Provenance\n& Asset Hub (lab_*)"]
  provenance --> worker["Async Ingest & Profile Sync"]
  api --> worker
  api --> agent["LangGraph Agent"]
  agent --> retrieve["SQL-Pushed ACL Hybrid Search"]
  retrieve --> pg["Postgres + pgvector\n(halfvec 4096 + Prefix HNSW)"]
  retrieve --> ollama["Host Ollama\n(Qwen3-Embedding-8B)"]
  retrieve --> bm25["jieba BM25 + RRF"]
  agent --> llm["OpenAI-Compatible LLM Gateway\n(PII Redacted) or Local Fallback"]
  api --> audit["Append-Only Hash-Chained Audit\n(prev_hash -> row_hash)"]
  api --> redis["Redis Rate Limit"]
  mcp["MCP Server (stdio / PAT)"] --> retrieve
```

登录后的浏览器只持有 `httpOnly` 会话 Cookie（`la_session`）。Next.js 同源代理路由将 Cookie 转换为 `Authorization: Bearer` 令牌并附带可信代理签名的 `X-Client-IP` 转发至后端 API。对话检索、文档读取、课题组资料库同步与 MCP 工具共用同一条 SQL 级 ACL 与外发 PII 脱敏管线；只追加哈希链审计表与对话正文物理隔离存放。

## What the baseline actually measured

**Embedder fingerprint**: `hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442` (slug: `hashing-hashing-cjk-v1-512-redact-0-qp-e3b0c442-dp-e3b0c442`, dataset `v1`).

The committed files `evals/calibrations/hashing-hashing-cjk-v1-512-redact-0-qp-e3b0c442-dp-e3b0c442.json` and `evals/results/v1/hashing-hashing-cjk-v1-512-redact-0-qp-e3b0c442-dp-e3b0c442.json` are for the **offline hashing embedder** (`hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442`), not `qwen3-embedding:8b` or `bge`. Thresholds, citation margin, and RRF `k` were chosen on the dev split only (51 questions). The test split (27 questions) was scored once. Refusal uses cosine similarity. Fusion is reciprocal rank fusion, not a weighted sum of min-max scores.

<!-- eval:begin -->
| Fingerprint | Dataset | Mode | Split | Questions | Recall@5 | MRR | Citation F1 | Refusal | False refusal | False answer | Date | Git SHA |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dense (selected) | dev | 51 | 0.949 [0.872, 1.000] | 0.926 [0.853, 0.987] | 0.795 [0.684, 0.897] | 0.804 [0.686, 0.902] | 0.103 [0.026, 0.205] | 0.500 [0.248, 0.752] | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dense (selected) | test | 27 | 0.952 [0.857, 1.000] | 0.888 [0.782, 0.976] | 0.667 [0.476, 0.857] | 0.704 [0.518, 0.852] | 0.191 [0.048, 0.381] | 0.667 [0.333, 1.000] | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | hybrid RRF | dev | 51 | 0.949 [0.872, 1.000] | 0.926 [0.853, 0.987] | 0.795 [0.684, 0.897] | 0.804 [0.686, 0.902] | 0.103 [0.026, 0.205] | 0.500 [0.248, 0.752] | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | hybrid RRF | test | 27 | 1.000 [1.000, 1.000] | 0.921 [0.833, 1.000] | 0.667 [0.476, 0.857] | 0.704 [0.518, 0.852] | 0.191 [0.048, 0.381] | 0.667 [0.333, 1.000] | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `b15d44b` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.795 [0.683, 0.897] | 0.824 [0.726, 0.922] | 0.103 [0.026, 0.205] | 0.417 [0.167, 0.667] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.897 [0.802, 0.976] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.906, 1.000] | 0.795 [0.683, 0.897] | 0.824 [0.726, 0.922] | 0.103 [0.026, 0.205] | 0.417 [0.167, 0.667] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.921 [0.825, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.889 [0.803, 0.957] | 0.843 [0.745, 0.941] | 0.026 [0.000, 0.077] | 0.583 [0.333, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.921 [0.825, 1.000] | 0.762 [0.603, 0.921] | 0.704 [0.518, 0.889] | 0.143 [0.000, 0.286] | 0.833 [0.500, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.906, 1.000] | 0.889 [0.803, 0.957] | 0.843 [0.745, 0.941] | 0.026 [0.000, 0.077] | 0.583 [0.333, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.921 [0.825, 1.000] | 0.762 [0.603, 0.921] | 0.741 [0.556, 0.889] | 0.143 [0.000, 0.286] | 0.667 [0.333, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.914 [0.846, 0.966] | 0.863 [0.765, 0.941] | 0.000 [0.000, 0.000] | 0.583 [0.333, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.952 [0.881, 1.000] | 0.762 [0.603, 0.921] | 0.667 [0.481, 0.852] | 0.143 [0.000, 0.286] | 1.000 [1.000, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.906, 1.000] | 0.914 [0.846, 0.966] | 0.863 [0.765, 0.941] | 0.000 [0.000, 0.000] | 0.583 [0.333, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.952 [0.881, 1.000] | 0.762 [0.603, 0.921] | 0.704 [0.518, 0.852] | 0.143 [0.000, 0.286] | 0.833 [0.500, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.940 [0.897, 0.974] | 0.882 [0.784, 0.961] | 0.000 [0.000, 0.000] | 0.500 [0.250, 0.750] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.976 [0.929, 1.000] | 0.825 [0.667, 0.953] | 0.741 [0.556, 0.889] | 0.095 [0.000, 0.238] | 0.833 [0.500, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.906, 1.000] | 0.940 [0.897, 0.974] | 0.882 [0.784, 0.961] | 0.000 [0.000, 0.000] | 0.500 [0.250, 0.750] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.976 [0.929, 1.000] | 0.825 [0.667, 0.953] | 0.741 [0.556, 0.889] | 0.095 [0.000, 0.238] | 0.833 [0.500, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.914 [0.846, 0.966] | 0.863 [0.765, 0.941] | 0.000 [0.000, 0.000] | 0.583 [0.333, 0.833] | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.952 [0.881, 1.000] | 0.762 [0.603, 0.921] | 0.667 [0.481, 0.852] | 0.143 [0.000, 0.286] | 1.000 [1.000, 1.000] | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.906, 1.000] | 0.914 [0.846, 0.966] | 0.863 [0.765, 0.941] | 0.000 [0.000, 0.000] | 0.583 [0.333, 0.833] | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.952 [0.881, 1.000] | 0.762 [0.603, 0.921] | 0.704 [0.518, 0.852] | 0.143 [0.000, 0.286] | 0.833 [0.500, 1.000] | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.492 [0.302, 0.698] | 0.630 [0.444, 0.815] | 0.429 [0.238, 0.619] | 0.167 [0.000, 0.500] | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.492 [0.302, 0.698] | 0.630 [0.444, 0.815] | 0.429 [0.238, 0.619] | 0.167 [0.000, 0.500] | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.914 [0.846, 0.966] | 0.863 [0.765, 0.941] | 0.000 [0.000, 0.000] | 0.583 [0.333, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.952 [0.881, 1.000] | 0.762 [0.603, 0.921] | 0.667 [0.481, 0.852] | 0.143 [0.000, 0.286] | 1.000 [1.000, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.906, 1.000] | 0.914 [0.846, 0.966] | 0.863 [0.765, 0.941] | 0.000 [0.000, 0.000] | 0.583 [0.333, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.952 [0.881, 1.000] | 0.762 [0.603, 0.921] | 0.704 [0.518, 0.852] | 0.143 [0.000, 0.286] | 0.833 [0.500, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.974 [0.936, 1.000] | 0.927 [0.867, 0.987] | 0.821 [0.718, 0.923] | 0.824 [0.706, 0.922] | 0.026 [0.000, 0.077] | 0.667 [0.417, 0.917] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dense | test | 27 | 0.952 [0.857, 1.000] | 0.888 [0.772, 0.976] | 0.651 [0.460, 0.825] | 0.667 [0.481, 0.852] | 0.143 [0.000, 0.286] | 1.000 [1.000, 1.000] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.937 [0.874, 1.000] | 0.821 [0.718, 0.923] | 0.824 [0.706, 0.922] | 0.026 [0.000, 0.077] | 0.667 [0.417, 0.917] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.921 [0.833, 1.000] | 0.698 [0.508, 0.873] | 0.778 [0.593, 0.926] | 0.143 [0.000, 0.286] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | dev | 51 | 0.987 [0.962, 1.000] | 0.962 [0.923, 1.000] | 0.829 [0.735, 0.923] | 0.863 [0.765, 0.961] | 0.077 [0.000, 0.154] | 0.333 [0.083, 0.583] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dense | test | 27 | 1.000 [1.000, 1.000] | 0.929 [0.857, 1.000] | 0.587 [0.397, 0.794] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | dev | 51 | 1.000 [1.000, 1.000] | 0.957 [0.910, 1.000] | 0.829 [0.735, 0.923] | 0.863 [0.765, 0.961] | 0.077 [0.000, 0.154] | 0.333 [0.083, 0.583] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | hybrid RRF (selected) | test | 27 | 1.000 [1.000, 1.000] | 0.929 [0.857, 1.000] | 0.587 [0.397, 0.794] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 40 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | lexical rerank, pool 15 | test | 27 | 1.000 [1.000, 1.000] | 0.917 [0.821, 1.000] | 0.571 [0.381, 0.762] | 0.630 [0.444, 0.815] | 0.333 [0.143, 0.524] | 0.500 [0.167, 0.833] | 2026-11-05 | `8403acc` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dense | dev | 160 | 0.884 [0.823, 0.935] | 0.852 [0.797, 0.903] | 0.716 [0.635, 0.787] | 0.787 [0.725, 0.850] | 0.138 [0.086, 0.198] | 0.409 [0.273, 0.545] | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dense | test | 160 | 0.935 [0.888, 0.974] | 0.848 [0.791, 0.899] | 0.724 [0.647, 0.799] | 0.869 [0.819, 0.919] | 0.035 [0.009, 0.069] | 0.386 [0.250, 0.545] | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | hybrid RRF | dev | 160 | 0.953 [0.914, 0.983] | 0.882 [0.834, 0.925] | 0.736 [0.658, 0.805] | 0.819 [0.756, 0.881] | 0.095 [0.051, 0.155] | 0.409 [0.273, 0.545] | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | hybrid RRF | test | 160 | 0.940 [0.896, 0.974] | 0.873 [0.821, 0.917] | 0.747 [0.675, 0.813] | 0.863 [0.812, 0.912] | 0.035 [0.009, 0.078] | 0.409 [0.273, 0.568] | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | lexical rerank, pool 40 | test | 160 | 0.957 [0.914, 0.991] | 0.921 [0.878, 0.958] | 0.839 [0.779, 0.888] | 0.844 [0.787, 0.900] | 0.017 [0.000, 0.043] | 0.523 [0.386, 0.682] | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | lexical rerank, pool 15 (selected) | test | 160 | 0.957 [0.914, 0.991] | 0.922 [0.878, 0.958] | 0.835 [0.774, 0.885] | 0.844 [0.787, 0.900] | 0.017 [0.000, 0.043] | 0.523 [0.386, 0.682] | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dense | dev | 160 | 0.983 [0.957, 1.000] | 0.886 [0.837, 0.923] | 0.770 [0.705, 0.829] | 0.850 [0.794, 0.906] | 0.052 [0.017, 0.095] | 0.409 [0.273, 0.568] | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dense | test | 160 | 0.957 [0.914, 0.991] | 0.891 [0.843, 0.932] | 0.805 [0.743, 0.863] | 0.881 [0.831, 0.925] | 0.017 [0.000, 0.043] | 0.386 [0.250, 0.523] | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | hybrid RRF (selected) | dev | 160 | 0.991 [0.974, 1.000] | 0.916 [0.874, 0.952] | 0.799 [0.737, 0.853] | 0.856 [0.800, 0.906] | 0.043 [0.009, 0.086] | 0.409 [0.273, 0.568] | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | hybrid RRF (selected) | test | 160 | 0.974 [0.940, 1.000] | 0.895 [0.852, 0.935] | 0.802 [0.740, 0.859] | 0.881 [0.831, 0.931] | 0.017 [0.000, 0.043] | 0.386 [0.250, 0.545] | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | lexical rerank, pool 40 | test | 160 | 0.957 [0.914, 0.991] | 0.921 [0.878, 0.958] | 0.839 [0.779, 0.888] | 0.844 [0.787, 0.900] | 0.017 [0.000, 0.043] | 0.523 [0.386, 0.682] | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | lexical rerank, pool 15 | test | 160 | 0.957 [0.914, 0.991] | 0.922 [0.879, 0.958] | 0.842 [0.780, 0.891] | 0.844 [0.787, 0.900] | 0.017 [0.000, 0.043] | 0.523 [0.386, 0.682] | 2026-11-05 | `4576097` |

### Breakdown by Question Type (Selected Mode)

| Fingerprint | Dataset | Split | Type | Questions | Recall@5 | Refusal | Date | Git SHA |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 0.889 | 0.889 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 0.750 | 0.250 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.500 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 1.000 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 0.750 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 0.800 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 0.500 | 0.000 | 2026-11-05 | `b15d44b` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.333 | 2026-11-05 | `b15d44b` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 0.909 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 0.778 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 0.750 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.583 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 0.750 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 0.600 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|1024\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.500 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 0.889 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.417 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|2048h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.333 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.417 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.167 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.500 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=9fab7feb\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.167 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dev | `paraphrase` | 9 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | dev | `unanswerable` | 12 | — | 0.417 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | test | `distractor` | 5 | 1.000 | 0.800 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | test | `multi_chunk` | 4 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | test | `paraphrase` | 5 | 1.000 | 1.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442\|ix=prefix1024` | `v1` | test | `unanswerable` | 6 | — | 0.167 | 2026-11-05 | `0c51133` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.417 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.167 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 0.750 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.333 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.500 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `direct` | 11 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `distractor` | 9 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `multi_chunk` | 6 | 1.000 | 1.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase` | 9 | 1.000 | 0.778 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `paraphrase_synonym` | 4 | 1.000 | 0.750 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | dev | `unanswerable` | 12 | — | 0.667 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `direct` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `distractor` | 5 | 1.000 | 0.800 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `multi_chunk` | 4 | 1.000 | 0.750 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase` | 5 | 1.000 | 0.600 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `paraphrase_synonym` | 2 | 1.000 | 0.000 | 2026-11-05 | `8403acc` |
| `openai_compatible\|qwen3-embedding:8b-q8_0@9704fd987c12\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | `unanswerable` | 6 | — | 0.500 | 2026-11-05 | `8403acc` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `acl` | 7 | — | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `direct` | 24 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `distractor` | 18 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `multi_chunk` | 17 | 0.971 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `numeric` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `paraphrase` | 22 | 0.955 | 0.864 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `synonym` | 18 | 0.889 | 0.889 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `unanswerable_far` | 12 | — | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | dev | `unanswerable_near` | 25 | — | 0.160 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `acl` | 7 | — | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `direct` | 24 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `distractor` | 18 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `multi_chunk` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `numeric` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `paraphrase` | 22 | 0.955 | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `synonym` | 18 | 0.778 | 0.889 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `unanswerable_far` | 12 | — | 1.000 | 2026-11-05 | `4576097` |
| `hashing\|hashing-cjk-v1\|512\|redact=0\|qp=e3b0c442\|dp=e3b0c442` | `v2` | test | `unanswerable_near` | 25 | — | 0.080 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `acl` | 7 | — | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `direct` | 24 | 1.000 | 0.958 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `distractor` | 18 | 1.000 | 0.889 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `multi_chunk` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `numeric` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `paraphrase` | 22 | 1.000 | 0.955 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `synonym` | 18 | 0.944 | 0.944 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `unanswerable_far` | 12 | — | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | dev | `unanswerable_near` | 25 | — | 0.280 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `acl` | 7 | — | 0.857 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `direct` | 24 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `distractor` | 18 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `multi_chunk` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `numeric` | 17 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `paraphrase` | 22 | 1.000 | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `synonym` | 18 | 0.833 | 0.889 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `unanswerable_far` | 12 | — | 1.000 | 2026-11-05 | `4576097` |
| `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v2` | test | `unanswerable_near` | 25 | — | 0.360 | 2026-11-05 | `4576097` |

### End-to-End Generation Evaluation

| LLM Model | Returned Model | Embedding | Dataset | Split | Questions | Refusal | False Refusal | False Answer | Citation P | Citation R | Citation F1 | Invalid Cite | Unannotated | Fact Recall | TTFT p50/p95 (ms) | Latency p50/p95 (ms) | Tokens | Date | Git SHA |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `antigravity/gemini-3.8-flash` | `gemini-3.8-flash` | `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | 27 | 0.852 | 0.191 | 0.000 | 0.762 | 0.762 | 0.762 | 0.000 | 0.000 | 0.738 | 2651 / 4507 | 2753 / 4515 | 8070 | 2026-11-06 | `265e8de` |
| `grok/grok-4.7-build-fast` | `grok-4.7-build-fast` | `openai_compatible\|qwen3-embedding:8b@64b933495768\|4096h\|redact=0\|qp=e1e90dad\|dp=e3b0c442` | `v1` | test | 27 | 0.852 | 0.191 | 0.000 | 0.738 | 0.762 | 0.746 | 0.000 | 0.000 | 0.738 | 2030 / 3215 | 2005 / 3307 | 37366 | 2026-11-05 | `4576097` |
<!-- eval:end -->

Mode choice is the best dev Recall@5, then MRR, so dense stays selected when hybrid ties it on dev. Hybrid RRF does not show the collapse that a naive BM25-weighted fusion produced on an earlier paraphrase set. The lexical reranker is not a cross-encoder. Multi-chunk Recall@5 is 1.0 on this set; citation F1 is lower because the tuned policy cites a single section. The hashing embedder still answers two thirds of the unanswerable test questions that share vocabulary with the corpus. That gap is why a calibrated neural embedder (`qwen3-embedding:8b`) is evaluated separately per fingerprint, not a claim this table already makes.

Query latency for this embedder is about 2 ms on CPU inside the eval harness. That number is not comparable to a neural embedder or cross-encoder.

### Qwen3-Embedding-8B v1 Ablation Analysis (E1–E8)

All eight `qwen3-embedding:8b` configurations (`E1`–`E7` via `./scripts/run_embedding_ablation.sh --all` and `E8` with `EMBEDDING_INDEX=hnsw_prefix` `EMBEDDING_PREFIX_DIM=1024`) were evaluated on the `v1` dev split (`n = 51`) and scored once on the `v1` test split (`n = 27`). Under the selection rule (highest dev `Recall@5`, tie-break `MRR`), `hybrid_rrf` is selected for all eight neural configurations.

- **Instruction ablation (`E1` vs `E2` vs `E3`)**:
  - `E1` (lab instruction, `qp=e1e90dad`) and `E2` (generic web search instruction, `qp=9fab7feb`) both reach dev `Recall@5 = 1.000` and dev `MRR = 0.957`, outperforming `E3` (no instruction, `qp=e3b0c442`, dev `Recall@5 = 1.000`, dev `MRR = 0.937`). Dev refusal correctness is `0.863` (`E1`), `0.882` (`E2`), and `0.824` (`E3`).
  - On the test split, `Recall@5 = 1.000` across `E1`/`E2`/`E3`, with test `MRR` of `0.952` (`E1`), `0.976` (`E2`), and `0.921` (`E3`), test `Citation F1` of `0.762` (`E1`), `0.825` (`E2`), and `0.698` (`E3`), and test refusal correctness of `0.704` (`E1`), `0.741` (`E2`), and `0.778` (`E3`).
  - Because `E1` ties `E2` on the primary dev selection metrics (`Recall@5 = 1.000`, `MRR = 0.957`), `E1` remains the default lab configuration.
- **Precision & storage — `halfvec` fp16 vs `vector` fp32 (`E1` vs `E6`)**:
  - `E1` (`4096h`, `halfvec`, `1,605,632` bytes / `1568.0 KiB` `chunks` relation size) and `E6` (`4096`, `vector` fp32, `1,859,584` bytes / `1816.0 KiB`) produce identical dev and test metrics across every column: test `Recall@5 = 1.000`, `MRR = 0.952`, `Citation F1 = 0.762`, `Refusal = 0.704`, `False refusal = 0.143`, and `False answer = 0.833` (difference `= 0.000`), validating ADR D15 with zero measurable accuracy loss.
  - Theoretically, `halfvec(4096)` halves raw vector coordinate payload by 50% (`2` bytes per fp16 dimension = `8,192` bytes/vector vs `4` bytes per fp32 dimension = `16,384` bytes/vector). In practice, `pg_total_relation_size('chunks')` includes PostgreSQL tuple headers, text and `tsvector`/token payloads, indexes, and allocated pages/dead tuples from sequential reindex and `replace_source` runs, resulting in a `13.7%` net reduction in total relation size on disk (`1,605,632` vs `1,859,584` bytes).
- **MRL dimension truncation (`E4` `2048h` HNSW and `E5` `1024` HNSW vs `E1` `4096h` exact)**:
  - Dev `Recall@5` remains `1.000` (and dev `MRR = 0.957`) across `4096`, `2048`, and `1024` dimensions; test `Recall@5` also stays `1.000` (`MRR` drops from `0.952` at `4096` to `0.921` at `2048` and `1024`).
  - Truncating dimensions degrades refusal calibration and citation F1: dev refusal correctness drops from `0.863` (`4096h`) to `0.843` (`2048h`) and `0.824` (`1024`), dev `Citation F1` drops from `0.914` (`4096h`) to `0.889` (`2048h`) and `0.795` (`1024`), and test refusal correctness changes from `0.704` (`4096h`) to `0.741` (`2048h`) and `0.630` (`1024`, test `Citation F1 = 0.571`). This provides the empirical basis for D15: keeping full 4096-dim exact cosine similarity preserves top-1 refusal discrimination.
- **Two-stage prefix HNSW (`E8` `4096h` with `EMBEDDING_INDEX=hnsw_prefix`, `EMBEDDING_PREFIX_DIM=1024` vs `E1` `4096h` exact)**:
  - `E8` (`openai_compatible|qwen3-embedding:8b@64b933495768|4096h|redact=0|qp=e1e90dad|dp=e3b0c442|ix=prefix1024`) indexes `subvector(embedding, 1, 1024)::halfvec(1024)` with HNSW for stage-1 candidate generation (`LIMIT dense_pool`) and reranks those candidates in SQL using exact `halfvec(4096)` cosine distance (`embedding <=> CAST(:qvec AS halfvec)`).
  - On `v1`, `E8` matches `E1` (`hybrid_rrf`) with `0.000` drift across all dev and test metrics (`Recall@5 = 1.000`, `MRR = 0.952`, `Citation F1 = 0.762`, `Refusal = 0.704`, `False refusal = 0.143`, `False answer = 0.833`) while reusing `E1`'s calibrated thresholds when no dedicated `|ix=prefix1024` calibration file is present.
- **Quantization (`E7` `q8_0` `@9704fd987c12` vs `E1` default `Q4_K_M` `@64b933495768`)**:
  - Both reach dev `Recall@5 = 1.000`, dev `MRR = 0.957`, and dev refusal correctness `0.863` (`E7` dev false-answer rate `0.333` vs `E1` `0.583`). On the test split, `E7` records `Recall@5 = 1.000`, `MRR = 0.929`, `Citation F1 = 0.587`, `Refusal = 0.630`, and `False answer = 0.500` (vs `E1` test `Citation F1 = 0.762`, `Refusal = 0.704`, `False answer = 0.833`).
- **Latency & table size**:
  - Across `E1`–`E8` on a local Tesla V100 (32 GB) GPU, per-query embedding latency is `101.2–112.3 ms` mean (`p50 = 100.4–112.1 ms`, `p95 = 115.3–132.2 ms`), end-to-end search latency is `113.7–131.6 ms` mean (`p95 = 128.4–153.3 ms`), and full reindex duration is `15.2–18.2 s`. `pg_total_relation_size('chunks')` is `1.04–1.86 MB` across configurations.
- **Sample size & confidence intervals**:
  - The `v1` test split has only 27 questions (21 answerable and 6 unanswerable, where 1 unanswerable question shifts false-answer rate by `0.167`), so the 95% bootstrap confidence intervals reported in the table above are wide.

### 20,000-Chunk Scale & Two-Stage Prefix HNSW Benchmark

To evaluate retrieval scaling beyond the 48-chunk `v1` corpus without requiring `pgvector > 4000`-dim HNSW support, `python -m evals.bench_scale --chunks 20000` (`evals/results/scale/bench_scale_20000.json`) populates `20,000` chunks (`48` `v1` corpus chunks + `19,952` full-rank synthetic distractor chunks across `998` documents) and compares three retrieval paths on `qwen3-embedding:8b`:

1. **`exact_4096h`** (`EMBEDDING_DIM=4096`, `EMBEDDING_STORAGE=halfvec`, `EMBEDDING_INDEX=none`): exact sequential cosine scan over `halfvec(4096)`.
2. **`two_stage_prefix1024`** (`EMBEDDING_DIM=4096`, `EMBEDDING_STORAGE=halfvec`, `EMBEDDING_INDEX=hnsw_prefix`, `EMBEDDING_PREFIX_DIM=1024`): stage-1 functional HNSW index `chunks_embedding_prefix_hnsw` on `subvector(embedding, 1, 1024)::halfvec(1024)` (`halfvec_cosine_ops`, `m = 16`, `ef_construction = 64`) retrieving the top `dense_pool` (`80`) candidates, followed by stage-2 exact `halfvec(4096)` cosine reranking in the outer SQL query.
3. **`truncated_1024_hnsw`** (`EMBEDDING_DIM=1024`, `EMBEDDING_NATIVE_DIM=4096`, `EMBEDDING_TRUNCATE=true`, `EMBEDDING_STORAGE=vector`, `EMBEDDING_INDEX=hnsw`): single-stage MRL truncation to `vector(1024)` with HNSW (`E5`).

| Path | Storage & Index | DB Search p50 / p95 (ms) | Total Search p50 / p95 (ms) | Table + Index Size | Index Build (s) | Test Recall@5 | Test MRR | Test Citation F1 | Test Refusal |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `exact_4096h` | `halfvec(4096)` (`none`) | 1673.3 / 2078.3 | 1783.4 / 2191.4 | 172.4 MiB (`180,764,672` B) | 0.0 | 1.000 | 0.952 | 0.762 | 0.704 |
| `two_stage_prefix1024` | `halfvec(4096)` (`hnsw_prefix` 1024→4096) | 72.3 / 79.1 | 175.0 / 195.6 | 224.5 MiB (`235,388,928` B) | 275.1 | 1.000 | 0.952 | 0.762 | 0.704 |
| `truncated_1024_hnsw` | `vector(1024)` (`hnsw`) | 50.7 / 60.0 | 153.8 / 175.0 | 271.0 MiB (`284,213,248` B) | 30.0 | 1.000 | 0.944 | 0.571 | 0.630 |

- **Exact-scan chunk-count sweep (`exact_scan_p95_le_300ms_max_chunks = 1,000`)**:
  - Sweeping `exact_4096h` across `1,000`, `5,000`, `10,000`, and `20,000` chunks yields DB search `p50 / p95` latencies of `117.9 / 121.2 ms` (`1,000` chunks, paired total `221.2 / 243.6 ms` including `103.1 ms` `p50` / `124.0 ms` `p95` query embedding), `456.4 / 507.4 ms` (`5,000` chunks), `772.3 / 974.3 ms` (`10,000` chunks), and `1673.3 / 2078.3 ms` (`20,000` chunks).
  - Exact `halfvec(4096)` scan remains under the `p95 <= 300 ms` interactive budget up to **`1,000` chunks** (both DB-only and end-to-end with query embedding), and crosses `300 ms` between `1,000` and `5,000` chunks.
- **Two-stage prefix HNSW accuracy & speedup**:
  - At `20,000` chunks, `two_stage_prefix1024` reduces DB search `p95` latency by **26.3×** (`2078.3 ms` → `79.1 ms`, paired end-to-end `2191.4 ms` → `195.6 ms`) while preserving **100.0%** of `exact_4096h`'s `Recall@5` (`1.000` overall and `1.000` on test, exceeding the `>= 98%` target) and matching `exact_4096h` identically on test `MRR` (`0.9524`), `Citation F1` (`0.7619`), and `Refusal` (`0.7037`).
  - Compared to single-stage `truncated_1024_hnsw` (`vector(1024)`), `two_stage_prefix1024` stays well within interactive latency (`79.1 ms` vs `60.0 ms` DB `p95`) with smaller total disk footprint (`224.5 MiB` vs `271.0 MiB`, because `halfvec(4096)` + `halfvec(1024)` HNSW is more compact than `vector(1024)` fp32 + fp32 HNSW), while avoiding the `-0.191` test `Citation F1` drop (`0.762` vs `0.571`) and `-0.074` test refusal correctness drop (`0.704` vs `0.630`) caused by 1024-dim truncation.
- **Runtime HNSW session tuning (`hnsw.ef_search` & `hnsw.iterative_scan`)**:
  - When `resolved_embedding_index` is `hnsw` or `hnsw_prefix`, `search_knowledge()` executes `SET LOCAL hnsw.ef_search = max(pool * 2, 100)` (`160` for the default `dense_pool = 80`) and, on `pgvector >= 0.8`, `SET LOCAL hnsw.iterative_scan = relaxed_order`. Under selective SQL ACL filters, fixed-depth HNSW graph traversal without `iterative_scan` can return fewer than `LIMIT` rows after post-filtering; `relaxed_order` continues stepping through the HNSW graph until `LIMIT` ACL-visible candidates are collected.
- **Known BM25 candidate-pool `df` limitation at scale**:
  - In `search_knowledge()`, BM25 document frequencies (`df`) and average document length (`avgdl`) are computed over the retrieved candidate pool (`dense_pool + lexical_pool`, up to `160` chunks) rather than corpus-wide across all `20,000+` chunks. At `20,000` chunks, this local-pool approximation shifts relative BM25 IDF weights within the candidate set (for example, query terms that appear in most lexically retrieved candidates receive a lower local IDF than their global rarity across the full table would warrant).

## Known gaps

- **Offline hashing baseline in CI**: The table above is the `hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442` baseline. CI runs offline mode (`--offline`) and never downloads model weights. On the test split the hashing embedder still answers about two thirds of unanswerable questions that share vocabulary with the corpus (false-answer rate 0.667), and citation F1 lags Recall@5 because `cite_k = 1`.
- **Real mode depends on the author's Mac Studio**: Real-mode LLM calls go through the local Magpie gateway (`LLM_PROVIDER=openai_compatible`) and embeddings run on host Ollama (`qwen3-embedding:8b` with Apple Silicon Metal acceleration). If the host is asleep or Magpie/Ollama is stopped, real mode is unavailable.
- **4096-dim `EMBEDDING_INDEX=auto` defaults to exact scan (`none`)**: pgvector caps full-vector HNSW indexes at 2000 dims for `vector` and 4000 dims for `halfvec`. Real-mode 4096-dimensional `qwen3-embedding:8b` embeddings are stored as `halfvec(4096)` with `EMBEDDING_INDEX=auto` resolving to `none` (exact cosine scan, `p95 <= 300 ms` up to ~1,000 chunks). For larger corpora (5,000–20,000+ chunks), set `EMBEDDING_INDEX=hnsw_prefix` (`EMBEDDING_PREFIX_DIM=1024`) to enable two-stage functional prefix HNSW + exact `halfvec(4096)` reranking.
- **Model reranker is not enabled (`RERANKER=off`)**: No cross-encoder or remote `/v1/rerank` model reranker is active in the MVP. The lexical rerank modes in `evals/` are IDF coverage ablations over a candidate pool, not `bge-reranker`.
- **Unconfigured external integrations**: `search_papers` returns `not_configured`. OIDC verification fails closed until issuer, audience, and JWKS are set; it is not wired to a live IdP. Scanned (image-only) PDFs without extractable text are rejected with 422 `pdf_no_text` (no OCR).
- This repository has no license file.

## Layout

- `backend/app` — FastAPI routes, SQL-pushed ACL (`acl.py`), PII redaction (`redact.py`), hash-chained audit (`audit.py`), async ingestion worker, hybrid retrieval (`rag/`), LangGraph agent (`agent/`), MCP server (`mcp_server/`), and synthetic showcase seeding (`seed.py`, `seed_demo_lab.py`)
- `backend/app/importer` — Proposal `.doc` / `.docx` cold-start parser, in-memory PII stripper (`pii.py`), deterministic person attribution (`matching.py`), electronic signature extractor (`signature.py`), temporal field provenance & merge/revert engine (`provenance.py`), and Word/ZIP exporter (`exporter.py`)
- `evals/data/v1` & `evals/data/v2` — Synthetic Chinese evaluation corpora and benchmark question sets
- `evals/calibrations` & `evals/results` — Per-fingerprint calibration thresholds and evaluation reports
- `frontend` — Next.js 16 / React 19 UI (`/chat` Research Canvas & Inspector, `/lab` Assets Hub & Dossier/Basket, `/admin/imports` Cold-Start & Temporal Merge, `/documents`, `/audit`, `/settings`)
- `docs/assets/screenshots` — Real UI screenshots rendered from synthetic Qingheng Lab showcase fixtures
- `docs/specs` — Architecture & domain specifications (`mvp-and-roadmap.md`, `proposal-coldstart-spec-v2.md`, `proposal-coldstart-spec-v2.2-dossier-basket.md`)
- `docs/demo.md` — 10-minute end-to-end walkthrough script
- `docs/adr` — Architecture decision records (0001–0006)
- `SECURITY.md` — Data flow, secrets, retention, and outbound PII redaction rules

## Tests

```bash
cd backend && uv run pytest && uv run ruff check app evals tests && uv run mypy app evals
cd frontend && npm run lint && npx tsc --noEmit
```

Pytest uses the compose database `labagent_test` on port 55432 unless `TEST_DATABASE_URL` is set.

## 生产引导

当 `APP_ENV=prod` 时，入口脚本会跳过演示种子（`app.seed`），不会写入任何 `LabAgent-Demo-2026-*` 演示账号。首次部署生产环境时，通过 `python -m app.admin_cli` 从标准输入传入初始管理员密码（避免密码进入命令行历史或进程列表）：

```bash
# 1. 创建第一个管理员账号（密码至少 12 个字符、包含至少 3 类字符，不能包含邮箱本地部分，也不能使用任何 LabAgent-Demo-2026-* 演示密码）
read -r -s INIT_ADMIN_PW && printf '%s\n' "$INIT_ADMIN_PW" | \
  docker compose exec -T api python -m app.admin_cli create-user \
    --email admin@example.org --name "实验室管理员" --role admin --password-stdin
unset INIT_ADMIN_PW

# 2. 查看已有用户（不输出密码哈希）
docker compose exec -T api python -m app.admin_cli list-users

# 3. 修改角色、停用/启用账号或重置密码
docker compose exec -T api python -m app.admin_cli set-role --email member@example.org --role member
docker compose exec -T api python -m app.admin_cli deactivate --email member@example.org
docker compose exec -T api python -m app.admin_cli activate --email member@example.org
read -r -s NEW_PW && printf '%s\n' "$NEW_PW" | \
  docker compose exec -T api python -m app.admin_cli reset-password \
    --email member@example.org --password-stdin
unset NEW_PW
```

登录后，管理员也可以在前端导航栏进入 **用户**（`/admin/users`）页面在线创建账号、调整角色、停用或启用用户，以及重置密码。

