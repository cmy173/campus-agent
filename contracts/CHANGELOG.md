# 契约变更记录

格式：版本号（日期，状态）→ 变更类型（breaking / 兼容 / 补充）→ 明细。

## v0.2.0-local — 2026-10-14（对话附件与文件成果，CR013）

- 兼容新增会话 multipart 上传、预览/下载和文件交付；TurnRequest 可选 attachment_ids。
- 文件交付采用独立副本及幂等调用；file.published 为通用成果事件，UI 与产生文件的工具分开。
- 详情见 reviews/2026-10-14-CR013-对话附件与文件成果.md；PDF/表格生成仍待专门验收。

## v0.2.0-local — 2026-10-14（运行预算与子任务控制，CR012）

- 兼容新增通用工具租约、归一化官方模型错误、自有控制地址及单独停止子任务端点。
- 校园/通用工具共用预算；子任务停止请求和实际终态分开，catalog 事件支持名称显示。
- 详情见 reviews/2026-10-14-CR012-运行预算与子任务控制.md；持续任务与调度恢复仍待验收。

## v0.2.0-local — 2026-10-13（V2 通用运行时，CR011）

- 兼容新增个人运行设置、MCP/独立搜索测试、官方模型租约端点；运行时接收真实通用工具注册表。
- 事件新增通用工具与子任务类型；原领域工具与来源信封保留。密钥本地加密保存，不回传、不写入 Harness profile。
- 详情见 reviews/2026-10-13-CR011-V2通用运行时设置.md；真实官方服务与其余 C 档能力继续验收。

## v0.2.0-local — 2026-10-13（Agent 任务与后台作业增量）

- 新增 agent_job_requests / agent_feedback_receipts 表与两项迁移，任务/回执原子提交。
- 原反馈路由复用 record_feedback 业务函数，原公开参数保持兼容；原 job 取消业务函数支持调用方事务。
- 模型工具新增任务生成/反馈及业务任务取消，集合为 16 项。

## v0.2.0-local — 2026-10-13（Agent 计划动作增量）

- 新增会话动作读取与具体动作接受入口，更新 agent-openapi.yaml。
- 新增 agent_plan_actions 表和迁移 a13f47c908de；不改变原计划接口参数。
- 计划应用/撤销与授权回执同事务提交，临时约束不写长期偏好。前端闭环仍在实施。

## v0.2.0-local — 2026-10-13（Agent 对话兼容新增，CR010）

- 新增 `/agent` 会话、幂等轮次、进度读取/取消和内部运行时桥接，独立契约见 `openapi/agent-openapi.yaml`。
- 官方 Harness SDK 负责模型与工具循环；Python 管理对象范围、模型发送同意、会话记录和每轮授权。
- 验收范围是本机合成数据；不改变原数据表，不宣称多用户安全或整体 M1 完成。

## v0.2.0-local — 2026-10-12（复习卷集成验收）

- 合入 A/D 引擎及 B/C 接线，新增模型配置、材料预检、复习卷生成/修订/导出与题库能力 API；见 CR008/CR009。
- **兼容补充**：生成与修订 POST 支持可选 `If-Match`（预检 fingerprint）；材料变化返回 `409 PREFLIGHT_FAILED`，网页必须重新预览。
- 出卷排队或执行期间修改对应模型配置/清除密钥返回 `409 CONFLICT`；worker 检查受理时的地址/模型/协议快照，变化则 `PROVIDER_CHANGED`，停止发送。
- 部分成功出卷的重试新增版本并保留旧卷；网页幂等键覆盖完整请求，修改标题或模型后可以重新提交。
- 真实本地 API 与浏览器联调使用隔离测试数据库及模拟模型，不代表外部模型服务或真实学校账号验收。

## v0.2.0-local — 2026-10-11（本地集成修复，CR007）

