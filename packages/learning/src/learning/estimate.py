"""D6 估计校正（规则路径，无模型调用）。

用用户的“实际耗时反馈”逐步校正预计时长系数，供 `build_tasks` 下一次生成时应用。

输入（由 B 将 FeedbackRecord 与任务记录连接后传入；本模块不访问数据库）：

    {"task_type": "reading", "estimated_minutes": 30,   # 当时对用户展示的预计分钟
     "outcome": "completed", "actual_minutes": 42,      # 契约 FeedbackRecord 字段
     ...其余字段（id/created_at/note/difficulty 等）忽略}

规则口径（宁缺毋假）：

- 仅 `completed` 且 `actual_minutes` ≥ 1、`estimated_minutes` ≥ 1 的记录参与计算；
  `partial`/`skipped`/缺实际耗时的记录只计入 excluded，不推断、不插值。
- 单条比率 actual/estimated 先截断到 [1/ratio_clamp, ratio_clamp] 抑制离群值，
  再取**中位数**（对极端值稳健），不采用均值。
- 逐次校正：系数每次最多向目标移动 max_step（默认 0.15），并截断在
  [factor_min, factor_max]；结果保留 2 位小数。
- 语义说明：记录中的 estimated_minutes 是**当时的展示值**（可能已含当时系数）。
  历史记录跨系数代混用时，单步上限与稳定区间共同限制来回震荡；收敛后
  （展示值与实际一致）中位比≈1，系数保持稳定。
- 每类样本数不足 min_samples（默认 3）时**不调整**，报告原因（NO_SAMPLES /
  INSUFFICIENT_SAMPLES）；中位比接近 1（±stable_band）时保持系数并记 STABLE。
- 确定性：同一输入必得同一输出；不输出置信区间等未验证的“精确预测”。

系数应用：`TaskGenConfig.type_estimate_factors[task_type]`（见 taskgen.py）。
用户自设的耗时（任务提示 / 作业自带 estimated_minutes）不参与校正、不被系数改写。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

CALIBRATION_VERSION = "0.1.0"

TASK_TYPES: tuple[str, ...] = ("assignment", "practice", "reading", "review", "watching")


class CalibrationReason:
    NO_SAMPLES = "NO_SAMPLES"  # 无任何有效样本
    INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"  # 样本数不足，未调整
    STABLE = "STABLE"  # 中位比处于稳定区间，无需调整
    ADJUSTED = "ADJUSTED"  # 系数已逐步调整
    AT_BOUND = "AT_BOUND"  # 目标超界，系数已在下/上限，未再移动


@dataclass
class CalibrationOptions:
    min_samples: int = 3
    max_step: float = 0.15
    factor_min: float = 0.5
    factor_max: float = 2.0
    ratio_clamp: float = 3.0
    stable_band: float = 0.05

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_samples": self.min_samples,
            "max_step": self.max_step,
            "factor_min": self.factor_min,
            "factor_max": self.factor_max,
            "ratio_clamp": self.ratio_clamp,
            "stable_band": self.stable_band,
        }


@dataclass
class TypeCalibration:
    task_type: str
    samples: int
    excluded: int
    median_ratio: float | None
    old_factor: float
    new_factor: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "samples": self.samples,
            "excluded": self.excluded,
            "median_ratio": self.median_ratio,
            "old_factor": self.old_factor,
            "new_factor": self.new_factor,
            "reason": self.reason,
        }


@dataclass
class CalibrationReport:
    types: list[TypeCalibration] = field(default_factory=list)
    invalid_entries: int = 0
    options: CalibrationOptions = field(default_factory=CalibrationOptions)

    def factors(self) -> dict[str, float]:
        """完整系数表（含未调整项），供 B 存入配置后回传。"""
        return {t.task_type: t.new_factor for t in self.types}

    def summary_lines(self) -> list[str]:
        out: list[str] = []
        for t in self.types:
            factor = f"{t.old_factor:.2f}"
            if t.reason == CalibrationReason.NO_SAMPLES:
                out.append(f"估计校正：{t.task_type} 无有效反馈，未调整（系数保持 {factor}）")
            elif t.reason == CalibrationReason.INSUFFICIENT_SAMPLES:
                out.append(
                    f"估计校正：{t.task_type} 样本不足（有效 {t.samples} 条，"
                    f"最少 {self.options.min_samples} 条），未调整（系数保持 {factor}）"
                )
            elif t.reason == CalibrationReason.STABLE:
                out.append(
                    f"估计校正：{t.task_type} 中位比 {t.median_ratio:.2f} 接近 1，"
                    f"无需调整（系数保持 {factor}）"
                )
            elif t.reason == CalibrationReason.AT_BOUND:
                bound = "上限" if t.new_factor >= self.options.factor_max else "下限"
                out.append(
                    f"估计校正：{t.task_type} 已达系数{bound} {t.new_factor:.2f}，"
                    f"不再移动（目标方向仍偏差）"
                )
            else:
                out.append(
                    f"估计校正：{t.task_type} 样本 {t.samples} 条，中位比 {t.median_ratio:.2f}，"
                    f"系数 {factor} → {t.new_factor:.2f}"
                )
        if self.invalid_entries:
            out.append(f"估计校正：{self.invalid_entries} 条记录缺少必需字段或类型未知，已忽略")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CALIBRATION_VERSION,
            "types": [t.to_dict() for t in self.types],
            "invalid_entries": self.invalid_entries,
            "options": self.options.to_dict(),
            "summary": self.summary_lines(),
        }


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def calibrate_estimates(
    entries: list[dict[str, Any]],
    *,
    current_factors: dict[str, float] | None = None,
    options: CalibrationOptions | None = None,
) -> CalibrationReport:
    """按任务类型校正估计系数；返回报告（含未调整类型），不改写任何外部状态。"""
    options = options or CalibrationOptions()
    current = dict(current_factors or {})
    samples: dict[str, list[float]] = {t: [] for t in TASK_TYPES}
    excluded: dict[str, int] = {t: 0 for t in TASK_TYPES}
    invalid = 0
    for entry in entries:
        if not isinstance(entry, dict):
            invalid += 1
            continue
        task_type = entry.get("task_type")
        if task_type not in samples:
            invalid += 1
            continue
        estimate = entry.get("estimated_minutes")
        actual = entry.get("actual_minutes")
        if entry.get("outcome") == "completed" and _is_positive_int(estimate) and _is_positive_int(actual):
            ratio = actual / estimate
            clamped = min(options.ratio_clamp, max(1 / options.ratio_clamp, ratio))
            samples[task_type].append(clamped)
        else:
            excluded[task_type] += 1

    report = CalibrationReport(invalid_entries=invalid, options=options)
    for task_type in TASK_TYPES:
        old_factor = float(current.get(task_type, 1.0))
        values = samples[task_type]
        median_ratio: float | None = None
        if not values:
            report.types.append(
                TypeCalibration(task_type, 0, excluded[task_type], None, old_factor, old_factor, CalibrationReason.NO_SAMPLES)
            )
            continue
        median_ratio = round(statistics.median(values), 3)
        if len(values) < options.min_samples:
            reason = CalibrationReason.INSUFFICIENT_SAMPLES
            new_factor = old_factor
        elif abs(median_ratio - 1.0) <= options.stable_band:
            reason = CalibrationReason.STABLE
            new_factor = old_factor
        else:
            target = old_factor * median_ratio
            step = min(options.max_step, max(-options.max_step, target - old_factor))
            moved = old_factor + step
            new_factor = round(min(options.factor_max, max(options.factor_min, moved)), 2)
            old_rounded = round(old_factor, 2)
            if abs(new_factor - old_rounded) < 0.005:
                new_factor = old_rounded
                reason = CalibrationReason.AT_BOUND
            else:
                reason = CalibrationReason.ADJUSTED
        report.types.append(
            TypeCalibration(task_type, len(values), excluded[task_type], median_ratio, round(old_factor, 2), new_factor, reason)
        )
    return report
