# BAS-05 最新校园通知：工具与服务诊断 — 2026-10-17

## 本轮工具可用性检查

运行 `python tools/campus_bench/runner.py probe --api http://127.0.0.1:8000 --batch-id TOOL-CHECK-20261017`。本机 API health、状态和连接器端点均返回 HTTP 200；这只证明传输可达。校园工作区显示 `connected=false`、`browser_session_authorized=false`；`conn_zykt` 为 `service_unavailable`，其状态时间为 2026-10-15。公开通知的匿名只读探测上一轮成功并返回 7 条新摘要，但覆盖为 `LIMITED_SCOPE`，不含正文或历史数据。

因此本轮未触发私有教务同步，也未尝试登录。传输可用不代表校园工具能力可用。

## Agent 实测

- Batch：`20261017-103807-2fe76d8e`，场景 BAS-05，真实本地 Agent API 与配置 provider，单次运行、并发 1、单任务 90 秒、最多 8 次模型调用。
- 实际 1/1 执行完成（非任务成功）；任务评分为“不可验证”。API health 未报告 agent 源指纹，故服务代码版本未知。
- 端到端 21.40 秒；7 次模型调用、7 次工具调用；观测到 2 次 INVALID_INPUT 和 1 次 WEB_BLOCKED_URL。修复评分器重复计算后，本轮工具失败为 3 次调用，而非 5 个重复事件。
- 每次模型调用的完整 schema 均含 `campus.query`、`campus.sync`、`web.search`、`web.fetch`；因此不是工具缺失。根据基准对 alias 的规则，搜索工具组首次调用即可用。
- 公开来源 oracle 能确认回答引用的 7 个 URL 均匹配实测来源、7 个日期均可验证；标题/来源标签对齐为 0/7。脱敏后最终文本不完整，故不能判定任务内容完成或整体正确。

## 已证实与待验证

**已证实**：当前工作区中 `campus.sync` 明确不支持 `wait_seconds`，而运行轨迹里两次传入该字段并得到 INVALID_INPUT；公开网页抓取返回 WEB_BLOCKED_URL；当前 API 未公布源码指纹。最新报告重新构建时，将观测 schema 与当前工作区注册表比对，确认当前 54 工具 schema 中有多项描述/参数差异，包括 `campus.sync`。工具本身并未缺席。

**尚未证实**：旧 schema 是否导致模型生成 `wait_seconds`；旧服务的具体来源；网页读取安全拦截是否是错误 URL 选择或受支持范围限制。当前 checkout schema 不能自动代表正在运行的 API schema。

## 当前源码隔离 API 复测

使用 `isolated_api_server.py` 启动 loopback-only 临时 API（`source` 条件、临时数据库、不含校园账号或 session），确认工具可见后，以同一 Provider 复测 BAS-05。批次 `BAS05-CURRENT-SOURCE-N3-20261017`：n=3、串行、单次最多 8 次模型调用、整批最多 24 次、无重试。三个 run 的 API 源指纹均为 matched，所有已观察的本地领域 schema 均与 checkout 匹配。

| 指标 | 当前源码 n=3 |
|---|---:|
| 成功 / 部分 / 不可验证 | 0 / 0 / 3 |
| 平均耗时；P50；P95 | 43.90 s；51.85 s；56.84 s |
| 平均工具调用 / 模型调用 | 7.33 / 6.33 |
| 可核验引用 | 0/3 |
| schema mismatch | 0 |
| WEB_BLOCKED_URL | 3 次（每 run 1 次） |
| 记录到的人工介入 | 0；外部 UI 行为不可观测 |

run 1 正常结束（23.01 s），只调用 `campus.query` 与 `campus.sync`，0 工具错误，日期 oracle 有证据，但回答没有可核验引用。run 2、3 各到 8 次模型调用预算后取消；它们额外尝试 web search/fetch 与校园浏览器导航/等待，均未产出引用。三个 run 都没有触发私有校园同步。样本受到两次预算截断与模型非确定性影响，不代表稳定延迟。

