# V2 并行开发任务提示词

日期：2026-10-14。协作基础为本地稳定提交 e164b52；完整 Goal 继续，不把基础交付当作最终完成。

主协调者负责 Harness/Goal 生命周期、授权边界、接口挂载、整合集成测试与真实服务验收。当前有三个已启动的子 Agent：ui_v2、campus_migration、scheduler_v2。外部协作者接同一任务前先领取范围，避免重复修改。

## 共用执行合同

以下段落与所选任务一起发送给任意工程 Agent。工作目录由协作者自行替换。

```text
请实际完成下面任务，不停留在方案。先读项目适用 AGENTS.md（若存在）、README.md、docs/agent-design/goal-v2-personal-assistant.md、docs/agent-v2-progress.md、docs/agent-v2-migration.md，以及当前源码。以实际文件与运行结果为准，历史记录不能代替本次验证。
本轮是 Windows 单人本地完整助手：对话主入口，官方 Harness 唯一模型/Agent 主循环，Python 处理领域与数据；普通本地可撤销工作自动执行，对外提交具体结果一次确认。保全历史源码和个人数据；旧功能验收后再删旧唯一实现。不要把旧应用整体藏到 legacy 继续运行。
仅修改领取的文件范围。不要覆盖其他 Agent 的修改。跨模块需修改时给协调者精确集成点。不要自行推送、合并、部署或发送其他聊天消息。在共享工作区不自行 Git 提交，由协调者统一提交；独立克隆可提交自己的分支并提供补丁。
Python 测试在导入前设置 PYTHONUTF8=1 和独立 STUDY_DATABASE_URL/STUDY_FILES_DIR/STUDY_SECRETS_DIR/CAMPUS_AGENT_HOME；conftest可能清库，严禁用个人库跑测试。禁止提交数据库、凭据、浏览器配置、依赖与测试临时目录。不输出 Key、密码或 Cookie。仅清理自己创建的进程。
交付可评审代码、文件清单、实际验证命令/结果/证据路径、剩余工作与精确集成步骤。区分受控模型/学校响应与真实账号结果。有限相关测试通过后继续交付，不无理由重复全套。
```

## 任务 A：UI 收敛与改版边界

```text
将 React UI 收敛为聊天唯一主要入口。旧辅助工作台整套主导航退役；资料、日历、复习、文件、浏览器按任务进入工作区，账号/连接保留设置。旧 URL 跳转到聊天工作区，保留必要定位，不能继续渲染另一套旧主入口。
分离数据/事件适配、会话与动作状态、展示、样式 tokens。新增仅开发的布局切换示例：同一组会话/工具数据替换布局和主题，业务与运行时无需修改。未知工具走通用结果展示。
范围：apps/web 与 UI 专属文档。不要改 features/agent/{api.ts,TurnView.tsx,TurnView.test.tsx,useTurnEvents.ts,GoalActivity.tsx}，持续任务正在由协调者实现。useConversationController 尽量维持接口；不得改 Python/运行时。
验收：相关交互测试、TypeScript/生产构建；实际浏览器桌面与390窄屏的聊天、工作区、刷新/草稿、错误/取消；截图与DOM证据。保留仍有价值的编辑能力可达性，不能只删导航而使功能消失。
```

## 任务 B：浙大校园能力与数据迁移

```text
把旧 campus_agent 的教务/学院通知/WebVPN/账号等有价值实现迁入新 FastAPI/Harness 系统。旧实现已明确是ZJU CAS/ZDBK/ETA与浙大平台。参考现有 connectors/adapters/{xzzd,zykt}.py 和 campus_agent/{campus_auth,campus_endpoints,campus_profiles,campus_college_sources,webvpn_adapter,storage}.py。
将连接器/查询作为可复用服务与模型工具，避免启动旧 server。明确课程/作业/资料和本机单人身份关系。清点不可重新获取的笔记、反馈、个人安排；做显式映射、可重入导入、备份与回滚。完整保留真实旧数据库，测试仅用受控临时库，不拼两个数据库。
范围：connectors、apps/api 领域新增模块/schema/migrations、agent_bridge/tools.py领域注册、迁移脚本与专属文档。不得改 runtime.py/store.py/routers/agent.py/main.py、运行时或UI；给协调者路由挂载和初始化调用点。需要浏览器登录态提供服务端 adopt_browser_cookies(cookies)，由协调者接私有桥；Cookie不进入模型/响应/日志。
验收：受控学校响应的实际解析与查询、持久化/来源/时间精度、导入幂等和回滚、失败如实显示、原计划/复习回归；按迁移台账逐项说明旧文件删除条件。真实ZJU账号验收由协调者完成，不能拿模拟结果替代。
```

## 任务 C：持久定时任务

```text
实现本机应用后台运行时的定时任务：模型工具可创建/列出/编辑/停用/删除，时间和时区可检查；真实调度器触发唯一 Harness Runtime，不建第二模型循环，不安装系统服务/唤醒设置。
先检查固定0.2.0-rc.2上游 dsh-schedule 公共README/types及持久化语义。若SDK-only不可用或存在flush/task.put崩溃重复窗口，可用Python持久occurrence/outbox管理调度，触发仍经Store.enqueue/Runtime.start。每次发生有确定请求键，跨重启不得重复入队；执行状态不确定时保留并提示，不能自动重放对外副作用。
支持once/every/daily/weekly和明确latest/skip错过策略；覆盖时区/DST、编辑与触发竞态、停用、忙碌会话和失败。睡眠/关机期间不承诺执行，启动后补偿策略明确。
范围：新增agent_bridge/schedules.py、routers/agent_schedules.py、调度专属tests、campus-schedules.mjs与专属文档。不要改现有 runtime/store/routers/agent/main/campus-plugin/prepare-profile/领域tools或UI；提供精确挂载/启动关闭/工具桥集成点。
验收必须运行真实调度器（可注入时钟），不能手工调用业务函数冒充调度。保存重启、错过、防重与不确定状态证据；只模拟模型/学校/时钟边界，不模拟调度器本身。
```

## 可外包给 DeepSeek 的后续任务

明确范围的契约测试、CI迁移、文档核对与性能测量可以另外领取；避免与上述正在修改的模块重叠。使用独立源码目录或分支，协调者检查补丁再整合。用户已分别授权开发100元、真实验收30元，不能混账；开始付费前需本地密钥与可核对的用量/费用控制。模型生成快不等于工程交付快，按实测用时与返工量评估。
