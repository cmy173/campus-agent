# Agent 前后端 UX / 校园业务边界只读审计

## 口径

审计日期2026-10-15（Asia/Shanghai），源码为当前工作树，包含用户未提交前端/browser改动。审计员**没有修改任何现有源码**，仅新增本文。未读真实学生DB、凭据、学校cookie、真实对话/浏览器会话日志；未启动学校连接或收费模型。行号为审计时工作树，并行修复另标注。P1=可能错误承诺关键事实/执行结果；P2=可靠完成/恢复；P3=展示或规模。源码风险不等同实测失败。

## 1. 实际状态与连续对话

- 新对话需模型配置与发送授权（web `AgentPage.tsx:85-95`）。API再检查consent/协议/scope，保存provider及创建时所有课程/connections快照；预热不证明连通（API `routers/agent.py:270-288`）。
- 发送保留draft/attachment及幂等key，阻止并发；202后journal queued→running→succeeded/failed/cancelled/interrupted（web `useConversationController.ts:79-120`；API `routers/agent.py:338-357`、`agent_bridge/store.py:145-173`）。
- 实际进度是poll而非SSE：conversation活动时650ms整份turns；每TurnView 600ms按seq拉100events，失败2秒重试，终态继续分页（controller:28-35；`useTurnEvents.ts:21-45`；store:243-247）。
- Harness投影工具/子任务，领域tool_result另存coverage/freshness/evidence/job；领域错误是HTTP成功内error envelope，模型可继续（web `turnProjection.ts:61-110`；API router:454-494）。
- stopping立即撤token，等待工具/runner/browser清理后才terminal；UI可以重试未完成停止且禁止新发。停止对话与后台job取消分开（store:228-237；web `TurnView.tsx:81-99`；API tools:378-380）。
- 确有持久连续历史：重启活动轮改interrupted、保留调用不自动重放；runner按conversation复用，冷启动恢复官方JSONL（store:85-89；runtime:315-330,370-402）。本次reuse测试为FakeProcess，不是真LLM连续问答。
- provider原因优先generic Harness错误（runtime:195-210,409-435）；UI展示model_error、turn.error、连接恢复/子任务失败（TurnView:94-109）。
- 文件上传/成果快照有owner隔离及预览下载；聊天附件不自动等同课程Resource/已解析正文（router:302-335,524-539；runtime:341-343）。
- 预览不生效；可信UI接受具体action后随turn传action_id，模型文字不能伪造批准（AgentPage:79-82；controller:87-98；router:348-353,368-381）。

以上web路径均为 `apps/web/src/features/agent/`；API路径均为 `apps/api/app/`。

## 2. 严重度发现

### P1：排程没有自动纳入校园课程/活动硬阻塞

`apps/api/app/plan_engine.py:208-237`只读LearningTask、Availability、existing locks及调用者temporary_blocked/windows，没有campus timetable。`plan_input.py:20-31`revision不含课表/校历版本。安排自习避课依赖模型读events并手工blocked；遗漏/同步变化可冲突。**不能承诺backend保证避课**。建议合并已确认课程区间并记录上游版本，接受时复查；provisional/unplaced需确认。Lead本次不自动加默认阻塞，避免未验证课表改变计划语义，此项是后续工程机会。

### P2：academics指定day实际返回整周

`connectors/campus/academics.py:82-100`day→monday，116-158遍历7天；`campus.py:261-263`还返回today_agenda，后者也整周。明天下午必须按events.date/start/end筛选，API无hour参数。Lead已改`apps/api/app/agent_bridge/tools.py:163`描述明示整周/筛选/unknown边界，但未证明模型NLU遵守。

`connectors/campus/academic_defaults.py:53-64`fallback默认9月14起周一/2月20起周一、每季八周，usable但verified false，无假日/调课；`academics.py:110-114,140-146,152-168`保留provisional/unplaced/message。这是透明推算非官方确认，未知周次不能答无课。

### P2：相对时间基准不统一
API agent_bridge/runtime.py:329-330 hardcode Asia/Shanghai/+08；tools.py:389-390 preview用Availability.timezone；plan_engine.py:213-220同样偏好时区；web AgentPage.tsx:56历史上海。非上海偏好/午夜相对日期可不一致，应区分校园/用户时区，未知时澄清。

### P2：校园有后端能力，前端没有同等事实/来源卡片
API tools.py:322-329 campus.query/sync为data.records，academics为data.agenda；web TurnView.tsx:34-73只渲data.items中的assignment/task/course/material，112-115一键原文仅materials.read。课日期地点、通知来源正文、GPA精度依赖模型文字/technical输出。tools.py:356其他campus统一保守partial，未精细映射外层evidence/freshness；非假最新，但体验成本高。本次不动用户前端。

