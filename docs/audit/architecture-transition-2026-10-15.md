# 工作台转型 Agent：架构自查与修复

日期：2026-10-15，Asia/Shanghai。提交基点 `f5ebbbcc4ce7d163e44ed583b96cce06ff49bd9b`；**进入时已有大量未提交修复，基线是当时工作区，不是干净 HEAD**。本报告只把本轮有故障复现的增量列为本轮修复。未 reset、覆盖个人数据库或登录态，未修改 OS 代理、DNS、路由、TLS；不新增依赖。

结论：**当前系统已经有真正的模型工具循环、会话恢复和多工具执行，不能归类为“只有函数选择器”。但工作台的事务、分页、授权和操作串行假设确实未完整迁移，造成了可验证的能力退化。** 同时存在运行环境地址策略冲突和模型未及时结束任务的问题。无需重写 Harness；应继续修复业务适配与执行边界。

核心证据：[工具逐项审计](transition-tool-audit.md)、[现行27工具完整schema](transition-tool-schemas.json)、[实测汇总及源码哈希](transition-summary.json)、[原六项失败](transition-before.xml)、[最终关键回归](transition-core-final.xml)。先前 `docs/audit` 报告仅作历史线索，不替代本轮测试。

## 1. 架构如何演变

### 原始工作台

`e12fd7c`（10月9日）保留了 `campus_agent/server.py`：loopback 单 worker FastAPI 原生界面。原调用链为：

```text
页面按钮 / 前端学期与账号状态
 → passport_routes / campus_routes / 资料、通知等 HTTP 接口
 → CAS/教务/ETA 会话、CampusService、SQLite Store
 → 固定校园站点
 → 缓存、来源、资料片段、通知待办
 → 前端轮询、自动续传、结果展示
```

后续多人协作形成了 `apps/web` React 学习工作台和 `apps/api`：课程发现/绑定、资料解析、作业同步、任务生成、排程、复习卷、JSON/ICS 导出。浏览器登录、课程绑定、重新同步、上传资料、配置长期时间窗等步骤由用户/前端连接成工作流；后端路由通常负责提交事务、校验幂等和返回 job。

### Agent 转型

- `a5f4928`：V2 UI、校园迁移、调度合并；`6c98765`：删除旧应用入口，校园协议单份化到 `connectors/campus`。
- `6690c6f`：按组工具发现和长对话保留；`32974aa`、`b0aa96a`：会话级 runner 复用及预热；`98536ad`：保留具体模型错误。
- 现行产品合同 `docs/agent-design/goal-v2-personal-assistant.md` 已选择**本机单人**，不再提供原多账号注册语义。这是产品范围改变，不能把本机数据库当作可部署的多租户权限系统。

现行实际链路：

```text
React chat / 可信确认 / 附件选择
 → FastAPI owner cookie + Origin + runtime token
 → SQLite journal，单 conversation 串行 turn
 → Python Runtime → owned Node runner（可复用）
 → prepareProfile → 官方 Harness 0.2.0-rc.2 SDK / loop / JSONL history
 → 模型看到当前真实 schemas，生成带 ID 的 tool calls
    ├ 27 个 campus_* 领域工具 → 严格 Pydantic → 服务端 scope → 原业务函数/worker
    ├ web_search → 独立 DeepSeek 搜索 Messages API
    ├ web_fetch → 匿名 HTTP + DNS/地址检查 + HTML 转文本
    └ load_tools → 浏览器/MCP/子任务/后台工具/计划模式
 → 工具结果关联调用 ID，重新进入模型请求
 → 模型最终回答 / native Goal / 明确失败
 → journal 事件与结果 → UI 工作区
```

模型桥有两条路径：官方 DeepSeek 用原生 Messages/SSE；其他 Chat Completions provider 用 `agent_bridge/model.py` 和 `CampusModelAdapter`。后者不是第二套推理循环，但必须适配错误、finish、工具 ID 和消息协议。

**有实际执行证据**：完整合成模型演练完成83次模型请求，覆盖两轮历史、API重启、取消、材料 search→read、计划预览/接受/应用/撤销、后台任务、文件 read→write→publish、MCP计算、子任务选择性停止、Goal续行、浏览器下载和一次确认。证据位于本机 `var/agent-integration-qmh0k58f/evidence.json`；模型是受控端点，不是自然语言能力评测。

