# 校园本机认证/连接状态工具 envelope 修复 — 2026-10-17

## 修改前真实 Agent 基线

批次 `20261017-101410-36933e06`，真实本地 Agent API/provider，经正常对话与 turn 请求执行 AUTH-01 一次；只读取本机认证状态元数据，没有登录、同步或校园网络访问。配置为单并发、60 秒任务上限、每任务/总计最多 4 次模型调用；实际 2 次模型调用、1 次工具调用，5.51 秒完成。

运行时提供的精确工具 schema 完整且未截断，`campus.credentials.status` 在两次模型调用中都可见；Agent 首轮选择该工具，工具执行 27.97 ms，无错误，随后有模型回复。最终任务评分为“部分成功”，不是完全成功。

该次脱敏工具 envelope 给出 `status=partial`，但它读取的是本地凭据/工作区状态，没有远端校园数据获取时间。这来自 `tools.py` 对所有校园工具统一附加“校园数据原始获取时间及完整性声明”的通用缺失项。`partial` 是封装器错误语义：本地状态快照可以完整读取，但不能据此验证学校是否接受凭据或远端 session 是否有效。场景 oracle 还发现最终回复没有明确披露这一远端验证限制，因此该运行仍应为部分成功。

## 单变量修复

仅改变两个同类本机状态工具的结果 envelope：

- 以 `local_auth_metadata` 标注覆盖范围。
- 用本地状态快照观察时间填写 `freshness.fetched_at`。
- 将本地元数据读取结果标记为完整 `ok`，不再套用校园远端数据缺失项。
- 保留明确 warning，声明本次读取不验证学校是否接受凭据或远端 session 是否有效。
- 不增加网络访问，不触发登录，不改变凭据内容或权限。

`campus.status` 采用同一修复，scope 为 `local_campus_connection_metadata`；它同样读取本机 workspace 状态，不应因没有校园远端 `fetched_at` 而变成 partial。此工具修复由执行路径和定向单测支持，尚无本轮真实 Agent 调用证据。

工具主逻辑本来就会给出本地状态限制说明；本修复没有宣称提高 Agent 最终回复的完整性。

## 验证与限制

- 修改前真实基线如上，源码/工具运行版本由该批次轨迹记录；工具可见性检查结果为 `available_first_call`，不是缺少工具造成的失败。
- 修改后 `apps/api/tests/test_agent_tools.py`、凭据和持久授权回归：76 passed。其中两个状态工具均断言 `status=ok`、完整的本地覆盖、新鲜的本地观察时间和远端验证限制；凭据工具测试还验证没有用户名提示、保存时间或秘密字段。
- Benchmark runner 回归：78 passed。
- 当前本地 API 在真实批次完成后已停止监听 8000 端口；未擅自重启。因此修改后的真实模型/A-B 复测尚未完成。单测验证的是本地 API 执行函数和 envelope，不冒充修改后端到端结果。
- 修复后需在 API 以新代码正常启动的下一次只读 AUTH-01 测试中确认：工具 envelope 为 `ok`，并单独检查模型是否说明“已保存/自动重登/浏览器授权/连接状态”不等于远端验证。

**后续环境复核（2026-10-17 10:20，Asia/Shanghai）：**本机 API 在后续检查时可于 `127.0.0.1:8000` 访问，`probe` 批次 `20261017-authfix-preflight` 的健康检查通过。此前一次监听快照为空，说明该观察并非稳定服务状态。当前进程 PID 2556 的启动时间为 2026-10-16，早于本次 Python 修改；不能假定其已加载修复代码，也没有重启它。只读状态显示校园浏览器授权未授予、当前校园工作区 disconnected，`xzzd` 连接器状态 `ok` 的最后检查时间为 2026-10-16，`zykt` 为 `service_unavailable`。因此本轮没有执行依赖已认证校园 session 的真实私有读取。

这次发现 API 健康检查只有版本号，不能证明 API 进程加载了哪个工具执行器版本。已增加 `/api/v1/health.agent_source_sha256` 启动时指纹，覆盖 API Agent、校园 connector 与 learning executor Python 源；Benchmark preflight 将它与当前 checkout 比较，并在 manifest 记录状态。比较逻辑要求两批均有相同的已加载指纹，否则因果配对标为未验证。对当前老进程的直接 preflight 显示 `agent_source_status=not_reported`（健康检查 HTTP 200）；这证明它目前无法提供代码匹配证据，而不是证明其加载了新代码。

## 后续检查

1. 如果修改后任务仍为部分成功，逐项看 `distinct_auth_states_explained` 与 `remote_authentication_limit_disclosed`，不要将其归因于工具可用性。
2. 对 `campus.status` 和其他本机元数据工具做相同 envelope 契约审查，避免把“本地快照没有远端 fetched_at”统一报告为不完整。
3. 不删除 `campus.credentials.status`：本次 Agent 实际选中并成功执行它，缺陷是结果 envelope 语义。
