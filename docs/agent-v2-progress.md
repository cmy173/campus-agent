# Campus Agent V2 进度

Goal：按 V2 合同持续完成 V2.0–V2.4。实施中，全部验收尚未满足。
唯一实施目录 D:/AI/campus-agent；分支 codex/personal-assistant-v2。
首轮工作树保留为来源，后续不在那里修改产品源码。

## 已完成增量

- fetch 成功；发现 origin/feature/blue-chat-ui 设计参考 20a9c60，未覆盖当前工作。
- 新快照 D:/AI/campus-agent-goal-backups/20261013-v2-start：原138、新482源码文件逐项ZIP哈希验证；私有数据另存，49个SQLite在线备份 integrity_check 通过；Git bundle 验证通过。
- 原校园增量与 V2 合同本地提交 08ccece，无推送。
- 首轮源码提升至主目录，471个新增文件、6个一致、3份冲突文档保全；README与 .gitignore 单独归并。证据 agent-v2-adoption.json。
- launch.py 已委托 tools/dev.py，旧快捷启动也采用新系统；旧界面已退出默认启动链路，功能覆盖前仍留源码。
- 新个人数据保全带入 var，旧 data 保留；数据、凭据、依赖与构建目录 Git 排除。
- 主目录安装独立依赖成功，pip check 无缺失；46个运行 wheel 与构建 backend 采用同版本官方 URL/哈希锁。首次普通 pip index 解析长时间停滞，确认网络和进程后终止自有安装，切换精确 wheel；未更改运行依赖版本。生成锁时一次 TLS EOF 经缓存和有限重试修复。
- 实际主目录 API 整套272通过（42.62秒），前端19文件125通过（27.37秒），TypeScript/Vite生产构建成功。
- 新启动器环境检查与代理 smoke 成功，采用独立合成数据库，未以测试建库覆盖个人库。
- 实际 Harness 主目录集成成功，41次受控模型调用；证据 var/agent-integration-jvp5x4ct/evidence.json，覆盖多轮、取消、重启、计划、后台任务和反馈。仍是首轮16项校园工具，不能当作 C 档完成证据。
- 第一项 UI 分层已实施：useConversationController 管理请求/草稿/重试/恢复，AgentPage 负责展示与面板状态，agent-theme.css 集中视觉变量；既有交互回归125项通过。统一产物、换布局实例及浏览器验收尚缺。
- V2.1 已开始：复用固定版本 dsh-base，移除仅校园工具 guard；实际模型工具注册表为16项校园工具加24项上游通用工具，共40项。文件、PowerShell、网页、Skills、子 Agent、Goal 等来自上游，不另建主循环。
- DeepSeek 官方 provider 已接入代码路径；官方地址使用上游 provider，兼容地址保留桥接。模型租约仍验证服务端配置快照，密钥通过自有子进程环境使用。模型/工具/单轮预算改为可配置，默认128/512/1800秒，输出16384 tokens。真实付费验证尚未执行。
- MCP client 固定修复版2.2.0、fflate固定0.8.3，npm安装完成且audit零漏洞；真实MCP连接兼容性仍待验收。
- 扩展后网关相关15项测试通过；真实SDK校园集成41次受控模型调用通过，证据 var/agent-integration-bin9w2b6/evidence.json。不能把受控模型当成真实DeepSeek服务验证。
- 通用工具真实执行通过，证据 var/v2-capabilities-TEz8fE/evidence.json：实际写入/读取文件、PowerShell执行Python计算42、加载校园资料整理Skill、真实HTTP读取公开DeepSeek文档、两个独立子Agent读取/计算56并汇总。搜索provider实际运行但使用本地受控搜索响应，模型也受控；公开网页读取是真实网络访问。
- Windows原生程序调用问题已定位为子进程缺失PATHEXT，修复Python→Node→Harness环境传递并验证成功。普通本地工具不逐次要求批准；外部动作确认、持续任务恢复和调度仍待实现。

## 2026-10-14 增量：MCP 设置与通用结果

- 实际 MCP 集成通过：工具发现/调用、服务错误、真实服务进程退出、上游 supervisor 自动重连及恢复调用。证据 var/v2-mcp-2lAeqn/evidence.json；固定 MCP client2.2.0 兼容性已验证。
- 本地配置API和聊天设置抽屉已接入任务目录、额外技能目录、独立搜索地址/模型/密钥以及MCP服务。可粘贴标准mcpServers配置，保存、停用与测试连接。MCP诊断不调用模型，搜索测试按钮明确说明一次公开请求及可能费用。
- 凭据复用本地加密存储；API回读为保留标记，实际值只在自有运行进程/目标MCP环境中使用。薄适配器 campus-mcp.mjs 复用上游发现/调用/重连，生成的Harness profile不含MCP凭据。真实测试已确认凭据到达服务且不进入模型消息/验收记录。
- 通用工具事件与子任务状态适配为稳定展示模型 turnProjection.ts；分页/恢复移入 useTurnEvents.ts，ToolActivities.tsx负责展示。未知MCP工具可以显示输入/结果/失败，校园来源去重后保留原展示；文本不作为HTML解释。个别子任务的停止按钮与完整生命周期尚缺。
- 浏览器实际执行“设置MCP→创建对话→写文件→读文件→PowerShell/Python→MCP”，四项结果均成功，返回42与252。刷新后结果及未发送草稿保留；1280×900桌面与390×844窄屏检查通过（窄屏documentWidth375、viewport390）。证据 var/agent-integration-stfjf1mm/browser-evidence.json，截图同目录chat-tools-desktop.jpg/chat-tools-narrow.jpg。
- 浏览器初验发现API子进程在继承的Windows临时目录mkdtemp出现EPERM；改为每会话自有临时目录后实际Python执行通过。此前失败保留在 var/agent-integration-6_4c4isf；不把模型正常返回等同于每项工具成功。
- 完整实际Python/API/SDK集成扩展通过，46次受控模型调用，设置API→运行时→通用工具和MCP全部四项成功，原校园多轮/取消/重启/计划/任务/反馈回归保留。证据 var/agent-integration-0r0pze2b/evidence.json。
- 默认40项工具；启用示例MCP后上游自动增加计算工具及3项资源工具，实际44项。最初新增验证只预期41项，发现资源工具的合法按需注册后修正准确集合并重验；未删除成功/失败的实际执行断言。
- API整套279通过（53.32秒）；契约覆盖9通过；前端21文件131通过（maxWorkers=2，79.25秒）。首个默认并行前端运行出现一项连接界面嵌套查询等待超时，单文件复验4通过，限制测试worker后全套通过；未改变断言或放宽超时。
- 契约CR011已补录，由 tools/export_agent_contract.py 导出实际14项Agent路径/13个引用schema。新增使用文档 docs/agent-design/tools-v2.md，逐类区分实际执行、受控服务和仅注册状态。

## 验收台账

| 条件 | 状态 | 当前证据 / 尚缺 |
| --- | --- | --- |
| 1 唯一源码与安装启动 | 已验证（合成范围） | 全新目录（仅跟踪文件 679 个）`--setup` exit 0（53 锁定 wheel + 180 网页包 + 540 运行时包）→ `--check` exit 0 → 生产构建 exit 0 → 真实 Harness 集成 77 次受控调用 exit 0；旧服务器已删除；真实账号/模型试用仍缺 |
| 2 迁移与旧代码删除 | 已验证（合成范围） | 旧 `campus_agent` 应用整体删除（服务器/静态UI/旧模型转发/旧抽取管线/旧Store/旧调度/旧本地账号）；校园协议单份位于 `connectors/campus`；保留断言改挂到迁移实现（191项+80subtests）；旧库只读导入仍可用；真实校园账号试用与真实旧库导入仍缺 |
| 3 全部 C 档能力 | 进行中 | 文件/代码/Skills/MCP/文档/双子任务/持久目标/本机调度均已实际执行；按需发现的分组掩码已验证有效（83项/63KB → 45项/34KB），但**尚未安全接线**；真实搜索/模型仍缺 |
| 4 真实官方模型三场景 | 未开始 | 待 provider 与本地账号配置/预算 |
| 5 浏览器/下载/表单 | 进行中 | 应用自有持久Edge，真实MCP与API/Harness闭环、受控表单一次确认防重、字段变化拒绝、真实下载及UI恢复通过；真实校方登录态试用仍待完成 |
| 6 子 Agent/持续任务/调度 | 进行中 | 双子任务独立停止、持久目标冷启动恢复、本机调度真实线程+跨进程断点防重通过；跨轮子任务与独立浏览器归属仍待验收 |
| 7 自动执行及正确性 | 进行中 | 通用本地操作自动执行、可配置预算；计划意图授权与外部提交确认待改造 |
| 8 UI 分层与浏览器验收 | 进行中 | 请求/状态/展示/主题拆分，主导航收敛为 /chat 且旧URL跳转、任务工作区与换布局预览通过；**桌面全宽（1280×900、1920×1080）真实 Edge 回归通过**（无横向溢出、输入框在视口内、工作区打开、草稿刷新保留，含截图与人工查看）；窄屏沿用既有证据 |
| 9 性能前后对照 | 进行中 | 真实本机受控夹具基准已建立并两次重复（启动2476–2758ms、首个事件266–273ms、write/read≈50ms、pwsh≈630–690ms、额外确认0）；“改造前”对照待解锁，见 agent-v2-performance.md |
| 10 整体验证与交付 | 进行中 | 干净重建与契约新鲜度检查已通过；CI 已迁移；文档归档进行中；真实服务试用待完成 |

## 下一步（以本文件最后一节为准）

1. 官方原生协议的受控端点验收已通过；真实账号与预算确定后完成官方模型及搜索服务试用。
2. 通用预算、逐个子任务停止、文档生成/交付/预览、持久浏览器、常驻目标与调度恢复均已通过；补文件夹选择、文件编辑恢复、跨轮后台生命周期。
3. 校园迁移与调度已落地；继续旧代码退役、按需工具发现、桌面全宽视觉回归、性能基准、CI 迁移与干净重建。不要只加工具schema就宣称可用。

