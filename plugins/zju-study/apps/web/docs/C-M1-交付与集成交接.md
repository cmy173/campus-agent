# C-M1 · 网页端交付与集成交接（智能体 C → 智能体 B / 协调者）

- 日期：2026-10-09　分支：`agent/c`　worktree：`.worktrees/agent-c`
- 范围：`apps/web/` M1（C1–C10、C4b；其中 **C2 真实模式端到端联调尚未执行**，见 §0/§6）
- 基准：契约 v0.1 冻结版（`main@8ce5b43`）；本批两个提交：`f514df6`（C3–C7、C4b）+ `6f9dcbb`（C8/C9/C10 收尾）+ 本报告
- 格式对齐《00-协作总纲》§8：变更文件、输入输出契约、运行方法、测试结果、已知限制、待协作事项。

> **收口轮（2026-10-10）修订注记**：登录形态已按 A14 v0.2 修正为「受控浏览器 + 持久化 profile」——登录在**本机连接器打开的浏览器窗口内的平台官方页面**完成，新增「检查连接」手动兜底（`POST /connections/{id}/check`，C 侧 `api.checkConnection`）；C2 真实模式联调已完成（10 阶段），C11/C12 已交付。本文档正文中「跳转式登录」「entry_url=zjuam.zju.edu.cn」等表述为第一轮形态的描述，当前口径以 `C2-真实模式联调记录.md` 与 `C-M1收口轮交接.md` 为准。

> **M2 起步轮注记（2026-10-10）**：能力清单以 `apps/web/README.md` 与 `C-M2起步轮交接.md` 为准——导出（JSON）真实模式已可用、全文搜索与平台同步后端端点已就绪；本文档 §2 中「待 B4」等字样仅为 M1 时点快照。

## 0. 状态总览（区分口径）

| 状态 | 内容 |
|---|---|
| **已实现** | C1 契约确认回执（`713c486`，已并入 `contracts/reviews/`）；C3 字段适配（chunk_count 徽标 / chunks 抽屉 / 全局作业过滤 / 连接详情与 disable-enable / retry 语义 / jobs 列表与过滤）；C4 跳转式登录入口 + E4/E6/E7 界面重构；C4b 模型设置区；C5 预览四态差异；C6 Job 操作按钮规则；C7 反馈闭环 + 来源跳转；C8 安全渲染审计；C9 无障碍检查记录（README §可用性与无障碍）；C10 未接通能力清单（设置页 + README） |
| **已验证** | `tsc -b` 无错误；`vitest run` 4 文件 **44/44 全绿**；`npm run build` 成功；mock 全流程（导入→课程/作业/回放→可用时间→预览→应用→反馈→导出）由 handlers 测试逐端点覆盖 |
| **模拟验证** | 全部页面与流程在 mock 模式（确定性假数据）下验证；跳转式登录按「权威域名提示 + 外跳 + 回跳后状态展示」实现，mock 演示为 `entry_url=zjuam.zju.edu.cn` |
| **尚未验证** | **C2 真实模式端到端（硬验收）**——待 B4 写路径端点就绪；M1 界面重构后的浏览器复检与窄屏适配（计划 M3）；真实平台数据（由 A/B 侧提供） |

## 1. 变更文件（`apps/web/`）

| 文件 | 说明 |
|---|---|
| `src/types/contract.ts` | 契约 v0.1 冻结版逐字段镜像（无 `PlanBlock.change`、无 `Availability.revision`、`chunk_count` 必填可空、SCREAMING 原因码等） |
| `src/lib/api/http.ts` | `ApiErrorBody` 解包（code/message/retryable/details/request_id）、`Idempotency-Key`、mock/real 双模式切换 |
| `src/lib/api/mock/{handlers,db,engine}.ts` | 模拟后端全量对齐 v0.1：导入幂等/去重/版本、预览确定性、PLAN_STALE、锁定语义（E7 移动→400）、Q7 按钮规则、跳转式登录、导出等 |
| `src/lib/matching.ts` | Q3 任务→平台对象匹配键（`platform` + `external_id`），匹配不到禁用跳转 |
| `src/components/JumpLoginModal.tsx` | 跳转式登录：展示权威域名、新标签外跳 `recovery_action.entry_url`、回跳后由页面轮询/刷新连接状态；**无任何凭据输入** |
| `src/components/ChunksDrawer.tsx`、`LocatorText.tsx`、`SourceTag.tsx`、`tags.tsx` | 片段抽屉（page/slide/timestamp/text 定位 + 可复制定位文字）、来源标签、差异四态标签 |
| `src/features/onboarding/` | 四步引导重构：平台连接（跳转式登录）、导入/同步、手动挂接课程（409 明示占用、重复绑定幂等）、可用时间 |
| `src/features/sync/` | 连接面板（跳转登录/重新登录/禁用启用）、能力清单（如实四态）、后台任务（过滤、取消协作式、重试、不确定进度、errors 逐条） |
| `src/features/plan/PlanPage.tsx` | 预览四态差异（按 `task_id`）、应用/放弃/撤销、锁定（仅锁定语义）、无法安排原因码 + 缺口 + 建议；移动标注未接通 |
| `src/features/today/TodayPage.tsx` | 反馈弹窗（「学习完成 ≠ 学校平台已提交」）、来源跳转、临近截止作业 |
| `src/features/courses/` | 资料（chunk_count 徽标、解析重试）、作业（学习状态/平台提交状态分离）、回放（zykt 待接入如实标注）、挂接平台来源 |
| `src/features/settings/` | 可用时间表单（校验）、JSON 导出、模型与解析服务（仅状态展示）、**未接通能力清单**、关于 |
| `src/lib/validation.ts`、`src/features/settings/AvailabilityForm.tsx` | 可用时间校验（窗口格式/重叠、每日上限、日期例外=整天不可用） |
| `tests`：`src/lib/validation.test.ts`、`src/lib/api/mock/handlers.test.ts`、`src/features/today/TodayPage.test.tsx`、`src/features/plan/PlanPage.test.tsx` | 44 项用例（详见 §4） |
| `README.md` | M1 全量更新：运行模式、目录、已实现、未接通清单、测试、契约对齐、C9 检查记录 |
| `docs/C-M1-交付与集成交接.md` | 本文档 |

