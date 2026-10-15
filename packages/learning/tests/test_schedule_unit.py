"""排程引擎单元测试：优先级、完成/取消任务、截止精度、无效数据。"""

from learning.models import Availability, LearningTask, WeeklyWindow
from learning.schedule.engine import ReasonCode, ScheduleConfig, ScheduleOptions, WarningCode, generate_plan

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
        title=f"示例任务 {task_id}",
        estimated_minutes=minutes,
        **kwargs,
    )


def test_priority_ordering_in_scarce_window():
    tasks = [
        task("t_low", 60, priority=1),
        task("t_high", 60, priority=9),
    ]
    availability = make_availability(windows=((0, "19:00", "20:00"),))
    plan = generate_plan(tasks, availability, make_options())
    assert {b.task_id for b in plan.blocks} == {"t_high"}
    assert [(u.task_id, u.reason_code) for u in plan.unassigned] == [("t_low", ReasonCode.INSUFFICIENT_TOTAL_TIME)]


def test_completed_task_is_not_rescheduled_and_satisfies_prereq():
    tasks = [
        task("t_done", 60, status="completed"),
        task("t_next", 60, prerequisite_ids=["t_done"]),
    ]
    plan = generate_plan(tasks, make_availability(), make_options())
    assert {b.task_id for b in plan.blocks} == {"t_next"}
    assert plan.unassigned == []


def test_cancelled_prereq_leads_to_dependency_conflict():
    tasks = [
        task("t_can", 30, status="cancelled"),
        task("t_dep", 30, prerequisite_ids=["t_can"]),
    ]
    plan = generate_plan(tasks, make_availability(), make_options())
    assert plan.blocks == []
    assert [(u.task_id, u.reason_code) for u in plan.unassigned] == [("t_dep", ReasonCode.DEPENDENCY_CONFLICT)]


def test_due_precision_date_vs_datetime():
    # date 精度：截止为次日 00:00（当日可排）
    date_precision = task("t_date", 60, due_at="2026-10-12T00:00+08:00", due_precision="date")
    plan = generate_plan([date_precision], make_availability(), make_options())
    assert {b.task_id for b in plan.blocks} == {"t_date"}

    # datetime 精度：截止为该精确时刻，周一晚窗口在截止之后
    exact = task("t_exact", 60, due_at="2026-10-12T00:00+08:00", due_precision="datetime")
    plan2 = generate_plan([exact], make_availability(), make_options())
    assert plan2.blocks == []
    assert plan2.unassigned[0].reason_code == ReasonCode.INSUFFICIENT_WINDOW_BEFORE_DUE


def test_deadline_already_passed():
    past = task("t_past", 60, due_at="2026-10-08T00:00+08:00", due_precision="date")
    plan = generate_plan([past], make_availability(), make_options())
    assert plan.unassigned[0].reason_code == ReasonCode.DEADLINE_PASSED


def test_invalid_duration_and_duplicate_ids():
    tasks = [
        task("t_bad", 0),
        task("t_bad", 30),
        task("t_ok", 30),
    ]
    plan = generate_plan(tasks, make_availability(), make_options())
    by_task = {u.task_id: u.reason_code for u in plan.unassigned}
    assert by_task.get("t_bad") == ReasonCode.INVALID_TASK
    assert {b.task_id for b in plan.blocks} == {"t_ok"}
    codes = {w.code for w in plan.warnings}
    assert WarningCode.INVALID_DURATION in codes
    assert WarningCode.DUPLICATE_TASK_ID in codes


def test_dependent_of_unscheduled_prereq_reports_conflict():
    # 前置任务因窗口不足未排，后继任务 → 依赖冲突
    tasks = [
        task("t_prereq", 120, due_at="2026-10-13T00:00+08:00", due_precision="date"),
        task("t_after", 30, prerequisite_ids=["t_prereq"]),
    ]
    availability = make_availability(windows=((0, "19:00", "20:00"),))
    plan = generate_plan(tasks, availability, make_options())
    by_task = {u.task_id: u.reason_code for u in plan.unassigned}
    assert by_task["t_prereq"] == ReasonCode.INSUFFICIENT_WINDOW_BEFORE_DUE
    assert by_task["t_after"] == ReasonCode.DEPENDENCY_CONFLICT


def test_summary_counts():
    tasks = [task("t_a", 60), task("t_b", 30)]
    plan = generate_plan(tasks, make_availability(), make_options())
    summary = plan.summary
    assert summary["scheduled_task_count"] == 2
    assert summary["unassigned_task_count"] == 0
    assert summary["scheduled_minutes"] == 90
    assert plan.input_revision is None
