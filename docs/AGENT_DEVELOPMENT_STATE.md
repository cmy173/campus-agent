# Campus Agent 开发状态（接管记录）

- 更新时间：2026-10-17（Asia/Shanghai）
- 接管者：DeepSeek Harness 接管会话（下称 Lead）；上一任开发 Agent：Codex
- 分支 / HEAD：从 `codex/personal-assistant-v2` @ `650a934` 派生独立分支 `takeover/real-task-20261017`
  @ `343bb4a`；该提交是**接管快照**：既包含 Codex 留下的未提交改动（原样保留、未丢弃），也包含本轮 Lead 的修复与文档。
  需要回到接管前状态时：`git checkout codex/personal-assistant-v2`（原分支指针未动，远端未推送）。
- 本文件的用途：任何后续开发 Agent 先读这里，再读 `docs/campus-agent-benchmark.md`、`docs/audit/pr-validation.md`，即可从上次进度继续，不重复审计。

> 纪律：不覆盖用户未提交改动；重大改动走可回滚提交；不伪造工具结果、不用 Mock 冒充真实端到端成功；不推送远端。

---

## 1. 当前系统状态（实测事实，不是文档转述）

### 1.1 运行形态与入口

| 层 | 位置 | 说明 |
|---|---|---|
| 启动器 | `tools/dev.py` | 唯一正式入口：API(uvicorn `app.main:app`) + web(Vite)；`--no-platforms` 关闭校园连接器 |
| API | `apps/api/app/main.py` | FastAPI；路由在 `apps/api/app/routers/` |
| Agent 调度 | `apps/api/app/agent_bridge/runtime.py` | 拥有 Node runner 进程池、provider 密钥注入、工具租约、journal |
| Agent 主循环 | `apps/agent-runtime/runner.mjs` | 每会话一个官方 SDK 进程；`@deepseek-ai/dsh-sdk-client` 驱动 |
| 运行时组装 | `apps/agent-runtime/prepare-profile.mjs` | 组装 `@deepseek-ai/dsh-base` profile、系统提示、禁用 shell/workflow/ptc、注册 MCP |
| 校园插件 | `apps/agent-runtime/campus-plugin.mjs` | 通过 loopback bridge 注册 32 个校园工具、`publish_file`、模型适配器、工具租约 |
| 业务工具 | `apps/api/app/agent_bridge/tools.py` | 工具 schema/执行器/权限；`campus.*`、`courses.*`、`plans.*` 等 |
| Benchmark | `tools/campus_bench/runner.py` | 44 场景；real / mock-e2e / probe / compare / report |

### 1.2 本地服务与模型

- **用户自己的旧服务仍在 8000 端口运行**（PID 2556，2026-10-16 20:27 启动，`/health` **不含** `agent_source_sha256`）。它运行的是更早的代码，不能代表当前 checkout。不要杀它。
- 当前源码的隔离 API 通过 `tools/campus_bench/isolated_api_server.py` 启动在 `127.0.0.1:8017`（临时 state、引用已有加密密钥 ID，不复制密钥）。
- Provider：`prv_c68d2878`，`deepseek-flash`，`https://api.deepseek.com/v1`，已启用；密钥在 `var/secrets/provider-prv_c68d2878.key`（DPAPI 加密）。2026-10-17 12:33 通过旧服务 `/ai/providers/{id}/test` 实测 HTTP 200（1.6 s）。
- 环境变量里没有 `DEEPSEEK_API_KEY`（符合设计：密钥只经运行进程使用，不落日志）。

### 1.3 真实数据与授权状态

- `var/personal.db`：`courses=33`、`assignments=147`、`resources=269`、`resource_versions=269`、`content_chunks=12640`、`connections=2`。这是真实校园缓存数据（非模拟），可支撑 Level 2 的“读取本地课表/作业”类问题。
- 校园登录态 / 浏览器授权：`browser_session_authorized=false`，但本机保存的 passport 凭证可自动重登（2026-10-17 13:44 实测 `AUTO_LOGIN_OK`），
  随后真实 zdbk 课表同步成功（36 条，2026-10-17 13:46，见 4.18）。`conn_zykt=service_unavailable`；学在浙大/智云课堂本地连接器服务（8765/8766）未启动，
  因此作业/课件的实时同步仍不可用。**不要把“浏览器 grant 未授权”等同于“整个校园私有数据不可用”**；也不要把自动重登当作已证明学校端所有能力可用。
- 公开通知工具（`campus.sync(notices)` → `zdbk_public`）可匿名读取近期待办摘要，但 `completeness=partial / LIMITED_SCOPE`：只有近期置顶摘要，没有正文和历史。

### 1.4 网络（2026-10-17 实测）

| 观察 | 结果 |
|---|---|
| Node `fetch` → `https://www.zju.edu.cn/` | **200，90,236 B**（校内网络可达 10.x 私网地址） |
| Node `fetch` → `api-docs.deepseek.com` / `example.org` / `baidu.com` | 200 |
| `curl.exe` → 任意公网 URL | 全部 `000`（受本会话沙箱限制；**不要用 curl 判断网络**） |
| DNS：`www.zju.edu.cn` 等 | 解析为 `10.x` 校园私网地址 |
| `web_fetch` 工具 → ZJU 域名 | 2026-10-17 第二轮已改为**项目自有 fetch transport + 精确公开主机白名单**：`www/rd/bksy/zdbk/www.intl/www.chem/www.psych/www.math/lsi.zju.edu.cn` 可读；其他主机仍走上游公网策略 |
| `web_fetch` → 私网/回环 IP 字面量 | 仍为 `WEB_BLOCKED_URL`（安全边界保留） |

结论：**不是断网**。`web_search`（DeepSeek 原生检索，服务端执行）可用；ZJU 公开站点已可通过 `web_fetch` 直接读取（真实审计 5/5 通过，见 4.13）。

### 1.5 已实现功能（真实可用，保留）

- 对话/轮次 API：`POST /api/v1/agent/conversations` → `turns` → `GET /api/v1/agent/turns/{id}` 事件流；journal 持久化；空闲 runner 复用/回收。
- Harness 执行链路：API → Node runner → 官方 SDK → 工具执行 → 结果回传 → 下一轮模型调用；工具租约、取消、错误分类、原生 provider 有界重试。
- 32 个校园业务工具（`docs/campus-agent-tool-map.md`）：`campus.*`、`connections.*`、`courses.*`、`materials.*`、`assignments.*`、`tasks.*`、`plans.*`、`availability.read`、`jobs.*`、`artifacts.export`、`reviews.*`；按会话授权过滤数据。
- 通用能力（来自官方 Harness）：`web_search`、`web_fetch`、文件读写、附件、浏览器（应用自有 Edge profile + 一次确认）、MCP、子 Agent、jobs。
- 产物导出 `artifacts.export`（服务端固定渲染器，交付真实 PDF/XLSX/Markdown）与 `publish_file`。
- Benchmark：44 场景（33 real-runnable、9 脚本化 mock fixture、5 blocked-precondition）；证据脱敏、四层分层（direct tools / campus auth contract / harness dispatch / agent task / UX）。

