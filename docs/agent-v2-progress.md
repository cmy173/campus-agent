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
| 1 唯一源码与安装启动 | 进行中 | 主目录独立安装/构建/启动已验证；V2最终干净重建仍待全功能实施后验收 |
| 2 迁移与旧代码删除 | 进行中 | 快照与逐项台账；校园协议实现已单份化到 connectors/campus，campus_agent 协议模块改为别名；旧服务器/静态UI仍在树中待逐项退役 |
| 3 全部 C 档能力 | 进行中 | 文件/代码/Skills/MCP/文档/双子任务/持久目标/本机调度均已实际执行；按需工具发现（上游 PTC/发现机制）与真实搜索/模型仍缺 |
| 4 真实官方模型三场景 | 未开始 | 待 provider 与本地账号配置/预算 |
| 5 浏览器/下载/表单 | 进行中 | 应用自有持久Edge，真实MCP与API/Harness闭环、受控表单一次确认防重、字段变化拒绝、真实下载及UI恢复通过；真实校方登录态试用仍待完成 |
| 6 子 Agent/持续任务/调度 | 进行中 | 双子任务独立停止、持久目标冷启动恢复、本机调度真实线程+跨进程断点防重通过；跨轮子任务与独立浏览器归属仍待验收 |
| 7 自动执行及正确性 | 进行中 | 通用本地操作自动执行、可配置预算；计划意图授权与外部提交确认待改造 |
| 8 UI 分层与浏览器验收 | 进行中 | 请求/状态/展示/主题拆分，主导航收敛为 /chat 且旧URL跳转、任务工作区与换布局预览通过；桌面全宽视觉回归尚缺 |
| 9 性能前后对照 | 未开始 | 待本地基准，远程耗时单列 |
| 10 整体验证与交付 | 进行中 | 建立文档与工具，最终待验收 |

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

下一步：按台账退役旧 `campus_agent` 服务器与静态 UI（先将其承重的服务级回归改挂到 connectors/campus，避免丢失有效断言）、实现按需工具发现、补桌面全宽视觉回归与性能基准、CI 迁移到新测试入口，最后做干净重建与真实服务试用。
