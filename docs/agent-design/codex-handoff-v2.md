# Codex 交接提示词：Campus Agent V2 收尾

日期：2026-10-14。用途：把本文件内容作为提示词交给 Codex（或同类强 agent），与现有实现者协同完成 V2 剩余验收。
本文件是**可直接复制的驱动提示词**；执行时以仓库内文档与实际运行结果为准。

---

## 可直接复制给 Codex 的提示词（从这里开始）

```text
你是本项目的高级工程协作者。工作目录 D:\AI\campus-agent（Windows，Python 3.12，Node 24，Microsoft Edge）。
任务：与现有实现者协同，完成 Campus Agent V2 合同中的剩余验收项，直到第 8 节全部条件满足。

# 0. 先读，再动手（不要凭本提示词臆断现状）
按顺序读：
1. docs/agent-design/goal-v2-personal-assistant.md —— 执行合同（第 5 节能力范围、第 8 节完成条件、第 9 节记录规则）
2. docs/agent-v2-progress.md —— 唯一权威进度记录与验收台账（最后一节是最新状态）
3. docs/agent-v2-migration.md —— 旧能力→新实现→数据处理→验收→删除 台账
4. docs/agent-v2-performance.md —— 性能基准与按需工具发现的实测结论
5. docs/agent-design/tools-v2.md、docs/agent-design/campus-migration-v2.md、scheduling-v2.md、ui-redesign-v2.md
6. AGENTS.md（若存在）、README.md

然后用实际命令核对事实（git log/status、跑测试），不要相信文档里的旧数字。

# 1. 硬约束（违反即视为失败）
- **不得用模拟结果冒充真实验收**。受控模型/合成响应可以用于开发，但第 8 节第 4、5 条要求真实官方模型与真实学校登录态，缺账号/预算时保持"未完成"并列出最小解锁条件，不要降级为已完成。
- **不得删除有效断言来制造通过**。若某测试必须随退役实现删除，先确认等价覆盖已在新位置存在，并在 docs/agent-v2-migration.md 写明理由。
- **不得破坏已验收的运行时**。当前基线（实测）：API 320 passed、前端 27 文件 149 passed、根回归 191 passed + 80 subtests、tsc -b 与 Vite 生产构建通过、每轮下发 83 个工具 schema / 63406 B。改运行时后必须重跑相关验证；无法验证就不要提交。
- **单份实现**：不要新建第二套 Agent 主循环、第二个调度器或第二份会话事实源。上游固定 0.2.0-rc.2，不修改上游私有源码，只用公开接口。
- **不推送远端、不合并、不部署**。只做本地提交。
- **不提交**数据库、凭据、浏览器登录配置、node_modules/.venv/dist、var/ 证据目录、测试临时目录。
- 不输出 API Key、密码、Cookie；凭据只经运行进程环境使用。

# 2. 当前未完成项与已知事实（已在真机验证，勿重复走弯路）

## A（最难）按需工具发现：机制有效，但接线会死锁 —— 需要你解决
目标（合同第 5 节）：不能每轮把所有工具 schema 塞进上下文；常用校园工具直接可用。
现状：默认每轮下发 83 个工具 / 63406 B（实测，见 docs/agent-v2-performance.md）。
已实现但**未接线**的文件：
- apps/agent-runtime/campus-tool-groups.mjs —— 分组（browser 26 / mcp 3 / team 5 / jobs 4 / planning 1）、
  黑名单式判定（未分类工具保持可用）、load_tools 加载工具定义。
- tools/verify_tool_discovery.py —— 真实 API+Harness+工具执行，在线缆上断言默认隐藏/加载后出现/其余仍隐藏；
  未接线时 exit 2 并给出原因。**这是你的验收脚本**。
已实测有效：agent 作用域 restrict → 同一受控任务 83项/63406B 降到 45项/33887B（−47%），工具调用全部成功。
已实测**证伪**以下四类安装点（都会让 agent 创建永不完成：journal 只有 2 条事件、无 session 文件、turn 停在 running）：
  1) agent/created 内同步安装；2) agent/created + setTimeout(0) 延后；
  3) 仅向 agent.ctx 注册加载工具（掩码关闭）；4) 插件作用域注册加载工具 + 每 agent 仅 restrict。
建议路径：
  a) 先做最小复现——在 agent 作用域只 register 一个空工具、不做 restrict，判断是上游限制还是本仓库组合方式问题；
  b) 若 agent 作用域注册不可行，改用插件 apply(ctx) 注册加载工具 + 另一处安全的应用掩码时机；
  c) 若都不行，评估上游 PTC 呈现模式（@deepseek-ai/dsh-agent-tool-presentation，mode: 'ptc'/'both'，
     基座 @deepseek-ai/dsh-base 已挂载 ptc-runtime），注意 PTC 会改变模型调用工具的方式，
     现有受控集成脚本需要相应调整。
诊断已就绪：runner 的 stderr 默认丢弃；设 CAMPUS_RUNNER_STDERR=<path> 可捕获（tools 改动已在 runtime.py）。
交付要求：接线后 tools/verify_tool_discovery.py 必须 exit 0；同时不得降低既有能力（浏览器/MCP/子 Agent/调度
仍要能真正调用），必要时让受控集成脚本先调 load_tools。

## B 干净重建与交付（条件 1/10）
全新目录按锁文件可安装/构建/启动；日常运行不依赖旧工作树或旧服务器。
当前进行中：已用 git archive 生成全新目录 D:\AI\campus-agent-rebuild-v2（仅跟踪文件，679 个），
正在跑 tools/dev.py --setup。刚修了一个真实缺陷：dev.py 原先用 sys.executable 建 venv，
当该解释器是**别的项目 venv**时不可移植；且 Windows 杀软实时扫描会短暂锁住 pyvenv.cfg，
导致 venv 实际建好但命令非零退出。现改为 base_python() 定位基础解释器 + ensure_venv() 按
"解释器可用性"判定并有限重试。
你要做的：完成并**如实记录**该目录的 --setup → web 构建 → --check/--smoke-test → 代表性流程回归；
把结果写进 docs/agent-v2-clean-rebuild.json（或更新 docs/agent-clean-rebuild.json）与进度文档；
若失败，修产品而不是改判定标准。

## C 桌面全宽视觉回归（条件 8）
已有 390×844 窄屏证据；缺桌面全宽的真实视觉与无横向溢出验证。用应用自有持久浏览器（不要用开发期 Codex 浏览器）
实测 /chat 在 1280×900 与 1920×1080：聊天、工作区、刷新/草稿恢复、取消/错误态；截图 + DOM 尺寸证据。
注意：当前 IAB 可能无法完整截取原生表面，若如此就明确记录限制，不要用 DOM 尺寸冒充视觉验收。

## D 性能"改造前"对照（条件 9）
已有当前基线（tools/benchmark_local_overhead.py，本机受控夹具，排除远程模型波动）。
缺"改造前"：首轮未留可比时间数字，旧应用已退役；保留的首轮 workbench
（C:/Users/mingxiang/.codex/worktrees/campus-agent-core/campus-agent/workbench）在该路径下
tempfile.mkdtemp 超过 120 秒不返回（已判定环境问题）。最小解锁：把该 workbench 复制到普通本地目录，
用 CAMPUS_BENCH_ROOT=<目录> python tools/benchmark_local_overhead.py --label before --runs 2 测量，
再写对照结论。不要为了数字好看跳过正确性断言。

## E 真实旧库导入（条件 2 的数据部分）
tools/migrate_campus_data.py 已可用（inventory/import/rollback，只读源库、可重入、受保护回滚）。
新环境实测 inventory 可对 data/campus.sqlite3 只读运行。缺：选择要迁移的旧 owner 并执行真实导入 + 回滚演练。
不可再获取的数据（笔记/反馈/个人安排）必须显式字段映射、可重入、可回滚；不确定就保留原库不动并记录。

## F 真实服务试用（条件 4/5）—— 阻塞在用户配置，不要伪造
需要：本地配置官方 DeepSeek Key 与明确测试预算；用户在浏览器完成浙大登录（不把密码给模型）。
解锁后做三条真实自然语言场景（公开网页检索并引用、校园信息结合计划修改并撤销、附件读取并生成可打开产物）
以及真实校方登录态只读试用、真实搜索连通性。未解锁前不要标记完成。

## G CI 与文档（条件 10）
.github/workflows/tests.yml 已重写为 API 套件 + 根回归 + 契约新鲜度(--check) + 前端构建/测试。
剩余：把仍是首轮的说明文档归档到 docs/history/，保持一个权威进度记录；补齐工具使用文档与迁移/回滚报告。

# 3. 环境与测试规范（务必遵守）
- Python 测试**必须**先设置隔离环境，严禁对个人库跑测试：
  PYTHONUTF8=1
  STUDY_DATABASE_URL=sqlite:///<临时>/domain.db
  STUDY_FILES_DIR / STUDY_SECRETS_DIR / CAMPUS_AGENT_HOME 指向临时目录
- API 套件：cd apps/api && .venv/Scripts/python.exe -m pytest -q（当前应 320 passed）
- 根回归：仓库根 python -m pytest tests -q（当前应 191 passed + 80 subtests）
- 前端：cd apps/web && npx tsc -b && npx vitest run --maxWorkers=2（149 passed）&& npx vite build
- 真实 SDK 路径：tools/verify_agent_runtime.py（受控模型，必过）、tools/verify_schedule_runtime.py、
  tools/verify_tool_discovery.py、apps/agent-runtime/verify-goal.mjs
- 启动器：python tools/dev.py --check / --smoke-test --platforms
- 只清理自己创建的进程；不要终止用户正在运行的服务。

# 4. 工作方式
循环：核对事实 → 实现一个增量 → 相关验证 → 更新 docs/agent-v2-progress.md（含证据路径与命令）→ 本地提交 → 下一项。
不要重复无影响的全量测试，但改动运行时后必须复验。
可逆实现自行决策；只有新的重大范围歧义才询问。
遇到反复阻塞，按 Goal 规则处理并写清最小解锁条件，不要假装完成。

# 5. 交付格式
每个增量给出：改了什么、实际运行的命令与真实结果、证据路径、剩余工作。
区分"真实账号/真实模型证据"与"受控/合成证据"。不要用"装了包/有接口/文档写了"当作完成证据。
```