### 1.6 历史验证基线（来自 2026-10-15 快照，需在当前源码复核）

`docs/audit/pr-validation.md`：API 700 passed / 5 skipped；root+connectors 606 passed + 87 subtests；frontend 193 passed；runtime 85 passed；受控模型 Harness `tools/verify_agent_runtime.py` 83 次调用通过；agent contract 51 paths / 54 schemas。

---

## 2. 已知故障（按真实执行链路分层）

> 诊断纪律：先定位层级，再改代码。不要用改 System Prompt 掩盖工具/权限/调度问题。

### L0 环境（本会话沙箱，非产品缺陷）

- **`spawn EPERM`**：DSH 沙箱内启动的 API 无法让官方 SDK 以管道 stdio 拉起 runner 子进程 → 任何真实 Agent turn 立即失败（~0.9 s，0 次模型调用）。**解法：API 服务在沙箱外启动**（本会话用 `sandbox_permissions=danger-full-access` 启动隔离 API）。
- **Python `os.mkdir(mode=0o700)` / `tempfile.mkdtemp` 创建的目录在沙箱内不可写** → `pytest tmp_path`、`verify_agent_runtime.py` 的 `mkdtemp` 失败。Node `fs.mkdtempSync` 正常。规避：用默认模式建目录 / 在沙箱外跑测试。

### L1 工具 / 网络层

- `web_fetch` 曾对解析到 `10.x` 的 ZJU 公开域名返回 `WEB_BLOCKED_URL`（上游公网地址策略）。**2026-10-17 第二轮已修复**：项目自有
  `apps/agent-runtime/campus-web-fetch.mjs` 保留上游严格解析器，仅对精确的 ZJU 公开主机白名单放行校园私网地址，并继续拒绝
  loopback / link-local / 组播 / 私网 IP 字面量；审计见 4.13。
- 遗留：部分 ZJU 子域不在白名单（如 `www.math.zju.edu.cn` 已补入、`stusom.zju.edu.cn` 仍被拒）；`rd.zju.edu.cn` 只支持 http，
  用 https 访问会得到 `WEB_PROVIDER_ERROR`。
- 影响：Level 1「搜索浙大公开活动并给来源」只能依赖 `web_search` 摘要，或浏览器工具；直接读页面正文被阻断。
- 注意：放开该策略属于**安全边界变更**，只能做“受信任校园公开域名白名单”式最小改动，不能全局放开私网。

### L2 API / 业务层

- 旧 8000 服务源码指纹缺失、schema 与当前 checkout 漂移（历史上 10/34 工具不一致）→ benchmark `run` 的源码门禁会拒绝，这是**正确行为**；不要用 `--allow-unmatched-api-source` 冒充当前代码结果。
- 私有校园会话（`browser_session_authorized=false`、`conn_zykt=service_unavailable`）未授权 → Level 2/3 的“真实课表/座位”不可验证；必须如实标注。

### L3 Harness 调度层

- 当前源码在沙箱内：`HARNESS_FAILED`，根因已定位为 `spawn EPERM`（见 L0），不是产品 bug。
- 待验证：沙箱外 API 的真实 turn 是否恢复（本轮正在进行）。

### L4 模型 / 规划层（历史实测 + 本轮修复）

- BAS-05 真实批次 0/3 完全成功（2 部分、1 不可验证）；引用交付不稳定、`web_fetch` 被拒后重复调用。
- 已做的 `web-recovery.mjs` 同主机短路只减少重复外部请求，未提升任务成功率（不要宣称提升）。
- 2026-10-17 第二轮：长任务遇到流式异常会整轮失败且丢失已完成工作。根因是 SDK 默认重试码不含
  `MALFORMED_RESPONSE`/`STREAM_CLOSED`，已在 profile 与桥接适配器显式配置重试策略；A/B 证据见 4.10。

### L5 验证脚本漂移（本轮已修）

- `tools/verify_real_scenarios.py`：迁移 CLI 已拒绝 `--batch-id`（import 自己生成 `mig_*`），且脚本用系统 Python 而非 `apps/api/.venv` → 已修（见第 4 节）。
- Benchmark 内置 `harness-dispatch` 层在当前沙箱因 pytest tmp 目录权限失败（环境限制，非产品失败；`--skip-direct-tools` 仍会跑该层，失败不阻断场景执行，但会污染批次摘要）。

---

## 3. 当前主要开发目标（P0）

以真实任务完成能力为最高标准，按四级推进：

1. **自主联网**：「帮我搜索浙江大学最近的公开学术活动，并告诉我来源。」
   - 判据：真实 tool call 轨迹（web_search / web_fetch / 浏览器）→ 读到真实页面/搜索结果 → 回答含真实 URL 与标题；不得凭记忆、不得编造。
2. **校园工具调用**：「查询我明天有哪些课。」
   - 判据：真实课表数据或明确说明“本机缓存/学校会话不可用”；不得编造课表。
3. **多工具协同**：「结合我明天的课表，帮我安排两个小时自习。」
   - 判据：课表 + 可用时间约束 + 计划预览；无实时座位数据时不得假称查到空位。
4. **长任务执行**：「整理这门课最近的学习资料并安排复习计划。」
   - 判据：多步工具链、状态保存、失败恢复、产物有真实材料依据。

每一级必须用真实模型 + 真实工具链跑通才算完成；Mock/脚本化结果只能证明调度链路，不能计入能力。

---

## 4. 本轮已完成的修复与测试记录

### 4.1 接管后的第一个真实阻塞：沙箱内 Harness 无法启动（环境层，非产品）

- 现象：当前源码 API 上的真实 turn 在 ~0.9 s 内 `HARNESS_FAILED`，0 次模型调用；同一 checkout 在 10:39 还能真实运行。
- 定位：给 `runner.mjs` 的 catch 加了一个**默认不输出**的诊断（写 stderr，stderr 默认丢弃，只在 `CAMPUS_RUNNER_STDERR` 打开时可见）；
  `runtime.py` 的 runner 环境只在 `CAMPUS_TURN_DEBUG=1` 时透传该开关。得到 `TURN_FAILED {"name":"Error","code":"EPERM","brief":"spawn EPERM"}`。
- 结论：DSH 沙箱内 Node 无法以管道 stdio 拉起 SDK 运行时子进程（官方文档记录的沙箱边界），**不是产品缺陷**。
  把 API 服务放到沙箱外启动后，真实 turn 立即恢复（见 4.2）。
- 相关改动（诊断，可选、默认关闭）：`apps/agent-runtime/runner.mjs`、`apps/api/app/agent_bridge/runtime.py`。

### 4.2 真实任务 L1（用户 P0 第一级）——**端到端真实完成**

任务原文：`帮我搜索浙江大学最近的公开学术活动，并告诉我来源。`
环境：当前源码 session + 真实 provider `prv_c68d2878` / `deepseek-flash`；隔离 API `127.0.0.1:8017`（临时数据库、源码指纹一致）；真实公网与校内网络。
证据：`var/takeover-tasks/L1-20261017-124006/`（完整事件轨迹 + 截图 + 快照）。

