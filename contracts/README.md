# 共享契约（contracts/）

本目录是智能体 A/B/C/D 之间的**唯一接口事实来源**。由智能体 B 维护，A/C/D 审核各自相关部分。

当前状态：**v0.1 已冻结（2026-10-09，A/C/D 回执齐）**；复习卷扩展（CR008，2026-10-11）已按兼容新增并入本版。跨模块联调以本目录为准，变更走下方流程。审核与裁定记录见 `reviews/`。

## 目录结构

| 路径 | 内容 |
|---|---|
| `openapi/openapi.yaml` | OpenAPI 3.1 规范：全部 HTTP 端点、参数、响应与错误 |
| `schemas/*.schema.json` | 核心对象 JSON Schema（2020-12），OpenAPI 通过 `$ref` 引用，避免两处定义漂移 |
| `examples/` | 统一验收样例与模拟数据（两门课程、重复文件、同名新版本等场景） |
| `config-defaults.json` | 排程/任务生成的默认参数（B 与 D 冻结，服务端可配置覆盖） |
| `reviews/` | 对 A/C/D 提案的审核意见（冻结结论与重命名对账） |
| `tools/validate_contracts.py` | 契约自检：Schema 元校验、跨文件 `$ref` 解析、OpenAPI 结构检查 |
| `CHANGELOG.md` | 契约版本与变更记录 |

## 通用约定（总纲 §4.1）

1. **内部 ID**：不透明字符串（建议 `crs_`、`res_`、`asg_`、`rec_`、`tsk_`、`pln_`、`job_`、`chk_`、`fil_` 前缀 + ULID/UUID）。平台 ID 只出现在 `source_ref.external_id`，禁止混用。
2. **时间**：ISO 8601 且带偏移量（API 默认返回 UTC `Z`）；数据库统一保存 UTC；用户默认时区 `Asia/Shanghai`，渲染由客户端按用户时区完成。
3. **日期**：缺失一律 `null`，不猜测。截止类字段用 `due_at` + `due_precision`（`datetime` 精确时间 / `date` 仅日期 / `unknown` 未知）区分精度；`due_precision=unknown` 时 `due_at` 必须为 `null`。
4. **单位**：任务时长整数分钟；回放定位秒；页码、幻灯片号从 1 开始。
5. **事实来源**：`platform` / `user` / `inferred`；`inferred` 可附 `confidence`（0–1）。缺失数据不得补齐为事实。
6. **错误**：统一 `{error: {code, message, retryable, details, request_id}}`，见 `schemas/error.schema.json` 与错误码表。
7. **分页**：游标分页 `{items, next_cursor}`；`limit` 默认 50、最大 200；`cursor` 为不透明字符串，`next_cursor=null` 表示结束。
8. **长任务**：写操作与同步返回 `job`，通过 `GET /api/v1/jobs/{id}` 轮询；HTTP 请求不阻塞重任务。
9. **幂等**：`POST /api/v1/imports`、`/connections/{id}/sync`、`/tasks/{id}/feedback` 支持 `Idempotency-Key` 请求头；同用户同端点同键在 24 小时内重放返回首次结果（`409 IDEMPOTENCY_KEY_REUSED` 表示键复用但请求体不同）。平台资源唯一键至少为 (用户, 平台, 平台资源 ID)。
10. **并发控制**：`PATCH /tasks/{id}`、`PATCH /assignments/{id}` 支持可选 `If-Match: "<revision>"`；不匹配返回 `412 REVISION_MISMATCH`。
11. **计划**：预览生成候选（`status=candidate`），不改动当前生效计划；应用时校验候选 `input_revision` 与当前输入一致，否则 `409 PLAN_STALE`；撤销只影响计划安排，不回滚学校平台事实。
12. **学习状态与提交状态分离**：`LearningTask.status` 是本地学习进度；`Assignment.submission_status` 是学校平台提交状态，二者独立存储、独立更新。
13. **文件访问**：`file_id` 为受控访问标识；下载走 `GET /api/v1/files/{file_id}`，服务端执行所有权检查。服务器本地路径、平台临时签名 URL 永不进入 API 响应或普通日志。
14. **命名**：普通枚举值用小写 `snake_case`（如 `preview_only`、`auth_required`）；码表类字段（`*.code`、`reason_code`、错误码、告警码）用 `SCREAMING_SNAKE_CASE`（如 `PLAN_STALE`、`DEADLINE_PASSED`）。字段名统一小写 `snake_case`。

## 错误码表

API 层错误码（HTTP 响应）：

