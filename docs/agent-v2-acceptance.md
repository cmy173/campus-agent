# Campus Agent V2 验收报告（条件 1–10 逐条对照）

日期：2026-10-14。范围：`D:/AI/campus-agent`。本文是**验收总表**，逐条给出证据路径、命令与"真实 / 受控"标记；
细节在各专题文档：`docs/agent-v2-progress.md`（进度与增量）、`docs/agent-v2-migration.md`（迁移台账）、
`docs/agent-v2-performance.md`（性能与前后对照）、`docs/agent-design/tools-v2.md` 与
`docs/agent-v2-tool-discovery.md`（工具面与按需发现）、`docs/agent-design/ui-redesign-v2.md`（UI 改版指南）。

## 证据类型约定（贯穿全文）

| 标记 | 含义 |
| --- | --- |
| **真实** | 触达真实外部服务：官方 DeepSeek 模型/搜索（真实计费）、真实 Edge 页面与下载、真实旧库数据、真实调度器时钟 |
| **受控** | 本机夹具模型（无互联网、无计费）驱动同一产品路径：真实 uvicorn + 真实 Harness + 真实工具执行 |
| **配置** | 只读清点/静态核对（文件、路由、schema、契约） |

受控证据**不能**代替条件 4 要求的真实模型场景；反之，真实场景不覆盖全部工具面（见"已知限制"）。

## 逐条对照