| 指标 | 实测 |
|---|---|
| turn 状态 | succeeded |
| 端到端耗时 | 80.8 s（含冷启动） |
| 工具调用 | 15 次：`web_search` ×3、`campus.sync(notices)`、`web_fetch` ×2、`load_tools(browser)`、浏览器 `navigate` ×4 / 截图 / 网络请求 / find |
| 网页抓取失败与恢复 | `web_fetch` 对 `www.zju.edu.cn`、`lsi.zju.edu.cn` 返回 `WEB_BLOCKED_URL`（10.x 私网地址策略）；**Agent 自主改用浏览器工具打开 ZJU 页面并成功读取** |
| 最终回答 | 列出 14 条 2026-10-10～10-16 的浙大学术活动（日期/标题/原始页面链接），来源指向科研院「学术活动预告」栏目 `rd.zju.edu.cn/cs/list.htm` |
| 来源可核实性 | 17 个引用 URL 中 15 个的路径出现在**真实浏览器快照**里；14 条日期全部出现在工具结果中；2 个未在快照（1 个来自 web_search 来源，1 个为另一主机页面） |
| 人工干预 | 0 次（普通只读联网自动执行，符合 UX 原则） |

**结论：Level 1 真实通过。** 不是模型记忆、不是 Mock：有真实 `web_search`/浏览器轨迹、真实页面快照与 HTTP 200 网络记录，回答引用可回溯到工具结果。

### 4.3 真实任务 L2（用户 P0 第二级）——工具链真实，数据/会话缺口如实披露

任务原文：`查询我明天有哪些课。`
环境：当前源码 API + `var/personal.db` 的**真实数据副本**（33 门课、147 作业、269 资料），隔离 API `127.0.0.1:8018`。
证据：`var/takeover-tasks/L2-20261017-124250/`。

| 指标 | 实测 |
|---|---|
| turn 状态 | succeeded |
| 端到端耗时 | 35.4 s |
| 工具调用 | 11 次：`campus.academics` ×2、`campus.sync` ×3、`campus.query`、`courses.list`、`connections.status`、`campus.status`、`load_tools`、浏览器打开 zdbk 登录页 |
| 真实发现 | 本机教务快照课程/课表为空且学期未确认（`campus.academics` 返回“尚未确认当前学期”）；`campus.sync` 对 zdbk/eta courses 返回 `UNSUPPORTED`；学在浙大（xzzd）有课程清单但**无上课时间/星期**；智云课堂连接器不可达 |
| 最终回答 | 明确说“无法确认明天（2026-10-18 周日）的课表，也不打算猜测”，逐条说明缺什么数据，并请在浏览器窗口完成校园登录后继续 |
| 编造风险 | 未发现（没有凭空给出课程/时间/教室） |
| 人工干预 | 0 次自动查询；登录属于真实用户操作（Agent 主动打开登录页，未索取密码） |

**结论：Level 2 的“工具调用 + 不编造”通过；“真实课表端到端”仍未通过，阻塞在已授权校园会话（`browser_session_authorized=false`、`conn_zykt=service_unavailable`）。** 这与合同要求一致：缺凭证时明确标记，不用模拟结果冒充。

### 4.4 真实任务 L3（用户 P0 第三级）——工具链真实，计划引擎缺口暴露并被修复

任务原文：`结合我明天的课表，帮我安排两个小时自习。`
证据：`var/takeover-tasks/L3-20261017-124343/`（修复前）、`var/takeover-tasks/L3-20261017-125011/`（修复后复测）。

| 指标 | 修复前实测 |
|---|---|
| turn 状态 | succeeded |
| 耗时 | 65.2 s |
| 工具调用 | 17 次：`campus.academics` ×2、`campus.sync` ×3、`campus.query`、`courses.list`、`assignments.list`、`availability.read`、`plans.preview`、`connections.status`、`campus.status`、`campus.credentials.status`、`load_tools`、浏览器打开登录页、`glob` |
| 真实发现 | 明天 2026-10-18 是周日；本机无当前学期课表快照；`campus.sync(courses)` 对 zdbk/eta 返回 `UNSUPPORTED`；作业缓存是 2025 年旧数据；`plans.preview` 返回 `CONFLICT`（本机未配置长期可用时间） |
| 最终回答 | 如实说明无法确认明天是否有课（不猜），给出 A/B/C 三个时段方案和 2 小时拆分建议，要求用户确认“明天有没有课/想学什么”，并说明登录后可读真实课表 |
| 编造风险 | 未发现（方案明确标记为“若明天没课”的预案） |

**发现的真实产品缺口**：`plans.preview` 在本机未配置 `availability` 时直接 `CONFLICT`，而工具 schema 明明支持 `temporary_windows`；`availability.read` 的描述还写着“未配置时请用户提供本次可用窗口”。结果是 Agent 无法产出任何**日历级**计划，只能给文字方案。

**修复**（`apps/api/app/agent_bridge/tools.py`）：当本机没有持久化可用时间、且本次调用没有给 `temporary_windows` 时，工具层用一次性的默认窗口 08:00–22:00 生成预览，并在 `warnings` 明确告知“仅本次临时、不写入长期偏好、接受前应告知用户”。REST `/plans/preview` 契约不变，不伪造长期偏好。

### 4.5 真实任务 L4（用户 P0 第四级）——真实资料 + 真实交付物，重试后完成

任务原文：`帮我整理这门课最近的学习资料，并给我安排一个复习计划。`
证据：失败 `var/takeover-tasks/L4-20261017-124449/`；成功 `var/takeover-tasks/L4-20261017-124633/`。

| 指标 | 第一次 | 第二次（重试） |
|---|---|---|
| turn 状态 | **failed** | **succeeded** |
| 耗时 | 82.7 s | 124.4 s |
| 模型调用 | 14 次后失败 | 完整多步 |
| 工具调用 | 24 次（`materials.list/read`、`assignments.list`、`plans.preview`、`todo_write`…） | 33 次（含 `connections.sync`、`jobs.read`、`artifacts.export`） |
| 失败原因 | `PROVIDER_INVALID_RESPONSE`：DeepSeek 流式响应格式不完整（第 14 次调用，14.6 s） | — |
| 最终交付 | 空回答（已完成的工具工作全部丢失） | 识别出「大学物理（甲）Ⅱ」；读取 10 份真实资料（PDF/PPT 解析文本）；产出 PDF 268,321 B（`%PDF-` 魔数，sha256 与工具回执一致）+ Markdown 11,160 B；如实说明缺第 13–15 章资料、可用时间未配置、同步 job 仍在排队 |

**结论：Level 4 真实完成（重试后）。** 交付物是真实文件（已按 sha256 核对），内容依据来自真实课程资料缓存，不是模型空想。
**遗留问题（未修，下一轮优先）**：长任务在提供商流式响应异常时会整轮失败并**丢失已完成工作**；当前只有“用户重试”这一条恢复路径，没有 in-turn 重试/检查点续跑。这是 Level 4 稳定性的最大风险。

### 4.6 本轮修复：`plans.preview` 无可用时间时的任务阻塞（真实瓶颈）

