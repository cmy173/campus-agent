# V2 本地性能基准

日期：2026-10-14。范围：本地自有开销（启动、首个可见事件、工具耗时、请求次数、额外确认次数）。
远程模型波动按构造排除：模型提供方是**本机受控夹具**（本地 HTTP 服务），不触达互联网。

命令（可在同一台机器重复执行，覆盖同一标签文件）：

```powershell
python tools/benchmark_local_overhead.py --label current --runs 2
```

## 基准任务

隔离环境（独立 `STUDY_DATABASE_URL` / `STUDY_FILES_DIR` / `STUDY_SECRETS_DIR` / `CAMPUS_AGENT_HOME`），
真实 uvicorn + 真实 `apps/agent-runtime` SDK，受控模型驱动一个普通本地任务：
`write` 写文件 → `read` 读回 → `pwsh` 执行本机 Python 计算 → 汇总回答。每轮两次。

## 实测结果（两次独立运行，2026-10-14）

| 指标 | baseline（16:54） | current（17:2x） |
| --- | --- | --- |
| 进程启动 → `/api/v1/health` 200（中位数） | 2758 ms | 2476 ms |
| 入队 → 首个可见事件（中位数） | 273.5 ms | 266 ms |
| 入队 → 轮次结束（中位数，本机受控模型） | 6219 ms | 5695.5 ms |
| 每轮模型请求次数（中位数） | 6 | 6 |
| 每轮工具调用数（中位数） | 3 | 3 |
| `write` 工具耗时 p50 | 52.6 ms | 54.5 ms |
| `read` 工具耗时 p50 | 49.7 ms | 49.6 ms |
| `pwsh`（Python 计算）耗时 p50 | 690.4 ms | 628.0 ms |
| 待确认动作数（普通本地任务应为 0） | 0 | 0 |
| 落盘文件字节与预期一致 | 是 | 是 |
| 每次请求携带的工具 schema 数 / 字节 | 未测 | **83 / 63406 B**（第 3 次运行，标签 toolschemas） |

证据：`var/v2-bench-_g6mxube/benchmark-baseline.json`、`var/v2-bench-bymalltp/benchmark-current.json`、
`var/v2-bench-ajqrxmpv/benchmark-toolschemas.json`（本地未提交）。

### 工具 schema 的每轮成本（条件 3 的“按需发现”依据）

第三次运行记录到受控模型**每次请求**都收到 **83 个工具 schema、约 63 KB**（仅 schema 文本，
不含对话）。这直接量化了当前“全部 schema 每轮入上下文”的代价，约合 1.6–2 万 token 量级。
上游 `0.2.0-rc.2` 已有的按需形态是 agent preset 的 `tool-presentation`（`native`/`ptc`/`both`，`ptc` 下原生 schema 不下发，改为生成的 SDK + `run_code`），本机 profile 目前未挂该行，
因此仍按 `native` 全量下发。这是条件 3 尚未完成项的具体证据，而不是主观判断。

**选定的实现路径（已核对固定版本 `0.2.0-rc.2` 的公开类型）**：`@deepseek-ai/dsh-tools` 的
`ctx.tools.restrict({ allow, deny })` 提供**调用方 agent 作用域内的全局工具掩码**（多条限制取交集，
作用域内注册的工具仍可见），`ctx.tools.presentAs(mode)` 提供作用域级呈现方式。因此“按需发现”可在
`apps/agent-runtime/campus-plugin.mjs` 内实现为：默认用 `restrict` 保留常用校园工具与核心通用工具，
再注册一个作用域内的加载工具按需放开其余分组；不需要第二套主循环，也不必启用 PTC。
下一增量按此实现并用真实运行时验收（含现有受控集成回归不被削弱）。

### 本轮实现进度与实测发现（未完成，运行时保持原状）

已落地但**尚未接线**：