本轮全部验证句柄均已正常结束：安装80250、API48862、构建10833、前端54034、启动66019、实际Harness50902。旧安装36819和61018按诊断主动停止，终态exit1，已用修复后的安装成功取代，不再重复执行它们。

测试临时目录落在 apps/api/pytest-of-mingxiang，提交后立即审计发现；已添加全局排除并从尚未推送的本轮提交移除，原始测试文件仍在本地。后续提交前同时检查临时目录与运行凭据扩展名，不能仅检查 var/data。

真实模型接入路径与总预算已通过异步问题请求用户选择，尚未收到答案，不运行付费验证；继续独立开发。

真实账号/模型尚未验证，受控模型不能代替。需要真实试用时请求本地配置与明确预算，独立开发继续。

本次新增验证句柄：网关57295 exit0（19项）、契约覆盖exit0（9项）、前端23316/38239 exit0（12+3项），MCP凭据集成exit0；浏览器服务83795/92323按停止文件正常exit0；完整集成61172因预期工具集合遗漏资源工具exit1，修复后56824 exit0。API83544 exit0（279项）；前端8058一次等待超时exit1，单文件80687及全套5886 exit0（4/131项）；最终TypeScript/Vite构建19189 exit0。所有服务已停止、临时浏览器已关闭。之后仅补标准MCP type/disabled导入兼容，并执行相关设置测试与类型检查。

## 2026-10-14 增量：原生协议、统一预算与子任务控制

- native_tool_leases 与领域调用缓存统一计数；通用/MCP/子任务调用预执行预留，同一 ID 重放不重复扣除，跨会话/已停止授权拒绝。实际集成 8 项通用调用计入预算。
- 官方 provider 使用实际 Harness Messages/SSE 流式协议完成写入→读取→回答，不走 Python 模型转发；401 错误正文含合成密钥时，应用事件与上游 JSONL 均不泄露。错误分类、重试时长、图片 offload 恢复参数保留。最新证据 var/v2-official-protocol-XA36KO/evidence.json；没有真实付费服务验收。
- 通过薄 loopback 控制适配器调用上游公开 agents/subagents API。前台 one-shot 与 continuable 子任务均实际停止 A、保持 B 和父任务继续、B 读取真实文件并完成；控制接口需本轮授权，接受停止不等于已经停止。证据 var/v2-subagent-controls-Q09zUX/evidence.json。当前按轮次关闭 SDK，跨轮持久后台运行仍未完成。
- 完整 Python/API/SDK 集成 51 次受控模型调用通过，含所有原校园回归及真实选择性子任务停止、重复停止幂等。证据 var/agent-integration-kevzgkp4/evidence.json；通用工具及 MCP 独立重验分别在 var/v2-capabilities-tTz6Z3 与 var/v2-mcp-njAfsy。模型及搜索响应受控，文件/命令/MCP/公开网页访问是真实操作。
- 浏览器实际点击“停止 独立计算 A”，收到 A aborted，B 正常读取 v2-browser-731 并完成；刷新保留任务名称与状态，取消结果显示“有操作已停止”。证据 var/agent-integration-_a0zrz3h/browser-child-evidence.json，截图 child-controls-desktop.jpg。初次人工验收因受控模型等待超时失败，定位后改为等待实际停止事件，重新执行通过，未改产品超时或成功断言。
- 相关 API 网关/契约 30 项、前端会话/投影 15 项通过；TypeScript/Vite 构建成功。没有将之前全套 279/131 项记录当作本次全套运行。契约 CR012，导出当前 18 路径/16 schema。
- 所有本轮已知验证进程和浏览器服务均已结束；没有继续收费或后台测试进程。

Goal 保持实施中：浏览器、文件工作区、持续任务/调度、校园迁移、旧代码退役、整体 UI 与性能及真实服务试用仍需完成。

## 2026-10-14 增量：对话附件与统一文件成果

- 会话文件 API 支持实际 multipart 多文件上传（前端逐个传输）、列表、文本预览与原始下载；附件 ID 归属当前会话并随轮次持久化，只发送本轮选中的文件，空列表兼容旧幂等请求。默认单文件100 MiB、每条消息32个附件；文本预览512 KiB并明确截断。
- 新增 publish_file 工具与 file.published 事件，复制已有文件形成独立成果副本，同调用重放保持原副本，路径变化拒绝。源文件后续修改不改变交付副本，缺失/不可读文件如实失败。工具纳入通用预算；默认实际41项，示例MCP后45项。
- useConversationFiles 负责传输/选择/刷新，FileWorkspace 与 FileActivities 负责预览/成果展示；聊天直接打开成果与下载，历史消息显示选中附件名称，工作区可重新选择文件，草稿和选择刷新恢复。未知生成工具通过统一文件事件展示，不需要修改聊天业务控制器。
- 实际完整 API/SDK 合成验收55次模型调用通过，新增 multipart 上传→真实read→真实write生成Markdown→publish_file→下载内容含上传来源。证据 var/agent-integration-0iztn5pk/evidence.json。初次SDK验收因新工具缺少上游强制输出schema启动失败；按公开契约补齐后重验通过，未删成功/失败断言。
- 真实浏览器文件选择器上传 browser-upload.md，刷新保留选中附件与草稿，发送后3个实际工具全部成功；聊天成果卡片刷新恢复，可直接打开预览及真实下载，下载 SHA256 匹配成果副本。桌面1280×900与窄屏390×844预览通过，聊天 documentWidth375≤viewport390，HTML来源显示为文本。证据 var/agent-integration-zey6chn_/browser-file-evidence.json，截图 file-workspace-desktop.png/file-workspace-narrow.png。
- 浏览器下载的合成文件已另存验收目录；跨盘rename返回WinError5，复制/哈希验证成功，原Downloads副本删除也因权限被拒绝，保留 C:/Users/mingxiang/Downloads/附件整理成果.md。未改变目录权限或删除其他文件。浏览器tab及自有服务9784正常结束，扫描未见自有runner残留。
- API整套284项通过（51.75秒），新增归属/幂等、实际下载、源文件修改后副本保留、上传上限及HTML原文预览测试。原生官方协议独立重验通过，证据 var/v2-official-protocol-OK2qst/evidence.json。前端21文件136项通过（maxWorkers=2，87.37秒），最终TypeScript/Vite生产构建成功（1.67秒）。契约CR013，从实际API导出22条路径/18个schema。
- Markdown真实最小闭环已完成；PDF/图片预览代码已接入但专门视觉验收尚缺，PDF/表格实际生成、文件夹选择、编辑历史与回滚仍未完成。模型是受控响应，真实官方服务仍未试用。

## 2026-10-14 增量：文档生成、格式预览与生产运行修复

- 新增 document-artifacts Skill、输入格式说明和可独立运行脚本；项目 Python 环境固定安装 reportlab5.0.1、openpyxl3.1.5、Pillow12.3.0、et-xmlfile2.0.0，官方 wheel/哈希锁增至50项，pip check通过。产品运行不依赖 Codex 内置文档运行库；后者仅用于开发视觉检查。技能格式验证通过。
- 脚本从已核实的结构化内容生成中文 PDF、Excel 和 Markdown；PDF嵌入字体、自动分页及重复表头，缺字符明确报错。Excel保留数字/零/空值/日期、明确公式及原始“=”文本；不宣称 openpyxl 已计算公式缓存。同名输出拒绝覆盖，交付仍沿用 publish_file。
- 完整 API/SDK 合成验收64次模型调用通过，证据 var/agent-integration-7tu0pepb/evidence.json。新增真实加载Skill、读取说明、读取上传CSV、写JSON、PowerShell运行Python、逐一交付三份产物共8项成功工具；实际下载PDF正文保留原始记录-619、Excel为数字0/12。最初SDK启动因 shellEnv 不接受 PYTHONUTF8 名称失败，改用公开 DSH_* 变量与 Python -X utf8 后重验；未修改上游校验。
- 100条额外记录的PDF生成5页，Poppler渲染逐页检查，表头重复、最后记录99及来源正常，无重叠/截断。证据 var/v2-document-visual-check/分页资料.pdf 与 pagination-page-1..5.png。实际Excel经独立Artifact Tool导入、检查并渲染，最终图 var/v2-document-render/workbook-final-bordered-preview.png；列间结构线避免数值与下一列混读。
- 通用文件预览新增Excel工作表/表格，显示真实类型和公式原式，截断明确标记；坏文件返回 FILE_PREVIEW_FAILED，原件仍可下载。preview声明正式按kind区分的Pydantic响应，原文本/图片/PDF/下载保留，严格封闭旧kind枚举的客户端需升级。契约CR014，实际导出22路径/24schema。
- 浏览器实际上传CSV→Harness生成三份文件→PDF与Excel下载，SHA256均匹配交付副本。查看Excel的零、未知截止与2026-10-20；上传PNG正常预览；五页PDF可切到第5页并放大。证据 var/agent-integration-pbg09978/browser-document-evidence.json 与 browser-download-evidence.json。模型仍受控，无真实付费或学校账号试用。
- 原生iframe在当前IAB显示空白，改为应用PDF.js6.4.299 canvas/worker渲染，支持翻页/放大，关闭与换页销毁/取消自有加载任务；代码按需加载，普通聊天不加载PDF模块和worker。预览移到工作区首位并滚动到内容，避免先翻长文件列表。PDF/图片/Excel实际预览与390×844窄屏通过（documentWidth390≤viewport390）；当前IAB完整1280截图未覆盖全原生表面，仅验证桌面DOM尺寸，最终桌面全宽视觉回归保持未完成。
- 实际生产预览最初白屏：旧UI vendor跨块初始化出现 TypeError:nt is not a function。将Ant Design/rc/style相关依赖归到同一ui-vendor，保留React和PDF独立块；实际生产网页恢复会话及三份成果，生产PDF模块/worker真实绘制中文页面。证据 production-pdf-preview.png；UI块约1.12MB/355KiB gzip，构建仍提示大块，性能基准尚待完成，不以隐藏提示替代验证。
- 文件/文档/契约相关API16项通过（7.13秒）；前端23文件140项通过（maxWorkers=2，84.81秒），PDF页渲染取消/文档销毁与失败提示、工作表切换/公式/截断有覆盖。此前测试夹具重定义不可配置ResizeObserver、测试参数属性不满足erasableSyntaxOnly分别失败，按真实测试环境/TS配置修复后通过。没有宣称本增量重跑API全套284项。最终TypeScript/Vite构建通过；npm生产依赖audit零漏洞。
- 自有浏览器tab已关闭，开发验收服务94812与生产服务44650停止文件触发后均exit0。Downloads中的两份合成文档已复制到验收目录并验证哈希，原副本保留；不更改下载目录权限。