- 现象：L3/L4 第一次运行都撞到 `plans.preview → CONFLICT 尚未设置可用时间`，Agent 只能给文字方案，无法产出可执行/可接受的日历计划。
- 原因：`tools.py` 的 `plans.preview` 分支把 `temporary_windows` 直接透传（None），计划引擎在缺少 availability 时抛 409；而工具 schema 与 `availability.read` 描述都暗示可以用“本次窗口”替代。
- 修复：工具层一次性默认窗口 08:00–22:00 + `warnings` 披露；不持久化、不改 REST 契约、不放宽写操作确认。
- 验证：`python -m pytest tools/campus_bench/test_build_tool_map.py tools/campus_bench/test_runner.py -q` → **101 passed**；API 计划/Agent 子集见 4.7；L3 修复后真实模型复测见 4.7。

### 4.7 回归测试（本轮实测）

| 套件 | 结果 |
|---|---|
| Benchmark 单测（`tools/campus_bench/test_build_tool_map.py` + `test_runner.py`） | **101 passed**（3.4 s） |
| API 计划/Agent 子集（plan actions、plans API、agent gateway、agent tools、agent constraints、plan campus constraints） | **154 passed**（51.3 s） |
| 新增回归：`test_preview_without_saved_availability_uses_one_disclosed_temporary_window` | **15 passed**（`test_agent_plan_actions.py` 3.5 s） |
| 真实场景 BAS-03（benchmark runner，当前源码 API） | 部分成功；`source_grounded` aligned，5/5 引用命中（批次 `TAKEOVER-BAS03-N1C-20261017`） |
| Runtime 边界单测（`npm run test:audit`，apps/agent-runtime） | **93 passed**（原 87 + 新增 6 条 fetch 白名单策略测试，2.7 s） |
| 校园 fetch transport 真实网络审计（`audit-campus-web-fetch.mjs`，无模型） | **5/5 检查通过**；ZJU 公开站点可读、公网可用、私网/回环仍拒绝（`docs/audit/campus-web-fetch.json`） |
| 真实 L1 复测（fetch 白名单后，API 8021） | succeeded，**65.0 s / 12 次工具调用 / 19 个引用全部可回溯**（对比修复前 80.8 s / 15 次；证据 `var/takeover-tasks/L1-20261017-130424/`） |
| 浏览器产物专项验证（`verify-browser-outputs.mjs`，真实 Edge） | **ok=true，4/4**：截图落 workspace、绝对路径拒绝且无写入、快照不落源码树 |
| 真实模型视觉任务（浏览器→截图→`read_image`→`publish_file`） | **succeeded，114.9 s / 25 次工具调用，`read_image` 0 报错**，422.7 KB PNG 已发布为会话文件（`var/takeover-tasks/browser-visual-20261017-131917/`） |
| API 浏览器桥接测试（`tests/test_campus_browser_bridge.py`） | **15 passed**（5.6 s） |
| 回答侧引用核对单测 + runner 复用测试 | **26 passed**（`test_response_grounding.py` + `test_agent_runner_reuse.py`） |
| 真实 L1 回归（启用引用核对后，API 8024） | succeeded，**41.8 s / 14 次工具调用**；journal 出现 `response_grounding: aligned, checked=14, grounded=14`，回答未被追加噪声 |
| 真实 BUS-04（fetch 白名单后，API 8023，n=1） | succeeded，48.8 s / 6 工具调用 / **0 工具错误**；产出 intl.zju 14 条真实活动清单（日期/地点/链接）；benchmark 记为“不可验证”后经**原始 journal 重放验证 14/14 引用可核对**，属评估轨迹截断的观测限制（见 4.15） |
| 真实 MULTI-04（校园+公网综合，n=1） | **部分成功**，126.6 s / 14 工具 / 10 模型调用；campus notices（如实标注 LIMITED_SCOPE）+ 公网来源；1 次 `WEB_BLOCKED_URL` 后成功回退浏览器读官方页面 |
| 真实多轮任务（同一会话 2 轮：计划预览 → 改到下午） | **两轮均 succeeded**；第 1 轮 44.8 s / 8 工具，第 2 轮 **13.5 s / 1 工具**；真实生成新预览 `pln_5771d9e0` v3 并说明旧候选未变（`var/takeover-tasks/multi-turn-20261017-132940/`） |
| 浏览器回归门禁刷新（`verify-browser.mjs`，真实 Edge） | **ok=true，13 项检查全过**（密码/任意脚本按当前策略断言拒绝） |
| 原生 provider 协议验证（`node apps/agent-runtime/verify-official-provider.mjs`，含新增 malformed-stream 重试用例） | **ok=true**（修复后；A/B 见 4.10） |
| 真实 L4 长任务复测（重试策略修复后，API 8021） | succeeded，91.0 s，32 次工具调用，PDF 218,585 B + MD 8,564 B（证据 `var/takeover-tasks/L4-20261017-125742/`） |
| 真实 BUS-04 回归（benchmark runner，当前源码 API 8021，n=1） | **部分成功**：33.8 s，6 工具 / 6 模型调用；`campus.query`/`campus.sync` + `web_search` + 2 次 `web_fetch` 均被 `WEB_BLOCKED_URL`；未回退浏览器，未达完全成功（与 Codex 历史批次同类，无退化）。批次 `TAKEOVER-BUS04-N1-20261017` |
| Benchmark 内置离线 harness-dispatch 层（本会话放宽文件策略后） | **passed（mock_provider_real_api_tool_round_trip）**，说明此前的失败是受限沙箱的临时目录权限问题，不是产品失败 |

### 4.8 L3 修复后真实复测（验证 `plans.preview` 修复真的改善了任务）

- 证据：`var/takeover-tasks/L3-20261017-125011/`（API `127.0.0.1:8021`，当前源码，真实 provider）。
- 结果：succeeded，80.8 s，22 次工具调用；**生成真实候选计划** `pln_0840fc46`（version 2），
  窗口 09:00–11:00（临时默认窗口），三个时间块 + 合计 102 分钟；
  随后 `plans.apply` 被正确拦截为 `APPROVAL_REQUIRED`（写操作需要用户接受，符合安全边界）。
- Agent 明确披露：`CAMPUS_TIMETABLE_UNCERTAIN`（课表/校历缓存缺失）、临时窗口未写入长期偏好、
  教务 courses 同步 `UNSUPPORTED`、需要用户接受后生效；并给出改时段/补足 120 分钟/登录后按真实课表重排的选项。
- 对比修复前（4.4）：从“只能给文字方案 + CONFLICT”变为“真实日历级候选计划 + 明确确认点”。这是本轮**可复现的能力提升**。

### 4.9 环境/进程卫生（新发现）

- 上一任 Agent（Codex）留下了 **8019 端口上的旧源码 API 进程**（系统 Python，12:05 启动，无当前源码指纹）。本轮 L3 首测误连该服务得到无效结果（证据目录已重命名为 `var/takeover-tasks/_INVALID-L3-ran-against-stale-8019-server/`，**不要引用其结论**）；随后改用 8021 复测。不要杀非本会话创建的进程，改用空闲端口。
- 用户自己的旧服务仍占 8000；本会话按轮次使用 8017/8018/8021/8022/8023 等当前源码服务（每个轮次重启以加载改动，旧端口进程由本会话停止）。
  **当前源码 + 真实数据副本的 API 现在运行在 `127.0.0.1:8024`**（`tools/run_takeover_api.py`，state `var/takeover-state/realdata-6`，
  含引用核对与浏览器产物落盘修复）；如不需要，结束监听该端口的那个 Python 进程即可（它是本会话启动的，可安全停止）。