### 未迁移或行为不同的能力

1. 领域工具主要复用了原业务函数，部分还直接导入 `routers`。共享业务实现是合理渐进策略，但路由承担的提交事务等责任必须显式补齐；本轮发现了实际遗漏。
2. `connections.courses → import → sync → jobs.read → materials/assignments` 原来由用户界面串联。进入时已有这几个工具包装，但仍有权限与事务断链，不能仅凭注册成功判定完整工作流成立。
3. 复习卷预检/生成/修订/导出、一般资料导入/解析、全库JSON/ICS导出仍有可靠 REST 服务，却**没有对应领域工具**。聊天的 `publish_file` 不等于将资料导入课程，`tasks.build` 也不等于生成复习卷。
4. 当前能力策略明确禁用任意 Shell/workflow/PTC；document-artifacts skill 当前只能指导 Markdown/CSV。已有 PDF/XLSX 渲染脚本与依赖仍在，但没有可信的模型可调用导出入口。这是可定位的能力缺口；不能恢复无限制 Shell 来掩盖它。
5. 登录/MFA、长期授权、具体外部提交确认仍由可信界面完成，是必要人机边界；正常只读查询和公网搜索不需要每次点击确认。

## 2. 根因排序及实际修改

|优先级/类别|已证实故障及直接证据|修复位置与行为|
|---|---|---|
|P1 业务事务/工具包装|`connections.sync` 只 flush，HTTP请求关闭 Session 后 job 回滚；独立 worker Session 读不到已返回的 job ID|`tools.py:_connection_sync` 在返回/等待前 commit；跨请求和独立会话测试，保证返回真实持久 job|
|P1 调度并发|慢浏览器 RPC 持有整轮 `call_lock`，另一个子任务的本地 read 被 tool-lease 阻塞；完整演练在 `child_b_waiting` 超时|`routers/agent.py:internal_browser` 使用独立 `browser-call:<turn>` 锁；`Runtime._begin_stop/_drain_stop` 捕获两把原锁，取消仍等待实际收尾；完整83请求演练随后通过|
|P1 作用域|新课程导入成功，但创建会话时冻结的 course_ids 不包含新ID，紧接 materials.list 返回 SCOPE_DENIED|`internal_tool` 从已提交 Course/SourceRef/已授权Connection核验回执；`Store.grant_imported_courses` 原子更新**该会话**；并行请求在锁内重取 scope，重放幂等，不影响其他会话|
|P1 权限范围|受限会话同步了平台全部绑定课程；同 connection 的宽范围job可被窄会话读取/取消|`enqueue_connection_sync(allowed_course_ids=...)`、`job_actions.owns/cancel`；按课程交集入队，不复用不同范围的活跃job，不因知道ID扩大权限|
|P2 Harness协议|自定义桥抛 plain Error，安装版 `normalizeLlmFailure` 将自定义code/retryable降为 UNKNOWN；6类故障复现全部失败|`CampusModelAdapter.stream` 发出 rc.2 标准 error finish：TIMEOUT/RATE_LIMIT/TRANSPORT/SERVER/AUTH/BAD_REQUEST；复用官方有界退避，不新增循环，不重放副作用工具|
|P2 工具语义|`jobs.read` 读到 failed/partial_failed 仍返回顶层 ok；多个job入队也可能顶层ok|`tools.execute` 区分 queued/partial/error，保留job与错误依据；终态失败不假成功、不无限重新入队|
|P2 分页/失败隔离|课程发现丢弃 next_cursor；导入忽略 cursor；首个平台失败抛出导致其他结果丢失|`_connection_courses/_connection_course_import` 保留游标；多平台返回逐连接continuations/errors，partial不冒充全量；游标必须对应唯一平台/连接；分页导入all只表示当前页全部|
|P2 网络诊断|设置诊断进程不传代理环境，也未安装官方代理dispatcher；测试通道与实际运行不同|Python `diagnostics.check` 只继承代理变量；Node `diagnose.mjs` 安装官方同一策略，退出恢复。真实本地HTTP代理+子进程测试通过，不修改系统|
|P2 只读误拦截|真实模型导航教务公开通知被 BROWSER_APPROVAL_REQUIRED 拒绝；固定只读详情URL包含 `doType=save`，被通用危险词误判|`browser-action-policy.mjs` 只为既有通知契约的精确HTTPS域/端口/路径/32位ID/两项唯一参数放行；其他save、不同来源、额外action、重复参数、含凭据URL仍拒绝|
|P2 结果恢复|web_fetch 地址拒绝只给错误，真实模型连续换多个校园域名、搜索，耗尽诊断预算|新增 `web-recovery.mjs`：保留原失败码与isError，提供固定校园摘要/授权浏览器的合法下一步及停止提示；不关闭SSRF、不改网络、不扩权。第一次只加提示复测仍失败，**不把提示当万能修复**|