## 复制结束

---

## 给协调者的补充说明（不要把这一节发给 Codex）

分配建议：

| 优先级 | 任务 | 适合谁 | 理由 |
| --- | --- | --- | --- |
| 1 | A 按需工具发现死锁 | Codex | 需要读上游类型、构造最小复现、跨 Node/Python 调试，是当前最难且最有价值的开放问题 |
| 2 | B 干净重建与交付 | Codex 或并行 | 机械但有真实阻塞（安装耗时、环境问题），适合独立跑 |
| 3 | C 桌面视觉回归 | 任意 | 需要真实浏览器与截图判断 |
| 4 | D 性能对照 | 任意 | 已有一半，剩余是环境搬迁 + 复测 |
| 5 | E 旧库导入 | 需用户定 owner | 涉及用户数据，必须先确认迁移哪个 owner |
| 6 | F 真实试用 | **只能用户解锁** | 缺 Key/预算/学校登录，任何人无法代做 |
| 7 | G CI/文档 | 任意 | 收尾 |

与 Codex 协作时的分工纪律（照抄本项目并行协作约定）：各自只改自己领取的文件范围；跨模块改动给出精确集成点；
不互相覆盖；不推送远端；同一文件不同时改。建议 Codex 先领 A，实现者领 C/D/G。