### 4.10 长任务的流式异常 in-turn 重试修复（本轮第二个真实瓶颈）

- 现象：L4 第一次运行在第 14 次模型调用遇到 `PROVIDER_INVALID_RESPONSE`（DeepSeek 流式响应不完整）→ 整轮 `failed`、空回答，已完成 24 次工具调用的工作全部丢失；只能靠用户重试。
- 分层定位（L3 Harness/模型层，不是工具层）：SDK 的 `dsh-llm-retry` 默认 `retryableCodes` 只有
  `EMPTY_RESPONSE / RATE_LIMIT / SERVER / TIMEOUT / TRANSPORT`；而 `dsh-llm-deepseek` 对截断/损坏的 SSE 抛
  `MALFORMED_RESPONSE` / `STREAM_CLOSED`，**不在默认可重试集合里**。我们的 profile 也没有显式配置 `retryPolicy`，
  桥接用的 `CampusModelAdapter` 也没有暴露 `providerRetryPolicy`。
- 修复（使用上游公开 API，不改上游源码）：
  1. `apps/agent-runtime/prepare-profile.mjs`：给 `llm-deepseek` 配置 `retryPolicy.retryableCodes` 追加
     `MALFORMED_RESPONSE`、`STREAM_CLOSED`（保留原有默认码；重试次数/退避用 SDK 默认）。
  2. `apps/agent-runtime/campus-plugin.mjs`：`CampusModelAdapter.providerRetryPolicy()` 返回同口径策略，
     让受控桥接路径行为一致。
- A/B 证据（受控本地 SSE 端点，无付费调用，`verify-official-provider.mjs` 新增用例）：
  | 条件 | 结果 |
  |---|---|
  | 无修复（stash 掉两处配置） | 断言失败：`finalResponse` 为空，malformed 流未被重试，整轮失败（与 L4 首次症状一致） |
  | 有修复 | `ok=true`：同一 session 内 malformed 流被重试 1 次后继续，模型请求 4 次（1 次坏流 + 3 次正常），真实文件工具写/读链路仍正确，最终回答含 `Actual native provider tool marker 926` |
  证据：`var/v2-official-protocol-01nQzd/evidence.json`。
- 真实回归：L4 在修复后重跑 **succeeded**（91.0 s / 32 次工具调用），产出 PDF 218,585 B + MD 8,564 B；本次还成功用了 `plans.preview`、`tasks.build`、`artifacts.export`，并创建了 2 条本地复习提醒（可撤销的本地写操作）。
- 边界：这只覆盖“同一模型请求可重试”的瞬时流错误；**不**覆盖模型请求本身无效或多轮上下文丢失，也**不**替代检查点续跑。若重试后仍失败，整轮仍会失败（如实上报）。

### 4.11 验证脚本漂移修复明细

`tools/verify_real_scenarios.py`（真实场景驱动）在接管时已不可运行，修了 3 处**真实缺陷**：
1. 迁移 CLI 现在拒绝 `import --batch-id`（import 自己生成 `mig_*`），脚本仍传该参数 → 删除；
2. 脚本用系统 Python 启动 uvicorn/迁移，而 API 依赖在 `apps/api/.venv` → 改用项目 venv 并显式设置 `PYTHONPATH`；
3. `prepare()` 失败后 `finally` 读不存在的 `api.log` 会二次崩溃，掩盖原始错误 → 加存在性保护。

### 4.12 本轮发现但未修的次要问题

- Benchmark 内置 `harness-dispatch` 离线层在受限沙箱内因 pytest 临时目录权限失败（环境限制）；本会话文件策略放宽后可正常跑。
- `schedule_create` 这类本地可逆写操作目前不需要一次用户确认（与“本地可撤销修改自动执行”一致），但应在回答中明确告知已创建哪些提醒；本轮 L4 做到了。
- 视觉任务成本偏高（打开页面→截图→读图→失败排查共 25 次工具调用/114.9 s）；可考虑在浏览器提示中给出更省的等待/截图策略。
- `campus.query(courses)` 在会话过期时以 `isError=true + data.records(缓存)` 形式返回，模型可用但语义易被误解为完全失败；
  评估改为 `status=partial`（见 4.16）。

### 4.13 校园公开域名白名单：让 `web_fetch` 直读 ZJU 页面（本轮第三个真实瓶颈）

- 现象：L1 首轮 `web_fetch` 对 `www.zju.edu.cn`、`lsi.zju.edu.cn` 返回 `WEB_BLOCKED_URL`（校园 DNS 解析到 10.x），只能回退浏览器：
  15 次工具调用 / 80.8 s，且多一次 `load_tools`+截图+`read_image` 失败；BUS-04 回归中也因同一原因只拿到部分来源。
- 修复（安全边界明确）：新增项目自有 fetch transport `apps/agent-runtime/campus-web-fetch.mjs`
  - 复用上游 `HttpFetchProvider`，只替换域名解析器：**白名单内的精确主机**（9 个 ZJU 公开站点）允许解析到校园私网地址，
    但继续拒绝 loopback / link-local / unspecified / multicast / IPv4-mapped 危险地址；
  - 非白名单主机仍走上游 `resolvePublicAddresses`（公网单播才放行），私网 IP 字面量继续 `WEB_BLOCKED_URL`；
  - 上游传输仍做地址钉扎 + 同源重定向限制（跨域重定向仍拒绝），白名单不能变成开放代理。
  - `prepare-profile.mjs` 禁用 base 的 `web-fetch-http`，注册本项目 provider。
- 无模型审计（真实网络，`node apps/agent-runtime/audit-campus-web-fetch.mjs`，报告 `docs/audit/campus-web-fetch.json`）：
  | 用例 | 结果 |
  |---|---|
  | `https://www.zju.edu.cn/` | ✅ HTTP 200 可读 |
  | `http://rd.zju.edu.cn/cs/list.htm` | ✅ HTTP 200，3,470 字符真实活动列表（438 ms） |
  | `https://example.org/` | ✅ HTTP 200（公网仍可用） |
  | `http://10.203.4.70/`（私网字面量） | ⛔ `WEB_BLOCKED_URL` |
  | `http://127.0.0.1:8021/...`（回环字面量） | ⛔ `WEB_BLOCKED_URL` |