原六条失败证据见 `transition-before.xml`。随后又分别复现了后台失败假成功、同步job回滚、浏览器锁阻塞、跨课程job越权、只读通知误拒绝和六类桥接错误。新增回归测试直接验证行为，不是断言类名存在。

证据等级必须分开：上表均为**已复现**的工程故障或环境现象；REST仍有复习卷/导出而Agent缺入口，属于**源码直接证明的覆盖缺口**，但它造成多少实际用户失败尚未量化；“DeepSeek自身推理弱”“压缩导致长期约束丢失”“部署普遍禁止出站”“工具太多必然降低智能”目前均**没有足够对照证据**。本机真实搜索/读取成功已经排除了本机全面禁网的解释。

实现期间工作区另有课程记录 `id/external_id` 与不支持的远端 `term` 参数修正；已保留并适配当前接口，未覆盖。它们不计入本轮独立修复功劳。最初六项回归的import用例最初还验证term，现改为核实真实支持的cursor；因此这组不是字节完全相同的盲测基准。现行学在发现工具不宣称支持远端学期筛选。

## 3. 联网与权限的真实边界

[Python逐跳实测](transition-network-python.json) 与 [实际web_fetch调度实测](transition-network-node.json) 均无校园凭据、无Cookie复用，TLS验证开启。

|访问|后端 requests，system/direct|官方匿名 web_fetch|
|---|---|---|
|example.org|两者200，有HTML|200，HTML解析并渲染为模型可读文本，290字符|
|浙大官网|两者200，有HTML|WEB_BLOCKED_URL，DNS返回私网地址|
|教务公开登录页|两者200，有HTML|同上；即使200也只证明登录页面可达|
|WebVPN门户|两者302→`/login`→200|同上；没有验证VPN认证或业务权限|

当前解析的三个校园域名都落在10/8私网。**只能断定当前DNS/地址环境与匿名抓取的SSRF策略冲突，不能据此断定由用户TUN、学校DNS还是本执行环境代理造成。** 不更改系统DNS，也不以公网域名后缀为由放行任意私网地址。

- 后端固定校园适配器可走已登记站点；通用web_fetch只匿名访问公开地址，不继承浏览器Cookie。其地址拒绝不是“模型权限不够”。
- 搜索是独立服务请求。真实官方模型已多次自主调用 `web_search`，拿到标题/来源并继续读取，不是只有HTTP客户端却没有搜索工具。
- 运行时Node与Python后端有独立网络实现，浏览器又有独立profile/路由；浏览器可达不证明上述两条链都可达。
- 普通Shell在**产品模型能力**里不可见且执行被guard拒绝。本次开发终端能联网，不代表产品给模型Shell权限；没有为解决检索而打开它。
- 本次验证的是launcher启动的本机Python/Node/Edge进程；没有将本机结果推广到Docker、学校服务器或其他沙箱。它们需要在各自部署环境重跑同一无凭据探针。
- 校园system/direct、WebVPN通道和普通公网分层保留。direct仍使用OS路由，不能保证绕过TUN。浏览器WebVPN登录不等于HTTP连接器完成认证。
- 学在、智云、CAS/教务/ETA、助手浏览器都有各自会话/探针/授权。当前有显式浏览器会话交接和持久授权代码；是否真实认证成功必须看官方probe，不能看Cookie文件是否存在。
- `var/agent/settings.json` 本次检查时不存在，自定义MCP配置为空；合成calculator的成功不能冒充用户配置的外部服务已验证。

## 4. Harness 链路诊断

### 工具可见性

