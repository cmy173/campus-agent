# 校园 Agent 工具体系改造基线（2026-10-16）

## 盘点范围与运行时分层

- 从 `apps/api/app/agent_bridge/tools.py:SPECS` 和 `tools.schemas()` 生成当前 34 个领域工具的 schema、参数和工具名映射，见 [工具地图](campus-agent-tool-map.md) 与机器可读 [JSON](campus-agent-tool-map.json)。
- 运行时另有 Harness 原生 `web_search` / `web_fetch`、`load_tools`、`publish_file`、浏览器 MCP、MCP、工作区读写、通用 job/workflow、子 Agent/team 工具。浏览器/MCP 名称按当前连接配置动态变化，静态领域 registry 不包含它们。
- 真实调用可确认的只有 trace 中出现过的工具；34 个领域 schema 注册不等于所有工具都被当前模型看到或在真实服务端验证。原生 provider trace 暂缺完整可见 schema 清单、准确 native 工具耗时和所有外部人工步骤。
- 运行主链路：真实 Agent API turn → Harness → `campus-plugin.mjs` → `POST /api/v1/agent/internal/tool` → 以服务端授权构造 `ToolContext` → `tools.execute()` → adapter/router/action/store → 结构化 envelope 回 Harness。公网搜索与网页读取由原生 provider 工具执行，不走领域工具执行分支。

## 当前覆盖

| 层 | 本轮结果 | 真实性边界 |
|---|---|---|
| 工具基础层 | runner 离线 direct tool、认证/session/WebVPN contract 均 passed | 合成/契约测试，不访问学校端 |
| Harness 调度 | mock provider bridge contract passed；scripted Harness 子进程 E2E passed | 真实 API/Harness/只读课程执行器，但合成 SQLite 和脚本模型 |
| 真实 Agent | BAS-02 有 3 次完整重复，均部分成功 | 本机真实 API/Harness/DeepSeek provider 和公网查询 |
| 真实校园数据 | 直接只读 `campus.sync` 查询 2026 秋季学期 18 门课程，状态 fresh、complete | 用户真实账户的只读 API 探测，不经过模型；benchmark 只保存条数/时间/覆盖，不保存课程内容 |

当前目录为 37 个场景：32 个可经真实模型路径运行，BUS-03 与 JOB-01 另有隔离 scripted mock 路径，4 个因缺少可复现故障 fixture/人工认证恢复而 blocked；JOB-01 属于 mock-only。可运行不代表真实 Agent 已执行或成功。完整目录和测试数据见 `tools/campus_bench/scenarios.json`。本轮没有真实执行完整基线。

## 第一批改造前 BAS-02 基线

- 场景：`BAS-02`，“学校的本科生一般在哪里查看教务安排？如果你不确定具体校内入口，请说明。”
- Hypothesis under test：普通公开入口问答里，模型会额外检查个人校园状态；如果不需要这些个人状态工具，明确限制何时调用状态查询可减少无关读取和等待，同时维持官方入口准确性。
- 工具基础层和 Harness 定向层已在每个 runner 批次自动运行并通过；Agent 样本是只读。
- Provider `prv_c68d2878`（本地已配置模型，不读取或导出密钥）；模型配置保持不变；并发 1；每任务超时 90 秒；单任务最多 6 次模型调用；固定 runner seed 20261016。成本未知，因此严格限制为 3 次 Agent 运行。
- `git_head`：`650a9348e859ee24544c85fce1a3f397ae3e2345`；工作树有未提交改动。两批次：`20261016-160217-b4c00004`（1 次）和 `20261016-160721-75c411a9`（2 次）。

| 重复 | 结果 | 端到端 | 模型调用 | 工具调用 | Agent 失败工具 | 观察到的领域工具 |
|---|---|---:|---:|---:|---:|---|
| 1 | 部分成功 | 30.33 s | 4 | 4 | 0 | `campus.status`, `connections.status`, `web_search`, `web_fetch` |
| 2 | 部分成功 | 37.06 s | 2 | 2 | 0 | `campus.status`, `web_search` |
| 3 | 部分成功 | 41.88 s | 4 | 5 | 0 | `campus.status`, `web_search`, `web_fetch` 等 |

合并 n=3：平均 36.42 s，中位数 37.06 s，样本范围 30.33–41.88 s；平均 3.33 次模型调用、3.67 次工具调用；结果 3/3 部分成功，0 失败，0 不可验证；没有观测到确认事件。native provider 成本没有可信价目，记为 null。首个响应时间及 native web 工具精确耗时只能部分观察。

真实输出指出本科教务平台为 zdbk，且检查了个人教务和连接器状态后附上公网入口信息；评分器确认入口核心主张与 `campus.status` 一致，但附加来源/URL主张未独立逐项校验，因此只计部分成功。没有证据证明多余状态调用导致模型选错或正确性下降；这是待验证的效率/隐私最小化假设，不能称为已证实根因。

## 分批变更记录

### 批次 1：明确状态工具的适用范围

- 改动前批次和版本：见上表；Agent schema 哈希在后续报告中记录。
- 可证伪假设：提示 `campus.status` / `connections.status` 只用于用户问自己的认证/连接状态，或任务确实依赖个人校园数据时调用。对 BAS-02 这种一般公开入口问题，状态查询数量应下降；场景仍应指出官方教务入口，不能凭空声称个人账号状态。
- 最小改动：仅收窄这两个工具的描述文本，不改参数、权限、执行器、缓存或 Harness。
- 回归要求：运行器 direct/auth contract + Harness dispatch；BAS-02 再重复 3 次；对照成功、部分成功、失败/不可验证比例、状态工具数、所有工具数、耗时范围。不得把单次改善外推到所有工具。
- 决策门槛：如果状态调用下降但入口正确性或必要任务回归变差，撤销或细化描述；若重复结果高度波动或评级仍因证据不足为部分成功，只报告不确定性，不宣称用户体验提升。

## 优先级（证据与置信度分开）

1. **追踪盲点（部分解决）：** Native provider 现已记录调用级 schema 快照、模型选择和 Harness `llm/stream` 请求墙钟。独立 native 工具精确耗时仍通过 Harness step span 估算；静态 registry 仍不能代表当前 provider 下每个动态组的可见情况。
2. **认证状态多层（已证实差异，原因已澄清）：** 保存的 Passport/WebVPN 凭据、browser-session reuse consent、连接器是否在线、当前 workspace connected 是不同状态。真实课程同步成功 18 条，证明直接同步路径可使用保存凭据；browser grant=false 并不能推出“账号未登录”。后续面向 UI 的状态工具要避免把这些状态合并成单一 authenticated 布尔值。
3. **状态查询是否过度（待验证、中低置信）：** BAS-02 三次都访问 `campus.status`，一次还访问 `connections.status`；均无错误，但公开入口任务可能不需要读取个人状态。
4. **可靠评分（已证实限制）：** BAS-02 的核心入口与状态证据对齐，附加动态事实未被独立来源 oracle 逐条验证；部分成功是正确的谨慎标注，不应只为追求成功率放宽标准。
5. **覆盖缺口（已证实）：** ERR-04/05/07/08 缺少故障注入或暂停恢复夹具；review、plans、文件导出、课程资料、作业和多工具任务尚未按家族完成真实 Agent 3×基线。

## 下一批建议

- 先完成本页的状态描述单变量实验并评价是否减少个人状态读取。
- BAS-03 已完成新的真实 n=3 检索基线和来源 oracle 重评；当前没有证据支持因调用次数而改检索策略。下一步先人工抽查未对齐的引用与正文事实，再用同样公开任务与固定来源做有界重复/新旧配置消融。
- 之后选择真实课程资料/作业（已有授权只读）或本地学习计划工具家族，先跑 direct + Harness + 真实 Agent 基线，再改一个因素。
- 暂不执行任何真实报名、提交作业、付费、发送、删除或自动接受计划动作。

## AUTH-01 工具家族迭代复测

- 工具地图已从生产 `SPECS` 生成，现含 34 个领域工具、9 个动态工具组和 36 个场景。原 33 场景均保留；新增 AUTH-01、BUS-09、BUS-10。动态 provider/MCP schema 仍由运行时配置决定，不能由领域 registry 数量推断。
- 改动前：批次 `20261016-163139-3e2f354a`，AUTH-01 n=1。真实 Agent 使用 `campus.status` 与 `connections.status` 两个宽状态接口；轨迹观察到用户名提示/保存时间元数据被用于回答，并把“browser reuse 未授权”推断为登录失败原因。缺少专用安全状态接口，因此任务失败。该基线仅 n=1，不代表稳定失败率。
- 第一候选：批次 `20261016-170425-b316ee7b`，n=3。加入只读 `campus.credentials.status` 后，三次都选中了专用工具；平均 1.67 个工具调用、2 个模型调用、18.48 秒。两次同时调用了旧状态接口，因私有状态答案必须脱敏而成为不可验证；另一次仍过度断言需要浏览器登录。证明了专用工具已被发现，但冗余与推断问题仍在。
- 第二候选：仅收窄 `campus.credentials.status` 描述，明确纯状态查询不应串联旧接口，并明确配置状态不能证明学校端接受凭据或解释失败原因。批次 `20261016-171102-e10e6d6d`，真实 Agent n=3、并发 1、单任务超时 90 秒、模型调用上限 6/任务。三次均只调用 `campus.credentials.status`，没有登录、同步、校园网络请求或私有答案脱敏；工具数量从首轮候选均值 1.67 降为 1。平均 16.40 秒（样本 10.60–19.57 秒，P50 19.04、P95 19.57），2/3 回答符合安全状态边界，1/3 漏说“本机状态无法验证学校端是否接受凭据/不能单独证明失败原因”，所以保守记为部分成功而非满分。
- 判断：工具选择/最小读取范围的方向改善有重复轨迹支持；小样本且模型采样不同，不能宣称端到端成功率或时延因果提升。回答边界仍有 1/3 波动，下一步优先加针对性确定性评分/提示边界回归，而非再改 Harness。专用工具返回字段有 API 单测；AUTH-01 是真实 Agent层，Harness 通用调度已通过 mock-provider 往返；这不是校园学校端认证实测。
- 当前四层证据：工具基础 20/20 合成 transport/tool 用例通过；认证/浏览器授权/WebVPN 合约 76/76 测试通过（均为 mock/隔离）；Harness dispatch 2/2 通过；真实 Agent AUTH-01 3/3 选择正确的专用工具、2/3 满足回答边界；用户体验中的实际点击、外部登录次数不可由当前 API 事件完整观测。
- 可复现批次文件：`var/campus-bench/20261016-163139-3e2f354a`、`var/campus-bench/20261016-170425-b316ee7b`、`var/campus-bench/20261016-171102-e10e6d6d`。后两批次的分数由当前 rubric 对原始安全 trace 重评；manifest 保留当时配置和代码工作区摘要。

## 现在的主要系统限制与优先级

1. **P1 评分器过度要求自然语言措辞（已见 1/3 边界漏答；中等置信）**：AUTH-01 的状态数据与工具选择三次一致，单次遗漏限制声明导致部分成功。下一实验应比较 schema 描述候选与已有可验证答案要点，不应将“必须出现某个固定句子”当唯一证据；以状态字段与回答是否过度声称为核心。
2. **P1 native 工具可观测性（全局代码路径限制；待验证影响）**：工具调用/结果能从 Harness journal 重建，但 native 工具精确耗时、完整模型可见 schema 列表并非每次稳定落盘。先补不含凭据的实际工具清单/调用关联耗时遥测，再对公网搜索/读取或动态分组做消融。
3. **P1 基线覆盖不足（已证实）**：只做了若干真实只读场景，而非全部 30 个 runnable 场景各 3 次。课程资料、作业、计划、分页/日期边界、长任务恢复和故障注入还缺家族基线；ERR-04/05/07/08 继续阻塞，不把 mock 合约通过当真实能力验证。
4. **P2 用户操作可观察性（已证实限制）**：事件日志只能统计 harness 内确认事件，不能证明用户在外部浏览器点击或重复登录；这些指标必须标 `null/needs_human_review`，不能按零处理。

