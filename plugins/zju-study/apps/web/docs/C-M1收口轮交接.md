# C-M1 · 收口轮交接（智能体 C → 智能体 B / 协调者）

- 日期：2026-10-10　分支：`agent/c`　worktree：`.worktrees/agent-c`
- 范围：`apps/web/` 收口轮——**C2**（真实模式端到端联调，硬验收）、**C11**（任务生成入口 + 候选任务确认）、**C12**（登录/连接引导按 A14 v0.2 调整 + 检查连接）、**CR004 对账**（恢复 getTask）、**C13 质量加固**（代码分包/弃用清理/浏览器复检，见 `docs/C13-质量加固与浏览器复检.md`）
- 基准：`main@471e736` 起（首次 merge `d94eee2`）→ 收口整改并入 `main@3156550`（二次 merge `c2401a1`）；本轮提交：`fb24e3e`（C11）→ `f111562`（C12）→ `edc11b2`（CR004 + C2 第五次复跑）→ C13 批（分包/弃用清理/冒烟修复，与本报告同批提交）
- 格式对齐《00-协作总纲》§8：变更文件、输入输出契约、运行方法、测试结果、已知限制、待协作事项；§0 逐项区分已实现/已验证/模拟验证/尚未验证

## 0. 状态总览（区分口径）

| 项 | 状态 | 说明 |
|---|---|---|
| C2 真实模式 E2E | **已实现 + 已验证（客户端层）** | 五次运行：前两次 9/9 → 追加 C11 相位 10/10 → 追加 C12 哨兵 10/10 → B 整改并入后 10/10（deviations 4→1 提示项）。全程无 mock 混入，四类错误信封按 `ApiErrorBody` 呈现；同一份业务代码（`client.ts` 层）在 mock/real 下页面零分支。**浏览器 UI 冒烟尚未验证**（见 §5） |
| C11 任务生成入口 | **已实现 + 已验证** | 今日页/课程资源页入口 + `BuildTasksDrawer`（来源标注/耗时依据 fact_source/完成标准如实提示/warnings 原样展示/改耗时/归档）；mock 镜像 B13 幂等语义（match_key 对齐、用户修正保留、archived 不复活）；mock 测试 + 组件测试覆盖；真实模式 E2E 验证 warnings 如实上报与重复触发零新增 |
| C12 登录/连接引导 | **已实现 + 已验证（文案与检查错误路径）** | v0.2 文案：弹窗内自行登录/权威域名展示/本机操作提示/不循环重试；无凭据输入 UI、无扩展引导；`checkConnection` client/hook/mock/UI 全链路；E2E 哨兵实测 `502 DEPENDENCY_UNAVAILABLE` 如实返回（不造假 ok）。**check 成功路径尚未验证**（被适配器阻塞，A 桥接后自动覆盖） |
| CR004 对账 | **已实现 + 已验证** | 恢复 `GET /tasks/{task_id}`（client/mock/契约测试/E2E 断言），真实后端 200/404 均验证 |
| C13 质量加固 | **已实现 + 已验证** | ① 代码分割：单包 1621.71 kB → 入口 81.36 kB + vendor/页面按需分包，>500 kB 告警消除；② `List`→`Listy`（3 处）、`Drawer width`→`size`（2 处）、`Steps description`→`content`（1 处）；③ 真实浏览器冒烟完成并**发现修复 3 个真实问题**（今日页查询键毫秒抖动导致的请求风暴、课程详情窄屏 207px 溢出、能力清单错误文案）；531px 窄屏 7 页零溢出；390px 手机档未验证（环境限制） |

## 1. 变更文件（本处理轮，均为 `apps/web/`）