Goal继续实施：文件夹/文件恢复、持久浏览器、后台目标与调度恢复、校园迁移/旧代码退役、换布局与性能、干净交付及真实服务试用仍未全部满足。

## 2026-10-14 增量：应用持久浏览器与一次确认

- 固定 @playwright/mcp0.0.83、Playwright1.64.0-alpha-1790635538000 与 MCP server2.2.0。通过公共 createConnection/contextGetter 和 InMemoryTransport/Client 复用上游工具；API自有Node进程持有独立Edge配置，不依赖Codex浏览器，也不随SDK轮次退出。内置 campus_browser stdio薄适配复用现有按轮授权网关。
- 默认实际70项工具，示例计算MCP后71项：原41项+浏览器26项+MCP资源3项；仍非按需工具发现。保留名称避免配置覆盖，WindowsAPPDATA/LOCALAPPDATA/ProgramFiles及适用代理环境传递。最初Python/API实际浏览器调用因缺ProgramFiles误查用户Edge路径失败；增加真实安装路径检测后成功。浏览器错误最初使用ApiError不支持的位置参数，修为retryable关键字后正常错误信封。
- 标准表单点击、输入提交和回车先生成具体预览，用户确认前重查目标与字段/私有状态；重复确认返回原回执，变化拒绝，未知结果不补发。普通GET搜索自动执行，不因提交型交互而增加读取确认。非标准网页按钮通过明确预览工具采集实际可编辑字段并确认；任意脚本仍遵守交互合同，未冒称全面沙箱。
- 独立真实Edge/MCP13项检查通过，证据 var/v2-browser-Nrk0Ac/evidence.json：授权边界、无确认GET搜索、表单/回车/网页按钮、密码/字段变化拒绝、重复确认、Cookie/网页存储重启保留、实际附件字节、指定轮次导航停止、认证存储与快照文件凭据隐藏。上游快照原本只返回文件链接且包含密码框值；现在附带实际页面内容，并处理模型可见输出文件中的已观察凭据。
- 实际完整API/Harness合成验收70次模型调用通过，证据 var/agent-integration-svde00pc/evidence.json，模型可见71项。四项浏览器工具真实成功，下载自动复制到统一成果并发file.published，表单仅在应用确认后提交，同记录重放仍仅一次；下一轮可信上下文准确得到executed回执。不让已执行动作因为原模型历史仍是needs_user而被重复提交。
- useBrowserWorkspace管理请求/动作状态，BrowserWorkspace负责页面信息、窗口打开/停止、预览和回执。实际UI var/agent-integration-b1ouspmg/browser-ui-evidence.json：刷新恢复待确认操作，显示准确目标与姓名，一次点击产生实际页面回执，可见窗口打开/停止，附件预览及下载字节一致，390×844内容宽390。截图 browser-confirmation-narrow.png/browser-receipt-narrow.png/browser-download-preview-narrow.png。真实UI发现button.formAction无覆盖属性也返回当前页面，已改为检查属性后读取实际form.action并增加目标地址断言；旧预览拒绝后重验通过。
- 动作记录原子保存；进程恢复时executing变uncertain，公开状态不包含私有调用/指纹/下载路径。无需启动浏览器即可读已保存回执，下一轮只注入最近8项摘要；记录故障不会阻止独立文件等任务。可见窗口切换会重启上下文，登录配置保留，表单草稿不保证保留并须重新检查。
- API全套290项通过（50.67秒）；随后补恢复/错误回归，网关+浏览器24项通过（15.05秒）。前端24文件141项通过（maxWorkers=2，92.18秒），最终TypeScript/Vite构建通过（0.90秒，UI大块提示保留）。没有将新增测试后的全套数量当作再次全套运行。一次相关命令误用不存在的test_contract_coverage.py，未执行测试，按实际路径重新运行相关用例。
- 契约CR015导出27路径/34schema，工具说明与浏览器设计文档更新。所有已知自有验证服务停止：10478、79920按停止文件正常exit0，Cua标签关闭；后续SDK验证句柄92808已missing，检查权威证据中的70调用及完整断言字段、当前无相关Python/browser-host进程。Downloads中本次合成browser-campus.txt复制到证据目录，原件保留，未改权限。

当前并未完成整轮Goal。普通导航的真实停止已经通过；Root取消仍立即写cancelled再异步收尾，任意浏览器脚本/后台命令的物理停止、跨轮目标/子任务与调度需继续审计实现。真实校方登录、真实官方模型/搜索、文件目录/恢复、校园迁移退役、换布局/完整桌面视觉/性能/干净安装仍未完成。真实服务选择与预算的问题仍待用户回答，不运行付费试用。

## 2026-10-14 增量：实际停止、Windows进程归属与UI状态

- 修复前实际复现：var/v2-browser-stop-townctm_/evidence.json，约0.343秒返回BROWSER_STOPPED，之后延迟JavaScript仍发真实请求。保留browser_run_code_unsafe能力，停止含脚本的浏览器时结束自有进程组。
- ProcessOwner使用Windows公共Job Object API，仅KILL_ON_JOB_CLOSE，不新增文件/网络/CPU/内存限制；提供任务输入前绑定归属。根进程退出的监听清理残留子进程，终止查询active归零，回收句柄。实际detached A子进程未再写标记，独立B正常写出结果，证据 var/v2-owned-process-o2_s_igs/evidence.json。
- 延迟浏览器脚本收尾约0.344秒、CPU阻塞约2.063秒，真实进程归零，无延迟操作；证据分别 var/v2-browser-stop-qc445dvm 与 var/v2-browser-stop-paap5jde。控制帧签名，普通/无签名stdout不改变归属；控制请求无法响应时按自有活动强制收尾，异常重启清理旧句柄。源码里的Web/脚本功能保持开放。
- Turn增加stopping，立即撤销授权，SDK和浏览器并行收尾并等待原API调用锁；物理结束才转cancelled/failed，迟到结果不得覆盖。停止失败保留状态并可重试；重启未收尾轮次标interrupted。前端轮询/事件投影包括stopping，未执行完的工具不提前标中断，取消旧查询避免覆盖新响应。
- 实际完整API/Harness71次受控模型调用通过，证据 var/agent-integration-wc8y93uf/evidence.json；新增真正开始浏览器脚本后取消，stopping拒新轮次、无延迟操作、收尾后恢复会话。合成HTTP夹具在关闭真实连接时有10054复位诊断，但全部执行断言及最终exit0通过；不把诊断改为假成功。
- 真实UI两次点击停止浏览器脚本，观察“停止中”及禁用按钮，最后“已停止”及刷新保留；文件 var/agent-integration-1023p1_9/root-stop-ui-evidence.json、root-stopped-narrow.png，宽375≤390；服务最后stop_fixture.started=true/late=false。首次15秒夹具在人工观察前自然完成，核实终态后仅把UI夹具改为90秒并重验，未放宽产品超时。过渡截图绘制滞后于DOM，不用它证明视觉过渡。
- API全套294通过（55.61秒）；前端25文件142通过（maxWorkers=2，94.77秒），最终TypeScript/Vite通过（0.998秒，UI大块提示仍保留）。此前新测试因loading图标改变按钮可访问名称失败，补稳定aria-label后相关18项通过，再跑全套。CR016实际导出27路径/36schema，停止生命周期设计文档更新。
- 自有UI标签已关闭；32897/69055停止文件触发后exit0，独立进程与CPU验证均结束。数据、凭据、依赖及证据目录未提交。当前仍只有本地变更/提交，不推远端。

完整Goal继续实施。root与普通受管理进程的代表性停止已验证，不能据此宣布跨轮子Agent、子Agent浏览器隔离、持久目标/调度完成；共享浏览器与其他轮次竞争时仍等待资源。校园迁移/旧实现退役、目录/编辑恢复、按需发现、整体UI/性能与干净安装、真实官方服务/学校试用尚未全部满足。

## 2026-10-14 增量：三路成果整合、契约补录与实际运行验收