- **兼容补充**：平台课程发现新增可选 `term` 查询参数，智云支持 `current`／`all`；默认读取当前时间窗口内的个人课表与学习记录。当前时间窗口按每年2月/9月划分，不伪装为权威学期元数据；学在浙大不传此参数。
- **breaking**：Connection / ConnectionState 新增 `unsupported`、`service_unavailable` 枚举，封闭枚举客户端必须升级。原 v0.1 无改动的下游不能直接视为兼容。
- 新增平台课程发现、原子课程导入和按需智云字幕 API；详情见 [CR007](reviews/2026-10-11-CR007-个人使用闭环收尾.md)。
- ZIP 不再一律跳过：有限解包为独立文档资源；原包保留、状态为 skipped，子文档分别解析。无支持成员和损坏压缩包仍报告明确错误。
- 网页默认真实 API、个人数据库启动、同步后文件获取与解析接线；人工时长修正标记为 user，推断时长随解析结果刷新。
- 本版的自动测试包含合成平台响应，不等同于当前真实账号验收。

## v0.1 — 2026-10-09（**已冻结**；此后仅接受兼容新增并入本版，breaking 推 v0.2）

首版契约草案。基线为总纲 §4.1 通用约定、§4.2 核心对象、§4.4 最小 API 清单。

**对象（schemas/）**

- 核心对象全部落地为 JSON Schema 2020-12：`course`、`resource`、`resource-version`、`source-ref`、`assignment`、`recording`、`content-chunk`、`learning-task`、`availability`、`plan`（含 `plan-block`/`unassigned`/`warning`）、`job`、`error`。
- 通用枚举集中在 `common.schema.json`（`fact_source`、`due_precision`、`locator` 相关共享定义）。
- 状态枚举：`job_status`（queued/running/succeeded/partial_failed/failed/cancelled）、`parse_status`（pending/processing/ready/failed/skipped）、`transcript_status`（pending/processing/ready/unavailable/unknown/failed）、`submission_status`（unknown/not_submitted/submitted/graded）、`task_status`（pending/in_progress/completed/cancelled）、`plan_status`（candidate/active/superseded/revoked）、`connection_status`（ok/auth_required/network_error/blocked/disabled）。
- `due_at` 与 `due_precision` 配套，禁止用猜测值补全缺失截止时间。
- `locator` 支持 `page`（页码）、`slide`（幻灯片）、`timestamp`（回放秒区间）、`text`（文本偏移）。
- `resource.version` 为逻辑资源当前版本号；历史版本经 `resource-version` 对象与 `/resources/{id}/versions` 暴露；`content_chunk.resource_version` 指向解析所依据的版本。

**适配器边界对象（A3 §7 冻结，2026-10-09 审核通过）**

- 新增 `connection-state`（state: ok/auth_required/network_error/blocked + RecoveryAction）、`capabilities`（features 三态 supported/unsupported/unknown/preview_only）、`sync-result`（cursor/upserts/deletions/errors/stats 信封）、`fetch-outcome`（handle/denial 双分支）、`mapping-candidate`（结构冻结，API 推迟 v0.2）。
- 字段命名对账（A3 提议 → 契约冻结）：`bytes`→`size_bytes`、`media_type`→`mime_type`、平台侧 `updated_at`→`source_updated_at`、`a/b_course_id`→`left/right_course_id`；`transcript_status` 的 `none` 并入 `unavailable`。
- 删除语义入契约：空响应与失败**不视为删除**；仅 `reason=explicit_delete_signal` 为删除事件（A4 严格删除语义）。
- `AUTH_EXPIRED` 的可重试性修正为 **false**（对齐 A4"登录失效不重试、不循环"），恢复路径随 `details.action`（user_login + entry_url）下发。
- 适配器 upsert 载荷不含 `parse_status`（B/D 侧关注点），读取路径的 `parse_status` 枚举为 pending/processing/ready/failed/skipped。
- 审核记录：`reviews/2026-10-09-A3-审核意见.md`（§7 十二项逐条结论 + B→A 触发参数与上传通道）。

**计划引擎对账（D0 §6，2026-10-09 审核通过）**

