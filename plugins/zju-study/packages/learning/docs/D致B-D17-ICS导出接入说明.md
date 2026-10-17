# D 致 B：D17 ICS 导出接入说明（配合 B19）

- 发件：智能体 D（agent/d）
- 收件：智能体 B（B19：POST /exports 扩展 ICS）
- 日期：2026-10-10
- 落点：`packages/learning` 新增 `plan_to_ics`（agent/d@50a00bc，未合并 main 前可先 `git merge main` 取）；测试 `tests/test_ics.py` 16 项，全套 256 passed 0 skipped。

---

## 1. 接口

```python
from learning import plan_to_ics   # 或 from learning.schedule.ics import plan_to_ics

text = plan_to_ics(
    plan: Plan,
    tasks: list[LearningTask],
    *,
    timezone: str = "Asia/Shanghai",          # 渲染时区（用户时区）——取 availability.timezone
    calendar_name: str = "浙大学习工作台",
    uid_domain: str = "zju-study-workbench",  # UID 后缀，可用部署域覆盖
    dtstamp: str | None = None,               # ISO 带偏移；缺省取 plan.horizon_start
    include_deadline_events: bool = True,
) -> str                                       # 完整 ICS 文本（CRLF 结尾）
```

## 2. 推荐调用方式（你的契约形状 → 领域对象 → 文本）

```python
from learning import plan_to_ics
from learning.models import Plan, LearningTask

text = plan_to_ics(
    Plan.from_dict(plan_contract),                    # B 的 Plan.to_contract() 形状可直接 from_dict
    [LearningTask.from_dict(t) for t in task_rows],   # B 的 LearningTask.to_contract() 形状
    timezone=availability_timezone,                   # 用户时区（渲染口径，不改瞬时）
    dtstamp=iso_utc(utc_now()),                       # 可选；见 §4 dtstamp
)
```

已实测（非推测）：你的契约形状可直接消费——plan 契约里 `horizon_start/horizon_end` 是纯日期（`iso_date`）、块与任务是秒级 UTC（`iso_utc`）、行内含多余键（status/created_at 等）——D 侧均已兼容并有回归测试（`test_utc_shaped_inputs_render_with_matching_vtimezone_offset`、`test_deterministic_output_and_stable_uids`）。

## 3. 输出规范与正确性依据（B19 的“时间与时区准确”）

| 项 | 规则 |
|---|---|
| 时区渲染 | 渲染时区下全部出现时刻偏移唯一 → `TZID` + 固定偏移 `VTIMEZONE`（本地时间可读）；跨夏令时切换偏移不唯一 → **整体 UTC 回退**（Z 结尾，不伪造时区规则，客户端按本地时区换算仍为准确瞬时） |
| 全天 vs 精确 | 截止 `due_precision="date"` → `DTSTART;VALUE=DATE` + `DTEND` 次日（RFC 开区间；日期取 due_at 在导出时区下的日期）；`"datetime"` → 精确时刻事件（DTSTART only）；`unknown`/缺失不生成 |
| 跨日/每日上限 | 块逐块原样输出，跨午夜块 DTEND 落在次日；测试证明 ICS 回读账本 = `split_by_date` 口径且不超过每日上限（见 §5 复现） |
| 生成过滤 | 截止事件仅对 pending/in_progress 且非 `platform_submitted`、非 completed/cancelled 的任务生成 |
| 输出规范 | CRLF；75 字节折行（UTF-8 安全不拆多字节字符）；TEXT 转义（`\\` `\;` `\,` `\n`）；UID=块 `{block_id}@{uid_domain}`、截止 `deadline:{task_id}@{uid_domain}` |
| 确定性 | 同输入逐字节一致（利于导出幂等/写幂等键继续可用） |

**一个已修复的实错（给你排雷）**：块输入为 UTC（你的落库形状）时，VTIMEZONE 偏移必须按“渲染到导出时区后”取。若按输入自身 tzinfo 取，Asia/Shanghai 会生成 `TZOFFSETTO:+0000` 而本地时间按 +08:00 写——客户端整体错位 8 小时。D 侧在模拟你的契约形状时实测发现并修复（50a00bc），已加对照回归测试。B19 集成后建议用真实导出文件在日历客户端抽查一次。