- 整合上一轮三个并行任务（UI 收敛、校园迁移、本机调度）与持久目标生命周期，形成本地提交 a5f4928（125 文件：54 新增、11 重命名、58 修改、2 删除），未推送。
- 修复集成缺口：`app/__init__.py` 在包初始化时调用 `config.ensure_repo_root_on_path()`，使 `connectors.campus.*` 在 pytest 与生产同路径解析（此前 `tests/test_campus_migration.py` 收集即 ImportError）。修复前实际失败命令与报错已记录，不以“模块能导入”代替。
- 修复集成缺口：校园工作区路由从 lifespan 内挂载改为应用装配期挂载。lifespan 内挂载导致运行时与 `app.openapi()`/契约导出看到不同路由表，契约回归实际报出 17 条“实现多出但契约未定义”，其中 11 条正是被误判的正常路由。改装配期后同一份路由表。
- 契约按既有 CR 流程重新导出：`tools/export_agent_contract.py` → 42 条 Agent 路径 / 49 个 schema（原 27/36）。`apps/api/tests/test_agent_plan_actions.py` 的写效果工具集合断言补入 `campus.sync`、`campus.notice.import`；授权边界断言（模型不能自带批准）保持不变，未放宽超时或删断言。
- 实际运行验证（非文档结论）：API 全套 **320 passed（66.23 秒）**；前端 **27 文件 149 passed（maxWorkers=2，111.69 秒）**；`tsc -b` 退出 0；Vite 生产构建成功（981 毫秒，UI vendor 大块提示保留）；真实端到端调度 `tools/verify_schedule_runtime.py` **exit 0，6 次受控模型调用**，证据 var/v2-schedule-runtime-pd3vr0ea/evidence.json，覆盖自然语言建任务→真实后台线程触发第二轮→真实 SDK write/read/publish_file→下载字节核对→两个独立 API 进程前后同一发生/同一 turn、重启无新增模型调用。
- 真实 Harness 原生目标驱动：`node apps/agent-runtime/verify-goal.mjs complete` **exit 0**，证据 var/v2-goal-6ZKfdf/evidence.json；`create_goal`→原生 round driver→`update_goal complete`，run 返回时目标仍 active、随后 phase=complete、activation=disarmed、agent idle。模型受控，不等于真实 DeepSeek 服务验收。
- 完整真实 API/Harness 集成回归：`tools/verify_agent_runtime.py` **PY_EXIT=0，77 次受控模型调用**，证据 var/agent-integration-o_omw2us/evidence.json；全部检查组为 true——计划应用/撤销与持久回放、后台任务构建与取消、学习反馈不改学校状态、通用文件/Python/本地MCP、多文件上传/读写/交付下载、PDF+XLSX+Markdown 三产物、双子任务选择性停止、原生长任务轮次、浏览器MCP四项工具/下载交付/表单一次确认防重。可见工具 84 项。首次运行经 `2>&1 | Select-Object` 管道时 pwsh 报 exit 1，经重跑直接捕获真实退出码确认 **PY_EXIT=0**、stderr 为空，判定为 PowerShell 包装层伪影而非产品失败（已按实际退出码复核）。
- 启动链路复核：`tools/dev.py --check` 通过；`tools/dev.py --smoke-test --platforms` 真实起 API（含全部迁移）与 Vite 并检查网页代理后自动关闭，exit 0。
- 依赖按需发现的现状核查：固定 0.2.0-rc.2 上游的按需工具形态是 agent preset 的 `tool-presentation`（native / `run_code` PTC / both），PTC 需要 host 侧 `dsh-ptc-runtime-node` 与沙箱；`dsh-tool-cordis` 提供运行时 API 检视。本轮只做核查与选型，未实现，继续列为条件 3 缺口。
- 清理 `.gitignore` 补充 `.tmp-install/` 与 `tmp*/`；提交前审计未纳入数据库、凭据、依赖、证据目录与测试临时目录。

下一步：实现按需工具发现、补桌面全宽视觉回归与性能基准、完成干净重建与真实服务试用。

## 2026-10-14 增量：旧应用整体退役，回归改挂迁移实现

- 校园协议实现单份化到 `connectors/campus`，并新增 `connectors/campus/campus_store.py`（缓存 schema + `SQLiteStore`）：此前 `campus_snapshots`/`connector_cache` 的建表只在 `apps/api` 的 `CacheStore` 里，迁移后的连接器包自己需要持久化契约；现在 `CacheStore` 组合 `CAMPUS_SCHEMA + IMPORT_SCHEMA`，连接器回归测试不再需要旧 `Store`。
- 旧 `campus_agent` 包整体删除（`git rm` 47 个路径）：`web.py`/`server.py`/`__main__.py`/`campus_routes.py`/`passport_routes.py`（旧本地账号 + CSRF 服务器）、`static/`（10 个旧界面文件）、`llm.py`（第二套模型转发）、`notice_analysis.py`/`notice_contract.py`/`notice_model.py`/`parser_worker.py`/`services.py`（旧抽取管线）、`storage.py`、`campus_sync.py`、`campus_crypto.py`、`core.py`，以及此前的兼容别名模块与 `connectors/` 别名。
- **保留有效断言而不是删测试**：把原先必须经由已退役 HTTP 层才能断言的 27 项 `CampusWebTests` 与 7 项 `ProfileSyncTests` 改写成直接驱动迁移后的 `CampusManager`（`prepare/login/sync/status/academic_view/calendar/disconnect/adopt_passport/connect_and_sync`）。仅两类断言随实现删除并已在台账记录理由：旧多账号注册/CSRF/consent 语义（V2 是单人本机身份），以及旧静态页与旧 `/api/documents` 路由断言（其端到端覆盖已由 `apps/api/tests/test_campus_migration.py::test_notice_to_material_read_and_real_task_engine_is_versioned` 承接）。测试数由迁移后的 162+72 subtests 提升到 **191 passed + 80 subtests**。
- 移植过程中实际发现并修复一个产品缺陷：`CampusManager.academic_view`/`agenda` 期望 ISO **字符串** 日期，而 `apps/api/app/routers/campus.py` 与 `apps/api/app/agent_bridge/tools.py` 都传入 `date` 对象，真实调用会 `TypeError` 变成 500。两处改为先校验再传原字符串。
- 历史导入测试自带冻结 schema：`apps/api/tests/test_campus_migration.py` 不再 `from campus_agent.storage import Store`，改为在测试内声明首轮历史表结构（与被测模块“不实例化旧 Store”的立场一致），9 项导入/回滚/隔离测试保持通过。
- CI 重写：`.github/workflows/tests.yml` 现在跑 API 套件（隔离数据库/文件/凭据/Harness home）、迁移后根回归、契约新鲜度检查与前端构建+测试，不再引用已删除的 `campus_agent/static/*.js` 或旧 unittest 入口。`tools/export_agent_contract.py` 新增 `--check`（只比较不写入）。
- 文档：`docs/local-setup.md`（整篇是旧服务器启动指引）归档到 `docs/history/first-round-legacy/` 并修正相对链接；`validation.md`、`notice-extraction.md`、`college-notices.md`、`connectors.md` 加迁移横幅；`docs/agent-v2-migration.md` 台账改为逐项“已删除/已单份化/已迁移”并注明未完成项。
- 退役后实际验证：根回归 **191 passed + 80 subtests（24.95 秒）**；API 全套 **320 passed（60.96 秒）**；`tsc -b` 退出 0、前端 **149 passed**、Vite 生产构建 exit 0；`tools/dev.py --check` 与 `--smoke-test --platforms`（真实起 API+Vite 并检查网页代理）**exit 0**；`tools/migrate_campus_data.py inventory --source data/campus.sqlite3` 在无 `campus_agent` 环境下仍真实只读运行（`source_read_only: true`、`unmapped: []`、凭据/会话/审计表列在 excluded）；CI YAML 解析通过。全树已无对退役模块的引用（仅历史文档提及）。

剩余：按需工具发现、桌面全宽视觉回归、性能基准“改造前”对照、真实旧库导入与真实校园/模型试用、干净重建。

## 2026-10-14 增量：本地性能基准（条件 9 起步）

- 新增 `tools/benchmark_local_overhead.py`：真实 uvicorn + 真实 `apps/agent-runtime` SDK，模型提供方是本机受控 HTTP 夹具，因此**远程模型波动按构造排除**。测量启动（进程→health 200）、首个可见事件、轮次完成、每轮模型请求次数、每工具耗时（由真实 `tool/call`→`tool/result` 事件时间差算出）、待确认动作数，并断言落盘字节与预期一致。支持 `CAMPUS_BENCH_ROOT` 指向其它检出以便跨版本对照。
- 实际两次独立运行（各 2 轮真实本地任务 write→read→pwsh Python 计算）：启动 **2758ms / 2476ms**，首个可见事件 **273.5ms / 266ms**，轮次完成 6219ms / 5695.5ms，`write` p50 52.6/54.5ms，`read` 49.7/49.6ms，`pwsh` 690.4/628.0ms，每轮 6 次模型请求、3 次工具调用，**待确认动作 0**，落盘校验通过。差异在 ±12% 内且无单向恶化，**未出现可重复的明显回退**。证据 `var/v2-bench-_g6mxube/benchmark-baseline.json`、`var/v2-bench-bymalltp/benchmark-current.json`（本地）。
- 首次运行因同一 home 重复播种导致 provider id 唯一约束失败，改为幂等 upsert 后通过；不为跑通而放宽断言。
- 实测记录“改造前”对照当前不可得：首轮未留下可比时间数字，旧应用已退役；保留的首轮 workbench 虽带 venv 与 node_modules，但在该路径下 `tempfile.mkdtemp` 超过 120 秒不返回（进程持续占用 CPU、无输出），判定为该目录环境问题并已终止自有进程。最小解锁条件与复测命令写入 `docs/agent-v2-performance.md`。
- 新增文档 `docs/agent-v2-performance.md`（方法、两次数字、解释、未完成项与解锁条件）。
- 追加实测工具 schema 成本：受控模型**每次请求**收到 **83 个 schema、63406 字节**（第三次运行，`var/v2-bench-ajqrxmpv`）。这把条件 3 的“按需发现”缺口量化成可复测的数字，而不是主观判断；上游 `0.2.0-rc.2` 的按需形态是 agent preset `tool-presentation` 的 `ptc` 模式（原生 schema 不下发，改为生成 SDK + `run_code`），当前 profile 未挂该行，仍按 `native` 全量下发。

## 2026-10-14 增量：按需工具发现的分组掩码（已验证有效，尚未接线）

- 实现 `apps/agent-runtime/campus-tool-groups.mjs`：按前缀把 36 项实测可延后工具分为 browser(26)/mcp(3)/team(5)/jobs(4)/planning(1)；黑名单式判定，未分类工具保持可用（失败开放）；`load_tools` 加载工具带强制 `output.schema`，调用后返回实际可见工具名。
- 新增 `tools/verify_tool_discovery.py`：真实 API/Harness/工具执行，受控模型直接在**线缆上**断言默认请求缺少延后分组、加载后出现、其余仍隐藏；未接线时以 exit 2 + 明确原因报告（实测输出该诊断），不伪装成功。
- 实测：preset 作用域的 `restrict` 对会话 agent 请求无效（84项/64644B）；改到 agent 作用域 `setup` 后同一受控任务降到 **45 项 / 33887 B（−47%）**，3 个工具调用全部成功、额外确认 0（`var/v2-bench-obj8a_o_`、`var/v2-bench-dlaoug8l`）。
- 实测发现并回退：包装 SDK `agents.create/resume` 注入 `setup` 会破坏工具派发（每次调用只写 `tool/call` 后在 `tools/pre-execute` 前停滞，API 无 `tool-lease` 请求；连普通 `pwsh` 同样复现，关闭特性后仍复现=包装本身问题）。已回退 `campus-sdk.mjs` 与 `campus-plugin.mjs`，**运行时保持已验收原状**（探针任务实测 83 项、`pwsh` 正常）。
- 因此条件 3 的按需发现记为“掩码验证有效、接线未完成”，下一增量的问题与候选安装点写入 `docs/agent-v2-performance.md`。