工具地图重建命令：`python tools/campus_bench/build_tool_map.py`。若 API 依赖未安装在活动 Python 环境，可用 `apps/api/.venv/Scripts/python.exe`；生成器会自行补上 `apps/api` 导入路径。完整批次仍需分家族扩展，当前结果不代表所有工具家族已完成重构。

## 实验基础设施迭代：捕获真实模型可见工具 Schema

- 假设：当前真实 provider 使用 DeepSeek Harness 原生路径，若只观察域工具 registry 或 `/internal/model` 桥接，将无法区分工具缺失与模型未选择。记录 `llm/stream` 本轮 options.tools、模型选中工具和请求耗时后，可直接根据真实轨迹归因。
- 改动前证据：AUTH-01 批次 `20261016-171954-cd7665c1` 的真实 Agent 回归能记录 2 次 assistant/message、1 次校园工具调用，却没有后端 `model_call` 事件。代码路径显示当前 provider=`deepseek-official`，请求绕开 `/internal/model`；因此只给 `/internal/model` 加快照没有覆盖用户真实常用 provider。这是观测盲点，不是 Agent 任务失败。
- 最小改动：在 Harness `llm/stream` 成功结束后回传可见 schema、选中工具和模型 stream 墙钟耗时；错误事件也附上同一轮可见 schema。API 仅记录工具名、脱敏 schema 快照与哈希，64,000 字符上限；不记录 prompt、模型密钥或响应正文。遥测桥接失败不影响真实模型返回。
- Harness 验证：`test_campus_bench_harness.py` 覆盖 native observation 事件、schema 快照限长及敏感字段脱敏；本轮定向 API 测试 40/40 通过（另有现存 StarletteDeprecationWarning），Node recovery/tool-group 测试 15/15 通过，runner 测试 28/28 通过，`py_compile`/`node --check`/地图生成通过。
- 真实回归：批次 `20261016-173114-88922e72`，AUTH-01 n=1，仍为部分成功；2 条 `model_call` 事件各记录 53 个模型可见工具 schema，调用了 `campus_campus_credentials_status`，1 次工具调用，模型调用耗时分别由事件记录。快照 53/53 均在 64KB 上限内，哈希 64 位；扫描基准导出轨迹未发现凭据关键字或脱敏用户名值。此批只证明追踪链路运行，不能作为 Agent 成功率比较。
- 与改动前相比：实际轨迹现在可回答“调用时模型看到哪些工具、最终选了哪个工具、schema 版本摘要是什么”；此前只能从静态清单推测当前请求工具面。native `web_search`/`web_fetch` 的独立执行耗时尚未直接追踪；模型请求总耗时是 Harness `llm/stream` 包络，包含 SDK stream 处理。外部点击/MFA 人工步骤仍不可观测。
- 注意：`options.tools` 快照只代表本轮模型看到的工具表，不等于仓库全部工具或永远可调用的能力；完整 schema 超过上限时只留 hash/count。真实调用仍以已记录 `tool/call` 和对应结果为准。

## BAS-03 原生联网工具基线与来源评分校准

- 场景：用户要求搜索 DeepSeek 官方 API 文档、概述文档内容并提供来源。没有改 Agent 检索提示、Schema、Harness 搜索/读取实现或模型配置；仅扩展模型可见 schema/耗时遥测，并在读取真实结果后收紧该场景的确定性评分标准。
- 当前批次 `20261016-174150-1d95f1e2`：真实 API / provider、并发 1、n=3、每任务模型调用上限 10、超时 120 秒、无失败重跑。三次均 4 次模型调用、每次模型看到 53 个工具 schema；均按 `web_search` → 两轮并行 `web_fetch` → 最终回答执行。每次 1 次搜索、4 次网页读取（四个不同 URL），没有相同 URL 重复抓取、工具错误、重试或未关联结果的轨迹证据。
- 任务指标：3/3 轨迹包含来源工具，平均端到端 33.16 秒（P50 34.25、P95 35.36、样本 29.86–35.36 秒）；平均 5 次工具调用、4 次模型调用，输入/输出 Token 均值约 11,795/2,857。模型 stream 平均 14.25 秒；native 工具整体 step span 估算均值 7.34 秒（并行调用会重叠，不能解释为独立工具精确耗时）。API 成本为 null，因为没有为当前 provider/model 配置可信价格表。
- 旧真实 BAS-03 批次 `20261016-130728-c264a7d1` 使用同 provider ID、n=3、并发 1、同 runner seed；经当前 runner 和当前 rubric 重建后是 3/3 不可验证，平均 34.38 秒、P50 32.57、P95 42.50，平均 6.67 工具调用、4.67 模型调用。新旧 runner/catalog hash、调用预算与时间不同、模型采样和实时网页响应不配对；因此当前批次工具调用较少（5 vs 6.67）和均值快 1.22 秒只作描述性差异，不能归因于本轮仅用于追踪/评分的代码改动。
- 评分 oracle 变更：BAS-03 现在要求至少一个引用 URL 与本轮实际 search/fetch 工具来源匹配、引用标题能在来源内容中对齐，并限制在 `api-docs.deepseek.com`；“抓取时间”不再被误当成网页发布日期来验证。此判定不证明摘要中的每条事实正确，因此即使引用对齐也只记部分成功。
- 当前 n=3 在新 oracle 下：run02 的 8/8 引用全部匹配本轮官方来源、标题与域名，记部分成功；run01 有 5 条引用但标题/来源对齐不足，run03 有 5 条引用且 1 条标题对齐不足，均记不可验证；总计 0 成功、1 部分成功、2 不可验证、0 失败。没有把来源不完整样本算作 Agent 失败，也没有把 Agent 的自述当作事实正确证据。剩余工作是人工抽查正文中 API 参数与价格/模型能力等细节，再决定是否添加逐事实 oracle。
- 归因：观察到 4 次不同 URL 的网页读取，不能称为重复/无效抓取；抓取到页面后确实有后续模型步骤并产生摘要。现有数据不支持“网络能力不足”或“Harness 未回传”根因。自动诊断主类 J（评分事实 oracle 不足）符合证据；效率变化尚未证实。

## BUS-02 作业查询与同步范围基线

- 场景 `BUS-02`：查询“我课程里最近的作业和截止时间”。只读真实 Agent API、同一 provider、串行、n=3、每任务 120 秒、最多 8 次模型调用、无重试。保留原始 `evaluation_as_run` 与当前 rubric 重评字段；当前报告将三次都标为不可验证，不能计为确认失败。
- 环境首次不完整批次 `20261016-175849-d707b7b6`：3/3 不可验证，平均 56.71 秒（P50 55.65、P95 60.21），平均 9.0 次工具调用、7.0 次模型调用、47,141 输入 / 4,337 输出 Token。三次都尝试 `connections.sync`；其 `jobs.read` 观测含 `DEPENDENCY_UNAVAILABLE`，同一时刻本机 `127.0.0.1:8765` 无监听者。确认这是测试环境连接器未启动，不是学校登录失败。
- 一次明确只读 `runner.py probe --private-courses` 在连接器就绪后从真实 ZDBK 返回 18 条课程，fresh、complete；落盘探测只含数量、学期、来源时间，不含课程正文。该结果不证明 browser-session grant 已授权或 xzzd 会话仍有效；健康响应中的 Cookie 数量也不作认证成功证据。
- 连接器就绪后的对照批次 `20261016-180705-85151f41`：3/3 仍不可验证，平均 56.00 秒（P50 54.13、P95 63.03），平均 9.33 次工具、7.0 次模型调用、37,556 输入 / 4,125 输出 Token。平均时延仅描述性下降 0.71 秒，工具数反而增加 0.33；没有任务成功率改善证据。三次 Agent 仍调用 `connections.sync`，其中 `jobs.read` 重复 1–4 次且均未给出同步终态。作业列表出现 `INVALID_INPUT` 的次数为 2/3；调用参数已脱敏，具体错误参数尚不能归因。
- 环境和真实同步证据：无连接器的一次真实同步最终失败，33 条错误全为 `DEPENDENCY_UNAVAILABLE`；连接器就绪后后台同步覆盖 33 门已绑定课程，最终 `partial_failed`，记 52 条资源错误（36 `UNSUPPORTED_MEDIA_TYPE`、15 `CORRUPT_FILE`、1 `PERMISSION_DENIED`）。这些错误来自全量课程资源/附件处理；不能据此断言作业接口本身失败。样本显示作业查询触发了更宽的同步工作。
- 已证实代码事实：`connections.sync` 仅接受平台/连接 ID，worker 总以 `kinds=None` 调用适配器，因此总会获取课件和作业，并继续处理资源附件；XZZD 适配器及受控浏览器 Connector 已支持 `kinds=['assignments']` / `['resources']`。改进假设：用户只查作业时把该范围一路传到 Connector，可避免无关课件读取/下载和对应解析错误，同时保留省略参数时的兼容全量行为。当前回归在加入范围字段后单独记录，不把第一次启动连接器或课程刷新当作该代码改动的效果。
- 启动根因与小修：此前 `tools/dev.py --platforms` 允许空参数并把空列表解释成“不开校园连接器”，而常用示例正是裸写该开关；这造成连接器端口 8765 缺失。已把 CLI 改成 `--platforms xzzd [zykt]`（要求至少一个值）、新增显式 `--no-platforms`，并把遗漏模块根路径的私有 live probe 修正为可从仓库根命令行导入 `connectors`。这样不会自动认证或取消任何校园授权。

## BUS-02 第一轮范围优化与复测

- 可证伪假设：作业问题只需要 assignment 元数据；全量同步触发的课件读取/附件获取是额外负担。新增可选 `connections.sync.kinds` 并把它传至排队 payload、worker、协议 adapter 和浏览器 connector 后，`kinds=['assignments']` 应跳过资源读取/下载，得到成功状态且显著减少同步错误/时长；不传参数仍保持旧的全量行为。
- 最小实现：`ConnectionSyncArgs` 与 REST `StartSyncRequest` 增加 `assignments|resources` 可选列表；同步 job 保存并恢复该范围，active job 去重只有范围完全相同才复用；worker 和 protocol adapter 把 `kinds` 原样传到 connector。XZZD adapter 原本支持 kinds，本轮没有改变平台接口行为。
- 工具直接层：固定合成 sync API 测试确认 job payload 和 worker adapter 收到 `['assignments']`；协议桥接测试确认参数跨层保留；定向 `test_agent_connection_sync_tool.py` + `test_sync_api.py` 为 41 passed。另用真实连接、真实只读 `connections.sync(kinds=['assignments'])` 执行一次，job `succeeded`、0 错误；紧接同一日第二个真实 Agent 作业限定同步也 `succeeded`、0 错误，平均 job 时长约 49 秒。没有读取或落盘任务正文到报告。
- 实测对照：范围优化前连接器可用时的全量同步 job 遍历 33 门已绑定课程，历时 468.48 秒，最终 `partial_failed`，52 个资源处理错误（36 `UNSUPPORTED_MEDIA_TYPE`、15 `CORRUPT_FILE`、1 `PERMISSION_DENIED`）。范围优化后两个 `assignments` job 也覆盖同一批 33 门课程，分别 48.83 秒与 49.27 秒，均无错误；观测降幅约 89%，但样本小且缓存/平台状态会变化，作为真实 job 层证据，不外推为所有学期或平台的普遍收益。
- Agent 复测批次 `20261016-182553-2c8a2b67`：BUS-02 n=3、串行、provider 不变、每任务 8 model-call 上限、0 retries；三次当前 rubric 都是不可验证（0 success / 0 partial / 0 failed / 3 unverifiable），原运行时 rubric 是 2 partial、1 unverifiable。run01 的真实 Agent 确实发起了 `kinds=['assignments']` job；后两次直接读刚刷新缓存，没有再同步。平均端到端 34.67 秒（P50 35.09、P95 46.07），平均 7.0 工具调用 / 5.67 模型调用，工具 step span 5.96 秒，输入/输出 43,174/3,529 Token。相对连接器就绪但全量策略批次，调用 9.33→7.0、模型 7→5.67、端到端 56.00→34.67 秒；主要同时改变了同步范围与缓存冷热，不能归因成单变量因果，也没有任何一条变成已确认正确。
- 判断：保留范围参数改动，理由是两条真实窄范围 job 均成功且 0 错误，对照全量 job 的资源错误与长时延直接吻合假设。Agent 任务完成正确性仍不可验证；BUS-02 缺少隐私保护的实时作业事实 oracle，不能用模型措辞或 `partial` 代替事实核验。下一步补安全的作业日期/来源匹配 oracle，再检查 `assignments.list` 参数错误和重复分页；任何扩大至其他平台的结论需单独验证。
- BUS-03 前置检查：仅在本机数据库做只读计数，课程名匹配数为 0、资料数为 0；因此没有把“有《操作系统》课程及导入资料”当成当前实测环境，也没有为补前置而启动全范围资料同步。该场景保留在目录，等待真实课程/资料出现或为它提供隔离 fixture。

