# 当前已实现的领域工具

状态：M0–M2 核心试用版首轮证据已汇总，详见 agent-acceptance.md。设计目录中的36项不是本轮全部实现清单。

Python 实现：`apps/api/app/agent_bridge/tools.py`。当前注册 16 项工具，其中原九项查询如下：

| 工具 | 输入重点 | 返回与限制 |
|---|---|---|
| connections.status | 可选 connection_id | 本地连接状态，不登录或同步 |
| courses.list | query、term、cursor、limit | 已导入课程；明确本机覆盖范围 |
| materials.list | course_id、cursor、limit | 资源版本与解析状态 |
| materials.search | query、可选 course_id | 子串检索、片段与定位；受限会话必须指定课程 |
| materials.read | resource_id、expected_version | 当前版本正文；旧版本与跨资料片段拒绝 |
| assignments.list | course_id、日期范围 | 未知截止保留；学校提交状态与本地状态分开 |
| tasks.list | course_id、status、due_before | 本地学习任务，不代替平台提交事实；可信本机范围可包含无课程的个人任务 |
| plans.read | 可选 plan_id | 生效/指定安排；受限会话拒绝其他课程的时间块 |
| jobs.read | job_id | 仅会话授权的 job，排除内部 payload |

严格输入 schema 由 `schemas()` 生成，禁止额外 owner、URL、路径或身份参数。所有工具返回 status/data/evidence/coverage/freshness/warnings 等结构化字段。没有来源观测时间时标记缓存新鲜度未知/旧，不能假定为新同步。数据仅为单机个人工作区，本层范围校验不等于原数据库已经实现多用户隔离。

Harness 插件：`apps/agent-runtime/campus-plugin.mjs`。通过服务端 schema 注册模型工具，工具只调用固定 loopback bridge；profile 显式加载运行时与校园插件，无 Shell、MCP、任意文件工具。SDK 接口等待 campusReady 服务就绪，避免异步注册竞态。

## 安排、生成与反馈工具

| 工具 | 输入 | 实际行为 |
| --- | --- | --- |
| availability.read | 空对象 | 返回真实偏好和 configured；未配置返回 null |
| plans.preview | period=this_week/tomorrow/dates；start_date/end_date、blocked、temporary_windows | 服务端按偏好时区计算日期，最多 31 天；真实引擎生成候选、差异与持久化动作，不生效 |
| plans.apply | action_id | 消耗用户接受的具体 apply 动作，原子写入计划与回执 |
| plans.revoke | action_id | 消耗用户接受的撤销动作，恢复前版 |
| tasks.build | course_id | 从课程本地数据生成任务；返回真实 job，原子保存生成回执与会话归属 |
| jobs.cancel | job_id | 仅会话发起的后台任务；排队可直接取消，运行中须等 worker 检查点 |
| tasks.feedback | task_id、expected_revision、outcome；可选 actual_minutes/difficulty/note | 记录明确报告的本地学习结果；先核对任务版本，反馈和回执原子提交，不改学校提交状态 |

除 availability.read 外，本表六项均 effects=write。Harness 不把它们作为并发安全只读调用。严格 schema 禁止模型提供 approved、任意用户身份或 URL；任务反馈未报告的时间与难度应留空，不从估算补造。

plans 工具要求完整本机工作区范围并检查计划引用。任务生成需要明确课程且通过范围校验。生成/反馈幂等标识由网关 turn_id 与 call_id 组成，模型不能指定。已有生成 job 完成后重放同一调用仍返回原 job；同范围尚在运行的另一个请求可复用 job。job 归属写在业务库，桥接响应丢失或会话日志恢复不会丢失权限依据。返回 queued 表示仍排队/运行，不表示业务成功。

可信浏览器 GET /api/v1/agent/conversations/{cid}/actions 恢复动作列表；POST /api/v1/agent/conversations/{cid}/actions/{id}/accept 使用 operation=apply/revoke 与 Idempotency-Key。入口校验浏览器所有权与来源，实际变更由 Harness 工具执行。对话差异卡片和精确文字“按这个来”“撤销刚才安排”先接受具体方案，再提交 action_id。服务器校验该动作并将 accepted_action 与轮次一起持久化，作为运行时可信上下文。历史回执不代表此刻生效状态，需另读 plans.read/日历。

对话复用 JobProgressInline 展示真实进度、错误和取消请求状态。取消按钮是用户对具体 job 的直接操作，调用原本地业务接口。对话中的重新生成可使用 tasks.build；内联组件在对话中关闭通用重试按钮，避免新 job 未加入会话归属。

## 验证与限制

实际 Harness、真实 Python 网关和领域引擎验证入口：`apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py`。最新证据 `var/agent-integration-qbk_diui/evidence.json`：41 次受控模型调用、16 项模型可见工具；查询/原文/第二轮/取消/服务重启，计划预览/接受/应用/撤销/重放，任务生成/读取/取消、真实 worker 完成，以及先读取任务版本后反馈均通过。

`docs/assets/agent-browser-m2.json` 记录桌面方案生效、窄屏刷新草稿恢复和文字撤销。完整同会话浏览器补充证据见 docs/assets/agent-browser-full.json，涵盖本周三晚间、日历任务标题、后台取消、窄屏反馈、撤销与刷新。干净目录重建和全量回归已通过，逐项验收及限制见 agent-acceptance.md。

自动化入口：在 apps/api 运行 pytest tests/test_agent_tools.py tests/test_agent_gateway.py tests/test_agent_plan_actions.py tests/test_agent_job_actions.py tests/test_agent_feedback.py。固定 Harness 版本及许可证见 agent-runtime-version.json，运行接口见 agent-api.md。所有证据使用合成校园数据和受控模型；真实校园账户、付费模型回答质量和普通学生试点未验证。

本周晚间约束可用 unavailable_evenings（ISO星期1–7），仅适用于 this_week。服务端按18:00–次日00:00排除并明示，已过去的星期要求澄清。Agent预览给当天新增安排留10分钟检查时间，仍需执行前检查未开始；不移动锁定块。
