# V2 工具与使用说明

更新：2026-10-14。当前已实现部分，完整 C 档仍在推进。证据以 docs/agent-v2-progress.md 为准。

## 日常使用

启动新系统后进入聊天，选择并配置模型服务。描述要完成的事情即可，不需要记住工具名称。

- “整理任务目录里的说明文档，找出我需要提交的材料”：文件检索与阅读，必要时结合校园资料来源。
- “用 Python 计算这份数据，解释计算过程”：由 PowerShell 调用应用的 Python 环境；在任务目录保留需要交付的文件。
- “搜索这项政策并核对原文”：搜索和网页读取是两个操作；保留可访问来源。真实搜索服务必须有适用权限和凭据。
- “按校园资料整理技能处理这些附件”：按需加载 Skill，复用说明中的流程。技能本身不是完成证据，仍须实际执行。
- “分成两个子任务分别处理，最后汇总”：Harness 分配子任务，聊天展示子任务状态及结果。可以单独停止子任务，等待实际结束事件后显示已停止；后台跨轮生命周期与重启恢复仍在建设。

普通本地读取、计算与可撤销操作自动执行。用户可以停止当前轮次；未返回结果的操作不会因为会话结束被标为成功。对外提交的一次确认链路仍待浏览器部分完成。

## 设置入口

聊天右上角的设置抽屉包含“模型服务”和“工具与搜索”。

### 任务目录与 Skills

任务目录留空时使用应用的默认目录；填写绝对目录后，下一个轮次在该目录执行通用文件与命令操作。用户数据不会因为改变目录而被删除。

额外技能目录每行一个。项目自带 apps/agent-runtime/skills/campus-material-organizer/SKILL.md；还可以加载自定义目录内的 Skills。项目技能加载已执行验证，任意第三方技能的实际效果需分别检查。

### 独立网页搜索

搜索地址、模型及凭据独立于聊天设置。地址/模型留空时复用锁定 Harness 版本的上游默认值。地址是 Anthropic-compatible Messages 基址，不含末尾 /messages。

密钥可以共享当前对话的模型密钥，也可以独立保存。共享时需确保该凭据适用于搜索端点，不能把聊天连接成功等同于有搜索权限。“测试已保存的搜索设置”发送一次公开文档检索请求，不发送本地资料，可能产生服务费用。失败显示配置或连接错误，不显示成“没有结果”。

真实 DeepSeek 服务仍待用户选定接入方式并授权试用预算。已通过的搜索验证使用实际上游 provider 和受控端点，不能替代真实搜索试用。

### MCP 服务

支持标准 mcpServers 配置或服务列表。例子中的命令、地址和密钥需替换为自己的服务信息：

```json
{
  "mcpServers": {
    "mytools": {
      "command": "python",
      "args": ["D:/Tools/my_mcp_server.py"],
      "env": {"SERVICE_TOKEN": "填写本地服务凭据"}
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer 填写服务凭据"}
    }
  }
}
```

本地服务使用 stdio，远程 URL 使用 streamable-http。服务名必须唯一，由英文字母、数字、下划线或短横线组成，长度1–32。服务列表中 enabled=false 可停用，删除条目并保存可解除配置。

保存后“测试连接”会启动或连接实际服务并发现工具，不调用聊天模型。服务的工具以 mcp__服务名__工具名 注册到 Harness；不同服务的同名工具可以共存。上游同时按需提供 MCP 资源列表、模板及读取工具。

环境变量与请求头使用现有本地加密凭据存储。读取配置时显示 __SAVED_CREDENTIAL__；保留标记即可沿用凭据，替换标记会保存新值，从映射中移除名称会停止向服务传递该项。凭据不会写入生成的 Harness profile、聊天正文或测试响应。

断连后由上游 MCP supervisor 重连；实际进程断开、错误结果及重新连接后的调用已验证。当前每个会话轮次拥有独立 Harness 进程；会话外常驻服务与后台任务恢复还在实施。

## 当前工具清单与验证范围

默认实际注册40项：16项校园工具与24项通用工具。配置示例计算 MCP 后，实际注册44项：新增计算工具及3项 MCP 资源工具。工具数随配置和按需发现变化，不是任务成功率。

