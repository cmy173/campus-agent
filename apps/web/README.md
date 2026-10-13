# 浙大学习工作台 · 网页端（apps/web）

> 2026-10-11 收尾更新：个人使用请按根目录 README 和 `docs/coord/10-收尾修复与使用说明.md` 启动。默认网页为 real；统一启动器使用 personal.db，不 seed。以下旧章节中的样例导入仅用于开发演示；智云已有独立会话、详情和按需字幕接线，当前真实账号联调待确认。


智能体 C 负责的前端应用。页面与关键流程已对齐**契约 v0.1 冻结版**；M1（手动闭环）已交付并入 `main`，M2 起步轮完成**窄屏（375/768/1280）× 7 路由系统走查与修复**（见 `docs/C14-窄屏响应式系统验证.md`）与**可访问性基线核对**（见 `docs/C15-可访问性基线与键盘可达性.md`）；收官轮完成**真实后端数据下六路由走查、合并后 C14/C15 复验与 ICS 日历导出接通**（见 `docs/C18-C20-收官轮真实数据走查与复验.md`）。默认运行在**模拟 API 模式**下，数据为本地确定性假数据，不连接任何真实平台；真实模式端到端联调已完成（C2 起，收官轮复跑 11/11，见 `docs/C2-真实模式联调记录.md`）。

## 快速开始

```bash
cd apps/web
npm install
npm run dev        # http://localhost:5173
```

其余命令：

```bash
npm run test       # vitest 单次运行（75 个用例）
npm run test:watch # 监听模式
npm run build      # tsc -b && vite build（产物在 dist/）
npm run lint       # oxlint
npm run preview    # 预览构建产物
```

要求 Node.js 20+（开发验证于 Node 24）。

## 后端地址与运行模式

通过环境变量控制（Vite 约定，`VITE_` 前缀；可写 `apps/web/.env.local`）：

| 变量 | 取值 | 说明 |
| --- | --- | --- |
| `VITE_API_MODE` | `mock`（默认）\| `real` | `mock`：请求走 `src/lib/api/mock/`，不发出任何网络请求；`real`：`fetch` 到真实后端 |
| `VITE_API_BASE_URL` | 例 `http://localhost:8000` | 真实模式下的后端地址；请求路径为 `${BASE}/api/v1/...` |
| `VITE_PROXY_TARGET` | 例 `http://localhost:8000` | 仅真实模式且未设 `VITE_API_BASE_URL` 时，开发服务器把 `/api` 代理到该地址 |

真实模式对接说明：请求头支持 `Idempotency-Key`（导入/写操作幂等）与 `If-Match`（对象 revision 乐观锁，不匹配返回 412）；错误体按契约 `ApiErrorBody`（`code` / `message` / `retryable` / `details` / `request_id`）解析，见 `src/lib/api/http.ts`。后端尚未实现的端点会返回 501/错误信封，界面按其错误态如实展示（不做假交互）。

## 目录结构

```
src/
  app/            路由、布局、Providers（antd ConfigProvider + react-query）
  components/     跨页面组件（StateView、JobProgressInline、ChunksDrawer、JumpLoginModal、SourceTag 等）
  features/       按页面组织的功能模块
    today/        今日（今日任务、反馈弹窗、临近截止作业）
    courses/      课程与资源（列表、三个标签页、挂接平台来源）
    plan/         学习计划（预览四态差异/应用/撤销/锁定/无法安排原因）
    sync/         同步中心（连接与跳转式登录、后台任务）
    settings/     设置（可用时间、数据管理、模型区、未接通能力清单、关于）
    onboarding/   首次使用引导（四步）
  lib/
    api/          client / hooks / http；mock/ 为模拟后端（handlers 路由 + db 种子 + engine 规则排程）
    time.ts       时区与时间格式化（dayjs + timezone/isoWeek）
    matching.ts   任务→平台对象匹配键（platform + external_id）
    validation.ts 可用时间校验（窗口重叠、每日上限、日期例外）
  types/contract.ts  前端侧契约类型（对齐 v0.1 冻结版）
```

## 已实现（M1）

