# Campus Agent 核心试用版实施进度

目标：按 `docs/agent-design/goal-implementation-prompt.md` 完成 M0–M2 与全部首轮验收。

## 当前实施基线

- 实施目录：`C:/Users/mingxiang/.codex/worktrees/campus-agent-core/campus-agent/workbench`
- 所属工作树：`C:/Users/mingxiang/.codex/worktrees/campus-agent-core/campus-agent`，源自 `origin/main` 的独立 managed worktree。
- 实施分支：`codex/agent-core`。
- 发布包来源、SHA-256 和 408 项清单核验：`agent-baseline.json`。
- 原工作区：`D:/AI/campus-agent`。原有 55 个改动文件及 binary diff 备份：`D:/AI/campus-agent-goal-backups/20261013-082748`，没有覆盖原文件。
- 本目录包含展开后的学习工作台源码和复制的设计文档，旧校园模块留在原工作区。

## 已完成与证据

1. 重新读取首轮合同和设计；同步远端，main 仍为 `3a853560fcec1976ece2f72e0d90fc3b9e8307f0`。
2. 创建隔离工作树，安全解包源码并逐项核对 408 个 SHA-256，全部匹配。
3. 确认 Node 24.19.0 / npm 11.17.0 / Python 3.12.10；本地源码没有 AGENTS.md。
4. npm 官方包可访问，`@deepseek-ai/dsh` 当前可固定为 `0.2.0-rc.2`；正在核对 SDK 与真实包接口。
5. 学习工作台的独立环境安装完成，`npm run build` 成功，前端 18 个文件 / 116 项测试通过。
6. Python 全套 API 测试通过：219 项（原有 200 + 新增查询工具 19）；记录在 `var/baseline-test-results.txt`。测试只使用隔离合成数据库。
7. 九项查询工具已实现，严格参数、课程范围、资料版本、日期精度和证据测试通过，见 `agent-tools-implemented.md`。
8. 真实 `@deepseek-ai/dsh@0.2.0-rc.2` 的显式 campus profile 已启动并运行 SDK / Agent loop，模型→工具→回读结果通过；16 个持久事件，证据 `var/runtime-smoke-M8Z3f1/runtime-evidence.json`。该 smoke 使用合成 HTTP bridge，不能当作 M1 整体验收。
9. `apps/agent-runtime/package-lock.json` 与 `agent-runtime-version.json` 固定 npm 包和 integrity；`apps/api/requirements-lock.txt` 固定当前干净 Python 环境。
10. `python tools/dev.py --platforms --api-port 18000 --web-port 15173 --smoke-test` 完成数据库迁移、后端启动、Vite 启动与网页同源代理 /health 检查，退出码 0；服务随后由启动器关闭。
11. 复核备份 manifest：原工作区 55 个既有改动文件的 SHA-256 全部未变。
12. 根据提示词请求补充 Goal 接续命令、通用模板和逐项验收台账要求；原目录与实施目录的合同 SHA-256 一致。已核对官方 Goals 指南及当前 active 状态，没有新建重复目标，也没有将提示词交付视为 M0–M2 完成。
13. 实际 Python Agent 网关已接入：本机 cookie 会话所有权、服务端课程/连接范围、逐轮能力令牌、幂等请求和工具记录、模型配置快照与发送同意、取消、重启中断记录。新增契约与原契约并集仍逐项对账，见 CR010 与 `agent-api.md`。
14. `tools/verify_agent_runtime.py` 通过实际 FastAPI、官方 SDK 与合成业务 DB 完成作业查询→资料检索→原文读取、第二轮追问、重复请求、取消后继续与真实 API 重启恢复；13 次受控模型调用，证据 `var/agent-integration-0hgtky9u/evidence.json`。只有模型为受控响应，领域工具与运行时均实际执行。
15. 扩展后 API 全套回归 229 项通过；随后补充计划未安排项/警告范围校验及 2 项回归，查询工具专项 21 项通过。未为了通过而移除旧断言。
16. 一键 setup 加入锁定的 agent-runtime 依赖，dev 自定义端口传给 bridge 与 Origin 配置；新接线后启动与网页代理 smoke 再次通过，退出码 0。
17. 补上本机个人任务无课程归属时的可信范围开关，避免查询静默遗漏；模型不能设置该开关。最新相关回归 32 项通过（工具 22 + gateway 10），包含计划未安排/警告范围和个人任务边界。

