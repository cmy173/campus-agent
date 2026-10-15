# 按需工具发现：已接线与真实运行验证

2026-10-14。固定 Harness 0.2.0-rc.2，未修改上游代码。以下均为真实运行时与本地受控模型证据，不代表官方模型或学校登录验收。

## 使用方式

普通校园工具、文件、命令、搜索、调度和持续目标直接可用。模型需要浏览器、外部 MCP、子任务、后台命令或计划模式时，调用 `load_tools` 的 `group`（browser/mcp/team/jobs/planning），也可按具体工具名加载其所属分组。学生无需记住工具名。

默认 45 项 / 33861 B；加载浏览器后 71 项 / 54174 B；原默认为 83 项 / 63406 B。默认 schema 字节减少约 46.6%。39 项延后工具分为 browser 26、MCP 3、team 5、jobs 4、planning 1，另新增一个常驻加载工具。配置外部 MCP 后该组按实际注册变化。

加载状态属于当前活跃 agent；新进程恢复会话时重新按需加载。子 agent 加载会同步释放同一作用域祖先上本模块拥有的对应限制，避免父限制使子任务永远无法使用该能力。独立 agent 的加载状态不共享。上游额外限制不会被解除。

## 诊断与实现

绝对路径探针 `var/discovery-probe.log` 证明模块加载、apply 与 agent/created 都执行。只注册空工具的真实任务下发 84 项，单独 restrict 的任务下发 44 项，两者均实际执行 pwsh 成功。由此推翻“agent 作用域注册本身必死锁”的旧结论。

原实现的问题：`schemas()` 无参数读取全局目录，不代表当前 agent 继承的 preset 目录；应显式传 `schemas(agent)`。此外，每次变更后用定时器重新 dispose/restrict，会再触发 tools/change，形成自激更新。现在每个工具仅持有一个 exact disposer，只处理新增可见工具，忽略自身同步通知，加载时同步释放对应限制。没有 SDK create/resume 包装，没有第二套循环，也不需要环境变量诊断开关。

验收脚本原本用两个可见集合的交集声称证明“仍隐藏”，且选择浏览器组第一个工具却只处理 navigate，可能生成空响应。现在明确断言默认无延后工具、只出现浏览器组、实际 navigate 可见并执行，页面内容确实返回。原有效断言保留并增强。

## 实际验证

- `node apps/agent-runtime/verify-tool-groups.mjs`：公开上游 ToolRuntime 与真实 scope，默认面、子任务加载、独立作用域隔离、晚注册、MCP 重连、重复加载、额外能力策略均通过。
- `apps/api/.venv/Scripts/python.exe tools/verify_tool_discovery.py`：exit 0，3 次模型调用，真实 Edge 导航；`var/v2-discovery-bbr57_g5/evidence.json`。
- `apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py`：exit 0，103 次受控模型调用；`var/agent-integration-bvufvxs6/evidence.json`。广覆盖夹具先显式加载分组，实际工具数量断言计入新增 loader，原业务与产物断言未删除。
- 首次全量运行真实发现 200 条历史消息硬限制，按 CR017 修复后全量重跑成功；失败证据 `var/discovery-full-regression.log`。
- 调度集成 exit 0 / 6 次调用：`var/v2-schedule-runtime-kfuxsj3r/evidence.json`；原生目标 complete exit 0：`var/v2-goal-hCGGgO/evidence.json`。
- 隔离 API 322 passed；根回归与启动器 203 passed + 80 subtests（其中根回归 191，启动器 12）；前端 27 文件 / 149 passed；tsc -b、Vite build、契约 --check 与 dev.py --check 通过。

API 日志 `var/api-discovery-final-483fbad3f34d4ff9a748c924cf4de9b2/pytest.log`；根回归 `var/acceptance-72304ca946364dd184dccc8b825890a0/pytest.log`；前端 `var/discovery-web.log` 与 `var/discovery-build.log`。原有大 chunk 提示保留。证据、数据库、凭据与依赖不提交。