## 2. 输入输出契约

### 2.1 与 B 的对接面（页面实际消费）

| 端点 | 用途 | 当前状态（截至本报告） |
|---|---|---|
| `GET/POST /courses`、`/courses/{id}/resources|assignments|recordings` | 课程与资源 | main 已实现 |
| `POST /courses/{id}/source-refs` | 手动挂接（E6） | main 已实现（409 `CONFLICT` 附 `details.course_id/course_title`，重复绑本课幂等） |
| `POST /imports`（multipart + Idempotency-Key） | 导入（B1） | main 已实现（202 返 job） |
| `POST /resources/{id}/reparse` | 解析重试 | **待 B4**（未实现返回 501 信封，界面按错误态如实展示） |
| `GET /resources/{id}/chunks` | 片段抽屉（E1 读路径） | main 已实现 |
| `GET /assignments`（含过滤） | 今日页临近截止（E5） | main 已实现 |
| `GET/POST /tasks…`、`POST /tasks/{id}/lock` | 任务与锁定（E7：仅锁定，移动→400 `details.mock_reason=block_move_not_supported`） | main 已实现 |
| `POST /plans/preview`、`POST /plans/{id}/apply`、`POST /plans/{id}/revoke` | 预览（Q1 客户端四态）/应用/撤销 | main 已实现（`PLAN_STALE` 409 `retryable=true`，`details={plan_revision,current_revision}`） |
| `POST /tasks/{id}/feedback` | 反馈（Q2 直接采用响应 `task`） | main 已实现 |
| `GET /jobs`、`GET /jobs/{id}` | 任务列表与轮询 | main 已实现 |
| `POST /jobs/{id}/retry`（E3）、`POST /jobs/{id}/cancel`（Q7） | 重试/取消 | **待 B4** |
| `GET /connections`、`GET /connections/{id}`、`/capabilities` | 连接面板 | main 已实现 |
| `POST /connections/{id}/disable|enable`（E4） | 停用/启用 | **待 B4** |
| `POST /connections/{id}/sync`（B3） | 立即同步 | agent/b 已实现（`40c42e6`），main 待并入 |
| `POST /exports`、`GET /files/{id}` | JSON 导出 | **待 B4**（界面入口已就绪，按错误态展示） |
| `GET /search` | 搜索 | **待 B4**（界面入口暂未开放，C10 已标注） |

### 2.2 页面遵循的约定（C2 联调时请按此核对）

- **运行模式**：`VITE_API_MODE=mock|real`（默认 mock），mock 与 real 同路径同错误结构，**页面代码不改**；real 下 `VITE_API_BASE_URL` 或 `VITE_PROXY_TARGET` 指向后端。
- **错误呈现**：统一解包 `ApiErrorBody`；`retryable` 决定是否提示可重试；`request_id` 展示于错误信息，便于 B 侧排查。
- **跳转式登录**：首次连接/重登录均为「跳转平台通行证页（展示权威域名）→ 用户自行输入 → 回跳后展示连接状态」；`auth_required` 的 `recovery_action.entry_url` 是唯一重登录入口，**不循环重试**（`AUTH_EXPIRED.retryable=false`）；`POST /connections` 不入 v0.1，引导页只读展示 + 跳转。
- **时间语义**：`input_revision='ir_<n>'`；预览过期以 `PLAN_STALE`（409）与客户端修订比对双重防护；显示一律用户时区，保存带时区偏移。
- **Q1**：预览四态（新增/移动/保留/移出）由客户端按 `task_id` 对比得出，未新增契约字段；锁定块按「时间一致→保留」处理。
- **Q4**：已移除「延后 / 缺口顺延」按钮；`skipped` 反馈不改任务状态，下次预览自然重排。
- **安全边界**：任何页面不收集/代填/保存平台凭据；密钥不出现在页面/日志/前端存储；文档正文不注入未处理 HTML；外链均 `rel="noreferrer"`。

