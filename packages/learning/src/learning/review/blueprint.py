"""试卷蓝图：材料分析 + 用户约束 → 题型/难度/分值/用时配额 + 覆盖规划 + A 检索请求。

- 用户指定的题数/分数/章节是约束：引擎原样保留，不能满足时返回原因（不替用户改约束）。
- 分值分配保证“配额与总分一致”：内部以 0.5 分为单位分配，逐题分值由 distribute_points 确定性生成。
- search_requests 只含最小检索词/题型/知识点，不携带整份学校材料（发给 A 的题库服务）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .knowledge import analyze_materials
from .snapshot import material_fingerprint
from .types import (
    DEFAULT_DIFFICULTY_MIX,
    DEFAULT_MINUTES_BY_TYPE,
    DEFAULT_POINTS_BY_TYPE,
    GENERATION_MODES,
    QUESTION_TYPES,
    REVIEW_ENGINE_VERSION,
    BlueprintConstraints,
    KnowledgePoint,
    KpPlan,
    MaterialSnapshot,
    MaterialsAnalysis,
    PaperBlueprint,
    ReviewError,
    ReviewErrorCode,
    ReviewLimits,
    SearchRequest,
    TypeQuota,
    normalize_difficulty,
    normalize_question_type,
)

# 用户未指定题数时的默认配比（20 题 / 88 分；用户可全部覆盖）
DEFAULT_TYPE_COUNTS = {"choice": 8, "true_false": 4, "fill_blank": 4, "calculation": 2, "short_answer": 2}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _allocate(total: int, weights: list[float], min_each: int = 0) -> list[int]:
    """整数分配（最大余数法）：min_each=1 时先按权重给前 total 名各 1，再分余量。"""
    n = len(weights)
    out = [0] * n
    if total <= 0 or n == 0:
        return out
    order = sorted(range(n), key=lambda i: (-weights[i], i))
    seeded = min(total, n) if min_each else 0
    for i in order[:seeded]:
        out[i] = 1
    remaining = total - seeded
    if remaining > 0:
        s = sum(weights) or 1.0
        shares = [remaining * w / s for w in weights]
        base = [int(x) for x in shares]
        rem = remaining - sum(base)
        errs = sorted(range(n), key=lambda i: (-(shares[i] - base[i]), i))
        for i in errs[:rem]:
            base[i] += 1
        out = [out[i] + base[i] for i in range(n)]
    return out


def distribute_points(subtotal: float, count: int) -> list[float]:
    """把某题型的总分确定性拆到每题（0.5 分粒度；前 rem 题多 0.5）。Σ == subtotal。"""
    if count <= 0:
        return []
    units = int(round(subtotal * 2))
    base, rem = divmod(units, count)
    points = [0.5 * base] * count
    for i in range(rem):
        points[i] += 0.5
    return points


def _normalize_constraints(constraints: BlueprintConstraints | dict[str, Any] | None) -> BlueprintConstraints:
    if constraints is None:
        return BlueprintConstraints()
    if isinstance(constraints, dict):
        return BlueprintConstraints.from_dict(constraints)
    return constraints


def _normalize_types(raw_types: dict[str, int], limits: ReviewLimits) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, count in raw_types.items():
        qtype = normalize_question_type(str(key))
        if qtype is None:
            raise ReviewError(
                ReviewErrorCode.CONSTRAINT_INVALID,
                f"不支持的题型：{key!r}（支持：{'、'.join(QUESTION_TYPES)}）",
                details={"qtype": str(key), "supported": list(QUESTION_TYPES)},
            )
        try:
            value = int(count)
        except (TypeError, ValueError):
            raise ReviewError(
                ReviewErrorCode.CONSTRAINT_INVALID, f"题型 {qtype} 的题数必须为整数：{count!r}"
            ) from None
        if value < 0:
            raise ReviewError(ReviewErrorCode.CONSTRAINT_INVALID, f"题型 {qtype} 的题数不能为负数：{value}")
        normalized[qtype] = normalized.get(qtype, 0) + value
    total = sum(normalized.values())
    if raw_types and total == 0:
        raise ReviewError(ReviewErrorCode.CONSTRAINT_INVALID, "题数须大于 0（各题型题数均为 0）")
    if total > limits.max_questions:
        raise ReviewError(
            ReviewErrorCode.CONSTRAINT_INVALID,
            f"题数 {total} 超出上限 {limits.max_questions}，请减少题数或调整上限",
            details={"total": total, "max_questions": limits.max_questions},
        )
    return normalized


def _normalize_difficulty(raw: dict[str, float] | None) -> dict[str, float]:
    if raw is None:
        return dict(DEFAULT_DIFFICULTY_MIX)
    normalized: dict[str, float] = {}
    for key, value in raw.items():
        level = normalize_difficulty(str(key))
        if level is None:
            raise ReviewError(
                ReviewErrorCode.CONSTRAINT_INVALID,
                f"不支持的难度：{key!r}（支持：easy/medium/hard 或 简单/中等/较难）",
            )
        normalized[level] = normalized.get(level, 0.0) + float(value)
    total = sum(normalized.values())
    if abs(total - 1.0) > 0.01:
        raise ReviewError(
            ReviewErrorCode.CONSTRAINT_INVALID,
            f"难度比例之和须为 1（当前为 {round(total, 4)}）",
            details={"difficulty": normalized},
        )
    return {level: round(value, 4) for level, value in normalized.items()}


def _derive_counts(total_points: float) -> dict[str, int]:
    """仅给总分时：按默认配比缩放题数（每题分值再按类型微调至总分精确一致）。"""
    default_total = sum(DEFAULT_TYPE_COUNTS[t] * DEFAULT_POINTS_BY_TYPE[t] for t in DEFAULT_TYPE_COUNTS)
    scale = total_points / default_total if default_total else 0.0
    counts = {t: max(0, int(round(DEFAULT_TYPE_COUNTS[t] * scale))) for t in DEFAULT_TYPE_COUNTS}
    if sum(counts.values()) == 0:
        counts = {"choice": 1}
    return counts


def _match_sections(requested: list[str], available: list[str]) -> list[str]:
    """章节名按去空白/大小写归一匹配到实际章节；匹配不到返回空 → 调用方报不可满足。"""

    def norm(s: str) -> str:
        return "".join(s.split()).casefold()

    chosen: list[str] = []
    for want in requested:
        hit = next((a for a in available if norm(a) == norm(want)), None)
        if hit is None:
            return []
        if hit not in chosen:
            chosen.append(hit)
    return chosen


def _kp_reasons(kp: KnowledgePoint) -> list[str]:
    reasons: list[str] = []
    if kp.assignment_assessed:
        reasons.append("作业中考察")
    if kp.note_requested:
        reasons.append("备注要求")
    if kp.courseware_evidence > 1:
        reasons.append("课件多次出现")
    if kp.origin == "assignment":
        reasons.append("作业独有")
    if not reasons:
        reasons.append("课件出现")
    return reasons


def build_blueprint(
    snapshot: MaterialSnapshot,
    constraints: BlueprintConstraints | dict[str, Any] | None = None,
    analysis: MaterialsAnalysis | None = None,
    limits: ReviewLimits | None = None,
) -> PaperBlueprint:
    """材料快照 + 约束 → 试卷蓝图（含配额、覆盖规划、检索请求；不可满足时返回原因）。"""
    limits = limits or ReviewLimits()
    cons = _normalize_constraints(constraints)
    if cons.mode not in GENERATION_MODES:
        raise ReviewError(
            ReviewErrorCode.CONSTRAINT_INVALID,
            f"不支持的出卷模式：{cons.mode!r}（支持：{'、'.join(GENERATION_MODES)}）",
            details={"mode": cons.mode, "supported": list(GENERATION_MODES)},
        )

    error_gaps = [g for g in snapshot.gaps if g.severity == "error"]
    if not snapshot.has_readable_text():
        raise ReviewError(
            ReviewErrorCode.MATERIAL_UNPARSED,
            "没有可读的正文，无法出卷：" + ("；".join(g.message for g in error_gaps) or "未提供材料"),
            details={"gaps": [g.to_dict() for g in error_gaps]},
        )

    analysis = analysis or analyze_materials(snapshot)
    if not analysis.knowledge_points:
        raise ReviewError(ReviewErrorCode.MATERIAL_UNPARSED, "未能从材料中提取任何知识点，无法出卷")

    # 题型配额：用户显式题数优先；仅给总分时按默认配比推导；都没有则用默认配比
    types_requested = _normalize_types(cons.types, limits)
    if types_requested:
        counts = types_requested
    elif cons.total_points is not None:
        counts = _derive_counts(cons.total_points)
        total = sum(counts.values())
        if total > limits.max_questions:
            raise ReviewError(
                ReviewErrorCode.CONSTRAINT_INVALID,
                f"按总分推导的题数 {total} 超出上限 {limits.max_questions}",
                details={"total": total, "max_questions": limits.max_questions},
            )
    else:
        counts = dict(DEFAULT_TYPE_COUNTS)
    type_order = [t for t in QUESTION_TYPES if counts.get(t)]

    # 难度比例：显式校验（校验和不满足返回原因，不改用户值）
    difficulty_mix = _normalize_difficulty(cons.difficulty)

    # 章节约束：匹配实际可读章节；匹配不到 → 不可满足（列出可用章节）
    section_constraint = list(cons.sections)
    chosen_sections: list[str] | None = None
    if section_constraint:
        matched = _match_sections(section_constraint, analysis.sections)
        if not matched:
            raise ReviewError(
                ReviewErrorCode.CONSTRAINT_UNSATISFIABLE,
                "指定的章节在可读材料中不存在，请检查章节名或先完成解析",
                details={"requested": section_constraint, "available": list(analysis.sections)},
            )
        chosen_sections = matched

    kps = [
        kp
        for kp in analysis.knowledge_points
        if chosen_sections is None or kp.section in chosen_sections
    ]
    if not kps:
        raise ReviewError(
            ReviewErrorCode.CONSTRAINT_UNSATISFIABLE,
            "所选章节内没有可用于出题的知识点",
            details={"sections": chosen_sections or list(analysis.sections)},
        )

    total_questions = sum(counts.values())

    # 分值：总分为 0.5 分粒度；未给总分时按类型默认分值取整点
    if cons.total_points is not None:
        if abs(cons.total_points * 2 - round(cons.total_points * 2)) > 1e-6:
            raise ReviewError(
                ReviewErrorCode.CONSTRAINT_INVALID,
                f"总分须为 0.5 的整数倍：{cons.total_points}",
                details={"total_points": cons.total_points},
            )
        weights = [counts[t] * DEFAULT_POINTS_BY_TYPE[t] for t in type_order]
        units = _allocate(int(round(cons.total_points * 2)), weights)
        subtotals = {t: u / 2 for t, u in zip(type_order, units)}
        total_points = sum(subtotals.values())
    else:
        subtotals = {t: float(counts[t]) * DEFAULT_POINTS_BY_TYPE[t] for t in type_order}
        total_points = sum(subtotals.values())

    quotas = [
        TypeQuota(
            qtype=t,
            count=counts[t],
            points_each=round(subtotals[t] / counts[t], 2),
            subtotal=subtotals[t],
        )
        for t in type_order
    ]

    # 预计用时：用户指定优先；低于逐题最短用时给出警告（不改用户值）
    computed_minutes = sum(counts[t] * DEFAULT_MINUTES_BY_TYPE[t] for t in type_order)
    warnings: list[str] = list(analysis.warnings)
    if cons.total_minutes is not None:
        estimated_minutes = int(cons.total_minutes)
        if estimated_minutes < total_questions:
            warnings.append(
                f"预计用时 {estimated_minutes} 分钟低于题数（每题至少 1 分钟），可能不切实际"
            )
        elif estimated_minutes * 2 < computed_minutes:
            warnings.append(
                f"预计用时 {estimated_minutes} 分钟明显低于常规估计 {computed_minutes} 分钟，请确认"
            )
    else:
        estimated_minutes = computed_minutes

    # 知识点规划：按权重把题数分配到知识点（每题至少 1 题优先覆盖更多知识点）
    kp_weights = [kp.weight for kp in kps]
    budgets = _allocate(total_questions, kp_weights, min_each=1)
    kp_plan = [
        KpPlan(
            kp_id=kp.kp_id,
            name=kp.name,
            section=kp.section,
            weight=kp.weight,
            question_budget=budget,
            reasons=_kp_reasons(kp),
        )
        for kp, budget in zip(kps, budgets)
        if budget > 0
    ]
    planned_kp_count = len(kp_plan)
    covered_kps = {plan.kp_id for plan in kp_plan}
    skipped = [kp.name for kp in kps if kp.kp_id not in covered_kps]
    if skipped:
        warnings.append(f"题数不足，以下知识点未分配题目：{'、'.join(skipped[:8])}")

    sections = [s for s in analysis.sections if chosen_sections is None or s in chosen_sections]

    # A 检索请求（仅 bank/hybrid；最小检索词，不发整份材料）
    search_requests: list[SearchRequest] = []
    if cons.mode in ("bank", "hybrid"):
        search_requests = _build_search_requests(kp_plan, counts, limits, warnings)

    gaps = [g.message for g in analysis.gaps]

    constraints_echo = cons.to_dict()
    constraints_echo["types"] = dict(sorted(counts.items()))
    constraints_echo["sections"] = list(section_constraint)
    fingerprint = material_fingerprint(snapshot, constraints_echo)
    blueprint_id = "bp_" + hashlib.sha256(
        _canonical_json(
            {
                "course_id": snapshot.course_id,
                "fingerprint": fingerprint,
                "constraints": constraints_echo,
                "engine_version": REVIEW_ENGINE_VERSION,
            }
        ).encode("utf-8")
    ).hexdigest()[:12]

    return PaperBlueprint(
        blueprint_id=blueprint_id,
        course_id=snapshot.course_id,
        title=cons.title or f"{snapshot.course_title}复习卷",
        mode=cons.mode,
        quotas=quotas,
        total_questions=total_questions,
        total_points=round(total_points, 2),
        estimated_minutes=estimated_minutes,
        difficulty_mix=difficulty_mix,
        kp_plan=kp_plan,
        sections=sections,
        gaps=gaps,
        warnings=warnings,
        search_requests=search_requests,
        constraints=constraints_echo,
        fingerprint=fingerprint,
        engine_version=REVIEW_ENGINE_VERSION,
    )


def _build_search_requests(
    kp_plan: list[KpPlan],
    counts: dict[str, int],
    limits: ReviewLimits,
    warnings: list[str],
) -> list[SearchRequest]:
    """每个知识点按其题数预算 × 题型配额生成最小检索请求；超出上限截断并警告。"""
    type_order = [t for t in QUESTION_TYPES if counts.get(t)]
    requests: list[SearchRequest] = []
    for plan in kp_plan:
        weights = [counts[t] for t in type_order]
        per_type = _allocate(plan.question_budget, weights)
        for qtype, count in zip(type_order, per_type):
            if count <= 0:
                continue
            query = plan.name
            requests.append(
                SearchRequest(
                    request_id=f"sq_{len(requests) + 1:03d}",
                    query=query,
                    count=count,
                    qtype=qtype,
                    kp_id=plan.kp_id,
                    kp_name=plan.name,
                    difficulty=None,
                    notes=f"章节：{plan.section}",
                )
            )
    if len(requests) > limits.max_search_requests:
        dropped = len(requests) - limits.max_search_requests
        requests = requests[: limits.max_search_requests]
        warnings.append(f"检索请求超出上限，已截断 {dropped} 条（可提高上限或减少题数）")
    return requests