- `plan` 新增必填 `summary`（容量/排入/锁定分钟数与任务数）；`UnassignedItem.reason` 更名 `reason_code`，枚举采用 D0 §2.3 的 7 码（封闭）；新增 `suggestion`（可空）；`Warning` 新增必填 `task_ids[]`，`code` 为开放字符串（语义由 D 维护，结构冻结）。
- `learning-task` 新增必填字段 `match_key`（可空；稳定匹配键，重复解析对齐用，非 API 主键）；`type` 枚举新增 `watching`（回放观看）。
- `resource`/`resource-version` 新增 `extent_value`/`extent_unit`（page/slide，可空，供学习时长估算）；`Resource.source_ref` 改为 `source_refs[]`（跨平台合并读模型）。
- 新增 `config-defaults.json`：排程（25/60/true）与任务生成默认参数冻结。
- 审核记录：`reviews/2026-10-09-D0-审核意见.md`（ID 映射采用 match_key 方案，D 侧 `taskgen.py` 无需改动）。

**样例（examples/，2026-10-09）**

- 新增规范样例 16 个文件：`api/`（12：courses/resources/resource-versions/assignments/recordings/tasks/plans/availability/connections/jobs/capabilities/error-responses）与 `adapter/`（4：connection-state/sync-result/fetch-outcome/mapping-candidate）。
- 覆盖总纲 §6 全部验收场景（对照表见 `examples/README.md`）；列表响应统一 `{items, next_cursor}` 信封。
- 新增 `examples/manifest.json`（样例→Schema 映射）与 `examples/validate_examples.py`：Schema 校验 + 跨文件引用一致性（ID 引用、计划块/未安排互斥、summary 与块分钟数一致、前置依赖与附件存在）。
- `Connection` 抽为 `schemas/connection.schema.json`（openapi 改 `$ref` 引用）；`last_error`/`recovery_action` 修正为可空。

**API（openapi/openapi.yaml）**

- 覆盖总纲 §4.4 全部 13 项最小 API。
- 补充端点（M1 手动闭环与验收所需，均为新增非破坏）：
  - 课程：`POST /courses`、`GET /courses/{id}`、`POST /courses/{id}/source-refs`（跨平台映射人工确认）。
  - 作业维护：`POST /assignments`、`PATCH /assignments/{id}`（`fact_source=user`）。
  - 资源：`GET /resources/{id}`、`GET /resources/{id}/versions`、`POST /resources/{id}/reparse`、`GET /files/{file_id}`（受控下载，所有权检查）。
  - 任务：`GET /tasks`、`POST /tasks`、`DELETE /tasks/{id}`（软归档，仅解除计划安排引用，不影响资源与作业记录）。
  - 计划：`GET /plans`（历史）、`GET /plans/{id}`、`POST /plans/{id}/revoke`。
  - 连接：`GET /connections`、`GET /connections/{id}/capabilities`、`POST /connections/{id}/sync`（Idempotency-Key）。
  - 作业任务：`GET /jobs/{id}`、`POST /jobs/{id}/cancel`。
  - 导出：`POST /exports`（JSON 导出，产物为受控文件，经 `GET /files/{file_id}` 下载）。
  - 健康检查：`GET /health`。

**v0.1 冻结前补录（C0 审阅答复，2026-10-09，全部为兼容新增）**

