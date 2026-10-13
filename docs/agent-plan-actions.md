# 计划动作与授权回执

当前实现位于 `apps/api/app/agent_bridge/plan_actions.py`，是实际业务数据库服务。已注册计划工具和可信浏览器接受入口；前端差异卡片、具体接受、精确文字承接及实际Harness/浏览器闭环已验证，见 agent-acceptance.md。

## 事务与身份边界

`AgentPlanAction` 与 Plan 存在同一数据库。迁移 `a13f47c908de` 只新增动作表与会话索引。会话身份由网关提供；后续公开入口必须先用浏览器 cookie 校验会话所有权，不接受模型声明身份。

`preview(db, conversation_id, request_key, start, end, blocked=..., windows=...)` 调用原确定性引擎。候选及其动作在同一事务提交；临时时间约束只存入动作快照，不写长期可用时间。request_key 应使用可信 turn_id 与 call_id 的组合。相同标识和参数回读原动作；标识复用但参数不同返回 IDEMPOTENCY_CONFLICT。

预览保存完整候选、当前生效计划、输入指纹、临时约束及 added/removed/retained 时间块。前端应展示这些具体内容，并提供容量缺口、锁定块和原始计划状态，不把模型总结当成授权对象。

`approve(db, conversation_id, action_id)` 只供经过会话所有权验证的直接浏览器接受入口调用。它不属于模型工具。动作默认 pending，批准变为 approved；模型参数 approved=true 不构成授权。批准使用条件更新，不能把已经完成的动作改回 approved。

`execute(db, conversation_id, action_id, kind)` 只消耗 approved 动作。领取动作、实际 apply/revoke、完成回执同事务提交；中途异常全部回滚。重复执行已完成动作返回原回执，不再次改变计划。回执是历史操作结果；需要了解此刻生效计划时必须另读 plans.read，不能把历史回执当作最新状态。

确认与执行均重新检查 30 分钟有效期、候选完整内容、当前生效计划（包含锁定块）和任务输入指纹。新增时间块已经开始也拒绝应用。变化返回 PLAN_STALE，要求重新预览；不偷偷更新已被接受的方案。

`prepare_revoke(db, conversation_id, applied_action_id, request_key)` 只能为本会话已应用且仍生效的方案生成撤销动作。动作同时绑定将恢复的前版快照；撤销也经过 approve/execute。新请求标识允许在旧撤销预览过期后重新检查。业务引擎恢复实际前版，不删除任务或修改学校提交状态。

## 已验证与剩余工作

`tests/test_agent_plan_actions.py` 的 10 项实际数据库测试覆盖：预览持久化和幂等、缺少授权拒绝、跨会话拒绝、应用重放、撤销恢复、任务变化、候选篡改、生效锁定块变化、授权过期、业务变更后异常回滚和重试。

与临时约束及原计划接口合跑 30 项通过。独立合成库 `var/agent-actions-migration-check.db` 从空库执行全部迁移成功，alembic check 未发现模型与迁移差异。没有迁移真实学生数据库。

工具严格schema、工作区范围、可信浏览器接受、差异卡片、任务/job能力及实际Harness与浏览器验证已落地。当前模型可见集合为16项，工具说明见 agent-tools-implemented.md。真实账户/模型质量仍未验证。