| # | 条件摘要 | 状态 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 一套源码、一个启动命令、一个学生入口；全新目录按锁文件可安装/构建/启动 | **已验证（本轮在 HEAD 上重做）** | `docs/agent-v2-clean-rebuild.json`（`git archive` 679 文件）；本轮 HEAD 全新目录：`--setup` exit 0（53 锁定 wheel、web 180 包、runtime 540 包、0 漏洞）→ `--check` exit 0 → 全新 venv API 套件 **329 passed** → 全新 node_modules 下 `verify_agent_runtime.py` ok=true（83 次调用） |
| 2 | 旧能力台账逐项有迁移证据或明确放弃理由；旧 UI/重复代码移除；旧 URL 不再渲染旧 UI；历史源码与用户数据可恢复 | **已验证** | `docs/agent-v2-migration.md` 台账（47 个 `campus_agent` 路径、旧静态 UI、旧启动脚本已删）；快照恢复入口 `D:/AI/campus-agent-goal-backups/20261013-v2-start/RESTORE.md`；**真实**旧库导入+重入+回滚演练 `var/migration-rehearsal-20261014/` |
| 3 | 第 5 节能力有实际实现与最小闭环；文档列明插件、模型可见工具、provider、服务依赖 | **已验证** | `docs/agent-v2-tool-discovery.md`（默认 45 项 / 33861 B，加载浏览器后 71 项；分组与装载工具）、`docs/agent-tool-schemas.json`、`docs/agent-design/tools-v2.md`；**受控** `tools/verify_agent_runtime.py` ok=true（83 次调用覆盖工具/文件/文档/浏览器/MCP/子任务/取消/目标） |
| 4 | 真实 Harness 工具链 + 真实官方模型完成三条自然语言场景 | **已验证（真实）** | `tools/verify_real_scenarios.py`；**27 次真实计费请求**：s1 网页检索引用（`web_search`+6×`web_fetch`，5 个可核实 URL）、s2 校园信息→计划预览→接受生效→撤销（`plans.apply`/`plans.revoke`，`active`→`revoked`，`plans.read` 返回 null）、s3 附件→`todo-report.md` 可读产物并发布。证据 `var/real-scenarios-s1|s2|s3/` |
| 5 | 登录态浏览器可读取页面与下载文件；受控表单填写、一次确认、提交、防重；真实校外提交非必需 | **部分完成（唯一缺口：WebVPN 登录态）** | **真实** Edge：`verify_agent_runtime.py` 的浏览器流（真实导航、填表、下载 `browser-campus.txt`、提交前 `needs_user` 一次确认、pending review 唯一）；`tools/verify_browser_cancellation.py`、`apps/agent-runtime/verify-browser.mjs`。**真实校园只读试用**（`tools/verify_real_school_browser.py`，真实模型）：直连 `zdbk.zju.edu.cn` 本网络返回空响应（实测 68 字节）→ 模型按产品语义调用 `campus_webvpn_entry` 取得 WebVPN 地址 → 浏览器打开后 **302 跳转到 `webvpn.zju.edu.cn/login`**（“请使用上网账号登录”），即产品路径通、但**该浏览器当前无 WebVPN 登录态**。证据 `var/real-school-browser-20261014-203823/` |
| 6 | 子任务/持续任务/调度：真实执行、取消、失败、进程重启恢复；真实调度器 + 可控时间 | **已验证** | **受控** `apps/agent-runtime/verify-subagent-controls.mjs`（前台/可继续后台两种子 agent 并存、选择性中断：无凭据 403、有凭据 200、只中止 A 而 B 完成）、`verify-goal.mjs`（原生 goal 到 `phase=complete`）、`python tools/verify_schedule_runtime.py`（6 次调用）、`python tools/verify_scheduler.py` ok |
| 7 | 本地明确执行无需重复确认；预览不写入；外部提交前一次确认；错误/取消不显示成功；首轮回归通过 | **已验证** | **受控** API 套件 `apps/api/tests`（同意/浏览器归属/取消/应用与撤销/幂等/防重）；基准 `pending_confirmations = 0`；s2 预览在未被接受前 `plans.read` 仍为 `candidate` |
| 8 | UI 分层与换布局示例；同一聊天可搜索/查校园/操作浏览器/看文件与日历；桌面与窄屏无阻断；刷新保持状态 | **已验证** | `docs/agent-design/ui-redesign-v2.md`；`python tools/verify_ui_desktop.py`（**真实** Edge，1280×900 与 1920×1080 无横向溢出、输入区在视口内、工具栏可见、日历工作区可打开、刷新后草稿保留）；更早的 390×844 证据见同文档 |
| 9 | 记录改造前后同类本地受控任务的启动/首个可见事件/工具耗时/请求次数/额外确认；出现可重复明显回退则诊断修复 | **已验证** | `docs/agent-v2-performance.md`「改造前后对照」；**受控** 同一夹具同一任务：首轮 workbench → 改造后；发现并修复每轮 runner 启动回退（稳态轮 5.1 s → **0.41 s**）；`var/v2-bench-t4iou9ra/`、`apps/api/tests/test_agent_runner_reuse.py`（7 项） |
| 10 | 自动化/构建/干净启动/最终目录回归通过；交付真实与合成证据区分、已知限制、工具使用、迁移、UI 指南、进度记录 | **已验证** | `.github/workflows/tests.yml`（API 套件 + 根回归 + 启动器 + 契约新鲜度 + 前端构建/测试 + 运行时工具分组/子任务检查）；**本轮 HEAD 全新目录回归**：`--setup`/`--check` exit 0、全新 venv 329 passed、全新 node_modules 集成 ok=true；交付物：本文（真实/合成区分 + 已知限制）、`docs/agent-v2-migration.md`、`docs/agent-design/tools-v2.md`、`docs/agent-design/ui-redesign-v2.md`、`docs/agent-v2-progress.md`、`docs/history/README.md` |

## 已修复的真实缺陷（本轮独立复验中发现，均非文档层面）

1. **每轮 runner 启动回退**（条件 9）：`runtime.py` 每轮新起 runner，profile 又是完整 `dsh-base`（93 依赖）+
   浏览器 MCP 子进程，导致每轮多花约 3.3 秒。改为按会话跨轮复用同一 runtime 子进程后，稳态轮 **约 0.41 秒**。
2. **`verify-subagent-controls.mjs` 失效**：按需发现落地后 `team` 分组延后加载，受控模型直接调 `subagent`
   永远不会成功（20 秒超时）。补 `load_tools({group:'team'})` 后通过。
3. **迁移工具的操作陷阱**：`--batch-id` 可被 `import` 静默忽略，用自造 id 回滚会返回 `removed: false`（看似成功）。
   现在两处误用都明确报错。