## 正在进行

- 接对话主入口、来源工作区与会话恢复 UI。
- M2 临时约束与可检查的计划授权闭环。

## 未完成

- M0 的新完整入口、重建验证以及其他原模块的相关基线检查。
- M1 前端与端到端三场景。实际 Python 桥接、模型适配、持久化会话已有端到端证据。
- M2 临时约束、计划写工具、用户授权与撤销。
- 自动化与桌面/窄屏浏览器验收、最终报告。

## 下一步

实现 `apps/web/src/features/agent/` 对话页面，将默认入口改为对话并把旧模块收进辅助工作台。复用 apiRequest 的同源 cookie 请求、模型配置与来源定位组件；展示真实轮次/工具结果、支持取消和恢复，草稿/课程上下文不因工作区切换丢失。M2 临时排程约束需扩展现有 preview_candidate，不能改写长期 Availability；计划应用授权必须来自具体用户操作，不能接受模型提供 approved=true。

## 首轮验收台账

| 条件 | 状态 | 当前证据与剩余 |
| --- | --- | --- |
| 1 干净安装/构建/启动 | 进行中 | 原环境安装、前端构建与新接线启动 smoke 通过；还须包含运行时的另一干净目录重建 |
| 2 实际 Harness、插件与工具集合 | 已验证 | integration-0hgtky9u/evidence.json：官方固定版本、真实插件和九项实际工具，模型/工具/回读均通过 |
| 3 同一对话三个场景 | 进行中 | 查询/原文解释已有后端工具链；网页和计划闭环未完成 |
| 4 成功/空/无依据/非法/越权/版本/重复/失败/取消 | 进行中 | 工具 22 项、gateway 10 项及实际取消证据；M2 写入相关覆盖尚缺 |
| 5 确定性计划约束及事实精度 | 进行中 | 未知日期保留、本地/学校提交状态分开、计划引用范围检查通过；临时约束待实施 |
| 6 桌面与窄屏浏览器主流程 | 未开始 | 不能以源码或 HTTP 验证替代 |
| 7 基线与新增回归 | 进行中 | API 整套 229 + 后续相关 32；前端基线 116；最终跨模块检查待完成 |
| 8 实际工具/运行/限制/验收文档 | 进行中 | tools-implemented、agent-api、版本与进度已更新；最终报告待闭环 |

## 失败尝试与阻碍

- 查询猜测的 `@deepseek-ai/deepseek-harness-sdk` 名称返回 404；已改用实际发现的 `@deepseek-ai/dsh-sdk-client`，不是目标阻碍。
- 新测试夹具首次缺少 content_chunks.provenance，补齐合成来源后 19 项通过。
- SDK 首次初始化早于异步插件注册，新增 campusReady 服务并让 SDK 等待；随后修正上游不支持 type 数组的结果 schema，真实 smoke 通过。
- `pip freeze` 在跨盘 managed worktree 的 editable 源上报 relpath 错误；改用 `pip list --exclude-editable` 记录版本，并显式保留相对的 learning editable 安装。
- 固定版 SDK 没有单轮 cancel 请求。计划通过关闭/清理该运行时停止本轮，再从持久化会话恢复；后台业务 job 的取消必须另走业务服务，不能混为一谈。
- 实际第二轮首次失败：上游 SDK 冷进程会调用 agents.create，与已存日志冲突。改用公开 context.extend 在 SDK 部署上下文选择 agents.resume，官方 SDK/循环/关闭逻辑未改；第二轮、取消后恢复和 API 重启后恢复均已通过。
- 集成夹具曾引用不存在的 profile 局部变量 here，修正为已有 root 后运行成功；首次 cookie 测试使用了 httpx Cookies 不存在的私有属性，改为标准 jar 检查，回归通过。
- 真实校园账号与真实付费模型不在首轮合成验收门槛内，本轮不调用已有凭据。