| code | HTTP | retryable | 说明 |
|---|---|---|---|
| `VALIDATION_FAILED` | 400 | false | 参数或请求体校验失败，`details` 含字段级原因 |
| `UNAUTHENTICATED` | 401 | false | 未认证或凭据失效 |
| `FORBIDDEN` | 403 | false | 无权限访问该资源（含跨用户访问） |
| `NOT_FOUND` | 404 | false | 资源不存在或对当前用户不可见 |
| `IDEMPOTENCY_KEY_REUSED` | 409 | false | 幂等键已用于不同请求体 |
| `REVISION_MISMATCH` | 412 | false | `If-Match` 与服务端版本不一致 |
| `PLAN_STALE` | 409 | false | 候选计划输入已过期，需重新预览 |
| `CONFLICT` | 409 | false | 其他状态冲突（如重复课程映射） |
| `FILE_TOO_LARGE` | 413 | false | 上传超出大小限制 |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | false | 文件类型不受支持 |
| `AUTH_EXPIRED` | 409 | false | 平台登录失效；**不自动重试**，按 `details.action`（user_login + entry_url）引导用户登录 |
| `UNSUPPORTED` | 409 | false | 平台/连接不支持该能力（对照 capabilities） |
| `RATE_LIMITED` | 429 | true | 请求过频（adapter 侧遵守 Retry-After） |
| `DEPENDENCY_UNAVAILABLE` | 502 | true | 上游平台/模型/存储暂不可用 |
| `INTERNAL_ERROR` | 500 | false | 服务端错误，`request_id` 用于诊断 |
| `JOB_CANCELLED` | 409 | false | 任务已取消，操作无效 |

适配器与后台任务错误码（出现在 `SyncResult.errors`、`Job.errors`、`Connection.last_error`；HTTP 映射见上行）：

| code | retryable | 语义与恢复动作 |
|---|---|---|
| `AUTH_EXPIRED` | false | 登录失效，不重试不循环；连接置 `auth_required`，等待用户登录 |
| `PERMISSION_DENIED` | false | 平台拒绝访问该项；跳过并报告，不重试 |
| `RESOURCE_NOT_FOUND` | false | 资源不存在；不据此产生删除（需显式删除信号） |
| `RATE_LIMITED` | true | 遵守 Retry-After，降速后重试 |
| `NETWORK_ERROR` | true | 有界指数退避重试（含抖动） |
| `UPSTREAM_CHANGED` | false | 平台接口形态变更/解析失败；报警、能力置 unknown、降级 |
| `UNSUPPORTED` | false | 平台不支持该能力；直接展示缺失 |
| `PARSE_FAILED` | false | 解析失败（D 侧）；保留旧产物，可重试（重新解析） |

`retryable=true` 仅表示“重试可能成功”，客户端应配合退避与任务状态查询；`AUTH_EXPIRED` 属“需用户动作”类，不得自动重试。

复习卷（出卷）错误码（出现在 `ProviderTestResult.error` 与 `Job.errors`（review_generate）；HTTP 层仍用上表）：

| code | retryable | 语义与恢复动作 |
|---|---|---|
| `PROVIDER_KEY_MISSING` | false | 未配置密钥（未发出请求，`latency_ms=null`）；引导用户填写密钥 |
| `PROVIDER_AUTH_FAILED` | false | 服务端拒绝凭据（401/403）；不自动重试，检查密钥与地址 |
| `PROVIDER_TIMEOUT` | true | 请求超时；出卷作业内有界重试（≤2 次） |
| `PROVIDER_RATE_LIMITED` | true | 429；遵守 Retry-After，作业内有界重试（≤2 次） |
| `PROVIDER_UNREACHABLE` | true | DNS/连接/TLS 失败；有界重试 + 退避 |
| `PROVIDER_HTTP_ERROR` | 按状态 | 其他非 2xx；5xx retryable=true、4xx retryable=false |
| `PROVIDER_INVALID_RESPONSE` | false | 响应非 JSON/无 choices/模型输出不可校验（含 1 轮修复后仍失败）；不自动重试 |

任何情形下响应、错误 `message/details`、日志与导出均不含密钥、请求体或模型原始输出中的敏感内容。

## 适配器边界（A → B）

适配器（`connectors/`）输出以下结构，Schema 已冻结于 `schemas/`；适配器不写业务数据库，B 负责持久化与幂等：

| 操作 | 结构 | Schema |
|---|---|---|
| `check_connection` | 连接状态与用户恢复动作 | `connection-state.schema.json` |
| `get_capabilities` | 能力三态 + 认证与限制 | `capabilities.schema.json` |
| `sync_course` | 同步信封（upserts/deletions/errors/stats） | `sync-result.schema.json` |
| `fetch_resource` | 文件句柄 / 明确拒绝原因 | `fetch-outcome.schema.json` |

同步语义保证（写入契约）：空响应与失败**不产生删除**；仅 `reason=explicit_delete_signal` 是删除事件；单项失败隔离；游标不透明；`content_hash` 格式 `sha256:<64hex>`、未知为 `null`。B → A 的触发参数与上传通道：B 提供（内部）`connection_id/course_id/mode(incremental|resync)/since_cursor/kinds[]` 调用参数；适配器暂存文件后经 `POST /api/v1/imports`（payload 携带 `source_ref`）上传，幂等键 `(platform, external_id, content_hash)`。

## 计划引擎边界（B ↔ D）

学习引擎（`packages/learning`）输出**候选**计划（`plan.schema.json`：`blocks`/`unassigned`/`warnings`/`summary`）；边界约定：