- 单元测试：`node --test tests/campus-web-fetch.test.mjs` → **6 passed**；`npm run test:audit` → **93 passed**（原 87 + 新增 6）。
- 真实 L1 前后对比（同一任务、真实模型、当前源码 API）：

  | 指标 | 修复前（L1-20261017-124006） | 修复后（L1-20261017-130424） |
  |---|---:|---:|
  | 状态 | succeeded | succeeded |
  | 耗时 | 80.8 s | **65.0 s（−20%）** |
  | 工具调用 | 15 次（含浏览器 4 次 navigate、截图、read_image） | **12 次（全部 web_search / web_fetch，无浏览器）** |
  | 失败工具 | `read_image` 能力拒绝 | 仅 2 次非白名单 ZJU 子域 `WEB_BLOCKED_URL`（agent 继续用其他来源，未中断） |
  | 引用可核实性 | 15/17 URL 路径出现在真实快照 | **19/19 引用 URL 出现在工具结果中（0 ungrounded）** |

**结论：Level 1 不再依赖浏览器回退即可读官方页面；这是有 before/after 真实证据的能力提升。**

### 4.14 浏览器模型可见产物落盘到会话工作区（修复 `read_image` 被拒 + 源码目录污染）

- 现象（L1 首轮真实轨迹）：`browser_take_screenshot` 把图片写到 `apps/agent-runtime/`（源码目录）；随后模型调用 `read_image`
  被能力策略拒绝（`CAMPUS_CAPABILITY_DENIED`），白花一次工具调用，也无法把截图交付给用户。
- 分层定位（工具/文件权限层）：浏览器 daemon 属于 **API 进程**（不是 runner 子进程），旧实现以 `CAMPUS_BROWSER_HOME`
  （私有目录，能力策略禁止模型读取）+ Playwright MCP 的“workspace root = 进程 cwd”解析相对文件名，导致截图落在源码目录。
- 修复：
  1. `apps/api/app/agent_bridge/browser.py`：启动浏览器 daemon 时传入 `CAMPUS_BROWSER_WORKSPACE`（App 工作区）。
  2. `apps/agent-runtime/browser-host.mjs`：上游 `outputDir` 与文件名解析根目录改为 `<workspace>/browser-outputs`；
     转发调用时用 `_meta.cwd` 钉住该目录；绝对文件名降级为 basename（浏览器 action 策略本身也拒绝模型传入绝对路径）；
     页面快照/内联内容读取路径同步更新（保持模型可见的页面正文不被截断）。
- 验证：
  | 验证 | 结果 |
  |---|---|
  | 专项真实 Edge 验证 `node apps/agent-runtime/verify-browser-outputs.mjs` | **ok=true，4/4**：相对名截图落在 workspace、绝对路径被策略拒绝且无写入、页面快照留在 workspace、源码目录无残留 |
  | 真实模型端到端（浏览器打开 ZJU 英文站 → 截图 → `read_image` → `publish_file`） | **succeeded，114.9 s / 25 次工具调用**；`read_image` **0 次报错**，模型真实描述了截图内容（页面标题/导航/分页/活动区 0 高度），并把 422.7 KB PNG 发布为会话文件；证据 `var/takeover-tasks/browser-visual-20261017-131917/` |
  | 文件落点检查 | 截图/快照只在 `var/takeover-state/realdata-5/workspace/browser-outputs/`；`apps/agent-runtime/` 无 `*.png`、无 `browser-outputs` |

**结论：浏览器视觉能力现在真实可交付（截图可读、可发布），且不会再污染源码目录。**
代价：视觉任务本身较重（25 次工具调用/114.9 s，含等待渲染与多轮截图排查），后续若频繁使用可优化提示与等待策略。

### 4.15 回答侧引用核对：不隐藏、不改写地标记未核实链接（P2 答案验证）

- 背景：BUS-04 真实运行产出了完整活动清单（intl.zju.edu.cn，14 条），benchmark 却判“不可验证”。用**原始 journal**（未截断的 44 KB
  工具结果）重放后发现：14/14 引用 URL 都能在工具结果中找到（那 2 个“缺失”链接就在抓取到的列表页 HTML 里），
  **benchmark 的持久化轨迹对长工具结果做了截断**，导致评估器看不见，属于评估观测限制，不是模型编造。
- 但同时存在真实风险：真实回答里可能出现模型“按 ID 规律推测”的链接。因此新增回答侧核对，原则是**只披露、不改写**：
  - `apps/api/app/agent_bridge/response_grounding.py::citation_grounding_summary`：抽取最终回答中的 URL，
    与本轮 journal 工具结果做 exact-URL / path 匹配；全部命中 → `status=aligned`（回答原样保留）；
    有未命中 → 在回答末尾追加一句“来源核对：本轮回答引用了 N 个链接，其中 K 个未能在本轮工具结果中找到对应访问记录……请谨慎引用”，
    **不删除也不重写任何链接**；
  - `apps/api/app/agent_bridge/runtime.py`：在窄渲染器（作业列表）之后接入；每次核对结果写一条 `response_grounding` journal 事件
    （含 checked/grounded/ungrounded/status，不含链接内容）。
- 验证：
  | 验证 | 结果 |
  |---|---|
  | 单元测试（`apps/api/tests/test_response_grounding.py`） | 5 条用例覆盖：无链接不介入、精确命中、相对路径命中、未命中披露且回答原样保留、重复链接只计一次 |
  | 与 runner 复用测试一起跑 | **26 passed**（`test_response_grounding.py` + `test_agent_runner_reuse.py`） |
  | 真实 BUS-04 journal 离线重放 | `checked=14, grounded=14, ungrounded=0, status=aligned`，回答未被改写/未加噪声 |
  | 真实 L1 回归（API 8024，启用核对后） | 见 4.7 回归表 |
- 边界：这是**披露式**核验，不是事实正确性判定；路径匹配可能把同路径不同 query 视为命中；不改变 benchmark 评分标准。

### 4.16 多轮长任务延续 + 真实 stale 课表缓存（P2 上下文保持验证）

- 真实任务（同一会话两次用户输入，API 8024，真实 provider）：
  1. `结合我明天的课表，帮我安排两个小时自习。`
  2. `把自习时间改到下午 14:00–16:00，其他尽量保持不变；说明你是否真的改成了新预览。`
- 结果：**两轮均 succeeded**；第 1 轮 44.8 s / 8 次工具调用（`campus.academics`、`campus.query(courses)`、`plans.preview`×2、
  `assignments.list`、`plans.apply` 被 `APPROVAL_REQUIRED` 正确拦截）；第 2 轮 **13.5 s / 仅 `plans.preview` 1 次工具调用**。
- 第 2 轮真实生成了新预览 `pln_5771d9e0`（version 3，周日 14:00–16:00），并说明旧候选 `pln_c2d55dc1`（09:00–11:00）仍未被覆盖/删除、
  仍未生效；说明它复用了上一轮的会话上下文、计划对象和约束，而不是重新开始。证据：`var/takeover-tasks/multi-turn-20261017-132940/`。
- **重要发现（Level 2 的新可能）**：`campus.query(capability=courses)` 在教务会话过期时返回 `AUTH_REQUIRED`，但错误信封的
  `data.records` 里带有 **2026-10-16 18:06 抓取的 zdbk 课表缓存**（`stale=true`，18 门课，全部在周一至周五）。
  Agent 正确使用了这份 stale 缓存并明确标注“数据过期、未确认当前学期、可能缺周末/补课”。这意味着：
  - Level 2「明天有哪些课」在**无 live 登录**时也可能给出有依据的答案（必须带 staleness 警告），不再是“本机无任何课表数据”；
  - 工具层可改进点：`AUTH_REQUIRED` + 可用 stale 缓存时，返回 `status=partial`（数据+警告）比 `isError=true` 更准确，
    可减少“有缓存却像失败”的误判（下一轮评估）。