真实官方会话JSONL `request/header` 初始含**47工具**，27领域+20常用原生；工具名来自真实请求头，不是静态注册猜测。完整演练加载浏览器/子任务/后台/计划后含77项。第三方MCP取决于可信配置。`load_tools` 返回的catalog不授予权限；guard和参数校验仍执行。

安装版测试覆盖作用域继承、late registration、loader空参、scope-owned工具、MCP错误、隐藏代码工具。没有发现当前动态发现排除 `web_search/web_fetch`；它们默认可见。域工具命名有 `campus_campus_*` 冗余，但目前模型实际能调用，未为了“整洁”破坏兼容名称。

按需与全量暴露的**确定性对照**：同一installed ToolRuntime在加载前/后核对schemas和guard；前者少带browser等schema，后者真实可调用，拒绝项仍被拒绝。没有做固定模型多次随机对照，不能声称动态发现比全量准确率更高。真实通知任务确实自行load browser再调用。

### 执行与错误恢复

- ID关联、多个tool call、多轮history、cold resume、取消、重复确认均由真实Harness和API演练验证；没有以SDK idle简单等同任务成功，现行goal-lifecycle检查root turn/end。
- 默认预算128模型/512工具/1800秒、输出16384；真实官方日志contextWindow为1,000,000。没有证据证明生产默认轮数普遍过低。自定义Chat Completions adapter仍缺模型容量元数据，超长上下文压缩需要单独实测。
- 当前自定义桥主要支持文本/tool，不等于官方原生多模态；`model.py` 仍丢弃provider usage，未在本轮扩大改造。真实官方路径的usage已经记录。
- 本轮标准化的是**模型请求**重试，429/超时/传输/5xx可以由官方有界重试；401/配置拒绝不重试。不是对所有失败工具自动重放。
- 参数错误保留严格校验及字段提示；缺能力、认证、数据源失败仍不同。异步job现在将终态错误正确返回；不能把队列受理当学校业务完成。
- 认证恢复模块已持久保存原goal/参数/已收结果、缩窄scope、一次probe和幂等continuation；通过Mock/重启/停止测试。**本轮没有真人校园登录过期再恢复的E2E证据**，也没有证明浏览器任意交互都可由这个模块自动续跑。

### 实际日志的用途与限制

本轮只读汇总既有个人journal的状态/错误码，没有导出对话、校园资料或凭据：31轮中17取消、3失败、3中断、1运行中、7succeeded；历史错误包括RUNTIME_INTERRUPTED、RUNTIME_FAILED、TURN_TIMEOUT。这不是任务成功率样本，succeeded也不保证回答满足目标。本轮不打断已有运行中的轮次。

新的具体链路证据来自隔离合成环境和付费官方模型实测。完整演练第一次因工具清单漏掉已存在的3个连接工具失败，更新验收清单后又暴露真正的浏览器锁饥饿；拆锁后83请求演练通过。未删除失败断言或将异常当成功。

## 5. A—E诊断与16个代表任务

所有Mock测试都使用合成数据库/传输；真实模型使用独立合成课程库，校园公开页读取是真实的，学校私有登录数据未读。