4. **三处计数型断言**（附件流/文档流/浏览器流）把一次性 `load_tools` 结果算进本轮流；复用后该 step 不再重复而失败。
   改为断言本流自己的工具调用序列，更精确。

## 已知限制

1. **真实学校登录态未试用**（条件 5 唯一缺口）：需要用户在本机浏览器完成浙大登录，模型不接触密码。
   解锁后做只读试用（读页面、下载文件），不做对外提交。
2. **重新启动 API 后第一轮仍有一次冷启动**（约 4.8 秒，**每会话一次**）：完整上游 bundle + 浏览器 MCP 子进程构建。
   稳态轮已优化到 0.41 秒。可选后续优化：让浏览器 MCP 随 `browser` 分组懒启动，或裁剪 profile。
3. **真实付费证据只覆盖三条场景**（27 次请求）。全工具面的端到端回归使用受控夹具模型（无计费）；
   受控证据不能替代真实模型证据，二者在本文中分列。
4. **校园计划在没有本地学习任务时生成为空**：`plans.preview` 只产生 120 分钟容量预留、`blocks: []`，
   模型会明确要求课程挂载点与复习资料。这是数据缺失而非流程缺陷（`active`→`revoked` 已验证）。
5. **旧库导入范围**：仅支持 legacy schema 的 documents/chunks/tasks/document_sources/notice_analyses/connector_cache；
   笔记/反馈/计划在该 schema 中不存在，导入工具明确报告而不静默丢失。凭据不迁移。
6. **外部 MCP** 默认只有校园浏览器服务；其他服务器由用户在设置里配置（可从设置页做真实连通性诊断）。
7. **npm 安装脚本提示**：5 个包（koffi、node-pty、dsh-subprocess-local 等）的 install scripts 未被 allowScripts 覆盖，
   已观察并记录；干净重建仍然 exit 0 且真实 Harness 集成通过。

## 最小解锁条件

| 缺口 | 解锁条件 |
| --- | --- |
| 条件 5 真实学校只读试用 | 在**应用自有浏览器**里登录 `https://webvpn.zju.edu.cn/login`（上网账号 + 验证码；校外访问教务走 WebVPN，直连 zdbk 在本网络返回空响应）。无需把密码交给模型 |
| 更多真实付费场景 | 用户确认预算（此前授权 100 元，已用 27 次真实请求 + 条件 5 试用的少量请求） |

## 复现命令（本轮实测）

```powershell
# 受控：真实产品路径 + 夹具模型（无计费）
apps/api/.venv/Scripts/python.exe -m pytest -q                       # apps/api：329 passed
apps/api/.venv/Scripts/python.exe -m pytest tests tools/test_launcher.py -q   # 203 passed + 80 subtests
apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py      # ok=true
apps/api/.venv/Scripts/python.exe tools/verify_tool_discovery.py     # exit 0
apps/api/.venv/Scripts/python.exe tools/verify_schedule_runtime.py   # ok=true
apps/api/.venv/Scripts/python.exe tools/verify_scheduler.py          # ok=true
apps/api/.venv/Scripts/python.exe tools/verify_ui_desktop.py         # ok=true（真实 Edge）
node apps/agent-runtime/verify-tool-groups.mjs                       # PASS
node apps/agent-runtime/verify-goal.mjs                              # ok=true
node apps/agent-runtime/verify-subagent-controls.mjs                 # ok=true
apps/api/.venv/Scripts/python.exe tools/export_agent_contract.py --check      # exit 0（42 paths / 49 schemas）
apps/api/.venv/Scripts/python.exe tools/dev.py --check               # exit 0

# 真实：官方 DeepSeek 模型/搜索（真实计费，逐条断言见脚本）
apps/api/.venv/Scripts/python.exe tools/verify_real_scenarios.py --scenario all

# 性能对照
apps/api/.venv/Scripts/python.exe tools/benchmark_local_overhead.py --label current --runs 2 --task campus-status
```