- 该轮也再次验证 runner 复用：第 2 轮 13.5 s 无冷启动。

### 4.17 刷新 `verify-browser.mjs` 回归门禁（验证脚本漂移修复）

- 旧脚本与当前浏览器安全策略冲突：仍断言“模型可 `browser_evaluate` 读写 localStorage、可向密码框输入”，而策略已禁止这两类操作。
- 刷新内容：密码输入 → 断言 `BROWSER_AUTH_REQUIRED` 且不回显秘密；任意页面脚本 → 断言 `BROWSER_APPROVAL_REQUIRED`（重启后仍然拒绝）；
  删除依赖 `browser_evaluate` 的 localStorage 持久化断言，保留 cookie 会话持久化与提交回执检查；其余检查不变。
- 结果：`node apps/agent-runtime/verify-browser.mjs` → **ok=true，13 项检查全过**（真实 Edge，无模型调用）。
  至此上一轮记录的唯一“验证脚本过期”项已关闭。

### 4.18 **Level 2 真实端到端打通**：课表迁移 + 自动重登 + 缓存保留（本轮核心成果）

- 发现的真实断点（三层，逐层修复）：
  1. **Agent 工具拿不到课表**：`campus.sync/query(courses)` 走连接器适配器，返回 `UNSUPPORTED`。真正的课表属于
     workbench 时代 `CampusManager`（`connectors/campus/campus.py`）的 snapshot，Agent 工具层从未迁移——正是“业务模块依赖旧工作台”的遗留问题。
  2. **有可用缓存却像完全失败**：会话过期但缓存可用时，`campus.query` 信封是 `status=error`（见 4.16）。
  3. **失败的 sync 会清空缓存**：`CampusService._sync_locked` 在解析失败时写入空白行覆盖旧缓存；登录成功时 `login()` 按设计清空旧账号快照；
     随后失败的 sync 又把“清空状态”持久化，造成数据丢失。
- 修复：
  1. `apps/api/app/campus_workspace.py`：新增 `timetable_records()`（只读 snapshot，无网络）与
     `sync_timetable(allow_recovery=...)`（**仅**用已有连接或一次有界已存凭证自动重登，然后执行真实只读教务同步；不打开浏览器、不索要密码）。
  2. `apps/api/app/agent_bridge/tools.py`：`campus.query/sync(courses)` 连接器返回 `UNSUPPORTED` 或空记录时，回退到教务课表 snapshot；
     `campus.sync` 可触发有界同步，`campus.query` 保持纯缓存读取。
  3. `apps/api/app/agent_bridge/tools.py::_campus_query_envelope`：错误+可用缓存 → `status=partial`（保留 data/error/警告），不再谎报完全失败。
  4. `connectors/campus/campus_service.py`：解析失败或 scope 未命中导致取不到旧缓存时，用 `read_latest` 保留最近缓存（`CACHE_RETAINED`）；
     `campus_repository.py` 新增 `read_latest()`。
- **真实端到端验证（API 8029，真实学校会话）**：
  | 指标 | 实测 |
  |---|---|
  | 用户输入 | `查询我明天有哪些课。` |
  | 状态 / 耗时 | **succeeded / 33.5 s** |
  | 工具调用 | **3 次**：`campus.academics` → `campus.sync(courses)` → `campus.academics` |
  | 真实数据 | `campus.sync` 返回 **36 条真实 ZDBK 课表记录**，`fetched_at=2026-10-17T13:46:43+08:00`（刚同步，不是缓存猜测） |
  | 最终回答 | **“明天（2026-10-18，周日）你没有课”**，并列出本周周一至周五的真实课程、时间、地点；如实说明校历为推算值、并指出周二两门课时间冲突需到教务核对 |
  | 人工干预 | 0 次（自动重登复用已保存凭证；未要求用户登录/复制任何内容） |
  | 证据 | `var/takeover-tasks/L2-20261017-134619/`、状态 `var/takeover-state/realdata-11/`（snapshot 36 条） |
- **结论：Level 2 由“工具链真实但无数据”升级为真实完成**（真实学校课表 + 日期解析 + 来源/边界说明）。
  注意：`campus.academics` 在该会话仍返回空 agenda（校历/term 解析未补齐），但 `campus.sync` 的记录已足够回答并按日期过滤；
  这属于后续可优化点，不影响本轮真实结论。
- 风险与边界（如实记录）：
  - 自动重登使用的是本机已保存的校园凭证（应用设置中已授权 auto_relogin）；每次 `campus.sync(courses)` 在会话失效时可能触发一次真实登录，
    已受冷却时间约束，但**频繁任务仍可能触发学校限流**。下一轮应缓存“本轮已登录”状态或复用 runner 会话。
  - 连接器能力（xzzd/zykt 的作业/资料）仍依赖本地连接器服务（8765/8766 未启动）；本项修复不改变这一边界。
  - 课表 snapshot 的 `stale/complete` 标记仍偏保守（本次 `freshness=stale`、`complete=false`），回答中已披露；可继续校准。

### 4.19 回归测试（本轮新增/复跑）

| 套件 | 结果 |
|---|---|
| `test_campus_readonly_boundary.py` + `test_agent_tools.py`（新增 5 条：缓存保留 ×2、信封 partial、课表只读 ×2） | **67 passed** |
| 真实 L2（修复前 → 修复后） | 修复前 46–96 s 且回答“查不到/无法确认” → 修复后 **33.5 s / 3 工具 / 36 条真实课表 / 明确回答** |
| 真实 L3 复测（同一修复后，API 8029） | **succeeded，43.5 s / 8 次工具调用**；按真实课表（第 5 周仅周一至周五有课）给出 10-18 19:00–21:00 两小时方案，基于真实作业（常微分方程，截止 10-21 23:59）；生成候选计划 `pln_2e4ae828`，`plans.apply` 正确拦截为待用户接受；0 人工干预（`var/takeover-tasks/L3-20261017-134803/`） |
| 工具路径单测：课表优先 snapshot（连接器不重复触发） | 新增 1 条，`test_agent_tools.py` **68 passed** |
| 真实 L2 两轮连续任务（会话复用验证，API 8030） | 第 1 轮 **31.6 s / 3 工具**（一次真实同步，`fetched_at 13:50`）；第 2 轮 **6.7 s / 1 工具（仅 `campus.academics`）**，无重复同步/登录，正确回答 10-19 周一课程与第 6 教学周（`var/takeover-tasks/l2-repeat-20261017-135107/`） |

### 4.20 课表工具路径收敛 + 第二轮会话复用验证（本轮）