和旧服务 BAS-05 n=1 对照，旧服务为 7 次模型/工具调用、21.40 秒、3 次不同工具失败及 schema drift；当前源码 n=3 schema 全匹配，但任务仍 0/3 可验证，后两次也有网页读取失败并达到预算。这个比较不是单变量因果实验：旧 API 源指纹未报告、Catalog 与 runner hash 不同，账户/缓存/来源状态未配对。当前可确认的改进只有 schema 与 checkout 对齐，`wait_seconds` INVALID_INPUT 不再出现；不能声称任务成功率提高。

## 缺少正确工具的失败类型

BAS-06 记录了不同于 schema 漂移的能力缺口：校级公告正文/历史列表没有经过验证的工具路径。`campus.notice.read` 只接受学院通知 ID；`campus.query/sync` 只给近期校级摘要；当次 `web_fetch` 被安全策略拦截。已登记详情 URL 的匿名读取虽返回 HTTP 200，实际内容是登录页。可能需要新增受授权会话约束、只接受已登记缓存 `record_id`、固定访问 `zdbk.zju.edu.cn` 的详情读取路径；认证后页面结构和会话通路未验证，因此没有臆造解析器，也没有把它标成已实现。

## 本轮工程变化

- 报告现在比较模型调用时观察到的领域 schema 与当前 checkout，并仅保存哈希、差异计数和工具名，不保存 schema 文本。
- 工具错误统计按调用关联去重；required tool 别名会映射到具体可见候选，不会把“模型没选”记成工具不可用。
- 新增 BAS-06（expected_missing，blocked_precondition）：校级公告详情读取。真实只读探测中，列表来源仅给近期摘要；对已登记详情 URL 的匿名读取返回 HTTP 200，但最终页面是登录页。现有 `campus.notice.read` 只读学院通知缓存，因而没有可验证的校级详情执行路径。HTTP 200 不作正文成功证据。
- 工具地图已重新生成，包含 BAS-06；当前 44 个场景，新增场景被明确标记为能力缺失并阻断运行，不计入可运行任务成功率。
- `layers` 与命名 `run --batch-id` 在新批次时自动创建 manifest；恢复要求已有批次，重复批次拒绝覆盖，路径穿越 ID 会被拒绝。
- 离线四层批次 `TOOL-LAYERS-CONTINUE-20261017`：合成直接工具 20/20、认证/浏览器授权/会话/WebVPN 契约 76 passed、Harness dispatch 8 passed。均为 mock/契约数据，不计入真实 Agent 成功率。
- Benchmark 回归：`python -m pytest tools/campus_bench/test_build_tool_map.py tools/campus_bench/test_runner.py -q`，94 passed；`python -m py_compile tools/campus_bench/runner.py` 通过。

这些更改改善的是诊断可观测性和能力覆盖标注，不代表 Agent 任务成功率已提升。本批样本只有一次真实 Agent 运行，不能作为统计显著的工具效果对照。校级正文能力是一个经真实来源探测确认的缺口候选；认证后详情页面结构尚未验证，因此暂不臆造解析器或声称扩展工具已完成。

## 下一步

1. 在同一 schema 版本下改进 BAS-05 的引用完成率；3 次真实模型运行均无可核验引用，当前工具可见但 Agent 未可靠完成出处交付。
2. 用户完成官方校园登录后，在授权边界内确认详情页 DOM 和来源；再以固定主机、缓存 record ID 和严格解析器扩展 `campus.notice.read`，否则维持 BAS-06 缺失状态。
3. 对 schema 描述实验使用两个当前源码隔离 API、相同工具数据和模型参数，仅控制 `campus.sync/campus.query` 描述变化后重复 n=3；不要再用旧服务批次作因果基线。
4. 基于同版本证据再决定调整或删除工具；先补上真实任务缺失能力，再评估冗余工具。

## BAS-05 单变量描述改进复测

本轮继续先检查工具可用性：API 控制面可达，但 `browser_session_authorized=false` 且 `conn_zykt=service_unavailable`，所以仍只执行匿名公开通知读取，不执行私有校园查询。随后在隔离 loopback API 上用相同 Provider、BAS-05 场景、串行并发、每任务最多 8 次模型调用，对比改动前后各 n=3。两批均为真实模型与真实公开数据源的 Agent 请求链路；不是私有校园数据实测。

