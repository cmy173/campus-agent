"""试卷蓝图测试：配额与总分精确一致、约束校验报错原因、检索请求、指纹敏感性。"""

from __future__ import annotations

import pytest
from review_fixtures import (
    CHAPTER3,
    COURSE_TITLE,
    NOTES,
    courseware_materials,
    sample_constraints,
    sample_snapshot,
)

from learning.review import (
    BlueprintConstraints,
    MaterialInput,
    ReviewError,
    ReviewErrorCode,
    analyze_materials,
    build_blueprint,
    build_snapshot,
    material_fingerprint,
)


def _blueprint(mode="ai", **overrides):
    snap = sample_snapshot()
    return snap, build_blueprint(snap, sample_constraints(mode, **overrides))


def test_fixture_quota_and_points_exact():
    _snap, bp = _blueprint()
    assert bp.mode == "ai"
    assert bp.total_questions == 11
    assert bp.total_points == 100.0
    assert bp.estimated_minutes == 40
    subtotals = {q.qtype: q.subtotal for q in bp.quotas}
    assert subtotals == {
        "choice": 29.5,
        "true_false": 7.5,
        "fill_blank": 11.0,
        "calculation": 37.0,
        "short_answer": 15.0,
    }
    assert sum(subtotals.values()) == 100.0
    counts = {q.qtype: q.count for q in bp.quotas}
    assert counts == {"choice": 4, "true_false": 2, "fill_blank": 2, "calculation": 2, "short_answer": 1}
    assert bp.quota_for("choice").points_each == 7.38  # 29.5 / 4
    assert bp.quota_for("unknown") is None
    assert bp.title == f"{COURSE_TITLE}复习卷"
    assert bp.difficulty_mix == {"easy": 0.3, "medium": 0.5, "hard": 0.2}


def test_kp_plan_budgets_sum_and_weight_order():
    _snap, bp = _blueprint()
    assert sum(p.question_budget for p in bp.kp_plan) == 11
    assert all(p.question_budget >= 1 for p in bp.kp_plan)
    top3 = [p for p in bp.kp_plan if p.weight == 3.5]
    assert len(top3) == 3
    assert all(p.question_budget == 2 for p in top3)
    assert [p.name for p in top3] == ["3.1 括号匹配与边界条件", "3.2 队列", "循环队列"]
    assert bp.sections == [CHAPTER3, "第4章 树与二叉树", "作业与附件"]


def test_search_requests_only_for_bank_and_hybrid():
    snap = sample_snapshot()
    for mode, expected in (("ai", 0), ("bank", 11), ("hybrid", 11)):
        bp = build_blueprint(snap, sample_constraints(mode))
        assert len(bp.search_requests) == expected
    bank_bp = build_blueprint(snap, sample_constraints("bank"))
    plan_ids = {p.kp_id for p in bank_bp.kp_plan}
    assert {r.kp_id for r in bank_bp.search_requests} <= plan_ids
    assert sum(r.count for r in bank_bp.search_requests) == bank_bp.total_questions
    assert [r.request_id for r in bank_bp.search_requests] == [
        f"sq_{i:03d}" for i in range(1, len(bank_bp.search_requests) + 1)
    ]
    assert all(r.query in {p.name for p in bank_bp.kp_plan} for r in bank_bp.search_requests)


def test_search_requests_truncated_by_limit():
    snap = sample_snapshot()
    from learning.review import ReviewLimits

    bp = build_blueprint(snap, sample_constraints("bank"), limits=ReviewLimits(max_search_requests=5))
    assert len(bp.search_requests) == 5
    assert any("检索请求超出上限" in w for w in bp.warnings)


def test_invalid_constraints_raise_with_reason():
    snap = sample_snapshot()
    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(types={"编程题": 1}))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID
    assert "编程题" in err.value.message

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(types={"choice": -1}))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(types={"choice": 0}))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(types={"choice": "abc"}))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(mode="quiz"))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(types={"choice": 1}, total_points=10.3))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID
    assert "0.5" in err.value.message

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, BlueprintConstraints(types={"choice": 1}, difficulty={"easy": 0.5, "hard": 0.3}))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID
    assert "难度比例" in err.value.message


def test_question_limit_enforced():
    snap = sample_snapshot()
    from learning.review import ReviewLimits

    with pytest.raises(ReviewError) as err:
        build_blueprint(
            snap,
            BlueprintConstraints(types={"choice": 41}),
            limits=ReviewLimits(max_questions=40),
        )
    assert err.value.code == ReviewErrorCode.CONSTRAINT_INVALID
    assert err.value.details["total"] == 41


