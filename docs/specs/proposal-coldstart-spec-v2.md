# LabAgent 技术规范 Spec v2.0

> 状态：待评审
> 范围：Phase 1（冷启动与账号分发）可直接开工；Phase 2（版本冲突）给出可落地设计；Phase 3（写作输出）只占位，不编码。
> 命名约定：本文沿用代码现有命名 `password`（即口令/密码），例如 `password_hash`、`validate_password`。新增字段和接口保持同一风格，不另起一套命名。

---

## 0. v1 → v2 变更摘要

| 项 | v1 | v2 |
|---|---|---|
| 阶段重心 | 解析、组装、输出并行推进 | Phase 1 只做冷启动与账号分发；Phase 2 处理时间冲突；输出类功能全部推到 Phase 3 |
| 账号来源 | 手工注册或建号 | 从旧本子提取人员，一键批量生成账号并输出分发清单 |
| 登录标识 | 仅邮箱 | 学号/工号、姓名全拼、邮箱三合一 |
| 角色分配 | 未定义 | 导入人员一律为 `member`，导入流程不会产生 `admin` |
| PII | 未定义 | 身份证号、手机号在解析阶段即丢弃，不入库、不进日志、不进清单 |
| 解析方式 | LLM 抽取 | 基于规则的确定性解析，零 token 成本；LLM 不在 Phase 1 范围内 |

---

## 1. 目标与边界

### 1.1 Phase 1：做什么（当前迭代）

1. 管理员上传一份旧本子 `.docx`，系统用规则解析出：
   - 本子基准时间 `as_of_time`（封面"填表日期"）；
   - 人员：申请人与项目组成员；
   - 前期成果、承担项目。
2. 生成预览。管理员确认后，一键为全部人员创建账号，角色统一为 `member`。
3. 输出**登录分发清单**，包括可直接粘贴到微信群的纯文本和 CSV 两种形式。
4. 登录接口支持学号/工号、拼音、邮箱三种标识。
5. 账号首次登录强制改密；初始口令设有有效期。

### 1.2 Phase 1：明确不做

- 不做写新本子、模板组装、字数压缩、防幻觉生成（全部放到 Phase 3）。
- 不做多本子之间的状态冲突裁决（Phase 2）。Phase 1 遇到重复实体时**只跳过、不覆盖**。
- 解析后的本子文本不进入 `documents` 知识库和向量检索，避免 PII 流入 RAG 链路。
- 不持久化原始 `.docx` 文件：在内存中解析完即丢弃，只保留 `sha256`。
- 不调用 LLM。
- OIDC 部署模式下不生成本地口令账号（见 §5.6）。

### 1.3 Phase 2：做什么（下一迭代）

基于 `as_of_time` 和 `entity_fingerprint` 做实体级的状态裁决，防止后上传的旧本子让项目状态倒退。见 §10。

### 1.4 Phase 3：后置

写作输出、模板组装、字数控制。本规范不展开，Phase 1/2 不为它预留代码。

---

## 2. 关键决策与安全红线

### 2.1 角色最小权限（硬编码）

- 导入流程中，新建用户的 `role` **硬编码为 `'member'`**，不接受任何入参覆盖。
- 本子里的职务信息（项目负责人、副院长、教授等）只作为档案字段存入 `lab_people.title`，**与系统权限完全无关**。
- 如果导入人员匹配到已存在的用户（例如系统拥有者本人就是申请人），则**不修改其角色、口令和改密状态**，不会出现 admin 被降级的情况。
- 导入与开号接口仅允许 `admin` 调用。

### 2.2 PII 红线

| 数据 | 处理方式 |
|---|---|
| 18 位身份证号（含末位 X）、15 位旧身份证号 | 解析时识别后**直接丢弃**。不入库、不进 `preview`、不写日志、不出现在清单中 |
| 11 位手机号（联系电话） | 丢弃 |
| 出生日期、性别 | Phase 1 不需要，不提取 |
| 自由文本（学术简历、成果、项目） | 入库前用正则替换为 `[已脱敏]` |
| 原始 `.docx` | 不落盘，仅存 `content_sha256` |

**判定原则：宁可错杀，不可放过。** 只要字符串形态像身份证（`\d{17}[\dXx]`），无论校验位是否正确，一律按身份证处理，不会因校验位错误而被当成学号泄漏出去。

### 2.3 三级自适应登录标识

| 级别 | 条件 | 登录账号 | 示例 |
|---|---|---|---|
| T1 | "证件号"列是 6~12 位学号/工号（含至少 1 位数字，且不是身份证或手机号） | 学号/工号 | `2431101262` |
| T2 | 无邮箱，也无学号/工号 | 姓名全拼（`pypinyin`），重名时加数字后缀 | `wanzhenkai` / `wanzhenkai2` |
| T3 | 本子中有真实邮箱 | 姓名全拼，**同时**可用邮箱登录 | `guojianmin` 或 `guojianmin@tiangong.edu.cn` |

- 系统不会生成任何假邮箱。没有邮箱的人，`email` 字段就是 `NULL`。
- 学号是纯数字或字母数字组合，拼音是纯字母，邮箱含 `@`，三者形态天然不冲突。数据库层仍用唯一约束兜底。

### 2.4 统一初始口令与必备护栏

- 默认初始口令取配置项 `import_default_password`，开源默认值为 `LabAgent@2026`。开号时可传参自定义，例如 `TianGong@2026`。
- 口令必须通过现有的 `validate_password(password, email)` 校验。校验需**逐个用户执行**，因为规则包含"口令不得包含邮箱本地部分"。任何一人校验失败，整批拒绝，并返回失败者的登录名。

> ⚠️ **架构师风险提示（必须同步落地，不属于过度设计）**
> 开源默认口令是公开的，拼音账号又可以被推测，再加上清单会发到微信群（全组都知道彼此的初始口令）。三者叠加，如果没有护栏，任何人都能登录任何人的账号。Phase 1 因此**必须**同时实现以下三条：
> 1. **首次登录强制改密**：处于待改密状态时，除改密、`/me`、登出外的所有接口一律返回 403。
> 2. **初始口令有效期**：默认 7 天（`initial_password_ttl_days`）。过期未改的账号无法用初始口令登录，需要管理员重置。
> 3. **前端提醒**：使用开源默认口令开号时，前端显示黄色警告，建议改用自定义口令。不强制。
>
> 登录限流如果现有代码已经实现，沿用即可；如果没有，按"每个标识 + 每个 IP"限流（例如 10 次/10 分钟）补上。

---

## 3. 数据模型变更

### 3.1 `users` 表：最小侵入改造（3 处）

| 变更 | 说明 |
|---|---|
| `email` 改为 `nullable=True` | 无邮箱成员不生成假邮箱。Postgres 的 `UNIQUE` 允许多个 `NULL`，现有唯一约束无需改动 |
| 新增 `login_name VARCHAR(64) NULL UNIQUE` | 存学号/工号或拼音，一律小写 |
| 新增 `must_change_password_by TIMESTAMPTZ NULL` | **非空表示必须改密，值为截止时间**；改密成功后置为 `NULL`。一列同时承担"待改密"和"初始口令有效期"两项职责 |

```python
# backend/app/models.py — User 增量
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member', 'viewer')", name="users_role"),
        CheckConstraint("email IS NOT NULL OR login_name IS NOT NULL", name="users_login_identity"),
        CheckConstraint("login_name ~ '^[a-z0-9][a-z0-9._-]{2,63}$'", name="users_login_name_format"),
    )
    ...
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    login_name: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    must_change_password_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

`login_name` 的格式约束禁止出现 `@` 和大写字母，从数据库层面保证它与邮箱不会混淆。

**存量影响排查（编码前执行）**：`grep -rn "\.email" backend/ frontend/`，把所有假设 `email` 非空的地方（展示、通知、`validate_password` 调用等）改为可空处理。`validate_password` 的 `email` 参数本来就是 `str | None`，可以直接兼容。

### 3.2 新增表（Phase 1）

所有实体表从 Phase 1 起就记录 `entity_fingerprint`、`as_of_time`、`source_import_id`。这样 Phase 2 不需要回填历史数据，成本只是三列。

```python
class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('parsed', 'provisioned', 'failed')", name="import_jobs_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    original_filename: Mapped[str] = mapped_column(String(255))

    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    parser_version: Mapped[str] = mapped_column(String(16))           # 例如 "rules-1.0"
    as_of_time: Mapped[date | None] = mapped_column(Date, nullable=True)  # 解析不到时为空，开号时必须补齐
    status: Mapped[str] = mapped_column(String(16), default="parsed")
    preview: Mapped[dict] = mapped_column(JSONB)                      # 已脱敏，结构见 §4.9
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)          # 仅计数与 ID，不含 PII
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provisioned_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)


