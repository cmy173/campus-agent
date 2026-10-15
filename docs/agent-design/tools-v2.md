# V2 工具与使用说明

更新：2026-10-15。当前能力与独立验收结果以 [Harness 提交说明](../audit/pr-validation.md) 为准。

当前项目能力策略禁止模型调用任意 Shell、PowerShell、Python 代码执行及 workflow；工具发现不等于授权。
下文保留的早期通用能力验证记录属于历史阶段，不能作为当前可调用工具清单。
生成 PDF/XLSX/Markdown 应使用只接受数据的 `artifacts.export`，出卷应使用 `reviews.*` 与真实后台 job。

## 日常使用

启动新系统后进入聊天，选择并配置模型服务。描述要完成的事情即可，不需要记住工具名称。

- “整理任务目录里的说明文档，找出我需要提交的材料”：文件检索与阅读，必要时结合校园资料来源。
- “根据这份附件整理报告和表格”：先读取已授权材料，再用 `artifacts.export` 生成真实文件；缺失事实保留未知，当前不提供任意 Python 执行。
- “搜索这项政策并核对原文”：搜索和网页读取是两个操作；保留可访问来源。真实搜索服务必须有适用权限和凭据。
- “按校园资料整理技能处理这些附件”：按需加载 Skill，复用说明中的流程。技能本身不是完成证据，仍须实际执行。
- “分成两个子任务分别处理，最后汇总”：Harness 分配子任务，聊天展示子任务状态及结果。可以单独停止子任务，等待实际结束事件后显示已停止；后台跨轮生命周期与重启恢复仍在建设。

普通本地读取、计算与可撤销操作自动执行。停止先显示“停止中”，进程与在途调用收尾后显示“已停止”；未返回结果不会因为会话结束被标为成功。浏览器提交已接入一次确认；延迟/CPU脚本、普通派生进程及当前轮次实际停止通过，跨轮及各子Agent浏览器归属仍待完善。

## 设置入口

聊天右上角的设置抽屉包含“模型服务”和“工具与搜索”。

### 任务目录与 Skills

任务目录留空时使用应用的默认目录；填写绝对目录后，下一个轮次在该目录执行获准的文件操作。目录设置不会授予命令执行或私有凭据访问能力。用户数据不会因为改变目录而被删除。

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

默认实际注册70项：16项校园工具、24项上游通用工具、1项文件交付工具、26项应用浏览器工具和3项 MCP 资源工具。配置示例计算 MCP 后实际71项。当前仍在每轮注册这些工具，按需工具发现尚待完善；工具数不是任务成功率。

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
| 浏览器 | mcp__campus_browser__browser_* | Edge读取/下载/填写、一次确认、登录状态重启保留、导航及root延迟/CPU脚本停止已验证；真实学校试用、跨轮及独立子Agent浏览器归属待补 |

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

聊天通用结果通过 turnProjection.ts 适配，ToolActivities.tsx 负责展示，useTurnEvents.ts 负责分页/重连。未知工具可显示输入、文本结果和失败状态；校园来源保持专用信封，并按 session/call ID 去重。文本结果不解释为 HTML。文件下载/预览工作区已接入；Markdown、PDF、Excel 生成/交付及 PDF/图片/表格预览已实际验证；通用浏览器见下一节。

## 登录态浏览器与一次确认

在聊天中描述浏览器任务，例如“读取这个页面，下载附件并整理要求”，或“把报名表填好，提交前让我确认”。不需要自己配置浏览器 MCP 服务，应用保留 campus_browser 名称作为内置连接。

需要登录时，点“浏览器”→“打开浏览器 / 登录”，可以先填写网站 URL。用户在助手自己的 Edge 窗口完成登录或验证码，再继续聊天。不要把密码复制到对话。浏览器配置位于当前 CAMPUS_AGENT_HOME 的 browser/profile，与日常 Edge 分开；Cookie 和网页存储保留。聊天轮次结束后窗口/登录态继续存在；“停止浏览器”关闭当前浏览器，配置仍保留。后台浏览器切换为可见窗口需要重启浏览器上下文，当前页面表单草稿不保证保留，提交前会重新检查。

读取、普通 GET 搜索、下载和填写自动执行。标准表单提交、输入后提交或表单内回车会生成待确认记录；工作区显示真正的提交地址、字段值与文件名。点“确认并执行”只执行一次，同一记录的重复确认返回原回执。字段、密码、隐藏状态或目标发生变化时旧确认失效，须重新准备。密码和隐藏字段不出现在公开预览中。

非标准发送/提交按钮先用 browser_prepare_action，提供具体对象、内容、附件与按钮目标；预览同时采集当前网页的可编辑字段，确认前重新检查字段与按钮。通用网页脚本和联网代码仍可使用，遵守同一提交约定，不用脚本绕过待确认操作。这是个人自动执行的交互合同，不把 C 档改成受限脚本沙箱，也不声称可识别每个网站的所有副作用。

“已执行操作”只表示浏览器动作完成，网站业务是否成功仍需核对实际页面。结果不确定时保留 uncertain 回执，不自动重复提交。刷新和应用重启后记录仍可查看；下一轮会得到最近操作的实际状态，避免对已执行动作重复提交。

附件实际下载后复制为会话成果，自动出现在聊天卡片和文件工作区；工作副本后续变化不会改变已交付文件。页面快照直接附在工具返回中，保存的快照/文本输出也会隐藏已观察到的密码、Cookie、认证存储及认证头值。浏览器原生私有配置仍保存登录凭据，不提交 Git。

