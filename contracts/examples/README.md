# 共享样例与模拟数据（examples/）

面向全部智能体与前端开发的**规范样例**：B 的模拟 API（M1）按此返回，A 的 mock 适配器按 `adapter/` 对齐边界结构，C 可直接用作前端开发数据。

## 结构

| 路径 | 内容 |
|---|---|
| `api/*.json` | API 响应形状；列表统一 `{items, next_cursor}` 分页信封 |
| `adapter/*.json` | A3 适配器边界形状（`SyncResult`、`ConnectionState`、`FetchOutcome`、映射候选） |
| `manifest.json` | 样例 → Schema → 校验目标（`root` 或 `<key>[*]`）的映射清单 |
| `validate_examples.py` | 校验全部样例：Schema 符合性 + 跨文件引用一致性 |

## 场景覆盖（对齐总纲 §6 验收场景）

| 场景 | 样例位置 |
|---|---|
| 两门课程 / 跨平台同课 | `api/courses.json`（数据结构基础 ×2 平台）、`adapter/mapping-candidate.json` |
| 重复文件（同内容哈希） | `api/resources.json` 的 `res_01b2`（合并为多 `source_refs`） |
| 同名新版本 | `api/resources.json` 的 `res_01a7`（v2）+ `api/resource-versions.json` |
| 逾期作业 | `api/assignments.json` 的 `asg_a101` + `api/plans.json` 未安排 `DEADLINE_PASSED` |
| 无截止作业 | `api/assignments.json` 的 `asg_a102`（`due_at=null, due_precision=unknown`） |
| 无字幕回放 | `api/recordings.json` 的 `rec_r103`（`transcript_status=unavailable`） |
| 登录过期 | `api/connections.json` 的 `conn_8c1de`（`auth_required` + `recovery_action`）、`adapter/connection-state.json`、`api/error-responses.json` 的 `AUTH_EXPIRED` |
| 每日限额 | `api/availability.json`（`daily_limit_minutes=180`）；`api/plans.json` 的块满足限额 |
| 锁定任务 | `api/plans.json` 的 `blk_7a03`（`locked=true`，跨 90 分钟整块保留） |
| 时间不足 | `api/plans.json` 未安排项 `INSUFFICIENT_TOTAL_TIME`（缺口 60 分钟） |
| 提交状态与学习状态分离 | `asg_a103`（平台已提交）vs `tsk_6f2b47`（学习完成）；二者独立 |
| 复习卷：Provider 配置（密钥不回传） | `api/providers.json`（仅 `has_api_key`，无密钥字段；含本机地址无密钥样例） |
| 复习卷：材料选择与快照 | `api/review-materials.json`（含未解析/受限不可用）、`api/review-preflight.json`、`api/review-paper.json`（备注原文+哈希、缺材料 `MATERIAL_GAP`） |
| 复习卷：缺答案不伪造 / 修复轮 | `adapter/bank-candidates.json` 的 `qbc_0103b`（答案 `null`）→ 试卷 `qst_7c1a08` 标 `low_confidence`；`qst_7c1a05` 标 `repaired` |
| 复习卷：历史版本 | `api/review-revisions.json`（rev1/rev2 索引；fingerprint 与 summary 与试卷对账） |
| 复习卷：出卷作业与进度 | `api/jobs.json` 的 `job_5j6k7l`（succeeded）/ `job_6l7m8n`（running，`phase=generation`） |
| 复习卷：题库能力如实三态 | `api/questionbank-capabilities.json`（未配置来源 `configured=false`，不假装可搜） |

## 校验

```bash
python contracts/examples/validate_examples.py
```

除 Schema 校验外，脚本还会检查跨文件引用一致性：各对象 `course_id` 存在、计划块/未安排任务引用真实任务且互斥、`summary` 与块一致（分钟数与任务数）、前置依赖存在、作业附件存在等。复习卷样例另校验：正文/备注 `chars` 与哈希自洽（片段文本按 ordinal `\n` 连接）、快照↔预检↔试卷↔当前修订 fingerprint 一致、题量与配额对账、题库候选与告警交叉引用、Provider 样例不含密钥字段、出卷作业与试卷互引。**修改任一 ID 后请运行校验，避免样例漂移。**

## 约定

- 样例之间共享同一批 ID（课程 `crs_*`、资源 `res_*`、作业 `asg_*`、回放 `rec_*`、任务 `tsk_*`、计划 `pln_*`、连接 `conn_*`、任务作业 `job_*`），与 `contracts/schemas/` 一一对应。
- 时间为 ISO 8601 带 `+08:00` 偏移（样例演示本地时区渲染；API 默认返回 UTC）。
- 未验证的数据保持 `null` / `unknown`，不猜测（如受限件的 `content_hash=null`）。
- 契约变更后需同步更新样例并重跑校验；样例漂移会直接表现为校验失败。