## 2026-10-14 增量：安装点全面排除与 runner 诊断能力

- 按候选逐项以真实运行排查按需发现的安装点，四类全部证伪：`agent/created` 内同步安装（创建永不完成）、`agent/created` + `setTimeout(0)` 延后安装、仅向 `agent.ctx` 注册加载工具（掩码关闭）、以及“插件作用域注册加载工具 + 每 agent 仅 `restrict`”的分体设计。据此再次回退接线，**运行时保持已验收原状**：实测 83 项 / 63406 B、3 个工具调用成功、额外确认 0、API 全套 **320 passed**（一次全量运行出现 1 项真实线程计时用例失败，单文件复验 15 passed、全量复验 320 passed，判定为负载下的计时抖动而非回归——已实证而非假定）。
- 保留分体设计模块（`registerToolLoader` / `installToolMask`），未接线即无副作用；`tools/verify_tool_discovery.py` 仍以 exit 2 + 明确原因报告未接线状态。
- 新增诊断能力（本轮保留且已验证）：runner stderr 原先固定 `DEVNULL`，插件级死锁完全无日志。现支持 `CAMPUS_RUNNER_STDERR=<path>` 显式捕获（默认仍丢弃，避免运输细节/模型输入进入日志），句柄按轮次持有并在回合结束或提前返回时关闭，追加写入不截断。新增单元测试覆盖默认丢弃、开启捕获、追加模式与关闭；网关 24 项通过；真实产品路径实测文件被创建（健康运行为空）。
- 结论写入文档：问题不在“装在哪个钩子”，而在该组合下 agent 作用域的工具注册/限制本身阻塞创建；下一步先做最小复现（agent 作用域只注册一个空工具、不做 restrict），否则退回上游 PTC 呈现模式。

## 2026-10-14 增量：干净重建通过，修出三个真实安装缺陷

- 用 `git archive HEAD` 生成**仅含跟踪文件**的全新目录（679 个，无 .git/.venv/node_modules/var/data），并用**另一个项目的 venv 解释器**驱动安装（真实用户从 wrapper/别的环境调用时的场景）。结果：`tools/dev.py --setup` **exit 0**（venv + 53 个锁定 wheel 含 --require-hashes + `packages/learning` editable + pip check 无缺失；网页 180 包、运行时 540 包，均 0 漏洞）→ `tools/dev.py --check` **exit 0** → `npx vite build` **exit 0** → `tools/verify_agent_runtime.py` **exit 0，77 次受控模型调用，ok=true**。证据 `docs/agent-v2-clean-rebuild.json` 与 `var/agent-integration-cleanrebuild/evidence.json`（已从临时重建目录复制到常规证据目录）。
- 该重建**修出并验证了三个真实缺陷**（都不是文档层面，而是实际阻断全新安装）：
  1. `dev.py` 用 `sys.executable` 建 venv：当启动者是别的项目 venv 时不可移植（WinError 5）。改为 `base_python()` 定位基础解释器。
  2. `python -m venv` 在刚解压的目录里可能**已建好可用环境却返回非零**（实测 `Permission denied: pyvenv.cfg`，三个组件都在），且该现象在 dev.py 之外同样复现（杀软实时扫描导致的临时占用）。新增 `ensure_project_venv()`：按“解释器可用性”判定而非退出码，有限重试，失败时给出明确原因。
  3. 启动器把调用者的 npm 环境原样透传，外层 `npm exec --allow-scripts` 注入的 `NPM_CONFIG_ALLOW_SCRIPTS` 让 `npm ci` 直接失败（`EALLOWSCRIPTS ... not allowed in project-scoped installs`）。新增 `npm_environment()` 按**大小写不敏感**移除 npm 自身的生命周期/调用标记（Windows 的 `os.environ` 键是大写，第一版按小写移除无效，实测发现后修正），保留 registry/cache 等真实配置。
- 新增 6 项启动器测试（base_python、可用性判定、重试上限、缺解释器时不崩溃、npm 环境大小写、真实进程环境），`tools/test_launcher.py` **12 passed**；其中大小写陷阱由新增的“对照真实进程环境”用例守住。
- 诚实记录未验证项：npm 对 5 个包（koffi/node-pty/dsh-subprocess-local 等）报 install scripts 未被 allowScripts 覆盖，但 `npm ci` 仍 exit 0 且该目录下真实 Harness 集成 77 次调用全部通过，故按“已观察但未影响本次验证”记录，不宣称已解决；真实官方模型/搜索与浙大登录态试用仍需用户解锁。
- 另新增 `docs/agent-design/codex-handoff-v2.md`：可直接交给 Codex 的收尾提示词（含硬约束、未完成项与已证伪路径、环境与测试规范、分工建议），用于与更强 agent 协同完成剩余验收。

## 2026-10-14 增量：桌面全宽视觉回归（条件 8 补完）

- 新增 `tools/verify_ui_desktop.py`：启动真实 API（alembic + bootstrap + uvicorn）与真实 Vite，用**应用自有持久 Edge 配置**（`channel=msedge` + 持久 profile 目录，与 `browser-host.mjs` 同布局）在 **1280×900 与 1920×1080** 记录可测量断言与截图，并验证刷新后草稿保留。脚本不接触模型（入口页/工具栏/日历工作区都不需要 provider），因此不受账号与预算阻塞。
- 实测 **ok: true、无失败项**：两个尺寸均 `scrollWidth == innerWidth`（无横向溢出）；输入框可见且完全在视口内（1280：183–1097，底831<900；1920：503–1417，底1011<1080）；工具栏可见；点「日历」打开右侧工作区后聊天与输入框仍可用；刷新后草稿保留。证据 `var/v2-ui-desktop-*/ui-desktop-evidence.json` 与 4 张截图。
- **人工查看截图**确认布局正确（标题栏、工具栏四入口、历史/课程选择、空态建议、模型提示与输入框在位不截断；工作区为右侧面板且不遮挡输入框），未仅凭 DOM 数字下结论。如实记录取舍：headless 真实 Edge；1920 截图可能保留前一次点击的焦点环（截图时序，非界面缺陷）。
- 环境问题与既有约定一致：Playwright 默认使用继承的 Windows 临时目录会 `EPERM: mkdtemp`（该目录在工作区之外），按项目既有做法改为每会话自有临时目录（`TEMP/TMP/TMPDIR` 指向会话内）后正常。
- 本轮未触碰 Codex 领取的任务 A 文件（`campus-plugin.mjs`/`campus-tool-groups.mjs`/`runtime.py`/`verify_tool_discovery.py`）；写范围为 `apps/web` 相邻的验证工具与文档。

## 2026-10-14 增量：Codex 接通按需工具发现（任务 A）

- 最小真实探针推翻“agent 作用域注册必死锁”的旧推断；修正 schemas(agent) 的作用域与 tools/change 自激更新。只使用上游公开 registry/scope，不修改 SDK 或上游源码。
- 默认 45 项 / 33861 B（原 83 / 63406 B，字节减少 46.6%）；仅加载浏览器后 71 项，其余组仍隐藏。真实 Edge 页面读取成功，verify_tool_discovery.py exit 0。
- 修复验收脚本原有不成立的隐藏集合断言与随机首工具选择；广覆盖受控模型先实际调用 load_tools，仍验证完整工具面、MCP、子任务、取消、文件与浏览器一次确认防重。
- 完整集成首次在第 25 轮暴露 200 条消息硬限；按 CR017 改按已有内容预算限制，240 条短消息与超预算拒绝测试通过。完整集成复跑 exit 0，103 次受控模型调用，证据 var/agent-integration-bvufvxs6/evidence.json。
- 最终隔离 API 322 passed；根回归+启动器 203 passed + 80 subtests；前端 149 passed、tsc/Vite 通过；真实调度 6 次调用、原生目标 complete、契约 --check、启动器 --check 均通过。完整命令、路径与作用域限制见 agent-v2-tool-discovery.md。
- 协作：只提交 Codex 文件。实现者继续负责桌面、性能与 CI/归档；建议 CI 增加 node apps/agent-runtime/verify-tool-groups.mjs。用户已选择有数据 owner 进行任务 E，已授权真实模型与搜索总预算 100 元；尚未完成学校登录。真实付费请求截至此记录未发出。

条件 3 的按需发现缺口已验证；整个合同仍未完成，真实数据迁移、官方模型/搜索与学校登录态需继续。

## 2026-10-14 增量：验收 Codex 遗产（它额度耗尽退出），并补齐条件 9 的前后对照

Codex 在完成任务 A 后因额度耗尽退出，实现者成为唯一执行者。先**独立复跑它的全部验收**，不采信它的文字结论，
再接手它的文件范围（`apps/agent-runtime/**`、`apps/api/app/agent_bridge/runtime.py`）。

### 遗产验收结果（全部由实现者本轮真实复跑，HEAD=6690c6f）