## BUS-02 第二轮：隐私安全事实核验与作业工具 schema

- 改动前批次：`20261016-184134-baace782`；候选批次：`20261016-184627-3adc4782`。同一 provider、同一只读提示、每批 n=3、并发 1、每场景 100 秒、每任务最多 8 次模型调用、总上限 24；模型成本无法读取，故以调用上限控制预算。两批次均在单日账户缓存环境，但具体缓存/学校数据更新状态未绑定记录，延迟仅为描述性对照。
- 新增隐私安全日期 oracle：在进程内比对最终回答日期和 `assignments.list` 原始返回日期，只持久化结果计数、来源覆盖布尔值和匹配状态。作业标题、课程 ID、正文和具体日期不写入基准轨迹。只要工具覆盖声明为 incomplete，即使日期匹配也最多部分成功；来源外日期确定性判失败。
- 改动前观测：3/3 真实 Agent runs；工具/模型调用分别为 15/8（预算截尾）、11/8、6/3；平均 10.67 工具调用、6.33 模型调用、41.63 秒。两次完成任务均检测到来源不支持的日期，另一次被调用预算截断。脱敏统计中的作业工具结果数分别为 11、2、3；一次轨迹有 `INVALID_INPUT`（工具层返回时区参数错误）。重复查询计数不能单独证明所有调用无效，但分页游标、过滤哈希不同和错误均记录在对应 JSON 的脱敏 trace。
- 最小改动仅为 `assignments.list` 的 schema 与描述：解释默认单页/主键排序、游标续页、禁止重复首屏、日期范围必须为含时区 ISO-8601、不要逐课程穷举，以及本地缓存的覆盖边界。查询执行语义未改。另将 BUS-02 评分改为来源日期 oracle，而非关键词命中。
- 定向回归：`tools/campus_bench/test_runner.py` 31 passed；API 环境 `apps/api/.venv` 下 `tests/test_agent_tools.py` 33 passed（含 schema 契约断言；1 条现有 Starlette deprecation warning）。
- 候选真实结果：3/3 runs；工具/模型调用为 9/8（预算截尾）、6/5、6/5；平均 7.0 工具调用、6.0 模型调用、37.82 秒。日期 oracle 为 1 次不可验证（预算截尾）、1 次日期与部分本机来源对齐、1 次含一个来源不支持日期。平均工具调用数描述性下降 3.67（约 34%），平均耗时下降约 9%；样本仍有预算截尾且缓存状态未配对，不能宣称因 schema 带来因果性能提升。改动保留为低风险候选，因调用明显减少的方向和工具契约语义一致；需要下一批匹配数据状态重复验证。
- 已证实根因：两次最终回答包含至少一个未由本轮 `assignments.list` 来源支持的日期，根因标为 F（模型未正确利用工具结果），报告只保留聚合数量，不暴露日期。预算截尾样本归为 J（实验预算中断），不算 Agent 失败。当前本机赋值数据工具状态多为 partial，说明快照范围不能证明学校端完整，故只对齐日期仍不算完全任务成功。
- 继续优先级：1) 将作业工具结果以可引用的结构化事实传回/呈现，要求最终日期逐条关联来源或省略无法验证项；2) 对照验证 schema 描述是否持续减少重复分页和无效范围调用；3) 有空再补齐作业源的增量/完整性状态。不得在没有可靠来源 oracle 前把 BUS-02 计作完全成功。

## 运行追踪关联修正（针对上述两批轨迹回算）

- 审核候选批次时发现 Harness `tool/result` 在并行调用下不一定带 call ID，但事件对象包含 `sourceEventSeqs`，精确指向对应的 `tool/call` 事件序号。原评分器忽略该字段，只能按 ID 或 turn/step 一对一关联，因而把同一步骤内多个正确返回的结果误报为未关联。
- 已在 `execution_dimensions` 加入基于 `sourceEventSeqs` 的精确优先关联，并保留 call ID 和唯一 turn/step 作为后备；没有修改 Agent 调度或工具行为。
- 合成回归明确构造两个相同 turn/step 的并行调用及各自来源序号，结果要求两者都能关联；`tools/campus_bench/test_runner.py` 32 passed。重新生成两批报告后，候选批次 3/3 的所有工具结果均已关联；基线仅 run01 保留 1 个未完成/无返回调用，符合其达到模型调用预算并被截尾的轨迹。该变化是追踪/评分精度改善，不是 Agent 行为改善。

## BUS-02 日期边界与低负载结果实验

- 时区复核：作业 API 提供 UTC 时间戳，回答可按 Asia/Shanghai 展示。Oracle 已在内存中同时容忍 UTC 与 Asia/Shanghai 日历日，避免跨午夜误判；单测覆盖 UTC 到本地跨日。之后真实批次 `20261016-185516-b6964e17`（n=3）仍有 2 个已完成运行各含一个来源集合不支持的日期，第三次在 8 次调用预算处截尾且没有可核验日期。因而这两处不匹配不是单纯 UTC/本地跨日造成；当前只保留数量和状态，不落具体日期。
- 按 oracle 代码审查发现另一个评分优先级问题：一页查询错误不应掩盖同轮其他有效作业来源已证明的无来源日期。现在若聚合证据包含 unsupported date，即使同轮另一页报错，也仍记为 mismatch；错误同时保留在轨迹供根因分析。对历史报告只能在脱敏前写入的聚合计数上回算，不尝试恢复已删除的敏感值。
- 另一个单因素试验为 `assignments.list.include_instructions`，默认 false；明确询问作业要求时可设 true。定向 API 测试 `test_agent_tools.py` 34 passed（含默认省略、显式取回）。真实批次 `20261016-185940-80ee58e5`，n=3，结果 1 失败、2 不可验证；工具调用 5/9/6，平均 6.67；模型调用 3/6/4，平均 4.33；平均端到端 33.92 秒，输入 Token 平均 35,461。上批（禁用说明正文前）是 2 失败、1 不可验证；平均工具 6.67、模型 5.33、41.16 秒、输入 Token 24,727。账户缓存/实时数据和样本随机性未配对，输入 token 反而上升，结果质量也未改善，故不把更低的平均耗时/模型调用归因于省略正文。保留默认省略仅基于最小必要数据原则，作为用户明确请求时可回开的可选字段；后续若证据显示遗漏作业要求，再调整默认或分拆元数据/正文工具。
- 最新批次重新核验后，2/3 运行的回答有来源不支持日期，根因 F 已证实；另 1/3 为作业来源调用失败且没有足够事实作答，仍不可验证。没有形成真实任务成功证据。优先下一步应设计工具返回的截止日期结构化引用/可追溯标识，或缩小“最近”查询结果并按截止时间排序，再以同一 oracle 重复测试；不要把基于日期列表的启发式评分当成已完成优化。

## BUS-02 第三轮：`jobs.read` 有界等待实验（未证明改善）

- 触发证据：`20261016-191814-32e22f97` 的真实 Agent 轨迹中，assignment-only 同步的 `connections.sync(wait_seconds=15)` 约 15.39 秒返回 running；随后多次 `jobs.read(wait_seconds=14)` 每次约 28–38ms 即返回 running。确认原因是现有 `job_wait_window` 没有为 `type=sync` 开放等待窗口。这是已证实的执行语义缺陷，不代表已证实任务成功率根因。
- 可证伪假设：让 sync job 的 `jobs.read` 在当前 Harness 调用中真实等待，并以受理时刻为基准共享最多 60 秒观察预算，可以减少快速空轮询，让模型观察到终态；若每次等待挤占任务时限或没有减少调用，则该策略不构成 Agent 体验改善。
- 最小实现：`jobs.read` 对 sync job 使用可中断的轮询等待；每次最多 14 秒，受理后总观察窗口 60 秒；超窗仍回传当前状态。只允许 job 所有者读取。相关定向 API/Harness 测试 55 passed；Benchmark runner 测试 33 passed；`git diff --check` 通过（存在仓库 LF/CRLF 警告，无 whitespace error）。
- 真实复测：`20261016-193055-a951f1e3`，BUS-02 n=3、串行、真实 API/provider、8 model calls/run、24/batch、100 秒/task、无自动重试；工具基础合成层、认证/session 契约层、Harness dispatch 合约层均通过。Agent 层 0 success、0 partial、1 failed、2 unverifiable；平均耗时 66.92 秒（P50 82.64、P95 85.27），平均 8 工具调用 / 7.33 模型调用，平均 42,679 输入 token、4,084 输出 token。run01 与 run03 在 8 次调用预算下取消；run02 完成但日期 oracle 判失败。确认 `jobs.read` 不再是零时长：两个取消运行各观察到约 14.1s + 14.1s + 3.3–3.6s 的 wait；同步最终观察仍经过多个模型决策。
- 与 `20261016-191814-32e22f97` 的比较仅描述性：前批 1 partial / 1 fail / 1 unverifiable，平均 46.28 秒、7.67 工具、6.67 模型调用；本批指标更差。但账号缓存和实时平台状态未配对，且 runner hash 不同，不能把差异归因于本代码。结论是“同步 wait 语义修复已通过单测并在真实轨迹体现”，不是“Agent 变快/成功率提高”。本轮不据此扩大修改；需要固定数据/缓存条件、提高每任务模型预算或使用隔离 job fixture 后再测试较短单次等待策略。报告：`var/campus-bench/20261016-193055-a951f1e3/index.html` 与 `comparison.html`。
- 新问题与优先级：BUS-02 仍会分页遍历 assignment snapshot（最多多页）且出现预算取消；至少一次最终日期不受来源支持，事实 oracle 判失败。下一批优先研究“deadline 查询是否能窄范围/少页读取，且工具明确返回快照覆盖度”，必须先以同一缓存快照配对，不把工具数减少作为任务正确性的替代指标。该项目前是高置信轨迹观察、尚非全局根因。

## BUS-09：逐课程下一项作业查询（真实回归未通过质量门槛）

- 证据起点：BUS-02 真实轨迹曾对作业截止问题多次读取全局分页结果，且出现无来源日期。为隔离只读工具选择与事实利用，新增 BUS-09：明确禁止校园同步/登录，只从本机缓存列出每门课程一项最近且截止时间已知的作业，并披露本机覆盖边界。当前场景总数36，32个runnable、4个因缺少故障注入/恢复条件blocked。
- 工具改动：新增只读 `assignments.next_by_course`，使用 SQL `row_number()` 按授权课程分组后选择最早已知截止时间，排序后分页；截止时间全未知的课程不返回，并明确说明。结果收窄为课程名、作业名、中国时区截止时间、提交状态和来源引用，避免模型为配对课程再读取课程目录。未触发校外网络或登录。
- 评分改动：BUS-09 检查专用工具是否执行、是否误触发同步、返回课程是否重复、日期是否有工具来源、是否披露本机缓存边界。报告只保留聚合数量和分类，不保存课程名、作业名、ID或日期。日期诊断增加未匹配日期的语境类别计数，以区分截止/作业语境、缓存说明和普通语境。
- 定向测试：`test_agent_tools.py` + `test_agent_connection_sync_tool.py` 56 passed；`test_runner.py` + `test_build_tool_map.py` 36 passed；`git diff --check` 通过（仅有已存在的 LF/CRLF 转换警告）。隔离 DB 验证稳定排序、授权范围、未知截止排除和组合分页；隐私测试验证课程名、作业名和日期不会写入运行轨迹。
- 真实实验基线与迭代：
  - `20261016-195257-5968546e`：专用工具初测，n=3，3 次均失败；每次都选对工具、返回 17 门课程且无重复、没有同步；平均 2.0 工具 / 3.0 模型调用，30.32 秒。日期 oracle 每次发现 1–2 个不受来源支持的日期。
  - `20261016-195802-c023d25f`：给结果加入课程名称后，n=3 仍 3 次失败；平均 2.33 工具 / 3.33 模型调用，33.52 秒。模型仍有 2/3 运行额外调用 `courses.list`。
  - `20261016-200325-2304c91a`：加入 `due_at_local` 后，n=3 仍 3 次失败；平均 2.33 工具 / 2.67 模型调用，28.24 秒，仍有每次 1–2 个未匹配日期。
  - `20261016-200844-1be3c47f`：把专用结果压缩到任务必要字段后，n=3 仍 3 次失败；平均 3.0 工具 / 3.67 模型调用，37.45 秒。压缩未显示稳定收益。
  - `20261016-201335-719b3426`、`20261016-201803-93dd967c`：增加脱敏日期语境统计后，各 n=3 仍 0 成功；最后一批平均 2.33 工具 / 2.67 模型调用、33.79 秒。日期语境计数不能重建旧批次中的原文；已脱敏日志不支持进一步的逐句归因。