Goal 仍进行中；没有满足任何整体完成声明。


## 接续核验与事务基础（2026-10-13）

上一目标轮属于实际进展：对话页面、浏览器证据、临时约束和真实 Harness 恢复验证均已落地。本轮重新读取执行合同，回收此前仍运行的 session 64002：资料工具与 gateway 35 项全部通过（11.55 秒），不是凭历史状态推断。

- 默认对话入口、来源和日历抽屉、草稿保存、课程上下文、轮次取消与恢复已实现。桌面及 390px 窄屏合成数据验证见 `docs/assets/agent-browser-m1.json` 及相邻截图；计划写入流程尚未做浏览器验收。
- 实际 Harness 最新集成证据为 `var/agent-integration-2aa9tgv_/evidence.json`：13 次受控模型调用，实际领域适配、取消后立即续问和服务重启恢复。真实账户与付费模型质量仍未验证。
- 此前完整 API 240 项、前端 121 项及构建通过；后续局部检查不能冒充新的全量结果。
- 临时不可用时间与临时可用窗口已接入确定性预览引擎，不写长期偏好；锁定冲突拒绝，容量不足保留未分配结果。
- 本轮新增 `app/plan_mutations.py`，复用原应用/撤销语义，允许调用方用 commit=False 将变更与未来授权回执合并到一个事务。HTTP 路由仍使用默认提交，不新增公开参数。
- 新增实际数据库回滚测试：应用回滚恢复原生效计划与候选状态；撤销回滚恢复当前生效与被取代状态；正常撤销恢复前版。运行 `pytest tests/test_agent_constraints.py tests/test_plans_api.py -q --tb=short`：20 passed，3.52 秒。

### 当前未满足条件与下一步（覆盖上方旧“下一步”）

M2 仍缺持久化授权回执、计划差异预览工具、可信用户接受入口、apply/revoke 工具、任务生成/反馈与业务 job 取消，以及同一对话完整浏览器验收。下一步在业务数据库增加动作/回执模型和迁移，绑定会话、目标候选内容、当前生效计划和输入版本；用上述事务服务保证写入和回执原子提交。模型提供 approved=true 不构成授权。

M0 仍需另一干净目录重建；最终完整回归、工具与运行文档更新和逐项验收报告尚未完成。不要把 M1 截图或局部测试当作全部验收。

原工作区保全核验记录：55 个原文件中 53 个仍逐字节一致；README 与 goal-implementation-prompt 两份设计文档已按后续用户请求更新，旧版在备份中保留。不得继续声称目前 55 个均未变化。


## 持久化计划动作服务（2026-10-13）

上一轮为进展：应用/撤销事务服务与 20 项验证已落地。本轮在真实业务数据库新增 AgentPlanAction 模型、迁移 a13f47c908de 和 plan_actions 服务。预览、候选、约束及差异一起持久化；批准绑定具体会话/动作；执行领取、计划变更和回执同事务提交，异常回滚。执行前检查输入、候选内容、当前生效计划/锁定块、有效期和新增安排时间。撤销绑定本会话已应用方案并恢复前版。

验证：新授权服务 10 项 + 临时约束/原计划 API 合计 30 passed（4.36 秒）。后续将批准更新改为条件更新，并让撤销预览使用独立请求标识，10 项再次通过（1.58 秒）。独立空合成库 var/agent-actions-migration-check.db 执行 alembic upgrade head 成功；alembic check 输出 No new upgrade operations detected。未动真实学生数据。

服务说明见 docs/agent-plan-actions.md。尚未接入工具 registry 或公开浏览器入口，模型集合仍为原 9 项。下一步接 availability.read、plans.preview/apply/revoke，严格校验整个安排范围；网关从可信 turn/call 生成幂等标识；新增有浏览器所有权校验的具体动作接受入口；在对话中展示差异并承接“按这个来”。随后完成任务反馈/job 取消和 M2 全链路验收。整体 Goal 保持进行中。


## 工具与可信接受入口接线（2026-10-13）