- 新增端点：`GET /resources/{id}/chunks`（片段列表，按 ordinal 升序，仅当前版本）、`GET /jobs`（任务历史，创建时间倒序，`status`/`type` 过滤）、`POST /jobs/{id}/retry`（仅 failed/partial_failed；以原参数新建任务，partial 仅重试失败条目）、`GET /connections/{id}`、`POST /connections/{id}/disable` / `enable`（用户停用/启用，幂等；首次建立连接不在 API 内，属部署/适配器流程）。
- 补录既有实现（修复契约-实现漂移）：`GET /assignments`（全局，支持 `due_before`/`due_after`/`include_archived`）、`GET /assignments/{id}`。
- `Resource` 新增必填可空 `chunk_count`（`parse_status=ready` 时为当前版本片段数，否则 `null`）。
- 样例：新增 `api/content-chunks.json`（8 条片段，4 个已解析资源全量枚举，幻灯片/页码定位）；`resources.json` 补 `chunk_count`；`jobs.json` 新增 parse 任务（片段 provenance 引用）；`validate_examples.py` 增片段一致性校验。
- 语义裁决（不改结构）：预览差异由客户端按 `task_id` 对比（不新增 `PlanBlock.change`）；「延后（defer）」v0.1 移除按钮（`due_at` 属实事实；`skipped` 已表达本轮未完成；snooze 约束推 v0.2）；任务→来源跳转按 `source_refs` 客户端匹配；Job 取消/重试按钮由 `status` 派生；`duplicate_of` 不入（去重以多 `source_refs` 表达）；块移动 `PATCH /plans/current/blocks/{id}` 不入 v0.1（v0.2 随引擎约束设计）。
- 审核记录：`reviews/2026-10-09-C0-审核意见.md`（C 审阅归档）、`reviews/2026-10-09-C0-答复.md`（B 对 E1–E7 / Q1–Q7 的逐条答复）。

**v0.1 补录（CR001 裁定，2026-10-09，兼容新增）**

- CR001（登录回跳）裁定：两平台通行证均**不可回跳**（xzzd CAS `service` 写死、zykt OAuth2 无 client 登记通道；A12 工程判定）→ 本期降级口径——**不新增授权/回调端点**；登录为「打开权威登录页（用户自行输入）→ 回工作台触发检查衔接」。
- 新增端点：`POST /connections/{id}/check`——在本地连接器适配器上执行 `check_connection`，结果透传到连接行（`status`/`last_checked_at`/`last_error`/`recovery_action`；回跳完成≠连接可用，不作假 ok）。`disabled` → 409 `CONFLICT`；适配器未配置 → 502 `DEPENDENCY_UNAVAILABLE`（不自动重试）。
- `Connection` 新增可选字段 `entry_url`：平台权威登录入口常量（xzzd `https://course.zju.edu.cn/ua/login`、zykt `https://classroom.zju.edu.cn/`；不含任何回跳参数）。
- 完整回跳通道（state 一次性 + 回跳白名单 + 回调端点）保留 A12 §2 规格；待平台侧登记完成后按新 CR 补录启用，届时 UI 与既有契约零改动。
- 审核记录：`reviews/2026-10-09-CR001-A12裁定.md`（A 裁定 DECISION-1/2/3）、`reviews/2026-10-09-CR001-登录回跳.md`（含 B 结案裁决与范围修订）。

**v0.1 补录（CR003 B13 任务生成接入，2026-10-10，兼容新增）**

- CR003：把 `learning.taskgen.build_tasks` 接入后台链路（评估报告 P0-1 整改），三处兼容新增：
  - `Job.type` 枚举新增 `build_tasks`（后台任务：由资源/作业/回放生成候选学习任务；`GET /jobs` 的 `type` 过滤枚举同步）。
  - `Job` 新增必填字段 `warnings`（数组，可为空；`{code,message,task_ids[],details}` 与 `plan.warnings` 同形）：`TaskGenResult.warnings` 的 7 个码值（DUPLICATE_CONTENT/TASK_VERSION_UPDATED/UNSUPPORTED_EXTENT/MISSING_DURATION/DUPLICATE_TASK_ID/EXISTING_TASK_UNMATCHED/HINT_UNRESOLVED）经此对用户可见（C11 展示）；与 `errors`（失败）语义分离，成功任务也可带告警；既有 job 样例同步补空数组。
  - 新增端点 `POST /tasks/build`（请求体可省略：`BuildTasksRequest.course_id` 限课程，缺省/null 为全部；响应 202 `JobAcceptedResponse`；同范围排队/执行中任务复用不重复入队）：重复触发不制造重复任务（按稳定 `match_key` 对齐既有任务，用户修正不被覆盖——D0 §6 ID 映射方案的落地）；parse 任务成功完成后按课程范围自动派发（B13 ④，不开新端点）。
- 审核记录：`reviews/2026-10-10-CR003-B13任务生成接入.md`（B 裁决 merged）。