改动前批次 `BAS05-SCHEMA-GUIDANCE-N3-20261017` 的评分器没有保留来源 URL，三次都为不可验证；从原始轨迹重评可见其中两次产生 6/6 正确来源 URL，但标题标签未对齐，第三次无引用。改动只聚焦 `campus.sync` 的校园最新通知描述：直接触发一次窄范围同步，并要求以来源记录的精确标题作为 Markdown 链接标签、原样保留来源 URL。报告脱敏器只放行固定 ZDBK 公告详情 URL 格式，其余查询参数仍脱敏。

改动后 `BAS05-LINKLABEL-N3-20261017`：3/3 部分成功、0/3 完全成功、0/3 不可验证；每次 1 次 `campus.sync`、2 次模型调用、0 次工具错误，均有 6/6 来源 URL 匹配、6/6 标题标签匹配、日期可核验；无网页拦截或记录到的人工介入。平均耗时 16.75 秒，P50 18.79 秒，P95 18.88 秒。部分成功是因为公开列表真实提供的数据只有 `LIMITED_SCOPE` 摘要，不能声称完成了公告正文/完整范围任务。基线平均 16.89 秒，工具调用与模型调用数相同；本轮收益是可核验引用交付由 0/3 提升到 3/3（按新明确的标题对齐判据），不是任务完成率或效率的整体提升。

对照文件：[comparison.html](../var/campus-bench/BAS05-LINKLABEL-N3-20261017/comparison.html) 与 `comparison.json`。比较器确认只改变 `campus.sync` 描述，其他目标外工具 schema、模型配置、场景与评分标准一致；runner 脱敏序列化与目标 schema 指纹随实现更新，故完整源码哈希不同。两批隔离数据库相同类型但非同一个持续会话，公开数据有时间变化可能；n=3 只能视作方向性小样本，尚非统计结论。

这轮没有删除工具：实测显示 `web.search/fetch` 对该公告任务属于冗余路径，描述改动后模型不再调用它们；但没有证据表明这些工具对其他任务无用。需要优先补齐 BAS-06 的已认证详情正文读取能力，再扩展同一证据流程到其他工具家族。

## 8000 服务高预算真实请求复测

为响应“保证任务有足够执行预算”，对用户当前 8000 API 另跑批次 `BAS05-LIVE-8000-24CALL-N3-20261017`：真实 Agent API、真实 Provider、真实公网只读任务、n=3、串行、每次 600 秒/24 轮、批次 72 轮、无重试。预检通过；结果为 0 成功、0 部分、3 不可验证，平均 56.57 秒、P50 65.99 秒、P95 83.35 秒；每次模型/工具调用分别为 12、3、11 次，均未触及 24 轮预算。总计记录 2 次 `WEB_BLOCKED_URL`；没有记录确认或登录步骤。

关键归因证据：每次模型调用观察到 34 个领域工具，其中固定 10 个 schema 与本工作树不同，包含 `campus.query`、`campus.sync`、`campus.status`、`campus.notice.read`；API health 没有提供加载源码指纹（`not_reported`）。故这三次是“当前服务实测”，但**不能验证刚提交的 checkout schema 变更**。这比将失败归因给模型不遵循新描述更准确：服务实际暴露的 schema 已漂移，且运行源码未知。应先解决服务版本/schema 同步，再做单变量工具选择实验；没有擅自重启 8000 服务。

逐次来源 oracle：run01 1/1 URL 匹配、标题标签不匹配，回答还含 1 个未获工具结果支持的日期，至少需要 2 条来源故不可验证；run02 2/2 URL 匹配、0/2 标签匹配、日期通过，但因同轮触发 `campus.status` 而按隐私策略脱敏最终答案；run03 1/1 URL 匹配、0/1 标签匹配、日期不完整，最终答案因调用 `campus.notice.read` 脱敏。工具轨迹显示 run01/02 先读了 `campus.query` 再同步；run03 发生 3 次工具错误并转入 `campus.notice.read`。这些结果支持“无关校园工具被选择/重复调用”的问题，但当前服务 schema drift 是混杂因素，不能证明是最新 schema 的选择效果。