| 验证项 | 命令 | 实测结果 |
| --- | --- | --- |
| API 套件 | `apps/api/.venv/Scripts/python.exe -m pytest -q`（隔离数据库/目录） | **322 passed** |
| 根回归 + 启动器 | `python -m pytest tests tools/test_launcher.py -q` | **203 passed + 80 subtests** |
| 工具分组（上游公开 registry/scope） | `node apps/agent-runtime/verify-tool-groups.mjs` | **PASS**（默认面/子任务加载/独立作用域隔离/晚注册/重连/重复加载/额外策略） |
| 按需发现（真实 Edge 导航） | `apps/api/.venv/Scripts/python.exe tools/verify_tool_discovery.py` | **exit 0**，3 次模型调用；默认隐藏延后组、加载后仅出现浏览器组、其余仍隐藏 |
| 受控模型完整集成 | `apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py` | **exit 0，103 次受控模型调用，ok=true** |
| 契约新鲜度 | `python tools/export_agent_contract.py --check` | **exit 0**，42 paths / 49 schemas |
| 启动器 | `python tools/dev.py --check` | **exit 0** |
| 前端 | `npx tsc -b` / `npx vitest run --maxWorkers=2` / `npx vite build` | tsc **exit 0**；**27 文件 149 passed**；build **exit 0**（大 chunk 提示为已知项） |

本轮另以独立基准复现了它的核心数字：默认请求 **45 项 / 33861 B**（改造前全量 83 项 / 63406 B），
且该请求下 `campus_connections_status` 真实执行、回执 `{"status":"ok"}`、额外确认 0。

### 条件 9：改造前后对照已补齐，并查出**一项可重复的明显回退**

- 受控任务改用 `campus_connections_status` + 回答（两侧都真实声明的工具）；原 `write→read→pwsh` 任务在首轮系统
  上**根本无法执行**——首轮模型可见面只有 16 个校园工具，没有任何本地文件/命令工具（实测该轮以 RUNTIME_FAILED 结束）。
  这是能力差异而非耗时差异，已如实记录；`local-files` 任务只在改造后一侧保留数字。
- 实测（同机、同夹具、同任务、同脚本）：启动 2961 → 2718.5 ms（持平）；首个可见事件 289 → 281.5 ms（持平）；
  **入队→首个模型请求 1781 → 5062.5 ms（+3.28 s，可重复）**；工具往返 141 → 203 ms；尾部 101.5 → 476.5 ms；
  每轮模型请求 2 → 2；额外确认 0 → 0；schema 16/10771 B → 45/33861 B。
- 定位：回退集中在“把这一轮送到模型面前”之前。`runtime.py` 每轮起一个全新 runner（注释明确：a fresh runner
  resumes the same JSONL session），而 profile 从 16 行手写树改成完整 `@deepseek-ai/dsh-base`（`dependencies` 93 项），
  并且每轮都要真起浏览器 MCP 子进程（`playwright` + `@playwright/mcp` 实测导入 539 ms，`failOnStartupError: true`）。
  两次受控实验证明该 MCP 是**硬启动依赖**：不生成 MCP 行 → runner 274 ms 内结束；让子进程立即退出且
  `failOnStartupError: false` → 238 ms 内结束（实验在一次性 worktree 内做，事后 `git checkout` 还原并确认干净）。
- 上游 SDK 文档明确 `DeepSeekHarness` **owns one runtime subprocess across many sessions**（子进程 lazy 启动、
  `close` 前一直由该实例持有），因此正确修法是**让 runtime 子进程跨轮复用**，而不是每轮重启。
- 详细数字、证据路径与建议修法见 `docs/agent-v2-performance.md`「改造前后对照」。条件 9 记为
  **对照已补齐、存在可重复明显回退、待修复后复测**，不记为完成。

### 其余状态

条件 1/2/6/8/10 已有本轮或前轮证据；条件 4/5 的真实官方模型、真实搜索与学校登录态仍在用户侧解锁范围内
（用户已授权真实模型/搜索预算；学校登录尚未完成）。下一步按优先级：修每轮 runner 启动回退 → 条件 10 的
CI/文档归档 → 条件 2 的真实旧库导入与回滚演练。

## 2026-10-14 增量：条件 4 的三条真实场景全部通过（真官方模型 + 真搜索 + 真校园数据）

新增 `tools/verify_real_scenarios.py`：走**真实产品路径**（真实 uvicorn API + 真实 `apps/agent-runtime` Harness +
本机已配置的官方 DeepSeek provider `prv_c68d2878`/`deepseek-flash`），把**真实旧库导入**（owner 显式选择、映射到
`local`、`school_cache_imported: true`、不导入凭据）作为前置，然后在隔离 home 内跑三条场景；每轮结束记录
`/internal/model` 调用数、工具调用名、计划行状态与最终回答。所有真实请求都在一次性 home 内发生，不写用户数据。

| 场景 | 断言 | 实测 | 真实模型请求 |
| --- | --- | --- | --- |
| s1 公开网页检索并引用 | 真实网页工具调用 + 回答含可核实 URL | `web_search` 1 次 + `web_fetch` 6 次；回答引用 5 个 URL，且这些 URL 也出现在工具返回里 | 10 |
| s2 校园信息结合计划修改并撤销 | 读取校园信息 + 计划动作可接受/生效/撤销 | 校园工具 18 次（含导入资料读取、课表/成绩/通知缓存、校历）；预览动作 → 接受(apply) 200 → `plans.apply` 执行，计划 `status=active` 且 `applied_at` 有值 → 接受(revoke) 200 → `plans.revoke` 执行，`status=revoked`、`revoked_at` 有值、`plans.read` 返回 `plan: null` | 11 |
| s3 附件读取并生成可打开产物 | 附件读取 + 产物存在且可读 | `skill`/`read`/`pwsh`/`write`/`read`/`publish_file`；产出 `workspace/todo-report.md`（550 字符，UTF-8 可读）并发布 | 6 |

合计 **27 次真实付费模型请求**（预算已由用户授权）。证据：`var/real-scenarios-s1|s2|s3/{evidence.json,transcript.json}`。

诚实记录两点限制，不作为已完成项糊过去：

1. **校园方案里没有时间块**：s2 的预览生成了 `blocks: []` 的 120 分钟容量预留，因为本机没有可安排的学习任务
   （`learning_tasks` 与本地 `courses` 都是空的，导入的 4 份文档不挂课程）。模型自己也明确说明需要课程挂载点与
   复习资料才能排块。**已生效→已撤销**的机制验证成立，但"有真实内容的计划修改"尚未验证。
2. **真实校园数据的读取依赖旧库导入**：三条场景里的校园事实来自 `data/campus.sqlite3` 的显式 owner 导入，
   不是刚同步的学校端点数据；教务连接器仍 `connected: false`，缓存标记 `stale`，模型在回答里如实说明。
   条件 5（真实学校登录态的只读试用）仍待用户在本机浏览器完成登录。

真实问答里包含用户的真实姓名/学号等个人信息，**只留在 `var/` 证据目录（不提交）**，本文档不复述。

## 2026-10-14 增量：条件 2 的真实导入/回滚演练，条件 6 的调度/目标/子任务复验（并修好一个失效验收脚本）

继续独立复验继承来的证据，并对真实旧库做了一次完整演练（不产生模型费用）。

**条件 2 —— 真实旧库导入 + 回滚（已执行，源库只读）**

- `inventory` 实测 3 个 owner，只有 `4e7ff483…` 有数据（documents 4 / chunks 61 / tasks 2 / campus_snapshots 1 /
  connector_cache 1 / notice_analyses 1）；显式 `--owner` 导入后映射到 `local`，`school_cache_imported: true`、
  `credentials_imported: false`（不迁移旧密码/会话）。
- 重入：同源同 owner 再导入 → `replayed: true`，无重复；回滚：`removed: true`，再回滚 `removed: false`（幂等）。
- 这次导入同时充当条件 4 场景 s2 的真实校园数据来源，模型真的读到了导入的课表/成绩/通知/校历。
- **修掉一个真实操作陷阱**：`--batch-id` 传给 `import` 会被静默忽略（批次 id 由源指纹+owner+school-cache 派生），
  之后用这个自造 id 回滚会得到 `removed: false`，看起来像回滚成功。现在 `import` 拒绝 `--batch-id`、
  `rollback` 拒绝非 `mig_<32hex>` 的 id（exit 2 且给出原因）；已实测两种误用都报错，正确流程不变。
- 证据 `var/migration-rehearsal-20261014/`；台账与结论见 `docs/agent-v2-migration.md`。

**条件 6 —— 调度 / 原生持续目标 / 子任务控制复验**

- `tools/verify_schedule_runtime.py`：**exit 0，6 次受控模型调用**，真实调度器 + 可控时间。
- `apps/agent-runtime/verify-goal.mjs`：**exit 0**，原生 goal 驱动到 `phase=complete` 并落盘。
- `apps/agent-runtime/verify-subagent-controls.mjs`：**发现它在 Codex 的按需工具发现落地后已经失效**——
  受控模型直接返回 `subagent` 调用，而 `team` 分组现在是延后加载的，于是永远不会创建子 agent，
  20 秒超时（隔离复跑两次都失败）。**这是验收脚本没跟上按需发现的真实缺口**，不是能力缺陷：
  按产品语义让受控模型先调 `load_tools({group:'team'})` 后，脚本 **exit 0**，前台与可继续后台两种子 agent
  生命周期、选择性中断（无凭据 403 / 有凭据 200、只中止 A、B 仍完成）全部通过。
  同类问题 Codex 已在广覆盖夹具里修过，但漏了这个专用脚本；本轮补齐。

## 2026-10-14 增量：修掉每轮 runner 启动回退（条件 9 复测通过）

按上一轮定位的成因动手改运行时（Codex 退出后其文件范围由实现者接管）：

- `apps/agent-runtime/runner.mjs`：由“读一行 payload → 跑一轮 → 退出”改为**按帧循环复用同一个 harness**；
  每轮输出帧带 `turn` 序号，进程退出前不再重装 profile。
- `apps/api/app/agent_bridge/runtime.py`：按会话保留 warm runner；签名（provider/model/budget/search/skills/MCP/workspace）
  变化、`goal_resume` 轮、取消/超时/失败、空闲 900 秒或超过 4 个进程时换/收进程；复用轮把该轮重新绑定到进程原有
  bridge token（新增 `store.bind_token`），控制地址随进程携带（`register_controls` 记住最近一份），
  `Runtime.close()` 收掉全部 warm 进程，迟到帧按 `turn` 丢弃以免串轮。

