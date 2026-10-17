"""D5 局部重排与解释：在上一次计划的基础上，仅根据变化与必要连锁影响调整。

用法（由 B 调用并持久化，模块不接触数据库）：

    revision = revise_plan(previous_plan, tasks, availability, options)

语义：
- 保留：仍满足新硬约束的原有时间块原样保留（含时间与块 ID）；任务完成/取消/移除后旧块释放；
- 调整：仅释放失效块（时间已过、可用窗口变化、超出新截止、与锁定块冲突、任务时长变化）
  并补排缺口；后继任务受“前置先完成”约束按需连带调整（连带原因记录为 DEPENDENCY_CASCADE）；
- 冲突：锁定块与新硬约束冲突时计入 `conflicts` 并将 `fully_valid` 置为 False，
  不假装计划完全有效；冲突同时镜像到 `plan.warnings`；
- 未安排：四类原因口径与引擎一致（总时间不足 / 截止前窗口不足 / 任务太长不可拆分 / 依赖冲突）；
- 摘要：`changes.summary_lines` 为供 C 展示的中文变化说明（如“10月13日 19:00 调整至 …”）。

启发式说明：与 D4 相同——同输入、同配置、同输入修订号得到可重复结果；首版不承诺全局最优，
失败表述为“当前策略未能安排”。`summary.capacity_minutes` 表示本次修订放置前（扣除锁定块与
保留块占用后）可用于安排的总容量。
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field, replace
from typing import Any

from ..models import (
    Availability,
    LearningTask,
    Plan,
    PlanBlock,
    UnassignedItem,
    WarningItem,
    deadline_of,
)
from ..timeutil import (
    expand_availability,
    fmt_dt,
    minutes_between,
    parse_dt,
    resolve_tz,
    split_by_date,
)
from .engine import (
    ReasonCode,
    ScheduleOptions,
    WarningCode,
    _Space,
    _find_cycle_paths,
    _try_place,
)


class ChangeKind:
    ADDED = "added"
    REMOVED = "removed"
    MOVED = "moved"


class ChangeReason:
    NEW_TASK = "NEW_TASK"
    TASK_CHANGED = "TASK_CHANGED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_REMOVED = "TASK_REMOVED"
    TASK_INVALID = "TASK_INVALID"
    PLATFORM_SUBMITTED = "PLATFORM_SUBMITTED"
    BLOCK_EXPIRED = "BLOCK_EXPIRED"
    DEADLINE_CHANGED = "DEADLINE_CHANGED"
    WINDOW_CHANGED = "WINDOW_CHANGED"
    BLOCK_CONFLICT = "BLOCK_CONFLICT"
    DEPENDENCY_CASCADE = "DEPENDENCY_CASCADE"
    LOCKED_REMAINDER = "LOCKED_REMAINDER"
    REFIT_FAILED = "REFIT_FAILED"
    REARRANGED = "REARRANGED"


REASON_TEXT: dict[str, str] = {
    ChangeReason.NEW_TASK: "新增任务",
    ChangeReason.TASK_CHANGED: "任务信息变化",
    ChangeReason.TASK_COMPLETED: "任务已完成",
    ChangeReason.TASK_CANCELLED: "任务已取消",
    ChangeReason.TASK_REMOVED: "任务已移除",
    ChangeReason.TASK_INVALID: "任务数据无效",
    ChangeReason.PLATFORM_SUBMITTED: "平台已提交，不参与排程",
    ChangeReason.BLOCK_EXPIRED: "原安排时间已过",
    ChangeReason.DEADLINE_CHANGED: "超出新的截止时间",
    ChangeReason.WINDOW_CHANGED: "可用时间发生变化",
    ChangeReason.BLOCK_CONFLICT: "与锁定块或其它安排冲突",
    ChangeReason.DEPENDENCY_CASCADE: "前置任务变化（连带调整）",
    ChangeReason.LOCKED_REMAINDER: "该任务已有锁定安排",
    ChangeReason.REFIT_FAILED: "剩余部分无法放入，整体重排",
    ChangeReason.REARRANGED: "为其他任务让位",
}


class ConflictCode:
    """锁定块与新硬约束冲突的分类（出现即 fully_valid=False）。"""

    LOCKED_BLOCK_INVALID = "LOCKED_BLOCK_INVALID"
    LOCKED_BLOCK_OUTSIDE_HORIZON = "LOCKED_BLOCK_OUTSIDE_HORIZON"
    LOCKED_BLOCK_OUTSIDE_WINDOW = "LOCKED_BLOCK_OUTSIDE_WINDOW"
    LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT = "LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT"
    LOCKED_BLOCK_OVERLAP = "LOCKED_BLOCK_OVERLAP"
    LOCKED_DEPENDENCY_CONFLICT = "LOCKED_DEPENDENCY_CONFLICT"


class RevisionWarningCode:
    PLAN_HAS_CONFLICTS = "PLAN_HAS_CONFLICTS"


@dataclass
class BlockChange:
    kind: str
    task_id: str
    task_title: str
    reason: str
    before: PlanBlock | None = None
    after: PlanBlock | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "reason": self.reason,
            "reason_text": REASON_TEXT.get(self.reason, self.reason),
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
        }


@dataclass
class RevisionChanges:
    entries: list[BlockChange] = field(default_factory=list)
    new_unassigned: list[UnassignedItem] = field(default_factory=list)
    resolved_unassigned: list[str] = field(default_factory=list)
    unchanged_block_count: int = 0
    summary_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "new_unassigned": [u.to_dict() for u in self.new_unassigned],
            "resolved_unassigned": list(self.resolved_unassigned),
            "unchanged_block_count": self.unchanged_block_count,
            "summary_lines": list(self.summary_lines),
        }


@dataclass
class RevisionConflict:
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


@dataclass
class PlanRevision:
    plan: Plan
    changes: RevisionChanges
    conflicts: list[RevisionConflict] = field(default_factory=list)
    fully_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "changes": self.changes.to_dict(),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "fully_valid": self.fully_valid,
        }


def _block_minutes(block: PlanBlock) -> int:
    return minutes_between(parse_dt(block.starts_at), parse_dt(block.ends_at))


def revise_plan(
    previous: Plan | None,
    tasks: list[LearningTask],
    availability: Availability,
    options: ScheduleOptions,
) -> PlanRevision:
    """在上一次计划基础上局部重排；`previous=None` 等价于全量生成（仍走同一管线）。"""
    config = options.config
    horizon_start = parse_dt(options.horizon_start)
    horizon_end = parse_dt(options.horizon_end)
    reference = parse_dt(options.reference_time) if options.reference_time else horizon_start
    tz = resolve_tz(availability.timezone)
    days = expand_availability(availability, horizon_start, horizon_end)
    day_map = {day.date: day for day in days}
    warnings: list[WarningItem] = []
    conflicts: list[RevisionConflict] = []
    conflict_keys: set[tuple[Any, ...]] = set()

    def add_conflict(code: str, message: str, task_ids: list[str], block_ids: list[str]) -> None:
        key = (code, tuple(task_ids), tuple(block_ids))
        if key in conflict_keys:
            return
        conflict_keys.add(key)
        conflicts.append(
            RevisionConflict(code=code, message=message, task_ids=list(task_ids), block_ids=list(block_ids))
        )
        warnings.append(
            WarningItem(
                code=code,
                message=message,
                task_ids=list(task_ids),
                details={"block_ids": list(block_ids)} if block_ids else None,
            )
        )

    if all(not day.gaps for day in days):
        warnings.append(
            WarningItem(
                code=WarningCode.EMPTY_HORIZON,
                message="计划范围内没有任何可用窗口，所有任务将进入未安排清单",
            )
        )

    # 任务索引（重复 ID 保留先出现者）
    index: dict[str, LearningTask] = {}
    duplicates: list[str] = []
    for task in tasks:
        if task.id in index:
            duplicates.append(task.id)
        else:
            index[task.id] = task
    for task_id in sorted(set(duplicates)):
        warnings.append(
            WarningItem(
                code=WarningCode.DUPLICATE_TASK_ID,
                message=f"任务 ID 重复：{task_id}（保留先出现者）",
                task_ids=[task_id],
            )
        )

    invalid: dict[str, str] = {}
    for task_id in sorted(index):
        task = index[task_id]
        if (
            isinstance(task.estimated_minutes, bool)
            or not isinstance(task.estimated_minutes, int)
            or task.estimated_minutes <= 0
        ):
            invalid[task_id] = f"预计时长无效：{task.estimated_minutes!r}"
            continue
        if task.due_at:
            try:
                deadline_of(task.due_at, task.due_precision)
            except ValueError as exc:
                warnings.append(
                    WarningItem(
                        code=WarningCode.INVALID_DUE_DATE,
                        message=f"任务 {task_id} 截止时间无法解析，按无截止处理：{exc}",
                        task_ids=[task_id],
                    )
                )

    prereq_of: dict[str, list[str]] = {}
    for task_id in sorted(index):
        raw = list(index[task_id].prerequisite_ids or [])
        unknown = sorted({p for p in raw if p not in index})
        if unknown:
            warnings.append(
                WarningItem(
                    code=WarningCode.UNKNOWN_PREREQUISITE,
                    message=f"任务 {task_id} 引用了不存在的前置任务：{', '.join(unknown)}（按已满足处理）",
                    task_ids=[task_id],
                )
            )
        prereq_of[task_id] = [p for p in raw if p in index]

    dependents: dict[str, list[str]] = {}
    for task_id in sorted(index):
        for prereq_id in prereq_of[task_id]:
            dependents.setdefault(prereq_id, []).append(task_id)

    def task_deadline(task_id: str) -> _dt.datetime | None:
        task = index[task_id]
        if not task.due_at:
            return None
        try:
            return deadline_of(task.due_at, task.due_precision)
        except ValueError:
            return None

    space = _Space(days, reference, tz)

    # ── 1) 锁定块先占位（含冲突检测；原样保留、不静默移动） ─────────────────
    locked_kept: list[PlanBlock] = []
    pinned: list[tuple[PlanBlock, _dt.datetime, _dt.datetime]] = []
    pinned_ids: set[str] = set()
    for block in sorted(options.locked_blocks, key=lambda b: (b.starts_at, b.id)):
        try:
            start, end = parse_dt(block.starts_at), parse_dt(block.ends_at)
        except ValueError as exc:
            add_conflict(
                ConflictCode.LOCKED_BLOCK_INVALID,
                f"锁定块 {block.id} 时间无法解析，已忽略：{exc}",
                [block.task_id],
                [block.id],
            )
            continue
        if end <= start:
            add_conflict(
                ConflictCode.LOCKED_BLOCK_INVALID,
                f"锁定块 {block.id} 起止时间无效（{block.starts_at} ~ {block.ends_at}），已忽略",
                [block.task_id],
                [block.id],
            )
            continue
        locked_kept.append(block)
        pinned.append((block, start, end))
        if block.task_id not in index:
            warnings.append(
                WarningItem(
                    code=WarningCode.LOCKED_TASK_NOT_FOUND,
                    message=f"锁定块 {block.id} 引用的任务 {block.task_id} 不存在；时间仍被保留占用",
                    task_ids=[block.task_id],
                )
            )
        else:
            pinned_ids.add(block.task_id)
        if start < horizon_start or end > horizon_end:
            add_conflict(
                ConflictCode.LOCKED_BLOCK_OUTSIDE_HORIZON,
                f"锁定块 {block.id} 超出计划范围（{block.starts_at} ~ {block.ends_at}）",
                [block.task_id],
                [block.id],
            )
        day = day_map.get(start.astimezone(tz).date())
        inside = day is not None and any(g0 <= start and end <= g1 for g0, g1 in day.gaps)
        if not inside:
            add_conflict(
                ConflictCode.LOCKED_BLOCK_OUTSIDE_WINDOW,
                f"锁定块 {block.id} 不在任何可用窗口内（{block.starts_at} ~ {block.ends_at}）",
                [block.task_id],
                [block.id],
            )
        space.occupy(start, end)

    pinned_sorted = sorted(pinned, key=lambda x: (x[1], x[0].id))
    cursor_block: PlanBlock | None = None
    cursor_end: _dt.datetime | None = None
    for block, start, end in pinned_sorted:
        if cursor_end is not None and cursor_block is not None and start < cursor_end:
            add_conflict(
                ConflictCode.LOCKED_BLOCK_OVERLAP,
                f"锁定块相互重叠：{cursor_block.id}（{cursor_block.starts_at} ~ {cursor_block.ends_at}）"
                f"与 {block.id}（{block.starts_at} ~ {block.ends_at}）",
                sorted({cursor_block.task_id, block.task_id}),
                sorted({cursor_block.id, block.id}),
            )
        if cursor_end is None or end > cursor_end:
            cursor_end = end
            cursor_block = block
    exceeded_days = sorted(
        day for day, used in space.used.items()
        if space.limit.get(day) is not None and used > space.limit[day]
    )
    if exceeded_days:
        add_conflict(
            ConflictCode.LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT,
            "锁定块使以下日期超出每日上限："
            + "、".join(day.isoformat() for day in exceeded_days)
            + "（计划仍生成，但不会假装计划完全有效）",
            [],
            [],
        )

    # ── 2) 上一次计划的块：分类与校验（保留仍然有效的原有安排） ─────────────
    locked_input_ids = {b.id for b in locked_kept}
    prev_by_task: dict[str, list[PlanBlock]] = {}
    if previous is not None:
        for block in previous.blocks:
            if block.id in locked_input_ids:
                continue  # 已在锁定集合中按新位置占位，不重复处理
            prev_by_task.setdefault(block.task_id, []).append(block)
        for lst in prev_by_task.values():
            lst.sort(key=lambda b: (b.starts_at, b.id))

    block_reason: dict[str, str] = {}
    kept: dict[str, list[PlanBlock]] = {}

    # 任务级原因：整任务旧块全部释放（不再需要/不再有效）
    task_reason: dict[str, str] = {}
    for task_id in sorted(prev_by_task):
        task = index.get(task_id)
        if task is None:
            task_reason[task_id] = ChangeReason.TASK_REMOVED
        elif task.status == "completed":
            task_reason[task_id] = ChangeReason.TASK_COMPLETED
        elif task.status == "cancelled":
            task_reason[task_id] = ChangeReason.TASK_CANCELLED
        elif task.platform_submitted:
            task_reason[task_id] = ChangeReason.PLATFORM_SUBMITTED
        elif task_id in invalid:
            task_reason[task_id] = ChangeReason.TASK_INVALID
        elif task_id in pinned_ids:
            task_reason[task_id] = ChangeReason.LOCKED_REMAINDER
    for task_id in sorted(task_reason):
        for block in prev_by_task.get(task_id, []):
            block_reason[block.id] = task_reason[task_id]

    # 块级校验：按时间顺序逐一核对新约束，通过者原样保留
    accepted: list[tuple[_dt.datetime, _dt.datetime]] = [(s, e) for (_b, s, e) in pinned]

    def overlaps_accepted(start: _dt.datetime, end: _dt.datetime) -> bool:
        return any(start < e and s < end for s, e in accepted)

    candidates: list[tuple[str, PlanBlock]] = []
    for task_id, blocks in prev_by_task.items():
        if task_id in task_reason:
            continue
        for block in blocks:
            candidates.append((task_id, block))
    candidates.sort(key=lambda x: (x[1].starts_at, x[0], x[1].id))

    for task_id, block in candidates:
        reason: str | None = None
        try:
            start, end = parse_dt(block.starts_at), parse_dt(block.ends_at)
        except ValueError:
            start = end = None  # type: ignore[assignment]
            reason = ChangeReason.BLOCK_EXPIRED
        if reason is None and end <= start:  # type: ignore[operator]
            reason = ChangeReason.BLOCK_EXPIRED
        if reason is None and end <= reference:  # type: ignore[operator]
            reason = ChangeReason.BLOCK_EXPIRED
        if reason is None and (start < horizon_start or end > horizon_end):  # type: ignore[operator]
            reason = ChangeReason.WINDOW_CHANGED
        if reason is None:
            deadline = task_deadline(task_id)
            if deadline is not None and end > deadline:  # type: ignore[operator]
                reason = ChangeReason.DEADLINE_CHANGED
        if reason is None:
            day = day_map.get(start.astimezone(tz).date())  # type: ignore[union-attr]
            inside = day is not None and any(g0 <= start and end <= g1 for g0, g1 in day.gaps)
            if not inside:
                reason = ChangeReason.WINDOW_CHANGED
        if reason is None and overlaps_accepted(start, end):  # type: ignore[arg-type]
            reason = ChangeReason.BLOCK_CONFLICT
        if reason is None and not space.caps_ok(start, end):  # type: ignore[arg-type]
            reason = ChangeReason.WINDOW_CHANGED
        if reason is None:
            kept.setdefault(task_id, []).append(block)
            accepted.append((start, end))  # type: ignore[arg-type]
            space.occupy(start, end)  # type: ignore[arg-type]
        else:
            block_reason[block.id] = reason

    def drop_kept(task_id: str, reason: str) -> bool:
        blocks = kept.pop(task_id, None)
        if not blocks:
            return False
        for block in blocks:
            block_reason[block.id] = reason
            space.release(parse_dt(block.starts_at), parse_dt(block.ends_at))
        return True

    # ── 3) 循环依赖：环内任务旧块释放，稍后标记未安排（平台已提交任务不参与排程——PD-3） ──
    pending_ids = [
        task_id for task_id in sorted(index)
        if index[task_id].status == "pending"
        and task_id not in invalid
        and not index[task_id].platform_submitted
    ]
    pending_set = set(pending_ids)
    graph = {
        task_id: [p for p in prereq_of[task_id] if p in pending_set]
        for task_id in pending_ids
    }
    cycle_paths = _find_cycle_paths(graph)
    if cycle_paths:
        warnings.append(
            WarningItem(
                code=WarningCode.DEPENDENCY_CYCLE,
                message="检测到循环依赖：" + "；".join(sorted(set(cycle_paths.values()))),
                task_ids=sorted(cycle_paths),
            )
        )
    for task_id in sorted(cycle_paths):
        if task_id in pinned_ids:
            continue
        drop_kept(task_id, ChangeReason.DEPENDENCY_CASCADE)

    # ── 4) “前置先完成”检查：固定块（锁定/保留）之间的顺序违例逐轮修复 ────
    def first_start_of(task_id: str) -> _dt.datetime | None:
        starts = [
            s for (b, s, _e) in pinned if b.task_id == task_id
        ] + [
            parse_dt(b.starts_at) for b in kept.get(task_id, [])
        ] + [
            parse_dt(b.starts_at) for b in placed_by_task.get(task_id, [])
        ]
        return min(starts) if starts else None

    def last_end_of(task_id: str) -> _dt.datetime | None:
        ends = [
            e for (b, _s, e) in pinned if b.task_id == task_id
        ] + [
            parse_dt(b.ends_at) for b in kept.get(task_id, [])
        ] + [
            parse_dt(b.ends_at) for b in placed_by_task.get(task_id, [])
        ]
        return max(ends) if ends else None

    placed_by_task: dict[str, list[PlanBlock]] = {}

    while True:
        changed = False
        fixed_first: dict[str, _dt.datetime] = {}
        fixed_last: dict[str, _dt.datetime] = {}
        for task_id in sorted(index):
            first = first_start_of(task_id)
            last = last_end_of(task_id)
            if first is not None:
                fixed_first[task_id] = first
            if last is not None:
                fixed_last[task_id] = last
        for task_id in sorted(fixed_first):
            task = index[task_id]
            if task.status != "pending" or task.platform_submitted:
                continue
            for prereq_id in prereq_of.get(task_id, []):
                prereq = index.get(prereq_id)
                if prereq is None or prereq.status == "completed" or prereq.platform_submitted:
                    continue
                if prereq_id not in fixed_last:
                    continue  # 前置无固定块：由放置阶段用截止约束保证顺序
                if fixed_first[task_id] >= fixed_last[prereq_id]:
                    continue
                if task_id in pinned_ids:
                    if prereq_id in pinned_ids:
                        add_conflict(
                            ConflictCode.LOCKED_DEPENDENCY_CONFLICT,
                            f"锁定块时间无法同时满足“前置先完成”：{task_id} 的后继块开始于 "
                            f"{fmt_dt(fixed_first[task_id])}，早于前置 {prereq_id} 的末块结束 {fmt_dt(fixed_last[prereq_id])}",
                            [task_id, prereq_id],
                            [],
                        )
                        continue
                    if drop_kept(prereq_id, ChangeReason.DEPENDENCY_CASCADE):
                        changed = True
                else:
                    if drop_kept(task_id, ChangeReason.DEPENDENCY_CASCADE):
                        changed = True
        if not changed:
            break

    # ── 5) 任务时长变化 / 不可拆分变化：必要时整任务重排 ────────────────────
    for task_id in sorted(kept):
        task = index[task_id]
        est = int(task.estimated_minutes)
        kept_sum = sum(_block_minutes(b) for b in kept[task_id])
        if kept_sum > est:
            drop_kept(task_id, ChangeReason.TASK_CHANGED)
            continue
        effective_splittable = task.splittable if task.splittable is not None else config.default_splittable
        if not effective_splittable and len(kept.get(task_id, [])) > 1:
            drop_kept(task_id, ChangeReason.TASK_CHANGED)

    need_placement: set[str] = set()
    fully_kept_ids: set[str] = set()
    for task_id in pending_ids:
        if task_id in cycle_paths or task_id in pinned_ids:
            continue
        est = int(index[task_id].estimated_minutes)
        kept_sum = sum(_block_minutes(b) for b in kept.get(task_id, []))
        if kept_sum == est:
            fully_kept_ids.add(task_id)
        else:
            need_placement.add(task_id)

    capacity_minutes = int(space.available())

    # ── 6) 放置阶段：补排缺口 / 安排新任务（含依赖与固定后继约束） ──────────
    unassigned: dict[str, UnassignedItem] = {}
    for task_id in sorted(invalid):
        warnings.append(
            WarningItem(
                code=WarningCode.INVALID_DURATION,
                message=f"任务 {task_id} 数据无效：{invalid[task_id]}",
                task_ids=[task_id],
            )
        )
        unassigned[task_id] = UnassignedItem(
            task_id=task_id,
            reason_code=ReasonCode.INVALID_TASK,
            message=f"任务数据无效，无法排程：{invalid[task_id]}",
            suggestion="请修正任务数据后重新生成计划",
        )
    for task_id in sorted(cycle_paths):
        if task_id in pinned_ids or task_id in unassigned:
            continue
        unassigned[task_id] = UnassignedItem(
            task_id=task_id,
            reason_code=ReasonCode.DEPENDENCY_CONFLICT,
            message=f"依赖冲突（循环依赖）：{cycle_paths[task_id]}",
            suggestion="请先解决前置任务的安排问题，或调整依赖关系",
        )

    used_block_ids: dict[str, set[str]] = {}
    for block in locked_kept:
        used_block_ids.setdefault(block.task_id, set()).add(block.id)
    for task_id, blocks in kept.items():
        for block in blocks:
            used_block_ids.setdefault(task_id, set()).add(block.id)

    def new_block_id(task_id: str) -> str:
        used = used_block_ids.setdefault(task_id, set())
        i = 0
        while f"blk:{task_id}:{i}" in used:
            i += 1
        used.add(f"blk:{task_id}:{i}")
        return f"blk:{task_id}:{i}"

    def satisfied_now(task_id: str, visiting: frozenset[str] = frozenset()) -> bool:
        task = index.get(task_id)
        if task is None:
            return True
        if task.status == "completed":
            return True
        if task.status == "cancelled" or task_id in invalid or task_id in cycle_paths:
            return False
        if task.platform_submitted:
            # PD-3：不排程、不阻塞后继（取消除外，已在上行返回 False）；与 engine.satisfied 一致
            return True
        if task_id in pinned_ids or task_id in placed_by_task:
            return True
        if task_id in fully_kept_ids:
            if task_id in visiting:
                return False
            nxt = visiting | {task_id}
            return all(satisfied_now(p, nxt) for p in prereq_of.get(task_id, []))
        return False

    def order_key(task_id: str) -> tuple[float, int, str]:
        task = index[task_id]
        deadline = task_deadline(task_id)
        due_key = deadline.timestamp() if deadline else math.inf
        return (due_key, -(task.priority or 0), task_id)

    remaining = set(need_placement)
    while remaining:
        ready = [
            task_id for task_id in sorted(remaining)
            if all(satisfied_now(p) for p in prereq_of[task_id])
        ]
        if not ready:
            break
        task_id = min(ready, key=order_key)
        remaining.discard(task_id)
        task = index[task_id]
        est = int(task.estimated_minutes)
        kept_blocks = kept.get(task_id, [])
        kept_sum = sum(_block_minutes(b) for b in kept_blocks)
        residual = est - kept_sum

        prereq_last = [last_end_of(p) for p in prereq_of[task_id]]
        prereq_nb = max((e for e in prereq_last if e is not None), default=None)
        kept_last = max((parse_dt(b.ends_at) for b in kept_blocks), default=None)
        not_before = kept_last if kept_last is not None else prereq_nb
        if kept_last is not None and prereq_nb is not None:
            not_before = max(kept_last, prereq_nb)

        def _dep_first(include_movable: bool) -> _dt.datetime | None:
            starts: list[_dt.datetime] = []
            for d in dependents.get(task_id, []):
                if d in pinned_ids:
                    first = first_start_of(d)
                elif include_movable and d in kept and d not in unassigned and d not in placed_by_task:
                    first = first_start_of(d)
                else:
                    continue
                if first is not None:
                    starts.append(first)
            return min(starts, default=None)

        def _movable_deps() -> list[str]:
            return sorted(
                d for d in dependents.get(task_id, [])
                if d in kept and d not in pinned_ids and d not in unassigned and d not in placed_by_task
            )

        def _movable_others() -> list[str]:
            return sorted(
                t for t in kept
                if t != task_id and t not in pinned_ids
                and t not in unassigned and t not in placed_by_task
            )

        # 后继已固定安排 → 本任务需在它开始前完成；锁定后继为硬约束，
        # 保留（未锁定）后继先按软约束对待，放不下时可让其顺延（连带调整）
        override_hard = _dep_first(include_movable=False)
        override_soft = _dep_first(include_movable=True)

        attempts: list[tuple[bool, _dt.datetime | None, bool, bool]] = []
        if residual < est:
            attempts.append((False, override_soft, False, False))
            attempts.append((True, override_soft, False, False))
        else:
            attempts.append((True, override_soft, False, False))
        if override_soft != override_hard:
            # 释放后继保留块（连带顺延）后按硬约束重试
            attempts.append((True, override_hard, True, False))
        if _movable_others():
            # 最后一档：最不急的保留块逐块让位（仍失败则整体回滚）
            attempts.append((True, override_hard, True, True))

        placement = None
        released_kept = False
        released_deps = False
        displaced: list[tuple[str, list[PlanBlock], bool]] = []
        used_override: _dt.datetime | None = None
        last_item = None
        for full, override, release_deps, displace in attempts:
            if full and not released_kept and residual < est:
                drop_kept(task_id, ChangeReason.REFIT_FAILED)
                released_kept = True
            if release_deps and not released_deps:
                for d in _movable_deps():
                    if drop_kept(d, ChangeReason.DEPENDENCY_CASCADE):
                        fully_kept_ids.discard(d)
                        if d not in placed_by_task:
                            remaining.add(d)
                released_deps = True
            attempt_nb = prereq_nb if released_kept else not_before
            used_override = override

            def _try_here() -> "_Placement":
                return _try_place(
                    task if full else replace(task, estimated_minutes=residual),
                    space,
                    reference,
                    config,
                    not_before=attempt_nb,
                    deadline_override=override,
                )

            if displace:
                for other in sorted(_movable_others(), key=order_key, reverse=True):
                    if other in kept:
                        blocks = kept[other]
                        was_fully_kept = other in fully_kept_ids
                        if drop_kept(other, ChangeReason.REARRANGED):
                            displaced.append((other, blocks, was_fully_kept))
                            fully_kept_ids.discard(other)
                            remaining.add(other)
                    result = _try_here()
                    if result.blocks is not None:
                        placement = result
                        break
                    last_item = result.item
                if placement is not None:
                    break
                continue

            result = _try_here()
            if result.blocks is not None:
                placement = result
                break
            last_item = result.item

        if placement is None and displaced:
            # 让位后仍无法安排：回滚到让位前状态（保留块原样恢复）
            for other, blocks, was_fully_kept in displaced:
                kept.setdefault(other, []).extend(blocks)
                for block in blocks:
                    block_reason.pop(block.id, None)
                    space.occupy(parse_dt(block.starts_at), parse_dt(block.ends_at))
                if was_fully_kept:
                    fully_kept_ids.add(other)
                    remaining.discard(other)

        if placement is not None and placement.blocks is not None:
            placed_by_task[task_id] = [
                PlanBlock(
                    id=new_block_id(task_id),
                    task_id=task_id,
                    starts_at=block.starts_at,
                    ends_at=block.ends_at,
                )
                for block in placement.blocks
            ]
            # 后继的保留块若与本任务新位置冲突（须晚于本任务结束），连带顺延
            task_last = last_end_of(task_id)
            if task_last is not None:
                for d in dependents.get(task_id, []):
                    if d in pinned_ids or d in unassigned or d in placed_by_task:
                        continue
                    d_first = first_start_of(d)
                    if d_first is None or d_first >= task_last:
                        continue
                    if drop_kept(d, ChangeReason.DEPENDENCY_CASCADE):
                        fully_kept_ids.discard(d)
                        remaining.add(d)
            continue

        item = last_item
        assert item is not None
        own_deadline = task_deadline(task_id)
        dep_driven = used_override is not None and (
            own_deadline is None or own_deadline > used_override
        )
        if dep_driven and item.reason_code in (
            ReasonCode.DEADLINE_PASSED,
            ReasonCode.INSUFFICIENT_WINDOW_BEFORE_DUE,
        ):
            item = UnassignedItem(
                task_id=task_id,
                reason_code=ReasonCode.DEPENDENCY_CONFLICT,
                message=(
                    f"依赖冲突：需在后继任务已固定安排的开始（{fmt_dt(used_override)}）前完成，"
                    f"但该时刻前可用时间不足（需要 {item.needed_minutes} 分钟，可用 {item.available_minutes} 分钟）"
                ),
                needed_minutes=item.needed_minutes,
                available_minutes=item.available_minutes,
                max_contiguous_minutes=item.max_contiguous_minutes,
                deadline=fmt_dt(used_override),
                suggestion="调整后继任务的固定时间，或缩短本任务预计时长",
            )
        unassigned[task_id] = item

    # ── 7) 收尾：未能安排的任务释放旧块；依赖未满足的保留块一并释放 ─────────
    while True:
        changed = False
        for task_id in sorted(set(remaining) | set(kept)):
            if task_id in unassigned:
                continue
            unsatisfied = [
                p for p in prereq_of.get(task_id, []) if not satisfied_now(p)
            ]
            if not unsatisfied:
                continue
            drop_kept(task_id, ChangeReason.DEPENDENCY_CASCADE)
            if task_id in cycle_paths:
                message = f"依赖冲突（循环依赖）：{cycle_paths[task_id]}"
            else:
                message = "依赖冲突：前置任务无法满足（" + "、".join(unsatisfied) + "）"
            unassigned[task_id] = UnassignedItem(
                task_id=task_id,
                reason_code=ReasonCode.DEPENDENCY_CONFLICT,
                message=message,
                suggestion="请先解决前置任务的安排问题，或调整依赖关系",
            )
            changed = True
        if not changed:
            break

    # ── 8) 锁定块的后继条件复核：前置未满足时报冲突（不假装计划完全有效） ──
    checked_locked: set[str] = set()
    for block in locked_kept:
        task_id = block.task_id
        if task_id in checked_locked or task_id not in index:
            continue
        checked_locked.add(task_id)
        task = index[task_id]
        if task.status != "pending":
            continue
        for prereq_id in prereq_of.get(task_id, []):
            if satisfied_now(prereq_id):
                continue
            add_conflict(
                ConflictCode.LOCKED_DEPENDENCY_CONFLICT,
                f"锁定块 {block.id}（任务 {task_id}）的前置任务 {prereq_id} 未能安排，"
                "“前置先完成”无法满足",
                [task_id, prereq_id],
                [block.id],
            )
            break

    # ── 9) 组装计划 ───────────────────────────────────────────────────────
    blocks: list[PlanBlock] = list(locked_kept)
    for task_id in sorted(kept):
        blocks.extend(kept[task_id])
    for task_id in sorted(placed_by_task):
        blocks.extend(placed_by_task[task_id])
    blocks.sort(key=lambda b: (b.starts_at, b.task_id, b.id))

    unassigned_items = [unassigned[task_id] for task_id in sorted(unassigned)]
    scheduled_minutes = sum(_block_minutes(b) for b in blocks)
    locked_minutes = sum(_block_minutes(b) for b in blocks if b.locked)

    if conflicts:
        warnings.append(
            WarningItem(
                code=RevisionWarningCode.PLAN_HAS_CONFLICTS,
                message=f"计划未完全满足硬约束：{len(conflicts)} 项冲突（详见 conflicts 列表）",
            )
        )

    plan = Plan(
        horizon_start=fmt_dt(horizon_start),
        horizon_end=fmt_dt(horizon_end),
        input_revision=options.input_revision,
        blocks=blocks,
        unassigned=unassigned_items,
        warnings=warnings,
        summary={
            "capacity_minutes": capacity_minutes,
            "scheduled_minutes": scheduled_minutes,
            "locked_minutes": locked_minutes,
            "scheduled_task_count": len({b.task_id for b in blocks}),
            "unassigned_task_count": len(unassigned_items),
        },
    )

    changes = _build_changes(previous, index, blocks, unassigned, block_reason, tz)
    return PlanRevision(
        plan=plan,
        changes=changes,
        conflicts=conflicts,
        fully_valid=not conflicts,
    )


def _fmt_range(block: PlanBlock, tz: _dt.tzinfo) -> str:
    start = parse_dt(block.starts_at).astimezone(tz)
    end = parse_dt(block.ends_at).astimezone(tz)
    if start.date() == end.date():
        return f"{start.month}月{start.day}日 {start.hour:02d}:{start.minute:02d}-{end.hour:02d}:{end.minute:02d}"
    return (
        f"{start.month}月{start.day}日 {start.hour:02d}:{start.minute:02d}"
        f" - {end.month}月{end.day}日 {end.hour:02d}:{end.minute:02d}"
    )


def _build_changes(
    previous: Plan | None,
    index: dict[str, LearningTask],
    blocks: list[PlanBlock],
    unassigned: dict[str, UnassignedItem],
    block_reason: dict[str, str],
    tz: _dt.tzinfo,
) -> RevisionChanges:
    prev_by_task: dict[str, list[PlanBlock]] = {}
    if previous is not None:
        for block in previous.blocks:
            prev_by_task.setdefault(block.task_id, []).append(block)
        for lst in prev_by_task.values():
            lst.sort(key=lambda b: (b.starts_at, b.id))

    new_by_task: dict[str, list[PlanBlock]] = {}
    for block in blocks:
        new_by_task.setdefault(block.task_id, []).append(block)
    for lst in new_by_task.values():
        lst.sort(key=lambda b: (b.starts_at, b.id))

    def title_of(task_id: str) -> str:
        task = index.get(task_id)
        return task.title if task is not None else task_id

    entries: list[BlockChange] = []
    unchanged = 0
    for task_id in sorted(set(prev_by_task) | set(new_by_task)):
        old_blocks = prev_by_task.get(task_id, [])
        new_blocks = new_by_task.get(task_id, [])
        unmatched_new = list(new_blocks)
        matched_old: set[str] = set()
        for old in old_blocks:
            for new in unmatched_new:
                if new.starts_at == old.starts_at and new.ends_at == old.ends_at:
                    unmatched_new.remove(new)
                    matched_old.add(old.id)
                    unchanged += 1
                    break
        removed = [b for b in old_blocks if b.id not in matched_old]
        pairs = min(len(removed), len(unmatched_new))
        task_entries: list[BlockChange] = []
        for i in range(pairs):
            task_entries.append(
                BlockChange(
                    kind=ChangeKind.MOVED,
                    task_id=task_id,
                    task_title=title_of(task_id),
                    reason=block_reason.get(removed[i].id, ChangeReason.TASK_CHANGED),
                    before=removed[i],
                    after=unmatched_new[i],
                )
            )
        for block in removed[pairs:]:
            task_entries.append(
                BlockChange(
                    kind=ChangeKind.REMOVED,
                    task_id=task_id,
                    task_title=title_of(task_id),
                    reason=block_reason.get(block.id, ChangeReason.TASK_CHANGED),
                    before=block,
                )
            )
        extra_reason = ChangeReason.NEW_TASK if not old_blocks else ChangeReason.TASK_CHANGED
        if removed:
            extra_reason = block_reason.get(removed[-1].id, extra_reason)
        for block in unmatched_new[pairs:]:
            task_entries.append(
                BlockChange(
                    kind=ChangeKind.ADDED,
                    task_id=task_id,
                    task_title=title_of(task_id),
                    reason=extra_reason,
                    after=block,
                )
            )
        order = {ChangeKind.MOVED: 0, ChangeKind.ADDED: 1, ChangeKind.REMOVED: 2}
        task_entries.sort(key=lambda e: (order.get(e.kind, 9), e.before.starts_at if e.before else e.after.starts_at))  # type: ignore[union-attr]
        entries.extend(task_entries)

    prev_unassigned = {u.task_id: u for u in (previous.unassigned if previous is not None else [])}
    new_unassigned_list = [unassigned[task_id] for task_id in sorted(unassigned)]
    new_unassigned = {
        u.task_id: u for u in new_unassigned_list
    }
    resolved = sorted(
        task_id for task_id in prev_unassigned
        if task_id not in new_unassigned
    )
    fresh_unassigned = [
        u for u in new_unassigned_list
        if u.task_id not in prev_unassigned or prev_unassigned[u.task_id].reason_code != u.reason_code
    ]

    # ── 中文摘要（供 C 展示） ──────────────────────────────────────────────
    lines: list[str] = []
    by_task: dict[str, list[BlockChange]] = {}
    for entry in entries:
        by_task.setdefault(entry.task_id, []).append(entry)
    for task_id in sorted(by_task):
        task_entries = by_task[task_id]
        segs: list[str] = []
        reasons: list[str] = []
        moved = [e for e in task_entries if e.kind == ChangeKind.MOVED]
        added = [e for e in task_entries if e.kind == ChangeKind.ADDED]
        removed = [e for e in task_entries if e.kind == ChangeKind.REMOVED]
        if moved:
            first = moved[0]
            when = (
                f"{_fmt_range(first.before, tz)} 调整至 {_fmt_range(first.after, tz)}"  # type: ignore[arg-type]
            )
            if len(moved) == 1:
                segs.append(f"时间块由 {when}")
            else:
                segs.append(f"{len(moved)} 个时间块调整（如 {when}）")
        if added:
            total = sum(_block_minutes(b.after) for b in added)  # type: ignore[arg-type]
            first_added = min(added, key=lambda b: b.after.starts_at)  # type: ignore[union-attr]
            segs.append(f"新增安排 {total} 分钟（{_fmt_range(first_added.after, tz)}）")  # type: ignore[arg-type]
        if removed:
            total = sum(_block_minutes(b.before) for b in removed)  # type: ignore[arg-type]
            segs.append(f"移除 {total} 分钟安排")
        for entry in task_entries:
            text = REASON_TEXT.get(entry.reason, entry.reason)
            if text not in reasons:
                reasons.append(text)
        lines.append(f"{title_of(task_id)}：{'；'.join(segs)}（原因：{'、'.join(reasons)}）")

    for item in fresh_unassigned:
        lines.append(f"{title_of(item.task_id)}：未安排——{item.message}")
    for task_id in resolved:
        task = index.get(task_id)
        if task is not None and task.status == "completed":
            suffix = "任务已完成"
        elif task is not None and task.status == "cancelled":
            suffix = "任务已取消"
        elif task is None:
            suffix = "任务已移除"
        else:
            suffix = "已不再处于未安排状态"
        lines.append(f"{title_of(task_id)}：{suffix}")

    return RevisionChanges(
        entries=entries,
        new_unassigned=fresh_unassigned,
        resolved_unassigned=resolved,
        unchanged_block_count=unchanged,
        summary_lines=lines,
    )
