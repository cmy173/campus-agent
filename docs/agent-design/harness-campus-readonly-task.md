# 交给 DeepSeek Harness：校园只读任务闭环与认证恢复

你正在开发 `D:\AI\campus-agent`。上一轮文档导出、复习业务接线和 Harness 工具执行修复已经完成独立验收。本轮继续原定校园 Agent 目标：让模型通过现有校园能力完成有来源、可恢复的完整任务。请读取代码、建立复现、实际修复并运行验证，不要重新做上一轮导出功能，也不要只提交报告或提示词修改。

实施方是具备仓库开发权限的 DeepSeek Harness 会话；验收方是原 Codex 对话。你的自测结论不能代替独立验收。配套合同是 `docs/audit/harness-campus-readonly-acceptance.md`。

## 0. 基线与工作区

- 本任务编写时基线为 `650a9348e859ee24544c85fce1a3f397ae3e2345`，分支 `codex/personal-assistant-v2`，已有 PR #6。开始时核实实际 HEAD、工作区、适用 AGENTS.md 和并发写入者，不回退后续提交。
- PR 最新一次失败是 Python CI 没有安装实际诊断所需 Node 依赖，已在上述提交修复。重新读取实际 CI 状态；不能把正在运行写成全绿。本机网关测试 31 项通过。
- 工作区保留很多未跟踪审计材料，它们不是待删垃圾。禁止 reset/clean、stash 他人修改、覆盖整个目录。只改任务所需文件，默认不 commit/push/合并 PR。
- 所有自动化数据放 UUID 隔离目录，独立 DB、校园缓存、凭据目录、浏览器 profile 和端口；不得改个人数据库或普通浏览器 profile。
- 先核实开发权限。产品校园助手 profile 禁止通用命令执行不等于开发 Harness 无法运行测试；没有开发工具就报告缺失能力，不能修改产品权限给自己提权。

## 1. 已有证据与必须先读的代码

先读 `docs/audit/pr-validation.md`、`docs/agent-design/tools-v2.md`、`docs/agent-design/campus-migration-v2.md`、`docs/webvpn-analysis.md`、`docs/integrations.md`。其中部分文档包含历史工具数量或过时状态，以当前实现和分日期证据为准，不能照抄可用性声明。

重点路径：

- `connectors/campus/campus_service.py`、`connector_contracts.py`、`campus_repository.py`、`campus_network.py`、`campus_auth.py`、`webvpn_tunnel.py`、`connectors/` 内的教务、ETA 与公开通知适配器。
- `apps/api/app/campus_workspace.py`、`campus_domain.py`、`campus_schedule_constraints.py`。
- `connectors/adapters/{base,transport,xzzd,zykt}.py`，`apps/api/app/{connectors_registry,sync_engine,worker}.py`。
- `apps/api/app/routers/{campus,connections}.py`，`apps/api/app/agent_bridge/{tools,recovery,runtime,store}.py`。
- `apps/agent-runtime/{campus-plugin,tool-groups,capability-policy}.mjs` 与实际系统指令。
- 现有校园、同步、auth_resume、scope、分页、计划约束及公开网页测试。

先画出当前两条真实调用链：教务/ETA/通知经 CampusService；学在/智云经连接注册、同步任务和领域库。指出哪些步骤仍依赖 UI，哪些只是用户认证必需交互。两条链不等于必须合并为一个巨型类；允许保留稳定适配器并渐进抽取必要应用服务。

一个已经复现的契约冲突：

`CampusService.query` 是缓存读取，但 `CampusWorkspace.query(refresh=False)` 遇到认证错误时可能调用 `auto_relogin_locked`，继而调用 `service.sync`。基线隔离探针替换外部边界后得到 `query=1, auto_relogin=1, sync=1`，没有真实学校请求。这证明调用路径存在，不证明真实学校认证成功。部分现有测试与工具描述把自动恢复当作预期，因此不能只改一行就宣布完成。