本轮提高预算没有改变这批任务表现，因为三次均在 24 轮前自行结束；预算不是此次首要瓶颈。更高优先级的是服务版本一致性、隔离公私数据查询路径，以及标题标签/日期 grounding。批次轨迹及原始 summary 位于 `var/campus-bench/BAS05-LIVE-8000-24CALL-N3-20261017/`；文件经过隐私脱敏，run02/run03 的最终文本不可恢复，评分器仅保留来源匹配与日期等聚合证据。

## 当前源码预算消融复测

为隔离上述旧服务 schema 漂移，使用新空数据库在 loopback 8017 运行当前源码，并再次先做工具检查（API、Provider、工具暴露预检通过；workspace disconnected、无浏览器授权）。批次 `BAS05-CURRENT-SOURCE-24CALL-N3-20261017` 与 `BAS05-LINKLABEL-N3-20261017` 使用相同场景、Provider、随机种子及串行运行；主要改变是预算从每任务 8 轮/90 秒提高到 24 轮/600 秒。前者 API 源指纹及 schema 均 matched，后者也 matched。真实公开来源、无校园登录；实验只读。

改动前 n=3 为 3/3 部分成功，平均 16.75 秒、每次 1 个工具/2 次模型调用。提高预算后为 1/3 部分成功、2/3 不可验证；平均 36.96 秒、P50 20.46 秒、P95 76.98 秒，平均 4.0 个工具/4.67 次模型调用。逐次模型/工具调用为 2/1、2/1、10/10；三个运行均未触及 24 轮上限。run01 6/6 URL 与标题对齐；run02 6/6 URL 匹配但仅 3/6 标签对齐；run03 搜索/网页读取及浏览器导航后出现 2 次 `WEB_BLOCKED_URL`，最终 1/1 可观察引用与标题对齐但无法验证整体答案。未观测到确认或人工登录。

这不是“高预算有害”的因果结论：三次模型轨迹高度波动，缓存/实时公开数据时点也未锁定。可确认的是这组任务没有预算截断，放宽上限没有带来可重复的整体完成提升；run03 的额外步骤反而增加了耗时和失败暴露。预算应保留为充足上限，同时降低无关工具的可见/调用机会，并让被阻断的网页读取及时收敛。此批与 8 轮批次的完整逐场景对照见 `var/campus-bench/BAS05-CURRENT-SOURCE-24CALL-N3-20261017/comparison.html`。

## 本轮直接工具可用性复核

批次 `TOOL-CHECK-ROUND-20261017` 先确认本地 API 传输可达、校园 workspace disconnected、无 browser grant，且连接器整体 degraded。默认探针不会直接调用领域工具；随后显式运行 `probe --public-notices`（`TOOL-CHECK-DIRECT-NOTICES-20261017`），对匿名公开通知同步执行一次真实只读查询：HTTP/执行无错误，返回 7 条记录，来源 `zdbk_public`，但 `completeness=partial` 且带 `LIMITED_SCOPE`（近期/置顶摘要、标题可能截断、不读正文与历史）。所以结论是 `campus.sync(notices)` 当前真实可用但能力边界有限；私有校园会话与 `conn_zykt` 仍不可用/未授权。探针仅保留 3 条脱敏样例摘要，不保存课程或会话凭据。

## 旧/未指纹服务的实测门槛

基于 8000 服务 source fingerprint 为 `not_reported` 且模型观察 schema 与工作树出现 10/34 工具差异，runner 现在默认要求 `/health.agent_source_sha256` 与当前 checkout 完全匹配，才允许真实 `run` 进入 Provider 查询、创建批次和调用模型。已用 8000 实际验证：未加覆盖时命令退出 2，报告明确未创建运行记录；另用无效 Provider 验证 `--allow-unmatched-api-source` 能显式越过源码门槛，但仍在 Provider 预检失败，未创建批次或调用模型。此覆盖只用于标注为旧服务诊断的数据，不可作为当前代码效果证据。`probe` 仍可用来诊断服务状态，不要求源码匹配。

随后在新建的 loopback 隔离 API 上验证门槛正向路径，批次 `BAS05-SOURCE-GATE-SMOKE-N1-20261017`：API 源码指纹 `matched`、Provider 预检通过、运行时 schema `matched`，正常通过门槛并完成 Agent 请求。该单次真实公开任务为部分成功（不是全任务成功），耗时 13.43 秒、2 次模型调用、1 次工具调用，没有网页拦截；无校园登录或私有数据读取。该 smoke 证明新门槛没有挡住匹配源码的执行，但 n=1 只用于门槛回归，不替代 BAS-05 的 n=3 结果。8017 隔离服务在验证后停止，未重启用户的 8000 服务。

