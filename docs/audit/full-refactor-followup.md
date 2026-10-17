# 校园 Agent 增量重构交付

2026-10-15，Asia/Shanghai。保留进入工作区已有未提交修改，不reset/checkout、不改node_modules；未读取个人凭据/数据/浏览器Cookie库，未调用真实学校或收费模型。

分报告：[认证/WebVPN](auth-vpn-followup.md) · [Harness](harness-followup.md) · [前端](frontend-followup.md)。此前README/test-summary属于此前审计，本轮证据以本文和followup-test-summary为准。

## 原架构最重要的问题

实际主链是React可信UI→FastAPI owner/Origin+journal→每会话owned Node runner→DeepSeek Harness官方loop/SDK持久history→校园工具/按需发现/受控浏览器。未推倒重写，不另写planner，不盲目扩大128模型/512工具/1800秒预算。

- **P1** 默认danger-full-access；浏览器任意脚本/上传/未知非form点击可绕标准表单确认。已收紧默认workspace-write+ask及危险入口，未知激活准备具体预览，但不是完整网络级沙箱。
- **P1未全修** shell联网/host读权限、MCP全工具信任、网页onchange/registered listener/GET副作用；分报告明确限制，不以提示词当强权限。
- **P2** adopt_browser_cookies仅测试调用，助手浏览器登录与校园语义工具断链。新增可信账户授权私有桥接，官方probe+唯一身份确认才grant。
- **P2** 学在HTTP追随认证跳转、Cookie属性/域丢失、未知JSON成功。已禁API跳转、加结构探针/恢复语义与平台Cookie隔离。
- **P2** standalone VPN假恢复/静默直通；已fail-closed/真实probe/固定映射URL护栏，生产CampusTransport尚未接入。
- **P2** 202解析/5xx丢幂等key；已保完整payload/key，可靠4xx拒绝才清。
- **P2/P3** 历史硬截100、无搜索标题，顶栏+Select；已sidebar/Drawer+owner全文字面搜索/分页。
- **P3** 事件近O(n²)去重；改Map+seq排序，空/重放保原引用。

此前领域预算/工具mask/模型协议等进入工作区已有改动不冒认为本轮新增。
 

## 登录改造

代码：campus_workspace.py:authorize_browser_session/query、routers/campus.py:connect-browser、BrowserHost.campus_cookies_private。

本人在助手独立浏览器完成官方SSO/MFA，在账户页同意复用校园登录。POST空JSON且owner/Origin检查，不接收凭据或任意URL，不向模型注册。私有RPC只固定CAS/ZDBK/ETA域，保留Cookie安全属性，不向网页/模型返回Cookie或导出磁盘。官方只读probe与唯一身份核验成功才grant，失败保原连接。授权仅当前API进程。

有效query不重采纳/重登；认证失效仅授权内只读重试1次，失败30秒冷却。disconnect撤销grant并清内存jar，缓存可选清除，不暗删独立profile。桥15项Mock及原migration9项通过。

学在controlled-browser、助手BrowserHost、CAS connector三套状态仍隔离。教务桥不移交VPN或混同courses认证。学在有效snapshot1 API，失效至多官网刷新1次再复核，无密码提交/MFA绕过。全批次sync/fetch任务再认证续跑尚未完成。改动在adapters/transport/xzzd/zykt、browser/session_cookies/controlled_login/session_runner。

VPN低层修复在campus/webvpn_adapter.py有Mock证明，但生产CampusTransport仍direct/system固定HTTPS，VPN工具只提供观测代理入口交助手浏览器。host token来源2026-10-12需实时核验。无真实账户/离校网络，不能宣称生产VPN全链解决。

## 工具安全与前端

prepare-profile默认workspace-write+ask；实际安装版FS/Approval验证工作区写自动、外部写拒绝，无answerer即时unavailable不挂起。browser-host:dispatch与browser-action-policy禁任意脚本/直接上传，未知激活预览、fingerprint复用、变化stale、确认防重。已授权读取/GET搜索/普通分页仍自动，模型资料发送同意仍按会话，不取消隐私边界。不宣称完整shell网络/MCP隔离。