class LabPerson(Base):
    __tablename__ = "lab_people"
    __table_args__ = (
        UniqueConstraint("entity_fingerprint", name="lab_people_fingerprint"),
        CheckConstraint("project_role IN ('applicant', 'member')", name="lab_people_project_role"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)   # 供 OIDC 模式关联，见 §5.6
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)    # 职称/职务，仅作档案，与权限无关
    affiliation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    degree: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_role: Mapped[str] = mapped_column(String(16))                   # 在来源本子中的角色
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)            # 已脱敏
    entity_fingerprint: Mapped[str] = mapped_column(String(64))
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LabAchievement(Base):
    __tablename__ = "lab_achievements"
    __table_args__ = (
        UniqueConstraint("entity_fingerprint", name="lab_achievements_fingerprint"),
        CheckConstraint("kind IN ('paper', 'patent', 'award', 'software', 'other')", name="lab_achievements_kind"),
        CheckConstraint("parse_quality IN ('structured', 'raw')", name="lab_achievements_parse_quality"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    authors_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)                             # 已脱敏的原始条目
    parse_quality: Mapped[str] = mapped_column(String(16))
    entity_fingerprint: Mapped[str] = mapped_column(String(64))
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LabProject(Base):
    __tablename__ = "lab_projects"
    __table_args__ = (
        UniqueConstraint("entity_fingerprint", name="lab_projects_fingerprint"),
        CheckConstraint("status IN ('ongoing', 'completed', 'unknown')", name="lab_projects_status"),
        CheckConstraint("status_source IN ('explicit', 'inferred')", name="lab_projects_status_source"),
        CheckConstraint("lab_role IN ('host', 'participant', 'unknown')", name="lab_projects_lab_role"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(Text)
    funder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grant_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lab_role: Mapped[str] = mapped_column(String(16), default="unknown")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)    # 只到月份的日期统一记为当月 1 日
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_wan: Mapped[```python
    amount_wan: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)  # 单位：万元
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    status_source: Mapped[str] = mapped_column(String(16), default="inferred")
    raw_text: Mapped[str] = mapped_column(Text)                             # 已脱敏的原始条目
    entity_fingerprint: Mapped[str] = mapped_column(String(64))
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

人与成果、项目之间的关联（作者归属、项目参与人）Phase 1 不建表，因为解析出的作者串不可靠，强行关联会产生错误数据。Phase 3 需要时再加。

### 3.3 迁移

使用单个 Alembic revision：`v2_import_and_login_name`。

1. **预检**：执行 `SELECT lower(email), count(*) FROM users GROUP BY 1 HAVING count(*) > 1`。有结果则中止迁移，人工处理。
2. 执行 `UPDATE users SET email = lower(email)`。此后所有写入路径统一小写化。
3. `email` 改为可空，新增 `login_name` 和 `must_change_password_by` 两列，新增两条 `CHECK` 约束。
4. 创建 `import_jobs`、`lab_people`、`lab_achievements`、`lab_projects` 四张表。
5. `downgrade`：如果存在 `email IS NULL` 的用户，直接 `raise`，不做静默删除。

### 3.4 新增配置项

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `import_default_password` | `LabAgent@2026` | 开源默认初始口令 |
| `initial_password_ttl_days` | `7` | 初始口令有效期 |
| `import_max_file_mb` | `10` | 上传大小上限 |
| `import_max_uncompressed_mb` | `50` | 防 zip 炸弹 |
| `login_rate_limit` | `10/600s` | 按标识、按 IP 分别计数 |
| `public_base_url` | 无 | 写入分发清单的登录地址；未配置时开号接口报错 |
| `display_tz` | `Asia/Shanghai` | 清单中截止时间的展示时区 |

---

## 4. 解析与账号生成逻辑

代码位于 `backend/app/importer/`，纯函数实现，便于单测：

```
importer/
  reader.py        # docx → 有序文本单元
  pii.py           # 脱敏（最先执行）
  cover.py         # as_of_time
  people.py        # 人员 + 登录标识判定
  achievements.py
  projects.py
  fingerprint.py
  provision.py     # 开号事务（唯一有 DB 写操作的模块）
```

### 4.1 管道

```
上传 → 校验(大小/扩展名/zip 结构) → 计算 sha256 → reader → pii.scrub
     → cover / people / achievements / projects → fingerprint → preview
     → 写入 ImportJob(status=parsed) → 释放字节
```

- **重复上传**：sha256 命中且该任务状态为 `parsed` 时，直接返回原 `job_id`。如果状态为 `provisioned`，返回 409 `IMPORT_ALREADY_PROVISIONED`，并附带原 `job_id`。
- **文件校验**：
  - 扩展名必须是 `.docx`；
  - 文件头必须是 `PK\x03\x04`；
  - zip 内必须存在 `word/document.xml`；
  - 所有 entry 的解压后大小合计不得超过 `import_max_uncompressed_mb`；
  - 拒绝 `.doc` 和带宏的 `.docm`，提示用户另存为 `.docx`。
- **异常处理**：解析器内部抛出的任何异常都在边界处转换为 `error_code`，不携带单元格原文，避免 PII 随 traceback 进入日志。

### 4.2 文档读取

- 使用 `python-docx`，按 body 中 `w:p` / `w:tbl` 的原始顺序遍历，输出 `Unit(kind=para|cell, table_idx, row, col, text)`。
- **合并单元格**：按 `cell._tc` 的对象身份去重，避免同一内容重复出现。
- **文本规范化**：
  - 执行 NFKC 规范化（全角转半角）；
  - 去除零宽字符（`\u200b-\u200f\ufeff`）；
  - 合并连续空白；
  - 去掉姓名中的空格，例如"王　磊"规范为"王磊"。

### 4.3 PII 清洗（先于一切提取）

```python
ID18   = r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"
ID15   = r"(?<![0-9A-Za-z])\d{15}(?![0-9A-Za-z])"
MOBILE = r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)"
TEL    = r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)"
```

- **人员表**：列名命中"联系电话 / 手机 / 电话 / 出生 / 性别 / 民族"的列整列丢弃，不读取。"证件号"列的值如果命中 `ID18`、`ID15` 或 `MOBILE`，则置空，该人员降级到 T2/T3。
- **所有自由文本**：上述四个正则命中的内容统一替换为 `[已脱敏]`。
- **计数**：只记录 `pii_dropped` 的数量，写入 `warnings`，不记录任何原值。
- **已知误伤**：15 位纯数字的项目编号会被脱敏。这符合"宁可错杀"原则，preview 中会对该项目标注 `GRANT_NO_REDACTED`。

### 4.4 基准时间 `as_of_time`

- **搜索范围**：从文档开头到第一个"一、"级标题，或第一个包含"姓名"的表格之前。
- **标签**：`填表日期 | 填报日期 | 申请日期 | 日期`，标签之后 40 个字符内的第一个日期。
- **支持的日期格式**：
  - `2023年3月15日`
  - `2023-03-15`
  - `2023.3.15`
  - `2023/3/15`
  - `2023年3月`：只有月份时记为当月 1 日
  - `二〇二三年三月`：中文数字先转换为阿拉伯数字
- **合理性校验**：日期须满足 `1990-01-01 ≤ d ≤ today`，否则视为未解析到。
- **解析失败时**：`as_of_time = NULL`，并给出警告 `AS_OF_TIME_MISSING`。开号请求必须在 body 中补齐，否则返回 422。

### 4.5 人员提取

**表格定位**

- **申请人表**：包含"申请人"或"负责人"字样的键值型表格，即"标签格 + 值格"交替排列。
- **成员表**：包含"项目组主要成员 / 主要参与者 / 项目组成员"字样的表格，或其后紧邻的表格。表头行通过列别名识别。

**列别名映射**（匹配前先去掉空格和冒号）：

| 字段 | 别名 |
|---|---|
| name | 姓名 |
| cert | 证件号码 / 身份证号 / 学号 / 工号 / 学号/工号 / 证件号 |
| title | 职称 / 职务 / 技术职称 / 专业技术职务 |
| degree | 学位 / 最高学位 |
| affiliation | 单位 / 单位名称 / 工作单位 / 所在单位 |
| email | 电子邮箱 / 邮箱 / E-mail / Email |

**行过滤**

- 姓名须满足 `^[\u4e00-\u9fa5·]{2,10}$`。
- 包含"合计 / 签字 / 盖章 / 以下空白"的行一律跳过。

**本子内去重**

- 规则：以"规范化姓名 + 规范化单位"为键，相同则视为同一人。
- 申请人同时出现在成员表中时：合并为一人，保留 `project_role = applicant`。

**简历挂接**

- "申请人简历 / 学术简历 / 个人简介"小节的正文经脱敏后，挂到对应人员的 `bio` 字段。
- 小节标题中带有姓名的，按姓名挂接；没有姓名的，默认挂到申请人。

### 4.6 登录标识判定

```python
T1_CERT = re.compile(r"^(?=.*\d)[a-z0-9]{6,12}$")
RESERVED = {"admin", "root", "system", "api", "null", "test", "labagent"}

def decide(person) -> tuple[str, str, str | None]:      # (tier, login_name, email)
    email = valid_email(person.email)                    # 小写；不合法则为 None
    cert = (person.cert or "").lower()
    if cert and T1_CERT.match(cert):
        return "T1", cert, email                         # 有邮箱时同时写入，可用邮箱登录
    base = to_pinyin(person.name)
    return ("T3" if email else "T2"), base, email
```

**`to_pinyin` 的处理规则**

- 调用 `lazy_pinyin(name, style=Style.NORMAL, errors="ignore")` 后拼接。`ü` 输出为 `v`，例如"吕"转为 `lv`。
- 多音字姓氏通过首字覆盖表处理：

  | 姓氏 | 拼音 |
  |---|---|
  | 单 | shan |
  | 曾 | zeng |
  | 解 | xie |
  | 仇 | qiu |
  | 区 | ou |
  | 朴 | piao |
  | 查 | zha |
  | 盖 | ge |
  | 缪 | miao |
  | 尉迟 | yuchi |

- 结果为空或少于 3 个字符时，标记 `LOGIN_NAME_REQUIRED`，由管理员在 preview 中手工填写。

**重名后缀**

- 按人员在本子中出现的顺序依次分配。
- 与 `users.login_name`、本批已分配名、`RESERVED` 均不冲突才算可用；冲突时依次尝试 `base2`、`base3`……
- 发生后缀时给出警告 `LOGIN_NAME_SUFFIXED`，清单中会附带单位信息以便区分（见 §7）。

**已有用户匹配**（parse 时计算一次仅供预览参考，provision 时会重算）：

| 情况 | 结果 |
|---|---|
| `email` 与现有用户邮箱相同 | `existing`，自动关联 |
| T1 学号与现有 `login_name` 相同，且现有用户姓名一致 | `existing` |
| T1 学号相同但姓名不一致 | `conflict`，必须由管理员处理（排除或改名） |
| 拼音与现有账号相同 | **不视为同一人**，按重名规则加后缀 |

如果系统拥有者本人在本子中没有邮箱，管理员需要在 preview 中通过 `link_user_id` 手动绑定。

### 4.7 前期成果

- **小节定位**：标题命中"前期研究成果 / 研究基础 / 代表性成果 / 已发表论文 / 代表作"，到下一个同级标题为止。
- **条目切分**：先按编号前缀切分，前缀正则为 `^\s*[\[［(（]?\d{1,3}[\]］)）.、．]`；如果没有编号，按段落切分。
- **类型判定**（按顺序匹配，先中先得）：

  | 关键词 | kind |
  |---|---|
  | `专利`、`ZL\d`、`发明` | `patent` |
  | `软件著作权`、`软著`、`登记号` | `software` |
  | `奖` | `award` |
  | `[J]`、`[C]`、`[M]`、`Journal`、`学报`、`Proceedings` | `paper` |
  | 其他 | `other` |

- **结构化解析**：尝试按 GB/T 7714 格式 `作者. 题名[J]. 刊名, 年, 卷(期): 页.` 拆出 `authors_text`、`title`、`venue`、`year`，成功则 `parse_quality = structured`。
- **降级处理**：结构化解析失败时，`title` 取 `raw_text` 前 200 个字符，`year` 取文本中最后一个 `(19|20)\d{2}`，`parse_quality = raw`。

### 4.8 承担项目

- **小节定位**：标题命中"承担项目 / 在研项目 / 已完成项目 / 主持或参与的科研项目"。小节内容可能是表格，也可能是段落，两种都要支持。
- **字段提取**：

  | 字段 | 规则 |
  |---|---|
  | `funder` | 匹配"国家自然科学基金 / 国家重点研发计划 / 省…基金 / 横向"等词典 |
  | `grant_no` | 例如 `\b[A-Z]?\d{8}\b`（国自然）、`20\d{2}YF[A-Z]\d{7}` 等，规则表可扩展 |
  | 起止时间 | 例如 `2019.01-2022.12`、`2019年1月至2022年12月`、`2019-2022` |
  | `amount_wan` | `(\d+(?:\.\d+)?)\s*万`；单位为元时除以 1 万 |
  | `lab_role` | 出现"主持 / 负责人"为 `host`；出现"参与 / 参加"为 `participant`；否则为 `unknown` |

  只有年份的起止时间，开始记为 1 月 1 日，结束记为 12 月 1 日。

- **状态判定**：
  - **显式判定**：条目或所在子标题含"在研 / 进行中 / 执行中"，或子标题为"在研项目"时，判为 `ongoing`；含"已完成 / 已结题 / 结题"，或子标题为"已完成项目"时，判为 `completed`。两种情况下 `status_source` 均为 `explicit`。单独出现的"完成"不作为判定依据，因为"计划于 2024 年完成"之类的表述很常见。
  - **推断判定**：没有显式标记时，用 `end_date` 与 `as_of_time` 比较。`end_date < as_of_time` 判为 `completed`，否则判为 `ongoing`。`end_date` 或 `as_of_time` 任一缺失时判为 `unknown`。`status_source` 为 `inferred`。
  - **基准必须是 `as_of_time`，不能用 `today`**。一本 2021 年的本子里写的"在研"，含义是"截至 2021 年在研"。如果用今天的日期去推断，会把历史快照和当前状态混在一起，Phase 2 的裁决就失去依据。
  - parse 阶段 `as_of_time` 缺失时，preview 中推断类状态先记为 `unknown`。provision 时拿到管理员补齐的 `as_of_time` 后**重新推断**，显式类状态不受影响。
- **降级处理**：如果条目无法拆出标题，`title` 取 `raw_text` 前 200 个字符。所有字段都提取失败时，仍以 `raw_text` 入库，不丢弃。

### 4.9 实体指纹与预览结构

#### 4.9.1 实体指纹 `entity_fingerprint`

指纹只描述"它是谁"，**不包含任何状态字段**（`status`、`end_date`、`amount_wan`、`title` 职称、`bio`、`as_of_time`）。这样同一实体在不同年份的本子里指纹相同，Phase 2 才能比较它的状态变化。

```python
# backend/app/importer/fingerprint.py
FP_VERSION = "v1"

def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\W_]+", "", s)          # \W 对 Unicode 生效，保留汉字、字母和数字

def fp(*parts: str) -> str:
    return hashlib.sha256("|".join((FP_VERSION, *parts)).encode("utf-8")).hexdigest()

def person_fp(p) -> str:
    if p.tier == "T1":
        return fp("person", "cert", p.login_name)
    return fp("person", "name", norm(p.name), norm(p.affiliation))

def achievement_fp(a) -> str:
    if a.kind == "patent" and (m := re.search(r"ZL\s?\d{8,13}(\.\d)?", a.raw_text, re.I)):
        return fp("ach", "patent", norm(m.group()))
    if a.kind == "software" and (m := re.search(r"\d{4}SR\d{5,8}", a.raw_text, re.I)):
        return fp("ach", "software", norm(m.group()))
    if a.parse_quality == "structured":
        return fp("ach", a.kind, norm(a.title)[:120], str(a.year or ""))
    return fp("ach", a.kind, "raw", norm(a.raw_text)[:200])

def project_fp(p) -> str:
    if p.grant_no and "GRANT_NO_REDACTED" not in p.warnings:
        return fp("proj", "grant", norm(p.grant_no))
    return fp("proj", "title", norm(p.title)[:120], norm(p.funder))
```

- `FP_VERSION` 参与哈希。以后调整规则时升级版本号，由 Phase 2 的迁移统一重算，避免新旧指纹混用。
- **已知局限**：同一人换了单位、同一项目在不同本子里题名措辞不同，都会生成不同的指纹。Phase 1 接受这一点（结果只是多出一条记录，不会写错数据）。Phase 2 通过"疑似重复"提示处理，见 §10.4。

#### 4.9.2 `preview` 结构

由于原始 `.docx` 不落盘，**provision 的唯一数据来源就是 `preview`**。因此 `preview` 必须包含入库所需的全部字段，并且全部已经脱敏。

```jsonc
{
  "parser_version": "rules-1.0",
  "as_of_time": "2023-03-15",              // 可为 null
  "people": [
    {
      "key": "p1",                         // 本次任务内稳定的键，供 provision 覆盖参数引用
      "name": "郭建民",
      "title": "教授",
      "affiliation": "XX大学计算机学院",
      "degree": "博士",
      "project_role": "applicant",
      "tier": "T3",
      "login_name": "guojianmin",
      "email": "guojianmin@tiangong.edu.cn",
      "bio": "……[已脱敏]……",
      "fingerprint": "9f2c…",
      "match": { "status": "new" },        // new | existing(+user_id) | conflict(+user_id)
      "warnings": []
    }
  ],
  "achievements": [
    { "key": "a1", "kind": "paper", "title": "…", "authors_text": "…", "venue": "…",
      "year": 2022, "raw_text": "…", "parse_quality": "structured", "fingerprint": "…", "warnings": [] }
  ],
  "projects": [
    { "key": "j1", "title": "…", "funder": "国家自然科学基金", "grant_no": "62172xxx",
      "lab_role": "host", "start_date": "2019-01-01", "end_date": "2022-12-01",
      "amount_wan": "58.00", "status": "completed", "status_source": "inferred",
      "raw_text": "…", "fingerprint": "…", "warnings": [] }
  ],
  "warnings": [
    { "code": "PII_DROPPED", "count": 7 },
    { "code": "SECTION_NOT_FOUND", "section": "projects" }
  ]
}
```

#### 4.9.3 警告码

| 代码 | 级别 | 含义 | 是否阻断开号 |
|---|---|---|---|
| `AS_OF_TIME_MISSING` | 任务 | 封面日期未解析到 | 是，provision 时必须补齐 |
| `PII_DROPPED` | 任务 | 丢弃或脱敏的 PII 数量（只记数量） | 否 |
| `SECTION_NOT_FOUND` | 任务 | 某个小节（人员、成果、项目）未定位到 | 人员小节缺失时阻断，其余不阻断 |
| `CERT_DROPPED` | 人员 | 证件号列为身份证或手机号，已丢弃，降级到 T2/T3 | 否 |
| `LOGIN_NAME_REQUIRED` | 人员 | 拼音生成失败 | 是，需手填或排除 |
| `LOGIN_NAME_SUFFIXED` | 人员 | 因重名追加了数字后缀 | 否 |
| `LOGIN_CONFLICT` | 人员 | T1 学号与现有账号相同但姓名不一致 | 是，需排除或改名 |
| `ACHIEVEMENT_RAW` | 成果 | 未能结构化，降级为 raw | 否 |
| `GRANT_NO_REDACTED` | 项目 | 项目编号被 PII 规则误伤 | 否（指纹改用题名） |

---

## 5. 开号事务（provision）

`provision.py` 是 importer 中唯一执行写操作的模块，整个流程在**单个数据库事务**内完成。

### 5.1 请求参数

```python
class PersonOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    exclude: bool = False
    login_name: str | None = None          # 手工指定，同样经过格式校验和 RESERVED 校验
    link_user_id: uuid.UUID | None = None  # 手工绑定到已有用户

class ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 传入 role 等未知字段直接返回 422
    as_of_time: date | None = None              # preview 中为 null 时必填；非 null 时可用于覆盖
    initial_password: str | None = None         # 不传则使用 import_default_password
    people: list[PersonOverride] = []
```

请求模型**不包含 `role` 字段**，并且设置了 `extra="forbid"`。调用方无论怎样构造请求，都无法影响新用户的角色。

### 5.2 执行步骤

```
1. SELECT … FROM import_jobs WHERE id = :id FOR UPDATE
   status ≠ parsed → 409 IMPORT_ALREADY_PROVISIONED / IMPORT_NOT_PARSED
2. 校验 public_base_url 已配置（OIDC 模式除外）
3. 确定 as_of_time = body.as_of_time ?? preview.as_of_time；仍为空 → 422 AS_OF_TIME_REQUIRED
   用最终的 as_of_time 重新推断项目中 status_source = inferred 的状态
4. 应用 overrides（exclude / login_name / link_user_id）
5. 重算 login_name 分配与已有用户匹配（不信任 parse 时的结果，期间可能有人注册）
   任一阻断性警告未处理 → 422，并返回对应的人员 key 列表
6. 对每个"新建"用户执行 validate_password(password, email)
   任一失败 → 422 CAIBINQE_POLICY_VIOLATION，附带失败者的 login_name 列表，整批不写入
7. 计算口令哈希（每个用户单独加盐；可以在第 1 步之前预先计算，以缩短持锁时间）
8. INSERT users：role = 'member'（硬编码字面量）、email、login_name、
   must_change_password_by = now() + initial_password_ttl_days
9. INSERT lab_people / lab_achievements / lab_projects
   … ON CONFLICT (entity_fingerprint) DO NOTHING，统计跳过数量
10. UPDATE import_jobs SET status = 'provisioned', provisioned_at, provisioned_by, stats
11. COMMIT → 渲染分发清单（§7），口令明文只出现在本次响应中
```

### 5.3 已有用户的处理

- 匹配为 `existing`，或通过 `link_user_id` 手工绑定的用户：**只关联、不修改**。`role`、`password_hash`、`must_change_password_by`、`email`、`login_name` 一律保持原样。
- `link_user_id` 指向的用户不存在，或该用户已关联其他 `lab_people` 记录 → 422 `LINK_USER_INVALID`。
- 已有用户已经关联了某条 `lab_people`，而本次又生成了一个指纹不同的人员记录：不插入新记录，计入 `stats.skipped.people_user_linked`。这样可以避免触发 `lab_people.user_id` 的唯一约束。
- 已有用户在分发清单中单列，标注"已有账号，沿用原登录方式"，不附带口令。

### 5.4 重复实体（Phase 1 规则）

- 指纹冲突一律**跳过，不覆盖**，即使新本子的 `as_of_time` 更晚也同样跳过。状态更新是 Phase 2 的职责。
- 跳过的数量写入 `stats.skipped`，按 `people`、`achievements`、`projects` 分别计数，并在结果页展示，让管理员知道"有 N 条已存在、未更新"。

### 5.5 并发与失败

- 开号期间如果有人注册或改名，占用了同一个 `login_name`：唯一约束报错 → 整个事务回滚 → 返回 409 `LOGIN_NAME_TAKEN`，附带冲突的登录名。前端提示管理员刷新预览后重试。
- 同一任务被两个管理员同时提交：第 1 步的 `FOR UPDATE` 保证只有一个请求成功，另一个返回 409。
- 任何失败都不会留下部分写入的数据。`import_jobs.status` 仍为 `parsed`，可以直接重试。`failed` 状态只用于 parse 阶段的不可恢复错误。
- 日志只记录 `job_id`、操作者 ID 和计数，不记录姓名、登录名或口令。

### 5.6 OIDC 部署模式

`auth_mode = oidc` 时：

- provision **不创建 `users` 记录**，也不生成口令。`initial_password` 参数如果传入，会被忽略并给出提示。
- 照常写入 `lab_people`（包含 `email`）、成果和项目。
- 用户首次通过 OIDC 登录时，如果其 IdP 邮箱（小写）与某条 `user_id IS NULL` 的 `lab_people.email` 一致，则自动关联。新建用户的角色同样硬编码为 `member`。
- 分发清单只包含登录地址和"请使用统一身份认证登录"的说明，不含登录名和口令。
- 本子中没有邮箱的人员无法自动关联，需要管理员在人员档案页手动绑定。

---

## 6. 登录与改密

### 6.1 登录标识解析

```python
def resolve_user(identifier: str) -> User | None:
    ident = unicodedata.normalize("NFKC", identifier).strip().lower()
    if "@" in ident:
        return db.scalar(select(User).where(User.email == ident))
    return db.scalar(select(User).where(User.login_name == ident))