不可外包给任何 agent 的部分：F（真实账号与预算）、E 中"选哪个 old owner"的用户决策。

## 协作状态与文件边界（2026-10-14，供 Codex 参考）

- 唯一实现者（本 Goal 会话）：**当前不持有任何未提交改动，工作树干净，HEAD = 文档提交
  `docs: point the clean-rebuild evidence at its permanent location`**。
- Codex 若看到 `campus-plugin.mjs` / `campus-tool-groups.mjs` / `runtime.py` 有未提交改动，那是
  **本会话为定位按需工具发现死锁而临时加入的埋点**，不是交付内容；我在收到询问后已全部回退
  （`git checkout` 三个文件，工作树随后干净，并实测运行时基线不变：83 项工具 / 63406 B、3 个工具调用、
  额外确认 0）。请**不要保留**这些埋点，以提交状态为准。
- 建议的写范围划分（避免互相覆盖）：
  - **Codex**：`apps/agent-runtime/campus-tool-groups.mjs`、`apps/agent-runtime/campus-plugin.mjs`、
    `apps/api/app/agent_bridge/runtime.py`、`tools/verify_tool_discovery.py`（即任务 A 全部相关文件）。
  - **实现者**：`apps/web/**`（任务 B 桌面视觉回归）、`docs/agent-v2-performance.md`（任务 C）、
    `.github/workflows/**` 与 `docs/history/**`（任务 F）。
  - **共享只读**：`docs/agent-design/goal-v2-personal-assistant.md`、`docs/agent-v2-progress.md`
    由谁改谁在提交信息里注明；`docs/agent-v2-progress.md` 按“追加一节”的方式写，避免行级冲突。
- 任务 A 的前置结论（务必先读 `docs/agent-v2-performance.md` 的“第 10 轮诊断”）：
  埋点本身此前是失效的——`CAMPUS_*` 环境变量到不了 runner（白名单），且 runner 的 stderr 捕获很可能
  不是插件所在进程（存在 `@deepseek-ai/dsh-subprocess-local` 子进程）。**先把可观测性做出来，再判死锁**，
  不要沿用“日志为空＝没有异常”的推断。