- `apps/agent-runtime/campus-tool-groups.mjs`：分组定义（browser 26 / mcp 3 / team 5 / jobs 4 /
  planning 1，共 36 项实测可延后）、纯函数 `deferredNames`（按**黑名单**工作，未分类的新工具保持
  可用=失败开放）、以及 `load_tools` 加载工具的完整定义（含强制 `output.schema`）。
- `tools/verify_tool_discovery.py`：真实 API+Harness+受控模型，直接在**线缆上**断言默认请求缺少
  这些分组、加载后出现、且仍隐藏其余分组；工具未接线时以 exit 2 与明确原因报告，不伪装成功。

实测确认与实测发现：

1. **掩码机制有效，但必须在 agent 作用域安装**。在插件 `apply(ctx)`（preset 作用域）调用
   `restrict` 对会话 agent 的请求**无影响**（实测仍 84 项 / 64644 B）；改到 SDK `create` 的
   `setup(agentCtx)` 后，实测同一受控任务从 **83 项 / 63406 B 降到 45 项 / 33887 B
   （−47%，约 29.5 KB）**，且 3 个工具调用全部成功、额外确认 0。证据
   `var/v2-bench-obj8a_o_/benchmark-ondemand.json`、`var/v2-bench-dlaoug8l/benchmark-ondemand2.json`。
2. **包装 SDK 的 `create/resume` 注入 `setup` 会破坏工具派发**：实测每次工具调用只写入
   `tool/call` 事件，随后在 `tools/pre-execute` 之前停滞（API 日志无 `tool-lease` 请求），
   连普通 `pwsh` 也一样；关闭整个特性后仍复现，证明是包装本身而非掩码逻辑。已**回退**
   `campus-sdk.mjs`，运行时恢复原状（实测同一探针任务 83 项工具、`pwsh` 正常返回）。
3. 掩码实现需避免重入：`restrict` 会触发 `tools/change`，再同步 `restrict` 会递归；现改为
   `applying` 守卫 + `setTimeout(0)` 延后，且在工具派发之外更新。

**下一增量的精确问题**：为会话 agent 找一个不改写 `agents.create/resume` 选项的安装点
（候选：agent 创建后的公开句柄 `handle.agent.ctx`、agent-loop 的首轮前钩子、或在 agent 作用域内
由 `campusReady` 服务发起的注册），把 `installOnDemandTools(agentCtx)` 装上去，然后用
`tools/verify_tool_discovery.py` 验收；在那之前本文件与台账都按“条件 3 未完成”记录。

### 第 6 轮补充：更多安装点已排除，并补上诊断能力

继续按上面的候选排查（每项都以真实运行判定）：

- **`agent/created` 内同步安装**：死锁。该事件监听器“在创建 resolve 之前被 await”，在窗口内改
  注册表会让 agent 创建永不完成（journal 只有 2 条事件、无 session 文件、turn 停在 running）。
- **`agent/created` + `setTimeout(0)` 延后安装**：仍死锁。
- **仅注册加载工具到 `agent.ctx`（掩码关闭）**：仍死锁 → 说明卡住的是**在 agent 作用域注册工具**，
  而不只是 `restrict`。
- **插件 `apply(ctx)` 注册加载工具 + 每 agent 只做 `restrict`（第 6 轮实现的分体设计）**：仍死锁。

四类安装点全部以真实运行证伪后，本轮**再次回退接线**，运行时保持已验收原状（实测 83 项 /
63406 B、3 个工具调用成功、额外确认 0、API 全套 320 passed）。分体设计保留在
`campus-tool-groups.mjs` 中（`registerToolLoader` / `installToolMask`），未接线即无副作用。

诊断能力（本轮新增并保留）：runner 的 stderr 原先固定为 `DEVNULL`，导致插件级死锁**无任何日志**，
只能靠推断。现新增 `CAMPUS_RUNNER_STDERR=<path>` 显式开启捕获（默认仍丢弃，避免运输细节与模型输入
进入日志），句柄按轮次持有并在回合结束/提前返回时关闭，避免描述符泄漏；文件以追加方式打开。
已实测：真实产品路径下开启后文件被创建（健康运行内容为空），且基准与 API 网关 24 项测试通过。

