# 当前工具完整审计表

与 [架构自查报告](architecture-transition-2026-10-15.md) 配套。当前源码有32领域工具（2026-10-15 实测；本文早前写的27是旧快照）；完整schema及参数见 `transition-tool-schemas.json`（该 JSON 同样仍是旧快照，计数以当前实现为准：`var/harness-campus-readonly-2b0480ed/m0/baseline.json`）。领域名字在模型侧统一加 `campus_`，点替换为下划线，例如 `campus.query` → `campus_campus_query`。保留名称兼容，没有因文字冗余直接删工具。

可见性：**D**=默认真实可见；**L**=load_tools后可见；**X**=能力策略隐藏/执行拒绝；**C**=可信配置决定。权限上下文由服务端构造，不接受模型owner_id。所有“合成通过”均不表示校园账户真实授权成功。

## 领域工具（32/32）

|工具/名称理解|来源模块|预期与实际能力|网络/认证与执行条件|可见/调用效果/建议|
|---|---|---|---|---|
|campus.status，含义清楚|旧校园连接页→CampusWorkspace|状态、缓存、来源；在线需probe，不能以connected推全平台|服务端单人校园身份；通常本地状态|D；真实模型调用成功；保留|
|campus.query，缓存读|旧教务/ETA/通知查询|返回已缓存records、freshness、completeness、error；**纯缓存读，不联网、不读浏览器Cookie、不提交密码**，已授权会话失效时只返回结构化错误（2026-10-15 修正：此前实现会在缓存读里自动重登并远端sync，与本节契约冲突，已改为显式 campus.sync 才恢复）|固定站点；校园会话与浏览器grant按平台隔离|D；真实公开通知/合成个人数据；恢复入口=campus.sync或可信恢复探针|
|campus.sync，显式远端读|CampusService/适配器|教务课程/成绩、ETA档案、已登记通知；失败保留缓存|固定TLS站点与网络策略；私有数据需登录，公开摘要不需|D；真实免登录通知成功；个人数据未新真人验证；保留|
|campus.academics，日期语义较复杂|旧学业看板/academics|day所在整周events及绩点/学分，不是自动筛单日；未定位课不能证明无课|本地校园快照、校历精度与版本|D；真实模型合成预览时使用；保留警告，后续可增单日应用查询|
|campus.notice.read，学院正文|旧学院通知正文|只读已缓存、已展示学院通知；**不提供校级摘要详情任意抓取**|登记学院及record_id；正文/附件链接不是附件内容|D；错误UNSUPPORTED真实被消费；名称描述应持续明确覆盖范围|
|campus.imported.read，历史|旧资料迁移清单|读取迁移批次/资料片段/证据，不能当实时同步|本地owner映射，导入库只读来源|D；迁移/版本回归；保留|
|campus.webvpn.entry，入口不是隧道|旧WebVPN工具|返回登记浏览器入口；不是登录成功/接口通道证明|真实官方VPN登录独立；不能自动继承日常浏览器|D；Mock与匿名门户读取；保留边界|
|campus.notice.import，导入|通知→资料/待办|把缓存学院正文挂接指定课程，幂等与正文版本；不读未下载附件|已授权课程+record_id，本地修改|D；合成notice→material→task链；保留|
|connections.status，连接状态|React连接设置/API|只读本地学在/智云连接状态|connection scope；不自动登录|D；真实模型调用，数据为合成/空连接；保留|
|connections.courses，实时发现|连接课程发现页面|平台当前页课程，来源ID、游标、逐连接errors；不是全量本地课程|学在/智云独立登录、固定适配器；远端term未承诺支持|D；修复丢页及失败传播；Mock证明，未真人平台联调|
|connections.courses.import，绑定|平台课程导入按钮|指定当前页ID或all=当前页全部，写本地绑定、授权新课程；非同步资料|已授权connection；服务端验证回执与SourceRef；不需要前端点击|D；修复导入后scope断链/错页；保留分步语义|
|connections.sync，刷新|连接“立即同步”|入队只读平台同步；job持久、scope交集，完成后须poll|授权connection+绑定课程；worker独立运行；过期需官方登录|D；修复返回虚假job、宽范围复用；真实HTTP合成闭环；保留|
|courses.list，本地课程|课程列表页|按名称/学期分页，多个匹配可needs_user；不是教务课表|本地课程scope；无校园认证要求|D；真实模型完成；保留与campus.query区别|
|materials.list，元数据|课程资料页|列授权课程资源/解析状态；不表示读过全文|本地course scope，无网络|D；真实模型/合成读取；保留|
|materials.search，全文发现|资料检索页|当前版本子串检索、摘要预算、chunk/version/locator；不是语义搜索|授权course集合过滤在分页前，无网络|D；真实search→read成功；保留|
|materials.read，精确正文|资料原文页|指定版本/chunk读取、24k字符预算分页，stale需重新发现|本地scope，必须可信ID/version；不依赖当前页面|D；真实模型使用原文；此前预算修复已保留|
|assignments.list，学校事实缓存|课程作业页|已导入作业、提交状态、未知截止；空不等于学校没有|本地scope；刷新须connections.sync|D；真实模型unknown不编造；保留|
|tasks.list，本地任务|学习任务工作台|本地状态/截止/课程过滤；完成不等于学校提交|本地course/uncoursed scope|D；真实模型计划链使用；保留|
|plans.read，当前安排|安排页|已生效/指定计划、锁定块/版本|本地完整计划引用校验，不能泄露其他课程任务|D；完整演练通过；保留|
|jobs.read，后台状态|job轮询组件|只读状态/结果引用；现在failed=error、partial_failed=partial|job receipt或connection+course scope；不暴露payload|D；修复假业务成功/越权；保留|
|availability.read，长期偏好|时间设置页|读取长期时间窗；不设置新偏好|本地，无网络|D；真实模型使用；长期修改尚无tool，可临时preview windows|
|plans.preview，可信预览|安排生成页|确定性计划及差异，不生效；可传本次窗口|完整scope、可信request_key；服务端日期/时区|D；真实模型与合成引擎成功；保留|
|plans.apply，应用|安排接受按钮|只执行已由可信UI接受的action，持久回执|模型不能自行accept；幂等、版本、scope|D；完整演练真实点击受控确认；保留必要边界|
|plans.revoke，撤销|撤销安排按钮|已接受撤销action，恢复前版|同上；不可复用其他会话action|D；完整演练通过；保留|
|tasks.build，生成任务|学习任务生成按钮|入队从已导入材料生成任务；不生成复习卷|授权课程、费用/任务服务配置、worker|D；完整演练build→poll/cancel；保留|
|jobs.cancel，停止后台任务|job取消按钮|取消自己/授权范围任务，running须等待检查点；不同于停止聊天|receipt、connection及课程scope；现在宽job拒绝|D；回归通过；保留|
|tasks.feedback，学习反馈|任务完成/反馈组件|版本化学习状态与耗时/难度；不提交学校作业|本地scope+expected_revision、幂等|D；完整演练通过；保留|