默认使用已安装 Edge。可通过 CAMPUS_BROWSER_CHANNEL 或 CAMPUS_BROWSER_EXECUTABLE 选择适用 Chromium 浏览器；不自动下载浏览器。默认不禁用系统代理，需要指定代理时使用 CAMPUS_BROWSER_PROXY。应用启动与 Harness 子进程保留 Windows APPDATA/LOCALAPPDATA 等适用环境。

浏览器工具包括导航/后退、页面快照与查找、表单填写/点击/键盘/选择/上传、标签页、对话框、截图、网络检查、浏览器脚本以及明确操作预览。复用固定 @playwright/mcp0.0.83 和对应 Playwright1.64.0-alpha-1790635538000 公共接口；没有依赖 Codex 的开发浏览器工具。

独立实际验证：node apps/agent-runtime/verify-browser.mjs。完整 API/Harness 验证仍使用 tools/verify_agent_runtime.py。受控站点、受控模型及真实文件/进程分开记录；这些证据不替代真实 DeepSeek 和学校账号试用。架构与后续停止审计见 browser-v2.md。

停止回归还包括 tools/verify_browser_cancellation.py（--cpu验证事件循环阻塞）及 tools/verify_process_ownership.py。它们运行实际本地进程和页面，不调用付费模型。实现及范围见 runtime-lifecycle-v2.md。

## 子任务停止与官方协议验证

子任务卡片显示任务名称、进度和停止按钮。停止请求被接受时显示“停止中”，收到 Harness 实际 aborted 结束事件后才显示“已停止”。单独停止一个任务不会取消其他子任务或父任务。父轮已结束的任务不能继续通过此入口操作；当前 SDK 仍按轮次关闭，不能将 continuable 工具注册当作跨轮后台任务完成。

官方 provider 的验证使用实际上游 Messages/SSE 协议和本地受控端点，覆盖写文件→读文件→回答、模型/工具租约、401 错误及 JSONL 日志脱敏。保留错误分类、重试时间和图片 offload 恢复参数；没有执行真实付费请求。

## 附件与文件成果

开始对话后，点输入框旁的“附件”选择本机文件（支持多选，单文件上限默认100 MiB）。成功上传的文件自动选中，随下一条消息发送；附件选择和草稿会在刷新后恢复。每条消息最多选择32个附件，不会自动把会话里的所有文件发送给模型。移除标签只取消本次选择，原文件保留。

聊天上方“文件与成果”列出上传资料与生成成果，可以重新选作附件、预览和下载。文本/Markdown/CSV/代码按原文预览，HTML 也是原文，不运行其中脚本。文本预览最多512 KiB，下载保留完整内容。PDF 使用按需加载的应用渲染器，支持页码、翻页和放大，不依赖原生 PDF 插件；图片直接预览。Excel 可以按工作表查看，最多8张表、每表100行24列；截断有提示，原件可完整下载。公式显示为原式，不冒充计算结果。其他格式可下载后打开。

模型在实际写出文件后调用 publish_file(path)。相对路径使用该轮任务目录，绝对路径可交付其他明确任务文件。工具复制现有文件形成独立成果副本，返回文件ID/大小/哈希；主聊天出现统一成果卡片，点“打开成果”进入预览，点下载得到原始文件。原工作文件后续修改不会改变已交付副本；同一交付调用重放仍返回原副本，更换文件路径会报幂等冲突。文件生成失败或不存在不会被交付为成功。

验收使用实际 multipart 上传、真实 Harness 读取/写入/交付与实际浏览器下载。模型仍受控，不替代真实 DeepSeek 决策验收；Markdown、中文 PDF、Excel 最小生成/交付闭环已通过。文件夹选择与可撤销编辑历史不包含在这项完成声明中。

## 文档生成技能

“读取这些资料，生成一份 PDF 和 Excel 清单”会使用文件/网页工具读取材料，按需要加载 document-artifacts 技能，再使用应用的 Python 环境生成成果。技能提供报告和清单的可复用脚本及输入格式说明；复杂排版、图表或分析可以直接编写 Python，不限于固定模板。

脚本可生成 Markdown、中文 PDF 和 Excel。PDF 使用可嵌入字体并自动分页，表头跨页重复；Windows 默认微软雅黑，缺失字体或字符会明确失败，可通过 --font 指定字体。Excel 保留数字、零、空值、日期和明确的公式，普通以“=”开头的文本保持文本；公式不自动计算缓存。缺失截止时间保持未知，来源由输入材料提供。现有同名成果拒绝覆盖，修订可另存名称；完成后用 publish_file 交付到聊天。

项目依赖固定 reportlab5.0.1、openpyxl3.1.5、Pillow12.3.0、et-xmlfile2.0.0，已写入 Python 版本/官方 wheel 哈希锁。前端 PDF.js6.4.299 固定在 npm 锁中，只在打开 PDF 时加载其解析器与 worker。Codex 的内置运行库仅用于开发版式检查，不是产品运行依赖。

技能与脚本：apps/agent-runtime/skills/document-artifacts/SKILL.md，references/input-format.md，scripts/export_documents.py。实际 Harness 验收包含加载技能/读取格式说明/读取上传 CSV/写入文档内容/执行 Python/交付三份文件共8项工具操作。模型响应受控，真实服务的格式选择和内容整理效果仍需试用。

实现依据：[ReportLab 字体文档](https://docs.reportlab.com/reportlab/userguide/ch3_fonts/)、[PDF.js 示例](https://mozilla.github.io/pdf.js/examples/index.html)、[openpyxl 文档](https://openpyxl.readthedocs.io/en/stable/)。
