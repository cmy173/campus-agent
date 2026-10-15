"""revise_plan 局部重排测试：最小变化、保留锁定块、冲突显式返回、四类原因、变化摘要。"""

from learning.models import Availability, LearningTask, PlanBlock, WeeklyWindow
from learning.schedule.engine import (
    ReasonCode,
    ScheduleConfig,
    ScheduleOptions,
    WarningCode,
    generate_plan,
)
from learning.schedule.revise import (
    ChangeKind,
    ChangeReason,
    ConflictCode,
    RevisionWarningCode,
    revise_plan,
)
from learning.schedule.validate import validate_plan

MONDAY_EVENING = ((0, "19:00", "22:30"),)


def make_availability(windows=MONDAY_EVENING, limit=None):
    return Availability(
        weekly_windows=[WeeklyWindow(weekday=w, start=s, end=e) for (w, s, e) in windows],
        daily_limit_minutes=limit,
    )


def make_options(**kwargs):
    return ScheduleOptions(
        horizon_start=kwargs.get("horizon_start", "2026-10-12T00:00+08:00"),
        horizon_end=kwargs.get("horizon_end", "2026-10-19T00:00+08:00"),
        reference_time=kwargs.get("reference_time", "2026-10-09T12:00+08:00"),
        locked_blocks=kwargs.get("locked_blocks", []),
        config=kwargs.get("config", ScheduleConfig()),
    )


def task(task_id, minutes, **kwargs):
    return LearningTask(
        id=task_id,
        course_id="crs_demo",
        type="reading",
        title=f"任务 {task_id}",
        estimated_minutes=minutes,
        **kwargs,
    )


def blocks_of(plan, task_id):
    return [(b.starts_at, b.ends_at) for b in plan.blocks if b.task_id == task_id]


def reason_of(revision, task_id):
    for item in revision.plan.unassigned:
        if item.task_id == task_id:
            return item.reason_code
    return None


def locked_block(block_id, task_id, start, end):
    return PlanBlock(id=block_id, task_id=task_id, starts_at=start, ends_at=end, locked=True)


# ── 最小变化与保留 ─────────────────────────────────────────────────────────


def test_unchanged_plan_keeps_blocks_exactly():
    availability = make_availability()
    tasks = [task("t_a", 60), task("t_b", 30)]
    options = make_options()
    plan = generate_plan(tasks, availability, options)

    revision = revise_plan(plan, tasks, availability, options)

    assert [(b.id, b.starts_at, b.ends_at) for b in revision.plan.blocks] == [
        (b.id, b.starts_at, b.ends_at) for b in plan.blocks
    ]
    assert revision.changes.entries == []
    assert revision.changes.summary_lines == []
    assert revision.changes.unchanged_block_count == 2
    assert revision.conflicts == []
    assert revision.fully_valid is True


def test_new_task_does_not_move_existing_blocks():
    availability = make_availability()
    t_a = task("t_a", 60)
    options = make_options()
    plan = generate_plan([t_a], availability, options)

    revision = revise_plan(plan, [t_a, task("t_b", 30)], availability, options)

    assert blocks_of(revision.plan, "t_a") == blocks_of(plan, "t_a")
    assert blocks_of(revision.plan, "t_b") == [("2026-10-12T20:00+08:00", "2026-10-12T20:30+08:00")]
    assert all(e.task_id != "t_a" for e in revision.changes.entries)
    assert (ChangeKind.ADDED, "t_b", ChangeReason.NEW_TASK) in [
        (e.kind, e.task_id, e.reason) for e in revision.changes.entries
    ]
    assert any("新增安排 30 分钟" in line for line in revision.changes.summary_lines)