| 工具类别 | 模型注册名 | 当前证据 |
| --- | --- | --- |
| 文件 | read、write、edit、glob、grep、read_image | read/write 已实际运行；其余已注册，专门流程待验收 |
| 命令 | pwsh（Windows）、job_list、job_output、job_kill | 实际 PowerShell/Python 成功；通用后台命令的恢复与停止待验收 |
| 网页 | web_search、web_fetch | 搜索为实际 provider 配受控响应；fetch 已读取真实公开网页 |
| 技能 | skill、workflow | 自带校园 Skill 实际加载；工作流专门闭环待验收 |
| 子任务 | subagent、subagent_fork、list_agents、send_message、interrupt_agent | 两个独立子任务实际执行与汇总，前台/continuable 子任务单独停止及兄弟任务继续已验证；后台跨轮/重启恢复待验收 |
| 目标与步骤 | create_goal、get_goal、update_goal、todo_write、exit_plan_mode | 上游已注册；Campus 内的持续任务、常驻调度与重启恢复尚未完成 |
| 连接/课程 | campus_connections_status、campus_courses_list | 实际领域服务合成数据集成已通过 |
| 作业/资料 | campus_assignments_list、campus_materials_list、campus_materials_search、campus_materials_read | 多轮检索、正文、来源与日期精度已回归；学校真实账号待试用 |
| 学习任务 | campus_tasks_list、campus_tasks_build、campus_tasks_feedback | 实际生成任务、后台 worker、学习反馈与回执已回归 |
| 时间安排 | campus_availability_read、campus_plans_read、campus_plans_preview、campus_plans_apply、campus_plans_revoke | 现有具体预览/接受/应用/撤销通过；自然语言直接安排的自动授权改造待完成 |
| 领域后台任务 | campus_jobs_read、campus_jobs_cancel | 实际任务状态和取消已回归 |
| MCP | mcp__<server>__<tool>；list_mcp_resources、list_mcp_resource_templates、read_mcp_resource | 实际计算服务发现、执行、错误、断连/重连；资源工具已注册，资源读取专门验收待补 |

## 运行与维护

运行时固定 @deepseek-ai/dsh 与 sdk-client 0.2.0-rc.2；使用 dsh-base 的公开能力。MCP client 固定2.2.0，fflate固定0.8.3。Python 保留校园业务与会话/配置边界，不重建通用 Agent 主循环。

模型调用、单轮时长和输出预算通过 CAMPUS_MAX_MODEL_CALLS、CAMPUS_TURN_TIMEOUT_SECONDS、CAMPUS_MAX_OUTPUT_TOKENS 配置；默认128次、1800秒、16384 tokens。CAMPUS_MAX_TOOL_CALLS 默认512，已统一计数校园、通用、MCP及子任务工具调用；同一调用重放不重复扣预算。不能把这些计数当作金额账单。

验证命令（使用隔离数据，不运行付费服务）：

```powershell
node apps/agent-runtime/verify-capabilities.mjs
node apps/agent-runtime/verify-mcp.mjs
node apps/agent-runtime/verify-official-provider.mjs
node apps/agent-runtime/verify-subagent-controls.mjs
apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py
```

verify-capabilities 使用受控模型/搜索响应及真实文件、命令、公开网页和子任务；verify-mcp 使用受控模型和真正独立的 stdio MCP 进程；完整 Python 验证还覆盖设置API→实际运行时→文件/Python/MCP 的链路。证据保存在各自 var 临时目录，不纳入 Git。

聊天通用结果通过 turnProjection.ts 适配，ToolActivities.tsx 负责展示，useTurnEvents.ts 负责分页/重连。未知工具可显示输入、文本结果和失败状态；校园来源保持专用信封，并按 session/call ID 去重。文本结果不解释为 HTML。文件下载/预览工作区和通用浏览器仍待实现。

## 子任务停止与官方协议验证

子任务卡片显示任务名称、进度和停止按钮。停止请求被接受时显示“停止中”，收到 Harness 实际 aborted 结束事件后才显示“已停止”。单独停止一个任务不会取消其他子任务或父任务。父轮已结束的任务不能继续通过此入口操作；当前 SDK 仍按轮次关闭，不能将 continuable 工具注册当作跨轮后台任务完成。

官方 provider 的验证使用实际上游 Messages/SSE 协议和本地受控端点，覆盖写文件→读文件→回答、模型/工具租约、401 错误及 JSONL 日志脱敏。保留错误分类、重试时间和图片 offload 恢复参数；没有执行真实付费请求。
