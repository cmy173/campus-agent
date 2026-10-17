# 【C→B】M1 收口轮转答（可直接粘贴给 B）

发件：智能体 C（分支 `agent/c`，worktree `.worktrees/agent-c`）｜日期：2026-10-10

---

【C→B】M1 收口轮 C2 复跑结论 + C11 接口对账，三部分：

① **C2 真实模式联调（第五次复跑，你的整改并入 `main@3156550` 后）：10/10 全绿，此前 4 项偏差逐项转绿**：

- `B-PUT-AVAILABILITY` ✅ `PUT /preferences/availability` 写入并回读一致；
- `B-PLAN-STALE-DETAILS` ✅ details 键现含 `input_revision`（附带 `plan_id`）；
- `B-AUTH-EXPIRED-DETAILS` ✅ details 键含 `platform`，`action` 含 `entry_url`+`message`；
- `B-POST-COURSES` 等写端点 ✅ `POST /courses`、`POST /assignments`、`PATCH /assignments/{id}`、`PUT /preferences/availability` 均已实现（路由级核对；`createCourse` 已由 E2E 实测成功）。
- **台账提醒**：偏差清单现仅剩 1 条提示项 `B-COURSE-WRITE-IMPLEMENTED`（"POST /courses 现已实现，请从台账移除"）——这是 C 侧 E2E 的自更新提示，你从台账/交接文档把 `B-POST-COURSES` 一笔勾掉即可，无需其他动作。
- 如实边界：`POST /assignments`、`PATCH /assignments/{id}` 前端页面目前未消费，本次仅路由级核对、未逐端点断言；下轮前端接入时会补联调断言。
- `check` 哨兵现状：zykt 仍无检查适配器 → `502 DEPENDENCY_UNAVAILABLE` 如实返回（不造假 ok）；A 桥接后 E2E 同一用例自动接受 200 路径。
- 证据：`var/c2-e2e-report.json`（第五次运行）+ `apps/web/docs/C2-真实模式联调记录.md`（§2 第五次运行表、§3 各偏差复验批注）。

② **CR004（`GET /tasks/{task_id}`）对账与 C 侧恢复**：C11 曾按"不消费未冻结面"将该端点从 client/mock 移除；CR004 补录进契约后，C 侧已恢复消费（`client.ts getTask` + mock 路由 + 契约测试 + E2E 断言：真实后端 200/404 均验证）。提请知悉一处时间线：CR004 审核记录写"C 客户端在用"——严格说当时 C11 已移除、补录后 C 才恢复在用；若审核记录需要校准可加一行注记，不影响任何实现结论。

③ **C11 相关接口对账（供你/协调者裁定，均不阻塞交付）**：

1. `PATCH /tasks/{id}` 后 `fact_source` 仍为 `inferred`（未自动置 `user`），实测证据：任务 `tsk_0f3a91`；C 界面按返回值如实标注"推断"。**建议**：契约明确"人工修改是否置 user"口径（B 裁定或 CR）。
2. 完成标准 `completion_criteria`：D 侧计算但未持久化、接口未返回（契约无字段）；C11 界面已如实提示"当前接口不返回完成标准"。**建议**：后续经 CR 增补可选字段。
3. `BuildTasksRequest` 仅 `course_id`（契约与后端一致）；C 的单资源入口按整门课程触发并在界面明示，不伪造单资源参数。如需单资源范围，同样走 CR。
4. `scope=today`（GET /tasks）：C 侧已移除 mock 私有参数，改前端 `filterTodayTasks` 过滤，契约参数面不变。
5. `DELETE /assignments/{id}`：C 侧多实现的幽灵端点已移除（契约与后端均无）。
6. mock 未镜像项（如实标注）：真实后端解析完成自动入队 `build_tasks`（`worker.py` reason=`"parse"`），mock 不模拟该联动——C 侧文档已注明，E2E 首次 `refs=0` 属幂等正常。

C 侧收口轮状态：tsc 0、55/55 测试、构建成功、真实 E2E 10/10（第五次）。收口轮交接文档见 `apps/web/docs/C-M1收口轮交接.md`。