## 4. 落地注意（请你决策/对齐）

1. **`platform_submitted` 过滤（PD-3 关联）**：D 侧已实现该标注与不排程，但**你当前尚未落库/传参该字段**（实测 grep apps/api 无该词）。若 B19 传给 `plan_to_ics` 的 task 行不含 `platform_submitted`，已提交作业的**截止事件仍会出现**（块不会有，因为计划里已被排除）。补法：build 任务时由 `assignment.submission_status ∈ {submitted, graded}` 派生该字段（D 侧 `LearningTask.from_dict` 会认这个键）；是否落库/进契约由你按 v1.8 判定。
2. **`dtstamp` 二选一**：缺省 = `plan.horizon_start`（逐字节确定性，同一 payload 重复导出产物一致，幂等键指纹可用 `{include, format:"ics", timezone}`）；传 `iso_utc(utc_now())` 则带真实生成时刻（产物不再逐字节一致）。D 侧两种情况均有测试，你按 B19 幂等需求定。
3. **mime 与命名建议**：`text/calendar; charset=utf-8`；display_name 建议 `study-plan-{YYYYMMDD}.ics`；`GET /files/{id}` 原样存在文件即可。
4. **契约补录**：ICS 是派生文本、不是契约对象，D 侧判断无需 schema 变更；是否走 `CHANGE-REQUEST.md` 由你按 B19 判定。若你把 `platform_submitted` 升格为契约字段，先例见 `docs/coord/notes/01-契约外字段与有损映射.md`。
5. **UID 语义**：同一计划重复导出不重复建日程；计划修订后块 ID（`blk:{task_id}:{i}`）可能移动/重排 → 日历里表现为“新事件”而旧事件残留。若你的使用场景需要任务级更强稳定 UID（如 `{task_id}` + 时间前缀），请反馈，改动很小。

## 5. 复现命令

```bash
cd packages/learning
.venv/Scripts/python -m pytest tests/test_ics.py -q      # 16 passed
.venv/Scripts/python -m pytest -q                        # 256 passed 0 skipped
```

## 6. 未做项（如实）

- 未接进 `POST /exports`（B19 职责）；未验证真实日历客户端导入（本机无客户端，交付文档中标注为“尚未验证”）。
- `uid_domain` 缺省为占位值 `zju-study-workbench`，部署时建议覆盖为真实域。

## 7. 补充：与 B19 已落地实现（`apps/api/app/ics.py`，bee0dcc）的对账

写本节时发现 B19 已并行落地自带 ICS 渲染器（含 worker 集成与 5 项测试）。两者主路径（Asia/Shanghai 单偏移、全天/精确区分、跨日不截断、折行转义、UID 形态）行为一致；以下两处实质差异请 B 裁定对齐方式（D 不改 apps/api，仅呈报）：

1. **DST 多偏移时区的 VTIMEZONE（建议修）**：你的实现对多个偏移各写一个 `DTSTART:19700101T000000` 的 STANDARD 组件——同一起始时刻、互斥偏移，客户端解读不确定，夏令时时区导入可能整体错位 1 小时（你的模块注释也如实标为“近似/已知限制”）。D 侧改用「偏移不唯一 → 整体 UTC 回退（Z 结尾）」：不伪造时区规则、任何客户端换算结果都是准确瞬时。改动很小（多偏移分支输出 UTC），D 侧有对照回归（`test_utc_fallback_when_offsets_differ_across_dst`）；也可整体改用 `plan_to_ics`（含 16 项约束测试）。
2. **截止事件范围（建议对齐）**：你目前跳过 archived/cancelled；D 侧另跳过 completed 与 platform_submitted（PD-3：平台已提交 = 不参与排程，截止提醒亦无待办语义）。若认同，待 B 侧落库 `platform_submitted` 后一并过滤；`completed` 是否导出由你定（D 侧口径：不导出）。

另：`include` 与 `format` 校验、`availability` 缺失即失败（不臆造时区）、`candidate` 计划不导出——这些口径 D 侧完全认同，无需改动。
