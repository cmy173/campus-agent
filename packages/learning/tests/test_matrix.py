"""D9 测试矩阵：M1 任务书 11 项边界全覆盖的定位与补强。

| # | 边界 | 覆盖位置（↗=本文件新增） |
|---|------|--------------------------|
| 1 | 零空闲时间 | ↗ test_zero_free_time_all_unassigned_by_reason；样例 scenarios/zero_free_time |
| 2 | 跨日窗口 | test_samples_expectations.test_cross_midnight_block_structure |
| 3 | 每日上限 | test_samples_expectations.test_tight_daily_limit_split_across_days；test_revise.test_locked_block_exceeds_daily_limit_reported；↗ test_validate_detects_capacity_violations |
| 4 | 已过期作业 | test_schedule_unit.test_deadline_already_passed；test_samples_expectations.test_baseline_overdue_reason_detail |
| 5 | 缺失截止时间 | ↗ test_missing_due_is_scheduled_not_due_passed；test_taskgen.test_due_facts_not_guessed |
| 6 | 锁定任务 | test_samples_expectations.test_baseline_locked_block_preserved；test_revise 锁定块冲突 5 例；↗ test_validate_detects_capacity_violations |
| 7 | 依赖环 | ↗ test_dependency_cycle_members_unassigned_others_kept；test_revise.test_dependency_cycle_releases_blocks；样例 scenarios/dependency_cycle；↗ test_validate_detects_prerequisite_violations |
| 8 | 不可拆分长任务 | test_samples_expectations.test_indivisible_reports_max_contiguous |
| 9 | 截止时间改变 | test_revise.test_earlier_deadline_swaps_tasks_and_formats_summary |
| 10 | 模型超时 | test_model.test_timeout_is_mapped_and_retried；test_model.test_timeout_without_retry |
| 11 | 伪造引用 | test_model.test_fabricated_task_reference_rejected；test_model.test_fabricated_prerequisite_rejected |
| 附 | 校验器反向用例（证明约束预言机真能查出违规） | ↗ test_validate_detects_block_level_violations / capacity / prerequisite |

排程断言遵循“约束测试优先”：以原因码、告警码与 validate_plan 结果为准，不固化具体时段布局。
"""

from learning.models import Availability, LearningTask, Plan, PlanBlock, WeeklyWindow
from learning.schedule.engine import (
    ReasonCode,
    ScheduleConfig,
    ScheduleOptions,
    WarningCode,
    generate_plan,
)
from learning.schedule.validate import validate_plan

MONDAY = ((0, "19:00", "22:30"),)


def make_availability(windows=MONDAY, limit=None):
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
        title=f"示例任务 {task_id}",
        estimated_minutes=minutes,
        **kwargs,
    )


def block(block_id, task_id, starts_at, ends_at, locked=False):
    return PlanBlock(id=block_id, task_id=task_id, starts_at=starts_at, ends_at=ends_at, locked=locked)


def make_plan(blocks):
    return Plan(
        horizon_start="2026-10-12T00:00+08:00",
        horizon_end="2026-10-19T00:00+08:00",
        input_revision=1,
        blocks=blocks,
    )


def unassigned_reasons(plan):
    return {item.task_id: item.reason_code for item in plan.unassigned}


# ── 矩阵补强：引擎侧单元约束测试 ─────────────────────────────────────────────


def test_zero_free_time_all_unassigned_by_reason():
    """无任何可用窗口：全部未安排；有截止→窗口不足，无截止→总时间不足；EMPTY_HORIZON 告警。"""
    tasks = [
        task("t_no_due", 45),
        task("t_due", 60, due_at="2026-10-16T00:00+08:00", due_precision="date"),
    ]
    availability = make_availability(windows=())
    options = make_options()
    plan = generate_plan(tasks, availability, options)
    assert plan.blocks == []
    assert unassigned_reasons(plan) == {
        "t_no_due": ReasonCode.INSUFFICIENT_TOTAL_TIME,
        "t_due": ReasonCode.INSUFFICIENT_WINDOW_BEFORE_DUE,
    }
    assert WarningCode.EMPTY_HORIZON in {w.code for w in plan.warnings}
    assert validate_plan(plan, tasks, availability, options) == []


def test_missing_due_is_scheduled_not_due_passed():
    """缺失截止（null/unknown）不得被当作已过期：正常排入窗口，未安排清单为空。"""
    missing = task("t_missing", 60)
    availability = make_availability()
    options = make_options()
    plan = generate_plan([missing], availability, options)
    assert [b.task_id for b in plan.blocks] == ["t_missing"]
    assert plan.unassigned == []
    assert validate_plan(plan, [missing], availability, options) == []


