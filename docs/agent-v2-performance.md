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

在此之前，本文件的“当前”数字与重复性结论成立；条件 9 的对照半项保持未完成。

## 使用约定

- 不要把远程模型耗时混入本基准；需要远程数字时单独记录并标注提供方与时延。
- 不要为了数字好看而跳过正确性校验：本基准同时断言落盘内容与待确认动作为 0。
- `--runs` 越大越稳；标签文件写在同一临时目录，可直接比较两次产物。