以上工具只接受明确schema；其中`campus.query/sync`的query是业务选择器，仍由已登记连接器类型校验，不是任意URL入口。

## 通用工具及执行权限

|工具（逐项列名）|实际能力/条件|网络与认证|可见与结论|
|---|---|---|---|
|web_search|真实搜索服务，多query，返回来源/摘要/日期；普通HTTP请求不等于搜索|独立DeepSeek Messages搜索端点/权限；官方共享key本轮真实可用|D；真实模型主动选用；保留，不把搜索失败当0结果|
|web_fetch|匿名GET、同源跳转限制、DNS/IP校验、HTML→模型文本|无浏览器Cookie；当前校园私网DNS被正确拒绝，公网可用|D；保留SSRF；新增合法恢复提示|
|read|读文本，workspace/精确附件/skill目录内|无网络；凭据与私有根拒绝，link路径规范化|D；真实材料/附件演练；保留|
|read_image|读取允许路径图像|本地scope；自定义模型桥多模态受限|D；安装版权限测试；不能概括为所有provider可视觉理解|
|write|写允许工作区文本|本地，不可写凭据根/外部路径|D；真实产物演练；保留|
|edit|修改允许工作区文本|同上|D；安装版权限测试；保留|
|glob|按路径找文件|受限树预检，不可递归凭据/外链树|D；权限回归；保留|
|grep|按文本找文件|同上，防绕过fs读取敏感文件|D；权限回归；保留|
|publish_file|将已有允许文件保存为会话下载快照|本地workspace/精确附件来源、owner、path/幂等|D；真实read→write→publish；不是任意课程资料导入器|
|skill|加载受信技能文本|固定skill目录，不能扩大权限；document-artifacts当前不运行脚本|D；完整演练；保留，补专用导出能力|
|todo_write|记录当前任务步骤|本地Agent状态，不是学校待办同步|D；不要混同tasks.feedback|
|create_goal/get_goal/update_goal|原生持续目标状态与续行|Harness预算及可信Goal控制；不因此新增业务权限|D；真实安装版Goal演练；保留|
|schedule_create/schedule_list/schedule_update/schedule_delete/schedule_history|本机定时任务及历史|存储、时区/预算、显式设置；不是云托管保证|D；API/安装版回归；保留|
|subagent/subagent_fork|并行独立子任务/分叉历史|继承scope与guard；模型预算仍有界|L（team）；真实安装版子任务演练；拆锁后可协同|
|list_agents/send_message/interrupt_agent|查看、协调、选择性停止子任务|同一Agent树；不能替代外部发送授权|L；完整演练选择性停止通过|
|list_subagent_models/spawn_teammate/team_task_*|上游按插件实际注册的团队工具|同scope/配置；不是固定部署必有|L；分类测试覆盖，不冒称本机77工具含所有这些名字|
|job_list/job_output/job_kill|上游本机后台任务，不是校园worker Job表|能力策略、进程归属、预算|L（jobs）；保留与campus_jobs_read区别|
|exit_plan_mode|退出已经开启的计划模式|不会凭loader进入模式或授予权限|L；保留，提示明确|
|mcp__<server>__<tool>|可信配置服务器提供的具体能力|transport权限/凭据按server；若allowedTools非空按精确名单，为空则信任该已启用服务全部工具|C+L；本机未配置自定义server；合成calculator真实stdio成功，不等于外部服务已通|
|list_mcp_resources/list_mcp_resource_templates/read_mcp_resource|上游资源枚举|当前项目能力策略明确拒绝|X；注册存在不代表模型可用，不能强行要求模型调用验收|
|bash/shell/pwsh/powershell/workflow/run_code/ralph/PTC|任意代码执行|产品当前禁用；开发终端联网不扩大产品权限|X；保留隔离；对应PDF/XLSX等需求应加可信业务工具|