|编号/任务|可见与选择|执行与结果|介入/时间/费用口径|
|---|---|---|---|
|1 搜索最新校园通知并核对日期|真实47工具起步，模型选search，随后load browser|最终读取生命科学研究院官方正文，核对2026-10-14发布日期；诚实报告不能覆盖全部通知|0次；61.171s；7模型/10工具；usage见JSON，金额未知|
|2 指定公开网页提取|真实模型选web_fetch|example.org标题、内容、链接进入最终回答|0次；17.031s；2模型/1工具|
|3 查询课程安排所需本地课程|真实模型选courses.list|正确返回合成课程；不是本人真实校园课表联调|0次；11.328s；3模型/2工具|
|4 学在资料完整闭环|领域工具真实可见，LLM选择未测|实际HTTP合成import→scope更新→sync持久入队→模拟worker在独立会话写资料→poll→list→版本原文read|Mock只替换学校发现/worker输出；无真实学在授权；费用/真人介入不适用|
|5 WebVPN合法路径|entry领域工具可见，LLM选择未测|门户匿名302→login可达；隧道、会话、失效测试594套件内Mock通过|真实VPN认证未验证，不能填已完成|
|6 学习任务+可用时间+计划预览|真实模型选tasks/availability/preview|多工具预览成功，未应用，说明学校课表未核验|0次；21.766s；4模型/5工具|
|7 检索再读完整原文|真实模型search→read|读取指定资料版本及chunk_synthetic来源|0次；21.688s；3模型/3工具|
|8 临时限流后恢复|真实Harness，受控模型|首次429→官方重试一次→read一次→最终使用marker|0次；3模型请求；不是真实限流服务压力测试|
|9 登录失效后继续原目标|Mock SDK/探针/持久journal|保留目标/已收结果、登录通知后只读续跑一次；停止/撤权不续跑|真人SSO未验证；详见test_agent_auth_resume|
|10 未知截止不编造|真实模型选assignments.list|明确unknown，未推断截止日|0次；18.766s；3模型/3工具|
|11 导入课程后继续读|真实HTTP桥+合成DB|原SCOPE_DENIED→同会话可读，其他会话仍不获得新scope|Mock，1个导入+1个读取+幂等重放|
|12 分页与平台失败隔离|直接工具，未测LLM选择|首分页partial+cursor、指定页导入；一平台TIMEOUT保留另一平台结果|Mock，原三处断链已复现|
|13 入队回执后跨请求查询|真实HTTP桥+两个DB会话|修前job回滚；修后worker/下一请求可见同job|Mock；不冒称学校同步成功|
|14 宽job/窄授权隔离|直接工具，模型选择不适用|拒绝复用/读取/取消不属于课程scope的job|Mock，保留平台正常调用|
|15 后台失败正确反馈|直接工具，模型选择未测|failed→error，partial_failed→partial，保留原因及结果引用|Mock；终态失败不重复入队|
|16 并行读取与浏览器/可信提交|真实Harness、实际浏览器、本地合成站点|子任务A慢导航时B读取可前进；选择性停止、附件下载、相同确认不重复提交通过|受控模型；真实学校提交未做|

对应A：直接工具/HTTP/独立worker测试；B：真实请求header及官方模型选择；C：实际安装SDK+429恢复、浏览器并发故障；D：marker与原文内容必须出现在最终模型答案；E：search/read、计划链、import/sync、子任务及重启演练。不能用A的通过冒充B—E全部通过。

### 修改前后可核实指标

|指标|前|后|解释|
|---|---|---|---|
|最初6个故障行为|6失败|对应行为通过|针对性回归，非随机用户样本；term接口变化见前述说明|
|跨请求sync job可见|返回queued但job不存在|同job由独立worker与下一请求读取|业务事务修复，不依赖模型智力|
|并行browser/read|租约等待超时，完整演练失败|关键并发测试通过；完整演练83请求完成|保留取消drain与原锁测试|
|自定义桥六类错误|plain Error，UNKNOWN/无正确恢复|六项通过；真正Harness限流1次后恢复，认证只请求1次|不计作真人网络故障恢复统计|
|真实首轮7任务|6轮终态成功，1轮预算耗尽|没有重跑完整7任务集|不能拼接成“整体100%”|
|同一通知任务观测|8模型/19工具/10错误，65.328s，预算耗尽|7模型/10工具/1错误，61.171s，回答完成|最终实验上限16请求，实际用7；网页、采样未固定，非严格因果对照|
|仅加web恢复提示的中间试验|—|8模型/13工具/3错误，仍预算耗尽|保留失败证据，不宣称提示单独解决问题|

真实调用已报告的**对话**token：首次7任务342342；中间单题140912；最终单题148943。包含大量cache输入，并非新增生成token；搜索服务的独立请求用量未计入，金额没有可靠账单/价格快照，记未知。[原始汇总](transition-summary.json)与各次JSON保留token组成、调用、耗时、工具结果、回答和真实可见工具。尚无固定seed/温度、相同网页快照、重复N次的模型能力比较；不能归因到底层模型智力。

## 6. 回归与交付边界

