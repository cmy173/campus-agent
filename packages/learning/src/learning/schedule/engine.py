"""D4 规则排程引擎（纯函数、确定性）。

输入：候选任务、可用时间、每日上限、锁定时间块、计划范围与输入修订号。
输出：时间块、未安排项（含分类原因与解释）、告警与摘要。

硬约束（违反即不满足验收）：
- 任务块不得重叠，必须位于可用窗口内；
- 不超过每日学习上限；
- 必要前置任务先完成（前置全部落位后，后继才可开始）；
- 已完成/已取消任务不再安排；平台已提交（platform_submitted）任务保留在任务列表、
  但不参与排程（PD-3：不产生时间块、不进未安排清单；其“前提前置”视同已满足）；
- 锁定块原样保留、不静默移动；
- 截止前无法安排的任务进入未安排清单，并给出原因，不偷偷违反约束。

启发式说明：按截止时间与优先级在合法空闲窗口中靠前填充；同输入、同配置、同输入修订号
得到可重复结果（无随机性）。首版不承诺全局最优——打包失败时使用“当前策略未能安排”的
表述，不宣称数学上不存在可行解。
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
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
    DAY,
    DayWindows,
    expand_availability,
    fmt_dt,
    minutes_between,
    parse_dt,
    resolve_tz,
    split_by_date,
)


class ReasonCode:
    """未安排原因分类（对齐《04》D5 区分口径）。"""

    DEADLINE_PASSED = "DEADLINE_PASSED"
    INSUFFICIENT_WINDOW_BEFORE_DUE = "INSUFFICIENT_WINDOW_BEFORE_DUE"
    INSUFFICIENT_TOTAL_TIME = "INSUFFICIENT_TOTAL_TIME"
    TASK_TOO_LONG_INDIVISIBLE = "TASK_TOO_LONG_INDIVISIBLE"
    FRAGMENTED_WINDOW = "FRAGMENTED_WINDOW"
    DEPENDENCY_CONFLICT = "DEPENDENCY_CONFLICT"
    INVALID_TASK = "INVALID_TASK"


class WarningCode:
    EMPTY_HORIZON = "EMPTY_HORIZON"
    DUPLICATE_TASK_ID = "DUPLICATE_TASK_ID"
    INVALID_DURATION = "INVALID_DURATION"
    INVALID_DUE_DATE = "INVALID_DUE_DATE"
    UNKNOWN_PREREQUISITE = "UNKNOWN_PREREQUISITE"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    LOCKED_TASK_NOT_FOUND = "LOCKED_TASK_NOT_FOUND"
    LOCKED_BLOCK_INVALID = "LOCKED_BLOCK_INVALID"
    LOCKED_BLOCK_OUTSIDE_HORIZON = "LOCKED_BLOCK_OUTSIDE_HORIZON"
    LOCKED_BLOCK_OUTSIDE_WINDOW = "LOCKED_BLOCK_OUTSIDE_WINDOW"
    LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT = "LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT"


@dataclass
class ScheduleConfig:
    """排程参数（需与 B 冻结；默认值见 README 参数说明）。"""

    min_chunk_minutes: int = 25
    max_chunk_minutes: int = 60
    default_splittable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_chunk_minutes": self.min_chunk_minutes,
            "max_chunk_minutes": self.max_chunk_minutes,
            "default_splittable": self.default_splittable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScheduleConfig":
        data = data or {}
        return cls(
            min_chunk_minutes=int(data.get("min_chunk_minutes", 25)),
            max_chunk_minutes=int(data.get("max_chunk_minutes", 60)),
            default_splittable=bool(data.get("default_splittable", True)),
        )


@dataclass
class ScheduleOptions:
    horizon_start: str = ""
    horizon_end: str = ""
    input_revision: int | str | None = None
    reference_time: str | None = None
    locked_blocks: list[PlanBlock] = field(default_factory=list)
    config: ScheduleConfig = field(default_factory=ScheduleConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_start": self.horizon_start,
            "horizon_end": self.horizon_end,
            "input_revision": self.input_revision,
            "reference_time": self.reference_time,
            "locked_blocks": [b.to_dict() for b in self.locked_blocks],
            "config": self.config.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleOptions":
        return cls(
            horizon_start=data["horizon_start"],
            horizon_end=data["horizon_end"],
            input_revision=data.get("input_revision"),
            reference_time=data.get("reference_time"),
            locked_blocks=[PlanBlock.from_dict(b) for b in (data.get("locked_blocks") or [])],
            config=ScheduleConfig.from_dict(data.get("config")),
        )


class _Space:
    """可排时间空间：空闲区间 + 每日上限核算（锁定块先占用）。"""

    def __init__(self, days: list[DayWindows], reference: _dt.datetime, tz: _dt.tzinfo) -> None:
        self.tz = tz
        self.limit: dict[_dt.date, int | None] = {}
        self.used: dict[_dt.date, int] = {}
        self.intervals: list[list[_dt.datetime]] = []
        for dw in days:
            self.limit[dw.date] = dw.daily_limit_minutes
            self.used.setdefault(dw.date, 0)
            for s, e in dw.gaps:
                if e <= reference:
                    continue
                self.intervals.append([max(s, reference), e])
        self.intervals.sort(key=lambda iv: iv[0])

    def remaining(self, day: _dt.date) -> float:
        limit = self.limit.get(day)
        if limit is None:
            return math.inf
        return max(0, limit - self.used.get(day, 0))

    def caps_ok(self, start: _dt.datetime, end: _dt.datetime) -> bool:
        for day, minutes in split_by_date(start, end, self.tz):
            if minutes > self.remaining(day):
                return False
        return True

    def max_fit(self, start: _dt.datetime, cap_end: _dt.datetime) -> int:
        """从 start 出发、在 cap_end 前，受每日上限约束可连续放入的最大分钟数。"""
        upper = minutes_between(start, cap_end)
        if upper <= 0:
            return 0
        low, high = 0, upper
        while low < high:
            mid = (low + high + 1) // 2
            if self.caps_ok(start, start + _dt.timedelta(minutes=mid)):
                low = mid
            else:
                high = mid - 1
        return low

    def find_slot(
        self, size: int, cursor: _dt.datetime | None, deadline: _dt.datetime | None
    ) -> tuple[_dt.datetime, _dt.datetime] | None:
        for iv in self.intervals:
            s = iv[0] if cursor is None else max(iv[0], cursor)
            cap_end = iv[1] if deadline is None else min(iv[1], deadline)
            if s >= cap_end:
                continue
            if self.max_fit(s, cap_end) >= size:
                return s, s + _dt.timedelta(minutes=size)
        return None

    def occupy(self, start: _dt.datetime, end: _dt.datetime) -> None:
        kept: list[list[_dt.datetime]] = []
        for iv in self.intervals:
            if end <= iv[0] or start >= iv[1]:
                kept.append(iv)
                continue
            if iv[0] < start:
                kept.append([iv[0], start])
            if end < iv[1]:
                kept.append([end, iv[1]])
        self.intervals = kept
        for day, minutes in split_by_date(start, end, self.tz):
            self.used[day] = self.used.get(day, 0) + minutes

    def release(self, start: _dt.datetime, end: _dt.datetime) -> None:
        self.intervals.append([start, end])
        self.intervals.sort(key=lambda iv: iv[0])
        merged: list[list[_dt.datetime]] = []
        for iv in self.intervals:
            if merged and iv[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], iv[1])
            else:
                merged.append(iv)
        self.intervals = merged
        for day, minutes in split_by_date(start, end, self.tz):
            self.used[day] = self.used.get(day, 0) - minutes

    def available(self, until: _dt.datetime | None = None) -> int:
        """剩余可安排分钟数（按每日上限核算）；until 限定截止前容量。"""
        budget = {day: self.remaining(day) for day in self.limit}
        total = 0
        for iv in self.intervals:
            start, end = iv
            if until is not None:
                if start >= until:
                    break
                end = min(end, until)
            for day, minutes in split_by_date(start, end, self.tz):
                room = budget.get(day, math.inf)
                take = minutes if room == math.inf else min(minutes, room)
                if room != math.inf:
                    budget[day] = room - take
                total += take
        return total


@dataclass
class _Placement:
    blocks: list[PlanBlock] | None
    item: UnassignedItem | None


def _chunk_sizes(total: int, config: ScheduleConfig) -> list[int] | None:
    """把任务按允许粒度均匀拆分：块数取 ceil(total/max)，每块在 [min, max] 内。"""
    mx = max(1, int(config.max_chunk_minutes))
    mn = max(1, int(config.min_chunk_minutes))
    if total <= mx:
        return [total]
    count = math.ceil(total / mx)
    while count > 1 and total / count < mn:
        count -= 1
    base, rem = divmod(total, count)
    sizes = [base + (1 if i < rem else 0) for i in range(count)]
    if any(size < mn or size > mx for size in sizes):
        return None
    return sizes


def _find_cycle_paths(graph: dict[str, list[str]]) -> dict[str, str]:
    """返回处于依赖环中的节点 → 环路径描述（确定性顺序）。"""
    white, gray, black = 0, 1, 2
    color = {node: white for node in graph}
    stack: list[str] = []
    paths: dict[str, str] = {}

    def visit(node: str) -> None:
        color[node] = gray
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == white:
                visit(nxt)
            elif color[nxt] == gray:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                desc = " → ".join(cycle)
                for member in stack[idx:]:
                    paths.setdefault(member, desc)
        stack.pop()
        color[node] = black

    for node in sorted(graph):
        if color[node] == white:
            visit(node)
    return paths


def _max_contiguous(space: _Space, deadline: _dt.datetime | None) -> int:
    best = 0
    for iv in space.intervals:
        cap_end = iv[1] if deadline is None else min(iv[1], deadline)
        if iv[0] >= cap_end:
            continue
        best = max(best, space.max_fit(iv[0], cap_end))
    return best


def _try_place(
    task: LearningTask,
    space: _Space,
    reference: _dt.datetime,
    config: ScheduleConfig,
    not_before: _dt.datetime | None = None,
    deadline_override: _dt.datetime | None = None,
) -> _Placement:
    """尝试安排任务（整排或按拆分）。

    - not_before：所有块的开始不得早于此时刻（用于前置任务结束后、D5 局部重排的锚点）；
    - deadline_override：额外收紧的截止时刻（取任务自身截止与 override 的较早者），
      用于 D5 中“后继任务已固定，前置必须在它开始前完成”的场景。
    """
    est = int(task.estimated_minutes)
    deadline = None
    if task.due_at:
        try:
            deadline = deadline_of(task.due_at, task.due_precision)
        except ValueError:
            deadline = None
    if deadline_override is not None:
        deadline = deadline_override if deadline is None else min(deadline, deadline_override)
    due_text = fmt_dt(deadline) if deadline else None

    if deadline is not None and deadline <= reference:
        return _Placement(
            None,
            UnassignedItem(
                task_id=task.id,
                reason_code=ReasonCode.DEADLINE_PASSED,
                message=f"截止时间（{due_text}）已过，截止前无可用窗口；该任务未安排",
                needed_minutes=est,
                available_minutes=0,
                deadline=due_text,
                suggestion="如仍需学习，请更新截止时间或将该任务标记为豁免后重新生成计划",
            ),
        )

    effective_splittable = task.splittable if task.splittable is not None else config.default_splittable
    attempts: list[list[int]] = []
    if not effective_splittable:
        attempts.append([est])
    else:
        sizes = _chunk_sizes(est, config)
        if sizes is None:
            effective_splittable = False
            attempts.append([est])
        else:
            attempts.append(sizes)
            if len(sizes) == 1 and est > 2 * config.min_chunk_minutes and est <= config.max_chunk_minutes:
                half = est // 2
                alt = [est - half, half]  # 先大后小，保持块顺序稳定
                if alt[-1] >= config.min_chunk_minutes:
                    attempts.append(alt)

    primary_sizes = attempts[0]
    for sizes in attempts:
        cursor: _dt.datetime | None = not_before
        placed: list[tuple[_dt.datetime, _dt.datetime]] = []
        ok = True
        for size in sizes:
            slot = space.find_slot(size, cursor, deadline)
            if slot is None:
                ok = False
                break
            start, end = slot
            space.occupy(start, end)
            placed.append((start, end))
            cursor = end
        if ok:
            blocks = [
                PlanBlock(
                    id=f"blk:{task.id}:{i}",
                    task_id=task.id,
                    starts_at=fmt_dt(start),
                    ends_at=fmt_dt(end),
                )
                for i, (start, end) in enumerate(placed)
            ]
            return _Placement(blocks, None)
        for start, end in placed:
            space.release(start, end)

    available = space.available(until=deadline)
    max_contiguous = _max_contiguous(space, deadline)
    if available < est:
        if deadline is not None:
            item = UnassignedItem(
                task_id=task.id,
                reason_code=ReasonCode.INSUFFICIENT_WINDOW_BEFORE_DUE,
                message=f"截止（{due_text}）前可用时间不足：需要 {est} 分钟，截止前剩余 {available} 分钟",
                needed_minutes=est,
                available_minutes=available,
                max_contiguous_minutes=max_contiguous,
                deadline=due_text,
                suggestion="缩短预计耗时、提前开始或调整截止时间",
            )
        else:
            item = UnassignedItem(
                task_id=task.id,
                reason_code=ReasonCode.INSUFFICIENT_TOTAL_TIME,
                message=f"计划范围内可用时间不足：需要 {est} 分钟，剩余 {available} 分钟",
                needed_minutes=est,
                available_minutes=available,
                max_contiguous_minutes=max_contiguous,
                suggestion="扩大计划范围、提高每日上限或减少任务量",
            )
    elif not effective_splittable:
        item = UnassignedItem(
            task_id=task.id,
            reason_code=ReasonCode.TASK_TOO_LONG_INDIVISIBLE,
            message=(
                f"任务不可拆分且找不到连续 {est} 分钟窗口：最长连续可用 {max_contiguous} 分钟"
                "（当前策略未能安排）"
            ),
            needed_minutes=est,
            available_minutes=available,
            max_contiguous_minutes=max_contiguous,
            deadline=due_text,
            suggestion="允许拆分该任务，或调整可用时间/每日上限",
        )
    else:
        min_size = min(primary_sizes)
        item = UnassignedItem(
            task_id=task.id,
            reason_code=ReasonCode.FRAGMENTED_WINDOW,
            message=(
                f"可用窗口碎片化：拆分后最小块 {min_size} 分钟无法放入"
                f"（最长连续可用 {max_contiguous} 分钟；当前策略未能安排，不代表不存在其他拆分方案）"
            ),
            needed_minutes=est,
            available_minutes=available,
            max_contiguous_minutes=max_contiguous,
            deadline=due_text,
            suggestion="调整最小拆分粒度、延长窗口或减少其他任务占位",
        )
    return _Placement(None, item)


def generate_plan(
    tasks: list[LearningTask],
    availability: Availability,
    options: ScheduleOptions,
) -> Plan:
    """任务 + 约束 → 计划候选（时间块、未安排项、告警、摘要）。"""
    config = options.config
    horizon_start = parse_dt(options.horizon_start)
    horizon_end = parse_dt(options.horizon_end)
    reference = parse_dt(options.reference_time) if options.reference_time else horizon_start
    tz = resolve_tz(availability.timezone)
    warnings: list[WarningItem] = []

    days = expand_availability(availability, horizon_start, horizon_end)
    if all(not day.gaps for day in days):
        warnings.append(
            WarningItem(
                code=WarningCode.EMPTY_HORIZON,
                message="计划范围内没有任何可用窗口，所有任务将进入未安排清单",
            )
        )
    space = _Space(days, reference, tz)

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

    # 数据校验
    invalid: dict[str, str] = {}
    for task_id in sorted(index):
        task = index[task_id]
        if isinstance(task.estimated_minutes, bool) or not isinstance(task.estimated_minutes, int) or task.estimated_minutes <= 0:
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

    # 锁定块：先占用时间空间（顺带校验）
    locked_kept: list[PlanBlock] = []
    locked_task_ids: set[str] = set()
    day_map = {day.date: day for day in days}
    for block in sorted(options.locked_blocks, key=lambda b: (b.starts_at, b.id)):
        try:
            start, end = parse_dt(block.starts_at), parse_dt(block.ends_at)
        except ValueError as exc:
            warnings.append(
                WarningItem(
                    code=WarningCode.LOCKED_BLOCK_INVALID,
                    message=f"锁定块 {block.id} 时间无法解析，已忽略：{exc}",
                    task_ids=[block.task_id],
                )
            )
            continue
        if end <= start:
            warnings.append(
                WarningItem(
                    code=WarningCode.LOCKED_BLOCK_INVALID,
                    message=f"锁定块 {block.id} 起止时间无效（{block.starts_at} ~ {block.ends_at}），已忽略",
                    task_ids=[block.task_id],
                )
            )
            continue
        locked_kept.append(block)
        if block.task_id not in index:
            warnings.append(
                WarningItem(
                    code=WarningCode.LOCKED_TASK_NOT_FOUND,
                    message=f"锁定块 {block.id} 引用的任务 {block.task_id} 不存在；时间仍被保留占用",
                    task_ids=[block.task_id],
                )
            )
        else:
            locked_task_ids.add(block.task_id)
        if start < horizon_start or end > horizon_end:
            warnings.append(
                WarningItem(
                    code=WarningCode.LOCKED_BLOCK_OUTSIDE_HORIZON,
                    message=f"锁定块 {block.id} 超出计划范围（{block.starts_at} ~ {block.ends_at}）",
                    task_ids=[block.task_id],
                )
            )
        space.occupy(start, end)
        day = day_map.get(start.astimezone(tz).date())
        inside = day is not None and any(g0 <= start and end <= g1 for g0, g1 in day.gaps)
        if not inside:
            warnings.append(
                WarningItem(
                    code=WarningCode.LOCKED_BLOCK_OUTSIDE_WINDOW,
                    message=f"锁定块 {block.id} 不在任何可用窗口内（{block.starts_at} ~ {block.ends_at}）",
                    task_ids=[block.task_id],
                )
            )
    exceeded = sorted(
        day for day, used in space.used.items()
        if space.limit.get(day) is not None and used > space.limit[day]
    )
    if exceeded:
        warnings.append(
            WarningItem(
                code=WarningCode.LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT,
                message=(
                    "锁定块使以下日期超出每日上限："
                    + "、".join(day.isoformat() for day in exceeded)
                    + "（计划仍生成，但不会假装计划完全有效）"
                ),
                details={"dates": [day.isoformat() for day in exceeded]},
            )
        )

    capacity_minutes = int(space.available())

    # 循环依赖检测（仅在待排任务之间；平台已提交任务不参与排程——PD-3）
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

    completed = {task.id for task in index.values() if task.status == "completed"}
    locked_scheduled = {
        task_id for task_id in locked_task_ids
        if task_id in index and index[task_id].status == "pending"
    }
    scheduled: set[str] = set(locked_scheduled)
    unassigned: dict[str, UnassignedItem] = {}
    blocks: list[PlanBlock] = list(locked_kept)

    # 前置任务已落位的结束时刻（含锁定块）：后继任务首块不得早于其前置末块，
    # 否则锁定块造成的碎片化可能让后继块插到前置块之前（违反“前置先完成”）。
    last_end: dict[str, _dt.datetime] = {}
    for block in blocks:
        end = parse_dt(block.ends_at)
        prev = last_end.get(block.task_id)
        if prev is None or end > prev:
            last_end[block.task_id] = end

    for task_id in sorted(invalid):
        task = index[task_id]
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

    def satisfied(prereq_id: str) -> bool:
        if prereq_id not in index:
            return True  # 未知前置已告警，按已满足处理
        prereq = index[prereq_id]
        if prereq.platform_submitted and prereq.status != "cancelled":
            # PD-3：平台已提交任务不排程，其“前提前置”视同已满足（否则后继永久无法安排，
            # 且用户无从恢复）；取消除外（与 cancelled 前置的既有语义一致）。
            return True
        return prereq_id in completed or prereq_id in scheduled

    def order_key(task_id: str) -> tuple[float, int, str]:
        task = index[task_id]
        deadline = None
        if task.due_at:
            try:
                deadline = deadline_of(task.due_at, task.due_precision)
            except ValueError:
                deadline = None
        due_key = deadline.timestamp() if deadline else math.inf
        return (due_key, -(task.priority or 0), task_id)

    remaining = set(pending_ids) - locked_scheduled
    while remaining:
        ready = [task_id for task_id in sorted(remaining) if all(satisfied(p) for p in prereq_of[task_id])]
        if not ready:
            break
        task_id = min(ready, key=order_key)
        remaining.discard(task_id)
        not_before = max(
            (last_end[p] for p in prereq_of[task_id] if p in last_end), default=None
        )
        placement = _try_place(index[task_id], space, reference, config, not_before=not_before)
        if placement.blocks is not None:
            scheduled.add(task_id)
            blocks.extend(placement.blocks)
            task_last = max(parse_dt(b.ends_at) for b in placement.blocks)
            prev = last_end.get(task_id)
            last_end[task_id] = task_last if prev is None else max(prev, task_last)
        else:
            unassigned[task_id] = placement.item  # type: ignore[assignment]

    for task_id in sorted(remaining):
        if task_id in cycle_paths:
            message = f"依赖冲突（循环依赖）：{cycle_paths[task_id]}"
        else:
            missing = [p for p in prereq_of[task_id] if not satisfied(p)]
            message = "依赖冲突：前置任务无法满足（" + "、".join(missing) + "）"
        unassigned[task_id] = UnassignedItem(
            task_id=task_id,
            reason_code=ReasonCode.DEPENDENCY_CONFLICT,
            message=message,
            suggestion="请先解决前置任务的安排问题，或调整依赖关系",
        )

    blocks.sort(key=lambda b: (b.starts_at, b.task_id, b.id))
    unassigned_items = [unassigned[task_id] for task_id in sorted(unassigned)]
    scheduled_minutes = sum(
        minutes_between(parse_dt(b.starts_at), parse_dt(b.ends_at)) for b in blocks
    )
    locked_minutes = sum(
        minutes_between(parse_dt(b.starts_at), parse_dt(b.ends_at)) for b in blocks if b.locked
    )

    return Plan(
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