- **ID**：引擎内部用稳定匹配键（`match_key`，如 `t:{course}:{type}:{key}`）保证重复生成确定性；B 落库分配不透明主键（`tsk_`/`pln_`），并保留 `match_key` 用于重复解析对齐。
- **input_revision**：由 B 在持久化时生成（任务/可用时间/反馈的指纹）；引擎纯函数输出中的同名字段为占位。
- **原因码/告警码**：语义由 D 维护（D0 §2.3/2.4），结构冻结于 `plan.schema.json`；`*_code` 一律 `SCREAMING_SNAKE_CASE`。
- **默认参数**：`config-defaults.json`（`min_chunk_minutes=25`、`max_chunk_minutes=60`、`default_splittable=true`、任务时长估算系数）；服务端可配置覆盖，改默认值走契约变更流程。
- **篇幅换算**：`Resource.extent_value/extent_unit`（`page`/`slide`）供 D 估算学习时长；未知为 `null`（D 回退类型默认时长，B/D 不猜测）。

## 复习卷边界（B ↔ A 题库 / B ↔ D 出卷引擎，CR008）

出卷（`POST /api/v1/review-papers/generate`）由 B 统一发起模型请求；A/D 模块为**本地 Python 包**，经环境变量覆盖接缝加载，未接入时相关端点 502 `DEPENDENCY_UNAVAILABLE`（不伪造结果）：

| 方向 | 模块与接缝 | 接口（冻结） |
|---|---|---|
| A → B | `connectors.questionbanks`（`STUDY_QUESTIONBANK_MODULE` 覆盖） | `search_candidates(...)`、`fetch_candidate(...)`、`get_questionbank_capabilities()`；请求/响应结构见 `bank-search-request.schema.json` / `bank-candidate.schema.json`；候选缺答案保持 `null`，不伪造 |
| D → B | `learning.review`（`STUDY_REVIEW_ENGINE_MODULE` 覆盖），`ENGINE_VERSION="0.1.0"` | `analyze_materials(...)`、`build_blueprint(...)`、`select_bank_questions(...)`、`build_generation_request(...)`、`parse_and_validate_output(...)`、`build_repair_request(...)` |

- **材料快照**：受理时冻结（`material-snapshot.schema.json`）；`fingerprint` = sha256 over 规范化 JSON `{course_id, mode, engine_version, notes_sha256, constraints, materials:[{kind,id,version,content_hash,chunk_ids,chars}]}`，序列化固定 `sort_keys=true, separators=(",",":"), ensure_ascii=false`（UTF-8）。预检、试卷与修订历史中的 fingerprint 一致即同一输入。
- **备注原文**：逐字节随请求发送；`notes.sha256`/`notes.chars` 留痕，schema maxLength 20000，超限 400 `VALIDATION_FAILED` 不静默截断。
- **模型输出**：由 D `parse_and_validate_output` 校验；不合格最多 1 轮修复（`build_repair_request`），仍不合格以 `validation.status`（repaired/low_confidence/incomplete）如实标注，不伪造通过。
- **URL 规范化**：`base_url` 规则见 `provider-config.schema.json` 描述（去首尾空白与末尾斜杠、仅 http/https、无查询串/片段/用户信息、http 仅限本机/内网、空路径补 `/v1`、拒绝 `/chat/completions` 后缀与 `v1/v1`）；实际请求 = `base_url + /chat/completions`，不跟随重定向。
- **密钥**：仅存本地密钥存储（Windows DPAPI；`STUDY_SECRETS_DIR`，默认 `var/secrets`），不落库、不进响应/日志/导出；API 只回 `has_api_key` 布尔。

## 校验方法

- 校验 OpenAPI 语法：`python -m openapi_spec_validator contracts/openapi/openapi.yaml`（或 Spectral/Redocly CLI）。
- 契约自检：`python contracts/tools/validate_contracts.py`（Schema 元校验、跨文件 `$ref` 解析、OpenAPI 结构）。
- 校验样例数据：`examples/validate_examples.py` 用 `jsonschema` 对照 `schemas/` 校验全部样例；CI/本地均可运行。
- 后端集成测试断言真实响应符合 `schemas/`，保证“OpenAPI 与实际返回结构一致”（验收 6）。

## 变更流程（v0.1 冻结批次起：变更申请制）

1. 填写模板 `reviews/CHANGE-REQUEST.md`，提交 `reviews/YYYY-MM-DD-CRNNN-*.md`：背景与动机、变更类型（兼容新增 / 补充说明 / breaking）、影响范围（契约文件 + A/B/C/D 智能体 + 配置 + 数据）、提案内容、迁移方式、待决问题。
2. 相关智能体同步意见；涉及多方的申请**一次性汇总**，避免多次小改。
3. B 裁决并补录：**仅兼容新增**可并入当前版本；**breaking 一律推 v0.2**。补录 `schemas/`、`openapi.yaml`、样例，并在 `CHANGELOG.md` 记录：版本号、变更类型、受影响端与迁移建议。
4. A/C/D 确认后，B 更新版本标记并同步通知。

在途申请：无（CR001–CR006、CR008 均已裁定结案，见 `reviews/` 与 `CHANGELOG.md`）。