## 隐私脱敏与公开任务评分边界

复查真实旧服务 run03 的脱敏轨迹后，确认它调用 `campus.notice.read` 并收到 `status=error`，却仍触发整个最终回答脱敏；同一轮的 `campus.query(capability=notices)` 属于公开摘要路径。`campus.status` 则不同：其 API 返回结构包含 workspace 状态和本地缓存快照，所以即使移除 credentials/browser-grant 字段，仍须按私有结果保护。runner 已改为：私有调用参数和结果始终脱敏；若调用成功返回数据或返回状态缺失/有歧义，最终回答继续脱敏；若所有私有调用都有可关联的显式错误且无数据载荷，则保留经通用密钥脱敏的最终回答，并正常运行公开来源 oracle。新增成功、错误空载荷和缺少结果三类测试；本轮尚未重跑模型端到端以观察该具体分支在真实模型轨迹中的频率。

## 隐私修复后的当前源码实测（n=3）

为按“每轮先检查工具可用性”执行，先用匹配工作树源码的隔离 loopback API 完成 API/Provider/工具暴露预检，并对匿名公开通知执行直接只读探针。API 与 Provider 可用；公开列表返回 7 条近期/置顶摘要，但完整度为 `partial/LIMITED_SCOPE`。校园 workspace 未连接且没有浏览器授权，因此未测试私有校园数据。隔离服务随后确认已停止（8017 无监听）。

批次 `BAS05-PRIVACY-REDaction-N3-20261017` 使用真实模型、当前源码 Agent API、匿名公开数据，串行 n=3，每次最多 24 次模型调用、600 秒，批次上限 72 次；无重试。3 次均未触及上限。汇总为 0 完全成功、2 部分成功、1 不可验证；平均耗时 41.27 秒，P50 20.00 秒、P95 85.43 秒；平均每任务 5.33 个工具调用和 6 次模型调用；记录 2 次网页服务阻断，未观察到 Agent 确认或用户操作事件。因校园摘要范围有限，即使引用可对齐也不能升级为完整成功。

工具选择出现明显运行间波动：两次只调用一次 `campus.sync`，各有 6/6 引用 URL、标题标签与结果对齐，日期可核验，但仍受 `LIMITED_SCOPE` 限制；另一次进行了 14 次工具调用，包括 9 次浏览器导航和 2 次 `WEB_BLOCKED_URL`，最终来源不足以核验整体回答。该轨迹未重复精确 URL 路径，因此不将其定性为重复抓取；更准确的诊断是公网/浏览器分支探索较宽、且引用交付不足。该 n=3 结果显示工具选择和耗时波动，但不能据此归因于刚修改的隐私脱敏逻辑：本批三次运行没有调用会触发“私有工具显式失败、空载荷后保留公开回答”新分支的私有工具。

**本轮优化及验证：** runner 根据私有工具实际关联结果的状态决定是否连带脱敏最终回答；私有参数/结果仍始终脱敏，私有结果缺失或歧义仍采取保守脱敏。`test_runner.py` 与工具地图导出测试合计 **99 passed**；runner 通过 `py_compile`，`git diff --check` 通过。历史旧服务轨迹的定向分析支持这个边界条件：明确私有工具错误且无数据载荷不应误伤可独立验证的公开结果；本轮真实 n=3 没有重新触发该路径，故隐私修复的端到端行为仍是待实测，而非声称已由真实请求验证。

**下一步优先级：** (1) 针对 BAS-05 的公网浏览分支做单变量工具描述/可用工具集对照，固定其他条件并至少 n=3；(2) 仍需补齐公开校园公告详情正文获取，或明确让任务限定在摘要；(3) 用户完成授权后再验证私有校园读取与恢复。当前证据不支持删除 `web.search/fetch` 这类全局工具，仅表明它们在这项公告摘要任务中可能造成额外路径和不稳定性。