### P2：scope快照使长期会话随课程变化失能
API router agent.py:279-284创建scope；store.py:158-160禁止快照外course；tools.py:207-211完整workspace发现课程变化拒绝并要求新会话；web controller:135旧scope过滤选课。正确拒绝不应改模型自主扩权，应有可信scope refresh/重新同意UI，保留连续历史。

### P2/P3：恢复缺结构化重试、长历史成本高
已有网络不确定key复用（web draft:18-21/controller:97-116；API store:147-153）。失败/重启提示重发但无safe_to_retry/needs_reconcile/已完成副作用清单。browser回执注入明确executed不等于业务成功且勿重复（runtime:332-340），仍依赖语言。创建无幂等（controller:69-74）可重复空历史。store:120-123全量turns；web useTurnEvents:34全数组findIndex去重近二次；历史100且list next_cursor恒null（store:109-112、router:291-293），actions100（router:360-365）。

### 并行已修复初始问题
search原强制course_id现授权跨课及budget（tools:426-434/search.py）；read原20chunk现24000字符自动分页仍明示未覆盖（tools:172/read分支）；validation原generic现安全字段/type（tools:298-307），gateway白名单贯通（router:454-468）；academics整周描述。不是实际NLU提升证明。

## 3. 大学生候选场景：真支持与缺数据

| 场景 | 判断与边界 |
|---|---|
| 这周作业/没交事项 | 支持本机已同步导入作业；unknown截止/提交保留，学习完成不等于学校提交（TurnView:44-55）。不是全平台实时全量。 |
| 资料提交要求和原文 | 支持已解析当前版本子串search/read及位置；未解析/未读分页不能编造，聊天附件未自动变课程资料。 |
| 明天下午课/地点 | 条件支持缓存课表+校历整周再过滤；需day/hour/term/周次/节次/调课来源，location仅原始row有才回答，unplaced不证明无课。 |
| 下午有哪些活动 | 部分：登记校级/学院通知摘要正文；无结构化全校活动目录/全院系覆盖，未读附件/空分页不等于无活动。 |
| 自习地点/空教室/图书馆座位 | 无注册领域实时能力；缺开放/占用/座位/校区/预约资格；通用浏览器可访问不等于业务验收。 |
| 安排自习避课/活动 | 支持本地tasks/availability/blocked预览及可信批准；没有自动校园硬约束，缺可用窗口/课表不能推断空闲。 |
| 最优路线/准确ETA | 无确定性路线/交通能力；缺校园图/坐标/交通/路况，不能捏造精确分钟。 |
| 成绩/GPA/毕业剩余学分 | 缓存成绩及源GPA加权支持；academics.py:21-36不换算分制、不筛主修/替换重修、不等同官方均绩，培养审核未接入。 |
| 通知变资料/任务 | 支持缓存正文导入明确course再tasks.build真实job；保留来源版本及附件未读（tools:164-167），附件URL非已解析材料。 |
| 报名/交作业 | 学校连接器只读；受控browser有高风险批准回执，非每个系统提交已验收，executed不等于业务成功（runtime:337-340）。 |
| 复习题/成果 | UI有review library/editor和review API（TaskWorkspace:9-24），领域SPECS无直接review生成工具，通用文件成果非专门服务调用。 |

注册边界：API tools.py:119-145,158-183（courses/grades/profile/notices/college_notices、academics、通知读导入、历史导入、WebVPN）。没有地图、座位、全量活动、培养审核schema。

## 4. Config、日志与历史工件

- API config.py:24,30-32默认domain DB/files/secrets在var；runtime.py:614-616 agent journal及campus_workspace.py:144-150 campus cache受CAMPUS_AGENT_HOME控制。测试只隔离domain DB不够。
- apps/api/tests/conftest.py:15-18会drop_all/create_all/seed；apps/api/conftest.py:8仅setdefault DB。如果继承真实STUDY_DATABASE_URL可破坏数据，本次明确隔离。
- .env.example:1-21明示不自动加载，但混legacy OPENAI/notice/旧data路径；agent provider是DB配置快照，写.env不证明读取/连通。
- 已有loopback/Origin/cookie owner（router:140-174）、密钥仅可信端/owned subprocess（model.py:50-65,87-100）、stderr默认丢弃且不回显raw SDK（runtime:72-83,432-435）。
- log_redact.py:19-26,36-45仅剔除uvicorn access URL query，非任意日志通用脱敏。store.py:39-64,239-247明文保存text/response/tool arguments/results；个人本地产品仍需保留/删除政策，不能误称资料均不会留存。
- 仅统计工件类型/状态，不读日志消息/图片/凭证：docs顶层27个md/19个json；outputs顶层2个log/6个png。类型为baseline、clean rebuild、dependency、adoption、gateway/files/documents/tools validation、public connector/notice validation。
- 历史状态自述：agent-v2-validation-files.json=partial-goal-active，tools.json=increment_verified_goal_active；college-notices/connector的2026-10-12工件completeness=partial；notice extraction status=ok但verification_kind为公开页面+合成profile/local owner。不是本次实时成功，不证明真实学生登录或完整校内数据。

