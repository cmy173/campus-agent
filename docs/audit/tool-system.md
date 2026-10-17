# 工具系统审计：确定性契约，不是模型能力评估

审计时间：2026-10-15，Asia/Shanghai。范围：campus-tool-groups/mcp/sdk、验证及 apps/api/app 工具实现只读审查。既有 UI/browser 用户改动未触碰；backend/plugin/runner 由各 owner 负责。

## 结论与baseline/after

保留全部24领域工具，不裁剪surface，不强制简单问题先规划。campus_campus_*重复前缀保留兼容。

已修：scope-owned subagent被mask调用restrict抛unknown global tool可能阻断agent创建；仅mask公开tools.view(agent).restrictableNames继承定义，自有定义保留可见。team补spawn_teammate/wait_agent/list_subagent_models。loader拒空参、去重限量、执行验证、报告未知/仍受policy限制名字；明确整组加载、下一轮schema才调用事实、非授权。planning说明加载不能开启plan mode。MCP credential/goal receipt JSON.parse原错误含私有输入前缀，现固定安全诊断及形状验证；MCP只注入当前服务对应transport。SDK否决saved goal时dispose尚未交付handle。verify-mcp原peer第一轮要求隐藏MCP schema为测试协议失配，现load→schema→call→final。

| 指标 | baseline | after |
|---|---:|---:|
| 首6个groups回归 | 0/6 | 6/6 |
| malformed credential回归 | 0/1 | 1/1 |
| 首3个SDK回归 | 1/3 | 3/3 |
| 最终扩展5文件测试 | 未全部原版跑，不填虚假总分 | 28/28 |
| 最终工具+runtime组合（含MCP results） | 无同等baseline | 43/43 |
| 原verify-tool-groups | PASS | PASS |
| backend tool tests | 26/26 | owner调整后组合读取/动作51/51 |
| 真实verify-mcp启动/重连 | spawn EPERM环境阻塞 | 同样阻塞 |
| scope-own subagent安装 | 异常 | 不抛、可见 |
| team分类12可选/配置名 | 9/12 | 12/12 |

Node默认test隔离也spawn EPERM，使用--test-isolation=none。MCP结果测试用公开createMcpToolDefinition+合成callback。SDK捕获ctx.extend proxy并阻止stdio；非持久化端到端。MCP helper为脚本化peer，不证明真实模型NLU。无真实服务、浏览器、模型、凭证。

复现（仓库根）：backend使用lead隔离runner覆盖DB/files/secrets/campus路径，不建议原默认pytest可能初始化真实campus。最终隔离组合51PASS；首次26项原工具测试只用in-memory DB，但import初始化风险是限制。
```powershell
node --test --test-isolation=none apps/agent-runtime/tool-groups.test.mjs apps/agent-runtime/mcp-config.test.mjs apps/agent-runtime/sdk.test.mjs apps/agent-runtime/mcp-verification-case.test.mjs apps/agent-runtime/mcp-results.test.mjs
node apps/agent-runtime/verify-tool-groups.mjs
node apps/agent-runtime/verify-mcp.mjs # 此环境spawn EPERM，非PASS
& apps/api/.venv/Scripts/python.exe tools/audit_regression.py apps/api/tests/test_agent_tools.py apps/api/tests/test_agent_plan_actions.py apps/api/tests/test_agent_job_actions.py apps/api/tests/test_agent_feedback.py -q -p no:cacheprovider
```

## 静态成本与共同约束

见domain-tool-inventory.json、tool-discovery-metrics.json、browser-static-schemas.json。紧凑JSON UTF-8 bytes，非token/请求总量/性能指标。domain仅name/description/parameters；loader整份runtime schema，不能直接相加。

| 项 | 数量 | bytes |
|---|---:|---:|
| after领域 | 24 | 14,197 |
| 当前Playwright注册 | 25 | input合计14,270；namespace模型schema数组18,091 |
| host额外prepare_action | 1 | 未含上项；总browser26 |
| loader before→after | 1 | 1,149→1,479，增加330 |

浏览器通过audit-browser-schemas.mjs公开InMemory MCP tools/list列出，browser factory抛错禁止启动，无网络。domain取backend after schemas，每工具成本列JSON。未测完整composition默认总量。

loader常可见；campus/schedule/goal/publish保护；未知新工具默认保留。沿scope chain仅释放自有mask；既有兄弟保留自己mask，新子scope继承父已加载状态；独立policy不解除。分类非安全边界。