上一轮为进展。本轮注册 availability.read、plans.preview/apply/revoke，集合从 9 增为 13；写工具 effects=write。预览幂等标识由真实 turn/call 生成；相对日期服务端计算；严格参数与工作区范围校验。浏览器新增动作列表和接受入口，验证 cookie 会话所有权与请求来源。接受和执行分离，模型额外 approved=true 被严格拒绝。

验证：领域工具/动作/gateway 47 passed（8.26 秒）；新增实际 HTTP 预览→未授权拒绝→浏览器接受→工具应用→接受撤销→工具撤销及跨来源/会话检查后，gateway+契约 21 passed（9.71 秒）。实际 Harness 原查询/取消/恢复集成成功，证据 var/agent-integration-ji268zx5/evidence.json，13 次模型调用，模型可见 13 项工具。此集成尚未调用新写工具，不能当作完整 M2 证据。

下一步：前端获取动作、显示真实差异/缺口和接受按钮；把已接受 action_id 作为可信上下文承接下一轮，支持“按这个来”和“撤销刚才安排”；再扩展受控模型集成执行实际计划工具与浏览器验收。任务生成/反馈、job 取消、干净目录重建和最终验收仍未完成。


## 对话方案卡片与可信轮次承接（2026-10-13）

上一轮为进展。本轮实现 PlanActions 卡片与动作查询，展示新增/移除/保留时间块、任务标题快照、锁定及未分配结果。按钮接受后发送绑定 action_id 的轮次，保留原草稿；精确输入“按这个来”“撤销刚才安排”会选择本会话对应方案，先走可信接受入口。approved 动作提供继续执行入口；历史完成回执明确不等于当前生效状态。

后端 TurnRequest 新增可选 action_id，校验会话所有权及 approved/completed 状态；会话 journal 在入队同事务写 accepted_action 事件，纳入请求幂等指纹。运行时读取事件，把 action_id 与具体工具名注入可信上下文。模型无法通过消息文字获得授权。

验证：网关 12 passed（8.65 秒），含未接受拒绝、绑定动作持久化、同键同动作重放及换动作冲突；动作/契约 21 passed（4.52 秒）；前端 AgentPage 7 passed（17.47 秒），含按钮/文字接受和保留草稿。首次新增前端测试因发送按钮图标影响可访问名称失败，给按钮明确 aria-label 后通过。前端构建已通过一次，最终小修后再次构建另有会话句柄记录。

下一步必须扩展 tools/verify_agent_runtime.py 的受控模型与合成数据，实际执行预览→可信接受→Harness apply→可信撤销→Harness revoke，并在桌面与窄屏实际浏览器验证卡片。当前新卡片只有组件测试与构建证据，不能声称已浏览器验收。还需任务生成/反馈、业务 job 取消、重建验证和最终报告。留意动作重试时不能将另一个卡片的 pending action_id 复用到新选择。


## 真实 Harness 计划闭环与浏览器验收（2026-10-13）

上一轮为进展。本轮扩展 tools/verify_agent_runtime.py：受控模型从真实工具结果组织回答，合成库配置真实任务和长期可用时间。实际 Harness 同一会话完成两次预览/接受/应用，第二次临时阻断明天 19–21 点，再撤销恢复前版，并重复撤销回读持久化回执。原查询、取消和服务重启恢复也保留。进程 session 82763 正常退出；证据 var/agent-integration-lwdz387_/evidence.json，27 次模型调用，plan_checks 全通过。模型可见 13 项工具。

浏览器服务 session 44632 经重新轮询确认仍存活，从已有页面继续。旧预览已超过 30 分钟，应用返回 ACTION_EXPIRED；重新生成后通过具体卡片接受并实际生效，日历显示避开 19–21 点的 18–19、21–22 两段。按钮操作保留输入草稿。390×844 窄屏刷新恢复同一会话和草稿，精确文字“撤销刚才安排”执行真实撤销，日历无生效安排。scrollWidth=375，小于 viewport=390，composerBottom=836；控制台 error 列表为空。证据 docs/assets/agent-browser-m2.json 及 agent-plan-active-desktop.jpg、agent-plan-revoked-narrow.jpg。浏览器临时视口已 reset，临时 tab 已关闭；自有服务通过 var/agent-m2-browser.stop 协作退出，session 44632 exit 0。