实测（同机同夹具同任务，2 次运行 × 每轮 2 次）：

| 指标中位数 | 首轮 workbench | 改造后未复用 | **复用后** |
| --- | --- | --- | --- |
| 入队 → 首个模型请求 | 1781 ms | 5062.5 ms | **2422 ms** |
| 入队 → 轮次结束 | 2023.5 ms | 5718.5 ms | **2734.5 ms** |
| 首个可见事件 / 尾部 | 289 / 101.5 ms | 281.5 / 476.5 ms | **179.5 / 117.5 ms** |
| 启动 → health 200 | 2961 ms | 2718.5 ms | 2476.5 ms |

逐轮：**轮 1（冷）4812/4672 ms，轮 2（复用）156/172 ms**——稳态轮约 **0.41 秒，比修复前快约 12 倍**，
比首轮系统约 2.0 秒也快约 5 倍。剩余的是**每会话一次**的冷启动（完整 93 依赖 bundle + 浏览器 MCP 子进程），
已按已知限制记录，不再是每轮成本。

正确性同步复验（全部 exit 0）：API **322 passed**；根回归+启动器 **203 passed + 80 subtests**；
`verify_agent_runtime.py` **ok=true，83 次受控调用**（比修复前 103 次更少，因为 `load_tools` 不再每轮重复）；
`verify_schedule_runtime.py`（6 次调用）、`verify-goal.mjs`、`verify-subagent-controls.mjs`、
`verify-tool-groups.mjs`、`verify_tool_discovery.py`、`verify_process_ownership.py` 全部通过。

同时把三处**计数型断言**改成断言本流的工具调用序列（附件流 `read→write→publish_file`、文档流
`skill→read→read→write→pwsh→publish_file×3`、浏览器流 `browser_navigate→browser_fill_form→browser_click×2`）：
原断言把一次性 `load_tools` 的结果算进本轮流，复用后该 step 不再重复，计数自然下降；新断言更精确而不是更宽松。
证据 `var/v2-bench-t4iou9ra/benchmark-after-reuse.json`、`var/agent-integration-7l8oaqya/evidence.json`；
详细分析见 `docs/agent-v2-performance.md`「改造前后对照 → 修复后复测」。

条件 9 判定：**对照已记录，可重复明显回退已诊断、修复并复测通过**（每会话一次冷启动作为已知限制）。

## 2026-10-14 增量：条件 10 的交付文档（验收总表 + 历史归档）

- 新增 `docs/agent-v2-acceptance.md`：条件 1–10 逐条对照表，每条给出**状态、证据路径与命令**，并显式区分
  **真实**（官方模型/搜索、真实 Edge、真实旧库、真实调度时钟）与**受控**（本机夹具模型 + 真实产品路径）
  两类证据；含**已知限制**清单（真实学校登录态未试用、每会话一次约 4.8 秒冷启动、真实付费只覆盖三条场景、
  空计划成因、旧库导入范围、外部 MCP 现状、npm allowScripts 提示）与最小解锁条件。
- 归档首批已被 V2 取代的首轮说明文档到 `docs/history/first-round-docs/`（vision/requirements/mvp/roadmap/
  open-questions/integration-review/agent-acceptance/agent-progress/agent-progress-history，共 9 个），
  新增 `docs/history/README.md` 说明三组历史与当前权威文档位置，并更新根 README 的文档入口
  （首轮验收 → 现改为 [验收总表](agent-v2-acceptance.md)，另加 UI 改版指南与历史文档入口）。
- 归档前先做引用扫描（代码/测试/工作流/根文档），确认这 9 个文件在仓库内**无外部引用**；仍被代码引用的
  首轮连接器文档（`campus-login.md`、`webvpn-analysis.md` 等）保留原位，并在历史索引里说明原因。
- 复跑：API **329 passed**、根回归+启动器 **203 passed + 80 subtests**、前端 149 passed、tsc/Vite exit 0、
  契约 `--check` exit 0、`dev.py --check` exit 0（见验收总表的复现命令）。

## 2026-10-14 增量：当前 HEAD 的"最终目录"回归（真实全新目录 + 真实安装）

按条件 1/10 的"全新目录按锁文件可安装/构建/启动 + 最终目录回归"，在本轮 HEAD 上重做了一次全新目录验证，
不用工作树里的任何未安装状态：

1. `git archive --format=tar -o head.tar HEAD` → 解到 `.test-tmp/v2-round/final-dir`（684 个文件）。
   诚实记录一个**工具限制**（不是产品缺陷）：Windows 自带 `tar.exe` 解 `git archive` 的 tar 时，
   5 个**非 ASCII 文件名**（`contracts/reviews/2026-10-13-CR011-V2通用运行时设置.md` 等 CR 文档）报
   `Invalid empty pathname` 未被解出；这些是评审文档，不参与安装/构建/测试。
   用 PowerShell 管道 `git archive | tar -x` 会直接损坏归档（文本转换），必须落盘再解。
2. `python tools/dev.py --setup`：**exit 0**（新建 venv + 53 个锁定 wheel、web 180 包、runtime 540 包，
   两处均 **0 漏洞**）；npm 对同样的 5 个包报 install scripts 未被 allowScripts 覆盖——与干净重建证据一致，
   仍 exit 0。
3. `dev.py --check`：**exit 0**（`missing: none`）。
4. 全新 venv 下 API 套件：**329 passed**（含 `test_env_integrity` —— 该用例断言 `packages/learning` 来自工作区源码，
   在未执行 setup 的目录里必然失败，正好证明它测的是真实安装状态而不是空跑）。
5. 全新 venv + **全新安装的 node_modules**（`npm ci` 覆盖了我为省时建的同名 junction）：
   `tools/verify_agent_runtime.py` **ok=true，83 次受控调用**；`tools/verify_tool_discovery.py` **exit 0**。
   根回归+启动器 **203 passed + 80 subtests**；契约 `--check` exit 0。

结论：当前 HEAD 的**跟踪文件集合自足**——全新目录按锁文件安装、检查、跑测试、跑真实 Harness 集成全部通过。
证据已从一次性目录复制到 `var/final-dir-regression/`（集成与按需发现各一份 evidence.json），
约 1 GB 的临时安装目录已删除，避免在工作区留下无用依赖树。

## 2026-10-14 增量：条件 5 的真实校园只读试用（真实浏览器 + 真实模型，定位到登录这一唯一阻塞）

用户确认应用自有浏览器已登录浙大后，新增 `tools/verify_real_school_browser.py`：它驱动**已经在运行、持有登录态
浏览器的应用实例**（`--base-url`），用真实官方模型发一轮只读任务，全程不填表、不提交，并在超时时用自己持有的
owner cookie **取消本轮**，避免在用户实例上留下悬挂回合。

实测（真实 Edge + 真实模型，`var/real-school-browser-20261014-203823/`）：

1. 直连 `https://zdbk.zju.edu.cn/` 在本网络**不可用**：浏览器拿到 `ERR_EMPTY_RESPONSE`，
   命令行复核该 URL 只返回 **68 字节**空响应；而 `https://www.zju.edu.cn/`（100 KB）与
   `https://webvpn.zju.edu.cn/`（47 KB）可达。**校外访问教务必须走 WebVPN**。
2. 模型自己按产品语义先调 `campus_webvpn_entry({"service":"zdbk"})`，拿到
   `https://webvpn.zju.edu.cn/https/<token>/jwglxt/xtgl/login_ssologin.html`，再用浏览器打开。
3. 该地址 **302 跳转到 `https://webvpn.zju.edu.cn/login`**，页面标题 `WebVPN`，正文是
   “欢迎试用浙江大学WebVPN系统访问校内网页资源，请使用上网账号（同VPN和RVPN账号）登录”。
   网络记录：`login_ssologin.html => [302] Found` → `webvpn.zju.edu.cn/login => [200] OK`。
4. 结论：**产品路径是通的**（WebVPN 入口工具 → 浏览器 → 真实页面 → 页面内容与网络记录都可读），
   但**该浏览器当前没有 WebVPN 登录态**，所以“登录态浏览器读取页面并下载文件”仍差最后一步。

诚实说明两点：

- 第一次试用（直连 zdbk）我发起的回合没有及时收敛，我事后**用自己回合的 runner 进程做了清理**
  （PID 41484，20:26:41 启动、父进程为 18100 实例），journal 里该回合已落为 `failed`；第二次试用由脚本
  自己取消（`timed_out: true`），未再留下悬挂回合。
- 期间发生过一次**我的操作事故并已修复**：我为省时在一次性目录里建了一个指向真实
  `apps/agent-runtime/node_modules` 的 junction，随后递归删除那个一次性目录时，PowerShell 跟随该 junction
  把**真实的 node_modules 内容清空**了。发现后立即：① 用 `cmd /c rmdir` 删掉残留 junction（只删链接）；
  ② 用启动器自己的 npm 环境清洗逻辑（`dev.py.npm_environment()`）在 `apps/agent-runtime` 执行 `npm ci`
  重装（exit 0，`@deepseek-ai/*` 289 个包，`playwright`/`dsh-sdk-client` 就位）；③ 复跑
  `tools/verify_agent_runtime.py` → **ok=true，83 次受控调用**，确认环境恢复。
  教训与防护：**任何递归删除前必须先确认目标目录内没有指向他处的 junction**，本次已把该 junction 移除。

最小解锁条件（现在有实测依据）：在**应用自有浏览器**里登录 `https://webvpn.zju.edu.cn/login`（上网账号，含验证码），
完成后我重跑 `tools/verify_real_school_browser.py --base-url <实例>`，即可完成条件 5 的只读真实试用。

## 2026-10-14 增量：用户登录后重跑校园只读试用（读取半边真实成立）＋ 冷启动预热

### 条件 5：登录态浏览器**确实读到了校方页面**（真实证据）

用户登录 WebVPN 后重跑 `tools/verify_real_school_browser.py`（真实官方模型 + 真实 Edge，
证据 `var/real-school-browser-20261014-205127|205855|211018/`）：