- 证据结论：新增工具的选择与范围边界已被三次重复的真实轨迹验证（17 个不同课程、无同步、无重复行），但任务答案仍未通过确定性日期 oracle，因此不宣称正确性改善。各批比较的账号/缓存/随机输出并未严格配对，耗时及调用数仅描述观察，不做因果归因。分组工具保留为明确的只读能力；对于全局的“Agent 已可靠回答逐课程截止日期”结论，当前证据是否定的。
- 下一优先级：A) 在日期评分中保留课程—作业—日期三元组的内存级匹配信号（只落盘聚合错误数），区分正确日期配错课程；B) 评估 Agent 是否应先引用工具返回的本地日期而不生成未要求的额外日期；C) 增加固定合成课程/作业 fixture 的响应级真实 Harness 用例，再做按模型配置的配对 A/B。不得把这批的 0/3 重新解释成通过。
- 本机认证探测：在最后重启后的 live probe 中 `browser_session_authorized=false`。BUS-09 因此只读近期本地缓存；没有把保存凭据视为有效登录，也没有执行真实校园刷新。

## BUS-09 第三轮：确定性回答收敛与脱敏重评分修复（2026-10-16）

- 可证伪假设：当用户要求按课程列最近截止作业时，模型在读到 `assignments.next_by_course` 后仍自行组织日期，存在无来源日期；若仅在单一、专用且无其他冲突工具族调用的只读路径，将最终答案用该工具的去标识化/内存事实渲染，则不支持日期数应降为 0，同时显式披露本机缓存边界。
- 最小实现：新增 `apps/api/app/agent_bridge/response_grounding.py`，仅针对该精确工具路径从工具结果构造日期表，转义 Markdown、按日期排序，并明确未提供截止日期的课程被省略、缓存不代表学校端完整数据；有未消费分页游标时声明结果不完整。Harness 在持久化最终 turn 前运行此限定渲染，并只记录聚合 `response_grounding` 事件。其他工具族或混合查询保持模型回复，不套用该渲染。
- 修改前真实 Agent 基线：批次 `20261016-201803-93dd967c`，同一 provider/代码头/目录哈希/运行器哈希，BUS-09 n=3，串行、单任务 100 秒、最多 8 次模型调用、0 重试。3/3 回答都包含来源不支持日期，评分 3 失败；平均 2.33 工具调用、2.67 模型调用、33.79 秒。此前已有专用工具选择，但模型事实使用不可靠。
- 修改后真实 Agent 批次：`20261016-202719-251ddaf6`，同样 n=3 / 串行 / 超时及模型调用上限 / provider；该场景明确禁止校园同步与登录，仅读本机缓存。3/3 都运行时执行了可信工具结果渲染，逐课程来源日期确定性核验为 17 行、17 个不同课程、0 重复、0 来源外日期，且 `connections.sync` 未观察到、缓存边界有说明。隐私安全重评分为 3/3 部分成功：日期对齐，但本机缓存不能证明学校端全量覆盖，因此不计作完整成功。平均 3.00 工具调用、3.33 模型调用、35.64 秒；相对基线耗时 +5.5%、工具调用 +0.67，样本不支持效率改善。因为只改变了 Harness 输出处理，模型/缓存状态并未严格配对，时延和调用差异为描述性，不作因果解释。
- 报告评分缺陷及修复：报告重建把 oracle 的顶层 `status=aligned`（事实对齐）误当作来源完整性标志，导致真实评分被覆盖为不可验证。现由 `apply_redacted_assignment_oracle` 分别读取对齐状态和 `date_evidence_status`；对于 partial source 保留部分成功，遇到来源外日期仍判失败。批次重建结果现为 0 成功、3 部分成功、0 失败、0 不可验证。无需重新访问校园资源即可恢复报告结论。
- 验证：`tools/campus_bench/test_runner.py` 36 passed；API `test_agent_runner_reuse.py` 14 passed（1 条现有 Starlette/httpx 弃用警告）。三个修改后真实 run 都有安全聚合的 grounding event；报告不保存课程名、作业名、ID 或具体日期。
- 保留理由：真实轨迹证明同一窄任务下已消除来源外日期，并继续明确数据不完整。只证明 BUS-09 的受控缓存路径改善，不推广为整个作业工具族、校园来源完整性或 Agent 全局能力改善。
- 仍待验证：`next_by_course` 分页超过首屏时，工具/Harness 是否穷尽并正确合并结果；其他作业任务的工具选择和实时刷新行为；登录和浏览器授权路径（本批明确不触发）。下一优先级是在隔离合成数据上覆盖分页/部分结果和其他工具族后，再做真实配对测试。

## BUS-09 第四轮：分页游标完整性状态

- 发现：代码审查发现渲染器把“任意页返回 next_cursor”永久等同于结果未完成；若模型成功续读，最终仍会误报不完整。这是此前没有覆盖的页链状态缺陷（代码推断，未在上一批真实轨迹中复现；上一批 17 行未产生 next_cursor）。
- 改动：渲染器现仅在 Harness 轨迹中将专用工具调用参数与成功结果按 call ID 关联，统计生成但未被后续成功页调用消费的游标。仅此类游标会触发“结果仍有未读取后续页”提示；游标值仅在内存中处理，不落报告。缺少调用/结果关联证据时保持不完整提示，避免误报完整。
- 验证：增加两页全续读与首屏游标未消费的 API 合成 Harness 测试；`apps/api/tests/test_agent_runner_reuse.py` 15 passed（1 条既有 Starlette/httpx 弃用警告）；工具分页契约 `apps/api/tests/test_agent_tools.py` 38 passed。原有 SQL/工具用例确认 grouped rows 可用 cursor 分页。
- 修改后真实 Agent 复测批次 `20261016-203618-e06ce5d1`：BUS-09 n=3、串行、同 provider、100 秒/任务、8 轮模型调用上限、无重试。三次均返回 17 行、未产生后续游标、0 来源外日期，评分均部分成功。平均 2.00 工具调用、2.67 模型调用、34.34 秒；相对上一批 3.00/3.33/35.64 秒的波动为描述统计，工具调用/延迟受模型随机路径和未配对缓存状态影响，不能归因于分页修复。真实轨迹确认本轮数据集未触及多页运行时分支；多页分支证据来自明确标记为合成的 Harness 回归，不冒充真实平台实测。
- 批次对照 `20261016-202719-251ddaf6` → `20261016-203618-e06ce5d1` 因 runner 哈希与缓存状态未配对，结论为评分不变、效率描述性波动，不据此宣称产品变快。报告文件存于后一批次目录。

- 工具选择信号：最新 n=3 中，run01 只调用专用逐课程工具（1 次）；run02 另调用 `courses.list(limit=50)`；run03 另调用 `assignments.list(limit=50)` 和 `courses.list(limit=50)`。专用工具本身已返回 17 行课程—作业—日期数据，因此这些追加调用与目标存在重叠，列为“疑似冗余”而非已证实无用（脱敏轨迹证明调用及范围，不能判断模型内部意图）。本轮下一候选实验是针对相同 BUS-09 比较完整工具暴露与窄工具集/工具发现，记录调用差异并确认是否损害其他需求；暂不改 Tool Schema 或做题目硬编码。

## BUS-09 第五轮：逐课程缓存状态契约与对照（2026-10-16）

- 基线：真实批次 `20261016-203618-e06ce5d1`，BUS-09 n=3、同 provider、串行、100秒/任务、每任务8轮模型调用上限、0重试。旧 `assignments.next_by_course` 只返回17门有已知截止日期的课程；三次模型调用数各为2、3、3（平均工具调用2.00）；另两次显式调用了 `courses.list`，其中一次还调用了 `assignments.list`。未说明的课程究竟是无作业、未知截止还是未同步，工具结果无法回答。
- 可证伪假设：让专用工具逐门返回当前授权本机课程的三态缓存状态（最近已知截止项、只有未知截止、无本机缓存作业），并将分页改为按课程ID续读后，模型无需再用通用课程/作业列表拼接；输出必须不把本机缺记录说成学校端无作业。
- 最小改造：仅改造 `assignments.next_by_course`。SQL 使用当前会话 course scope，按课程ID keyset分页；对每页课程用窗口函数选择截止最近的已知作业，另聚合缓存作业数和未知截止数；没有对应记录的课程返回明确状态。工具响应含缓存边界提示；旧的截止时间/作业ID游标拒绝并提示重新查询。响应渲染器展示已知截止表和无已知截止的各课程状态，分页仅在成功后续页消费游标时视为读完。普通 `assignments.list` 行为保持原契约。
- 环境验证：一次备用 API 冒烟首先发现测试进程的 `CAMPUS_BRIDGE_URL` 默认仍指向8000，尽管批次 API 在8010；该错误批次 `20261016-205514-dc0ad30c` 两次都走旧工具契约，已在 manifest 标记 `invalid_environment` 并从对照排除。显式将 Harness bridge 指向8010后，一次冒烟 `20261016-205832-3d5b1883` 成功返回33个课程状态。备用 API 未启动后台 worker；本轮不登录、不同步、不访问校园网。
- 修改后真实 Agent n=3 批次 `20261016-210107-fa3589ef`：每次33门/33个不同课程；17门有已知截止作业、16门无本机缓存作业记录，未观察到只有未知截止状态；0个来源外日期；日期表与工具数据一致，边界措辞明确区分“本机无缓存”和“学校端无作业”。3/3评分为部分成功，不计为已确认完整成功，因为本地快照不证明学校端覆盖全量。该环境同一缓存范围下课程列表也是33项。
- 工具选择和效率：新批次未调用 `courses.list` 或 `assignments.list`；每次仍调用 `connections.status`，其中2/3还调用 `campus.status`。这证明跨工具拼接消失，但status读取仍是疑似冗余。总工具调用均值从2.00升到2.67，模型调用从2.67降到2.33；平均耗时从34.34秒升到38.54秒（+12.2%），P95从41.29秒降到39.64秒（-4.0%），输入Token均值从11,278降至6,869。两批的runner/catalog哈希及缓存状态未配对，均值变化只作描述，不能归因；没有“更快”证据。
- 判定：保留工具契约改造，因为它真实增加了可确认覆盖范围（17→33门并逐门标出缺失状态），且去掉了通用列表拼接；不宣称整个任务完整成功或工具调用效率提升。下一轮优先评估无关 `connections.status` / `campus.status` 读取的触发原因，但先做可观测的工具选择基线，再单变量尝试工具筛选；不按固定题目硬编码。
- 回归：API `test_agent_tools.py` + `test_agent_runner_reuse.py` 共56 passed；Benchmark `test_runner.py` 38 passed（API测试有1条既有 Starlette/httpx 弃用警告）。工具地图更新为34领域工具、9原生组、35场景；31可运行，4个异常恢复场景仍因缺少确定性故障夹具阻塞。

## BUS-09 第六轮：状态预检调用单因素提示实验（2026-10-16）