## 浏览器工具（本次完整演练21项）

均为 `mcp__campus_browser__` 前缀，L（browser）；固定本机API桥/独立持久profile，业务登录必须本人合法完成。真实学校公开页读取已验证，私有会话与提交不因此视为通过。

|工具|实际作用/限制|效果/建议|
|---|---|---|
|browser_navigate|导航HTTP(S)/about:blank，危险URL拒绝；精确只读通知例外|真实公开校园页面读取；保留，不能把登录页当数据|
|browser_navigate_back|浏览历史导航，仍有URL/页面策略|合成浏览器回归；保留|
|browser_tabs|查看/切换/新建页，new URL有相同校验|不继承用户普通Edge配置；保留|
|browser_snapshot|页面可访问内容快照|供模型理解正文；真实模型消费成功|
|browser_find|寻找页面元素/文字|不等于通用搜索引擎；schema由安装版提供|
|browser_click|受控点击；未知非表单动作准备具体确认；只读导航/分页自动|真实合成下载及提交确认，重复确认不重放|
|browser_type|填写非敏感字段|password/OTP必须用户人工，不接受模型填写|
|browser_fill_form|结构化填写非敏感表单|标准提交另有确认；合成真实浏览器通过|
|browser_press_key|有限按键；Enter/Space检查活动元素/副作用|不能用快捷键绕过提交确认|
|browser_select_option|选择选项|页面事件可能有副作用，不能声称完全网络隔离|
|browser_hover|悬停|同上，复杂页面事件仍是边界|
|browser_handle_dialog|只允许dismiss，accept拒绝|确认不能借通用dialog代替|
|browser_prepare_action|准备具体外部动作预览|模型不能自批；必须可信UI确认|
|browser_network_requests|检查请求列表|不是发起任意HTTP；可能含敏感页面上下文，最少使用|
|browser_network_request|只读取response-body|request-body/headers拒绝，不能导出Cookie/人工密码|
|browser_take_screenshot|浏览器截图|文件名限制防路径逃逸；截图本身可能含敏感信息|
|browser_console_messages|检查页面诊断|不等于系统Shell；错误不得作可信业务回执|
|browser_resize|改变助手窗口尺寸|非业务数据能力|
|browser_emulate_media|媒体模式|同上|
|browser_wait_for|等待页面条件|有限工具超时/取消；不应无限等待|
|browser_close|关闭助手页面/浏览器|与断开授权、注销学校账户、清全部profile的语义不同|

完整77项manifest为27领域+21浏览器+29其他（其中默认20项、延迟9项）。以 `var/agent-integration-qmh0k58f/evidence.json` 的model_visible_tools和实时schemas为准；用户额外配置MCP会改变数量。

被禁止的 `browser_evaluate/browser_run_code_unsafe/browser_file_upload/browser_drop/browser_drag` 不属于正常可见工具。它们在宿主入口再次拒绝，不能靠MCP旧schema绕过。页面自行执行的JS、GET副作用、onchange自动保存仍需更强网络级隔离才能完全约束；本轮未承诺绝对隔离。

## 确认属于迁移缺口的工作台能力

|能力|已有可靠实现|当前Agent入口|建议|
|---|---|---|---|
|复习卷预检/生成/修订/版本/导出|review_papers REST + review_engine|没有领域tool；不能将tasks.build当出卷|新增薄工具适配，复用快照/幂等/费用和授权|
|一般资料导入/解析为课程材料|imports/resources/jobs|上传聊天附件只可本轮read；没有一般导入tool|单项迁移既有服务，不要求用户返回旧工作台串步骤|
|长期时间偏好修改|preferences API|只有availability.read及临时preview windows|需要时新增显式设置与撤销语义|
|JSON/ICS导出|exports API + worker|没有领域tool|复用export job和授权文件下载，不造第二引擎|
|PDF/XLSX文档|现存固定渲染脚本与reportlab/openpyxl|因禁代码执行而不可达|专用schema+受控渲染器；不开放任意Shell|
|真实学在/智云/教务/ETA私有读取|适配器/授权/缓存|工具链已实现，本轮合成验证|本人官方登录、校内/合法VPN条件后另做真人验收|

名称/Schema可见、工具执行成功、业务目标完成是三个指标。原sync job回滚和job失败仍ok正是后三者不能混用的直接案例。