| 文件 | 说明 |
|---|---|
| `src/features/tasks/BuildTasksDrawer.tsx`（+ `BuildTasksDrawer.test.tsx`） | C11 候选任务确认抽屉：来源标注、预计耗时 + `estimate_provenance`（事实/推断）、完成标准如实提示、warnings 原样渲染（7 码元数据 + 未知码兜底）、改耗时/归档 |
| `src/features/today/TodayPage.tsx`、`src/features/courses/ResourcesTab.tsx` | C11 入口（"由资料生成学习任务"）；资源行按整门课程触发并在界面明示 |
| `src/lib/api/mock/buildTasks.ts`、`mock/db.ts` | C11 mock 镜像：match_key 幂等对齐、警告码、用户修正保留、archived 不复活 |
| `src/lib/tasks.ts` | `filterTodayTasks` 客户端今日视图过滤（替代已移除的 mock 私有 `scope=today`） |
| `src/components/JumpLoginModal.tsx` | C12 重写：受控浏览器+持久化 profile 文案、权威域名、检查连接按钮、不循环重试 |
| `src/features/onboarding/OnboardingPage.tsx`、`src/features/sync/SyncCenterPage.tsx` | C12 检查连接入口 + 引导文案；连接由本机连接器建立的空态说明 |
| `src/components/ConnectionCapabilities.tsx`、`src/features/settings/SettingsPage.tsx`、`src/components/tags.tsx` | C12 文案对齐 / C11 标签 |
| `src/lib/api/client.ts`、`hooks.ts` | `checkConnection`（C12）；`getTask` 恢复（CR004）；`buildTasks`（C11）；移除 `scope=today`/`getTask`(C11 时)/`deleteAssignment` 幽灵面 |
| `src/lib/api/mock/handlers.ts`、`mock/handlers.test.ts` | mock `POST /connections/{id}/check`、`GET /tasks/{id}`（CR004）、entry_url 常量对齐 CR001-DECISION-3；测试 54→55（+getTask 契约测试、+检查连接测试） |
| `src/types/contract.ts` | `Connection.entry_url`（C12）；C11 类型面收敛 |
| `src/lib/api/real-mode.e2e.ts` | C11 相位、C12 哨兵、CR004 `getTask=200/404` 断言；报告产物 `var/c2-e2e-report.json` |
| `src/app/router.tsx`、`vite.config.ts` | **C13** 路由级 lazy（8 页 + Suspense 兜底）；`codeSplitting.groups` 手动分包（react/antd/rc/icons/cssinjs） |
| `src/features/today/TodayPage.tsx`、`src/features/plan/PlanPage.tsx` | **C13** `List`→`Listy`；TodayPage 查询窗口按天量化（修复请求风暴） |
| `src/components/ChunksDrawer.tsx`、`src/features/courses/ResourcesTab.tsx` | **C13** `Drawer width`→`size`（480/420 实测精确） |
| `src/features/courses/{AssignmentsTab,RecordingsTab}.tsx`、`src/features/sync/SyncCenterPage.tsx`、`src/features/tasks/BuildTasksDrawer.tsx` | **C13** 表格 `scroll={{x:'max-content'}}`（修复窄屏溢出）；SyncCenter 能力清单错误文案含 message |
| `src/features/onboarding/OnboardingPage.tsx` | **C13** `Steps items.description`→`content`；能力清单错误分支补全 |
| `docs/C13-质量加固与浏览器复检.md` | **C13** 主证据文档（分包对比/弃用清理/冒烟发现与修复/窄屏方法） |
| `docs/C2-真实模式联调记录.md` | 五次运行记录 + 偏差复验批注（C2 主证据文档） |
| `docs/C致B-收口轮转答-C2复跑与C11对账.md`、`docs/C致D-收口轮答-warnings展示回执.md` | 转答文件（可直接粘贴转交） |
| `docs/C-M1-交付与集成交接.md`（修订注记）、`docs/C-M1收口轮交接.md`（本文件） | 交接修订与收口轮交接 |

## 2. 输入输出契约

- **消费端点（全部在冻结契约 v0.1 + 补录面内）**：`POST /tasks/build`（CR003）、`POST /connections/{id}/check`（CR001/CR002 面）、`GET /tasks/{task_id}`（CR004 补录）、以及 M1 既有全部读写端点（含 `PUT /preferences/availability`，B 整改后真实模式可用）。
- **错误呈现**：统一 `ApiError {status, code, retryable, details, requestId}`；`AUTH_EXPIRED.recovery_action` 驱动重登录弹窗（**不循环重试**）；`NEEDS_OCR` 等按落库映射展示不推断。
- **不出网/不持凭据**：无凭据输入 UI、无代填代存；令牌/密钥不进页面、日志、前端存储（C4b/安全约束延续）。
- **对契约的建议（不阻塞，见转答文件）**：① `PATCH /tasks/{id}` 后 `fact_source` 未置 `user` 的口径；② `completion_criteria` 可选字段增补；③ 单资源范围生成（`BuildTasksRequest` 仅 `course_id`）。

## 3. 运行方法（可复制命令）

```bash
# ── 常规（apps/web 目录）──
npm ci
npx tsc -b
npx vitest run                                  # 55/55（含 mock 契约、组件、渲染安全等 5 个文件）
npx vite build

# ── 真实模式联调（需后端；完整步骤见 docs/C2-真实模式联调记录.md §1）──
# 终端 1：后端（worktree 内 apps/api，注意先 alembic upgrade head）
cd apps/api
STUDY_WORKER_INPROC=1 .venv/Scripts/python -m uvicorn app.main:app --port 8000

# 终端 2：E2E（apps/web）
cd apps/web
VITE_API_MODE=real VITE_API_BASE_URL=http://127.0.0.1:8000 \
  npx vitest run --config vitest.real.config.ts   # 10/10；报告写入 <repo>/var/c2-e2e-report.json
```