**结论（下一增量的起点）**：问题不在“装在哪个钩子”，而在**该组合下 agent 作用域的工具注册/限制本身**
会阻塞创建流程。后续应先做一个最小复现（在 agent 作用域只 `register` 一个空工具、不做任何
`restrict`），据此判断是上游限制还是本仓库的组合方式问题；若不成立，则退回上游 PTC 呈现模式
（`tool-presentation` 的 `ptc`，需要 host 侧 `dsh-ptc-runtime-node`，基座已挂载该行）。

### 第 10 轮诊断：埋点本身失效，先解决可观测性再谈死锁

本轮尝试给插件加埋点以定位停在哪一步，结果暴露了**两个独立的可观测性缺陷**，它们使此前的“无日志”
不能被解释为“插件静默阻塞”：

1. **`CAMPUS_*` 环境变量传不到 runner**。`apps/api/app/agent_bridge/runtime.py` 构建 runner 环境时用的是
   白名单（PATH/PATHEXT/SystemRoot/…），自定义 `CAMPUS_*` 开关会被丢弃。因此任何依赖 `CAMPUS_*` 的
   诊断/开关在 runner 内都是未设置状态（本轮临时加过透传，已回退）。
2. **runner 的 stderr 捕获可能不是插件所在进程的 stderr**。捕获机制本身已验证可用：用 `spawn_owned`
   起 `node -e "console.error('DIRECT-STDERR-MARKER')"` 并把 stderr 指向目标文件，日志内容正确落盘
   （实测 `captured: 'DIRECT-STDERR-MARKER\n'`）。但在真实运行中，即使把 `console.error` 放在插件
   `apply()` 第一行（无 env 守卫），捕获文件仍为 0 字节。注意依赖里存在
   `@deepseek-ai/dsh-subprocess-local`（带 `ensure-spawn-helper.mjs` postinstall），**插件很可能运行在
   子进程/孙进程里**，因此它的 stdout/stderr 不是 runner 的那一份。

**给接手者的正确顺序**（不要把“文件为空”当成“没有异常”）：

1. 先证明可观测性：确认 `CAMPUS_*` 能到达真正的插件进程（必要时在 `runtime.py` 增加
   `CAMPUS_*` 透传，并同时验证子进程继承），并把埋点写到**明确的绝对路径文件**（`appendFileSync`）
   而不是依赖 stderr。
2. 再证明 `apply()` 被进入（模块加载期与 apply 期各打一个标记）。
3. 只有以上成立后，才逐步加 `schemas()` / `restrict()` / `register()` 标记定位死锁点。
4. 最小复现优先级：agent 作用域只 `register` 一个空工具、不做 `restrict`；再单独试 `restrict`。
5. 回退路径：若 agent 作用域注册/限制在本组合下不可用，改用上游 PTC 呈现模式，并相应调整受控集成脚本。

本轮结束时 **工作树已回到已验收状态**（`git status` 干净、HEAD 为文档提交），
实测运行时基线不变：83 项工具 / 63406 B、3 个工具调用成功、额外确认 0。

结论：同环境重复运行差异在 ±12% 内，且方向为持平或略好；**没有出现可重复的明显回退**。
`pwsh` 是本机进程启动开销（约 0.6 秒）主导，`write`/`read` 约 50 ms 级别；普通本地任务不产生额外确认。

## 尚未完成：改造前的对照

真正的“改造前”应当是与首轮系统在同一台机器、同一受控任务下的同类测量。当前状态：

