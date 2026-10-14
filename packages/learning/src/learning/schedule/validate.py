"""独立计划校验器：对任意计划复核全部硬约束。

与引擎解耦，用于：
- 约束测试（不依赖某一份固定输出，而是检查计划本身是否合法）；
- B 在持久化/应用候选计划前的复核；
- 局部重排（D5，后续批次）复用同一套检查。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

from ..models import Availability, LearningTask, Plan, deadline_of
from ..timeutil import (
    expand_availability,
    minutes_between,
    parse_dt,
    resolve_tz,
    split_by_date,
)


@dataclass
class Violation:
    code: str
    message: str
    task_ids: list[str] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "task_ids": list(self.task_ids),
            "block_ids": list(self.block_ids),
        }


def validate_plan(
    plan: Plan,
    tasks: list[LearningTask],
    availability: Availability,
    options: Any = None,
) -> list[Violation]:
    """返回违规清单；为空表示计划满足全部硬约束。"""
    tz = resolve_tz(availability.timezone)
    horizon_start = parse_dt(plan.horizon_start)
    horizon_end = parse_dt(plan.horizon_end)
    days = expand_availability(availability, horizon_start, horizon_end)
    day_map = {day.date: day for day in days}

    index: dict[str, LearningTask] = {}
    for task in tasks:
        index.setdefault(task.id, task)

    violations: list[Violation] = []
    timed: list[tuple[Any, _dt.datetime, _dt.datetime]] = []
    for block in plan.blocks:
        try:
            start, end = parse_dt(block.starts_at), parse_dt(block.ends_at)
        except ValueError:
            violations.append(
                Violation("BLOCK_INVALID_TIME", f"时间块 {block.id} 时间无法解析", block_ids=[block.id])
            )
            continue
        timed.append((block, start, end))

    # 1) 不重叠（扫掠：跟踪已见最大结束时刻，避免“中间块较短”时漏检）
    ordered = sorted(timed, key=lambda x: (x[1], x[0].id))
    sweep_block = None
    sweep_end: _dt.datetime | None = None
    for block, start, end in ordered:
        if sweep_block is not None and sweep_end is not None and start < sweep_end:
            violations.append(
                Violation(
                    "BLOCK_OVERLAP",
                    f"时间块重叠：{sweep_block.id}（{sweep_block.starts_at}~{sweep_block.ends_at}）"
                    f"与 {block.id}（{block.starts_at}~{block.ends_at}）",
                    block_ids=sorted({sweep_block.id, block.id}),
                )
            )
        if sweep_end is None or end > sweep_end:
            sweep_end = end
            sweep_block = block

    # 2) 位于可用窗口；3) 截止时间；4) 完成/取消/平台已提交任务不再安排
    for block, start, end in timed:
        day = day_map.get(start.astimezone(tz).date())
        inside = day is not None and any(g0 <= start and end <= g1 for g0, g1 in day.gaps)
        if not inside:
            violations.append(
                Violation(
                    "BLOCK_OUTSIDE_WINDOW",
                    f"时间块 {block.id} 不在任何可用窗口内（{block.starts_at}~{block.ends_at}）",
                    task_ids=[block.task_id],
                    block_ids=[block.id],
                )
            )
        task = index.get(block.task_id)
        if task is None:
            violations.append(
                Violation(
                    "UNKNOWN_TASK_BLOCK",
                    f"时间块 {block.id} 引用了未知任务 {block.task_id}",
                    task_ids=[block.task_id],
                    block_ids=[block.id],
                )
            )
            continue
        if task.due_at:
            try:
                deadline = deadline_of(task.due_at, task.due_precision)
            except ValueError:
                deadline = None
            if deadline is not None and end > deadline:
                violations.append(
                    Violation(
                        "BLOCK_AFTER_DEADLINE",
                        f"任务 {task.id} 的时间块 {block.id} 超出截止（{task.due_at}，精度 {task.due_precision}）",
                        task_ids=[task.id],
                        block_ids=[block.id],
                    )
                )
        if not block.locked and task.status != "pending":
            violations.append(
                Violation(
                    "NON_PENDING_TASK_SCHEDULED",
                    f"任务 {task.id} 状态为 {task.status}，仍有新安排（块 {block.id}）",
                    task_ids=[task.id],
                    block_ids=[block.id],
                )
            )
        if not block.locked and task.platform_submitted:
            violations.append(
                Violation(
                    "PLATFORM_SUBMITTED_TASK_SCHEDULED",
                    f"任务 {task.id} 平台已提交（PD-3：不参与排程），仍有新安排（块 {block.id}）",
                    task_ids=[task.id],
                    block_ids=[block.id],
                )
            )

    # 5) 每日上限（含锁定块）
    used: dict[_dt.date, int] = {}
    for _block, start, end in timed:
        for day, minutes in split_by_date(start, end, tz):
            used[day] = used.get(day, 0) + minutes
    for day in sorted(used):
        daily = day_map.get(day)
        limit = daily.daily_limit_minutes if daily else None
        if limit is not None and used[day] > limit:
            violations.append(
                Violation(
                    "DAILY_LIMIT_EXCEEDED",
                    f"{day.isoformat()} 安排 {used[day]} 分钟，超出每日上限 {limit} 分钟",
                )
            )

    # 6) 前置关系：前置先完成（前提前置有安排时，后继首块不得早于前置末块）
    first_start: dict[str, _dt.datetime] = {}
    last_end: dict[str, _dt.datetime] = {}
    for block, start, end in timed:
        first_start[block.task_id] = min(first_start.get(block.task_id, start), start)
        last_end[block.task_id] = max(last_end.get(block.task_id, end), end)
    for task_id in sorted(first_start):
        task = index.get(task_id)
        if task is None:
            continue
        for prereq_id in task.prerequisite_ids or []:
            prereq = index.get(prereq_id)
            if prereq is None:
                continue
            if prereq.status == "completed":
                continue
            if prereq.status == "cancelled":
                violations.append(
                    Violation(
                        "PREREQUISITE_UNAVAILABLE",
                        f"任务 {task_id} 的前置任务 {prereq_id} 已取消，无法满足",
                        task_ids=[task_id, prereq_id],
                    )
                )
                continue
            if prereq.platform_submitted:
                # PD-3：平台已提交任务不排程，其前置视同已满足（取消除外，已在上分支报告）；
                # 与 engine.satisfied / revise.satisfied_now 同口径
                continue
            if prereq_id not in last_end:
                violations.append(
                    Violation(
                        "PREREQUISITE_UNSCHEDULED",
                        f"任务 {task_id} 已安排，但前置任务 {prereq_id} 未被安排",
                        task_ids=[task_id, prereq_id],
                    )
                )
                continue
            if first_start[task_id] < last_end[prereq_id]:
                violations.append(
                    Violation(
                        "PREREQUISITE_ORDER_VIOLATED",
                        f"任务 {task_id} 的开始时间早于前置任务 {prereq_id} 的完成时间",
                        task_ids=[task_id, prereq_id],
                    )
                )

    # 7) 时长完整性（新安排块之和必须等于预计时长，不允许半排）
    minutes_by_task: dict[str, int] = {}
    for block, start, end in timed:
        if not block.locked:
            minutes_by_task[block.task_id] = minutes_by_task.get(block.task_id, 0) + minutes_between(start, end)
    for task_id, minutes in sorted(minutes_by_task.items()):
        task = index.get(task_id)
        if task is None:
            continue
        if minutes != task.estimated_minutes:
            violations.append(
                Violation(
                    "DURATION_MISMATCH",
                    f"任务 {task_id} 已安排 {minutes} 分钟，与预计 {task.estimated_minutes} 分钟不一致",
                    task_ids=[task_id],
                )
            )

    # 8) 锁定块原样保留
    locked_in_plan = {block.id: block for block in plan.blocks if block.locked}
    for block in (getattr(options, "locked_blocks", None) or []):
        kept = locked_in_plan.get(block.id)
        if (
            kept is None
            or kept.starts_at != block.starts_at
            or kept.ends_at != block.ends_at
            or kept.task_id != block.task_id
        ):
            violations.append(
                Violation(
                    "LOCKED_BLOCK_NOT_PRESERVED",
                    f"锁定块 {block.id}（{block.starts_at}~{block.ends_at}）未被原样保留",
                    task_ids=[block.task_id],
                    block_ids=[block.id],
                )
            )

    return violations