- 实验假设：`campus.status` 与 `connections.status` 只读连接/session元数据，不能证明某个本机缓存项存在或完整。若明确告诉模型：专用缓存读取工具可直接回答时不需要先查状态，并保留用户明确询问连接状态时的调用用途，则纯缓存任务中的状态预检会减少。
- 唯一主要改动：只收紧这两个工具的 schema 描述；不改执行器、工具暴露集合、Harness或缓存数据。新增措辞解释状态工具的证据边界。更新后生成工具地图。
- 定向回归：`apps/api/tests/test_agent_tools.py` + `test_agent_runner_reuse.py`，56 passed（1条既有 Starlette/httpx 弃用警告）；Benchmark `test_runner.py` + `test_build_tool_map.py`，39 passed；`git diff --check` 通过。运行器与地图生成器均正常。
- 实测环境：真实 Agent API / 真实 provider / 本地真实缓存数据，独立会话、串行、BUS-09 n=3、单任务100秒、最多8次模型调用、总计24次、无重试。只读缓存；未登录、未同步、未访问校园远端。批次 `20261016-211023-cf50dd37`，完整报告 `var/campus-bench/20261016-211023-cf50dd37/index.html`。
- 修改前参考批次 `20261016-210107-fa3589ef`：相同场景/provider/预算，3/3部分成功；平均工具调用2.67、模型调用2.33、耗时38.54秒。`connections.status` 调用3次，`campus.status` 调用2次，共5次状态预检；通用 `courses.list` / `assignments.list` 调用0次。
- 修改后：3/3部分成功，逐课程返回33项（17项 `next_due`、16项 `no_cached_assignments`），0个来源外日期，均明确本机缓存完整性边界。状态工具调用0次；但run01额外调用 `courses.list` 与 `assignments.list`，run02/run03只调用专用工具。平均工具调用1.67（减少1.00），模型调用2.33（不变），平均耗时35.99秒（相对前批-6.6%），P95 39.07秒（-1.4%）。三次记录确认数均为0；外部登录事件仍不可观测。
- 判定：状态预检在这一个缓存任务上的触发减少有直接轨迹证据，且没有破坏当前成功条件；保留描述修改。整体工具调用均值下降与状态工具消失方向一致，但模型路径和运行环境未做严格配对，样本仅3次，耗时差异不能作因果结论。整体调用问题仍未解决：至少一次运行转而调用重叠的通用列表；进一步工具收窄必须先测相似任务是否受损。
- 覆盖/限制：API层真实 Agent 任务通过普通请求入口；批次 Harness层为 mocked-provider contract，不是这3次真实模型调用的额外事实；直接工具基础能力为合成数据测试。3/3仅部分成功，绝不计已确认完整成功。未覆盖其他个人任务、公开校园查询、真实远端认证或同步。
- 下一优先级：从 BUS-02 / BUS-09 已脱敏轨迹中分别统计 `assignments.list` 与 `assignments.next_by_course` 的重叠选择，再增加非固定题面的相似任务（指定课程、日期过滤、无缓存）作为保留集；先在相同数据/provider/代码配置下跑定向工具/Harness/Agent基线，再决定是否引入工具分组/发现，而不是只对 BUS-09 隐藏工具。

## BUS-10：作业列表课程关联与日期事实迭代（2026-10-16）

- 场景/覆盖：新增 BUS-10，真实 Agent API 本机缓存任务，输入要求按截止时间只列最近10项作业及课程名，不同步、不登录。全场景目录现为36项，32 runnable、4 blocked。工具评分新增可配置的精确工具、禁止工具及允许列入轨迹的安全参数核验，并分别记录 Harness 模型选择和后端执行。隐私轨迹只保留工具名、`limit`、`order_by`、记录数和哈希，不保存作业/课程内容。
- 修改前批次 `20261016-212237-727ac12b`：BUS-10 n=3、同 provider、串行、100秒、每任务最多8次模型调用、总24次、0重试；3/3调用 `assignments.list(order_by=due_soon, limit=10)`，也都调用 `courses.list`，工具均值2.00，模型均值2.67，平均23.12秒。日期 oracle 分别发现1、2、1个来源外日期，3/3失败。该场景在当时缓存快照有147项已知截止作业、17门课程、0项未知截止记录（仅记录聚合）。
- 第一轮单变量改造：`assignments.list` 每项结果加入所属 `course_title` 与 `course_term`，并在 schema 描述中说明该结果可直接把作业关联到课程；查询仅对本页结果涉及的课程批量读取标题，保持已有授权范围过滤。没有改 `assignments.next_by_course`、Harness循环或渲染器。API `test_agent_tools.py` + `test_agent_runner_reuse.py` 56 passed；工具排序、分页契约和跨课程授权仍通过。
- 修改后批次 `20261016-213144-bb393ceb`：n=3，3/3只执行 `assignments.list`，不再调用 `courses.list`；工具均值1.00（-1.00），模型均值2.00（-0.67），平均20.99秒（相对前批-9.2%）。日期仍3/3失败，每次有1个来源外日期。这证明课程映射调用减少，但没有改善日期正确性；不把效率变化完全归因于改动，因为模型生成有波动。
- 第二轮单一瓶颈改造：前批的运行轨迹已证明 Agent 会把工具返回日期重写出错，因此扩展可信结果渲染器，仅当用户明确要求数字 top-N、实际调用 `assignments.list` 的 `due_soon` 排序与 limit 与用户所求一致、所有行含课程标签、且轨迹不含其他工具家族时，才直接按工具行生成排序表；UTC 截止时刻统一转换为中国标准时间。通用提问、单门/逐课程任务、其他混合工具链仍保留模型回答。聚合 `response_grounding` 事件不保存内容、名称、ID或日期。
- 新增/定向测试：渲染器按可信工具值替换一个来源外日期、未知日期排序、去除跨页重叠、拒绝错误查询范围/缺失课程标签/混合副作用工具；最新 API `test_agent_runner_reuse.py` + `test_agent_tools.py` 60 passed，Harness合约与运行时回归 `test_campus_bench_harness.py` + `test_agent_runner_reuse.py` 上一轮24 passed；Benchmark `test_runner.py` + `test_build_tool_map.py` 43 passed。现有 Starlette/httpx 弃用警告仍在。
- 修改后批次 `20261016-213821-b2524ea8`：同场景/provider/预算 n=3；3/3全部运行时渲染了前10项作业、0个来源外日期，评分3/3部分成功（不是完整成功：只读本机快照不能证明学校端全量）。3/3未调用 `courses.list` 或同步；工具均值1.33，模型均值2.33，平均22.96秒。run01执行两次 `assignments.list`；参数哈希不同，第二次增加 `due_after` 参数键，实际值因隐私被脱敏，无法判定它是否必要。相对中间批次调用/时延差异仅作描述；事实准确性从3/3失败提升为3/3日期对齐部分成功有确定性 oracle 证据。
- 渲染重叠行保护：不同截止范围的多个安全只读结果可能覆盖同一作业。渲染器按工具结果中的作业 ID 只在内存中去重，然后输出 top-N；报告仅记录折叠的重复行数量，不保存 ID。增加合成测试验证重叠结果不产生重复表格行。真实批次 `20261016-214542-141fd016`：n=3均调用一次 `assignments.list`，均输出10行，`duplicate_rows_collapsed=0`，0个来源外日期、3/3部分成功；平均1.00工具、2.00模型调用，平均21.85秒。该批验证新代码路径，但未实际触发重叠去重分支；该分支证据来自合成回归。
- 批次报告：基线 [20261016-212237-727ac12b](../var/campus-bench/20261016-212237-727ac12b/index.html)、课程关联改造 [20261016-213144-bb393ceb](../var/campus-bench/20261016-213144-bb393ceb/index.html)、可信渲染 [20261016-213821-b2524ea8](../var/campus-bench/20261016-213821-b2524ea8/index.html)、去重保护复测 [20261016-214542-141fd016](../var/campus-bench/20261016-214542-141fd016/index.html)。四批均通过正常 Agent API 入口并调用真实配置 provider，但数据读取限本机缓存；不是学校远端认证/同步实测。
- 判定/下一步：保留课程标题字段（有直接的调用减少证据）与 top-N 可信渲染（来源外日期由3/3降为0/3）。新增精确工具评测能指出意外目录查询，评分不再用泛化 alias 遮蔽错误。后续先收集含安全 cursor/日期参数指纹的查询范围轨迹，再判断 BUS-10 的范围细化调用是否必要；同时补齐空缓存、未知截止和日期分页边界。不得将这些 BUS-10 结果推广到所有作业、多步骤计划或校园工具能力。

## jobs.read：零等待轮询与默认观察窗口实验

- 历史真实轨迹观察：已保存的 BUS-02 样本中，18次运行共出现58次 `jobs.read`；其中10次 `wait_seconds=0`，全部返回 `running`，工具耗时中位数28.11ms；3次在同一 job ID 上以零等待重复查询。另有6次 `wait_seconds>0` 的工具调用实际等待超过1秒。脱敏轨迹中3次零等待模型调用参数都只有 `job_id`，没有 `wait_seconds`；`JobArgs` 当前默认值为0，执行器因此只返回即时状态。这是执行行为证据；模型为何遗漏等待参数仍属推测。
- 重要区分：BUS-02 里多次 `wait_seconds=14` 后仍是 `running`，再等一段后转为 `succeeded`，符合服务端60秒有界窗口，不能一概判作冗余。BUS-10 的 `due_after` 二次筛选参数不同且值已脱敏，也没有证据称其重复或无效。当前仅把零等待、未完成且随即重复的作业读取作为优先排查项。
- 改动前依据：上面的 BUS-02 历史真实轨迹给出了省略参数时的零等待行为，但没有可安全复用的当前 job；因此把“模型为何省略参数”列为推测，不视作已证实的模型根因。
- 第一轮最小改动：`jobs.read.wait_seconds` 现在区分省略与显式 `0`。省略时，只有 `sync` 和 `review_generate` 在执行器内默认为最多等待14秒，仍受各 job 家族原有总窗口限制；其他 job 类型立即返回；显式 `0` 保持即时查询。没有改 Harness 循环、job 所有权、总窗口、重试或外部请求行为。
- 验证：增加直接工具合成测试，覆盖省略值让 sync 在同一次调用内观察到终态、显式 `0` 只读取一次。`test_agent_tools.py`、`test_reviews_agent.py`、`test_agent_gateway.py` 合跑结果为 102 passed / 1 failed；唯一失败是既有 gateway 测试仍断言工具 schema 总数为32，而当前注册表已暴露34个，失败与等待参数改动无关。完整日志中相关 jobs/review 与 Harness 用例均通过。工具地图已从注册表重建，`jobs.read` schema 显示 `wait_seconds` 可选且默认按 job 类型处理。
- 真实 Agent 后测限制：本机当前没有可安全复用的后台 job ID。触发真实同步会访问校园平台并依赖当前校园会话；生成任务/复习卷会写个人数据或产生模型成本。故本轮没有运行 jobs.read 的真实 Agent 三次重复对照，不能声称零等待已在当前真实 Agent 中复现或已改善用户任务完成率。历史真实轨迹仅作基线证据，合成 DB 测试仅作工具/Harness 契约验证。
- 判定：保留这个有界、兼容显式0的实现作为可验证的局部假设；当前只有单测层证据，尚未通过真实 Agent 质量门槛。下一步应为 job 生命周期建立隔离、可销毁 fixture 和标准 Agent 场景，覆盖 running→terminal、超时仍running、非等待型 job、取消及结果回传，再使用配对模型/配置重复至少3次；若无法隔离真实 provider 请求，则报告模拟与真实层分离，不把 fixture 结果冒充线上 Agent 实测。

## JOB-01 场景与 Harness 追踪补齐、BAS-03 实测（2026-10-16 夜间）