def test_completed_task_releases_block_without_touching_others():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_a", 60), task("t_b", 30)], availability, options)

    revision = revise_plan(
        plan, [task("t_a", 60, status="completed"), task("t_b", 30)], availability, options
    )

    assert blocks_of(revision.plan, "t_a") == []
    assert blocks_of(revision.plan, "t_b") == blocks_of(plan, "t_b")
    removed = {(e.task_id, e.reason) for e in revision.changes.entries if e.kind == ChangeKind.REMOVED}
    assert removed == {("t_a", ChangeReason.TASK_COMPLETED)}
    assert any("任务已完成" in line for line in revision.changes.summary_lines)
    assert revision.fully_valid is True


def test_cancelled_task_releases_block():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_a", 60)], availability, options)

    revision = revise_plan(plan, [task("t_a", 60, status="cancelled")], availability, options)

    assert revision.plan.blocks == []
    removed = [(e.task_id, e.reason) for e in revision.changes.entries if e.kind == ChangeKind.REMOVED]
    assert removed == [("t_a", ChangeReason.TASK_CANCELLED)]
    assert any("任务已取消" in line for line in revision.changes.summary_lines)


def test_removed_task_releases_block_and_dependent_is_still_scheduled():
    availability = make_availability()
    options = make_options()
    tasks = [task("t_a", 60), task("t_b", 30, prerequisite_ids=["t_a"])]
    plan = generate_plan(tasks, availability, options)

    # t_a 从任务列表消失（被删除），t_b 仍在
    revision = revise_plan(plan, [task("t_b", 30, prerequisite_ids=["t_a"])], availability, options)

    assert blocks_of(revision.plan, "t_a") == []
    assert blocks_of(revision.plan, "t_b") == blocks_of(plan, "t_b")
    removed = [(e.task_id, e.reason) for e in revision.changes.entries if e.kind == ChangeKind.REMOVED]
    assert removed == [("t_a", ChangeReason.TASK_REMOVED)]
    assert any(w.code == WarningCode.UNKNOWN_PREREQUISITE for w in revision.plan.warnings)
    assert reason_of(revision, "t_b") is None


def test_invalid_task_releases_previous_block():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_a", 60)], availability, options)

    revision = revise_plan(plan, [task("t_a", 0)], availability, options)

    assert revision.plan.blocks == []
    assert reason_of(revision, "t_a") == ReasonCode.INVALID_TASK
    removed = [(e.task_id, e.reason) for e in revision.changes.entries if e.kind == ChangeKind.REMOVED]
    assert removed == [("t_a", ChangeReason.TASK_INVALID)]


# ── 时长与拆分变化 ─────────────────────────────────────────────────────────


def test_estimate_growth_appends_remainder_without_moving_kept():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_p", 60)], availability, options)

    revision = revise_plan(plan, [task("t_p", 90)], availability, options)

    assert blocks_of(revision.plan, "t_p") == [
        ("2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00"),
        ("2026-10-12T20:00+08:00", "2026-10-12T20:30+08:00"),
    ]
    added = [e for e in revision.changes.entries if e.kind == ChangeKind.ADDED]
    assert [(e.task_id, e.reason) for e in added] == [("t_p", ChangeReason.TASK_CHANGED)]


def test_estimate_shrink_replaces_block_and_reports_move():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_p", 60)], availability, options)

    revision = revise_plan(plan, [task("t_p", 30)], availability, options)

    assert blocks_of(revision.plan, "t_p") == [("2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00")]
    entries = revision.changes.entries
    assert [(e.kind, e.task_id, e.reason) for e in entries] == [("moved", "t_p", ChangeReason.TASK_CHANGED)]
    assert entries[0].before.starts_at == "2026-10-12T19:00+08:00"
    assert entries[0].after.ends_at == "2026-10-12T19:30+08:00"