domain Pydantic strict/extra=forbid，cursor≤512/limit1–50；host注入principal/conversation/course/connection/job scope。日期过滤需UTC offset。envelope包含status/data/evidence/coverage/freshness/warnings/cursor/job/error。queued非完成、本地学习非提交。MCP成功保留text+structuredContent，isError抛错，invalid result拒绝，signal传到call；4合成回归通过，真实重连未测。

## 逐领域工具：24项默认可发现

可用=本地实现/契约，不承诺学校凭据/服务可用。D=默认；campus.status模型名campus_campus_status，courses.list为campus_courses_list。

| 工具 | 必要性 | 发现 | 参数/恢复 | 可用性、结果、权限 |
|---|---|---|---|---|
| campus.status | 连接/cache判断 | D | 无 | 本机状态；模型不收密码 |
| campus.query | 缓存事实 | D | capability/platform枚举、query字典 | 不联网；completeness/fetched_at；connector校验query |
| campus.sync | 更新学校事实 | D | 同query | 网络只读+本地cache写；AUTH_REQUIRED；失败留旧cache |
| campus.academics | 日期课表/学分 | D | day ISO/term | 真日期校验；校历/绩点原精度，非官方推算 |
| campus.notice.read | 通知全文 | D | record_id来自query | cache ID非任意URL；附件未读声明 |
| campus.imported.read | 旧导入追溯 | D | batch/document成对或均无 | server配对校验；历史非实时 |
| campus.webvpn.entry | 登录入口 | D | service=cas/zdbk | 登记入口；不代填密码、不假称HTTP已走VPN |
| campus.notice.import | 通知入学习流程 | D | record/course_id | course授权；正文版本化、附件未解析 |
| connections.status | 平台发现 | D | 可选connection_id | connection scope过滤；不登录/sync |
| courses.list | 可信ID/消歧 | D | query/term/分页 | scope先过滤；多匹配needs_user；本机非学校全量 |
| materials.list | 解析状态 | D | course_id/分页 | course授权；metadata非正文；未解析partial |
| materials.search | 原文依据 | D | query/可选course/分页 | owner修server scoped跨课程；无命中非要求不存在 |
| materials.read | 完整要求/引用 | D | resource/version；chunks/cursor互斥 | resource→course授权；版本冲突；owner修whole-chunk预算分页；locator/coverage |
| assignments.list | 学校作业事实 | D | course/date offset/分页 | scope；未知deadline保留；本地完成非提交 |
| tasks.list | 本地任务 | D | course/status/date/分页 | scope+host允许无课程任务；非学校提交 |
| plans.read | 真实安排 | D | 可选plan_id | blocks/unassigned/warnings全部refs授权 |
| jobs.read | 后台真实性 | D | job_id | 会话owns/job scope；无payload；queued非完成 |
| availability.read | 防编造用户时间 | D | 无 | 完整workspace；configured=false明确 |
| plans.preview | 具体安排差异 | D | period/dates/blocked/windows/evenings | 完整workspace+request_key；不生效；晚上转换明示 |
| plans.apply | 接受安排生效 | D | action_id非plan_id | host/UI具体接受；模型不自授；持久回执 |
| plans.revoke | 用户撤销 | D | 撤销action_id | 接受的撤销动作；版本拒旧动作 |
| tasks.build | 材料生学习任务 | D | course_id | scope+request_key；真实job复用；规则非模型 |
| jobs.cancel | 停本会话后台任务 | D | job_id | owns；cancel_requested仅请求；非turn取消 |
| tasks.feedback | 用户已报告结果 | D | task/revision/outcome；minutes/difficulty/note可选 | scope+request_key+版本；未报告不编造；非提交 |

## 逐部署/上游工具

D=默认；G=继承定义按组加载；S=自有scope可能可见；O=配置可选。未修改上游实现，参数以实际schema为准，不把未启用插件当能力。