- 问题：上一版 `campus.query/sync(courses)` 先走连接器（必然 `UNSUPPORTED` 且会触发一次多余自动登录、清空刚填的 snapshot），再回退到课表 snapshot。既慢又有缓存抖动风险。
- 修复（`apps/api/app/agent_bridge/tools.py`）：`capability=courses` 直接走迁移后的课表 snapshot（`sync` 用有界同步，`query` 纯缓存读）；只有 snapshot 为空时才回退旧连接器缓存。
- 真实两轮连续验证（同一会话，API 8030）：
  | 轮次 | 输入 | 状态/耗时/工具 | 结果 |
  |---|---|---|---|
  | 1 | 查询我明天有哪些课 | succeeded / **31.6 s** / 3 次（`campus.academics` → `campus.sync` → `campus.academics`） | 真实同步（`fetched_at 2026-10-17 13:50`）；回答明天周日无课，列出周一真实课程、教师、地点；披露校历为推算 |
  | 2 | 那后天（2026-10-19）呢？顺便告诉我这学期第几周 | succeeded / **6.7 s** / **1 次工具（`campus.academics`）** | 无重复同步/登录；正确列出周一 2 门课、空档与周次（第 6 教学周），并标注校历未核验 |
- 结论：连续问课表时，第 2 轮只读缓存/日历，不再触发学校请求；课表会话在进程内可复用。跨 API 重启后的首次同步仍会触发一次自动重登（见下一轮风险项）。

### 4.21 回归扩大化发现并修复一个真实鲁棒性缺陷（本轮收尾）

- 把 API 相关测试扩大到 8 个套件后，发现 `test_campus_browser_bridge` 失败：`sync_timetable` 只捕获 `CampusError`，
  底层客户端若抛出其他异常（测试 fake 的 `AttributeError`）会直接冒泡成“工具坏了”。
- 修复（`apps/api/app/campus_workspace.py`）：`sync_timetable` 增加兜底捕获，返回结构化
  `TIMETABLE_SYNC_FAILED`（`retryable=true`）并保留上次课表缓存；新增对应单测。
- 同时把该校验“认证恢复”语义的测试改用 `grades`（连接器依然支持的能力）：`courses` 已切到迁移后的课表 snapshot，
  不应再用连接器恢复逻辑代表它；测试意图（同一安全 workspace、有界重试、无需再次确认）保持不变。
- 回归结果（本轮实测）：
  | 套件 | 结果 |
  |---|---|
  | API：agent tools / campus boundary / agent gateway / runner reuse / connection sync / browser bridge / credentials / auth resume | **193 passed**（87.9 s） |
  | Benchmark 单测（tool map + runner） | **101 passed**（3.1 s） |

---

## 5. 下一轮任务与阻碍

**当前能力结论（2026-10-17，真实模型 + 真实工具链）**

| 级别 | 用户任务 | 结论 | 证据 |
|---|---|---|---|
| L1 自主联网 | 搜索浙大最近公开学术活动并给来源 | **真实完成**：`web_search` + `web_fetch` 直读 ZJU 官方页面（白名单），65.0 s / 12 次调用，19/19 引用可在工具结果回溯 | `var/takeover-tasks/L1-20261017-130424/` |
| L2 校园工具 | 查询明天有哪些课 | **真实完成**：自动复用已保存凭证重登 → 真实同步 36 条教务课表 → 回答“明天周日没有课”并列本周课程/时间/地点，附校历与冲突说明；**同一会话第二轮仅 1 次工具调用、6.7 s，无重复登录**；0 人工干预 | `var/takeover-tasks/L2-20261017-134619/`、`var/takeover-tasks/l2-repeat-20261017-135107/` |
| L3 多工具协同 | 结合课表安排两小时自习 | **真实完成**：按真实课表（第 5 周仅周一至周五有课）给出 10-18 19:00–21:00 方案，基于真实作业截止时间；生成候选计划并正确等待用户接受；0 人工干预 | `var/takeover-tasks/L3-20261017-134803/` |
| L4 长任务 | 整理资料 + 复习计划 | **真实完成**：真实资料读取 + PDF/Markdown 交付 + 缺口披露；首次因流式异常整轮失败，本轮已加 in-turn 重试（A/B 证据 4.10），修复后重跑 succeeded | `var/takeover-tasks/L4-20261017-124633/`、`var/takeover-tasks/L4-20261017-125742/` |

**阻碍（需要用户/环境解锁，不能伪造）**
1. **学在浙大/智云课堂连接器**：本地连接器服务（8765/8766）未启动，作业/课件的实时同步仍不可用；需要在启动器里显式启动对应平台。
   教务（zdbk）课表与已实现能力可经自动重登真实读取（见 4.18）。
2. 用户 8000 端口旧服务仍在运行（旧代码、无源码指纹）；真实 benchmark 必须用当前源码服务（本轮 8029 或用户重启）。
3. 真实校历/调课数据源、实时空教室/座位数据未接入（能力缺口，不得假装）。

**下一轮建议（按影响排序）**
1. **跨重启的登录与限流保护**：同一 API 进程内已可复用课表会话（4.20 第 2 轮 6.7 s / 1 工具，无重复登录）；但 API/runner 重启后的首次 `campus.sync(courses)`
   仍会触发一次真实自动重登。应持久化“最近一次成功登录时间”（或在 runner 生命周期内保持 workspace 连接），并用 3 次真实重复任务验证不会触发学校限流。
2. **校历/学期解析补齐**：`campus.academics` 仍依赖默认校历推算（回答里已披露 `verified:false`）；接入真实校历后，周次与“今天/明天”的日期映射才可标记为已核验，
   也能让 `campus.academics` 的 agenda 在同步后直接返回事件。
3. **xzzd/zykt 连接器启动引导**：作业/课件实时同步依赖本地连接器服务（8765/8766 未启动）；补齐自动启动或明确的一键提示。
4. **benchmark 观测限制（不改评分）**：真实 BUS-04 回答 14/14 引用在原始 journal 可核对，但持久化轨迹截断了长工具结果；
   应改进证据保留/核验方式（保留可校验的摘要或哈希），而不是改评分标准。
5. 长任务检查点续跑（若 in-turn 重试仍失败）：保留已完成的工具结果与部分回答，而不是整轮丢弃。
6. 视觉任务成本优化（25 次工具调用/114.9 s）与性能基线：L1 41.8–65.0s、L2 31.6s（首次）/6.7s（会话内复用）、L3 43.5s/8 calls、L4 91.0s/32 calls、BUS-04 48.8s/6 calls、MULTI-04 126.6s/14 calls。
7. 扩大 fetch 白名单（按证据逐项）与 `rd.zju.edu.cn` 仅 http 的提示优化。

---

## 6. 交接规则（后续 Agent 必读）

1. 先 `git status`；工作树中不属于你的改动不要 checkout/stash/reset；提交只 `git add` 自己的文件。
2. 真实 Agent turn 必须用**沙箱外**启动的 API（沙箱内 `spawn EPERM` 是环境限制，不是产品失败）。
3. 不要杀用户正在运行的服务（8000 端口旧进程）。
4. 诊断网络用 Node `fetch`，不要用 curl。
5. Benchmark 数字必须附批次 ID、源码指纹、provider/模型、重复次数；mock 与 real 分开记。
6. 修复要有 before/after 证据；不能证明改善时如实写“不确定/未验证”。
