# Harness follow-up（2026-10-15，离线）

## 证据与真实基线

先读取 git status、owned源码、已安装upstream，再向lead报告初审结论后修改。已有大量未提交用户改动全部保留，没有reset或修改node_modules；此前 docs/audit 仅作线索，不视为通过证明。Node v24.19.0，SDK/CLI/base 0.2.0-rc.2，Playwright MCP 0.0.83。本轮不调用真实远程模型、不登录校园账号、不读取个人凭据。

- 初始 prepare-profile 为 danger-full-access + approval never。never 会拒绝显式升级，不代表授予；但默认文件权限已无限制，所以 persona 的确认不是硬边界。
- campus-plugin 非campus tool-lease只传name/call_id，Store预算可防无限循环，不能判断工具参数是否外发。启用MCP相当于信任其全部工具；readOnlyHint不是授权。
- browser-host标准form提交已有fingerprint/确认重检/uncertain不重复执行；非form click、未知Enter及evaluate/run code/file upload/drop可绕过这条路径。
- 此前用户已增加Goal admission gap、native turn/end错误识别、domain error envelope、工具组mask等。真实基线 npm test:audit **43/43**，这些不冒认为本轮成果。

本轮增量：prepare-profile、browser-host、新browser-action-policy、tests/browser-safety、固定commit的before源码fixture；API agent_bridge/browser.py和test_agent_browser.py。未修改Store及routers/agent.py（lead负责）。

## 本轮修复

### 文件权限与升级

默认改实际支持的 workspace-write + ask，任务workspace和upstream允许的OS temp写自动，workspace外写需升级。真实installed SandboxedFileSystem已验证合成workspace写成功、外部路径FS_SANDBOX_DENIED；真实ApprovalService无answerer即时unavailable，配对记录approval/asked与approval/decided，不会空等，无自动full-access fallback。

**这不是网络隔离。**Windows upstream ACL partial、读未限制、workspace-write为FullLanguage，联网仍可用；不声称已彻底防止shell外发。

### 浏览器入口与确认复用

- catalog隐藏evaluate/run unsafe/file upload/drop/drag；旧schema或未知新工具实际执行仍fail-closed；API host前置拒绝已知高风险，daemon二次检查。owned内部inspect eval不接受模型代码。
- accept-dialog拒绝，dismiss自动；任意键盘快捷键拒绝，Enter/Space检查activeElement。installed press_key schema只有key，无target。
- 未知非form按钮/键盘激活创建具体页面、字段和目标pending；相同fingerprint复用；owner确认前重检私有字段hash和target_state，变化stale；重复确认不再次执行。标准form确认保留。
- HTTP anchor与无inline onclick的精确分页label是窄navigation例外；GET search自动，不是全部点击都确认。
- navigate及tabs-new均禁javascript/data/file，只允许HTTP/HTTPS/about:blank；Playwright模型输出filename仅basename，不能穿越native fs sandbox之外写文件。
- installed network_request schema只是inspection不是send，但request body可能含人工输入密码，headers含cookie。模型只允许part=response-body，省略part/request-body/所有headers拒绝；network_requests列表自动。
- type/fill前检查password及autocomplete password/OTP字段，用户必须人工填；公开预览排除OTP/password。**参数可能在到host前已写入upstream journal，不能承诺用户把密码发进对话后不会持久化。**必须不要求用户复制密码。

### 私有校园会话交接

BrowserHost.campus_cookies_private仅供可信owner校园adapter：固定内部cid/method=campus-session，无turn，不作为model schema；没有live daemon/context时AUTH_REQUIRED，不启动浏览器。Node/Python双重限定exact zjuam.zju.edu.cn/zdbk.zju.edu.cn/eta.zju.edu.cn/父zju.edu.cn；既有adopt会将父cookie重限定CAS host。值不出现在status/模型日志，cookie不是认证成功证明，仍需校园probe；owner endpoint集成由lead负责。未真实登录验证。