- mock 运行：`VITE_API_MODE=mock npx vite dev`（默认模式）；真实模式浏览器运行走 `VITE_PROXY_TARGET`（后端无 CORS，禁用直连）。

## 4. 测试结果

| 项 | 结果 |
|---|---|
| `tsc -b` | ✅ 0 错误（含 `real-mode.e2e.ts`） |
| `vitest run`（默认集） | ✅ **55/55**（5 文件；含既有 44 项无回归，C11 起新增抽屉组件测试、getTask 契约测试、检查连接测试等） |
| `vite build` | ✅ 成功且**无 >500 kB 告警**；入口 81.36 kB（gzip 24.09），vendor 分包 react 346.79 / rc 413.48 / antd 6 段 0.14–146.64 / icons 54.46 / cssinjs 44.53（kB），页面按需 2.07–15.68 kB |
| 真实模式 E2E | ✅ **10/10 × 6 次运行**（C13 修复后复跑一致）；最终：deviations 4→1（唯一提示项 `B-COURSE-WRITE-IMPLEMENTED`：POST /courses 已实现，请从台账移除） |
| 浏览器冒烟（真实模式） | ✅ 今日/计划/课程/任务生成/同步/登录引导全流程（详见 `C13-质量加固与浏览器复检.md` §3）；发现并修复 3 个真实问题；531px 窄屏 7 页零横向溢出 |
| 错误信封 | ✅ 404 / 400 / PLAN_STALE / AUTH_EXPIRED 四类链路核对；`check` 502 哨兵（如实） |

## 5. 已知限制（如实标注）

- **浏览器冒烟已完成（真实模式）**，但受内嵌浏览器 surface 不可见限制：交互以程序化方式执行（可访问性快照 + `evaluate_script`），**未做像素级视觉核对**；390px 手机档窗口不可达（环境原生 531px，`resizeTo` 无效）——真机视觉/手机档留 M3。
- **check / 同步成功路径未验证**：zykt 无适配器（502 如实返回）、真实会话依赖 A5b 受控实测；A 桥接后重跑 E2E 自动覆盖 200 路径。
- **mock 未镜像 parse→自动 build_tasks 联动**（`worker.py` reason=`"parse"`）；真实模式导入/解析后任务自动出现，mock 需显式触发——E2E 首触 `refs=0` 属幂等正常。
- **`capabilities` 404 为后端有意语义**（“连接能力信息尚未提供（同步/检查连接后由适配器写入）”，适配器未接入阶段）；前端已按“code + message”如实呈现，浏览器 console 会留有该 404 的 resource log（预期）。
- Dev 数据库增量：联调每轮新增若干资源/计划/反馈/文件，位于 worktree `var/`（gitignore）；删 `var/study.db` 重跑 `alembic upgrade head` + `app.seed` 即恢复。

## 6. 待协作事项

1. **B**：① 台账移除 `POST /courses` 提示项（`B-COURSE-WRITE-IMPLEMENTED`）；② 裁定/CR 三条接口建议（fact_source 口径、completion_criteria 可选字段、单资源范围生成）——详见 `C致B-收口轮转答-C2复跑与C11对账.md`。
2. **D**：无需动作；warnings 展示与 needs_ocr 落库映射已按你方口径落实（回执见 `C致D-收口轮答-warnings展示回执.md`）。
3. **A**：适配器桥接后，C 侧 E2E 哨兵自动接受 check 200 路径，无需 C 改动。
4. **协调者**：浏览器冒烟已在本轮完成（方法/限制见 `C13-质量加固与浏览器复检.md`）；如需真机视觉与 390px 手机档复检，请在 M3 安排。

## 7. 状态口径小结（总纲 §8）

- **已实现**：C11 全界面与 mock 镜像；C12 文案与 checkConnection 全链路；CR004 getTask 恢复；E2E 10 阶段用例与报告产物；C13 分包/弃用清理/3 个真实缺陷修复。
- **已验证**：55/55 测试、tsc、构建（无 >500 kB 告警）；真实后端全流程 10/10（第六次，C13 修复后）；错误信封四类；CR004 getTask 真实 200/404；C11 warnings 如实上报与幂等；warning message 原样渲染（7 码）；浏览器冒烟全页面与操作（含修复复验）；531px 窄屏 7 页零溢出。
- **模拟验证**：一切 mock 模式页面流（确定性假数据）；跳转弹窗文案（受控浏览器形态）在 mock 下可复现。
- **尚未验证**：390px 手机档真实窗口与像素级视觉（环境限制，留 M3）；check/sync 成功路径（适配器未接入）；mock parse 联动；真实会话有效期场景。