| 工具 | 必要性 | 发现 | 参数/结果/可用性/权限 |
|---|---|---|---|
| load_tools | 延期能力发现 | D | group/非空tools；整组；actual available；不授权限 |
| publish_file | 真交付 | D | path；metadata/hash快照；当前任意可读绝对路径，非workspace隔离 |
| pwsh | 本地计算/导出 | D Windows | command/signal；租约；danger-full-access/never |
| bash | 跨平台本地执行 | 非Windows默认 | 当前Windows禁用 |
| read | 输入/技能读取 | D | path/区间；文件观察；非交付 |
| read_image | 图像事实 | D | path；需image modality/attachment；custom文本桥不保证 |
| write | 生本地文件 | D | path/content；之后publish |
| edit | 精确文本修改 | D | 匹配文本/替换；observe/stale；不绕用户改动 |
| glob | 文件发现 | D | pattern/root；截断不等全量 |
| grep | 内容定位 | D | pattern/root；之后read |
| skill | 按需技能指导 | D | skill id；不预加载全部全文 |
| todo_write | 多步进度 | D | todos/status；简单问答不强制；非学校事实 |
| create_goal | 长任务目标 | D | objective/max_goal_rounds；非简单问答必需 |
| get_goal | 真checkpoint | D | goal引用；session归属 |
| update_goal | 进度/完成 | D | id/revision/action；模型不恢复paused |
| subagent | 独立委托 | G team/S | prompt/选路/background；子自报非独立证明；不扩大授权 |
| subagent_fork | 继承上下文委托 | G team/S | prompt；parent模型/history；冷进程需resume |
| list_subagent_models | 子模型路线 | G team/S | 可空provider/model；catalog非可用保证 |
| send_message | 子代理继续 | G team | target/message；continuable/one-shot；归属 |
| list_agents | 子进度 | G team | 空；inactive非完成 |
| interrupt_agent | 停子当前轮 | G team | target；请求非立即quiescent |
| job_list | 通用后台进程 | G jobs | 状态；区别domain jobs.read ID |
| job_output | 通用job输出 | G jobs | id/输出范围；保留截断/未完成 |
| job_kill | 停通用job | G jobs | id；观察退出；区别domain cancel |
| workflow | 有界脚本并行 | G jobs | script/PTC；非常用前置；网络权限独立 |
| web_search | 当前公开信息 | D | queries；需配置搜索服务，未外网验证 |
| web_fetch | URL正文 | D | HTTP(S)url；非学校登录态 |
| list_mcp_resources | 资源发现 | G mcp | server；零服务可空 |
| list_mcp_resource_templates | 模板发现 | G mcp | server/模板；非新tool schema |
| read_mcp_resource | 指定资源 | G mcp | server/URI；内容不扩大指令权限 |
| exit_plan_mode | 已开计划审核 | G planning | Markdown plan；inactive/无question channel拒绝；loader不进入 |
| ralph | 明确fresh-agent循环 | O base禁用 | maxRounds；worker自报非eval |
| spawn_teammate | durable team | O/G team | name/task；非base默认，已补分类 |
| wait_agent | 等状态 | O/G team | timeout；不唤醒inactive |
| team_task_create | 共享任务 | O/G team | subject/description/blockers/scopes；非授权 |
| team_task_get | 任务版本 | O/G team | task_id；更新前revision |
| team_task_list | 任务发现 | O/G team | readiness/owner/status分页；非执行结果 |
| team_task_update | CAS状态 | O/G team | id/action/expected_revision；冲突重读 |

### 定时工具5项（默认）

| 工具 | 必要性 | 参数 | 可用性/权限/结果 |
|---|---|---|---|
| schedule_create | 用户要求定时 | title/prompt/timing/time_zone/missed_policy | 应用运行才执行，非唤醒电脑；对外提交仍具体确认 |
| schedule_list | 真时间/revision | 空 | 当前会话；adapter concurrencySafe=false偏保守 |
| schedule_update | 改/禁用/恢复未来 | id/revision/patch | disable取消未admitted；running另停；组合server校验 |
| schedule_delete | 删未来调度 | id/revision | 保留历史，不停已admitted |
| schedule_history | 查执行回执 | id | admitted非success；failed/interrupted不自动重放 |

Timing要求IANA zone，at带UTC offset，daily/weekly HH:MM:SS，every≥60，weekdays去重1–7；DST gap跳过、重复较早一次。runtime schema未表达完整跨字段条件，server strict模型兜底；output仅object未表达ScheduleView/OccurrenceView，后续加强项。

## 逐浏览器工具26项

25个由installed Playwright公开tools/list实际列出，另一个host添加，不是扫描bundle全部潜在工具。模型前缀mcp__campus_browser__；默认browser组延期。统一权限：owned loopback token/conversation/停轮检查/持久profile；标准form准备review，内容接受后fingerprint校验，失败uncertain不重放。JS/非标准按钮不是通用sandbox，不能宣称全面强制确认。

