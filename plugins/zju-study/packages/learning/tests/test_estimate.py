"""D6 估计校正测试：逐步校正、样本不足不调整、离群稳健、界限与集成。"""

import pytest

from learning.estimate import (
    CalibrationOptions,
    CalibrationReason,
    calibrate_estimates,
)
from learning.models import Recording
from learning.samples import load_sample, parse_inputs
from learning.taskgen import TaskGenConfig, TaskGenOptions, build_tasks, scaled_minutes


def entry(task_type, estimate, actual, outcome="completed"):
    return {
        "task_id": f"t-{task_type}-{estimate}-{actual}",
        "task_type": task_type,
        "estimated_minutes": estimate,
        "outcome": outcome,
        "actual_minutes": actual,
        "difficulty": 3,
        "note": None,
        "created_at": "2026-10-09T10:00:00+08:00",
    }


def by_type(report, task_type):
    return next(t for t in report.types if t.task_type == task_type)


def test_no_feedback_means_no_adjustment():
    report = calibrate_estimates([])
    assert {t.reason for t in report.types} == {CalibrationReason.NO_SAMPLES}
    assert set(report.factors().values()) == {1.0}
    assert any("无有效反馈" in line for line in report.summary_lines())


def test_insufficient_samples_not_adjusted():
    entries = [entry("reading", 30, 60), entry("reading", 30, 60)]
    report = calibrate_estimates(entries)
    reading = by_type(report, "reading")
    assert reading.samples == 2
    assert reading.reason == CalibrationReason.INSUFFICIENT_SAMPLES
    assert reading.new_factor == 1.0
    assert reading.median_ratio == 2.0
    assert any("样本不足（有效 2 条，最少 3 条）" in line for line in report.summary_lines())


def test_gradual_adjustment_and_convergence():
    # 第 1 轮：展示 30 分钟，实际 39（比 1.3）；系数最多单步 +0.15
    first = calibrate_estimates([entry("reading", 30, 39) for _ in range(5)])
    reading1 = by_type(first, "reading")
    assert reading1.reason == CalibrationReason.ADJUSTED
    assert reading1.new_factor == 1.15

    # 第 2 轮：展示值已含 1.15 系数（30×1.15≈35），实际不变 39 → 目标 1.28（未达单步上限）
    second = calibrate_estimates([entry("reading", 35, 39) for _ in range(5)], current_factors=first.factors())
    reading2 = by_type(second, "reading")
    assert reading2.old_factor == 1.15 and reading2.new_factor == 1.28

    # 第 3 轮：展示值含 1.28 系数（30×1.28≈38），实际 39 → 比约 1.03，进入稳定区间
    third = calibrate_estimates([entry("reading", 38, 39) for _ in range(5)], current_factors=second.factors())
    reading3 = by_type(third, "reading")
    assert reading3.reason == CalibrationReason.STABLE
    assert reading3.new_factor == 1.28


def test_partial_and_skipped_excluded():
    entries = [
        entry("reading", 30, 45, outcome="partial"),
        entry("reading", 30, None, outcome="skipped"),
        entry("reading", 30, None),
        entry("reading", 0, 45),
        entry("reading", 30, 0),
    ]
    report = calibrate_estimates(entries)
    reading = by_type(report, "reading")
    assert reading.samples == 0 and reading.excluded == 5
    assert reading.reason == CalibrationReason.NO_SAMPLES


def test_outliers_do_not_dominate():
    entries = [
        entry("reading", 30, 36),   # 1.2
        entry("reading", 30, 36),   # 1.2
        entry("reading", 30, 36),   # 1.2
        entry("reading", 30, 300),  # 10.0 → 截断到 3.0
    ]
    report = calibrate_estimates(entries)
    reading = by_type(report, "reading")
    assert reading.median_ratio == 1.2
    assert reading.new_factor == 1.15  # 单步上限，而非被离群值拉大


def test_stable_band_keeps_existing_factor():
    entries = [entry("reading", 30, 31) for _ in range(4)]  # 比约 1.033
    report = calibrate_estimates(entries, current_factors={"reading": 1.2})
    reading = by_type(report, "reading")
    assert reading.reason == CalibrationReason.STABLE
    assert reading.new_factor == 1.2
    assert any("接近 1" in line for line in report.summary_lines())


