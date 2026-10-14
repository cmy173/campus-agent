# 本机定时任务

实现使用应用自身的真实后台线程、同一个 Agent journal SQLite 和唯一 `Store.enqueue → Runtime.start → Harness` 链路。没有第二套模型循环，不安装系统服务，不唤醒电脑。模型通过五个公开工具完成自然语言请求，API/工具共享持久状态。

## 用户行为

- 用户可以说“明天上午九点查通知”“每周一九点整理作业”“每十分钟检查资料处理结果”。模型根据当前日期与明确时区转换为时间规则，遇到日期歧义应先问清楚。创建结果必须显示确切日期/时间、IANA 时区、下次执行时间、错过策略和 ID。
- 时间规则：`at` 为带 UTC 偏移的未来 ISO 时间；`after` 为至少一秒延迟；`every` 为至少 60 秒固定间隔；`daily` 为本地 `HH:MM:SS`；`weekly` 增加不重复的 ISO 星期数，周一 1 至周日 7。所有规则均携带 `time_zone`。
- `latest`（默认）：应用重启后重复任务只补最新一次，跳过较老积压；一次任务补一次。`skip`：重启时错过的时间与尚未入队的遗留发生全部跳过；下次未来发生照常执行。Windows 睡眠没有进程重启时，恢复后按最新一次处理；不是睡眠检测器。
- 夏令时不存在的本地时间跳过该天；重叠时间仅执行较早的那个时刻一次。固定 `every` 按经过秒数，`daily`/`weekly` 按本地墙钟，不把 86400 秒当每天同一时刻。
- 编辑使用 `revision` 比较版本，调度推进也改变版本；冲突先刷新。时间编辑重新定位未来时间。停用/删除/编辑取消尚未入队发生。已经入队或执行的轮次保留历史，停止该轮使用现有取消按钮。取消一个轮次不代表停用所有未来重复，用户说“以后不再提醒”应停用或删除任务。
- 恢复已停用重复任务从当前时间计算未来发生；过去的一次任务需要指定新的未来时间。删除为可追溯软删除，不继续调度，保存发生和实际轮次状态。

## 工具与 HTTP

`apps/agent-runtime/campus-schedules.mjs` 使用公开 ToolRegistry 注册：

| 工具 | 参数 |
|---|---|
| `schedule_create` | `title,prompt,timing,missed_policy?` |
| `schedule_list` | 无；包含停用任务、下次 UTC/本地时间和版本 |
| `schedule_update` | `id,revision,title?,prompt?,timing?,missed_policy?,enabled?` |
| `schedule_delete` | `id,revision` |
| `schedule_history` | `id`；发生状态与实际 `turn_status`，入队不是完成 |

公开 HTTP 路由位于 `/api/v1/agent/conversations/{cid}/schedules`：GET 列表、POST 创建、`/{sid}` PATCH 编辑/DELETE 删除、`/{sid}/history` GET 历史。写操作必须 `Idempotency-Key`；PATCH/DELETE 必须 revision。所有端点使用现有本机 Origin 与 owner cookie 检查。内部 `POST /api/v1/agent/internal/schedules` 必须有效本轮 Bearer，作用域只允许自身会话；停止后的 token 立即拒绝。请求 schema 由 Pydantic 自动进入 OpenAPI。

调度工具写入的 request receipt 与任务变化在同一 SQLite 事务。重复相同工具 call/request key 返回原始 receipt；同一 key 不同内容拒绝。工具本身由已有 native pre-execute lease 计入工具预算。

## 发生身份与断点

发生 ID 由 `schedule_id + UTC scheduled_at 微秒值` 确定，数据库还约束 `(schedule_id, scheduled_at)` 唯一。任务的下次时间推进和发生快照写在同一事务。调度器使用 `schedule:{occurrence_id}` 作为现有 Store 的请求键；并发/恢复均不能为同一发生创建第二个轮次。