- Benchmark 目录由36扩到37：原33个场景保留，新增 AUTH-01、BUS-09、BUS-10、JOB-01。JOB-01 把 `jobs.read` 的真实 Agent 任务定义清楚，但因 runner 还不能给新隔离会话注入归属正确的 sync job，标记 `blocked_precondition`；当前共32 runnable、5 blocked。工具地图从生产注册表重建为34个领域工具、9组动态原生工具，`jobs.read` crosswalk 现显式关联 JOB-01。
- JOB-01 Harness 层回归：`JOB-READ-HARNESS-20261017` 由 runner 正常入口执行，6/6 passed。新增 API mock-provider/真实 FastAPI model+tool endpoint 测试，验证模型只发 `job_id` 时省略等待值、同步工具在单次调用内观察到合成终态，并按 call ID 将 job 结果回传模型。测试只用临时 SQLite 与合成状态，不访问校园网络。
- 本机只读预检：`preflight-job-20261017` 的健康检查与状态接口均 HTTP 200，但 `browser_session_authorized=false`、校园工作区 disconnected；连接器元数据显示 `xzzd=ok`、`zykt=service_unavailable`。没有发起登录、课程刷新或同步。一次匿名公开通知探测 `public-notice-live-20261017` 返回6条摘要，来源新鲜但 `completeness=partial`，范围仅登录页近期/置顶摘要，未读取正文或历史列表；它验证的是公开 adapter 读取，不是 Agent 自然语言能力。
- BAS-03 低预算真实 Agent 对照，真实 API、同一配置 provider `prv_c68d2878`、相同提示/seed、串行、0重试；调用上限是唯一主要实验变量，timeout随上限从60调至120秒（8-call任务约41秒内结束）：
  - `20261016-221049-c80cabd1`：上限4次模型调用，运行器在28.71秒处主动取消；当时已产生7个原生联网工具调用（2次搜索、5次抓取），但有2次 fetch 尚无结果事件，故最终回答被截尾、不可验证。这是预算中断，不能记为Agent本身答错。
  - `20261016-221246-13aab6c2`：上限8次，任务在40.80秒完成，实际5次模型调用、7次联网工具调用，0个未关联结果；但事实判定仍为不可验证：回答含21个链接，其中6个链接的域名、URL路径和引用标签均与本轮工具来源对齐，11个虽在官方文档域名内但没有与本轮精确来源/标签对齐，另4个落在允许域名之外。评分因此没有把“有工具调用/有很多引用”当作已证实正确。成本仍为null（没有可信价目表）。
- 评测器修复：这两条真实轨迹暴露 `tool_selection_evidence` 只从内部 `tool_started/tool_result` 统计已执行工具，漏掉由 Harness 原生事件记录的 `tool/call`→`tool/result`。现按 `callId` 在内存配对，只落盘规范化工具名和未返回结果的工具名，不保留 call ID。重建两个 BAS-03 报告后都正确显示选择且执行了 `web.search`/`web.fetch`；4-call批次明确显示2个未返回结果的 `web.fetch`，8-call批次为0。运行器测试 `test_runner.py` + 工具地图测试 `test_build_tool_map.py` 为44 passed；JOB-01 Harness 批次6 passed。
- 结论与下一步：4-call预算对本任务太低有直接证据；8-call足以结束这一条样本，但没有证据证明8是稳定预算，也没有真实任务正确性改善。BAS-03仅各跑1次，不能替代默认n=3；引用评分失败点仍是部分链接无法被本轮结果支持，而非网络不可用。下轮应单变量测试 schema/说明是否减少无关 `web.fetch`，保留精确 citation oracle，并以同配置、配对 n≥3 复测；同时先实现安全 job fixture 才能运行 JOB-01 的真实模型层。不要把两个单样本宣称成总体联网能力结论。
- 按默认重复数补测：`20261016-221927-cc00f13a` 使用同一 provider、BAS-03 场景与 seed，串行 n=3、每任务8次模型调用上限、全批24次、120秒/任务、0重试。3次都真实调用并执行 `web.search` 和 `web.fetch`，均有完整原生工具结果回传；调用数分别为7/5/6，模型数5/4/5，耗时42.52/34.12/37.80秒（均值38.15秒，P50 37.80、P95 42.52）。结果为1部分成功、2不可验证、0失败、0确认成功；成功率不因三次都完成回答而虚增。引用 oracle 对应为 run01 12/18项可对齐、2条域名不符；run02 8/8项对齐；run03 5/8项对齐、2条域名不符。最终摘要和动态事实仍需人工抽查。平均输入/输出 Token 14,991.67/2,441，API成本不可得。

## JOB-01 隔离 E2E 夹具落地（2026-10-17）

- 假设：可在不依赖真实校园平台与真实模型的情况下，使用正常对话/turn API、真实 Harness、真实 `jobs.read` 执行器及临时数据库验证会话归属、同步 job 有界等待和工具结果回传。
- 实现：`mock_e2e.py --scenario JOB-01` 在临时 SQLite 建立合成 running sync job 和独立会话归属回执，fixture 后台将状态更新为 succeeded；模型脚本必须只传 `job_id`，Harness 调用真实注册工具，结果再按调用关联返回模型。`runner.py mock-e2e --scenario JOB-01` 保存脱敏证据到 batch。JOB-01 现支持 `mock`、不支持 `real`；真实 Agent 场景仍未验证。场景总数仍37：32个 real-runnable、1个 mock-runnable、4个 blocked。
- 验收：`python tools/campus_bench/runner.py mock-e2e --batch-id JOB-01-MOCK-N3-20261017 --scenario JOB-01 --timeout 60` 默认 n=3，3/3 passed；每次均为两次脚本模型往返、1次 `campus_jobs_read`、终态 `succeeded` 且工具结果回传，均无校园服务访问。`COURSE-MOCK-E2E-20261017` 原课程 smoke 回归 1/1 passed。`test_runner.py` + `test_build_tool_map.py` 45 passed，Harness 6 passed，`test_agent_tools.py -k jobs_read` 3 passed。工具地图已重建。该层只证明 Harness/执行器的合成调度路径，不能证明真实模型选工具或真实校园任务完成。
- 对照结论：4-call样本在28.71秒被预算中断；提高到8-call后该样本完成，但仍不可验证；n=3的8-call批次没有预算中断。预算足够性在该场景有方向性证据，具体所需额度及全局适用性仍未知。8-call n=3内调用数/延迟波动说明搜索路径并非固定；目前没有证据支持针对所有联网任务加大预算。
- 评测器变化：`tool_selection_evidence` 现将 native call 与 result 按 call ID 对齐，并在脱敏报告中记录 `web.search`、`web.fetch` 为已执行工具。4-call样本的2个无返回 fetch 被标记；8-call真实样本均无未返回调用。原始样本保留 `evaluation_as_run`，`report` 以新 rubric 重建显示，避免把旧评分静默改写。当前仍没有 native 工具输入/输出内容的统一、持久化安全事实账本；引用评分只证明来源对齐，不证明摘要事实完整。
- 外部前置状态：只读本机状态探测 `preflight-job-20261017` 显示校园浏览器授权为 false、workspace disconnected；连接器最近状态分别为 xzzd ok 与 zykt service_unavailable。匿名公开通知单次请求 `public-notice-live-20261017` 返回6条近期/置顶摘要，约218毫秒，fresh 但 `completeness=partial`；没有访问个人课程、登录或刷新。该证据区分了匿名公开源可用与当前校园授权不可用。

## 工具选择轨迹映射修复与 BAS-04 真实补测（2026-10-17）

- 证据发现：全工具地图将 34 个域工具的 `real_agent_selected_run_count` 错误读成 0；同一 JSON 已记录每个工具的模型 schema 名与领域名两个不同键。真实保存轨迹中的 `model_call.selected_tools` 明确含 `campus_courses_list` 等模型名，而轨迹聚合先把它规范化为 `courses.list`，故报告侧索引错键。该问题已证实为 Benchmark 地图统计 bug，不是 Agent 没有选择工具。
- 最小修正：`build_tool_map.py` 的可见/选择/执行计数统一从 canonical domain-name key 读取，并抽出 `domain_tool_observation()`；新增回归测试覆盖 model name 与 domain name 不同的情形。`test_build_tool_map.py` + `test_runner.py` 47 passed。最终工具地图重建时扫描154条真实 API run / 35个含 schema snapshot 的批次；例如 `courses.list` 现在显示模型选择63个运行、实际执行64个运行，`assignments.next_by_course` 选择和执行均33个运行。选择少于执行的历史运行仍保留差异，不擅自推断原因。
- BAS-04 基线：通过正常本机 Agent API，provider `prv_c68d2878`，串行 n=3、同一场景、单任务120秒、每次最多8次模型调用、全批24次、0重试、runner seed 20261017；只请求并读取公开 URL `https://www.deepseek.com/`，不访问校园服务。批次 `20261016-223935-68822f53`。3/3 均执行一次目标 `web_fetch` 并收到非错误页面结果；run03 另 `load_tools` 一次。结果1成功、2部分成功、0失败/不可验证；工具调用分别1/1/2、模型调用均2次、端到端18.26/17.42/21.53秒（均值19.07、P50 18.26、P95 21.53秒），输入/输出 Token 均值2,905/734，成本未知。
- 评分与限制：两次部分成功来自确定性 lexical overlap coverage 9.6%/9.9%，略低于预设10%门槛；共享来源词分别21/24个、目标 fetch 结果各约1,840字符。该程序证明目标 URL 有关联成功读取且回答复用了来源词，不证明摘要选择与完整性。run01 coverage 11.1%。对存档同一输出做离线敏感度分析：coverage 阈值取8%、9%、9.5%时3/3 aligned；取10%或11%时仅1/3 aligned。由此证实当前 rubric 在阈值附近对措辞/长度敏感；这属于评测稳定性问题，不是已证实的 Agent 内容错误。未因结果调整阈值。
- 对照边界：历史 BAS-04 批次 `20261016-144420-a65067ad` 同 provider、n=3但 seed、目录 hash、runner hash和每任务调用预算（3次）不同，旧结果2成功/1部分成功，12.7/17.8/18.2秒。新批次描述性结果为1成功/2部分、平均耗时多约2.2秒；页面/采样/runner 条件未配对，不能归因于代码退化，也没有改动 Agent 或宣称改善。自动比较页已生成：[`comparison.html`](../var/campus-bench/20261016-223935-68822f53/comparison.html)。
- 优先级更新：当前本地授权状态仍为 `browser_session_authorized=false`、校园 workspace disconnected，近期 `zykt` connector 报 `service_unavailable`；真实私有课程/材料/学习计划及人工认证恢复不能从已跑的公开 Web 批次外推。已扫描的154条真实 run 中，`materials.search/read`、`plans.preview`、通知读取、复习卷、作业/计划写入和导出等多项域工具尚无真实 Agent 执行轨迹（注册和合成测试不等同真实验证）。下一阶段优先为只读材料检索/读取、计划预览及空/旧缓存建立安全 fixture 与对应真实缓存场景；写入/导出维持 mock 或专用测试账户，并先分层跑 direct/Harness，再尝试真实 Agent。外部授权步骤需用户实际完成，当前不绕过。

## BUS-03 材料检索→版本化读取 Harness fixture（2026-10-17）

- 假设：核心材料链路可通过正常 Agent public conversation/turn API、真实 Harness 和真实 `materials.search/read` 工具，在隔离 DB 中验证依赖参数及证据回传，不访问个人资料或校园服务。
- 实现：mock E2E 新增 `--scenario BUS-03`。临时 SQLite 只含一个合成课程、一份版本2大纲和一个合成正文片段；脚本模型先发 `materials.search(course_id, query)`，根据结果发精确 `materials.read(resource_id, expected_version, chunk_ids)`，要求最终轮收到相同片段。验证模型可见工具、正确顺序/参数、两次真实 executor 调用、两个 call-ID 结果与最终内容。场景目录 BUS-03 现兼有 `real` 与 `mock` 路径，真实模型状态仍未验证。
- 验收：[`BUS-03-MATERIALS-MOCK-N3-20261017`](../var/campus-bench/BUS-03-MATERIALS-MOCK-N3-20261017/index.html) 默认 n=3，3/3 passed；每次都按精确课程 ID 检索到1个合成命中，随后读取指定资源版本和片段 ID，3次模型往返/2次工具调用，最终片段与数据库 fixture 完全一致，结果均回传。`JOB-01-MOCK-N3-REGRESSION-20261017` 3/3 passed；课程 smoke regression 1/1 passed。测试：`test_runner.py` + `test_build_tool_map.py` 47 passed，Harness 7 passed，`test_agent_tools.py` 42 passed。所有 E2E 均为脚本模型、真实 local API/Harness/执行器、合成 SQLite；没有校园访问。该路径只能证明合成证据链路，不能证明真实模型会选择这些工具或真实课程资料可用。

