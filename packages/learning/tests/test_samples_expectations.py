"""样例驱动的完整断言：期望结果 + 硬约束校验（不依赖某一份固定输出）。"""

import pytest

from learning.samples import list_samples, run_sample
from learning.schedule.validate import validate_plan

SAMPLES = list_samples()

assert SAMPLES, "samples/ 目录下应有样例文件"


@pytest.mark.parametrize("name", SAMPLES)
def test_scheduled_and_unassigned_match_expected(name):
    result = run_sample(name)
    plan = result["plan"]
    expected = result["expected"]
    scheduled = sorted({block.task_id for block in plan.blocks})
    assert scheduled == sorted(expected.get("scheduled_tasks", [])), name
    got_unassigned = {item.task_id: item.reason_code for item in plan.unassigned}
    assert got_unassigned == expected.get("unassigned", {}), name


@pytest.mark.parametrize("name", SAMPLES)
def test_plan_warnings_match_expected(name):
    result = run_sample(name)
    codes = sorted({warning.code for warning in result["plan"].warnings})
    assert codes == sorted(result["expected"].get("plan_warnings", [])), name


@pytest.mark.parametrize("name", SAMPLES)
def test_hard_constraints_hold(name):
    """约束测试：无重叠、在窗口内、每日上限合法、前置合法、锁定块保留、完成不重排。"""
    result = run_sample(name)
    violations = validate_plan(
        result["plan"],
        result["tasks"],
        result["inputs"]["availability"],
        result["inputs"]["schedule_options"],
    )
    allowed = set(result["expected"].get("allowed_violations", []))
    codes = {violation.code for violation in violations}
    unexpected = codes - allowed
    messages = [violation.message for violation in violations if violation.code in unexpected]
    assert not unexpected, f"{name}: 出现未预期违规 {unexpected}: {messages}"


def test_baseline_taskgen_warnings():
    result = run_sample("baseline")
    codes = sorted({warning.code for warning in result["taskgen_warnings"]})
    assert codes == ["DUPLICATE_CONTENT", "TASK_VERSION_UPDATED"]


def test_baseline_overdue_reason_detail():
    result = run_sample("baseline")
    item = next(u for u in result["plan"].unassigned if u.reason_code == "DEADLINE_PASSED")
    assert item.needed_minutes == 90
    assert item.available_minutes == 0
    assert item.deadline is not None and item.suggestion


def test_baseline_locked_block_preserved():
    result = run_sample("baseline")
    locked = [block for block in result["plan"].blocks if block.locked]
    assert len(locked) == 1
    assert locked[0].id == "blk_locked_cs261_review"
    assert locked[0].starts_at == "2026-10-16T19:00+08:00"


def test_due_too_soon_reports_available_minutes():
    result = run_sample("scenarios/due_too_soon")
    item = result["plan"].unassigned[0]
    assert item.reason_code == "INSUFFICIENT_WINDOW_BEFORE_DUE"
    assert (item.needed_minutes, item.available_minutes) == (120, 60)


def test_cross_midnight_block_structure():
    result = run_sample("scenarios/cross_midnight")
    blocks = result["plan"].blocks
    assert [(b.starts_at, b.ends_at) for b in blocks] == [
        ("2026-10-12T22:00+08:00", "2026-10-12T22:50+08:00"),
        ("2026-10-12T22:50+08:00", "2026-10-12T23:40+08:00"),
        ("2026-10-12T23:40+08:00", "2026-10-13T00:30+08:00"),
    ]


def test_tight_daily_limit_split_across_days():
    result = run_sample("scenarios/tight_daily_limit")
    blocks = [block for block in result["plan"].blocks if block.task_id == "t_limit_split"]
    assert len(blocks) == 2
    assert blocks[0].starts_at.startswith("2026-10-12")
    assert blocks[1].starts_at.startswith("2026-10-13")


def test_indivisible_reports_max_contiguous():
    result = run_sample("scenarios/indivisible_too_long")
    item = next(u for u in result["plan"].unassigned if u.task_id == "t_big_indiv")
    assert item.reason_code == "TASK_TOO_LONG_INDIVISIBLE"
    assert item.max_contiguous_minutes == 30