本轮调整的收益是离线查询可靠、调用成本与认证时机可预测；代价是过去依赖隐式刷新的调用方需要明确同步或恢复。迁移策略：保留普通查询和工具名，默认 query 不访问远端；将已授权的自动重登与会话重新衔接放在显式 sync 或可信恢复入口。保留用户主动开启自动重登的设置、冷却、身份校验和撤销语义。提交可独立回滚的最小改动，不移除认证安全测试。

## 2. M0：冻结行为基线

先给简短计划，然后继续执行，无须等待日常实现选择确认。

记录实际 HEAD、目标源码哈希、修改文件、依赖版本、当前工具 schema、暴露分组与已有失败。先运行相关测试和合同 C01—C16；未有覆盖的补可复跑案例。分类报告：

1. 工具/业务层直接失败。
2. 模型看不到或不会选择工具。
3. 调用正确但 Harness/权限/参数/结果关联失败。
4. 工具成功但模型没消费结果。
5. 多步骤依赖、任务状态或恢复失败。
6. 外部数据、认证或网络条件缺失。

不得把后端常量 `OWNER=local` 当作多租户已支持。当前产品是单人本机工作区；课程/连接/会话 scope 和不同本机工作区仍必须隔离，多用户托管不在本轮范围。

## 3. M1：修正缓存查询与显式同步边界

目标：调用者只提供能力、明确查询和服务端上下文，缓存查询不会暗中登录或访问网络。

- query、status、缓存学业统计在缓存缺失/过期/认证错误/进程重启后均不得访问远端、读浏览器 Cookie 或提交密码；返回保留缓存、新鲜度、成功时间、尝试时间和结构化连接错误。
- sync 明确请求远端。仅在已有有效授权和用户设置允许时尝试一次认证恢复；验证码/MFA/拒绝/限流不能自动换代理重投密码。
- 显式恢复入口在成功认证通知后恢复原只读目标，重新核验原 scope，不把“连通”当“登录”。若原任务需要本地写入或导出，应按现有恢复权限继续到安全边界并明确剩余动作，不能顺手扩大自动恢复权限。
- 对未知平台/能力、非法查询类型和额外 owner/URL/Cookie 输入返回标准错误；不得回退通用网络请求。
- 如确有重复协议落在路由中，仅抽取本轮链路所需应用函数，使一个现有业务调用方和一个工具复用；无需迁移全部路由。平台适配器不能依赖 FastAPI、DB 或彼此私有实现。
- 同步失败、空响应、部分分页、缩小学期范围必须分别处理；不能清空未返回记录。断开连接保留资料，显式清数据才删除其授权范围内缓存。

修复必须覆盖调用方与恢复流程，尤其不能为了缓存纯读而导致用户显式 sync 永远无法恢复。更新有意改变语义的旧测试，解释变化；安全、隔离、撤销、冷却断言继续保留，不删测试获得通过。

## 4. M2：让现有校园数据完成业务闭环

优先使用现有工具，按实际支持完成以下三条链，禁止脚本替模型预先规划工具顺序：

1. 最新校园通知：搜索或已登记通知同步 → 打开来源/正文 → 回答标题、日期、原文依据和适用范围。搜索摘要不等于读过全文；“最新”仅针对实际查询范围。只有竺可桢学院来源时不能声称覆盖全校学院。
2. 课程资料/作业：已授权连接发现实际课程 → 按页本地挂接 → 显式同步并观察真实后台任务 → 读取资料/作业及来源 → 回答具体问题。仅目录元数据不等于材料正文；受限下载与未知截止保留原始状态。无需用户手工回工作台串联。
3. 课程与个人学习安排：缓存课表及校历精度 → 实际资料/学习任务 → 计划预览。冲突处理使用业务引擎；缺少日期/课表时保留不确定性，不生成虚构空闲时段。预览不写正式计划，用户明确要求本地应用时沿用已有机制。

每条链至少经过两个工具并检查最终答案，而不只断言工具被调用。优先修复阻塞这三条链的最多三个已证实工程问题；发现更大重构先说明收益、代价和渐进回滚方案。若某链已通过，复用并保留证据，不为凑改动重写。