**v0.1 补录（CR002 受控浏览器会话形态，2026-10-10，补充说明，无结构变更）**

- CR002（A14 受控浏览器 + 持久化 profile 形态下的端点影响）B 裁决：**补充说明为主，不新增 schema 结构、不新增端点**；版本号不变。
  - `POST /connections/{id}/check` 描述补充（openapi）：执行面可为本地连接器（适配器桥接或本地服务），受控浏览器与会话均在本地，本 API 仅调度并记录结果、不经手会话物料；正常路径由连接器在会话有效后自动触发（自动衔接），用户手动点击为兜底（A5 v0.2 §4.2）。
  - `Connection.entry_url` 描述补充（connection.schema.json）：双用途——主路径为连接器用同一常量打开受控浏览器登录窗口；兜底为网页端 `auth_required` 时的"前往登录"链接（常量值不变，CR001 DECISION-3）。
  - `recovery_action` 文案（无结构变更）：定稿"**登录已失效，请在连接器窗口完成登录后重试**"——B 侧默认（`sync_engine` 兜底/`recovery_action_for`）与样例（`examples/api/connections.json`、`error-responses.json`）已更新；适配器提供的 message 原样透传（A 侧文案随 A5b 对齐评估）。
  - 连接状态上报端点（原提案第 4 项）：**本期不采用**——既有 `check`（同步返回）+ `GET /connections/{id}` 已覆盖，A 侧不依赖；未来如需主动推送另行提报。
- 审核记录：`reviews/2026-10-10-CR002-受控浏览器会话形态.md`（含 B 裁决与补录记录）。

**v0.1 补录（CR004 任务详情端点补录，2026-10-10，兼容新增）**

- CR004：B16 双向对账（契约↔实现 route 集合实测）发现的唯一反向漂移——`GET /api/v1/tasks/{task_id}` 后端早已实现（`apps/api/app/routers/tasks.py`）且 C 客户端在用（`client.ts` `getTask`），契约此前仅有 `patch`/`delete`。按兼容新增补录：
  - 新增端点 `GET /tasks/{task_id}`（operationId `getTask`，summary 获取任务详情）：响应 200 `learning-task.schema.json`（全字段含 `source_refs`/`revision`/`archived_at`），401/404/500 同既有错误信封口径。
  - B 侧实现零改动（补录既有实现，修复契约-实现漂移；同 C0 补录 `GET /assignments/{id}` 先例）；C 消费方确认为在用。
- 审核记录：`reviews/2026-10-10-CR004-任务详情端点补录.md`（B 裁决 merged）。

**v0.1 补录（CR005 ICS 导出 + NEEDS_OCR 口径，2026-10-10，兼容新增 + 补充说明）**

- CR005：两项——① ICS 导出（B19，兼容新增）；② `parse_status` 的 NEEDS_OCR 口径正式关闭（B20，补充说明，无结构变更）。
  - `POST /exports` 的 `CreateExportRequest.format`：`const: json` → `enum: [json, ics]`（默认 `json`，既有调用零影响）。`ics` 导出属主 active 计划的定时块 + 任务截止为 RFC 5545 iCalendar：用户时区墙钟渲染（`DTSTART;TZID`）、`due_precision=date` 为全天事件（`DTSTART;VALUE=DATE` + 次日 `DTEND`）、`unknown` 不导出；候选计划/取消归档任务不导出；UID 稳定（`{id}@study-workbench`）幂等合并；产物为受控文件（`text/calendar`）经 `GET /files/{file_id}` 下载。
  - `include` 参数仅对 `format=json` 有效：`format=ics` 携带即 400 `VALIDATION_FAILED`（描述已写明）。
  - `parse_status` 的 NEEDS_OCR 口径（方案 ①，保留现映射）：枚举值集合不变（不新增 `needs_ocr`）；需要 OCR 的扫描件如实落 `failed` + 任务错误码 `NEEDS_OCR`（`retryable=true`）、`chunk_count=null`——openapi 资源列表查询参数与 `resource.schema.json` 描述均已显式写明。