```

- 登录请求体为 `{"identifier": "...", "password": "..."}`。为了兼容旧前端，保留 `email` 作为 `identifier` 的别名一个版本，下个版本移除。
- 用户不存在与口令错误**统一返回** 401 `INVALID_CREDENTIALS`。用户不存在时也执行一次哑哈希校验，使两种情况的响应耗时一致，防止通过响应时间枚举账号。
- 口令校验**通过之后**，才检查 `must_change_password_by`：
  - 值不为空且 `now() > must_change_password_by`：返回 403 `INITIAL_CAIBINQE_EXPIRED`，不签发令牌。由于此时口令已经验证通过，返回明确的错误码不会造成信息泄露。
  - 值不为空且未过期：正常签发令牌，响应中附带 `must_change_password: true` 和截止时间。

### 6.2 待改密状态的接口拦截

```python
CHANGE_PENDING_ALLOWLIST = {
    ("POST", "/api/auth/change-password"),
    ("GET",  "/api/me"),
    ("POST", "/api/auth/logout"),
}

def require_active_user(request: Request, user: User = Depends(current_user)) -> User:
    if user.must_change_password_by is not None:
        if user.must_change_password_by < now():
            raise HTTPException(403, "INITIAL_CAIBINQE_EXPIRED")
        if (request.method, request.scope["route"].path) not in CHANGE_PENDING_ALLOWLIST:
            raise HTTPException(403, "CAIBINQE_CHANGE_REQUIRED")
    return user
```

- 该依赖挂在**全局路由层**，不允许各接口自行决定是否挂载。新增接口默认就受到拦截，只有白名单中的接口例外。
- 拦截状态每次请求都从数据库读取，不写进令牌。这样管理员重置口令或用户改密之后，拦截状态立即生效。
- 令牌在有效期内、但初始口令已过期的情况同样会被拦截，令牌不能用来绕过口令有效期。

### 6.3 改密接口

`POST /api/auth/change-password`，请求体为 `{"old_password": "...", "new_password": "..."}`。

1. 校验旧口令。失败时返回 401，并计入限流。
2. 新口令须通过 `validate_password(new, user.email)` 校验，同时满足：
   - 新口令与旧口令不同；
   - 新口令不等于 `import_default_password`。
3. 写入新哈希，将 `must_change_password_by` 置为 `NULL`。
4. 如果现有代码有令牌版本号或会话撤销机制，使该用户的其他会话失效，当前会话保留。

### 6.4 管理员重置口令

`POST /api/admin/users/{id}/reset-password`，请求体可选 `{"password": "..."}`，不传时使用 `import_default_password`。

- 重置后设置 `must_change_password_by = now() + ttl`，并撤销该用户的全部会话。
- 新口令只在本次响应中返回一次。
- 管理员**不能**通过该接口重置自己的口令，自己改密走 §6.3 的接口。
- OIDC 模式下该接口返回 404。

### 6.5 登录限流

- 现有代码已实现限流的，沿用现有实现。没有的，按以下规则补上：
  - 两个计数维度：`login:id:{规范化后的 identifier}` 和 `login:ip:{客户端 IP}`；
  - 每个维度独立应用 `login_rate_limit`（默认 10 次 / 600 秒），**每次尝试都计数**，不论成功与否；
  - 超限返回 429 `RATE_LIMITED`，并带 `Retry-After` 响应头。
- 改密接口的旧口令校验共用 `login:id:` 计数。
- 部署在反向代理之后时，客户端 IP 只从配置的可信代理头中读取，防止伪造 `X-Forwarded-For` 绕过限流。

---

## 7. 分发清单

### 7.1 生成规则

- **排序**：申请人排在第一位，其余人员按在本子中出现的顺序排列。
- **排除范围**：被排除的人员和未处理的冲突人员不出现在清单中。
- **时间展示**：截止时间按 `display_tz` 显示，精确到分钟。
- **邮箱处理**：清单中**不打印邮箱原文**，T1/T3 中有邮箱的人员只标注"也可用本人邮箱登录"，以减少群聊中的信息暴露。
- **重名人员**：带后缀的登录名后面附上单位，便于本人辨认。
- **已有用户**：单列一节，不附带口令。

### 7.2 纯文本格式（可直接粘贴到微信群）

```
【LabAgent 账号已开通】
登录地址：https://lab.example.edu.cn
初始口令：TianGong@2026
⚠ 请在 2026-03-22 18:00 前登录并修改口令，逾期初始口令失效，需联系管理员重置。

郭建民　登录名：guojianmin（也可用本人邮箱登录）
万振凯　登录名：2431101262
王磊（XX大学计算机学院）　登录名：wanglei
王磊（XX大学自动化学院）　登录名：wanglei2

以下成员已有账号，请沿用原登录方式：
李明
```

- 开号请求可以带上 `include_password=false`。这时口令一行显示为"初始口令：另行私发"，适合管理员不想把口令发到群里的场景。
- 使用开源默认口令开号时，文本末尾**不会**额外提示这一点，避免在群里暴露"这是公开的默认口令"。相应的警告只在管理员的前端界面中显示（§2.4）。

### 7.3 CSV 格式

- 文件编码为 UTF-8，带 BOM，以保证 Excel 直接打开时中文不乱码。
- 列：`姓名, 登录名, 可用邮箱登录(是/否), 单位, 账号状态(新建/已有), 改密截止`。
- 当 `include_password=true` 时，额外增加一列 `初始口令`，仅新建账号的行有值。
- **防 CSV 注入**：所有以 `=`、`+`、`-`、`@`、`\t`、`\r` 开头的单元格，前面加一个 `'`。

### 7.4 口令的生命周期

- 口令明文**只出现在 provision 的响应中**，即响应体里的 `roster_text` 和 `roster_csv`，不写库、不写日志、不进缓存。
- `GET /api/admin/imports/{id}/roster` 可以在之后重新生成清单，但口令位置固定显示为"（见开号时的通知）"，CSV 中不包含口令列。
- 前端结果页需要明确提示："关闭本页后口令将无法再次查看"。

---

## 8. 接口与前端

### 8.1 接口清单

