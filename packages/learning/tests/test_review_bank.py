"""网络题库选题测试：匹配/去重/配额、缺答案与缺解析的如实处理、缺题原因。"""

from __future__ import annotations

from review_fixtures import (
    SECTION31,
    SECTION32,
    bank_candidates,
    sample_constraints,
    sample_snapshot,
)

from learning.review import (
    BankCandidate,
    BlueprintConstraints,
    build_blueprint,
    select_bank_questions,
    validate_bank_questions,
)


def _bank_blueprint(**overrides):
    snap = sample_snapshot()
    return snap, build_blueprint(snap, sample_constraints("bank", **overrides))


def test_fixture_selection_matches_quota_and_reasons():
    snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, bank_candidates())
    selected = [(s.candidate.candidate_id, s.candidate.qtype, s.answer_status) for s in sel.selected]
    assert selected == [
        ("bc_1", "choice", "complete"),
        ("bc_9", "fill_blank", "complete"),
        ("bc_3", "fill_blank", "explanation_missing"),
        ("bc_5", "choice", "complete"),
        ("bc_8", "choice", "explanation_missing"),
    ]
    rejected = [(r.candidate_id, r.reason) for r in sel.rejected]
    assert rejected == [
        ("bc_6", "UNSUPPORTED_TYPE"),
        ("bc_7", "NO_MATCH"),
        ("bc_2", "DUPLICATE"),
        ("bc_4", "QUOTA_FULLED"),
    ]


def test_deficits_have_readable_reasons():
    _snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, bank_candidates())
    deficits = {d.qtype: d for d in sel.deficits}
    assert set(deficits) == {"choice", "true_false", "calculation", "short_answer"}
    assert deficits["true_false"].reason == "no_candidates"
    assert deficits["true_false"].needed == 2 and deficits["true_false"].available_complete == 0
    assert deficits["choice"].reason == "insufficient"
    assert deficits["choice"].needed == 4 and deficits["choice"].available_complete == 3
    assert deficits["calculation"].reason == "insufficient"  # bc_4 缺答案，未选入
    assert deficits["short_answer"].reason == "no_candidates"
    assert all(d.message for d in sel.deficits)


def test_answer_missing_candidate_never_selected():
    _snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, bank_candidates())
    assert all(s.candidate.answer is not None for s in sel.selected)
    assert "bc_4" not in {s.candidate.candidate_id for s in sel.selected}


def test_low_confidence_and_usage_note_warn():
    _snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, bank_candidates())
    assert any("bc_8" in w and "置信度" in w for w in sel.warnings)
    assert any("bc_9" in w and "使用限制" in w for w in sel.warnings)
    assert any("true_false" in w for w in sel.warnings)


def test_candidate_with_matched_kp_hints_multi():
    _snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, bank_candidates())
    bc5 = next(s for s in sel.selected if s.candidate.candidate_id == "bc_5")
    assert len(bc5.matched_kp_ids) == 2  # 循环队列 + 3.2 队列
    assert bc5.match_score == 3.0


def test_stem_containing_kp_name_matches_without_hints():
    snap = sample_snapshot()
    bp = build_blueprint(snap, BlueprintConstraints.from_dict({"mode": "bank", "types": {"choice": 4}}))
    assert any(p.name == "循环队列" for p in bp.kp_plan)
    sel = select_bank_questions(
        bp,
        [
            {
                "candidate_id": "c_no_hint",
                "stem": "循环队列的取模运算起到什么作用？",
                "qtype": "choice",
                "options": ["复用空间", "加快索引", "减少冲突", "排序"],
                "answer": "A",
                "explanation": "取模使下标回绕。",
            }
        ],
    )
    assert [s.candidate.candidate_id for s in sel.selected] == ["c_no_hint"]
    assert sel.selected[0].match_score == 2.0


def test_no_candidates_all_deficits():
    _snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, [])
    assert sel.selected == []
    assert {d.reason for d in sel.deficits} == {"no_candidates"}
    assert sum(1 for _ in sel.deficits) == 5


def test_type_without_quota_rejected():
    snap = sample_snapshot()
    bp = build_blueprint(snap, BlueprintConstraints.from_dict({"mode": "bank", "types": {"choice": 4}}))
    sel = select_bank_questions(bp, bank_candidates())
    reasons = {r.candidate_id: r.reason for r in sel.rejected}
    assert reasons.get("bc_3") == "NO_QUOTA"
    assert reasons.get("bc_9") == "NO_QUOTA"
    assert {d.qtype for d in sel.deficits} == {"choice"}