## MULTI-02 资料/作业→计划预览隔离 Harness fixture（2026-10-17）

- 基线缺口：真实 API 日志扫描中 `plans.preview` 无真实 Agent 执行轨迹；此前只有直接工具/计划动作契约测试，没有通过 Agent API 的多工具结果依赖链。真实校园授权当前不可用，不能安全进行个人课程数据实测。
- 假设：即使不接触校园服务，也可用隔离合成数据库和真实 Agent API/Harness/执行器，确定性验证资料、作业与可用时间结果被依赖步骤接收，并验证 `plans.preview` 只生成待确认候选，不应用计划。
- 实现：`mock_e2e.py --scenario MULTI-02` 新增完整流程 `materials.search → materials.read → assignments.list → availability.read → plans.preview`。数据库每轮全新，仅含合成课程、版本化资料、作业、学习任务和可用时间；脚本模型必须逐步收到前序 tool result 才能继续。验收检查调用顺序/参数、五个真实本地工具结果、计划候选状态、待确认动作状态、最终来源利用，以及没有 `plans.apply`。最大6次模型调用/工具调用，单轮120秒，所有服务只绑定 loopback。
- 结果：[`MULTI-02-PLAN-PREVIEW-MOCK-N3-ELAPSED-20261017`](../var/campus-bench/MULTI-02-PLAN-PREVIEW-MOCK-N3-ELAPSED-20261017/index.html) 默认 n=3，3/3 passed；每次均五次工具调用、六次模型往返，动作 `pending`、计划 `candidate`，没有应用操作；材料原文和作业记录均传到最终回答。逐步轨迹记录模型/工具耗时、端到端耗时、工具状态、参数字段名、结果条数、coverage 完整性/缺失项数量、缺失/警告数量和错误码，不保存具体参数值。`assignments.list` 每轮均返回 `partial`、`coverage.complete=false`、1个覆盖缺口，原因是工具明确标记本机缓存不能证明学校端全量；成功判据要求作业 fixture 正确返回，不把该局部缓存声明误判为全量数据。对照是覆盖能力从“无端到端 fixture”变为“合成 Harness/执行器链路可验证”，不是产品完成率或真实模型能力的改善声明。场景目录仍为37个，其中3个有隔离 mock fixture；工具地图已更新。
- 限制：脚本模型预先规定了每次工具选择，因而不能测真实模型是否会选择正确工具、少调用或生成有用计划；合成数据库也不验证校园缓存新鲜度、数据完整性、个人授权或真实时区/课程安排表现。后续真实只读批次必须在授权有效时执行；否则保持未验证。
- 最新授权探测：[`MULTI-02-AUTH-RECHECK-20261017`](../var/campus-bench/MULTI-02-AUTH-RECHECK-20261017/summary.json) 显示本机 API 正常，但 `browser_session_authorized=false`、workspace disconnected，`zykt=service_unavailable`；不启动真实私有课程测试，不把凭据存在等同有效会话。

## ERR-02 空资料检索结果与缓存边界 Harness fixture（2026-10-17）

- 基线缺口：空结果场景此前只在真实模式下定义，缺少可复现的空缓存环境与 Agent/Harness 结果回传检查。
- 假设：用只含合成课程、没有任何资料的临时数据库，能验证 `materials.search` 的空命中、工具 warning 回传、最终答复保留“本地缓存未找到”边界，并且不触发同步。
- 实现：`mock_e2e.py --scenario ERR-02` 通过正常 conversation/turn API 和真实 Harness 调用 `materials.search` 一次；确定性 oracle 核验准确参数、零命中、`status=ok`、warning 中的“不据此推断要求不存在”、该结果到达最终模型轮、最终说明未刷新校园数据，以及没有同步调用。每轮临时数据库独立且只含合成课程；上限6次模型/工具调用和120秒，无校园网络访问。
- 结果：[`ERR-02-EMPTY-MATERIALS-MOCK-N3-20261017`](../var/campus-bench/ERR-02-EMPTY-MATERIALS-MOCK-N3-20261017/index.html) 默认 n=3，3/3 passed；每轮均1次 `materials.search`，零结果、status `ok`、1条 warning、结果已回传、无同步调用；总耗时约12.88–13.44秒（包含临时 API 初始化）。对照为“空缓存 Harness 路径未覆盖”变成“空结果的工具/回传/边界语义可确定验证”，并非真实模型或校园空缓存改善。
- 限制：脚本模型按预定逻辑使用 warning，不能证明真实模型总能正确理解空结果。空搜索 API 的 `coverage.complete=true` 只说明本地搜索已完成，不代表课程资料缓存完整；真正的跨平台资料完整性仍不可验证。ERR-02 现支持 real+mock，工具地图已重建。

## ERR-09 无效工具参数与纠正重试 Harness fixture（2026-10-17）

- 基线缺口：工具基础单测覆盖 Pydantic 参数限制，但缺少正常 Agent API/Harness 中“无效参数→结构化错误→模型收到错误→修正参数→成功结果回传”的端到端可复现证据。
- 实验假设：对 `materials.read` 注入一次越界 `expected_version=0`，真实 Harness 将在执行器访问数据前返回可辨识错误；模型收到后将参数改为 fixture 的版本2，只重试一次并读取到目标 chunk。
- 首轮发现：Harness 实际返回 `INVALID_INPUT`，并非最初假设的 `VALIDATION_FAILED`；首轮脚本 fixture 等待了错误码不匹配，导致脚本模型 provider 返回受控错误并触发后续重试。根据工具事件轨迹修正 rubric 与 fixture，未改产品代码或安全校验。
- 结果：正式 n=3 批次 [`ERR-09-INVALID-PARAM-RECOVERY-MOCK-N3-20261017`](../var/campus-bench/ERR-09-INVALID-PARAM-RECOVERY-MOCK-N3-20261017/index.html)：3/3 通过；每次两次 `materials.read` 调用，第一步 `status=error/error_code=INVALID_INPUT`，第二步版本2读取成功并返回一个合成片段；模型经过3次往返，最终回答与该片段一致。临时数据库和课程资料全为合成，校园服务未访问。该证据验证 Harness 错误回传及脚本化纠正路径，不证明真实模型总能自行恢复。
- 诊断结论：无效参数在工具执行边界被明确拒绝；Harness 把错误 envelope 作为 tool result 返回，Agent 循环继续，第二次调用成功。错误码契约已由运行轨迹证实为 `INVALID_INPUT`。限制是该值即使涵盖多类验证错误，也未提供字段级细分；真实模型会否据此正确修改参数仍待真实模型样本验证。

## BUS-09 第七轮：冗余候选调用重复测量（2026-10-17）

- 假设：若 BUS-09 的追加列表查询来自稳定的工具边界/schema 混淆，在相同 provider 与任务输入下重复执行应看到相似的多余工具选择；若路径变化大，则先不动产品工具暴露。
- 配置：真实 Agent API、同一 DeepSeek provider、BUS-09 连续两次、串行、每次最多4次模型调用、总预算8次、120秒/任务、0重试；只读本机作业缓存，禁止校园登录/同步。两次都是单独新会话。批次 [`20261016-234729-14cdc047`](../var/campus-bench/20261016-234729-14cdc047/index.html)。
- 结果：两次均部分成功，均3次模型调用、2次工具调用、0工具错误/0重试；平均端到端34.70秒（P50 30.13，P95 39.27），平均输入6,723、输出2,876 tokens，成本未知。两次都执行 `assignments.next_by_course`；额外调用分别为 `courses.list` 与 `connections.status`。只有 `courses.list` 命中本场景预先标记的冗余候选；没有一次重复 `assignments.list`。专用工具与结果均通过 Harness call/result 关联。缓存完整性限制导致两次仍为部分成功。
- 与同日 n=1 批次 [`20261016-233704-07ffb8b9`](../var/campus-bench/20261016-233704-07ffb8b9/index.html) 比较：n=1 有3次工具调用（专用工具、`assignments.list`、`courses.list`）；本批两次各2次，额外调用不同。结合先前真实 n=3 中“专用工具单独调用”和不同追加列表调用皆出现的记录，信号为不稳定的额外工具路径，未证实固定 schema 混淆，也未证明每次额外调用都无用。总模型调用数/耗时有波动，不做因果改进声明。
- 决策：保留精确工具与候选调用跟踪，暂缓修改产品 schema、工具分组或 Harness。下一实验应加入 BUS-02 与至少一个相似但真正需要课程/作业列表的保留场景，在可控固定数据下对比全工具暴露与工具筛选；先测工具/Harness，再做真实 Agent 重复，并检查非目标场景退化。校园认证不可用期间不触发远端校园数据。

## 本机授权状态复核（2026-10-17）

- 批次 [`AUTH-LOCAL-STATUS-RECHECK-20261017`](../var/campus-bench/AUTH-LOCAL-STATUS-RECHECK-20261017/summary.json) 只读访问本机 API，没有调用外部校园端点。API health 正常；校园工作区 `connected=false`、`browser_session_authorized=false`；连接表的 xzzd 状态仍为 `ok`，但最近检查时间为 `2026-10-16T12:27:21Z`；zykt 为 `service_unavailable`。因此不能把持久连接记录中的旧 `ok` 当作当前已认证会话，不能据此启动私有同步。WebVPN `ready` 仅表示本机连接器状态，不代表目标校园服务接受了会话。
- `apps/api/tests/test_agent_connection_sync_tool.py`：18 passed，覆盖最窄 `kinds` 参数、job 复用、auth_required 拒绝和错误边界；未访问真实校园网络。`test_agent_auth_resume.py` 当前验证仍是隔离 runtime/SQLite 回调测试；人工登录后同一原任务恢复尚无真实或 Harness E2E 证据。

- 后续认证恢复契约回归：`apps/api/tests/test_agent_auth_resume.py` 26 passed（1条既有 Starlette/httpx 弃用警告）。覆盖结构化 AUTH 错误 checkpoint、同一 conversation/原目标恢复、服务端可信只读 probe、认证冷却/通知、重启、取消、失败保留、窄课程权限和脱敏；这些是隔离 SQLite 与回调测试，不含真实浏览器登录、真实 Harness continuation 或外部学校数据。`ERR-10` 的正常 Agent API/Harness E2E 只证明停止路径，ERR-08 仍待更高层恢复测试。

## 全工具轨迹口径修正（2026-10-17）

- 新发现：旧地图把 `model_call.selected_tools` 作为“选择”计数，但真实 Harness 中 `tool/call` 是模型选中后调度层实际收到调用的直接证据；两者在历史运行中确有差异（例如 `campus.query` 的模型元数据计数为 0，但 Harness 收到并执行 9 次调用）。因此旧图把真实请求漏记，不能用来据此排序工具选择优化。
- 修正：地图现将 `selected_runs/selected_calls` 定义为 Harness `tool/call` 事件，将 `model_selected_runs/model_selected_calls` 单独保留为模型调用元数据；执行仍由 `tool_started` 或 Harness `tool/call` 证据统计，并按 call ID 去重。对照样例 `courses.list`：99 次可见、66 次 Harness 请求、82 次调用 / 66 次执行运行；模型元数据为 65 次运行/80 次调用。`campus.query`：99 次可见、7 次 Harness 请求、9 次调用/7 次执行运行，而模型元数据没有匹配记录。两种来源并不等价，不推断它们为何差异。
- 验证：新增单测证明模型元数据与 Harness 请求分开计数；`test_build_tool_map.py` + `test_runner.py` 52 passed；重建地图扫描 157 次真实 API run、37 个带 schema snapshot 的批次。此轮只修正观测，不改产品工具暴露/schema/调度，也没有声称真实任务成功率提升。地图中的 99/99 全工具可见信号仍值得做有控制的暴露 A/B，但应先修正并锁定选择观测口径。

## 复习卷工具按需暴露实验开关（2026-10-17）