- 今日：今日任务（时间块、状态、截止精度提示、来源标注）、反馈弹窗（完成 / 部分完成 / 本次未做，明确「学习完成 ≠ 学校平台已提交」）、任务→平台作业跳转（按 `(platform, external_id)` 匹配，匹配不到则禁用不猜测）、同步异常提示
- 课程与资源：课程列表与详情；资料（导入/去重/新版本/解析状态/重试/`chunk_count` 徽标/片段抽屉含页码、幻灯片、时间片定位与可复制定位文字）、作业（学习状态与平台提交状态分离）、回放（字幕状态、原平台播放）
- 学习计划：预览差异四态（新增 / 移动 / 保留 / 移出，按 `task_id` 客户端对比）、应用 / 撤销（回到上一生效版本）、锁定 / 解锁、**无法安排任务的原因码与中文解释**（含缺口分钟数与建议）、周视图；时间块移动标注「未接通」
- 同步中心：连接面板（跳转式登录：展示权威域名与说明，用户自行登录；`auth_required` → 重新登录入口，不循环重试；禁用 / 启用）、能力清单、后台任务（状态 / 类型过滤、取消（协作式）、失败 / 部分失败重试、`progress=null` 不确定进度、errors 逐条明细）
- 设置：每周可用时段（含一日多窗口）、每日学习上限、日期例外（空窗口 = 整天不可用）、保存；平台与同步摘要；数据管理（JSON 与 ICS 日历导出：模拟/真实模式均已接通，真实模式联调通过；ICS 为计划时间块与任务截止的整体导出，不支持 include 筛选；清理标注未接通）；模型与解析服务（仅展示配置与状态，密钥不出现在页面/日志/前端存储）；未接通能力清单
- 首次使用引导：四步（平台连接 / 导入或同步资料 / 挂接平台课程 / 设置可用时间），可跳过；挂接支持「已绑他课 409 明示占用、重复挂接本课幂等」

时间语义约定：`input_revision` 形如 `ir_<n>`（任务、可用时间、反馈变化时递增）；可用时间或任务集变化后旧预览作废，应用时返回 409 `PLAN_STALE`（`retryable=true`）并提示重新生成。

## 未接通能力（如实标注，界面不提供假按钮）

- 真实平台同步（真实模式）：端点与界面已就绪（`POST /connections/{id}/sync`），错误路径（`auth_required` → 重登录入口）已联调（C2）；成功路径未在网页端实测（依赖运行期真实平台会话；组件侧 A21 已证受控浏览器登录可行、真实课程两次同步指纹一致）
- 音视频转写：仅读取平台已有字幕，不做语音转写（后端无转写接口）
- 时间块拖动与微调：仅支持锁定 / 解锁（界面无拖拽交互）；需改时间时解锁后重新生成预览
- 全文搜索：后端 `GET /search` 已就绪；界面入口暂未开放
- 导入资料清理：尚无对应接口
- 模型调用：外接 API 仅留端口，默认不接入、不读取任何密钥
- 扫描件 OCR：暂不支持
- 平台写操作：首版不含任何自动提交作业等写操作
- 不出现「安装浏览器扩展」类引导；本工作台任何页面都不收集、代填或保存平台凭据

## 测试

- `src/lib/validation.test.ts`：可用时间校验规则（窗口格式/重叠、每日上限、日期例外）
- `src/lib/api/mock/handlers.test.ts`：契约形状、错误信封、今日范围、导入幂等/去重/版本/解析、预览确定性与原因码、`PLAN_STALE`、应用/撤销、锁定（含移动被拒）、反馈闭环、任务重试/取消、连接跳转式登录、导出、可用时间保存、来源挂接 409/幂等、搜索
- `src/features/today/TodayPage.test.tsx`：今日页渲染与「学习完成 ≠ 平台已提交」流程
- `src/features/plan/PlanPage.test.tsx`：计划页渲染（原因码展示、生成预览后的差异标签与应用/放弃入口、放弃回到生效计划）

浏览器端到端：M1 收口轮完成界面重构后的逐页冒烟复检（修复 3 个真实缺陷，见 `docs/C13-质量加固与浏览器复检.md`）；**M2 起步轮完成窄屏 375/768/1280 × 7 路由（含课程详情与首次引导）的系统走查**，发现并修复窄屏侧栏挤压内容导致的横向溢出，三档复测无横向溢出（方法与逐路由结论见 `docs/C14-窄屏响应式系统验证.md`）。**收官轮完成合并后复验（三档 21 格全部无横向溢出；弹窗/抽屉键盘可达性对 5 处复跑）与真实后端数据六路由走查（含 ICS 导出浏览器全链路与无法安排原因码实测）**（见 `docs/C18-C20-收官轮真实数据走查与复验.md`）。jsdom 渲染测试持续覆盖今日页与计划页主流程。