ConversationSidebar/AgentPage/agent.css支持桌面收起偏好、移动Drawer、服务端问题回答搜索/200ms debounce、标题/分页去重、账户设置直达保草稿。输入底部可操作，真实工具进度、技术详情折叠、友好错误。useConversationController保不确定请求key，mergeTurnEvents减少重复渲染。校园专属结果卡仍不足，不冒称课表/成绩全部卡片化。jsdom不是视觉真机验收。

## 回归结果（本轮）

最终完整API：398通过/1失败，69.80秒，唯一既有MCP真实子进程诊断失败；最小Node spawnSync(node --version)明确EPERM。保留失败，不删断言或宣称全绿。
认证完整12模块：306通过+67 subtests，22.31秒，含下载/shared-cache，无排除。
Runtime：58/58，1.35秒；真实安装版Cordis/FS/Approval/MCP内存协议和实际dispatch源码，非学校/模型端到端。
UI最终完整31文件166测试通过127.04秒（含事件merge和中文提示），build成功，lint零错误/25警告。标准配置bundle遇EPERM，native loader/threads可执行。
中间新增路由契约遗漏已CR018/OpenAPI同步并回归，最终route parity通过。

## 十场景覆盖

全为Mock/合成数据，真人认证/LLM调用/用户墙钟未测，不填零冒充。

1. 简单校园查询：20个既有domain脚本与API校园工具回归，partial不假实时。
2. 首次学在登录：无snapshot的Mock官网刷新1/API1，无程序凭据登录；真人SSO/MFA未测。
3. 已登录再次查：学在API1/刷新0；新桥授权1后query3/copy1，不重复批准。
4. 失效恢复：学在API2/刷新1；新桥query2/copy1，失败30秒冷却；整任务人机登录续跑未验收。
5. WebVPN：Mock显式login2请求、probe+业务累计4；失效第5后拒绝open无第6，生产未打通。
6. 连续多步：持久history/runner reuse、20脚本与domain恢复；无真实明天课程加自习地点模型验收。
7. 高风险：同实际dispatch源码IO fixture未知发送未经确认raw1变0、pending0变1，确认两次执行仅1。
8. 只读：GET搜索/普通导航pending0，领域read沿用授权，不新增逐次确认。
9. 历史：105条旧仅100，新3页105唯一ID，owner/游标/搜索隔离。
10. 侧栏移动：jsdom Drawer/收起/搜索/分页/账户/草稿验证；真机视觉仍未测。

没有真实任务成功率、tokens、总体延迟或确认降低比例数据；上述都是确定性契约指标，不是模型变聪明的证明。

## 未完成与本人配置

仍需真实校园SSO/学在/WebVPN与复杂模型端到端测试；当前缺凭据/离校网络及模型授权。P1 shell网络/host读、外部MCP全工具权限、任意网页事件副作用不能只靠提示词解决。
共享BrowserHost profile一键清Cookie暂未实现：需要全会话影响说明、原子队列排空、全部pending失效，未仓促删除。官方注销也不保证本地Cookie全面清除。
授权跨API重启持久存储、长期scope查看撤销、敏感journal保留期限/删除、整任务再认证resume仍需后续；当前grant仅内存，disconnect可撤销。
校园query/agenda专属来源卡、课表自动硬阻塞、时间窗口语义、实时空教室/座位/路线无可靠全链证明；不由提示词造数据。超长历史仍整份轮询，真机视觉未验收。

本人需：按既有launcher检查Node24/Python/Edge及锁定依赖；设置模型与每会话资料外发同意；只启用可信MCP、最少凭据；官方窗口本人SSO/MFA后账户页同意复用。服务重启需再次点击同意，不必重输密码（若profile有效）。旧学在连接器窗口仍独立。离校VPN本人官方认证，当前受控浏览器代理入口，不承诺direct connector自动VPN。
解除查询授权与学校注销分开；不要把密码/Cookie贴对话。持久浏览器会保存会话，不宣称Cookie绝不落盘。

本次仅本地工作区修改，无commit/push、无新框架/依赖，保留用户原改动。测试结构化汇总见followup-test-summary.json。
