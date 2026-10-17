> 最新架构级自查与实际修复：[architecture-transition-2026-10-15.md](architecture-transition-2026-10-15.md)，含27工具逐项审计、真实联网/官方模型、跨请求事务与并发故障证据。本页及 [full-refactor-followup.md](full-refactor-followup.md) 记录此前阶段，不代表当前全部状态。

# 校园 Agent 审计与已实施优化

2026-10-15，Asia/Shanghai。基线提交：`f5ebbbcc4ce7d163e44ed583b96cce06ff49bd9b`。保留进入工作区时已有UI/browser修改，不操作真实个人数据库、文件、凭据或学校登录态；只用合成内存DB/独立测试目录，没有调用收费模型或真实学校服务。

**结论：已发现并修复具体协议、工具作用域、读取预算和错误反馈缺陷；没有证明底层模型不够强，也没有证明真实自然语言任务成功率提高。** 动态发现、来源/覆盖率信封、可信动作确认和官方循环应保留，不必重写。

## A. 真实执行链路

```text
React draft / 附件 / provider consent
 → POST /api/v1/agent/conversations/{id}/turns（cookie/origin/scope/幂等）
 → SQLite journal queued → running；每conversation串行执行
 → Python Runtime拼接可信时间、当前课程、附件、已接受动作
 → owned Node runner（会话级复用，配置变化替换）
 → prepareProfile：官方dsh-base + 校园插件 / MCP / SDK persistence adapter
 → DeepSeekHarness SDK → stdio JSON-RPC runtime → 官方agent-loop
 → systemPrompt / history / 当前可见Tool Schema
 → 模型完成理解、工具选择与必要规划（没有独立关键词意图路由器）
    ├ 原生DeepSeek：官方流式provider，reasoningEffort=high
    └ campus-model adapter → Python可信网关 → 保存的Chat Completions provider
 → 官方loop调用工具
    ├ 校园领域24项：strict Pydantic → 服务端权限 → 业务/缓存/后台job
    ├ 原生read/write/shell/skill/search、Goal、schedule
    └ load_tools → 下一模型请求暴露browser/MCP/team/jobs/planning
 → 验证/渲染tool output，错误/来源/分页/覆盖率返回模型
 → 再次请求模型；可选native Goal继续，不注入自写续跑提示
 → root turn/end + Goal状态 → runner final/error frame
 → journal终态 → UI轮询messages（650ms）/events（600ms），展示证据/文件/确认卡
```

入口：`apps/web/src/features/agent/useConversationController.ts`、`apps/api/app/routers/agent.py`、`apps/api/app/agent_bridge/runtime.py`、`apps/agent-runtime/runner.mjs`、`prepare-profile.mjs`、`campus-plugin.mjs`。持久化为官方JSONL + `campus-sdk.mjs` cold resume，不是每轮失忆。SDK idle不等于native turn成功，需检查root outcome。

上下文压缩在上游compaction/basic/pruner中；profile `compression:'none'`是JSONL存储选项，**不是没有上下文压缩**。安装Basic默认阈值0.8、tail retention0.16、headroom65536。自定义campus-model容量识别/summary调用仍需实测；Python另有250000个JSON序列化字符护栏，与token非同量纲，可能先拒绝。没有额外长期语义记忆框架。

原生DeepSeek高推理/16384输出；自定义桥接非流式，仅文本/tool blocks，不转发reasoning，未显式统一temperature/seed，不能假定两条路径表现相同。预算128模型调用/512工具/1800s，**没有发现一般多步骤任务被极小迭代上限阻断**。来源、权限、停止、预览、幂等已有大量正确工程设计。

## B. 严重度诊断

逐文件证据及细节：[runtime.md](runtime.md)、[tool-system.md](tool-system.md)、[ux-backend.md](ux-backend.md)。下表位置采用函数名，避免并行编辑导致行号漂移；分报告有审计行号。