真实模式端到端联调（C2）：独立配置、不进入默认测试集（`npm test` 不触发）。需后端在线（apps/api README）：

```bash
VITE_API_MODE=real VITE_API_BASE_URL=http://127.0.0.1:8000 \
  npx vitest run --config vitest.real.config.ts
```

11 项用例（收官轮复跑 11/11）：真实模式生效（无 mock 混入）→ 导入（multipart + 同键幂等）→ 解析 chunks（chunk_count 一致）→ 课程/作业/回放读取 → 可用时间（含已知偏差核验）→ 预览→应用 → 反馈闭环 → JSON 导出下载 → ICS 日历导出下载（`include+ics` 被拒收 400）→ 错误信封（404/400/`PLAN_STALE`/`AUTH_EXPIRED` 哨兵）→ 任务生成（warnings 如实上报、不产生重复任务）；结果与契约偏差写入 `var/c2-e2e-report.json`。执行记录与偏差清单见 `docs/C2-真实模式联调记录.md`。

## 可用性与无障碍（C9 基线；M2 起步轮 C15 复检与键盘可达性改造）

- 关键表单（可用时间）有即时校验（窗口格式/重叠、每日上限、日期例外，见 `src/lib/validation.ts`）；校验失败逐字段提示且阻止保存；表单错误经 `aria-invalid` + `aria-describedby` 暴露（浏览器实测：挂接来源弹窗空提交后 combobox `aria-invalid="true"` 且 `aria-describedby="platform_help"`）
- 主要操作（导航、生成/应用/撤销计划、锁定/解锁、反馈、取消/重试任务、连接操作）均为标准按钮或表单控件，可键盘完成；`index.html` 声明 `lang="zh-CN"`，其余交互语义依赖 antd 内建 ARIA
- 抽屉/弹窗焦点管理经真实浏览器实测：打开时焦点移入容器（2026-10 复检，ChunksDrawer/BuildTasksDrawer/JumpLoginModal/完成反馈弹窗 4 处）；Esc 关闭经派发键盘事件实测有效（rc-drawer/rc-dialog 内建）；关闭归还焦点为源码级核对（rc-dialog 焦点还原经 motion 门控，隐藏页环境无法观测，见 C15 记录「已知限制」）
- 图标按钮带可读名称（`aria-label`）：导航开关、删除时段/日期例外、时段起止时间、例外日期/备注、每日学习上限、候选任务筛选、生成范围等；C15 复检各页按钮 0 个无名称（明细见 `docs/C15-可访问性基线与键盘可达性.md`）
- 危险操作（撤销计划）有 Popconfirm 二次确认；所有 `target="_blank"` 外链带 `rel="noreferrer"`
- 无拖动操作：块拖动未接通（v0.1）；改时间走「解锁 → 重新生成预览」，不需要拖放
- 桌面宽度下使用 antd 栅格/换行 Space 布局；**窄屏 375/768/1280 × 7 路由系统走查已完成**（窄屏侧栏改为固定抽屉式 + 遮罩，修复内容被挤压导致的横向溢出；结论见 `docs/C14-窄屏响应式系统验证.md`）

## 契约对齐（v0.1 冻结版，main@8ce5b43）

- 类型定义见 `src/types/contract.ts`，与冻结契约逐字段核对；mock API 与真实 API 同构：同一组路径、同一错误结构，页面不感知运行模式
- 预览四态由前端按 `task_id` 对比得出（Q1），`PlanBlock` 无 `change` 字段
- 无法安排的 `reason_code`（`UnassignedReasonCode`）：`DEADLINE_PASSED` / `INSUFFICIENT_WINDOW_BEFORE_DUE` / `INSUFFICIENT_TOTAL_TIME` / `TASK_TOO_LONG_INDIVISIBLE` / `FRAGMENTED_WINDOW` / `DEPENDENCY_CONFLICT` / `INVALID_TASK`；界面为每个原因码提供中文解释、缺口分钟数与建议
- 首版不支持通过锁定操作改块时间（E7）：请求携带不同的 `starts_at` 返回 400 并附 `mock_reason=block_move_not_supported`；已移除「延后 / 缺口顺延」按钮（Q4）
- 反馈直接采用 `FeedbackResponse.task`（Q2）：`completed`→`completed`、`partial`→`in_progress`、`skipped` 不改任务状态；无「最近反馈」读取端点，不做历史展示
- 来源匹配与跳转按 `source_refs` 的 `(platform, external_id)`（Q3）；「已合并 N 个来源」由 `source_refs.length > 1` 得出（Q5）