- 审核记录：`reviews/2026-10-10-CR005-ICS导出与NEEDS_OCR口径.md`（B 裁决 merged）。

**v0.1 补录（CR006 .zip 课件包入库 + skipped 口径，2026-10-10，兼容放宽 + 补充说明）**

- CR006：两项——① `/imports` 白名单新增 `.zip`（兼容放宽）；② `parse_status=skipped` 语义覆盖 zip 资源（补充说明，无结构变更）。
  - 背景：A21/A22 受控实测真实课程资源为 `.zip` 课件包（3/3）；原白名单（pdf/pptx/docx）415 拒收，真实材料无法归档。A 提请 B 裁决（不代裁、不重命名伪造类型），B 选选项 (a) 扩白名单（ZIP 魔数与 OOXML 同规，安全面不扩大——无解包/无解析）。
  - `/imports`：`EXTENSION_MIME` 新增 `".zip": "application/zip"`；受理提示 `pdf/pptx/docx/zip`；魔数 `PK\x03\x04` 校验同 OOXML 规则；大小与其他存储约束不变。
  - `parse_status`：zip 无解析器，解析任务如实失败 `UNSUPPORTED_MEDIA_TYPE`（retryable=false，errors 留痕）；资源终态落 **`skipped`**（"该类型不解析"，不误标 failed），`chunk_count=null`、无 ContentChunk。openapi（`/imports` files 描述、资源列表 `parse_status` 查询参数）与 `resource.schema.json` 描述均已写明。
  - zip 内容解包/渲染为未来项（v0.2+），本项不得解释为"支持解析 zip"。
- 审核记录：`reviews/2026-10-10-CR006-zip课件包入库与skipped口径.md`（B 裁决 merged）。

**v0.1 补录（CR008 复习卷契约冻结，2026-10-11，兼容新增）**

- CR008：复习卷（出卷）功能后端闭环的共享契约冻结（用户本轮任务书「首先冻结共享契约及请求响应样例，供 A、C、D 并行开发」）。12 个新 schema + 2 处兼容修改 + 12 路径/16 操作 + 7 个新样例，明细见 `reviews/2026-10-11-CR008-复习卷契约冻结.md`。
  - **Provider 配置**（`provider-config.schema.json`，首版仅 OpenAI 兼容 Chat Completions）：用户自行填写 `base_url`/`model`/密钥；密钥仅存本地（DPAPI；`STUDY_SECRETS_DIR`），**永不回传网页/日志/导出**（仅 `has_api_key`）；URL 规范化规则（末尾斜杠、无查询串/片段/用户信息、空路径补 `/v1`、http 仅限本机/内网）与服务端 `POST /providers/{id}/test` 连接测试（不发送用户材料；远端失败 200+`ok=false`）。
  - **材料快照**（`material-snapshot.schema.json`）：受理时冻结课件/作业/附件正文与**备注逐字节原文**（`notes.sha256`+`chars` 留痕；maxLength 20000 超限 400 不截断）；`fingerprint` 公式冻结（sha256 + 固定 JSON 序列化）。
  - **出卷作业**：`Job.type += review_generate`、新增可选 `phase`（materials→blueprint→bank_search→generation→validation→saving）；进度/取消/幂等（Idempotency-Key）沿用既有 Job 通道；模型请求仅由 B 发起，429/超时/5xx 有界重试（≤2）、401 不重试、修复轮 ≤1。
  - **试卷与版本**（`review-paper.schema.json`/`review-paper-revision.schema.json`）：题目含答案、解析、来源（`source_refs` minItems 1）与校验状态（valid/repaired/low_confidence/incomplete）；revision 保存时递增、不覆盖历史；导出 markdown/student（不含答案）/answers 三版。
  - **题库与引擎接缝**：A `connectors.questionbanks`（`search_candidates`/`fetch_candidate`/`get_questionbank_capabilities`）、D `learning.review`（6 函数，`ENGINE_VERSION="0.1.0"`），环境变量 `STUDY_QUESTIONBANK_MODULE`/`STUDY_REVIEW_ENGINE_MODULE` 覆盖；未接入 502 `DEPENDENCY_UNAVAILABLE`；bank/hybrid 无来源 400 `BANK_NOT_CONFIGURED`（不假装搜到题、不静默切 ai）。
  - **错误码**：新增 `PROVIDER_*` 7 码表（README 已补）；`examples/` 新增 7 样例、`validate_examples.py` 增复习卷交叉校验（当前全绿：24 文件 / 78 对象）。