- 首轮记录里没有留下可比的时间数字（`docs/agent-baseline.json`、`docs/agent-v2-baseline.json` 只记录源码指纹/路由/库计数）。
- 旧 `campus_agent` 应用已按合同退役（本轮删除），不能就地复测。
- 保留的首轮 workbench（`C:/Users/mingxiang/.codex/worktrees/campus-agent-core/campus-agent/workbench`）
  仍带独立 venv 与 node_modules，但**实测无法在本轮驱动**：该目录下 `tempfile.mkdtemp` 超过 120 秒不返回
  （进程 CPU 持续占用、无输出），属该路径的环境问题而非产品问题。已终止相关自有进程。

最小解锁条件（任选其一即可补齐对照，均不需要真实账号或付费）：

1. 把首轮 workbench 复制到本地普通目录（例如 `D:/AI/campus-bench-before`）后运行
   `CAMPUS_BENCH_ROOT=<该目录> python tools/benchmark_local_overhead.py --label before --runs 2`；
2. 或从快照 `D:/AI/campus-agent-goal-backups/20261013-v2-start` 还原旧应用，在同一环境用同一夹具测量。

> 本节状态：**已解锁**。第 1 条已执行（workbench 复制到工作区内 `.test-tmp/v2-round/before-workbench`），
> 见下一节「改造前后对照」；同时也暴露出一项可重复的明显回退，正在按合同处理。

在此之前，本文件的“当前”数字与重复性结论成立；条件 9 的对照半项保持未完成。

## 改造前后对照（条件 9，2026-10-14 第二轮：真实测得）

### 方法

| 项 | 值 |
| --- | --- |
| 机器/夹具 | 同一台机器、同一个本地受控模型夹具（无互联网、无远程模型波动） |
| 受控任务 | `campus_connections_status` 一次真实工具调用 + 汇总回答，每轮两次 |
| 改造前 | 保留的首轮 workbench，复制到 `.test-tmp/v2-round/before-workbench` |
| 改造后 | `HEAD=6690c6f` 的独立 worktree `.test-tmp/v2-round/after-clean`（干净提交状态，不混入任何未提交改动） |
| 命令 | `CAMPUS_BENCH_ROOT=<目录> python tools/benchmark_local_overhead.py --task campus-status --label <before\|after>-campus --runs 2` |

**为什么不用原来的 `write→read→pwsh` 任务做对照**：首轮系统**在线缆上只声明 16 个校园工具**
（`campus_connections_status` 等，实测 `tool_schema_names`），模型可见面里**没有任何本地文件/命令工具**，
受控夹具发出的 `write` 调用无法被它执行（实测该任务的轮次以 `RUNTIME_FAILED` 结束）。
这是能力差异而不是耗时差异，因此改用两边都真实声明的 `campus-status` 任务；`local-files` 任务只在
改造后一侧记录（见前文表格）。

### 实测数字（同环境、同任务、同脚本）

| 指标 | 改造前（首轮 workbench） | 改造后（HEAD 6690c6f） | 差异 |
| --- | --- | --- | --- |
| 启动：进程起 → `/api/v1/health` 200 | 2961 ms | 2718.5 ms | 持平（两侧都在 2.2–3.0 s 波动） |
| 入队 → 首个可见事件 | 289 ms | 281.5 ms | 持平 |
| 入队 → 首个模型请求到达夹具 | **1781 ms** | **5062.5 ms** | **+3281.5 ms（可重复）** |
| 工具往返（夹具侧：请求工具 → 带结果的下一次请求） | 141 ms | 203 ms | +62 ms |
| 最后一个模型响应 → 轮次终态 | 101.5 ms | 476.5 ms | +375 ms |
| 入队 → 轮次结束 | 2023.5 ms | 5718.5 ms | +3695 ms |
| 每轮模型请求次数 | 2 | 2 | 持平 |
| 每轮工具调用 | 1（真实执行，回执 `{"status":"ok"}`） | 1（同上） | 持平 |
| 额外确认次数 | 0 | 0 | 持平 |
| 每次请求携带的工具 schema | 16 项 / 10771 B | 45 项 / 33861 B | +29 项 / +23090 B（已由按需发现从 83 项压到 45 项） |