本次 M2 浏览器验证使用合成数据与受控模型，真实账户/付费模型效果未验证。日历仍用任务 ID 显示，后续复用任务列表补标题。全周场景、任务生成/反馈、真实业务 job 状态/取消、干净目录重建和最终全量回归/报告仍未完成，不能标记 Goal complete。


浏览器验收后的可读性修正：CalendarWorkspace 复用 api.getAllTasks 的完整分页读取，仅在日历打开且有生效计划时请求。时间块和未分配提示优先展示任务标题，读取失败明确提示并保留 ID。上述桌面截图为修正前记录，不能把它当作标题修正后的视觉证据；本次 TypeScript/Vite 构建通过，后续业务 job 浏览器验证时复核实际标题。


## 任务生成、后台取消与反馈接入（2026-10-13）

上一轮为进展。本轮新增 tasks.build/jobs.cancel/tasks.feedback，当前共 16 项工具。AgentJobRequest + 迁移 f27a68db491c 保存生成回执及会话 job 归属，与真实队列同事务提交；生成重放和跨会话权限测试通过。AgentFeedbackReceipt + 迁移 c61e294bd037 保存反馈回执；先用 expected_revision 条件校验任务，再复用原 record_feedback 服务，反馈、任务变化和回执同事务提交。原 HTTP 反馈接口仍保留原参数与幂等语义。取消复用原协作式业务服务，不把 running/cancel_requested 宣称为 cancelled。

对话 TurnView 根据真实 tool_result 中 job_id 展示 JobProgressInline，轮询原业务状态并提供具体 job 取消；对话禁用该组件通用重试入口，防止新 job 未记录会话归属，用户可通过 tasks.build 重新生成。工具说明已重写为当前事实，消除累积历史段落中的矛盾状态。

验证：任务/job/工具/gateway/计划动作 59 passed（10.73 秒）；之前含 worker 原回归的一组 59 passed（12.34 秒）；原写接口、B4 与生成 39 passed（6.94 秒）；前端对话 8 passed（16.76 秒），新增运行中取消必须显示等待检查点。新增迁移在独立 var/agent-feedback-migration-check.db 从空库升级成功，alembic check 无差异。曾误用不存在的 test_tasks.py 路径，未执行测试；已找到正确的 test_write_api.py/test_b4_endpoints.py/test_build_tasks.py 并通过，不能计入首次失败命令为通过。

真实 Harness 证据 var/agent-integration-xfc39mku/evidence.json，session 71134 exit 0，36 次模型调用、16 项工具。保留计划/查询/恢复验证，新增实际 tasks.build→jobs.read、真实 worker 完成、另一次生成→jobs.cancel、tasks.list→tasks.feedback；平台提交状态未改变。前一版本 job-only 证据 var/agent-integration-f9ofsy66/evidence.json 为 33 次。前端任务进度接线构建通过；首次把 job_id 类型加错到 AgentTurn，修正为 ToolEnvelope 后 TypeScript/Vite 成功。

未完成与下一步：任务/job/反馈的桌面与窄屏浏览器操作，本周完整安排及“本周三晚上没空”的自然表达验证（现有临时阻断已是确定性业务逻辑，但受控模型夹具主要验证绝对19–21点/明天）；干净目录安装构建启动与最终全量回归；模型错误在 UI 的具体原因呈现、文档/验收台账同步和最终逐项审计。既有工作区仍完整保留，未提交/推送，Goal 保持 active。


## 本周约束与完整同会话浏览器验证（2026-10-13）

上一轮为进展。本轮 PreviewArgs 增加 unavailable_evenings，服务端按 ISO 星期与偏好时区转换本周18:00至次日00:00阻断，并在预览显示范围/提示；已过去的星期返回 INVALID_INPUT，不偷偷改为下周。plans.preview 保持临时覆盖不写长期偏好。实际发现当天方案可能从当前分钟起排、确认时即过期；Agent 动作调用原引擎时通过内部 not_before 留10分钟检查时间，原公开计划接口默认不变。