- 审核记录：`reviews/2026-10-11-CR008-复习卷契约冻结.md`（B 裁决 merged）。

**v0.1 补录（CR009 review-question 来源引用枚举增补 note，2026-10-11，兼容新增）**

- CR009：#47 出卷流水线实现期发现的冻结遗漏——`review-question.schema.json` 的 `source_refs[].kind` 枚举缺 `note`，而备注（逐字节原文）是允许的题目依据来源（`paper-blueprint` 的知识点 evidence 早有 `kind: "note"`；D 引擎 `QuestionSourceRef` 含 `notes_line`）。若不扩枚举只能丢弃备注出处，违反「来源如实」。
  - `source_refs[].kind`：`enum: [resource, assignment, web, note]`（兼容加值）；`note` 形状：`locator="备注第N行"`（N 在备注行数内），`resource_id/chunk_id/assignment_id/url/site_name` 全 `null`；语义=题目依据用户备注原文行，不得伪造材料来源。
  - 样例 `api/review-paper.json`：qst_7c1a03 增一条 `note` 出处（示范"同一题可同时有材料出处与备注出处"）；`validate_examples.py` 出处校验显式分支（note 行号范围+字段为空、web 显式、未知类型报错）——全绿不变（24 文件 / 78 对象）。
  - 消费面：C 按通用 locator 渲染即零改动；A/D 无影响（D 的 `notes` 来源契约不变，B 适配层映射）。
- 审核记录：`reviews/2026-10-11-CR009-review-question来源引用note枚举.md`（B 裁决 merged）。

**变更流程（2026-10-09 起）**

- **冻结状态**：v0.1 已关闭冻结（2026-10-09）——三方确认回执齐：A `reviews/2026-10-09-A-契约v0.1确认回执.md`（无条件接受）、C `reviews/2026-10-09-C1-确认回执.md`（接受为冻结基线）、D `reviews/2026-10-09-D1-确认回执.md`（同意冻结）；本版内 C0 补录与 CR001 补录均按「兼容新增并入当前版本」处理。
- 冻结批次引入变更申请制：模板 `reviews/CHANGE-REQUEST.md`，含"影响范围 + 迁移方式"栏位；冻结后仅接受兼容新增（并入当前版本），breaking 一律推 v0.2。
- 首个申请 CR001 已裁定结案（本期降级：+`POST /connections/{id}/check` 与 `Connection.entry_url` 补录入 v0.1；回调通道待平台登记后另行补录）。
- CR002（A14 受控浏览器会话形态）已裁定结案（补充说明并入 v0.1，第 4 项上报端点不采用；见上）；CR003（B13 任务生成接入）已裁定 merged（见上）；CR004（任务详情端点补录 `GET /tasks/{task_id}`）已裁定 merged（见上）；CR005（ICS 导出枚举 + NEEDS_OCR 口径）已裁定 merged（见上）；CR006（.zip 课件包入库 + skipped 口径）已裁定 merged（见上）；CR008（复习卷契约冻结）已裁定 merged（见上）；CR009（review-question 来源引用枚举增补 `note`）已裁定 merged（见上）。

**已知待办（v0.2 及以上）**

- 截止时间变更历史的 API 暴露（v0.1 由服务端记录变更日志，先不开放端点）。
- 课程映射自动建议与确认流程端点（v0.1 为人工确认挂接 `POST /courses/{id}/source-refs`；候选结构已冻结于 `schemas/mapping-candidate.schema.json`）。
- 报表/统计类端点（ICS 导出已于 CR005 交付；本项仅余报表/统计）。