## 5. 安全离线测试实际结果与障碍

环境Windows PowerShell、Node24.19.0/npm11.17.0、API venv Python3.12/pytest9.1.1。无依赖安装/学校同步/收费模型。独立UUID临时目录，STUDY_DATABASE_URL/FILES_DIR/SECRETS_DIR/CAMPUS_AGENT_HOME/WORKSPACE全部隔离，worker关闭。

| 运行 | 实际结果 | 边界 |
|---|---|---|
| test_agent_tools + test_log_redact + test_env_integrity | 36 passed，2 warnings，2.76s | 内存/合成DB；Starlette httpx deprecation和pytest cache权限警告 |
| tests/test_academics.py::AcademicTests | 7 passed，1 warning，0.13s | 纯合成课程校历，无联网 |
| gateway/tools/browser/files/stopping/reuse/log_redact/env_integrity | 82 passed / 1 failed，26.83s | fake runner/synthetic provider；唯一gateway:232 calculator MCP ok=false MCP_CONNECTION_FAILED，不能记通过 |
| 初始两次同套83项 | tmp_path setup大量ERROR，cleanup WinError5退出1，无可信汇总 | TEMP及repo UUID都因pytest mkdir(mode=700)权限障碍；第三次仅本次UUID进程内mode700改777，不改源码/安装包/真实目录ACL |
| npm test -- src/features/agent | 启动失败未收集 | Vite externalize-deps/windowsSafeRealPath/execFile spawn EPERM |
| vitest configLoader runner重试 | 同样启动失败未收集 | 环境不是业务断言失败，未改用户源码绕过 |

Lead最终全API独立结果据其汇报360 passed / 1 failed（MCP诊断），72.87s；Node spawnSync实际EPERM。最终工件以Lead总报告为准。本次测试是确定性契约/fake runner/mock HTTP，不是真实LLM NLU；baseline/after scripted改善不能包装自然语言智能提升。

## 6. 给Lead的优先backend建议和新评测

1. 已修复跨授权course search、budget内whole read、安全字段/type恢复并gateway贯通：低风险高收益，继续保留strict/版本/coverage。
2. 后续优先校园时间与排程硬约束：day/window/timezone统一，已确认课表阻塞和revision校验；不确定时needs_user，不能默认修改用户长期偏好。
3. 结构化recovery：已完成副作用/可安全重试/需回执核对，executed与业务成功分离，不自动重放不确定提交。
4. 可信scope refresh/重新同意而非模型扩权；campus来源/完整性summary便于模型和UI核验。本次不改前端。

新增评测需分确定性契约层与实际模型层：
- 校园日窗矩阵：同周多日早午晚、跨周日到周一、午夜和非上海偏好；明天下午不能夹带别日/上午，输出requested_date/timezone。
- 不确定课表：unknown weeks/odd-even、无location、term未确认、fallback/假日/临时调课/stale/unplaced；没有events不能答无课，不编教室。
- 自习安全：availability冲课、confirmed课阻塞、未确认课澄清、预览后校园同步变化重新预览、未接受不写plan。
- 活动/座位/路线缺能力：说明缺全量/实时数据，选择可核验来源，不创建不存在工具、不补造精确分钟。
- 连续指代/恢复：多课程歧义后用户指定、再问那门明天；UI刷新/冷runner/停止/重启保留scope/history、不重复副作用。
- 实际配置模型在隔离sandbox运行：报告任务完成率、日期窗正确率、证据覆盖、缺能力诚实率、错误修复次数、延迟/预算、重复副作用；记录model/version/配置。脚本mock层与模型层分别报告。

## 7. 两项补充UX恢复风险（合计8项以上）

### P2：202响应体解析中断不按不确定网络结果保留幂等key
apps/web/src/lib/api/http.ts:93-118仅fetch调用catch映射NETWORK_ERROR；120在catch之外await res.json。若服务已enqueue并返回202、响应体截断/解析失败，得到SyntaxError；useConversationController.ts:113-116对非NETWORK_ERROR清pending，下次重发可新key/新turn。源码推导风险，未浏览器实测。建议transport对成功响应body读取失败明确uncertain outcome，保留原key直到服务对账；API应提供request-key查询/回执。

### P3：错误提示尚未显示新工具字段修复细节
web http.ts:39-46仅支持details.fields[].reason；新增工具反馈是error.details.validation_errors[].field/type（API tools:298-307/router:454-468）。TurnView:37、107主要显示message/code，不展示validation_errors。模型已可获得修复目标不等于用户可看到字段。可后续统一错误UI契约，但本次前端只读。