验证：约束/动作/原计划 API 33 passed（4.50 秒）；随后新增当天方案可检查并立即接受应用测试后动作14 passed（2.30 秒）。实际 Harness var/agent-integration-qbk_diui/evidence.json，session28331 exit0，41次模型调用，包含本周晚间预览和应用、明天调整、撤销、任务生成/取消、反馈和恢复。

真实浏览器本机合成环境 var/agent-integration-8qa99od9，session10073。一个会话完成作业查询、实际原文、本周三晚间预览、卡片应用和日历真实标题、排队任务生成并通过按钮取消、390px窄屏明确报告完成30分钟、撤销后刷新恢复。证据 docs/assets/agent-browser-full.json、agent-weekly-calendar-desktop.jpg、agent-feedback-jobs-narrow.jpg。控制台错误为空，scrollWidth375<viewport390。临时视口reset、tab关闭，自有服务通过stop marker正常关闭。这里受控模型回答有意返回原数据的状态枚举，真实模型措辞质量未验证。

下一步：干净目录重建当前源码（排除venv/node_modules/var等生成内容），锁定依赖安装、构建、启动smoke；修正UI模型错误原因展示；运行最终必要全量回归，统一运行/限制/验收台账与逐项报告。不可将本次浏览器成功当作整体验收完成。


## 最终回归与干净目录重建进行中（2026-10-13）

当前源码完整 API：pytest -q --tb=short，隔离 var/agent-final-regression.db/files/journal，270 passed（47.32 秒），session41044 exit0。网页全量 vitest：19文件124 passed（28.43秒），session51336 exit0。后续若再改代码，仅将这些作为本次版本证据，不冒充后续版本结果。

已创建全新目录 D:/AI/campus-agent-rebuild-c72uzzk5，复制479个源码/文档文件，排除venv/node_modules/var/dist/.git及缓存，没有复制已安装环境或真实数据。文件SHA清单见 docs/agent-clean-rebuild.json。正在该目录执行 python tools/dev.py --setup，session96650 最近已重新确认运行，Python锁定依赖安装成功，进入网页依赖安装。必须继续轮询同一96650句柄，不能因观察超时重启。随后在该目录运行 npm run build（apps/web）和 apps/api/.venv/Scripts/python.exe tools/dev.py --platforms --api-port18001 --web-port15175 --smoke-test；必要时按已有端口规则选择空闲端口，不终止未知进程。还须实际Harness集成在此干净目录可执行，最终逐项报告与模型错误UI原因显示尚待处理。


干净重建后续核验：setup session96650 exit0，Python/web/Harness锁定依赖完成。首轮构建发现新增测试的 Testing Library ByRoleOptions 不支持 exact 属性；已删除两个多余选项，未改变默认精确匹配断言。首轮集成发现 var 目录缺失，verify_agent_runtime.py 已主动创建；修复同步到干净目录，并更新 SHA清单。启动命令曾把参数和值连写导致 argparse拒绝，修正为空格分隔后成功。

干净目录 npm run build（session5143）exit0；tools/dev.py --platforms --api-port 18001 --web-port 15175 --smoke-test（session49489）exit0，完成全新库迁移及实际API/网页启动检查，服务由启动器退出清理。干净目录 Harness 集成 session96042 最近确认仍运行，需继续轮询同一句柄，不能按无输出重启。原始失败句柄均终态后才重跑。

固定 Harness 依赖安装时 npm audit 报13项（7 moderate/6 high），网页依赖0项；仍需检查具体依赖/启用范围并写入限制记录，不自动使用破坏性 audit fix --force 改变固定版本。下一轮接续模型错误UI修正、文档最终同步与完成审计。所有源码保留在原实施工作树；干净目录是复现副本，不替换唯一实施基线。

最终回收干净目录集成 session96042：exit0，41次模型调用，证据 D:/AI/campus-agent-rebuild-c72uzzk5/var/agent-integration-nr3tj3b5/evidence.json。上述运行中记录为较早观察，目前全部三个重建验证句柄均已终态成功。Goal仍active，剩余错误提示/依赖审计范围/文档与逐项验收报告。