- 证据与边界：新口径地图显示 34 个域工具中 17 个在所扫描 99 次真实模型运行里没有执行；其中包含生成/写入/导出工具，低频不代表无用或应移除。选 `reviews.*` 做单家族候选，是为了验证工具发现机制而非宣称其为已证实的业务瓶颈。需同时覆盖一个复习卷任务与普通课程任务，检查隐藏、加载和对照退化。
- 实现假设：只对该家族在实验组隐藏 schema，模型需要时调用 `load_tools(group="campus_reviews")` 获取四个 `reviews.*` 工具；这只改变模型可见 schema，不改变权限/执行策略。控制组仍显示全部工具，且其 `load_tools` 描述/枚举与原有组清单一致。API 必须以 `CAMPUS_BENCH_ALLOW_TOOL_GROUPS=1` 启动，Runner 用 `--defer-tool-group campus_reviews` 在指定会话启用 treatment；关闭 gate 时预检拒绝，未创建会话或调用模型。Harness 子进程收到 `CAMPUS_BENCH_DEFER_TOOL_GROUPS=campus_reviews`，profile 签名包含会话实验条件，避免跨条件复用。默认未指定组即全可见，未知组不会传递。
- 当前证据：agent-runtime 工具注册测试 10/10 passed，验证默认可见、实验组只隐藏四个 review 工具、加载整组后恢复、其他工具及控制 agent 不变；API runner reuse 测试 21 passed，验证 allowlist 及 profile signature 条件隔离。此前 `test_build_tool_map.py` + `test_runner.py` 52 passed；工具地图已重新生成并标记该组尚无真实模型 A/B。
- 兼容性和结论：默认生产行为不变。此轮只实现可切换实验条件和注册层回归，没有运行真实模型的复习卷任务，也没有测到端到端质量、Token 或耗时收益；不得据此推广到其他工具族。下一步在隔离/可控数据上分别用全暴露与按需暴露跑复习卷任务和普通校园查询，重复测量模型调用、工具请求、完成度及耗时；没有合适的可验证课程快照时保持这一限制，不填入个人校园数据。
- 对照回归：保持默认“全工具可见”，通过公开 Agent API 和真实 Harness 子进程，以脚本模型、合成 SQLite 资料/作业/空闲时间运行 `MULTI-02` 3 次；3/3 完成并经 oracle 确认计划仅预览未应用，校园服务未联系。批次 [`TOOL-GROUP-PLAN-REGRESSION-20261017`](../var/campus-bench/TOOL-GROUP-PLAN-REGRESSION-20261017/index.html)。这证明普通多工具路径的 Harness 回归，不是按需暴露 treatment 结果，也不是真实模型效果证据。Agent Runtime 全审计 87/87 passed。
- 覆盖映射修正：复核发现工具地图把 `MULTI-02` 的资料依赖误当成 `reviews.preflight`/`reviews.generate` 的家族覆盖。该场景只验证资料、作业和计划预览，未调用复习卷工具；已移除 `materials` 对 reviews 的别名映射，测试确认 review 工具显示“无直接匹配”。地图重建后，`test_build_tool_map.py` + `test_runner.py` 53 passed。复习工具专用任务仍缺场景和独立 oracle，这也是暂不宣称真实 A/B 结果的原因。

## TOOL-REV-01 复习卷工具按需暴露 E2E（2026-10-17）

- 新增专用场景：只做合成课程的复习卷预检，要求使用 `reviews.preflight`，禁止生成/导出，不访问校园服务。场景目录现有40个场景，32个 real-runnable、7个 mock-runnable、4个 blocked（类别可交叉）。
- 控制组 `TOOL-REV-CONTROL-SMOKE-20261017` 1/1通过：真实本地 API/Harness/执行器 + 脚本模型、合成 SQLite；1次工具调用、2次模型往返，端到端16.45秒（含启动），预检结果被工具执行器确认后用于最终回答。它不是真实模型/校园服务实测。
- 后续控制组 `TOOL-REV-CONTROL-N3-20261017` 3/3通过：每次均为 `campus_reviews_preflight` 1次、模型2次；端到端16.14–16.57秒（均值16.35秒），执行器耗时61.51–90.16毫秒。这个重复性仅适用于本地脚本 Harness 路径。
- 轨迹口径修正后重跑 `TOOL-REV-CONTROL-N3-TRACEFIX-20261017`：3/3仍通过，均为2次模型往返、1次工具调用，工具结果确实出现在最终 provider 请求上下文（`provider_received_tool_result_before_final=true`）；端到端16.38–16.49秒，执行器60.67–74.41毫秒。控制组 schema 未隐藏符合预期。旧 smoke/n3批次保留，但以 TRACEFIX 批次作为已修正汇总字段的报告证据。
- 首轮处理失败批次（`TOOL-REV-TREATMENT-SMOKE-20261017`、`TOOL-REV-TREATMENT-TRACEFIX-SMOKE-20261017`、`TOOL-REV-TREATMENT-API-SCHEMA-SMOKE-20261017`、`TOOL-REV-TREATMENT-DIAG-SMOKE-20261017`、`TOOL-REV-TREATMENT-DIAG2-20261017`）均保留。轨迹逐步证实两个独立问题：其一，插件作用域的复习工具不在 `restrictableNames` 中，不能靠 Harness `restrict()` 隐藏；其二，`runner.mjs` 对 Harness runtime 子进程的环境 allowlist 漏传 `CAMPUS_BENCH_DEFER_TOOL_GROUPS`，导致 `load_tools` 的 group enum 不含 `campus_reviews`，脚本模型持续遇到 provider 失败。两项都有轨迹与代码路径证据。
- 最小修复：在 API 模型网关按 conversation 的 allowlisted benchmark group 过滤 provider schema；仅当此前工具角色消息含成功的 `loaded_groups=[campus_reviews]` receipt 后才开放复习 schema。Harness 执行注册表和权限不变。将 `CAMPUS_BENCH_DEFER_TOOL_GROUPS` 加入 runner→Harness 明确子进程环境 allowlist，使 loader schema按 treatment 允许该组；不继承其他 `CAMPUS_*` 环境。未为 `campus_reviews` treatment 的非网关 provider 放行，API 在建会话时拒绝该实验条件。
- 评测器修正：原 oracle 把原生 `load_tools` 调用误当成校园域 `tool_result`，造成即使 Harness 路径完整也判失败。现在单独记录经过脱敏的事件 kind、工具名、状态/错误码，分别验证 Harness `tool/call` 顺序、领域工具执行、schema 前后可见性和结果回传。API schema 过滤契约新增网关测试。
- `TOOL-REV` 最终配对脚本 Harness 实测：控制 `TOOL-REV-CONTROL-RECEIPT-LINK-N3-20261017` 与处理 `TOOL-REV-TREATMENT-RECEIPT-LINK-N3-20261017` 均3/3通过，runner hash、环境、repeat=3 和 timeout=90s 匹配。控制每次2次模型往返、1次 `reviews.preflight`，平均16.07秒（范围15.51–16.40）；处理每次3次往返，Harness 调用 `load_tools → campus_reviews_preflight`，初始可见 schema 从54降至50，结果均在最终模型请求前回传，平均16.70秒（范围16.39–17.16）。处理比控制多1轮模型，均值多634毫秒；时间范围重叠。该任务明确要求预检，实验验证了按需发现链路及其开销，不证明真实模型效果或总体质量提升。最终 HTML 对照位于 `var/campus-bench/TOOL-REV-TREATMENT-RECEIPT-LINK-N3-20261017/comparison.html`。
- 未涉及该工具族的保持性回归：`MULTI02-REV-CONTROL-TRACE-ALIGNED-N3-20261017` 与处理 `MULTI02-REV-DEFER-N3-20261017` 均3/3通过；两组每次6次模型往返、5个领域工具调用、同一调用顺序。处理组所有6轮都隐藏复习 schema、没有调用 `load_tools`。控制平均17.90秒（范围17.62–18.33），处理平均17.64秒（范围17.26–17.97）；范围重叠，不据此称效率提升。配对页 `var/campus-bench/MULTI02-REV-DEFER-N3-20261017/comparison.html`。两项实验全用真实本地 API/Harness/执行器、脚本模型和合成 SQLite，不是真实模型或校园服务结果。
- 判定：保留窄范围 API schema treatment、子进程 env 传递与 oracle 修复；它们建立了可复现的动态工具暴露对照机制，但未证明真实模型改善，且目标任务多一轮模型调用。默认生产仍全工具可见；应先在真实模型、受控 provider 和 holdout 校园任务上验证任务完成质量与成本，再考虑产品化推广。

## BUS-10 schema-only follow-up (2026-10-17)

See the controlled [assignment/course tool experiment](campus-agent-tool-experiment-bus10-2026-10-17.md): with the updated runtime schema confirmed in traces, BUS-10 repeated 3/3 partial success and no longer called `courses.list`; tool calls fell from 2 to 1 per run. The earlier treatment attempt used a stale API schema and is excluded. Timing changes are descriptive because cache/network state was not matched.

## JOB-01 隔离 Harness 复测（2026-10-17）

- 批次 `JOB-READ-RECHECK-N3-20261017` 对 `jobs.read` 的 running → succeeded 路径运行3次，均通过。每次通过正常公开 Agent API、真实本地 Harness 与执行器，脚本 provider 两轮往返；1次 `jobs.read` 读取和1个 call-ID 关联的 tool/result，最终 turn succeeded。各 E2E 耗时14.97–16.29秒（含隔离 API 启动）。
- 真实性：独立临时 SQLite 与合成 job 状态，未调用校园服务；这只证明执行器有界等待、结果回传和隔离清理工作，不证明真实模型会省略 `wait_seconds`、真实同步耗时或用户任务完成率改善。历史轨迹里的58次 jobs.read、10次零等待仍是高频真实行为信号，但须在真实可安全复用 job 或匹配的真实 Agent 场景下再次验证。
- 报告：`var/campus-bench/JOB-READ-RECHECK-N3-20261017/index.html`。这一复测没有新增产品代码修改。

## BUS-11 课程目录保留集与隐私评分器（2026-10-17）

- 新增 BUS-11 持有集验证 BUS-10 的 schema 收窄是否误伤明确的课程目录请求；缓存只有聚合预检结果记入环境，不保存课程名。当前场景目录41项：33个 real-runnable、7个 mock-runnable、4个 blocked。
- 首两批 BUS-11 都真实选中并执行 `courses.list(limit=5)`，但评测器对返回事件/脱敏内容的处理存在缺陷，故其分数不可作为 Agent 成败：一是 `tool/result` 省略 call ID、以 `sourceEventSeqs` 关联 `tool/call`，旧 tool-selection 只认 call ID；二是 report rebuild 对脱敏输出统一覆盖为不可验证，未保留课程结果的安全聚合 oracle；第一次场景还使用了与真实 Harness 名称不匹配的宽松 alias。三批脱敏轨迹与 schema 快照证实这些是评测链路问题，不是 Agent 未选工具。
- 最小改造：Harness 请求/结果关联新增按 `sourceEventSeqs` 回溯；BUS-11 使用专门确定性 oracle，在内存逐项比对课程名与学期及缓存边界，只落盘条目数/匹配数/状态，不落盘名称、ID、学期或回答正文；report rebuild 只恢复这个 allowlisted aggregate。
- 修改后批次 `20261017-022743-6e9d91eb`：真实 Agent API/current-code 隔离服务、共享本机 SQLite、本机真实配置 provider、串行 n=3。三次均选中并执行 `courses.list(limit=5)`，没有作业/同步工具；每次5/5课程名、5/5学期匹配，均说明本机缓存边界，3/3成功。平均1次工具、2次模型、18.07秒；账户数据和答案均保持脱敏。课程准确性来自脱敏前确定性对照，而非模型自评。
- 改动验证：Harness source-event-sequence 定向测试2 passed；课程目录 oracle/脱敏 round-trip 合成测试通过；`test_build_tool_map.py` + `test_runner.py` 60 passed。地图重建目前扫描178次真实 API runs 与43个schema快照批次。此次验证了作业工具 schema 修改在该课程目录持有场景没有退化，不代表全工具体系已改善。详细数据见 [BUS-11实验记录](campus-agent-tool-experiment-bus11-2026-10-17.md) 与 `var/campus-bench/20261017-022743-6e9d91eb/index.html`。