- 进入时完整API：599 passed / 5 skipped；Node75 passed。
- 最终完整API：**616 passed / 5 skipped**，157.58s，`transition-api-final.xml`。较早一次完整运行为612 passed/5 skipped、wall1171.86s，其中一个认证检查点用例1003.486s；随后同一用例连同12项关键回归13 passed/4.23s，最终完整运行亦未复现长停顿，不将它归因于已确认代码故障或隐藏该时长。
- 同步/任务/取消专项38 passed；跨请求完整资料链与取消/同步专项33 passed。最后新增的单平台同步失败隔离用例先失败再修复，由最终完整API覆盖。工作区也有并行进入的测试增加，完整通过数增量不全部归于本轮。
- 根目录+connectors：**594 passed + 87 subtests**，含按账号缓存、会话、TLS/跳转、空页/中断、WebVPN与既有校园适配；只用Mock验证认证内容。
- Node最终**85 passed**；实际Harness恢复独立脚本有46条通知、恰好1次llm/retry、read只执行一次。
- 完整真实API/Harness/浏览器合成模型演练通过，83模型请求；中断socket产生的ConnectionReset诊断保留，脚本最终exit0。
- 前端TypeScript build检查通过；本轮没有改UI，不冒称重新完成真机视觉验收。
- OpenAPI导出检查通过：51 Agent paths / 54 schemas；无新HTTP路由或数据库结构迁移。
- CI已补上Node边界回归和安装版模型恢复演练；不在CI调用收费模型或真实学校服务。

修改集中于工具适配、入队业务入口、scope journal、执行锁、模型错误协议、联网诊断、固定通知导航规则及回归脚本。现有未提交页面/认证/WebVPN改动原样保留，没有将它们全部归入本轮成果。最终关键文件哈希见汇总，具体差异以工作区为准。

复现（均在仓库根目录，除注明命令）：

```powershell
apps/api/.venv/Scripts/python.exe tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py
node --test --test-isolation=none apps/agent-runtime/tests/model-recovery.test.mjs apps/agent-runtime/tests/network-diagnostic.test.mjs apps/agent-runtime/tests/campus-notice-navigation.test.mjs apps/agent-runtime/tests/web-recovery.test.mjs
node apps/agent-runtime/verify-transition-recovery.mjs
apps/api/.venv/Scripts/python.exe tools/audit_public_network.py
# 从apps/agent-runtime运行：node audit-public-network.mjs
# 下列命令调用真实已配置官方模型并产生费用；仅使用新建合成工作区：
apps/api/.venv/Scripts/python.exe tools/audit_live_transition.py --live
```

## 7. 后续架构选择与未完成能力

选择：**保留模块化单体和官方Harness，继续重构工具/执行适配层**。业务可靠性问题有直接证据，替换模型或另写planner不会提交遗漏的事务，也不会修复scope和锁。真实模型已经能够自行联网、选工具、使用结果和跨工具预览，未发现必须更换整个Harness的证据。

后续按具体业务逐项迁移，均应可回滚：

1. 把复习卷、资料导入解析、JSON/ICS导出接到现有业务服务，提供简洁且有权限的领域工具；先增加单项适配与契约回归，保留原REST/UI。无需另建整套通用框架。
2. 为现存PDF/XLSX渲染器提供固定输入schema、工作区路径、固定服务进程和publish回执的可信导出能力；不重新开放Shell。收益是恢复明确的文件产出能力，代价是单个格式的校验/资源上限/回归。
3. 已有校园业务方法逐步搬出router模块成为应用服务，HTTP与tool调用同一入口，并由应用服务负责事务。不要同时重写学校协议和缓存。
4. 补真人学在/智云/CAS/ETA/WebVPN认证及失效续跑；本轮仅公开通知/公开网页真实读取成功，私有校园平台仍是“实现存在、自动测试通过、本人新联调未做”。需要本人完成官方登录和网络条件，不能伪造在线态。
5. 以固定模型、预算、参数、合成数据及网页快照重复评测工具暴露策略/停止行为，记录goal达成而非仅turn=succeeded。首轮通知任务说明模型仍可能已有够用证据却继续查，不应靠无限扩预算解决。
6. 自定义模型桥的usage与contextWindow、MCP逐工具信任范围、浏览器页面脚本/事件副作用仍有工程边界。现行文件/提交guard不是OS级多租户沙箱，不对未知MCP承诺安全或连通。

本轮真实可用：官方模型工具循环、匿名公网搜索、可被地址策略接受的公开网页提取、浏览器公开页阅读、教务公开通知摘要。合成数据上验证可用：本地课程/资料/作业/任务/计划链、连接器入队与范围控制、认证恢复框架。**不声称真实课表/成绩/学在私有资料/WebVPN认证/学校提交本轮全部验收成功。**