def test_splittable_switch_to_indivisible_requires_single_block():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_p", 120)], availability, options)
    assert len(blocks_of(plan, "t_p")) == 2  # 120 → 2×60

    revision = revise_plan(plan, [task("t_p", 120, splittable=False)], availability, options)

    assert len(blocks_of(revision.plan, "t_p")) == 1
    assert reason_of(revision, "t_p") is None
    assert revision.fully_valid is True

    # 切回可拆分不会产生多余变化（单块仍然有效）
    again = revise_plan(revision.plan, [task("t_p", 120, splittable=True)], availability, options)
    assert blocks_of(again.plan, "t_p") == blocks_of(revision.plan, "t_p")
    assert again.changes.entries == []


# ── 截止时间与窗口变化 ─────────────────────────────────────────────────────


def test_earlier_deadline_swaps_tasks_and_formats_summary():
    availability = make_availability(windows=((0, "18:00", "22:00"),))
    options = make_options()
    plan = generate_plan([task("t_a", 60), task("t_b", 60)], availability, options)
    assert blocks_of(plan, "t_a") == [("2026-10-12T18:00+08:00", "2026-10-12T19:00+08:00")]
    assert blocks_of(plan, "t_b") == [("2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")]

    # t_b 截止提前到 19:30：原块越界，需抢占 t_a 的位置
    t_b_urgent = task("t_b", 60, due_at="2026-10-12T19:30+08:00", due_precision="datetime")
    revision = revise_plan(plan, [task("t_a", 60), t_b_urgent], availability, options)

    assert blocks_of(revision.plan, "t_b") == [("2026-10-12T18:00+08:00", "2026-10-12T19:00+08:00")]
    assert blocks_of(revision.plan, "t_a") == [("2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")]
    reasons = {e.task_id: e.reason for e in revision.changes.entries if e.kind == ChangeKind.MOVED}
    assert reasons == {"t_b": ChangeReason.DEADLINE_CHANGED, "t_a": ChangeReason.REARRANGED}
    joined = "\n".join(revision.changes.summary_lines)
    assert "时间块由 10月12日 19:00-20:00 调整至 10月12日 18:00-19:00" in joined
    assert "超出新的截止时间" in joined
    assert "为其他任务让位" in joined
    assert revision.fully_valid is True
    assert validate_plan(revision.plan, [task("t_a", 60), t_b_urgent], availability) == []


def test_window_change_moves_blocks_into_new_window():
    options = make_options()
    old_avail = make_availability(windows=((0, "18:00", "20:00"),))
    plan = generate_plan([task("t_a", 60), task("t_b", 60)], old_avail, options)

    new_avail = make_availability(windows=((0, "20:00", "22:00"),))
    revision = revise_plan(plan, [task("t_a", 60), task("t_b", 60)], new_avail, options)

    assert blocks_of(revision.plan, "t_a") == [("2026-10-12T20:00+08:00", "2026-10-12T21:00+08:00")]
    assert blocks_of(revision.plan, "t_b") == [("2026-10-12T21:00+08:00", "2026-10-12T22:00+08:00")]
    reasons = {(e.task_id, e.reason) for e in revision.changes.entries}
    assert reasons == {
        ("t_a", ChangeReason.WINDOW_CHANGED),
        ("t_b", ChangeReason.WINDOW_CHANGED),
    }
    assert revision.fully_valid is True


def test_displacement_rollback_when_still_infeasible():
    options = make_options()
    old_avail = make_availability(windows=((0, "19:00", "21:00"),))
    plan = generate_plan([task("t_p", 30)], old_avail, options)

    # 新窗口多一块碎片 + 新任务容量本身不足 → 让位也无法安排，应回滚
    new_avail = make_availability(
        windows=((0, "18:00", "18:30"), (0, "19:00", "21:00"))
    )
    t_q = task("t_q", 200, splittable=False, due_at="2026-10-12", due_precision="date")
    revision = revise_plan(plan, [task("t_p", 30), t_q], new_avail, options)

    assert blocks_of(revision.plan, "t_p") == [("2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00")]
    assert reason_of(revision, "t_q") == ReasonCode.INSUFFICIENT_TOTAL_TIME
    assert all(e.task_id != "t_p" for e in revision.changes.entries)