证据：
`.test-tmp/v2-round/before-workbench/var/v2-bench-fso8qg55/benchmark-before-campus.json`、
`.test-tmp/v2-round/after-clean/var/v2-bench-zzbkkezo/benchmark-after-campus.json`（两侧 `ok: true`，
即任务真的跑通、工具真的执行、落盘/回执与确认数断言全部通过）。

### 结论：存在一项可重复的明显回退，定位到「每轮 runner 启动」

回退不是分散在各处，而是集中在**一个阶段**：入队到第一个模型请求之间多花 3.28 秒。同一夹具下
模型响应是即时的，事件投影也快（首个可见事件 281 ms），工具往返只有 203 ms。也就是说每轮有约 5 秒
花在“把这一轮送到模型面前”之前。harness 会话日志进一步确认内部执行很快：
`turn/start → turn/end` 在改造后是 483 ms、改造前是 323 ms（会话内 `request/header` 分别在 +208 ms / +24 ms）。

成因（均以配置与实测为据，不是推断）：

1. **每轮起一个全新 runner**：`apps/api/app/agent_bridge/runtime.py` 明确写着
   “A fresh runner resumes the same official JSONL session after each turn.”，因此启动成本是**每轮**支付。
   首轮同样每轮起新进程，所以回退来源不是“起了新进程”，而是**新进程要装的东西重了很多**。
2. **profile 从 16 行显式树改成完整 `@deepseek-ai/dsh-base` 包**：
   `apps/agent-runtime/prepare-profile.mjs` 固定 `bundles = ['@deepseek-ai/dsh-base']`，
   该包 `dependencies` 实测 **93 项**（运行时目录下 `@deepseek-ai/*` 共 289 个包）；
   改造前的 profile 是 `bundles: []` + 16 行手写树（timer/llm/session/system-prompt/tools/agent/…）。
3. **每轮都要真起浏览器 MCP 子进程**：profile 里 `campus-mcp-0` 指向 `campus-mcp.mjs` → `browser-host.mjs`，
   后者静态导入 `playwright` 与 `@playwright/mcp`（实测这两个导入就要 **539 ms**），且 `failOnStartupError: true`。
   这是**硬依赖**，两次受控实验证明不能简单拿掉：
   - 直接不生成 MCP 行 → runner 在 274 ms 内结束，连一次 `/internal/*` 桥接请求都没发；
   - 保留行但让子进程立即退出（`failOnStartupError: false`）→ runner 在 238 ms 内结束。
   （两次实验都在 `.test-tmp/v2-round/after-clean` 这个一次性 worktree 内进行，事后 `git checkout` 还原并确认干净。）

### 建议的修法（按收益排序，属运行时/profile 所有者范围）

1. **让浏览器 MCP 的生命周期跟随 `browser` 工具分组**：默认 profile 不再起浏览器 MCP，模型调用
   `load_tools({group:'browser'})` 时再起（这正是刚落地的按需发现的自然延伸），同时保留“加载失败不影响
   其他工具”的降级路径，避免现在这种“浏览器没起来 → 整个 runner 结束”。
2. **或每条会话只保留一个 runner 进程**，把后续轮次投进去（JSONL 恢复路径已经存在，可作为降级）。
   这一条能把整块每轮启动成本直接去掉。
3. 复测判据（必须同时满足）：`ok: true`、`first_request_ms_median` 回到约 1.8 s 量级、
   `tool_results` 仍为 `{"status":"ok"}`、额外确认仍为 0。

修复并复测之前，条件 9 记“对照已补齐、但存在可重复明显回退，按合同待诊断修复后验收”，不记为完成。

## 使用约定

- 不要把远程模型耗时混入本基准；需要远程数字时单独记录并标注提供方与时延。
- 不要为了数字好看而跳过正确性校验：本基准同时断言落盘内容与待确认动作为 0。
- `--runs` 越大越稳；标签文件写在同一临时目录，可直接比较两次产物。