不增加空壳连接器。智云当前实现依赖历史观测，当前账号权限未验证；学校网页入口不是开放 API 证据。如果真实账号或协议不具备条件，交付清晰缺失条件，不用合成数据冒充真实接入。

## 5. M3：网络和真实验证

自动化保持离线、合成学校数据和模拟外部传输；集成测试使用真实 API、已安装 Harness、实际 worker/Repository，只替换学校与模型外部边界。

另提供显式命令开关 `--public-live`、`--model-live`、`--campus-live`（可复用现有入口而非强制这些名字），默认不访问真实服务：

- 公开联网：从已有可信来源选择公开页，无校园凭据。记录 DNS、连接、TLS/HTTP、跳转检查、内容类型/有效正文和最终回传模型的摘要；低层诊断与实际工具请求分开计量。一次正常请求即可，不扫站、不探限流阈值。
- 三种通路分别判断：后台校园 HTTP、Harness 公开搜索/网页、应用拥有的浏览器。某一路可用不能证明其他通路可用。system/direct 配置只影响校园 transport，不改 OS、模型 provider 或搜索配置；直连仍依赖系统路由，不能承诺绕过 TUN。
- WebVPN 使用独立受信适配与自己的认证条件，不能从“浏览器已登录”推定后台所有平台已授权。不得在多个浏览器同时登录挤掉现有校园会话。
- 真人校园：仅使用当前已授权的连接和最小只读查询。不导出 Cookie、不索取提示词中明文密码。验证码或缺授权时保留目标并说明一次必要交互，完成可独立实施部分，不把整轮停在等账号上。
- 真实模型：只沿用能够核实的既有预算和已启用 provider。固定 provider/model/策略/数据/判分，新增四项各一独立会话：通知依据回答、课程资料与截止、课表学习安排、登录失效后只读继续。每回合最多 12 模型请求、40 工具、150 秒；认证暂停的人类等待单列。恢复场景最多两个回合，分别计量；没有可核实预算或配置就标记待验证，不自行加额或伪造。不得重跑上一轮九项导出评测来替代本轮任务。

首次失败必须保留，修复后同任务对照。记录 usage、人工干预次数、耗时；金额无账单写 unknown，不把受控调用当真实模型的推理成绩。

## 6. 回归及交付

优先入口（按实际路径核实）：

```powershell
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py tools/test_launcher.py -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/export_agent_contract.py --check
apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_agent_runtime.py
node apps/agent-runtime/verify-transition-recovery.mjs
# apps/agent-runtime: npm run test:audit
# apps/web: npm run build; npm test -- --maxWorkers=2
```

只改 Python 服务时先跑相关回归，再跑完整 API/连接器/契约和实际 Harness 链；改 runtime 或 UI 才相应跑其完整回归。上轮数量仅作比较线索：API 700/5 个本机 skip、root/connectors/launcher 606+87 subtests、前端 193、Node 85、完整受控集成 83 次调用。不能把这些历史数抄成当前结果。远端 CI 曾实际执行 703 通过/2 失败，无本机 skip；差异须按环境解释。

交付到 `docs/audit/harness-campus-readonly-result.md`；原始证据放独立 `var/harness-campus-readonly-<UUID>/`，不纳入公共 PR。交付包含：

1. M0—M3 状态、准确源码版本/哈希及修改文件。
2. 每项缺陷：修改前复现、根因层次、最小修复、修改后同案例、回滚方式。
3. C01—C16 明细及三条业务链；真实模型/真实网络/真人校园分别报告。
4. 工具 schema 与暴露分组，调用 ID、参数摘要、结果关联、最终答案/来源和业务状态。
5. 命令、exit code、耗时、passed/failed/skipped、清理情况和未完成条件。
6. 当前平台能力矩阵：实现/合成验证/历史真人证据/本轮真人验证分列；同步修正文档中与本轮相关的过时工具数与能力描述。
7. 至少两个内部调用示例，使用服务端构造上下文，不含密码、Cookie、ticket、签名下载 URL 或个人记录。

最终只声明“完成实施与自测，待 Codex 独立验收”。不要以 turn=succeeded、目录已注册、job已受理或 HTTP 200 代替任务完成。