def test_dependency_cycle_members_unassigned_others_kept():
    """环成员与其依赖者→DEPENDENCY_CONFLICT；非环任务照常排入；DEPENDENCY_CYCLE 告警。"""
    a = task("t_cyc_a", 30, prerequisite_ids=["t_cyc_b"])
    b = task("t_cyc_b", 30, prerequisite_ids=["t_cyc_a"])
    c = task("t_cyc_c", 30, prerequisite_ids=["t_cyc_a"])
    ok = task("t_ok", 30)
    tasks = [a, b, c, ok]
    availability = make_availability(windows=((0, "19:00", "21:00"),))
    options = make_options()
    plan = generate_plan(tasks, availability, options)
    assert [blk.task_id for blk in plan.blocks] == ["t_ok"]
    assert unassigned_reasons(plan) == {
        "t_cyc_a": ReasonCode.DEPENDENCY_CONFLICT,
        "t_cyc_b": ReasonCode.DEPENDENCY_CONFLICT,
        "t_cyc_c": ReasonCode.DEPENDENCY_CONFLICT,
    }
    assert WarningCode.DEPENDENCY_CYCLE in {w.code for w in plan.warnings}
    assert validate_plan(plan, tasks, availability, options) == []


# ── 校验器反向用例：逐类构造违规，证明能被查出（非空转预言机） ────────────────

VALID = block("blk_a", "t_a", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")


def test_validate_accepts_valid_plan():
    tasks = [task("t_a", 60)]
    availability = make_availability()
    assert validate_plan(make_plan([VALID]), tasks, availability, make_options()) == []


def test_validate_detects_block_level_violations():
    availability = make_availability()
    options = make_options()
    base = [task("t_a", 60)]

    overlap = make_plan(
        [
            VALID,
            block("blk_a2", "t_a", "2026-10-12T19:30+08:00", "2026-10-12T20:30+08:00"),
        ]
    )
    codes = {v.code for v in validate_plan(overlap, base, availability, options)}
    assert "BLOCK_OVERLAP" in codes

    outside = make_plan([block("blk_early", "t_a", "2026-10-12T18:00+08:00", "2026-10-12T19:00+08:00")])
    codes = {v.code for v in validate_plan(outside, base, availability, options)}
    assert "BLOCK_OUTSIDE_WINDOW" in codes

    ghost = make_plan([block("blk_ghost", "t_ghost", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")])
    codes = {v.code for v in validate_plan(ghost, base, availability, options)}
    assert "UNKNOWN_TASK_BLOCK" in codes

    bad_time = make_plan([block("blk_bad", "t_a", "不是时间", "2026-10-12T20:00+08:00")])
    codes = {v.code for v in validate_plan(bad_time, base, availability, options)}
    assert "BLOCK_INVALID_TIME" in codes

    half = make_plan([block("blk_half", "t_a", "2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00")])
    codes = {v.code for v in validate_plan(half, base, availability, options)}
    assert "DURATION_MISMATCH" in codes

    due = [task("t_due", 60, due_at="2026-10-12T19:30+08:00", due_precision="datetime")]
    late = make_plan([block("blk_late", "t_due", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")])
    codes = {v.code for v in validate_plan(late, due, availability, options)}
    assert "BLOCK_AFTER_DEADLINE" in codes

    done = [task("t_a", 60, status="completed")]
    codes = {v.code for v in validate_plan(make_plan([VALID]), done, availability, options)}
    assert "NON_PENDING_TASK_SCHEDULED" in codes


def test_validate_detects_capacity_violations():
    tasks = [task("t_60a", 60), task("t_60b", 60)]
    limit_availability = make_availability(limit=90)
    blocks = [
        block("blk_60a", "t_60a", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00"),
        block("blk_60b", "t_60b", "2026-10-12T20:00+08:00", "2026-10-12T21:00+08:00"),
    ]
    codes = {v.code for v in validate_plan(make_plan(blocks), tasks, limit_availability, make_options())}
    assert "DAILY_LIMIT_EXCEEDED" in codes

    locked = PlanBlock(
        id="blk_locked",
        task_id="t_l",
        starts_at="2026-10-12T19:00+08:00",
        ends_at="2026-10-12T20:00+08:00",
        locked=True,
    )
    options = make_options(locked_blocks=[locked])
    moved = block("blk_locked", "t_l", "2026-10-12T20:00+08:00", "2026-10-12T21:00+08:00", locked=True)
    codes = {v.code for v in validate_plan(make_plan([moved]), [task("t_l", 60)], make_availability(), options)}
    assert "LOCKED_BLOCK_NOT_PRESERVED" in codes


def test_validate_detects_prerequisite_violations():
    availability = make_availability()
    options = make_options()

    pre = task("t_pre", 60)
    post = task("t_post", 30, prerequisite_ids=["t_pre"])
    order_blocks = [
        block("blk_post", "t_post", "2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00"),
        block("blk_pre", "t_pre", "2026-10-12T19:30+08:00", "2026-10-12T20:30+08:00"),
    ]
    codes = {v.code for v in validate_plan(make_plan(order_blocks), [pre, post], availability, options)}
    assert "PREREQUISITE_ORDER_VIOLATED" in codes

    only_post = [block("blk_post", "t_post", "2026-10-12T19:00+08:00", "2026-10-12T19:30+08:00")]
    codes = {v.code for v in validate_plan(make_plan(only_post), [pre, post], availability, options)}
    assert "PREREQUISITE_UNSCHEDULED" in codes

    cancelled_pre = task("t_pre", 60, status="cancelled")
    codes = {v.code for v in validate_plan(make_plan(only_post), [cancelled_pre, post], availability, options)}
    assert "PREREQUISITE_UNAVAILABLE" in codes