|级别/状态|位置与证据|影响/根因及处置|
|---|---|---|
|P1 已证实已修|`campus-tool-groups.mjs:applyMask`对scope-owned工具错误调用restrict；真实安装ToolRuntime测试抛异常|可阻断子Agent作用域创建，非模型能力。改用公开restrictableNames，保留scope-owned与独立策略|
|P1 已证实已修|`goal-lifecycle.mjs`只等SDK idle；安装SDK+fakeclient证实root error仍resolve|失败可能当成功。检查root turn/end.reason，child失败不污染root，native Goal仍唯一续跑者|
|P1 安全边界未E2E|`prepare-profile.mjs`danger-full-access/approval never；shell/MCP任意文件与course scope不同|提示词不等于强制所有联网脚本外发闸门，本机单人不是多租户沙箱。不擅改授权策略；保留现有browser/plan确认，后续须强制外发控制|
|P1 设计缺口未修|`plan_engine.py:208-237`/`plan_input.py`未纳入课表/校历版本|避课依赖模型手工blocked；不能保证调课后不冲突。未把未verified课程默默加入硬约束；后续版本化已确认区间|
|P2 已证实已修|`CampusModelAdapter.stream`旧字符串finish与rc.2 `{kind}`协议失配|修规范finish，tool-only/普通stop测试；真实规划影响未量化|
|P2 已证实已修|`tools.py:materials.read`默认20片段×6000，超24000直接失败|合法default需猜小limit。改whole-chunk自动预算分页，精确请求不静默丢，巨大单片段保留明确失败|
|P2 已证实已修|`materials.search`受限会话省略course_id就拒绝|跨授权课程检索冗余决策。内部query在打分/分页前过滤course_ids，HTTP不暴露授权参数|
|P2 已证实已修|`materials.search`可返回50份完整正文|挤上下文。单条摘要≤1200、总正文≤24000字符；标excerpt/offset/original chars/coverage，ID/version精确read|
|P2 已证实已修|`tools.py:execute`坏参笼统；gateway丢details|模型缺恢复信息。最多16项field/type，不回显值/未知字段名，gateway只传白名单|
|P2 已证实已修|HTTP200 status:error却canonical isError=false|业务与事件成功语义冲突。公开tools/execute仅映射inventory领域错误，完整content保留，partial/queued/needs_user不误判|
|P2 已证实已修|runner首次初始化在try外；SDK拒resume未dispose；MCP凭证parse诊断|初始化frame、拒绝handle回收、selected server字符串map和固定安全错误|
|P2 描述已修|`academics.py:82-158`day是整周，原描述指定日期|明示date/start/end筛选，unplaced/provisional不能证明无课；业务兼容保留|
|P3 已证实已修|loader空参允许、team漏3工具、exact tools实际整组|schema与发现说明修正；9/12→12/12分类；下一请求生效，不提升权限|
|P2/P3 风险未量化|UI 202 body解析异常可能丢幂等key；整份轮询/校园证据缺卡|见UX分报告。保留用户现有UI改动；后续uncertain outcome对账、增量poll、校园证据卡|

尚未验证：模型有工具却不调用、自然语言多轮约束遗忘、重复坏参、无意义调用、过早自然语言承诺、压缩丢约束。不能将代码/Mock对照当真实模型行为日志。

## C. 工具优化决策与代价

逐工具必要性/可发现性/参数复杂度/实际可用性见[tool-system.md](tool-system.md)；清单[domain-tool-inventory.json](domain-tool-inventory.json)、[browser-static-schemas.json](browser-static-schemas.json)。

- 保留24领域接口及版本/定位/覆盖率/权限。没有合并本地课程与教务课表、任务完成与平台提交、query与sync、preview与apply：这些差异有实际语义及确认意义。
- 高级工具按需发现已是现有架构，不冒充新增。修scope/漏分类/说明，没有凭想象削domain surface或重命名兼容的campus_campus_*。
- search为摘要发现→ID/version精确read。长命中可能新增一次调用，短正文不截断，原文仍在DB；REST search既有全文返回不变。
- read按整段预算分页，不有损总结。参数失败不回显输入；副作用确认、幂等不变。
- 24领域schema约14.2KB，25上游浏览器schema约18.1KB（另1个host确认工具），均静态序列化非tokens/每次完整请求实测。loader 1149→1479 bytes略增换来正确用法。未知未来工具保持可见，scope-owned受上游政策不能由此mask隐藏。

## D. 可核实评测

`tools/audit_agent_eval.py`20场景为**人工编排工具序列**：事实/歧义、search→read、跨授权课程/空scope、坏参/隐私、预算分页/续页、显式片段不丢、stale恢复、越权、未知截止/时区、build→poll、缺能力、空检索、多轮/普通聊天transport、摘要预算。

同数据、Python3.12.10、同评测文件哈希。baseline用本地git show加载原两个domain modules，不checkout或改用户代码；JSON含源/评测SHA256。新增用例是目标回归集合，**不是随机用户样本**。

|指标|前|后|限制|
|---|---:|---:|---|
|20离线目标契约通过|12/20|20/20|不是LLM任务成功率|
|default读8×6000|BUDGET_EXCEEDED|4片段+cursor|合法请求由失败变partial|
|8片段全读|首次失败|2调用、8唯一ID|无重复/遗漏的合成原文|
|跨授权检索不传course_id|INVALID_INPUT|授权命中；空scope空|先过滤再打分分页|
|8长命中search UTF-8响应|146586 bytes|32119 bytes（-78.1%）|非token，完整read增加一次|
|长命中search+read响应|预算断言失败|51207 bytes、2调用|没有声称调用数减少|
|坏limit恢复反馈|无fields|limit/int_type|模型是否修复未测|
|scope-owned mask|异常|不抛且保留policy|真实ToolRuntime非模型|
|team分类|9/12|12/12|非工具选择准确率|
|canonical领域错误|isError=false|true且content保留|真实ToolRuntime离线|