def test_type_alias_normalized():
    snap = sample_snapshot()
    bp = build_blueprint(snap, {"mode": "ai", "types": {"选择题": 2, "判断": 1}})
    assert {q.qtype: q.count for q in bp.quotas} == {"choice": 2, "true_false": 1}
    assert bp.total_questions == 3


def test_sections_filtered_and_unsatisfiable_reason():
    snap = sample_snapshot()
    bp = build_blueprint(snap, sample_constraints("ai", sections=[CHAPTER3]))
    assert bp.sections == [CHAPTER3]
    assert {p.section for p in bp.kp_plan} == {CHAPTER3}
    assert sum(p.question_budget for p in bp.kp_plan) == 11
    assert bp.constraints["sections"] == [CHAPTER3]

    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, sample_constraints("ai", sections=["第9章 不存在的章节"]))
    assert err.value.code == ReviewErrorCode.CONSTRAINT_UNSATISFIABLE
    assert CHAPTER3 in err.value.details["available"]


def test_no_readable_material_raises():
    snap = build_snapshot(
        "c1",
        "课程",
        [MaterialInput(material_id="m1", kind="resource", title="未解析.pdf", parse_status="pending")],
    )
    with pytest.raises(ReviewError) as err:
        build_blueprint(snap, sample_constraints("ai"))
    assert err.value.code == ReviewErrorCode.MATERIAL_UNPARSED


def test_default_counts_and_points_without_constraints():
    snap = sample_snapshot()
    bp = build_blueprint(snap, None)
    assert {q.qtype: q.count for q in bp.quotas} == {
        "choice": 8,
        "true_false": 4,
        "fill_blank": 4,
        "calculation": 2,
        "short_answer": 2,
    }
    assert bp.total_questions == 20
    assert bp.total_points == 88.0  # 8×4 + 4×2 + 4×3 + 2×10 + 2×8


def test_derive_counts_from_total_points_only():
    snap = sample_snapshot()
    bp = build_blueprint(snap, {"mode": "ai", "total_points": 50})
    assert bp.total_points == 50.0
    assert bp.total_questions == sum(q.count for q in bp.quotas)
    assert bp.total_questions > 0


def test_total_minutes_warning_without_changing_value():
    snap = sample_snapshot()
    bp = build_blueprint(snap, sample_constraints("ai", total_minutes=19))
    assert bp.estimated_minutes == 19
    assert any("明显低于常规估计" in w for w in bp.warnings)

    bp2 = build_blueprint(snap, sample_constraints("ai", total_minutes=5))
    assert bp2.estimated_minutes == 5
    assert any("不切实际" in w for w in bp2.warnings)


def test_fingerprint_and_blueprint_deterministic_and_sensitive():
    snap = sample_snapshot()
    cons = sample_constraints("ai")
    bp1 = build_blueprint(snap, cons)
    bp2 = build_blueprint(snap, cons)
    assert bp1.blueprint_id == bp2.blueprint_id
    assert bp1.fingerprint == bp2.fingerprint
    assert bp1.fingerprint != snap.fingerprint  # 约束进入指纹
    assert bp1.fingerprint == material_fingerprint(snap, bp1.constraints)

    notes_changed = sample_snapshot(notes=NOTES + "\n新增一行要求。")
    bp_notes = build_blueprint(notes_changed, cons)
    assert bp_notes.fingerprint != bp1.fingerprint
    assert bp_notes.blueprint_id != bp1.blueprint_id

    bp_other_types = build_blueprint(snap, sample_constraints("ai", total_points=99.5))
    assert bp_other_types.fingerprint != bp1.fingerprint


def test_gaps_surface_into_blueprint_warnings():
    snap = build_snapshot(
        "course_ds",
        COURSE_TITLE,
        [
            *courseware_materials(),
            MaterialInput(
                material_id="att_orphan",
                kind="attachment",
                title="孤立附件.docx",
                parse_status="ready",
                chunks=[{"ordinal": 0, "text": "附件正文"}],
            ),
        ],
        notes=NOTES,
    )
    bp = build_blueprint(snap, sample_constraints("ai"), analysis=analyze_materials(snap))
    assert any("未标注所属作业" in g for g in bp.gaps)