### 当前隔离 Mock E2E 回归

在同一轮真实 BAS-05 后，使用普通 Agent conversation/turn API、真实本地 Node Harness/注册工具、脚本化 Provider 响应和临时合成数据，串行运行 `course-list-smoke`、BUS-03、JOB-01、MULTI-02、ERR-02 各 1 次。5/5 `verified_success=true`，各进程树、脚本 Provider 和临时状态均清理完成。它们验证 Harness 工具结果回传、依赖顺序、任务状态等待与局部故障恢复链路；属于隔离 Mock E2E，不计入真实模型/校园场景成功率。明细批次：`MOCK-REGRESSION-POST-PRIVACY-20261017`（course smoke）及 `MOCK-POST-PRIVACY-{BUS-03,JOB-01,MULTI-02,ERR-02}-{2,3,4,5}-20261017`。

## 工具暴露对照实验：预检中止与执行器修复

为验证“全部工具可见 vs 延迟复习工具组”的 BAS-05 对照，先确认 8000 和隔离 API 的工具状态。隔离 API 源指纹匹配，Provider 已启用且密钥存在；公开通知工具返回 7 条 `LIMITED_SCOPE` 摘要。无校园登录或私有读取。

控制组 `BAS05-ALL-VISIBLE-AB-N3-20261017` 使用当前源码隔离 API、同一 DeepSeek 官方 Provider、BAS-05、串行 n=3、seed=20261017、每任务 24 轮/300 秒、无重试，得到 0 完全成功、2 部分成功、1 不可验证；平均 16.57 秒，P50 17.94 秒、P95 19.22 秒；每次 1 个工具调用、2 次模型调用，无网页阻断。每次来源对齐与完整度判断仍以逐场景轨迹为准。

处理组请求 `--defer-tool-group campus_reviews` 时，API 拒绝其官方直连 Provider：该实验被有意限制为经校园模型网关发送变更后的工具 schema，不能通过 DeepSeek 官方直连运行。当前机器的 Provider 列表只有这一项，没有兼容的校园网关 Provider。因此，**没有有效处理组结果，不得声称此消融改善/退化**。执行器原本仅预检服务端实验开关，随后才创建对话；此次 3 次拒绝都发生在对话创建阶段，记录为 blocked，未触发模型调用，但污染了批次统计。

已修复 `runner.py`：Provider 预检现在无密钥地依据已登记协议和官方 DeepSeek 主机判定 runtime provider；启用工具暴露处理组时，若不是 `campus-model` 则在创建批次/对话前失败。只记录 `runtime_provider` 类别，不输出 base URL 或密钥。新测试覆盖官方直连、网关 Provider 与拒绝前置条件。验证：`python -m pytest tools/campus_bench/test_runner.py -q` **94 passed**；实际用当前官方 Provider 请求处理组，runner 退出码 2，明确报告网关要求，且目标批次目录不存在；`py_compile` 与 `git diff --check` 通过。隔离 API 随后停止，端口 8018 无监听。

本实验对照结论是**环境不兼容而未执行**，不是产品模型或工具选择失败。下一次若配置了兼容网关 Provider，才可按相同场景、模型和种子运行处理组；控制组本批可作为参考但最好同日重跑以降低公开数据漂移。

## 2026-10-17 continuation: current 8000 服务门禁复核

先重新执行工具可用性探针：本地 API health、校园状态与连接器端点可达；workspace 为 disconnected、`browser_session_authorized=false`；匿名公开通知读取再次返回 7 条 `LIMITED_SCOPE` 摘要。`conn_zykt=service_unavailable` 的 last_checked 仍为 2026-10-15，状态可能陈旧；本轮未对其触发重新认证或同步。

随后对 8000 当前 Agent health 直接计算 runner 预检：`status=ok`，但 `agent_source_status=not_reported`、没有加载源码 SHA。正式 `run` 默认门禁据此以退出码 2 终止 BAS-05 请求；目标批次目录不存在，未创建场景记录，也没有模型调用。这是已验证的评测环境/服务版本限制，不是 Agent 场景失败。没有使用 `--allow-unmatched-api-source`，也没有重启用户服务。当前源码 real API 结果继续以隔离 8018 批次为准；私有校园登录仍未验证。