1. `pending`：发生已持久化，但未入队；会话忙则保留原发生并重试。未来同一重复的新发生会取代仍未入队的旧积压。
2. `dispatching`：已开始派发。重启检查唯一请求键；没有 turn 则可回到 pending；已有 turn 则关联 admitted，**不再次启动模型**。
3. `admitted`：已有唯一 turn。用户取消、失败、服务重启中断都显示其实际状态，不自动重放。入队后、启动前进程崩溃可能导致任务未执行；这会明确显示 interrupted/RECOVERED_NO_REPLAY，用户可以新建一次请求。用不重复执行换取可见的未完成状态。
4. `skipped/cancelled/failed`：分别为错过/被编辑停用或删除/不可重试入队失败。Runtime.start 抛错时轮次失败，标记 SCHEDULE_START_FAILED，不伪造完成。

这证明**发生 admission 去重**，不声称任意网站外部副作用恰好执行一次。入队后状态不确定不自动重放；浏览器对外提交继续沿用原确认与 receipt 防重。

## 上游复用决策

固定 `@deepseek-ai/dsh-schedule 0.2.0-rc.2` 的本地 README 与公开类型已审阅。其 README `Use this package` 明示 SDK-only 无法单独挂载，因为依赖 Host Web Session controller；`Storage, dispatch, and ownership` 明示 Session flush 与 task put 为分开的持久写，crash 后可以重复交付。安装实现 `lib/index.js` 的 `sessions.flush`（当前约 1597 行）和工具描述 `Delivery can repeat after a crash`（约 1893 行）相互印证。因此采用薄 Python 持久 admission 政策，并继续复用原唯一 Harness 执行链。**不要同时挂载 experimental schedule bundle**，否则工具名和调度事实源冲突。

## 集成调用点

1. `campus-plugin.mjs` 导入 `mountSchedules`，在 `apply(ctx)` 注册其他工具后、CampusReady 之前 `mountSchedules(ctx, bridge)`。
2. `main.py` 加载 `agent_schedules.router`，prefix 为既有 API。API lifespan 在 `agent_runtime.store.recover()` **之后**调用 `get_scheduler(agent_runtime).start()`。
3. 关闭先 `get_scheduler(agent_runtime).close()`，再 `agent_runtime.close()`，防止关闭期间又启动新模型。可将关闭放入 Runtime.close 最前；不要启动多个 scheduler。
4. 前端只使用以上稳定 API，列表与具体时间默认由聊天工具结果呈现。后续可用任务工作区，不造第二主入口。

## 当前验证与限制

- 专属单元/线程/API测试使用真实后台调度器和真实 Store；执行器受控，无付费模型。覆盖实际到时派发、忙会话等待、停用、版本冲突、跨会话授权、已停止 token 拒绝、派发前/入队后断点、启动失败、latest/skip、DST。
- `tools/verify_scheduler.py` 真正启动两个独立 Python 进程，首个进程在后台线程已持久入队时 `os._exit(73)`，第二个进程重启真实调度器。证据 `var/v2-scheduler-eqnbh19_/evidence.json`：原发生只一个 interrupted turn、不重放；重复任务只补一次并执行成功；skip 无执行。执行器为受控，不冒充真实模型。
- 产品接入现已完成（main 生命周期和 campus 插件）。`tools/verify_schedule_runtime.py` 完整实测通过，证据 `var/v2-schedule-runtime-lm4no243/evidence.json`：自然语言请求经真实 Harness 调用 schedule_create；真实后台线程产生第二轮；真实 SDK write/read/publish_file 写入、读取并交付 Markdown；下载字节核对；两个独立 API 进程前后 history 同一个发生/同一个 turn，重启后无新增模型调用。共 6 次本地受控模型调用，实际工具和调度器不模拟；不等同真实官方模型试用。
- 仅单 API 进程拥有本机调度器；不承诺多实例分布式租约。没有 cron、系统服务、关机执行、睡眠检测或桌面通知，聊天结果和历史为交付通道。本轮合同没有要求任意 cron 语法。