## 3. 运行方法（可直接复制）

```bash
cd apps/web
npm install
npm run dev        # mock 模式，http://localhost:5173
npm run test       # vitest 单次运行（44 用例）
npm run build      # tsc -b && vite build（产物 dist/）
npm run lint       # oxlint
```

真实模式（C2 联调）：

```bash
# 方式一：环境变量直连
VITE_API_MODE=real VITE_API_BASE_URL=http://localhost:8000 npm run dev
# 方式二：apps/web/.env.local 写入 VITE_API_MODE=real 与 VITE_PROXY_TARGET=http://localhost:8000
```

后端本地启动与种子数据由 B 提供（apps/api README）。

## 4. 测试结果（2026-10-09 复跑）

| 命令 | 结果 |
|---|---|
| `npx tsc -b` | 无错误 |
| `npx vitest run` | **4 文件 / 44 用例全绿** |
| `npm run build` | 成功（已知告警见 §5） |

用例分布与覆盖：

- `src/lib/validation.test.ts`（13）：窗口格式/重叠、每日上限（含 null）、日期例外（空窗口=整天不可用）、重复与越界提示
- `src/lib/api/mock/handlers.test.ts`（27）：契约形状与错误信封、今日范围、导入幂等/去重/版本 409-413-415、解析状态机与 chunk_count 一致性、预览确定性、原因码（DEADLINE_PASSED / TASK_TOO_LONG_INDIVISIBLE 等）、`PLAN_STALE`、应用/撤销、锁定（含移动被拒 400）、反馈闭环（Q2）、任务重试/取消（Q7）、连接跳转式登录（`auth_required.entry_url`）、部分失败重试、导出、可用时间保存、来源挂接 409/幂等、搜索
- `src/features/today/TodayPage.test.tsx`（2）：今日页渲染；反馈流程（「学习完成 ≠ 学校平台已提交」）
- `src/features/plan/PlanPage.test.tsx`（2）：生效计划与原因码展示；生成预览→差异标签（保留）/应用/放弃入口→放弃回到生效计划

## 5. 已知限制

- **C2 未执行**：真实模式端到端联调待 B4 写路径端点就绪（§6 第 1 条）；当前对未实现端点按 501 错误信封如实展示，无假交互。
- M1 界面重构后的**浏览器复检与窄屏（<375px）适配未系统验证**（M0 阶段曾全路由冒烟；计划 M3）。当前由 jsdom 渲染测试覆盖今日页与计划页主流程。
- M0 遗留、M3 处理：单包 1.56 MB（gzip 491 kB）体积告警；antd `List` 弃用告警（建议迁移 `Listy`）。
- 未接通能力（界面已如实标注、无假按钮）：真实平台同步（真实模式联调前）、音视频转写、ICS 导出、块拖动/微调、搜索入口、导入资料清理、模型调用、扫描件 OCR、平台写操作。
- zykt（智云课堂）区域维持「待接入」表述（Q6：字幕片段关联不入 v0.1）。
- 不出现「安装浏览器扩展」类引导（用户决策）。

## 6. 待协作事项

1. **B（阻塞 C2）**：请按 B4 批次实现写路径——`POST /jobs/{id}/retry`（E3 语义：以原参数发起新 job，202 返回新 job；仅 failed/partial_failed 可用）、`POST /jobs/{id}/cancel`（Q7：协作式，`cancel_requested`）、`POST /connections/{id}/disable|enable`（E4）、`POST /exports` + `GET /files/{id}`、`GET /search`、`POST /resources/{id}/reparse`、解析流水线（驱动 `chunk_count` 与片段写入）。`POST /connections/{id}/sync`（B3）已在 agent/b 实现，请随批并入 main。
2. **B**：合并本报告提交（`apps/web/docs/`）；如需冻结记录，可在 `contracts/CHANGELOG.md` 引用本报告的端点对账表（§2.1）。
3. **B/D**：C2 联调前请确认演示路径的种子数据（今日任务范围、临近截止作业、`PLAN_STALE` 触发路径、导入样例文件），以便按 §3 命令一键复现全流程。
4. **协调者**：C2 演示脚本（导入→课程/作业/回放→可用时间→预览→应用→反馈→导出，全程 real 模式）将在联调前提交，供验收对照。

---

备注：C1 契约确认回执见 `contracts/reviews/2026-10-09-C1-确认回执.md`；C0 审阅与对接清单见 `apps/web/docs/C0-契约v0.1审阅意见与对接清单.md`。