结果：[baseline-domain.json](baseline-domain.json)、[after-domain.json](after-domain.json)。没有编造tokens、全Agent成功率、端到端延迟、不必要追问数、选择/参数正确率或无效调用率。

### 实际回归

- 全API最终：**360 passed / 1 failed**，72.87s；唯一失败`test_agent_gateway.py::test_mcp_settings_actual_connection_diagnostic`。同环境最小`node spawnSync(process.execPath,['--version'])`明确EPERM；未到正常MCP连接，不删除/skip成全绿。
- 首轮另两项tempfile ACL失败，调整**仅测试进程UUID目录**的Windows0700兼容后通过。
- 新20领域+2安全error+4budget/scope均通过（包含全API）；原材料、权限、确认、幂等、回收等回归通过。
- Node纯本地测试：最终组合43项实跑全通过；实际安装Scope/ToolRuntime/SDK+fakeclient，不调用模型。原verify-tool-groups PASS。
- 前端`tsc -b apps/web` exit0；Vitest/Vite启动spawn EPERM，**未宣称UI行为测试通过**。
- verify-mcp受控模型脚本改为先load_tools再使用schema，但真实Harness/MCP subprocess受EPERM阻碍；未验证真实模型/校园/浏览器E2E。
- 未读真实用户会话日志；源码、既有测试、本次合成工件为事实依据，历史验收文档不作本次实测。

```powershell
# 根.venv当前没有SQLAlchemy，使用API环境
.\apps\api\.venv\Scripts\python.exe tools/audit_agent_eval.py --baseline-ref f5ebbbcc4ce7d163e44ed583b96cce06ff49bd9b --output .test-tmp/baseline.json
.\apps\api\.venv\Scripts\python.exe tools/audit_agent_eval.py --output .test-tmp/after.json
.\apps\api\.venv\Scripts\python.exe tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
Push-Location apps/agent-runtime
npm run test:audit
node verify-tool-groups.mjs
Pop-Location
node apps/web/node_modules/typescript/bin/tsc -b apps/web
```

baseline目标断言失败时exit1是预期。audit_regression所有DB/files/secrets/workspace/campus落独立.test-tmp UUID，ACL兼容仅该目录当前测试进程，不改生产权限。无新依赖/框架。

## E. 用户体验、未验证场景与下一步

[ux-backend.md](ux-backend.md)含8项体验问题与校园支持矩阵。目前可用课表缓存/推算校历、通知摘要/学院正文、资料/作业/任务/预览、授权浏览器、通用web search。无实时空教室/座位或可靠路径图，无完整活动报名系统。

[model-scenarios.json](model-scenarios.json)15个口语/多轮/多工具/失败/普通聊天候选，**没有运行**。真实对照应固定provider/model/API、推理/采样参数、synthetic服务快照和时间，每场景至少3独立会话；保留脱敏trace，人工按用户goal评分而不是工具数量。

指标：goal成功、必要工具precision/recall、schema参数正确率、重复无效比率、多步完成/恢复、非必要追问、tool/model calls、provider usage tokens、墙钟。source不足按诚实partial/必要澄清评分，不以编造答案为成功；外发须具体内容一次trusted确认。transport多轮测试不能测NLU。

主动放弃：额外意图分类层、二次planner、自写loop、向量库、长期memory框架、更多角色夸张提示词、扩大授权、重命名工具、未verified课表默认硬约束。暂无明确收益证据。

1. **工程可解**：shell/MCP强制外发闸门、校园时间语义高层过滤、已确认课表versioned blocked、字节护栏与token压缩协调、uncertain response保幂等/对账、校园证据卡/增量poll。
2. **数据/授权依赖**：真实教务登录/校历核对、通知附件覆盖、活动可报名状态、实时座位/空教室、校区路径图。提示词不能补数据。
3. **模型待证实**：口语歧义、隐含子任务、长期约束、失败重规划、少工具仍误选、跨域汇总。只有相同工具/数据/参数下较强模型重复显著更好，才有依据升级。

## 修改文件

运行时：campus-tool-groups.mjs、campus-plugin.mjs、goal-lifecycle.mjs、runner.mjs、campus-sdk.mjs、campus-mcp.mjs；verify-mcp.mjs+受控协议helper；package.json新增test:audit。后端：agent_bridge/tools.py、routers/search.py、routers/agent.py。新增Node/API回归、tools/audit_agent_eval.py、tools/audit_regression.py及本文证据。具体变更以git diff为准，**已有用户UI/browser改动不属于本次成果**。