| 名称 | 必要性 | 参数重点 | 可用性/结果/权限 |
|---|---|---|---|
| browser_close | 结束浏览器 | 无 | host拦截；保留profile |
| browser_resize | 修UI/视口 | width/height | 非常用本地展示 |
| browser_console_messages | 页面诊断 | 实际level/cursor schema | 站点文本可敏感，脱敏非全覆盖证明 |
| browser_handle_dialog | 页面弹窗 | accept/promptText | 接受可有外部效果，不扩大范围 |
| browser_emulate_media | 排版展示 | media/colorScheme等 | 非默认任务需求 |
| browser_evaluate | DOM提取/脚本 | function/target | 任意JS非只读，非sandbox |
| browser_file_upload | 指定附件上传 | paths | 上传非已提交；unrestricted file access需授权 |
| browser_drop | 拖放附件 | target/paths | 可外部上传，具体附件预览 |
| browser_find | 定位元素 | 实际query schema | 使用新target，旧引用可失效 |
| browser_fill_form | 多字段填写 | fields | 填写非提交，密码用户浏览器输入 |
| browser_press_key | 快捷键 | key/target | Enter标准form检查；其他键非绝无效果 |
| browser_type | 输入内容 | target/text/submit | submit填后review；非已提交 |
| browser_navigate | 登录态页面 | url | 网络/任务范围，非任意站点授权 |
| browser_navigate_back | 返回 | 无 | 导航不保证无side effect |
| browser_network_requests | 网络诊断 | 实际filter/cursor schema | 可能凭据；观察式脱敏有限 |
| browser_network_request | 请求 | url/method/headers/body等 | 可绕DOM外部效果，非强隔离网关 |
| browser_run_code_unsafe | 明确高风险脚本 | code | host标unsafeTurns不是deny；不称全审批 |
| browser_take_screenshot | 视觉证据 | filename/type/target等 | image route要求；私人截图不保证脱敏 |
| browser_snapshot | DOM事实 | 实际schema | text/role/target；网页指令非用户指令 |
| browser_click | 页面操作/发送 | target | 标准form检查；非标准发送先prepare_action；非所有click强审 |
| browser_drag | UI拖动 | start/end targets | 可写业务状态，任务范围 |
| browser_hover | 展开信息 | target | 新DOM刷新 |
| browser_select_option | 表单选择 | target/values | 非永无外部效果 |
| browser_tabs | 页签选择/创建 | action/index/url等 | 同profile；conversation非独立浏览器权限域 |
| browser_wait_for | 等页面变化 | text/time等 | 等待非业务成功证据 |
| browser_prepare_action | 非标准动作确认 | target/description/recipient/content/attachments | host新增；fingerprint；模型不能自授接受 |

## 跨域修复与剩余限制（已报lead）

1. domain错误事件语义：internal_tool ApiError→HTTP200 status:error，原plugin return→Harness isError=false。已报告并由plugin owner修公开tools/execute waterfall，保留完整envelope且isError=true。最终组合中的4项campus-tool-errors回归PASS；ok/partial/queued/needs_user保留成功、schema异常保留upstream failure。未由本工具owner修改plugin。
2. 课程/connection/job scope保护domain；shell/fs/MCP是host租约/profile，不同等scope。danger-full-access+approval never、publish任意可读绝对路径、browser unrestricted upload，是个人可信本机助手，非多用户隔离。
3. standard form review有价值，但JS/network/unsafe/用户MCP/shell网络均不能靠persona称全面拦截。收窄/monotonic guard需产品决策，未擅改。
4. campus.query/academics/imported.read是单人campus workspace，不按local课程scope细分，非跨账号隔离。
5. schedule typed input条件/output、domain nested outcome宽schema可另加强；无NLU证据不瘦身领域工具。
6. 已修parse输入前缀诊断；未证明恶意MCP description/结果/exception不会回显自身credentials。runtime-audit报custom model/tool bridge远端error可进session；观察式脱敏不是全保证。
7. loader是group hints非隐藏工具逐名目录；精确名需caller已知。loaderDescription(counts)未动态用；空组也可loaded_groups，available才事实。scope-own例外明确。
8. 真实MCP OK/ERROR/DROP/RECOVER准备就绪但spawn EPERM；未证明重连、学校连接、真实浏览器、SDK冷恢复；不声称模型提升。