def test_invalid_candidates_rejected():
    _snap, bp = _bank_blueprint()
    sel = select_bank_questions(
        bp,
        [
            {"stem": "无 ID 的题", "qtype": "choice"},
            {"candidate_id": "dup", "stem": "第一条", "qtype": "choice", "kp_hints": [SECTION32]},
            {"candidate_id": "dup", "stem": "重复 ID", "qtype": "choice", "kp_hints": [SECTION32]},
            {"candidate_id": "empty", "stem": "  ", "qtype": "choice", "kp_hints": [SECTION32]},
        ],
    )
    reasons = [(r.candidate_id, r.reason) for r in sel.rejected]
    assert ("(无 ID)", "INVALID") in reasons
    assert ("dup", "INVALID") in reasons
    assert ("empty", "INVALID") in reasons


def test_bank_candidate_from_dict_normalizes_options_and_type():
    candidate = BankCandidate.from_dict(
        {
            "candidate_id": "c1",
            "stem": "s",
            "qtype": "选择题",
            "options": ["甲", "乙"],
            "difficulty": "简单",
        }
    )
    assert candidate.qtype == "choice"
    assert [o["key"] for o in candidate.options] == ["A", "B"]
    assert candidate.options[0]["text"] == "甲"
    assert candidate.difficulty == "easy"


def test_validate_bank_questions_reports_deficits_without_repair():
    snap, bp = _bank_blueprint()
    sel = select_bank_questions(bp, bank_candidates())
    outcome = validate_bank_questions(sel, snap, bp)
    assert outcome.candidate is not None
    assert outcome.report.repair_needed is False  # 无模型可修复：交由用户决策
    codes = {i.code for i in outcome.report.issues}
    assert "DEFICIT_REMAINING" in codes
    assert "QUOTA_MISMATCH" in codes  # 缺题即配额不满足，如实报错
    assert "BANK_INCOMPLETE" in codes  # 缺解析的网络题如实标注
    assert outcome.report.counts_by_type["choice"] == 3
    assert outcome.report.counts_by_type["fill_blank"] == 2
    assert outcome.report.total_questions == 5

    questions = outcome.candidate.questions
    assert all(q.origin == "bank" for q in questions)
    assert {q.bank_candidate_id for q in questions} == {"bc_1", "bc_9", "bc_3", "bc_5", "bc_8"}
    bc1 = next(q for q in questions if q.bank_candidate_id == "bc_1")
    assert bc1.source_refs[0].kind == "bank"
    assert bc1.source_refs[0].url == "https://example.com/q/1"


def test_a_side_candidate_shape_compatible():
    """A（connectors.questionbanks）平铺候选形状可直接进入选题：type/analysis/平铺 source 等。"""
    candidate = BankCandidate.from_dict(
        {
            "candidate_id": "qb_" + "a" * 24,
            "stem": "以下关于括号匹配的说法，正确的是？",
            "type": "choice",
            "options": [
                {"label": "A", "text": "空串是合法的括号序列"},
                {"label": "B", "text": "单个左括号是合法序列"},
                {"label": "C", "text": "嵌套必须成对出现"},
                {"label": "D", "text": "括号匹配不需要考虑栈"},
            ],
            "answer": "A",
            "analysis": "空串不包含任何括号，按定义合法。",
            "knowledge_point": SECTION31,
            "match_keywords": ["括号匹配"],
            "difficulty": "easy",
            "source_url": "https://example.com/q/1",
            "source_site": "example题库",
            "source_locator": {"page_title": "括号匹配练习", "kindex": 2},
            "fetched_at": "2026-10-11T09:00:00+08:00",
            "origin": "online",
            "license_note": "仅供个人学习",
            "extract_confidence": 0.9,
        }
    )
    assert candidate.qtype == "choice"
    assert [o["key"] for o in candidate.options] == ["A", "B", "C", "D"]
    assert candidate.explanation == "空串不包含任何括号，按定义合法。"
    assert candidate.kp_hints == [SECTION31, "括号匹配"]
    assert candidate.source is not None
    assert candidate.source.url == "https://example.com/q/1"
    assert candidate.source.site == "example题库"
    assert candidate.source.title is None
    assert candidate.source.locator == "括号匹配练习·2"
    assert candidate.source.fetched_at == "2026-10-11T09:00:00+08:00"
    assert candidate.extraction_confidence == 0.9
    assert candidate.usage_note == "仅供个人学习"

    judgment = BankCandidate.from_dict({"candidate_id": "c2", "stem": "s", "type": "judgment"})
    assert judgment.qtype == "true_false"
    other = BankCandidate.from_dict({"candidate_id": "c3", "stem": "s", "type": "other"})
    assert other.qtype == "other"  # 诚实逃生阀：选题层按 UNSUPPORTED_TYPE 拒绝