所有 `/api/admin/*` 接口要求 `role = admin`，并经过 §6.2 的待改密拦截。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/admin/imports` | multipart 上传 `.docx`。新任务返回 201，命中重复的 `parsed` 任务返回 200；两者都返回 `{job_id, preview}` |
| GET | `/api/admin/imports` | 任务列表：文件名、状态、`as_of_time`、计数、时间。不返回 preview 正文 |
| GET | `/api/admin/imports/{id}` | 任务详情，包含 preview |
| DELETE | `/api/admin/imports/{id}` | 仅限 `parsed` 状态，物理删除。`provisioned` 状态返回 409，因为 Phase 2 回填需要保留其 preview |
| POST | `/api/admin/imports/{id}/provision` | 开号，请求体见 §5.1。返回 `{stats, deadline, roster_text, roster_csv}` |
| GET | `/api/admin/imports/{id}/roster?format=text\|csv` | 重新生成清单，不含口令 |
| POST | `/api/admin/users/{id}/reset-password` | 重置口令（§6.4） |
| POST | `/api/auth/login` | `{identifier, password}` |
| POST | `/api/auth/change-password` | `{old_password, new_password}` |
| GET | `/api/me` | 新增字段 `login_name`、`must_change_password`、`must_change_password_by`，`email` 可为 `null` |

### 8.2 错误码

所有错误统一使用 `{"error": {"code": "...", "detail": {...}}}` 格式。`detail` 中只放 key、登录名和计数，不放原文。

| HTTP | 代码 | 场景 |
|---|---|---|
| 413 | `IMPORT_FILE_TOO_LARGE` | 超过 `import_max_file_mb` |
| 413 | `IMPORT_UNCOMPRESSED_TOO_LARGE` | 解压后超过上限（疑似 zip 炸弹） |
| 415 | `IMPORT_UNSUPPORTED_FORMAT` | 非 `.docx`，包括 `.doc` 和 `.docm` |
| 422 | `IMPORT_PARSE_FAILED` | 结构无法解析，或人员小节缺失 |
| 409 | `IMPORT_ALREADY_PROVISIONED` | 重复上传或重复开号，附带原 `job_id` |
| 409 | `IMPORT_NOT_PARSED` | 任务处于 `failed` 状态 |
| 422 | `AS_OF_TIME_REQUIRED` | 基准时间缺失 |
| 422 | `LOGIN_NAME_REQUIRED` / `LOGIN_NAME_INVALID` | 登录名缺失或格式非法，附带人员 key |
| 422 | `LOGIN_CONFLICT_UNRESOLVED` | 存在未处理的 T1 冲突 |
| 422 | `LINK_USER_INVALID` | 手工绑定的目标用户无效 |
| 409 | `LOGIN_NAME_TAKEN` | 开号时登录名被并发占用 |
| 422 | `CAIBINQE_POLICY_VIOLATION` | 口令未通过校验，附带失败
的规则名，不回显口令 |
| 401 | `INVALID_CREDENTIALS` | 用户不存在或口令错误（两者不区分） |
| 403 | `INITIAL_CAIBINQE_EXPIRED` | 初始口令已过改密截止时间 |
| 403 | `CAIBINQE_CHANGE_REQUIRED` | 待改密状态下访问白名单以外的接口 |
| 403 | `ADMIN_REQUIRED` | 非管理员访问 `/api/admin/*` |
| 403 | `RESET_SELF_FORBIDDEN` | 管理员试图通过重置接口修改自己的口令 |
| 404 | `NOT_FOUND` | 资源不存在，或 OIDC 模式下调用重置口令接口 |
| 429 | `RATE_LIMITED` | 登录或改密超出限流，带 `Retry-After` |

- 前端只根据 `code` 分支处理，不解析 `detail` 以外的文案。后端返回的 `message` 字段（如果有）仅供调试，不直接展示给用户。
- 新增错误码必须同步更新本表和前端的错误码映射文件，CI 中用一个测试比对两边的错误码集合是否一致。

### 8.3 前端交互

#### 8.3.1 导入列表页（`/admin/imports`）

- 表格列：文件名、状态徽标（`parsed` 灰 / `provisioned` 绿 / `failed` 红）、基准时间、人员数、新建账号数、上传时间、操作。
- 操作列：`parsed` 显示"继续开号"和"删除"；`provisioned` 显示"查看清单"（不含口令）；`failed` 只显示"删除"，悬停显示失败原因的错误码。
- 页面右上角为"上传申请书"按钮。上传前前端先检查扩展名和文件大小，不通过时直接提示，不发请求；但以后端校验为准。
- 上传返回 200（命中已存在的 `parsed` 任务）时，提示"该文件已上传过，已打开原任务"，然后跳转到原任务的预览页。返回 409 `IMPORT_ALREADY_PROVISIONED` 时，提示"该文件已完成开号"，并提供跳转到原任务的链接。

#### 8.3.2 预览与开号页（`/admin/imports/{id}`）

页面自上而下分为四块，提交按钮固定在底部：

1. **基准时间**：日期时间选择器，默认值为解析器推断的时间（如果有），并在旁边标注推断来源，例如"取自封面填表日期"。未填写时提交按钮禁用，悬停提示 `AS_OF_TIME_REQUIRED` 对应的文案。
2. **人员表**：每行一位人员，列为姓名、单位、邮箱（打码显示，如 `g***@xx.edu.cn`）、分级（T1/T2/T3）、拟用登录名、处理方式。
   - 拟用登录名可直接编辑。前端使用与后端相同的正则做即时校验，不合法时整行标红。
   - 处理方式为下拉框：新建账号 / 关联已有用户 / 排除。冲突行默认值为空，并整行高亮为橙色，直到管理员做出选择。
   - 选择"关联已有用户"时弹出用户搜索框，按登录名或邮箱前缀搜索，结果只显示登录名、打码邮箱和姓名。
   - 表头显示汇总："共 N 人，新建 A，关联 B，排除 C，待处理 D"。D > 0 时提交按钮禁用。
3. **成果与项目**：默认折叠，只显示计数和"已存在 N 条、将新增 M 条"。展开后为只读列表，Phase 1 不支持在此编辑。
4. **口令设置**：
   - 单选：使用默认口令 / 自定义口令。选择默认口令且 `import_default_password` 为开源默认值时，显示 §2.4 规定的红色警告条，且警告条不可关闭。
   - 自定义口令输入框带强度提示，失焦时调用与后端一致的规则做本地校验。
   - 复选框"在清单中包含口令"，对应 `include_password`，默认勾选。
   - 只读显示改密截止时间的预估值（按 `display_tz` 和 TTL 计算），提示以开号成功后服务器返回的时间为准。

提交前弹出确认框，列出新建账号数和改密截止时间，确认后才发请求。请求进行中按钮显示加载状态并禁止重复点击。

错误处理：

- `LOGIN_NAME_TAKEN`：提示"登录名 xxx 刚被占用，请刷新预览后重试"，并提供"刷新预览"按钮。刷新时重新拉取任务详情，已填写的处理方式和登录名尽量保留，被占用的登录名所在行标红。
- `LOGIN_NAME_INVALID` / `LOGIN_NAME_REQUIRED` / `LOGIN_CONFLICT_UNRESOLVED`：根据 `detail` 中的人员 key 滚动到对应行并高亮。
- `IMPORT_ALREADY_PROVISIONED`：说明已有其他管理员完成开号，跳转到该任务的清单页。

#### 8.3.3 开号结果页

- 顶部为统计卡片：新建、关联、已有、排除、成果新增 / 已存在、项目新增 / 已存在。
- 中部为清单文本框（只读、等宽字体），提供"复制到剪贴板"按钮，复制成功后按钮短暂变为"已复制"。
- "下载 CSV"按钮在前端用响应中的 `roster_csv` 生成 Blob 下载，文件名为 `roster-{job_id}-{yyyyMMdd}.csv`，不再向后端发请求。
- 醒目的黄色提示条："关闭本页后口令将无法再次查看"。
- 口令相关数据**只保存在组件内存状态中**，不写入 `localStorage`、`sessionStorage`、URL 或全局状态管理的持久化插件。离开页面时清空。
- 注册 `beforeunload` 提醒，并在路由守卫中二次确认："口令将无法再次查看，确定离开？"。只要复制过一次或下载过一次 CSV，就不再弹出二次确认，只保留浏览器原生的离开提醒。

#### 8.3.4 登录页

- 输入框标签改为"登录名或邮箱"，`autocomplete="username"`；口令框 `autocomplete="current-password"`。
- 收到 `INITIAL_CAIBINQE_EXPIRED` 时显示："初始口令已过期，请联系管理员重置"，不显示"忘记口令"入口（Phase 1 不提供自助找回）。
- 收到 `RATE_LIMITED` 时，根据 `Retry-After` 显示倒计时，倒计时结束前登录按钮禁用。
- OIDC 模式下隐藏账号口令表单，只显示"统一身份认证登录"按钮。

#### 8.3.5 强制改密页（`/change-password`）

- 全局路由守卫：`/api/me` 返回 `must_change_password: true` 时，除本页和登出外的所有路由都重定向到本页。前端守卫只用于提升体验，真正的拦截在后端（§6.2）。
- 页面显示截止时间和剩余时间（剩余不足 24 小时时标红）。
- 表单包含旧口令、新口令、确认新口令三个输入框。新口令框 `autocomplete="new-password"`。规则提示逐条列出，满足一条勾选一条。
- 改密成功后重新请求 `/api/me`，确认 `must_change_password` 为 `false` 后跳转到首页。
- 任意接口返回 `CAIBINQE_CHANGE_REQUIRED` 时，前端统一拦截并跳转到本页，作为路由守卫的兜底。

#### 8.3.6 用户管理页

- 用户列表新增两列：登录名、待改密状态（显示截止时间；已过期的显示红色"已过期"）。
- 每行操作中新增"重置口令"。当前登录的管理员本人所在行不显示此按钮；OIDC 模式下整列隐藏。
- 重置成功后弹窗一次性显示新口令，附带复制按钮和"关闭后无法再次查看"的提示。

---

## 9. Phase 1 实施计划

### 9.1 工作拆分

| 序号 | 工作项 | 依赖 | 预估 |
|---|---|---|---|
| 1 | 数据库迁移：`users.login_name`（部分唯一索引）、`users.email` 改为可空、`users.must_change_password_by`、`lab_people`、`import_jobs`；成果与项目表增加 `source_job_id` | — | 1 天 |
| 2 | 配置项：`auth_mode`、`import_default_password`、改密 TTL、`import_max_file_mb`、`login_rate_limit`、`display_tz`；启动时校验并输出 §2.4 的警告日志 | — | 0.5 天 |
| 3 | `.docx` 安全读取：扩展名与魔数校验、解压大小上限、拒绝宏文档、禁用外部实体 | — | 1 天 |
| 4 | 解析器：人员小节、成果、项目、基准时间推断；输出 preview 结构 | 3 | 3 天 |
| 5 | 登录名生成：拼音转换、重名后缀、学工号优先规则、与现有用户的冲突检测与分级 | 1、4 | 1.5 天 |
| 6 | 上传接口与任务管理接口（含去重、删除限制） | 1、4、5 | 1 天 |
| 7 | 开号接口：事务、`FOR UPDATE`、唯一约束冲突处理、计数统计 | 1、5、6 | 2 天 |
| 8 | 分发清单：文本与 CSV 生成、CSV 注入防护、重新生成接口 | 7 | 1 天 |
| 9 | 登录改造：标识解析、哑哈希、过期判断、`email` 别名兼容 | 1 | 1 天 |
| 10 | 全局待改密拦截依赖、改密接口、管理员重置接口、会话撤销 | 9 | 1.5 天 |
| 11 | 登录限流（如现有代码未实现）与可信代理头配置 | 9 | 1 天 |
| 12 | OIDC 模式适配：跳过建号、首次登录邮箱自动关联、人员档案手动绑定 | 7 | 1.5 天 |
| 13 | 前端：导入列表、预览开号、结果页 | 6、7、8 | 4 天 |
| 14 | 前端：登录页、强制改密页、用户管理页改造 | 9、10 | 2 天 |
| 15 | 测试补全、文档、上线演练 | 全部 | 3 天 |

合计约 25 人日。后端 1–12 与前端 13–14 可以在接口定义冻结后并行，按两人配合约 3 周完成。

### 9.2 迁移注意事项

- `users.email` 改为可空之前，先检查所有直接使用 `user.email` 的代码路径（通知、头像、展示名等），统一改为允许为空的处理方式。用一次全仓搜索加类型检查完成，并在 PR 中列出清单。
- `login_name` 的唯一索引使用部分索引 `WHERE login_name IS NOT NULL`，存量用户的 `login_name` 保持为空，继续用邮箱登录。Phase 1 不为存量用户批量生成登录名。
- 迁移脚本必须可回滚。回滚时如果存在 `email IS NULL` 的用户，回滚脚本拒绝执行并给出提示，不静默删除数据。
- 成果和项目表新增的 `source_job_id` 可为空，存量数据保持为空。这一列是为 Phase 2 回填预留的，Phase 1 只写入、不读取。

### 9.3 测试要求

**单元测试**

- 解析器：至少准备 6 份脱敏样例本子，覆盖完整格式、缺少邮箱、重名、表格合并单元格、人员小节缺失、手工改过样式的文档。
- 登录名：多音字、复姓、生僻字（拼音库缺字时的回退规则）、重名三人以上的后缀顺序、学工号格式。
- CSV：所有危险前缀字符均被转义；BOM 存在；中文在 Excel 和 WPS 中打开正常（手工验证一次并截图存档）。
- 口令校验：§6.3 的每一条规则各有一个正例和一个反例。

**集成测试**

- 开号事务：模拟在步骤中途抛出唯一约束冲突，断言数据库中没有任何部分写入，任务状态仍为 `parsed`。
- 并发：两个请求同时开号同一任务，断言只有一个成功、另一个收到 409。
- 登录：用户不存在与口令错误的响应码、响应体完全一致；两者耗时的中位数差异小于 20%。
- 过期：把 `must_change_password_by` 设为过去时间，断言登录返回 403，并且已签发的令牌访问任何接口也返回 403。

**安全测试**

- **拦截覆盖率测试**：遍历应用中注册的全部路由，对每个不在白名单中的路由，用一个待改密用户的令牌发起请求，断言返回 `CAIBINQE_CHANGE_REQUIRED`。新增路由自动纳入，无需手工维护测试用例。
- zip 炸弹、`.docm`、改了扩展名的 `.doc`、包含外部实体引用的文档，均被拒绝且不产生 `failed` 以外的副作用。
- 日志审计：开号全流程跑完后扫描日志输出，断言其中不出现样例中的任何姓名、登录名、邮箱和口令。
- 限流：伪造 `X-Forwarded-For` 不能绕过 IP 维度计数（在配置了可信代理与未配置两种情况下分别验证）。

**端到端测试**

- 上传 → 预览 → 处理冲突 → 开号 → 复制清单 → 用新账号登录 → 被强制改密 → 改密成功 → 正常访问。
- OIDC 模式下同样走一遍，验证邮箱自动关联。

### 9.4 上线与回滚

1. 增加功能开关 `import_enabled`，默认关闭。关闭时导入相关接口返回 404，前端隐藏入口。登录改造与待改密拦截不受开关控制，随版本直接生效。
2. 先在预发环境用真实本子（经本人同意）完整演练一次，确认清单格式在微信中的显示效果。
3. 生产环境上线后，由一名管理员先用一份小本子（3–5 人）试开号，确认无误后再全面开放。
4. 出现问题时先关闭功能开关。已开出的账号不受影响，无需回滚数据；确需回滚版本时，按 §9.2 的规则执行迁移回滚。

### 9.5 验收标准

- 管理员从上传到拿到可粘贴的清单，在无冲突的情况下不超过 5 次点击。
- 本子中的所有人员都能以清单上的登录名和初始口令登录，并在改密后正常使用。
- 过了改密截止时间未改密的账号无法登录，也无法用旧令牌访问任何接口。
- 口令明文不出现在数据库、日志、缓存和浏览器持久化存储中。
- §9.3 中的全部测试通过，拦截覆盖率测试为 100%。

---

## 10. Phase 2：版本与时间倒退裁决

### 10.1 问题

Phase 1 对已存在的人员、成果和项目一律"不更新"，只统计数量。这样做是安全的，但实验室会持续有新本子导入，人员的职称、单位会变化，项目会从"在研"变为"结题"。只增不改，数据会越来越旧。

另一方面，"后导入"不等于"更新"。管理员完全可能在今年导入一份三年前的旧本子来补录历史成果。如果简单地用后导入的数据覆盖先导入的，就会出现"时间倒退"：一位已经是教授的老师，被旧本子改回副教授。

Phase 2 要解决的就是：**多份本子描述同一实体时，以哪份为准；以及如何让管理员看清并控制这一过程。**

### 10.2 基本原则

1. **以基准时间排序，而不是以导入时间排序。** 每份本子的 `as_of_time` 代表"这份数据描述的是哪个时间点的事实"，裁决以此为准。这也是 Phase 1 强制要求填写 `as_of_time` 的原因。
2. **人工修改优先于导入。** 管理员或本人在档案页手工改过的字段，任何导入都不会自动覆盖，只会在预览中提示存在差异。
3. **缺席不等于删除。** 本子只是选取部分成果和项目来写，新本子里没有出现的记录不代表应当删除。Phase 2 的导入永远不删除数据。
4. **旧数据可以填空，不能覆盖。** 基准时间更早的导入，只能填补当前为空的字段，不能修改已有值。
5. **账号数据不受影响。** Phase 2 的合并只作用于 `lab_people`、成果和项目，不修改 `users` 中的登录名、邮箱和口令。`lab_people.email` 变化时不会自动同步到 `users.email`。

### 10.3 数据模型

新增字段来源表，记录每个字段的每一次取值及其出处：

```sql
CREATE TABLE field_provenance (
    id             BIGSERIAL PRIMARY KEY,
    entity_type    TEXT        NOT NULL,   -- 'person' | 'output' | 'project'
    entity_id      BIGINT      NOT NULL,
    field          TEXT        NOT NULL,
    value          JSONB,                  -- NULL 表示该来源明确给出"空"
    source_kind    TEXT        NOT NULL,   -- 'import' | 'manual' | 'legacy'
    source_job_id  BIGINT      NULL REFERENCES import_jobs(id),
    as_of_time     TIMESTAMPTZ NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by    BIGINT      NULL REFERENCES users(id),
    superseded     BOOLEAN     NOT NULL DEFAULT false,
    CHECK (source_kind <> 'import' OR source_job_id IS NOT NULL)
);

CREATE INDEX ON field_provenance (entity_type, entity_id, field);
CREATE INDEX ON field_provenance (source_job_id);
```

- 实体表（`lab_people`、成果表、项目表）中保存的仍是**当前生效值**，读取路径不变，不需要每次查询都去计算来源。
- `field_provenance` 只在合并、撤销和审计时使用。
- `import_jobs.status` 新增 `merged` 状态，表示该任务的数据已按 Phase 2 规则合并。

### 10.4 字段分类与裁决规则

| 类别 | 示例字段 | 裁决规则 |
|---|---|---|
| 状态类 | 职称、职务、单位、研究方向、邮箱、项目状态、项目经费 | 取 `as_of_time` 最新的非空值 |
| 累积类 | 成果列表、项目列表 | 取并集，按去重键合并；合并后的记录内部字段按状态类处理 |
| 身份类 | 姓名、学工号 | 不自动修改，出现差异时只提示，由管理员手工处理 |

**生效值的计算**（对单个字段）：

```python
def effective_value(entries: list[Provenance]) -> Provenance | None:
    live = [e for e in entries if not e.superseded]
    manual = [e for e in live if e.source_kind == "manual"]
    if manual:
        return max(manual, key=lambda e: e.recorded_at)
    candidates = [e for e in live if e.value is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda e: (e.as_of_time, e.recorded_at))
```

- 人工值始终优先，直到管理员在档案页点击"恢复为导入值"，这一操作会将该字段的人工记录标记为 `superseded`。
- 同一字段在**相同 `as_of_time`** 下出现两个不同的导入值时，不自动选择，而是作为冲突交给管理员裁决（§10.5）。
- `legacy` 来源（见 §10.7）参与排序，但其 `as_of_time` 取记录的 `updated_at`，通常会被任何真实的导入值取代。

**去重键**：

- 成果：有 DOI 的按 DOI（小写）去重；否则按"规范化标题 + 年份"去重。规范化包括 NFKC、去除首尾空白与标点、全角转半角、转小写。
- 项目：有项目编号的按"资助机构 + 项目编号"去重；否则按"规范化项目名称 + 起始年份"去重。
- 去重键不确定的（例如标题相似度高于阈值但不完全一致），列为"疑似重复"交给管理员确认，不自动合并。

### 10.5 合并预览

新增接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/admin/imports/{id}/merge-preview` | 计算合并结果，不写库。适用于 `parsed` 和 `provisioned` 状态的任务 |
| POST | `/api/admin/imports/{id}/merge` | 按管理员的裁决执行合并，请求体为 `{decisions: [...]}` |
| POST | `/api/admin/imports/{id}/revert` | 撤销该任务的合并（§10.6） |

合并预览把每个字段的变化归入以下五类，前端分组展示：

| 类别 | 含义 | 默认处理 |
|---|---|---|
| 新增 | 实体或字段当前不存在 | 写入 |
| 更新 | 本次 `as_of_time` 晚于当前生效值 | 写入 |
| 填空 | 本次 `as_of_time` 更早，但当前值为空 | 写入 |
| 倒退 | 本次 `as_of_time` 更早，且当前已有不同的值 | **不写入**，仅记录来源 |
| 人工保护 | 当前生效值来自人工修改，且与本次值不同 | **不写入**，仅记录来源 |
| 同时间冲突 | 与已有导入值 `as_of_time` 相同但取值不同 | **必须裁决**，未处理时不能提交 |

- "倒退"和"人工保护"两类，管理员可以逐条勾选"强制采用本次值"。强制采用会写入一条 `source_kind = 'manual'`、`recorded_by` 为当前管理员的来源记录，从而在后续裁决中具有人工值的优先级。这样处理之后，强制操作是留痕的，也不会被下一份更新的本子意外覆盖。
- 预览页顶部汇总显示："新增 A、更新 B、填空 C、倒退 D（已忽略）、人工保护 E（已忽略）、待裁决 F"。
- 每条"倒退"都显示两个值和各自的来源本子与基准时间，例如"副教授（2023-05 本子）→ 当前：教授（2025-09 本子）"，让管理员一眼看出为什么被忽略。

### 10.6 撤销

- `revert` 将该任务产生的全部来源记录标记为 `superseded`，然后对涉及的每个字段重新计算生效值并写回实体表。
- 由该任务**新建**的成果和项目，如果没有其他来源记录支撑，则一并删除；有其他来源的保留。
- 由该任务新建的 `lab_people` 记录如果已关联 `users`，不删除，只回退字段值。撤销合并不影响账号。
- 撤销本身在同一事务中完成，并写一条审计日志（只记录 `job_id`、操作者和受影响的计数）。撤销后任务状态回到 `provisioned`（如果之前开过号）或 `parsed`。

### 10.7 存量数据回填

Phase 2 上线时需要为已有数据补建来源记录，按以下顺序执行一次性回填脚本：

1. 按 `as_of_time` 升序遍历所有 `provisioned` 状态的任务，用其保存的 preview 生成 `source_kind = 'import'` 的来源记录。这也是 §8.1 禁止删除 `provisioned` 任务的原因。
2. 对每个实体字段，如果当前值无法与任何导入来源对应（说明曾被手工修改，或在 Phase 1 之前就已存在），生成一条 `source_kind = 'legacy'` 的来源记录，`as_of_time` 取该记录的 `updated_at`。
3. 回填脚本**只写来源表，不修改实体表**。写完后对每个字段计算一次生效值，与实体表当前值比对，差异项输出为报告，由管理员逐项确认后再决定是否应用。
4. Phase 1 中写入的 `source_job_id` 用于加速第 1 步中实体与任务的对应；为空的记录按去重键匹配。

回填脚本可重复执行：执行前先删除所有 `source_kind IN ('import', 'legacy')` 且 `recorded_by IS NULL` 的来源记录。

### 10.8 人工编辑的接入

- 档案页、成果编辑页、项目编辑页的保存接口，在更新实体表的同时写一条 `source_kind = 'manual'` 的来源记录，`as_of_time` 取 `now()`。
- 本人编辑与管理员编辑同样视为人工值，通过 `recorded_by` 区分。
- 档案页对每个字段显示一个小图标，悬停查看当前值来自哪份本子或谁的手工修改，以及历史取值列表。

### 10.9 待定问题

1. **邮箱变化的联动**：`lab_people.email` 被新本子更新后，是否要提示管理员同步 `users.email`？倾向于只提示、不自动同步，因为邮箱是登录凭据之一，自动修改有账号接管风险。
2. **单位名称的规范化**：同一单位在不同本子中写法不一（"XX大学计算机学院"与"XX大学计算机科学与技术学院"），会被识别为"更新"。是否需要维护单位别名表，留待收集真实数据后再定。
3. **疑似重复的阈值**：标题相似度阈值暂定 0.9，需要用真实数据校准误报率。
4. **基准时间粒度**：目前精确到分钟，实际上本子的基准时间往往只能精确到月。同月多份本子很容易触发"同时间冲突"，是否在比较时降低到按日或按月，需要在试运行中观察冲突频率后决定。
5. **跨实验室数据**：同一位合作者出现在多个实验室的本子中时，各实验室的 `lab_people` 记录相互独立，Phase 2 不做跨实验室合并。

Phase 2 的排期在 Phase 1 上线并稳定运行至少一个月、积累到不少于 5 份真实导入之后，再根据实际的冲突分布细化本章设计并另行评审。

---

*（全文完）*

---

# LabAgent Spec v2.1 —— 课题组信息全量提取、结构化入库、看板展示与 RAG 问答复用

> **版本**：v2.1（基于 v2.0 增补）
> **状态**：Draft → Review
> **影响范围**：§3.2 数据模型（修订）、§4 冷启动导入流水线（扩充）、§7A 课题组资料库看板（新增）、§8 API 清单（扩展）、§9 Phase 1 实施计划（扩展）
> **核心目标**：上传一份旧申报书后，完成“解析 → 预览校对 → 开号入库 → 看板可用 → Chat 可问”五步闭环。成员首次登录即可看到并复用课题组的全部家底。

---

## 0. v2.1 变更摘要（Changelog）

| # | 变更 | 章节 | 类型 |
|---|---|---|---|
| C1 | `lab_people` 增加 `research_specialty` / `admin_duty` / `tutor_role` / `academic_resume` | §3.2 | 修订 |
| C2 | `lab_projects` 增加 `pi_name` / `relationship_note` | §3.2 | 修订 |
| C3 | `lab_achievements` 增加 `first_author` / `support_note`；`kind` 扩充 `report`、`exhibition` | §3.2 | 修订 |
| C4 | 新增 `lab_facilities`（实验平台、算力与设备条件） | §3.2 | 新增 |
| C5 | 采用“确定性规则 + 脱敏后 LLM 结构化提取”双模流水线；`LLM_PROVIDER=fake` 时自动降级为正则状态机 | §4 | 扩充 |
| C6 | 给出真实样本的 `preview` JSON 基线（10 位成员 / 5 项成果 / 6 项项目） | §4.8 | 新增 |
| C7 | 新增 `/lab` 课题组资料库看板（四个 Tab），每项配“一键复制申报书标准格式” | §7A | 新增 |
| C8 | provision 时自动生成 `lab-profile-{import_id}` 文档并写入 RAG；新增 `query_lab_assets` 工具和 Agent 路由 | §7A.4 | 新增 |
| C9 | API 清单与 Phase 1 计划表同步更新 | §8 / §9 | 扩展 |

**设计原则（v2.1 新增，优先级高于实现便利性）**

1. **PII 不出边界**：身份证号、手机号在解析阶段即从内存中剥离。这些数据不入库、不进 preview、不发送给 LLM、不写日志。
2. **能用规则的不用 LLM**：表格类 KV 数据全部用确定性规则解析，LLM 只处理自由长文本。
3. **双模等价**：`fake`（离线 CI）与在线 LLM 两种模式输出同一个 Pydantic Schema，下游代码无感知。
4. **单一格式源**：看板“复制”、RAG 文档渲染、未来的申报书生成，共用同一套 `formatters.py`。
5. **结构化优先、向量兜底**：精确问题（如“谁主持 AGE240005”）走 SQL；模糊问题（如“谁的专长和安全相关”）走向量检索。两条路径的回答都必须带引用。

---

## 3.2 课题组数据模型（修订）

### 3.2.0 ER 概览

```
lab_imports (1) ──< lab_people
     │       ──< lab_projects      (pi_person_id → lab_people, 可空)
     │       ──< lab_achievements  (first_author_person_id → lab_people, 可空)
     │       ──< lab_facilities
     └── rag_sources(doc_key = "lab-profile-{import_id}")
users (1) ── (0..1) lab_people.user_id
```

所有课题组表都带 `lab_id`（租户隔离）、`import_id`（溯源）、`source_ref`（原文定位，如 `T4#3.2`）。时间戳字段统一为 `created_at` / `updated_at` / `deleted_at`（软删除）。

### 3.2.1 `lab_people`（团队成员，修订）

```sql
CREATE TABLE lab_people (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lab_id              UUID NOT NULL REFERENCES labs(id),
  import_id           UUID REFERENCES lab_imports(id),
  user_id             UUID UNIQUE REFERENCES users(id),        -- 开号后回填
  name                VARCHAR(32)  NOT NULL,
  gender              VARCHAR(4),
  birth_ym            VARCHAR(7),                               -- 'YYYY-MM'，只保留年月
  ethnicity           VARCHAR(16),
  role_in_lab         VARCHAR(16)  NOT NULL DEFAULT 'member',   -- pi | member | student
  title               VARCHAR(32),                              -- 专业职务：教授/副教授/讲师/高级工程师…
  admin_duty          VARCHAR(64),                              -- 【v2.1】行政职务
  tutor_role          VARCHAR(16),                              -- 【v2.1】担任导师：博士生导师|硕士生导师|无
  research_specialty  TEXT,                                     -- 【v2.1】研究专长
  education           VARCHAR(32),                              -- 最后学历
  degree              VARCHAR(32),                              -- 最后学位
  affiliation         VARCHAR(128),                             -- 工作单位
  task_in_project     VARCHAR(128),                             -- 本课题分工
  academic_resume     TEXT,                                     -- 【v2.1】个人学术简历（仅申请人从 T4 抽取，成员可自行补全）
  is_applicant        BOOLEAN NOT NULL DEFAULT FALSE,
  sort_order          SMALLINT NOT NULL DEFAULT 0,
  source_ref          VARCHAR(32),                              -- 'T2#R3' / 'T2#R15'
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at          TIMESTAMPTZ,
  CONSTRAINT ck_tutor_role CHECK (tutor_role IN ('博士生导师','硕士生导师','博士生/硕士生导师','无') OR tutor_role IS NULL)
);
CREATE INDEX ix_lab_people_lab ON lab_people(lab_id) WHERE deleted_at IS NULL;
```

> ⚠️ **刻意不设** `id_card`、`phone` 列。联系方式由 `users` 表在用户登录后自行填写，与导入链路物理隔离。

### 3.2.2 `lab_projects`（承担项目，修订）

```sql
CREATE TABLE lab_projects (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lab_id             UUID NOT NULL REFERENCES labs(id),
  import_id          UUID REFERENCES lab_imports(id),
  title              TEXT NOT NULL,
  grant_no           VARCHAR(64),                    -- 立项批准号：AGE240005 / TJYGZ59 / 2023JWZD17
  funder             VARCHAR(128),                   -- 项目来源/类别
  level              VARCHAR(16),                    -- national | provincial | bureau | horizontal | school
  pi_name            VARCHAR(32) NOT NULL,           -- 【v2.1】主持人姓名（原文保留）
  pi_person_id       UUID REFERENCES lab_people(id), -- 姓名匹配成功后回填
  start_year         SMALLINT,
  end_year           SMALLINT,
  funding_wan        NUMERIC(10,2),                  -- 经费（万元）
  status             VARCHAR(16) NOT NULL DEFAULT 'ongoing',  -- ongoing | completed | unknown
  relationship_note  TEXT,                           -- 【v2.1】与本课题的关系说明（来自 T4 第 4 节）
  source_ref         VARCHAR(32),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT uq_lab_grant UNIQUE (lab_id, grant_no)
);
```

**`status` 推断规则**：原文出现“已结题/结项/完成”时取 `completed`；出现“在研/在研中”时取 `ongoing`。原文没有明确说明时，若 `end_year < 当前年` 则取 `completed`，否则取 `ongoing`，并在 preview 的 `warnings` 中标记 `status_inferred`。

### 3.2.3 `lab_achievements`（前期成果，修订）

```sql
CREATE TYPE achievement_kind AS ENUM (
  'paper','book','patent','software','award','standard',
  'report',       -- 【v2.1】研究报告 / 资政报告
  'exhibition',   -- 【v2.1】成果展
  'other'
);

CREATE TABLE lab_achievements (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lab_id                  UUID NOT NULL REFERENCES labs(id),
  import_id               UUID REFERENCES lab_imports(id),
  kind                    achievement_kind NOT NULL,
  title                   TEXT NOT NULL,
  first_author            VARCHAR(32) NOT NULL,             -- 【v2.1】第一作者/第一完成人
  first_author_person_id  UUID REFERENCES lab_people(id),
  authors                 TEXT,                             -- 全部作者，原文顺序
  venue                   VARCHAR(256),                     -- 期刊/出版社/采纳单位/展会名称
  pub_year                SMALLINT,
  pub_detail              VARCHAR(64),                      -- 卷期页 / 登记号 / 批示级别
  support_note            TEXT,                             -- 【v2.1】对本课题的支撑价值说明
  source_ref              VARCHAR(32),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
```

### 3.2.4 `lab_facilities`（实验平台、算力与设备条件，新增）

```sql
CREATE TABLE lab_facilities (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lab_id          UUID NOT NULL REFERENCES labs(id),
  import_id       UUID REFERENCES lab_imports(id),
  category        VARCHAR(16) NOT NULL,      -- compute | platform | equipment | data | space
  name            VARCHAR(128) NOT NULL,     -- 名称
  spec            TEXT,                      -- 型号/配置
  value_wan       NUMERIC(10,2),             -- 经费/原值（万元）
  value_note      VARCHAR(64),               -- 原文金额表述（如“约 150 万元”）
  usage_scenario  TEXT,                      -- 用途与支撑场景
  source_ref      VARCHAR(32),               -- 'T3#P5' / 'T6#R2'
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
```

### 3.2.5 `lab_imports`（补充字段）

| 字段 | 说明 |
|---|---|
| `extraction_mode` | `rule+llm` \| `rule+regex_fsm` \| `rule+llm→regex_fallback` |
| `preview_json` | JSONB 格式的预览快照，保存用户在预览页的修改 |
| `pii_report` | JSONB，只记录剥离数量和位置（如 `T2#R4`），**不记录原值** |
| `source_sha256` | 原始文件哈希，用于幂等去重 |
| `rag_doc_key` | `lab-profile-{import_id}` |
| `status` | `uploaded → parsed → previewed → provisioned → indexed` \| `failed` |

**Alembic 迁移**：`2025xx_v21_lab_assets.py`。迁移只追加新列、可空，可在线执行。`achievement_kind` 用 `ALTER TYPE ... ADD VALUE` 扩充。

---

## 4. 冷启动导入：双模解析流水线（扩充）

### 4.1 流水线总览

```
 .docx 上传
   │
   ▼
[S0 加载] python-docx → 表格网格化（合并单元格去重）→ 段落流
   │
   ▼
[S1 定位] 按表头指纹识别 T1 封面 / T2 申请人+成员 / T3,T6 条件保障 / T4 前期基础
   │
   ├──────────────── 结构化区（确定性规则） ────────────────┐
   │ [S2a] T1 封面 6 行 KV                                   │
   │ [S2b] T2 R1–R11 申请人 KV │ R12 表头映射 │ R13–R21 成员行  │
   │ [S3 ] PII 物理剥离（身份证/手机/座机/邮箱）               │
   └─────────────────────────────────────────────────────────┘
   │
   ├──────────────── 长文本区（锚点切分 + 脱敏 + 抽取） ──────┐
   │ [S4a] T4 单格：按 1．[学术简历] 2．[前期成果] …… 切分      │
   │ [S4b] T3/T6：定位“设备/条件/平台/算力”段落                 │
   │ [S5 ] PII 脱敏 → 占位符替换                               │
   │ [S6 ] 抽取器分派：                                        │
   │        LLM_PROVIDER != fake → LLM Structured Output       │
   │        LLM_PROVIDER == fake → 正则状态机（RegexFSM）       │
   │        LLM 失败/校验不过 → 自动回落 RegexFSM               │
   │ [S7 ] Grounding 校验（字段必须能在原文中定位）             │
   └─────────────────────────────────────────────────────────┘
   │
   ▼
[S8 归并] 人名对齐（pi_name / first_author → person_id）、去重、status 推断
   │
   ▼
[S9 输出] preview JSON（PII 零残留断言）→ 预览校对页 → provision
```

模块布局：

```
backend/app/lab_import/
  docx_grid.py        # S0：表格网格化、合并单元格去重
  locator.py          # S1：表格指纹识别
  rules_cover.py      # S2a
  rules_team.py       # S2b
  pii.py              # S3/S5：识别、剥离、审计
  anchors.py          # S4：锚点切分
  schemas.py          # Pydantic 抽取 Schema（双模共用）
  extract_llm.py      # S6 在线
  extract_fsm.py      # S6 离线
  grounding.py        # S7
  merge.py            # S8
  pipeline.py         # 编排入口：build_preview(file) -> PreviewModel
```

### 4.2 S0/S1：表格网格化与定位

python-docx 遇到横向合并单元格时，会在 `row.cells` 中重复返回同一个 `_tc`。因此必须按 `id(cell._tc)` 去重，否则 KV 会错位。

```python
def grid(table) -> list[list[str]]:
    rows = []
    for r in table.rows:
        seen, cells = set(), []
        for c in r.cells:
            if id(c._tc) in seen:
                continue
            seen.add(id(c._tc))
            cells.append(normalize(c.text))   # 全角空格/NBSP→空格，去除首尾空白
        rows.append(cells)
    return rows
```

**表格指纹**：不依赖表格序号，而是依据关键词命中来识别。表格序号只作为 tie-breaker，以兼容不同年份的模板。

| 逻辑表 | 指纹（至少命中 2 项） | 实测样本位置 |
|---|---|---|
| T1 封面 | `课题名称`, `学科分类`, `课题负责人`, `填表日期` | Table 1（6 行） |
| T2 申请人+成员 | `性别`, `民族`, `行政职务`, `研究专长`, `主要参加者` | Table 2（21 行） |
| T4 前期基础 | 单格且包含 `[学术简历]` 或 `[前期成果]` | Table 4（1×1） |
| T3/T6 条件 | `条件保障`, `设备`, `实验室`, `算力`, `平台` | Table 3 / Table 6 |

### 4.3 S2：确定性规则解析

**T1 封面（6 行）**：每行格式为 `[标签, 值]`，标签归一化后映射到字段。

| 原文标签（归一化后） | 字段 | PII 处理 |
|---|---|---|
| 课题名称 | `cover.project_title` | — |
| 学科分类 | `cover.discipline` | — |
| 课题负责人 | `cover.applicant_name` | — |
| 负责人所在单位 | `cover.affiliation` | — |
| 联系电话 | **丢弃** | 计入 `pii_report.phone` |
| 填表日期 | `cover.fill_date` | — |

**T2 R1–R11 申请人 KV**：每行可能是 `K V K V K V` 交错排列。解析时按“标签词典命中”切分 KV 对，而不是按固定列号，以适应合并单元格导致的列漂移。

```python
APPLICANT_KEYS = {
  "姓名":"name", "性别":"gender", "民族":"ethnicity", "出生日期":"birth_ym",
  "行政职务":"admin_duty", "专业职务":"title", "研究专长":"research_specialty",
  "最后学历":"education", "最后学位":"degree", "担任导师":"tutor_role",
  "工作单位":"affiliation", "所在省":"province",
  "身份证号":"__PII_ID__", "联系电话":"__PII_PHONE__", "手机":"__PII_PHONE__",
  "电子信箱":"__PII_EMAIL__", "通讯地址":"__DROP__", "邮政编码":"__DROP__",
}
def parse_kv_row(cells):
    i = 0
    while i < len(cells):
        key = match_key(cells[i])            # 前缀/模糊匹配，容忍"行 政 职 务"中间的空格
        if key and i + 1 < len(cells):
            yield key, cells[i + 1]; i += 2
        else:
            i += 1
```

以 `__PII_*` 为目标的字段在此处直接 `continue`，**值不会被赋给任何变量**，只累加 `pii_report` 计数。

**T2 R12 表头 → 列映射**：根据表头文字建立 `col_idx → field` 映射（如 `姓名 / 出生年月 / 专业职务 / 研究专长 / 学历学位 / 工作单位 / 分工`），不做列号硬编码。

**T2 R13–R21 成员行**：共 9 行。全空行跳过；`姓名` 为空但其他列有值时，视为上一行续写并合并。申请人（来自 R1–R11）与 9 名成员合并后共 **10 人**。

**规则级校验**：
- `birth_ym` 统一为 `YYYY-MM`，兼容 `1975.03`、`1975年3月`、`197503`；
- `tutor_role`：原文为“博导”时取“博士生导师”，为“硕导”时取“硕士生导师”，为空或“否”时取“无”；
- `学历学位` 列拆分：“博士研究生/博士”拆为 `education=博士研究生, degree=博士`。

### 4.4 S3/S5：PII 物理剥离与脱敏

```python
ID_CARD = re.compile(r'(?<!\d)[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)')
MOBILE  = re.compile(r'(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)')
LANDLINE= re.compile(r'(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)')
EMAIL   = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
```

| 区域 | 策略 | 说明 |
|---|---|---|
| T1/T2 结构化区 | **物理剥离（Drop）** | 字段级丢弃，不保留占位符 |
| T4/T3/T6 长文本区 | **占位替换（Mask）** | `[ID_CARD]`、`[PHONE]`、`[EMAIL]`，避免破坏 LLM 上下文 |
| 身份证校验 | 18 位校验码校验 | 校验不过但格式命中时仍然替换（宁错杀），在 warning 中记 `pii_suspect` |

**零残留断言**：在 `pipeline.build_preview()` 返回前，把 preview 序列化为 JSON 后再全量跑一遍上述 4 个正则。只要有命中，就抛出 `PIILeakError`，流水线失败。这一断言同时写入 CI。

**生命周期**：原始 `.docx` 只存放在加密临时存储中（`TTL=24h`），provision 完成后立即删除。日志中不打印单元格原文，只打印 `source_ref` 和长度。

### 4.5 S4：T4 锚点切分

T4 是一个单元格内的长文本。序号可能是全角“．”、半角“.”或“、”，方括号也可能是全角“［］”。

```python
ANCHOR = re.compile(
    r'^\s*([1-4])\s*[．.、]\s*[\[［【]\s*(学术简历|前期成果|承担项目|与已承担项目的关系)\s*[\]］】]',
    re.M,
)
SECTION_KEY = {"学术简历":"resume", "前期成果":"achievements",
               "承担项目":"projects", "与已承担项目的关系":"relationship"}

def split_t4(text: str) -> dict[str, str]:
    hits = list(ANCHOR.finditer(text))
    out = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out[SECTION_KEY[m.group(2)]] = text[m.end():end].strip()
    missing = set(SECTION_KEY.values()) - out.keys()
    if missing:
        warnings.append({"code": "t4_anchor_missing", "sections": sorted(missing)})
    return out
```

T3/T6 的设备条件没有固定锚点。处理方法是：以段落为单位，用关键词（`服务器|GPU|算力|实验室|平台|设备|原值|万元`）召回候选段落，召回结果作为 `facilities` 的输入文本。

### 4.6 S6：抽取 Schema（双模共用）

```python
# backend/app/lab_import/schemas.py
from pydantic import BaseModel, Field
from typing import Literal

class Evidence(BaseModel):
    quote: str = Field(..., description="原文中支撑该条目的连续片段，≤80字，必须逐字摘录")

class ResumeOut(BaseModel):
    academic_resume: str = Field(..., description="申请人学术简历，保留原文要点，去除 PII 占位符")
    research_specialty: str | None = None

class AchievementOut(BaseModel):
    kind: Literal["paper","book","patent","software","award","standard","report","exhibition","other"]
    title: str
    first_author: str = Field(..., description="第一作者/第一完成人姓名")
    authors: str | None = None
    venue: str | None = Field(None, description="期刊/出版社/采纳单位/展会")
    pub_year: int | None = Field(None, ge=1980, le=2100)
    pub_detail: str | None = None
    support_note: str | None = Field(None, description="对本课题的支撑价值，没有则留空，禁止编造")
    evidence: Evidence

class ProjectOut(BaseModel):
    title: str
    grant_no: str | None = Field(None, description="立项批准号，逐字摘录，如 AGE240005")
    funder: str | None = None
    pi_name: str
    start_year: int | None = None
    end_year: int | None = None
    funding_wan: float | None = None
    status: Literal["ongoing","completed","unknown"] = "unknown"
    evidence: Evidence

class RelationshipOut(BaseModel):
    grant_no: str | None = None
    project_title_hint: str | None = None
    relationship_note: str

class FacilityOut(BaseModel):
    category: Literal["compute","platform","equipment","data","space"]
    name: str
    spec: str | None = None
    value_wan: float | None = None
    value_note: str | None = None
    usage_scenario: str | None = None
    evidence: Evidence

class T4Extraction(BaseModel):
    resume: ResumeOut | None
    achievements: list[AchievementOut]
    projects: list[ProjectOut]
    relationships: list[RelationshipOut]

class FacilityExtraction(BaseModel):
    facilities: list[FacilityOut]
```

### 4.7 S6/S7：抽取器分派、在线抽取、离线状态机与 Grounding

#### 4.7.1 分派逻辑

```python
# backend/app/lab_import/pipeline.py
def get_extractor(settings) -> Extractor:
    if settings.LLM_PROVIDER == "fake":
        return RegexFSMExtractor()
    return FallbackExtractor(primary=LLMStructuredExtractor(settings),
                             fallback=RegexFSMExtractor())
```

`FallbackExtractor` 的触发条件（满足任一即回落到 RegexFSM，并在 `extraction_mode` 中记录 `rule+llm→regex_fallback`）：
- LLM 调用超时或报错（2 次重试，指数退避）；
- Pydantic 校验失败；
- Grounding 通过率低于 80%。

#### 4.7.2 在线：LLM Structured Output

```python
# backend/app/lab_import/extract_llm.py
SYSTEM = """你是科研申报书信息抽取器。只抽取原文明确出现的信息；原文没有的字段一律置 null，禁止推测和补全。
立项批准号、人名、期刊名必须逐字摘录。evidence.quote 必须是原文中的连续子串。
文本中的 [ID_CARD]/[PHONE]/[EMAIL] 是脱敏占位符，忽略它们。"""

class LLMStructuredExtractor:
    def __init__(self, settings):
        self.llm = build_chat_model(settings, temperature=0)

    def extract_t4(self, sections: dict[str, str]) -> T4Extraction:
        runnable = self.llm.with_structured_output(T4Extraction, method="json_schema", strict=True)
        prompt = "\n\n".join(f"### {k}\n{v}" for k, v in sections.items())  # 已脱敏
        return runnable.invoke([("system", SYSTEM), ("user", prompt)])

    def extract_facilities(self, text: str) -> FacilityExtraction:
        runnable = self.llm.with_structured_output(FacilityExtraction, method="json_schema", strict=True)
        return runnable.invoke([("system", SYSTEM), ("user", text)])
```

> 如果 Provider 不支持 `json_schema` 模式，改用 `method="function_calling"`，由 `build_chat_model` 按 Provider 能力表自动选择。

#### 4.7.3 离线：正则状态机（RegexFSM）

状态机逐行扫描每个切分后的 section，按“编号条目开始 → 续行累积 → 条目结束”的方式组装记录，然后对每个条目套用字段正则。

```
           ┌──────── 匹配条目头 ITEM_HEAD ────────┐
           ▼                                      │
 [IDLE] ──► [IN_ITEM] ── 非条目头的续行 ──► [IN_ITEM]
                │                                 │
                └── 空行 / 下一条目头 / 文本结束 ──► emit(item) ─► [IDLE|IN_ITEM]
```

```python
ITEM_HEAD = re.compile(r'^\s*(?:[（(]?\d{1,2}[)）．.、]|\[\d{1,2}\]|[①-⑳])\s*')

# —— 承担项目 ——
GRANT_NO  = re.compile(r'(?:批准号|编号|项目号)?[:：]?\s*([A-Z]{2,8}\d{2,8}|\d{4}[A-Z]{2,6}\d{1,4}|[A-Z]{2,}-?\d{4}-?\d{2,3})')
YEARS     = re.compile(r'((?:19|20)\d{2})\s*(?:年)?\s*[-—–~至到]\s*((?:19|20)\d{2})')
FUNDING   = re.compile(r'(\d+(?:\.\d+)?)\s*万元')
STATUS    = re.compile(r'(在研|已结题|结题|已结项|结项|完成)')
PI        = re.compile(r'(?:主持人|负责人|主持)[:：]?\s*([\u4e00-\u9fa5]{2,4})|([\u4e00-\u9fa5]{2,4})\s*[（(]主持[)）]')
TITLE_QT  = re.compile(r'[《“"](.+?)[》”"]')

# —— 前期成果 ——
KIND_RULES = [
  ("report",     r'资政报告|研究报告|咨询报告|决策参考|采纳|批示'),
  ("exhibition", r'成果展|展览|展出|展会'),
  ("software",   r'软件著作权|软著|登记号'),
  ("patent",     r'专利|ZL\d+'),
  ("book",       r'出版社|专著|编著|教材'),
  ("award",      r'获.*奖|一等奖|二等奖|三等奖'),
  ("paper",      r'期刊|学报|\d{4}\s*[,，(（]\s*\d+|CSSCI|SCI|EI|核心'),
]
GBT_AUTHORS = re.compile(r'^([\u4e00-\u9fa5]{2,4}(?:[,，、][\u4e00-\u9fa5]{2,4})*)\s*[.．]')
SUPPORT     = re.compile(r'(?:为本课题|对本课题|支撑本课题|奠定)[^。；;]*[。；;]?')

def fsm_items(section: str) -> list[str]:
    items, buf = [], []
    for line in section.splitlines():
        if ITEM_HEAD.match(line):
            if buf: items.append(" ".join(buf))
            buf = [ITEM_HEAD.sub("", line).strip()]
        elif line.strip():
            if buf: buf.append(line.strip())      # 续行
        elif buf:
            items.append(" ".join(buf)); buf = []
    if buf: items.append(" ".join(buf))
    return items
```

- **关系节（relationship）**：先按句号切句，每句尝试匹配 `GRANT_NO` 或项目标题关键词；命中的句子归属到对应项目的 `relationship_note`。未命中的句子并入“总体关系说明”。
- **学术简历**：直接取 `resume` 全文（去占位符）作为 `academic_resume`；从“主要研究方向为/研究专长”句式中补抽 `research_specialty`，前提是 T2 中该字段为空。
- **设施**：对召回段落按“；/。”切分，每段匹配 `(名称)(（型号/配置）)?…(\d+万元)?…(用于…)`。

**等价性要求**：RegexFSM 必须对实测样本产出与在线模式**条目数一致**（10/5/6），且关键字段（`grant_no`、`pi_name`、`first_author`、`kind`、`status`）完全一致。该要求作为 CI golden test 强制执行（见 §9 验收）。

#### 4.7.4 S7：Grounding 校验

```python
def grounded(item, source: str) -> bool:
    src = squash(source)                                 # 去空白和全半角差异
    if squash(item.evidence.quote) not in src: return False
    for f in ("grant_no", "pi_name", "first_author"):
        v = getattr(item, f, None)
        if v and squash(v) not in src: return False      # 关键字段必须逐字出现在原文中
    return True
```

未通过校验的条目**不会被丢弃**，而是标记 `needs_review=true` 并以黄色高亮显示在预览页，由人工确认。

### 4.8 实测样本 `preview` JSON 基线

> **样本**：《教育考试专属大模型构建与安全应用研究_申请书.docx》
> **说明**：以下为回归基线结构，存放于 `tests/fixtures/lab_import/edu_exam_llm.preview.json`。身份证号和手机号已物理剥离，不出现在任何字段中。项目批准号与主持人对应关系以样本为准；部分描述性字段已缩写，以真实解析输出为最终 golden。

```json
{
  "import_id": "7f3c2a9e-4b1d-4e8a-9c61-2d5e8f0a1b37",
  "source": {
    "filename": "教育考试专属大模型构建与安全应用研究_申请书.docx",
    "sha256": "b1e4…(64 hex)",
    "tables_detected": { "T1": 1, "T2": 2, "T3": 3, "T4": 4, "T6": 6 }
  },
  "extraction_mode": "rule+llm",
  "cover": {
    "project_title": "教育考试专属大模型构建与安全应用研究",
    "discipline": "教育学·教育技术学",
    "applicant_name": "郭建民",
    "affiliation": "（申请单位）",
    "fill_date": "2025-03",
    "source_ref": "T1#R1-R6"
  },
  "members": [
    {
      "name": "郭建民", "is_applicant": true, "role_in_lab": "pi",
      "gender": "男", "birth_ym": "1972-05", "title": "教授",
      "admin_duty": "教育考试研究所所长", "tutor_role": "博士生导师",
      "research_specialty": "教育测量与评价；教育考试信息化；人工智能教育应用",
      "education": "博士研究生", "degree": "博士",
      "affiliation": "（申请单位）", "task_in_project": "课题总体设计与统筹",
      "academic_resume": "长期从事教育考试理论与技术研究……主持全国教育科学规划课题等多项……（自 T4[学术简历] 抽取，全文略）",
      "proposed_username": "guojianmin", "source_ref": "T2#R1-R11;T4#1"
    },
    {
      "name": "王秋慧", "role_in_lab": "member", "birth_ym": "1980-09", "title": "副教授",
      "admin_duty": null, "tutor_role": "硕士生导师",
      "research_specialty": "教育考试安全治理；命题质量评价",
      "education": "博士研究生", "degree": "博士",
      "task_in_project": "考试安全风险评估与治理框架",
      "proposed_username": "wangqiuhui", "source_ref": "T2#R13"
    },
    {
      "name": "徐国伟", "role_in_lab": "member", "birth_ym": "1983-11", "title": "教授",
      "admin_duty": null, "tutor_role": "硕士生导师",
      "research_specialty": "自然语言处理；大模型微调与评测",
      "education": "博士研究生", "degree": "博士",
      "task_in_project": "专属大模型训练与评测",
      "proposed_username": "xuguowei", "source_ref": "T2#R14"
    },
    {
      "name": "万振凯", "role_in_lab": "member", "birth_ym": "1978-02", "title": "高级工程师",
      "admin_duty": "信息中心主任", "tutor_role": "无",
      "research_specialty": "数据安全；隐私计算",
      "education": "硕士研究生", "degree": "硕士",
      "task_in_project": "数据脱敏与安全部署",
      "proposed_username": "wanzhenkai", "source_ref": "T2#R15"
    },
    {
      "name": "李静怡", "role_in_lab": "member", "birth_ym": "1988-06", "title": "讲师",
      "tutor_role": "无", "research_specialty": "教育数据挖掘",
      "education": "博士研究生", "degree": "博士",
      "task_in_project": "考试语料构建与标注",
      "proposed_username": "lijingyi", "source_ref": "T2#R16"
    },
    {
      "name": "张宏博", "role_in_lab": "member", "birth_ym": "1986-03", "title": "工程师",
      "tutor_role": "无", "research_specialty": "考试信息系统架构",
      "education": "硕士研究生", "degree": "硕士",
      "task_in_project": "应用系统集成",
      "proposed_username": "zhanghongbo", "source_ref": "T2#R17"
    },
    {
      "name": "刘雨桐", "role_in_lab": "member", "birth_ym": "1990-12", "title": "助理研究员",
      "tutor_role": "无", "research_specialty": "智能评卷；主观题自动评分",
      "education": "博士研究生", "degree": "博士",
      "task_in_project": "智能评卷场景验证",
      "proposed_username": "liuyutong", "source_ref": "T2#R18"
    },
    {
      "name": "赵晨阳", "role_in_lab": "student", "birth_ym": "1997-04", "title": "博士研究生",
      "tutor_role": "无", "research_specialty": "大模型对齐与安全",
      "education": "硕士研究生", "degree": "硕士",
      "task_in_project": "安全对齐实验",
      "proposed_username": "2331100847", "source_ref": "T2#R19"
    },
    {
      "name": "孙一凡", "role_in_lab": "student", "birth_ym": "2001-08", "title": "硕士研究生",
      "tutor_role": "无", "research_specialty": "检索增强生成",
      "education": "大学本科", "degree": "学士",
      "task_in_project": "知识库与 RAG 实验",
      "proposed_username": "2431101262", "source_ref": "T2#R20"
    },
    {
      "name": "周可欣", "role_in_lab": "student", "birth_ym": "2001-10", "title": "硕士研究生",
      "tutor_role": "无", "research_specialty": "教育测量",
      "education": "大学本科", "degree": "学士",
      "task_in_project": "调研与数据整理",
      "proposed_username": "2431101275", "source_ref": "T2#R21"
    }
  ],
  "achievements": [
    {
      "kind": "paper", "title": "大语言模型在教育考试中的应用风险与治理路径",
      "first_author": "郭建民", "authors": "郭建民，王秋慧，徐国伟",
      "venue": "中国考试", "pub_year": 2024, "pub_detail": "2024(3)",
      "support_note": "系统梳理大模型进入考试场景的风险谱系，为本课题安全应用框架提供理论基础。",
      "needs_review": false, "source_ref": "T4#2.1"
    },
    {
      "kind": "paper", "title": "面向主观题自动评分的大模型提示学习方法",
      "first_author": "徐国伟", "authors": "徐国伟，刘雨桐，赵晨阳",
      "venue": "计算机工程与应用", "pub_year": 2023, "pub_detail": "59(18)",
      "support_note": "验证了大模型在评卷场景的可行性，支撑本课题专属模型的评测方案设计。",
      "needs_review": false, "source_ref": "T4#2.2"
    },
    {
      "kind": "report", "title": "关于生成式人工智能进入教育考试领域的安全风险与对策建议",
      "first_author": "郭建民", "authors": "郭建民，万振凯",
      "venue": "被省级教育主管部门采纳", "pub_year": 2024, "pub_detail": "资政报告",
      "support_note": "形成政策层面的风险判断，为本课题安全应用规范研究奠定决策咨询基础。",
      "needs_review": false, "source_ref": "T4#2.3"
    },
    {
      "kind": "exhibition", "title": "智慧考试安全技术成果展",
      "first_author": "王秋慧", "authors": "王秋慧，张宏博",
      "venue": "（展会名称）", "pub_year": 2023, "pub_detail": "参展",
      "support_note": "展示考试安全技术原型，积累了场景需求与用户反馈。",
      "needs_review": false, "source_ref": "T4#2.4"
    },
    {
      "kind": "software", "title": "考试数据智能脱敏处理系统 V1.0",
      "first_author": "万振凯", "authors": "万振凯，李静怡",
      "venue": "国家版权局", "pub_year": 2022, "pub_detail": "软著登记号 2022SR0xxxxxx",
      "support_note": "可直接用于本课题训练语料的隐私脱敏环节。",
      "needs_review": false, "source_ref": "T4#2.5"
    }
  ],
  "projects": [
    {
      "title": "生成式人工智能赋能教育考试命题与评价的机制研究",
      "grant_no": "AGE240005", "funder": "全国教育科学规划课题",
      "level": "national", "pi_name": "郭建民", "pi_matched": true,
      "start_year": 2024, "end_year": 2026, "status": "ongoing",
      "relationship_note": "本课题在其机制研究基础上，进一步聚焦专属大模型的构建与安全落地，是其技术深化与应用延伸。",
      "source_ref": "T4#3.1;T4#4"
    },
    {
      "title": "教育考试数据安全治理体系研究",
      "grant_no": "TJYGZ59", "funder": "省级哲学社会科学规划课题",
      "level": "provincial", "pi_name": "王秋慧", "pi_matched": true,
      "start_year": 2022, "end_year": 2024, "status": "completed",
      "relationship_note": "其形成的数据安全治理框架将直接用于本课题的安全应用规范设计。",
      "source_ref": "T4#3.2;T4#4"
    },
    {
      "title": "面向考试场景的中文大模型微调与评测关键技术",
      "grant_no": "2023JWZD17", "funder": "省教委科研计划重点项目",
      "level": "provincial", "pi_name": "徐国伟", "pi_matched": true,
      "start_year": 2023, "end_year": 2025, "status": "ongoing",
      "relationship_note": "提供领域微调与评测的技术积累，本课题在其基础上构建专属模型。",
      "source_ref": "T4#3.3;T4#4"
    },
    {
      "title": "考试机构智能评卷系统研发",
      "grant_no": "HX2022-031", "funder": "横向委托项目",
      "level": "horizontal", "pi_name": "郭建民", "pi_matched": true,
      "start_year": 2022, "end_year": 2023, "status": "completed",
      "relationship_note": "积累真实评卷场景数据与工程经验。",
      "source_ref": "T4#3.4;T4#4"
    },
    {
      "title": "考生隐私数据脱敏与安全计算平台建设",
      "grant_no": "2021KJ108", "funder": "厅局级科技项目",
      "level": "bureau", "pi_name": "万振凯", "pi_matched": true,
      "start_year": 2021, "end_year": 2023, "status": "completed",
      "relationship_note": "平台成果直接复用于本课题的数据安全环节。",
      "source_ref": "T4#3.5;T4#4"
    },
    {
      "title": "教育考试舆情监测与风险预警研究",
      "grant_no": "JYKS2020-07", "funder": "教育考试研究专项",
      "level": "bureau", "pi_name": "郭建民", "pi_matched": true,
      "start_year": 2020, "end_year": 2022, "status": "completed",
      "relationship_note": "其风险预警指标体系为本课题安全评估提供参照。",
      "warnings": ["status_inferred"],
      "source_ref": "T4#3.6;T4#4"
    }
  ],
  "facilities": [
    {
      "category": "compute", "name": "大模型训练推理服务器",
      "spec": "8×GPU（80GB 显存）/ 1TB 内存 / 100Gb 互联",
      "value_wan": 180.0, "value_note": "原值约180万元",
      "usage_scenario": "专属大模型继续预训练、指令微调与推理服务", "source_ref": "T6#R2"
    },
    {
      "category": "platform", "name": "教育考试数据安全实验室",
      "spec": "隔离网络环境 + 数据脱敏与审计系统",
      "value_wan": null, "value_note": null,
      "usage_scenario": "敏感考试数据的隔离存储、脱敏处理与安全实验", "source_ref": "T3#P4"
    },
    {
      "category": "data", "name": "历年考试题库与评卷语料（脱敏）",
      "spec": "覆盖多学科、多题型", "value_wan": null, "value_note": null,
      "usage_scenario": "领域语料构建与模型评测基准", "source_ref": "T3#P5"
    }
  ],
  "pii_report": {
    "id_card_removed": 10,
    "phone_removed": 11,
    "email_removed": 1,
    "locations": ["T1#R5", "T2#R4", "T2#R6", "T2#R13-R21"],
    "leak_check": "passed"
  },
  "stats": { "members": 10, "achievements": 5, "projects": 6, "facilities": 3 },
  "warnings": [
    { "code": "status_inferred", "ref": "projects[5]" }
  ]
}
```

### 4.9 S8/S9：归并与 provision

**人名对齐**：`pi_name` 和 `first_author` 先与 `members[].name` 做精确匹配。未命中时剔除空格和全角字符后再匹配。仍未命中则把 `pi_person_id` 置空（外单位主持人属于合理情况），不报错。

**provision 事务**（单个数据库事务；RAG 写入作为事务提交后的后置任务）：

1. 为 10 名成员创建 `users` 账号。用户名规则：教职工用姓名全拼（重名时加数字后缀），学生用学号（预览页可编辑）。初始口令随机生成 12 位，设置 `must_change_password=true`。
2. 写入 `lab_people` / `lab_projects` / `lab_achievements` / `lab_facilities`，并回填 `user_id`、`pi_person_id`、`first_author_person_id`。
3. 生成**分发清单**，包含用户名和一次性初始口令，仅可下载一次，下载后口令明文立即销毁。
4. `lab_imports.status = provisioned`，然后投递后置任务 `index_lab_profile(import_id)`（见 §7A.4），成功后状态变为 `indexed`。
5. 删除原始 `.docx`。

---

## 7A. 课题组资料库看板 `/lab`（新增）

### 7A.1 首次登录闭环

```
分发清单账号（guojianmin / wanzhenkai / 2431101262）
   → /login → 检测 must_change_password=true → 强制跳转 /account/password
   → 改密成功（≥10位，含字母和数字，不得与初始口令相同）
   → 首页引导卡片「课题组资料库已就绪：10 位成员 · 6 项项目 · 5 项成果」→ /lab
```

后端中间件 `require_password_changed` 的行为：`must_change_password=true` 的用户访问除 `/api/auth/*` 以外的接口时，一律返回 `403 PASSWORD_CHANGE_REQUIRED`。

### 7A.2 页面结构

```
/lab  课题组资料库                                     [导出全部 ▾] [重新同步到知识库]
┌───────────┬────────────┬────────────┬────────────────┐
│ 团队成员(10)│ 承担项目(6) │ 前期成果(5) │ 设备与支撑条件(3) │
└───────────┴────────────┴────────────┴────────────────┘
 搜索框（姓名/题目/批准号/关键词）   筛选器（随 Tab 变化）   视图：卡片 | 表格
```

| Tab | 列表字段 | 筛选 | 详情抽屉 |
|---|---|---|---|
| **团队成员** | 姓名、角色徽标（PI/成员/学生）、专业职务、行政职务、导师资格、研究专长（标签化） | 角色、职称、导师资格 | 个人学术简历全文、本课题分工、主持的项目、第一完成的成果（反向关联） |
| **承担项目** | 题目、**立项批准号**（如 `AGE240005` / `TJYGZ59` / `2023JWZD17`）、来源、**主持人**、起止年、状态徽标 | **状态（在研/已结题）**、主持人、级别、年份区间 | 经费、与本课题关系说明、原文定位 |
| **前期成果** | 类型图标（论文/专著/资政报告/成果展/软著…）、题目、**第一完成人**、载体、年份 | 类型、第一完成人、年份 | 全部作者、**支撑说明**、原文定位 |
| **设备与支撑条件** | 类别、名称、型号/配置、原值（万元） | 类别 | 用途与支撑场景 |

**编辑权限**：PI 和 lab_admin 可编辑全部条目；普通成员只能编辑本人的 `lab_people` 记录（专长、简历、分工），以及本人为主持人或第一完成人的条目。每次编辑都会触发 RAG 重新同步（防抖 30 秒，见 §7A.4.3）。

### 7A.3 一键复制「申报书标准格式」

每条记录的卡片和详情抽屉中都有 `[📋 复制申报书格式]` 按钮。表头另有批量操作 `[复制所选]` / `[复制本 Tab 全部]`。批量复制时自动编号 `[1] [2] …`，可勾选“仅在研 / 仅近 5 年”。

**格式由后端唯一实现**：`backend/app/lab/formatters.py`。前端调用 `GET /api/lab/{type}/{id}/format?style=application`，拿到文本后写入剪贴板。同一套模板也被 RAG 渲染复用（§7A.4），保证看到的、复制的、问到的内容完全一致。

| 类型 | 模板 | 示例输出 |
|---|---|---|
| 成员（一行式） | `{name}，{gender}，{birth_ym:%Y年%m月}生，{title}{，admin_duty}{，tutor_role}，研究专长：{research_specialty}，本课题分工：{task}。` | 郭建民，男，1972年5月生，教授，教育考试研究所所长，博士生导师，研究专长：教育测量与评价；教育考试信息化；人工智能教育应用，本课题分工：课题总体设计与统筹。 |
| 成员（学术简历） | `{academic_resume}` 原文 | — |
| 项目 | `{pi_name}（主持）．{title}．{funder}（批准号：{grant_no}），{start_year}—{end_year}，{status_cn}。` | 郭建民（主持）．生成式人工智能赋能教育考试命题与评价的机制研究．全国教育科学规划课题（批准号：AGE240005），2024—2026，在研。 |
| 项目 + 关系 | 上一行 + `\n与本课题关系：{relationship_note}` | — |
| 论文 | GB/T 7714：`{authors}．{title}[J]．{venue}，{pub_detail}．` | 郭建民，王秋慧，徐国伟．大语言模型在教育考试中的应用风险与治理路径[J]．中国考试，2024(3)． |
| 资政报告 | `{authors}．{title}[R]．{venue}，{pub_year}．` | 郭建民，万振凯．关于生成式人工智能进入教育考试领域的安全风险与对策建议[R]．被省级教育主管部门采纳，2024． |
| 成果展 | `{authors}．{title}（成果展）．{venue}，{pub_year}．` | — |
| 软著 | `{authors}．{title}．计算机软件著作权，{pub_detail}，{pub_year}．` | — |
| 设施 | `{name}（{spec}），原值{value_wan}万元，用于{usage_scenario}。` | 大模型训练推理服务器（8×GPU（80GB 显存）/ 1TB 内存 / 100Gb 互联），原值180万元，用于专属大模型继续预训练、指令微调与推理服务。 |

可选开关 `?with_support=1`：在成果条目后追加 `（支撑价值：{support_note}）`，适用于“前期研究成果及其对本课题的支撑”栏目。

缺字段时**跳过对应片段**，不输出“None”或空括号。复制成功后显示 Toast「已复制，可直接粘贴到申报书」。

### 7A.4 RAG 与 LangGraph Agent 打通

#### 7A.4.1 provision 后自动生成课题组档案并入库

```python
# backend/app/lab/indexing.py
from app.rag.ingest import upsert_source          # 复用现有入库函数
from app.lab.formatters import render_lab_profile_md

async def index_lab_profile(db, import_id: UUID) -> None:
    imp = await repo.get_import(db, import_id)
    assets = await repo.load_lab_assets(db, imp.lab_id)   # 只取未软删的记录
    md = render_lab_profile_md(assets)                    # 与 /lab 复制共用模板
    await upsert_source(
        doc_key=f"lab-profile-{import_id}",
        collection="project",
        visibility="lab",
        lab_id=imp.lab_id,
        title=f"课题组资料库 · {assets.cover.project_title}",
        content=md,
        mime="text/markdown",
        metadata={"kind": "lab_profile", "import_id": str(import_id),
                  "generated_at": now_iso(), "stats": assets.stats()},
    )
    await repo.set_import_status(db, import_id, "indexed", rag_doc_key=f"lab-profile-{import_id}")
```

**档案 Markdown 结构**：标题层级就是切块边界。每个成员、项目、成果、设施各自成为一个 H3，使检索命中的粒度精确到条目，引用时可以定位到“项目 · AGE240005”。

```markdown
# 课题组资料库：教育考试专属大模型构建与安全应用研究
> 来源：申报书导入（import 7f3c2a9e），生成于 2025-03-xx。本档案由 LabAgent 自动生成，以 /lab 为准。

## 一、团队成员（10 人）
### 成员 · 郭建民（课题负责人）
- 专业职务：教授；行政职务：教育考试研究所所长；导师资格：博士生导师
- 研究专长：教育测量与评价；教育考试信息化；人工智能教育应用
- 本课题分工：课题总体设计与统筹
- 主持项目：AGE240005、HX2022-031、JYKS2020-07
- 学术简历：……
### 成员 · 王秋慧
……

## 二、承担项目（6 项｜在研 2 · 已结题 4）
### 项目 · AGE240005 · 生成式人工智能赋能教育考试命题与评价的机制研究
- 主持人：郭建民；来源：全国教育科学规划课题；起止：2024—2026；状态：在研
- 与本课题关系：……
- 申报书格式：郭建民（主持）．……（批准号：AGE240005），2024—2026，在研。
……

## 三、前期成果（5 项）
### 成果 · 资政报告 · 关于生成式人工智能进入教育考试领域的安全风险与对策建议
- 第一完成人：郭建民；作者：郭建民，万振凯；载体：被省级教育主管部门采纳；年份：2024
- 支撑价值：……
……

## 四、设备与支撑条件（3 项）
### 条件 · 大模型训练推理服务器
……
```

#### 7A.4.2 档案的安全约束

- 档案中**不包含**出生年月以外的个人敏感信息。渲染器调用 `pii.assert_clean(md)` 后才允许入库。
- `visibility="lab"`：检索层强制附加 `lab_id = current_user.lab_id` 过滤，跨课题组不可见。
- `doc_key` 固定。重复执行 provision 或同步时走 upsert 覆盖：旧切块按 `doc_key` 先删后写，不会产生重复向量。

#### 7A.4.3 增量同步

`lab_*` 表发生任何写操作后，会发送事件 `lab_assets_changed(lab_id)`。后台任务防抖 30 秒后重新执行 `index_lab_profile`。`/lab` 页面上的「重新同步到知识库」按钮可手动触发（`POST /api/lab/reindex`）。

#### 7A.4.4 新增工具 `query_lab_assets`（`backend/app/tools.py`）

采用双路检索：**结构化 SQL**（精确、可枚举）和**向量检索**（语义、模糊）并行执行，合并后返回带引用的证据包。

```python
# backend/app/tools.py（新增）
from typing import Literal
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

AssetType = Literal["people", "projects", "achievements", "facilities", "any"]

@tool("query_lab_assets")
async def query_lab_assets(
    question: str,
    asset_type: AssetType = "any",
    status: Literal["ongoing", "completed", "any"] = "any",
    person: str | None = None,
    grant_no: str | None = None,
    kind: str | None = None,
    limit: int = 20,
    config: RunnableConfig = None,
) -> dict:
    """查询本课题组的资产家底：成员（职称/专长/简历/导师资格）、承担项目（批准号/主持人/在研或结题）、
    前期成果（第一完成人/类型/支撑说明）、设备与支撑条件。
    适用问题如：'谁主持 AGE240005'、'在研项目有哪些'、'王秋慧的研究专长'、'我们有哪些资政报告'。"""
    ctx = config["configurable"]
    lab_id, user_id = ctx["lab_id"], ctx["user_id"]

    # 1) 结构化路径：由参数驱动，精确命中
    records = await lab_repo.search(
        lab_id=lab_id, asset_type=asset_type, status=status,
        person=person, grant_no=grant_no or extract_grant_no(question),
        kind=kind, keyword=question, limit=limit,
    )

    # 2) 语义路径：限定在课题组档案内检索
    chunks = await rag.retrieve(
        query=question, lab_id=lab_id, user_id=user_id,
        filters={"collection": "project", "doc_key_prefix": "lab-profile-"},
        top_k=6,
    )

    return {
        "records": [r.to_card() for r in records],   # 每条带 type/id/字段/申报书格式文本
        "citations": [
            {"ref": f"[L{i+1}]", "title": c.heading_path, "doc_key": c.doc_key,
             "chunk_id": c.id, "snippet": c.text[:200]}
            for i, c in enumerate(chunks)
        ],
        "deep_link": "/lab",
        "counts": await lab_repo.counts(lab_id),      # 回答“有几项/多少人”类问题
    }
```

`extract_grant_no()` 复用 §4.7.3 中的 `GRANT_NO` 正则，使“AGE240005 是谁主持的”这类问题无需模型填参也能精确命中。

#### 7A.4.5 Agent 路由注册（`backend/app/agent.py`）

```python
# backend/app/agent.py（修改）
LAB_INTENT = re.compile(
    r"课题组|团队|成员|谁主持|主持人|负责人|在研|结题|结项|前期成果|成果|资政报告|成果展|"
    r"专长|研究方向|简历|导师|博导|硕导|职称|批准号|设备|算力|服务器|平台|条件保障|"
    r"[A-Z]{2,8}\d{2,8}|\d{4}[A-Z]{2,6}\d{1,4}"
)

def route(state: AgentState) -> str:
    q = state["messages"][-1].content
    if LAB_INTENT.search(q):
        return "lab_assets"
    intent = intent_classifier(q)            # 现有 LLM 意图分类器作为兜底
    return {"lab": "lab_assets", "kb": "rag", "write": "drafting"}.get(intent, "general")

TOOLS = [search_knowledge, query_lab_assets, ...]   # 注册到工具列表

graph.add_node("lab_assets", lab_assets_node)       # 绑定 query_lab_assets 的 ReAct 子节点
graph.add_conditional_edges("router", route,
    {"lab_assets": "lab_assets", "rag": "rag", "drafting": "drafting", "general": "general"})
graph.add_edge("lab_assets", "answer")
```

**`lab_assets_node` 的回答约束**（系统提示要点）：
1. 只依据 `records` 与 `citations` 作答，不得补充工具结果之外的事实；
2. 对精确事实（批准号、主持人、状态、第一完成人）优先使用 `records`，并在句末标注引用 `[L1]`；
3. 枚举类问题完整列出全部条目，不截断，可附表格；
4. 当用户提到“写本子/申报书格式”时，直接输出 `records[].application_text`；
5. 末尾附「在资料库中查看 → /lab?tab=projects&q=AGE240005」深链接；
6. 无结果时明确回答“课题组资料库中未找到”，并建议在 `/lab` 中补录。

**示例**：

> **用户**：AGE240005 是谁主持的？现在什么状态？
> **Agent**：AGE240005「生成式人工智能赋能教育考试命题与评价的机制研究」由**郭建民**主持，来源为全国教育科学规划课题，起止 2024—2026，目前**在研** [L1]。
> 申报书格式：郭建民（主持）．生成式人工智能赋能教育考试命题与评价的机制研究．全国教育科学规划课题（批准号：AGE240005），2024—2026，在研。
> 🔗 在资料库中查看

> **用户**：我们组谁的专长跟数据安全有关？
> **Agent**：万振凯（高级工程师，研究专长：数据安全；隐私计算）[L2]；王秋慧（副教授，研究专长：教育考试安全治理）[L3]。此外，万振凯主持的 2021KJ108 项目和第一完成的软著「考试数据智能脱敏处理系统 V1.0」也与此相关 [L4]。

---

## 8. API 清单（扩展）

> 所有 `/api/lab/*` 接口均需登录，且要求 `must_change_password=false`；默认按 `current_user.lab_id` 隔离。

### 8.1 导入与开号

| Method | Path | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/lab/imports` | lab_admin | 上传 `.docx`（≤20MB），同步执行 S0–S9，返回 `import_id` 与 `preview` |
| GET | `/api/lab/imports/{id}/preview` | lab_admin | 获取预览（含 `needs_review`、`warnings`、`pii_report`） |
| PATCH | `/api/lab/imports/{id}/preview` | lab_admin | 保存人工校对（JSON Patch） |
| POST | `/api/lab/imports/{id}/reparse` | lab_admin | 重新解析，可指定 `mode=llm\|fsm` |
| POST | `/api/lab/imports/{id}/provision` | lab_admin | 开号入库，并投递 RAG 索引任务 |
| GET | `/api/lab/imports/{id}/distribution` | lab_admin | **一次性**下载分发清单（CSV），下载后口令销毁 |
| GET | `/api/lab/imports/{id}` | lab_admin | 导入状态（`parsed/provisioned/indexed/failed`） |

### 8.2 认证（扩展）

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 响应增加 `must_change_password` |
| POST | `/api/auth/change-password` | 首次改密 / 主动改密 |

### 8.3 课题组资料库

| Method | Path | 关键参数 | 说明 |
|---|---|---|---|
| GET | `/api/lab/summary` | — | 四类计数、在研/结题分布，用于首页卡片 |
| GET | `/api/lab/people` | `role`, `title`, `tutor_role`, `q` | 成员列表 |
| GET/PATCH | `/api/lab/people/{id}` | — | 详情（含反向关联）/ 编辑 |
| GET | `/api/lab/projects` | `status=ongoing\|completed`, `pi`, `level`, `year_from`, `year_to`, `q` | 项目列表 |
| GET/POST/PATCH/DELETE | `/api/lab/projects[/{id}]` | — | CRUD |
| GET | `/api/lab/achievements` | `kind`, `first_author`, `year_from`, `q` | 成果列表 |
| GET/POST/PATCH/DELETE | `/api/lab/achievements[/{id}]` | — | CRUD |
| GET | `/api/lab/facilities` | `category` | 设施列表 |
| GET/POST/PATCH/DELETE | `/api/lab/facilities[/{id}]` | — | CRUD |
| GET | `/api/lab/{type}/{id}/format` | `style=application\|resume\|with_relation`, `with_support` | 单条申报书格式文本 |
| POST | `/api/lab/format/batch` | `{type, ids[], numbered, filters}` | 批量复制文本 |
| GET | `/api/lab/export` | `format=docx\|md` | 导出全部资产 |
| POST | `/api/lab/reindex` | — | 手动重建 `lab-profile-*` RAG 文档 |

### 8.4 Chat（行为变更）

| Method | Path | 变更 |
|---|---|---|
| POST | `/api/chat/stream` | 路由新增 `lab_assets` 分支；SSE 事件 `tool_call` 可能出现 `query_lab_assets`；`citations[]` 新增 `doc_key=lab-profile-*` 来源与 `deep_link` |

---

## 9. Phase 1 实施计划（扩展）

### 9.1 任务表

| 编号 | 任务 | 模块/文件 | 依赖 | 估时 | 验收标准 |
|---|---|---|---|---|---|
| P1-21 | 数据模型迁移 v2.1 | `alembic/2025xx_v21_lab_assets.py`, `models/lab.py` | — | 1d | 新列/新表/枚举扩充可在线迁移，可回滚 |
| P1-22 | docx 网格化与表格定位 | `lab_import/docx_grid.py`, `locator.py` | P1-21 | 1d | 样本 T1/T2/T3/T4/T6 全部定位正确；合并单元格无错位 |
| P1-23 | 规则解析 T1/T2 | `rules_cover.py`, `rules_team.py` | P1-22 | 1.5d | 抽出 10 位成员，字段与 golden 一致 |
| P1-24 | PII 剥离/脱敏与零残留断言 | `pii.py` | P1-22 | 1d | preview、DB、日志、LLM 请求体四处 PII 扫描 0 命中 |
| P1-25 | T4 锚点切分 + T3/T6 召回 | `anchors.py` | P1-24 | 0.5d | 4 个锚点全部切出；兼容全/半角变体单测通过 |
| P1-26 | Pydantic Schema + LLM 抽取 | `schemas.py`, `extract_llm.py` | P1-25 | 1.5d | 在线模式下 5 项成果 / 6 项项目，关键字段全部 grounded |
| P1-27 | RegexFSM 离线抽取 | `extract_fsm.py` | P1-25 | 2d | `LLM_PROVIDER=fake` 下产出与在线模式条目数、关键字段一致 |
| P1-28 | Fallback/Grounding/归并 | `grounding.py`, `merge.py`, `pipeline.py` | P1-26, P1-27 | 1d | 人为注入 LLM 错误时自动回落；`pi_matched` 正确 |
| P1-29 | 预览校对页 | `frontend/src/pages/LabImportPreview.tsx` | P1-28 | 1.5d | `needs_review` 高亮，可编辑并保存，用户名可改 |
| P1-30 | provision + 分发清单 + 强制改密 | `lab/provision.py`, `auth/*` | P1-28 | 1.5d | 10 个账号可登录；首次登录强制改密；清单只能下载一次 |
| P1-31 | formatters 单一格式源 | `lab/formatters.py` | P1-21 | 1d | §7A.3 全部模板快照测试通过；缺字段不出现空括号或 None |
| P1-32 | `/lab` 看板四个 Tab + 筛选 + 一键复制 | `frontend/src/pages/Lab/*`, `api/lab/*` | P1-30, P1-31 | 3d | 状态筛选、批准号搜索、单条/批量复制可用；权限矩阵正确 |
| P1-33 | 档案渲染 + `upsert_source` 入库 + 增量同步 | `lab/indexing.py` | P1-30, P1-31 | 1d | provision 后状态为 `indexed`；编辑后 30 秒内重建；重复同步无重复切块 |
| P1-34 | `query_lab_assets` 工具 + Agent 路由 | `app/tools.py`, `app/agent.py` | P1-33 | 1.5d | 问答评测集达标（见 9.2） |
| P1-35 | 端到端回归与 CI | `tests/e2e/test_lab_coldstart.py` | 全部 | 1d | 离线 CI 全绿；在线 nightly 全绿 |

**合计约 21 人日**。关键路径为 P1-22 → P1-25 → P1-27 → P1-28 → P1-30 → P1-33 → P1-34。P1-31/P1-32 可与抽取链路并行。

### 9.2 验收标准（Definition of Done）

**A. 解析正确性（golden test，离线/在线双模都必须通过）**

```python
@pytest.mark.parametrize("provider", ["fake", pytest.param("online", marks=pytest.mark.nightly)])
def test_edu_exam_llm_sample(provider, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    p = build_preview(FIXTURE_DOCX)
    assert p.stats == {"members": 10, "achievements": 5, "projects": 6, "facilities": p.stats["facilities"]}
    assert {x.grant_no for x in p.projects} >= {"AGE240005", "TJYGZ59", "2023JWZD17"}
    assert {x.kind for x in p.achievements} >= {"paper", "report", "exhibition"}
    assert all(x.pi_name for x in p.projects) and all(x.first_author for x in p.achievements)
    assert_no_pii(p.model_dump_json())                      # 零残留
    assert_matches_golden(p, "edu_exam_llm.preview.json",
                          keys=["grant_no", "pi_name", "status", "first_author", "kind"])
```

**B. 安全**
- 全链路 PII 扫描（preview / DB dump / 应用日志 / LLM 请求抓包 / RAG 切块）0 命中；
- 跨 lab 访问 `/api/lab/*` 与跨 lab 检索 `lab-profile-*` 均返回空或 403；
- 原始 `.docx` 在 provision 后不可再获取。

**C. 看板可用性**
- 用 `guojianmin`、`wanzhenkai`、`2431101262` 三类账号走完“登录 → 改密 → /lab”，全程不超过 1 分钟；
- 各 Tab 复制文本与 §7A.3 模板快照逐字一致。

**D. Chat 问答评测集**（`tests/eval/lab_qa.yaml`，30 题，覆盖 7 类意图）

| 意图 | 示例 | 指标 |
|---|---|---|
| 主持人查询 | AGE240005 是谁主持的？ | 精确正确率 100% |
| 状态枚举 | 我们有哪些在研项目？ | 召回 100%，不得混入已结题项目 |
| 成员专长 | 王秋慧的研究专长是什么？ | 正确率 ≥95% |
| 语义匹配 | 谁做数据安全方向？ | Top 结果包含万振凯 |
| 成果类型 | 列出我们的资政报告和成果展 | 召回 100% |
| 格式复用 | 把徐国伟主持的项目按申报书格式列出来 | 与 formatters 输出一致 |
| 拒答 | 我们有国家社科重大项目吗？ | 明确回答“资料库中未找到”，零幻觉 |

**全局指标**：路由命中 `lab_assets` 的比例 ≥95%；带引用回答比例为 100%；P95 首字延迟 ≤2.5s。

---

> **附注**：本章节新增的 `formatters.py` 与 `lab-profile` 档案，将作为 Phase 2「申报书智能起草」中“研究基础/前期成果/条件保障”三个栏目的自动填充数据源，届时无需再改动数据模型。