def test_bounds_stop_movement():
    high = calibrate_estimates([entry("watching", 30, 300) for _ in range(3)], current_factors={"watching": 2.0})
    assert by_type(high, "watching").reason == CalibrationReason.AT_BOUND
    assert by_type(high, "watching").new_factor == 2.0
    assert any("已达系数上限 2.00" in line for line in high.summary_lines())

    low = calibrate_estimates([entry("watching", 100, 20) for _ in range(3)], current_factors={"watching": 0.5})
    assert by_type(low, "watching").reason == CalibrationReason.AT_BOUND
    assert by_type(low, "watching").new_factor == 0.5


def test_invalid_entries_counted_and_ignored():
    entries = [
        entry("reading", 30, 45),
        {"outcome": "completed", "actual_minutes": 45},  # 缺 task_type
        entry("astrology", 30, 45),  # 未知类型
        "not-a-dict",
    ]
    report = calibrate_estimates(entries)
    assert report.invalid_entries == 3
    assert by_type(report, "reading").samples == 1
    assert any("已忽略" in line for line in report.summary_lines())


def test_report_serialization_and_factors_complete():
    report = calibrate_estimates([entry("reading", 30, 39) for _ in range(3)])
    data = report.to_dict()
    assert data["version"] and data["options"]["min_samples"] == 3
    assert len(data["types"]) == 5
    assert set(report.factors()) == {"assignment", "practice", "reading", "review", "watching"}
    assert data["summary"] == report.summary_lines()


def test_determinism_and_order_independence():
    entries = [entry("reading", 30, 39) for _ in range(3)] + [
        entry("watching", 60, 72) for _ in range(3)
    ]
    a = calibrate_estimates(entries)
    b = calibrate_estimates(list(reversed(entries)))
    assert a.to_dict() == b.to_dict()


def test_scaled_minutes_rounding_and_guard():
    cfg = TaskGenConfig(type_estimate_factors={"reading": 1.15, "watching": 0.5})
    assert scaled_minutes(30, "reading", cfg) == 35  # 34.5 → 就近取整
    assert scaled_minutes(1, "watching", cfg) == 1  # 0.5 → 至少 1 分钟
    assert scaled_minutes(30, "watching", cfg) == 15
    assert scaled_minutes(30, "assignment", cfg) == 30  # 无系数
    for bad in (0, -1, True, "1.5"):
        assert scaled_minutes(30, "reading", TaskGenConfig(type_estimate_factors={"reading": bad})) == 30


def test_config_round_trip_keeps_factors():
    cfg = TaskGenConfig(type_estimate_factors={"reading": 1.15, "watching": 0.9})
    again = TaskGenConfig.from_dict(cfg.to_dict())
    assert again.type_estimate_factors == {"reading": 1.15, "watching": 0.9}
    assert TaskGenConfig.from_dict({}).type_estimate_factors == {}


def test_build_tasks_applies_factors_to_inferred_only():
    inp = parse_inputs(load_sample("baseline"))
    cfg = TaskGenConfig(type_estimate_factors={"reading": 1.2, "watching": 0.5, "assignment": 2.0})
    result = build_tasks(
        inp["resources"], inp["assignments"], inp["recordings"], TaskGenOptions(config=cfg)
    )
    tree = next(t for t in result.tasks if t.title == "第4章 树与二叉树（课件）")
    assert tree.estimated_minutes == 166  # 46 页 × 3 × 1.2 = 165.6 → 166
    rec = next(t for t in result.tasks if "树的遍历（回放）" in t.title)
    assert rec.estimated_minutes == 45  # 90 × 0.5
    hw5 = next(t for t in result.tasks if "作业5" in t.title)
    assert hw5.estimated_minutes == 180  # 默认 90 × 2.0（规则推断）
    hw4 = next(t for t in result.tasks if "作业4" in t.title)
    assert hw4.estimated_minutes == 120 and hw4.estimate_provenance == "user"  # 用户自设不被改写


def test_missing_duration_warning_uses_scaled_value():
    rec = Recording(id="rec_x", course_id="crs_x", title="无时长回放")
    cfg = TaskGenConfig(type_estimate_factors={"watching": 1.5})
    result = build_tasks([], [], [rec], TaskGenOptions(config=cfg))
    assert result.tasks[0].estimated_minutes == 90  # 默认 60 × 1.5
    warning = next(w for w in result.warnings if w.code == "MISSING_DURATION")
    assert "90 分钟" in warning.message


def test_default_options_leave_estimates_unchanged():
    report = calibrate_estimates([], options=CalibrationOptions(min_samples=5, max_step=0.05))
    assert by_type(report, "reading").new_factor == 1.0
    assert report.options.max_step == 0.05
    assert report.to_dict()["options"]["min_samples"] == 5