# ── 锁定块：冲突显式返回，不改动原块 ───────────────────────────────────────


def test_locked_block_outside_window_reported_not_moved():
    availability = make_availability()
    options = make_options(
        locked_blocks=[
            locked_block("blk_x", "t_x", "2026-10-12T15:00+08:00", "2026-10-12T16:00+08:00")
        ]
    )

    revision = revise_plan(None, [task("t_x", 60)], availability, options)

    assert [(b.id, b.starts_at, b.ends_at) for b in revision.plan.blocks] == [
        ("blk_x", "2026-10-12T15:00+08:00", "2026-10-12T16:00+08:00")
    ]
    assert [c.code for c in revision.conflicts] == [ConflictCode.LOCKED_BLOCK_OUTSIDE_WINDOW]
    assert revision.fully_valid is False
    assert any(w.code == RevisionWarningCode.PLAN_HAS_CONFLICTS for w in revision.plan.warnings)


def test_locked_blocks_overlap_reported():
    availability = make_availability()
    options = make_options(
        locked_blocks=[
            locked_block("blk_x", "t_x", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00"),
            locked_block("blk_y", "t_y", "2026-10-12T19:30+08:00", "2026-10-12T20:30+08:00"),
        ]
    )

    revision = revise_plan(None, [task("t_x", 60), task("t_y", 60)], availability, options)

    assert [c.code for c in revision.conflicts] == [ConflictCode.LOCKED_BLOCK_OVERLAP]
    assert len(revision.plan.blocks) == 2
    assert revision.fully_valid is False


def test_locked_block_exceeds_daily_limit_reported():
    availability = make_availability(limit=60)
    options = make_options(
        locked_blocks=[
            locked_block("blk_x", "t_x", "2026-10-12T19:00+08:00", "2026-10-12T21:00+08:00")
        ]
    )

    revision = revise_plan(None, [task("t_x", 120)], availability, options)

    assert [c.code for c in revision.conflicts] == [ConflictCode.LOCKED_BLOCK_EXCEEDS_DAILY_LIMIT]
    assert revision.fully_valid is False


def test_locked_dependency_conflict_reported():
    availability = make_availability()
    options = make_options(
        locked_blocks=[
            locked_block("blk_p", "t_p", "2026-10-12T20:00+08:00", "2026-10-12T21:00+08:00"),
            locked_block("blk_d", "t_d", "2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00"),
        ]
    )
    tasks = [task("t_p", 60), task("t_d", 30, prerequisite_ids=["t_p"])]

    revision = revise_plan(None, tasks, availability, options)

    assert [c.code for c in revision.conflicts] == [ConflictCode.LOCKED_DEPENDENCY_CONFLICT]
    assert revision.fully_valid is False
    assert len(revision.plan.blocks) == 2


def test_locked_task_previous_block_released_as_remainder():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_p", 120)], availability, options)
    assert len(blocks_of(plan, "t_p")) == 2

    # 仅锁定第一块：第二块的旧安排应释放，且任务不再补排（已有锁定安排）
    options2 = make_options(locked_blocks=[plan.blocks[0]])
    revision = revise_plan(plan, [task("t_p", 120)], availability, options2)

    assert [(b.id, b.starts_at, b.ends_at) for b in revision.plan.blocks] == [
        (plan.blocks[0].id, plan.blocks[0].starts_at, plan.blocks[0].ends_at)
    ]
    released = [(e.task_id, e.reason) for e in revision.changes.entries if e.kind == ChangeKind.REMOVED]
    assert released == [("t_p", ChangeReason.LOCKED_REMAINDER)]
    assert reason_of(revision, "t_p") is None
    assert revision.fully_valid is True


# ── 依赖级联 ───────────────────────────────────────────────────────────────


def test_dependent_kept_block_cascades_after_prereq_growth():
    availability = make_availability(windows=((0, "18:00", "20:00"),))
    options = make_options()
    tasks = [task("t_p", 60), task("t_d", 30, prerequisite_ids=["t_p"])]
    plan = generate_plan(tasks, availability, options)
    assert blocks_of(plan, "t_p") == [("2026-10-12T18:00+08:00", "2026-10-12T19:00+08:00")]
    assert blocks_of(plan, "t_d") == [("2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00")]

    grown = [task("t_p", 90), task("t_d", 30, prerequisite_ids=["t_p"])]
    revision = revise_plan(plan, grown, availability, options)

    assert blocks_of(revision.plan, "t_p") == [
        ("2026-10-12T18:00+08:00", "2026-10-12T18:45+08:00"),
        ("2026-10-12T18:45+08:00", "2026-10-12T19:30+08:00"),
    ]
    assert blocks_of(revision.plan, "t_d") == [("2026-10-12T19:30+08:00", "2026-10-12T20:00+08:00")]
    t_d_reason = [e.reason for e in revision.changes.entries if e.task_id == "t_d"]
    assert t_d_reason == [ChangeReason.DEPENDENCY_CASCADE]
    assert revision.fully_valid is True
    assert validate_plan(revision.plan, grown, availability) == []


def test_dependent_unassigned_when_prereq_fails():
    availability = make_availability(windows=((0, "18:00", "20:00"),))
    options = make_options()
    tasks = [task("t_p", 60), task("t_d", 30, prerequisite_ids=["t_p"])]
    plan = generate_plan(tasks, availability, options)

    grown = [task("t_p", 180), task("t_d", 30, prerequisite_ids=["t_p"])]
    revision = revise_plan(plan, grown, availability, options)

    assert reason_of(revision, "t_p") == ReasonCode.INSUFFICIENT_TOTAL_TIME
    assert reason_of(revision, "t_d") == ReasonCode.DEPENDENCY_CONFLICT
    assert blocks_of(revision.plan, "t_d") == []


def test_dependency_cycle_releases_blocks():
    availability = make_availability()
    options = make_options()
    plan = generate_plan([task("t_a", 30), task("t_b", 30)], availability, options)

    cyclic = [
        task("t_a", 30, prerequisite_ids=["t_b"]),
        task("t_b", 30, prerequisite_ids=["t_a"]),
    ]
    revision = revise_plan(plan, cyclic, availability, options)

    assert revision.plan.blocks == []
    assert reason_of(revision, "t_a") == ReasonCode.DEPENDENCY_CONFLICT
    assert reason_of(revision, "t_b") == ReasonCode.DEPENDENCY_CONFLICT
    assert any(w.code == WarningCode.DEPENDENCY_CYCLE for w in revision.plan.warnings)


def test_dependent_starts_after_fragmented_prereq_last_block():
    # 引擎回归：前置任务被锁定块碎片化后，后继不得插入前置的块间空隙
    availability = make_availability(windows=((0, "18:00", "20:30"),))
    options = make_options(
        locked_blocks=[
            locked_block("blk_l", "t_l", "2026-10-12T19:15+08:00", "2026-10-12T19:20+08:00")
        ]
    )
    tasks = [
        task("t_l", 5),
        task("t_p", 80),
        task("t_d", 30, prerequisite_ids=["t_p"]),
    ]
    plan = generate_plan(tasks, availability, options)

    assert blocks_of(plan, "t_p") == [
        ("2026-10-12T18:00+08:00", "2026-10-12T18:40+08:00"),
        ("2026-10-12T19:20+08:00", "2026-10-12T20:00+08:00"),
    ]
    assert blocks_of(plan, "t_d") == [("2026-10-12T20:00+08:00", "2026-10-12T20:30+08:00")]


# ── 四类原因可复现 ─────────────────────────────────────────────────────────


def test_four_reason_codes_reproducible():
    options = make_options()

    # ① 总时间不足
    avail_small = make_availability(windows=((0, "18:00", "20:00"),))
    t1 = task("t1", 180)
    assert reason_of(revise_plan(None, [t1], avail_small, options), "t1") == (
        ReasonCode.INSUFFICIENT_TOTAL_TIME
    )

    # ② 截止前窗口不足
    avail_day = make_availability(windows=((0, "18:00", "22:00"),))
    t2 = task(
        "t2",
        90,
        due_at="2026-10-12T19:00+08:00",
        due_precision="datetime",
    )
    t2b = task("t2b", 240)
    assert reason_of(revise_plan(None, [t2, t2b], avail_day, options), "t2") == (
        ReasonCode.INSUFFICIENT_WINDOW_BEFORE_DUE
    )

    # ③ 任务太长不可拆分（总可用够、最长连续不足）
    avail_frag = make_availability(
        windows=((0, "18:00", "19:00"), (0, "20:00", "21:00"))
    )
    t3 = task("t3", 90, splittable=False)
    assert reason_of(revise_plan(None, [t3], avail_frag, options), "t3") == (
        ReasonCode.TASK_TOO_LONG_INDIVISIBLE
    )

    # ④ 依赖冲突
    t4p = task("t4p", 180)
    t4d = task("t4d", 30, prerequisite_ids=["t4p"])
    revision = revise_plan(None, [t4p, t4d], avail_small, options)
    assert reason_of(revision, "t4p") == ReasonCode.INSUFFICIENT_TOTAL_TIME
    assert reason_of(revision, "t4d") == ReasonCode.DEPENDENCY_CONFLICT
    for item in revision.plan.unassigned:
        assert item.suggestion


# ── 未安排清单的变化与新一致 ───────────────────────────────────────────────


def test_previously_unassigned_task_resolved():
    options = make_options()
    avail_small = make_availability(windows=((0, "18:00", "19:00"),))
    t_big = task("t_big", 90)
    plan = generate_plan([t_big], avail_small, options)
    assert [(u.task_id, u.reason_code) for u in plan.unassigned] == [
        ("t_big", ReasonCode.INSUFFICIENT_TOTAL_TIME)
    ]

    avail_big = make_availability(windows=((0, "18:00", "20:00"),))
    revision = revise_plan(plan, [t_big], avail_big, options)

    assert revision.changes.resolved_unassigned == ["t_big"]
    assert reason_of(revision, "t_big") is None
    assert any("已不再处于未安排状态" in line for line in revision.changes.summary_lines)
    assert any(e.kind == ChangeKind.ADDED for e in revision.changes.entries)


# ── 确定性与交叉校验 ───────────────────────────────────────────────────────


def test_revise_is_deterministic():
    availability = make_availability(windows=((0, "18:00", "22:00"),))
    options = make_options()
    plan = generate_plan([task("t_a", 60), task("t_b", 60)], availability, options)
    tasks = [
        task("t_a", 60),
        task("t_b", 60, due_at="2026-10-12T19:30+08:00", due_precision="datetime"),
    ]

    first = revise_plan(plan, tasks, availability, options)
    second = revise_plan(plan, tasks, availability, options)

    assert first.plan.to_dict() == second.plan.to_dict()
    assert first.changes.to_dict() == second.changes.to_dict()
    assert first.fully_valid == second.fully_valid


def test_fresh_revise_matches_generate_plan():
    availability = make_availability()
    options = make_options()
    tasks = [
        task("t_a", 60),
        task("t_b", 90),
        task("t_c", 30, due_at="2026-10-12", due_precision="date"),
    ]

    plan = generate_plan(tasks, availability, options)
    revision = revise_plan(None, tasks, availability, options)

    assert [(b.task_id, b.starts_at, b.ends_at) for b in revision.plan.blocks] == [
        (b.task_id, b.starts_at, b.ends_at) for b in plan.blocks
    ]
    assert [(u.task_id, u.reason_code) for u in revision.plan.unassigned] == [
        (u.task_id, u.reason_code) for u in plan.unassigned
    ]