## 测试与确定性前后指标

`cd apps/agent-runtime; npm run test:audit`：**58 passed/0 failed**，约1.45s，test-isolation=none。

真installed Cordis ToolRuntime、ApprovalService、SandboxedFileSystem、Playwright MCP in-memory catalog（launch callback禁止浏览器启动）。dispatch/pending/recheck使用实际源码body，仅browser IO/disk IO注入fixture，不是真实Playwright点击。

before fixture来自`git show HEAD:apps/agent-runtime/browser-host.mjs`，commit **f5ebbbcc4ce7d163e44ed583b96cce06ff49bd9b**（该文件本轮开始无用户改动）。保存完整source以避免Node spawnSync git EPERM，前后同一IO fixture实际执行，不用regex推断：

|未知非form发送click|HEAD|本轮|
|---|---:|---:|
|未经确认raw调用|1|0|
|pending review|0|1|
|needs_user|false|true|

回归另验证相同pending复用仍1条、确认两次只raw执行1次、target变化执行0次；GET search/普通nav仍自动。这不代表真实模型成功率提升。

API专项命令（仅UUID synthetic DB/files/secrets）：

```powershell
apps\api\.venv\Scripts\python.exe tools\audit_regression.py apps/api/tests/test_agent_browser.py apps/api/tests/test_agent_gateway.py apps/api/tests/test_agent_error_feedback.py apps/api/tests/test_agent_tool_budgets.py apps/api/tests/test_agent_runner_reuse.py apps/api/tests/test_agent_stopping.py apps/api/tests/test_agent_tools.py apps/api/tests/test_agent_schedules.py -q -p no:cacheprovider -k 'not test_mcp_settings_actual_connection_diagnostic'
```

**103 passed/1 deselected**，30.07s。未排除MCP的较早专项：98 passed/1 failed，旧actual MCP calculator连接返回MCP_CONNECTION_FAILED，环境Node spawnSync git亦明确EPERM。真实MCP子进程未通过，不以in-memory catalog冒称真实connection通过，不减弱断言。

直接pytest临时目录mode0700撞WinError5，使用已有tools/audit_regression，仅其合成UUID测试树适配ACL，未动真实data。

## Harness预算/context/错误反馈

Store model_calls与tool/native leases共享总预算，停止turn拒绝新执行；已授权idempotent结果复用不重复提交。默认model128/tool512/turn1800s/output16384。runtime reuse、stop draining、model_error、正文24k预算/真实分页游标专项通过。load_tools仅catalog mask，不授权；原parent/child policy不误提升测试保留。

历史/context仍official log/native compaction，不做最近N条重写。CampusModelAdapter未提供contextWindow，output maxTokens不是input budget；真实模型长上下文恢复本轮无新证据。来源与coverage/freshness/warnings保留；读到部分不宣称全量。

## 未解决限制

1. **P1 shell联网/host读权限与MCP全工具信任**；模型绕shell发送不能靠persona阻止。需网络/凭据隔离和完整owner MCP permission UI，frontend已收到可信服务/最少凭据说明建议。
2. **P1 任意网页非静态DOM**：anchor registered listeners、导航GET副作用、input/change自动保存、select/hover事件，仍可能外发。本轮窄规则是护栏，不是所有未知写入绝对不会发生的网络级证明。
3. screenshot/响应正文可含敏感页面，不承诺短值或视觉凭据100%脱敏。request-body/headers收紧减少明确泄露源。
4. workspace-write仍有session override、temp roots、Windows ACL partial、hard-link/TOCTOU边界；未真实confined child验证完整系统隔离。
5. shared browser profile清空暂不实现：需要所有会话授权说明、队列原子操作、active turn排空及全部pending staling；断开连接/官网退出不等于本地cookie清除。
6. 无真实模型/校园登录/远程提交验证；本地完成不是校园提交成功，确认执行仍需网站回执。未限定时间且用户时区未知仍需澄清。