- 访问 `.../jwglxt/xtgl/login_ssologin.html` 不再跳登录页，落到
  `.../jwglxt/xtgl/index_initMenu.html`，**页面标题「本科教学管理信息服务平台」**；
- `browser_evaluate` 读到真实内容：通知公告列表里出现
  「关于9月24日下午控制学号访问教务系统的通知」「【置顶】航空航天学院关于开设全英文…」等条目与 `xwbh` 编号；
- `browser_click` 的定位器解析到真实元素 `<a title="通知公告" … href="/https/7772…">`（登录态下的真实导航）。

即：**“登录态浏览器读取真实校方页面”这一半已在真实环境成立**，且走的是产品路径
（`campus_webvpn_entry` → 浏览器工具 → 页面内容）。

**未完成的一半**：由模型汇总并下载一个校方文件。四次试用中浏览器工具全部正常返回（一次 10 秒点击超时），
但每轮在最后一次工具结果之后**再无事件**，直到脚本超时取消：这不是浏览器失败，而是真实模型请求在 WebVPN
延迟下停住（应用实例的 `turn_seconds` 内未产出终态）。已按纪律清理：脚本用自己持有的 owner cookie 取消本轮；
更早那次悬挂回合由我停掉自己那个 runner 进程（PID 41484）后落为 `failed`，journal 里没有遗留 running 轮次。

### 冷启动预热：第一条消息 5.1 秒 → 0.78 秒

- 先用可选埋点量出：runner 自身模块加载仅 **74 ms**，约 5 秒全在 `harness.run()`（上游 runtime 子进程组装
  93 插件 + 浏览器 MCP 子进程 + 建会话）。
- 于是把 `DeepSeekHarness.start()` 搬到**会话创建时**后台执行：新增会话级 grant token（`runtime_grants` 表）、
  `Runtime.prewarm/prewarm_async`、runner 的 `{prewarm:true}` 帧，以及预热态可用的两个启动调用
  （`/internal/control-address` 带会话 id、`/internal/browser` 允许 `method:'tools'`）。预热不会拉起浏览器。
- 实测（2 次运行 × 每轮 2 次，创建会话后 12 秒才发第一条消息）：**第 1 轮首个模型请求 4812/4672 → 406/375 ms**，
  第 1 轮总时长 5141/5047 → **797/750 ms**；中位数首请求 2422 → **289 ms**。
- 复验全绿：API **331 passed**（含 9 项复用/预热测试）、根回归+启动器 **203 + 80 subtests**、
  集成 **ok=true（83 次调用）**、按需发现/调度/目标/子任务/工具分组/进程归属全部通过。
- 复验抓出并修掉两个真实问题：预热启动的两个网关调用 404（按轮次查行，而预热没有轮次行）；
  预热线程与第一轮的竞态（预热失败回收时撤销 grant，而该轮已绑定它 → `HARNESS_FAILED`），现共用会话锁。
  另把 runner 的轮次异常在 `CAMPUS_RUNNER_MARKS` 下写 stderr，避免再出现“只看到 HARNESS_FAILED 看不到原因”。
- 详细数字与限定见 `docs/agent-v2-performance.md`「冷启动预热」。

## 2026-10-14 增量（第三轮）：预算内真实试用、每轮模型调用上限、WebVPN 单会话

### 条件 5：读取半边继续坐实；下载半边卡在“这些通知确实没有附件”

在**同一个已登录实例**上（真实官方模型 + 真实 Edge）重跑 `tools/verify_real_school_browser.py`，
提示词改成**预算内**（每回合 ≤3–6 次工具调用）：

- `--task login-read`（≤3 次调用）：19 秒内收敛，读到「本科教学管理信息服务平台」，
  给出页面标题、当前 URL（`.../index_initMenu.html?jsdm=06`）与左侧栏目清单，
  并**如实说明**读到的部分没有学号/姓名原文（未展开的下拉菜单不算）——没有编造身份片段。
- `--task download`（两步，各 ≤6 次）：第一步读到通知公告列表（**3,811 条 / 255 页**，置顶三条标题）；
  第二步在预算内打开 3 条通知详情，逐条核对后确认**都没有附件**，正文给的是外部报名链接
  （`jsj.top`、`wj.qq.com`），模型据此明确回答“没有可下载的附件”并列出缺口。
- 证据：`var/real-school-browser-20261014-213144/`（客户端被杀但 journal 里 result 事件完整）、
  `…-215540/`（两步 download，两个回合都有完整回答与 settled_by=result_event）、`…-215910/`、`…-220523/`。
- 校方站点事实（真实观测）：通知公告列表页是 jqGrid 异步渲染、行内没有 href（只能点进详情拿 `xwbh`）；
  置顶项多为院系通知，附件要**用类别筛选或标题搜索**才找得到——新的三步流程已按此设计。

### 真实限制：该实例把每轮模型调用限制为 8 次（BUDGET_EXCEEDED）

- 一次写成“12 次工具调用”的提示词在第 8 次模型调用后被拒：journal 出现
  `model_error {"code":"BUDGET_EXCEEDED","message":"本轮模型调用达到配置预算。"}`，
  该轮以 `RUNTIME_FAILED` 结束、`response` 为空。**工具全部正常返回**，是预算耗尽而非工具失败。
- 计数依据：`turns.model_calls=8`；`RunLimits.model_calls` 来自 `CAMPUS_MAX_MODEL_CALLS`（默认 128），
  该实例被配置为 8。因此真实试用必须让每回合的工具调用数 ≈ N 且 N+1 ≤ 预算；
  现在学校流程拆成 3 个小回合（≤5 / ≤4 / ≤4 次工具调用），并新增 `--task download-flow`。
- 这同时修正上一节对“模型在最后一次工具结果之后停住”的解释：不是浏览器或 WebVPN 卡住，
  而是**预算用尽后上游拒绝下一次模型调用**；应用如实落为失败态（无回答、无假成功）。

### 运维事实：WebVPN 同一账号只允许一个会话

- 用户在第二个浏览器登录 WebVPN 后，App 自有浏览器里的会话被挤下线：`…-220523/` 第二回合明确记录
  当前 URL 变为 `https://webvpn.zju.edu.cn/login`、页面标题 `WebVPN`，因此再也取不到校方页面。
- 结论：**真实试用期间只在一个浏览器里保持登录**；需要重登时先在 App 的浏览器窗口里完成，
  再跑脚本（本轮已按此清理：开发用的插件浏览器会话已 stop，不再与 App 争抢会话）。

### 为什么端口 18100 那个实例的回合总是停在 running

- 该实例启动于 19:08，代码早于 `32974aa`/`b0aa96a`：它的 `_run_owned` 在读到 `result` 帧后仍要等
  `for line in proc.stdout` 自然结束并 `proc.wait()`，再以 `answered and proc.returncode == 0` 判定成功。
  于是这些回合永久停在 `running`（脚本用自己持有的 owner cookie 取消，journal 里没有遗留 running 回合）。
- HEAD 已改成**以 result 帧为准**：`answered` 后立刻 `store.finish(tid, "succeeded", ...)`，
  是否保留 runner 由 `proc.poll() is None` 决定。`tools/verify_agent_runtime.py` 在 HEAD 上多处断言
  `status == 'succeeded'` 且集成 ok=true（83 次调用），说明 HEAD 不存在该问题。
- **浏览器层没有版本差异**：`apps/agent-runtime/browser-host.mjs`（14:28 最后改动）与
  `apps/api/app/agent_bridge/browser.py`（09:56 最后改动）都早于该实例启动时间，
  所以本轮真实浏览器证据（读页面、下载路径、ledger）对 HEAD 同样成立。

## 2026-10-14 增量（第四轮）：真实校方表单确实触发"一次确认"＋修掉它的空等问题

### 现场证据：教务的「通知标题」查询被拦成一次待确认操作

在已登录的教务通知公告列表页，指令是"在「通知标题」填「附件」并回车"。真实发生的（`var/agent/browser/actions.json` 与 journal）：

```
call    browser_type {"element":"通知标题 …","text":"附件","submit":true}
result  {"status":"needs_user","action_id":"c74f1a7d-…"}
preview POST https://zdbk.zju.edu.cn/jwglxt/xtgl/xwgl_cxXwListGyxtzgg.html?gnmkdm=index
        字段 gglb=""、autocomplete=""、xwbt=附件   按钮「提交」
status  pending（会话 owner 在浏览器工作区点「确认并执行」后才执行）
```

- 这正是合同要求的「受控表单填写、外部提交前一次确认、防重复提交」在**真实校方页面**上的现场证据：
  工具结果明确显示要提交的 URL/方法/字段，模型**没有绕过、没有重复提交**。
- 模型的行为也正确：得不到确认就如实回答"筛选未生效、无法给出结果"，没有编造搜索结果。
- 说明：两次 pending（`979166fe` 属于我的自动化会话、`c74f1a7d` 属于操作者自己的会话），
  因为 browser review 是**按会话 + owner 校验**的，只有创建该会话的浏览器能确认——
  这也是产品设计的一部分（我的脚本不能替用户点确认）。

### 真缺陷（已修，HEAD 已验证）：needs_user 后模型空等，烧掉整轮预算

- 操作者那次会话里，模型拿到 `needs_user` 后连续 3 次 `browser_wait_for`（15+25+30 秒）空等，
  把 8 次模型调用预算耗光才回答——确认本来只能由人在界面完成，等多久都不会自动生效。
- 修法：`browser-host.mjs` 的 pending 结果里补上明确指引——**立即结束本轮、不要 `browser_wait_for`
  空等、不要重复调用同一操作**；`verify-browser.mjs` 新增断言（仍验证"一次确认、不重复提交、展示具体表单值"）。
- 复验：`node apps/agent-runtime/verify-browser.mjs` → **ok=true**，13 项检查全过
  （真实 Edge：填表→一次确认、改表单需重新确认、密码不进结果、附件下载字节一致、停止只关本页…）；
  同期 `tools/verify_agent_runtime.py` **ok=true（83 次调用）**。




