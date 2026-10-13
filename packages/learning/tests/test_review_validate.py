"""输出解析与校验测试：解析容错/截断、题型与选项、答案归一化、假引用、重复题、
配额与总分、章节覆盖、公式保真、混合合并（去重/补题）。"""

from __future__ import annotations

import json
from collections import Counter

from review_fixtures import (
    KP_BOUNDARY,
    KP_LOOP_QUEUE,
    ai_valid_output,
    ai_valid_output_fenced,
    ai_valid_payload,
    bank_candidates,
    sample_constraints,
    sample_snapshot,
)

from learning.review import (
    GENERATE_PROMPT_VERSION,
    REVIEW_ENGINE_VERSION,
    ValidationCode as VC,
    build_blueprint,
    kp_id_for,
    merge_ai_with_bank,
    parse_and_validate_output,
    select_bank_questions,
)


def _base():
    snap = sample_snapshot()
    bp = build_blueprint(snap, sample_constraints("ai"))
    return snap, bp


def _run(payload, snap=None, bp=None, selected=None):
    if snap is None or bp is None:
        snap, bp = _base()
    if payload is None or isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload, ensure_ascii=False)
    return parse_and_validate_output(raw, snap, bp, selected=selected)


def _issues(report, code):
    return [i for i in report.issues if i.code == code]


def test_clean_payload_status_ok():
    payload = ai_valid_payload()
    payload["questions"][9]["kp_ids"] = [KP_BOUNDARY]
    payload["questions"][10]["kp_ids"] = [KP_LOOP_QUEUE, kp_id_for("实验报告"), kp_id_for("队满判别")]
    outcome = _run(payload)
    report = outcome.report
    assert report.status == "ok"
    assert report.issues == []
    assert report.repair_needed is False
    assert report.counts_by_type == {
        "choice": 4, "true_false": 2, "fill_blank": 2, "calculation": 2, "short_answer": 1,
    }
    assert report.total_questions == 11
    assert report.total_points == 100.0
    assert report.duplicates == []
    assert report.coverage["planned"] == 8
    assert report.coverage["covered"] == 8
    assert all(report.coverage["sections"].values())
    assert report.academic_correctness == "not_verified"
    assert len(report.checks_limitations) == 3
    assert report.prompt_version == GENERATE_PROMPT_VERSION
    assert report.truncated is False
    candidate = outcome.candidate
    assert candidate is not None
    assert candidate.candidate_id.startswith("rpc_")
    assert candidate.fingerprint == outcome.candidate.blueprint.fingerprint
    assert candidate.engine_version == REVIEW_ENGINE_VERSION
    assert candidate.partial is False
    assert str(candidate.questions[0].question_id) == "rq_001"
    assert str(candidate.questions[-1].question_id) == "rq_011"
    assert [q.points for q in candidate.questions] == [
        7.5, 7.5, 7.5, 7.0, 4.0, 3.5, 5.5, 5.5, 18.5, 18.5, 15.0,
    ]


def test_standard_payload_ok_with_warnings():
    outcome = _run(ai_valid_output())
    report = outcome.report
    assert report.status == "ok_with_warnings"
    assert Counter(i.code for i in report.issues) == Counter(
        {VC.KP_UNKNOWN: 1, VC.COVERAGE_MISSING: 1}
    )
    assert report.repair_needed is False  # 仅警告，无需修复
    assert _issues(report, VC.KP_UNKNOWN)[0].question_index == 9
    assert outcome.candidate.partial is False


def test_fenced_and_prose_wrapped_output_parse():
    for raw in (
        ai_valid_output_fenced(),
        "运行结果如下：\n" + ai_valid_output() + "\n（完）",
    ):
        outcome = _run(raw)
        assert outcome.candidate is not None
        parse_codes = {VC.OUTPUT_EMPTY, VC.OUTPUT_NOT_JSON, VC.OUTPUT_TRUNCATED, VC.SCHEMA_INVALID}
        assert not [i for i in outcome.report.issues if i.code in parse_codes]
        assert outcome.report.truncated is False


def test_parse_empty_and_missing_output():
    for raw in ("", "   ", None):
        outcome = _run(raw)
        assert outcome.candidate is None
        assert outcome.report.status == "failed"
        assert outcome.report.repair_needed is True
        assert outcome.report.issues[0].code == VC.OUTPUT_EMPTY


def test_parse_not_json_vs_truncated():
    outcome = _run("抱歉，我无法完成这次生成。")
    assert outcome.report.issues[0].code == VC.OUTPUT_NOT_JSON
    assert outcome.report.truncated is False

    outcome = _run('{"title": "半截", "questions": [{"qtype": "choice", "stem": "未完成')
    assert outcome.report.issues[0].code == VC.OUTPUT_TRUNCATED
    assert outcome.report.truncated is True
    assert outcome.report.repair_needed is True


def test_parse_top_level_not_object():
    outcome = _run("[1, 2, 3]")
    assert outcome.candidate is None
    assert _issues(outcome.report, VC.SCHEMA_INVALID)[0].message.startswith("模型输出顶层不是 JSON 对象")


def test_parse_questions_missing_or_empty():
    for payload in (
        {"title": "x"},
        {"title": "x", "questions": []},
        {"title": "x", "questions": "oops"},
    ):
        outcome = _run(payload)
        assert outcome.candidate is None
        assert outcome.report.status == "failed"
        issues = _issues(outcome.report, VC.SCHEMA_INVALID)
        assert issues and "缺少 questions 数组或为空" in issues[-1].message


def test_type_alias_normalized_end_to_end():
    payload = ai_valid_payload()
    payload["questions"][0]["qtype"] = "选择题"
    payload["questions"][4]["qtype"] = "判断"
    outcome = _run(payload)
    assert not _issues(outcome.report, VC.TYPE_UNSUPPORTED)
    assert outcome.report.counts_by_type["choice"] == 4
    assert outcome.report.counts_by_type["true_false"] == 2
    assert outcome.candidate.questions[0].qtype == "choice"


def test_type_unsupported_drops_question_and_breaks_quota():
    payload = ai_valid_payload()
    payload["questions"][0]["qtype"] = "编程题"
    outcome = _run(payload)
    report = outcome.report
    assert report.status == "errors"
    issue = _issues(report, VC.TYPE_UNSUPPORTED)[0]
    assert issue.question_index == 0
    assert report.counts_by_type["choice"] == 3
    messages = [i.message for i in _issues(report, VC.QUOTA_MISMATCH)]
    assert any("选择题需要 4 题，实际 3 题" in m for m in messages)
    assert report.repair_needed is True


def test_choice_options_missing_and_answer_not_in_options():
    payload = ai_valid_payload()
    payload["questions"][0]["options"] = []
    outcome = _run(payload)
    assert _issues(outcome.report, VC.OPTIONS_MISSING)[0].question_index == 0
    assert _issues(outcome.report, VC.ANSWER_NOT_IN_OPTIONS)[0].question_index == 0


def test_choice_answer_lowercase_normalized():
    payload = ai_valid_payload()
    payload["questions"][0]["answer"] = "b"
    outcome = _run(payload)
    assert outcome.candidate.questions[0].answer == "B"
    assert not _issues(outcome.report, VC.ANSWER_NOT_IN_OPTIONS)


def test_options_out_of_order_keys_preserved_and_answer_mapped():
    payload = ai_valid_payload()
    payload["questions"][0]["options"] = [
        {"key": "C", "text": "合法，栈为空即为合法"},
        {"key": "A", "text": "合法，只需左右括号数量相等"},
        {"key": "D", "text": "无法判断"},
        {"key": "B", "text": "不合法，左括号多余"},
    ]
    outcome = _run(payload)
    question = outcome.candidate.questions[0]
    assert [o["key"] for o in question.options] == ["C", "A", "D", "B"]  # 顺序原样保留
    assert question.answer == "B"  # 答案按 key 对应，与排列顺序无关
    assert not _issues(outcome.report, VC.ANSWER_NOT_IN_OPTIONS)


def test_option_count_warning_and_key_duplicate_error():
    payload = ai_valid_payload()
    payload["questions"][0]["options"] = [{"key": chr(65 + i), "text": f"选项{i}"} for i in range(5)]
    outcome = _run(payload)
    assert _issues(outcome.report, VC.OPTION_COUNT)[0].question_index == 0

    payload = ai_valid_payload()
    payload["questions"][0]["options"] = [
        {"key": "A", "text": "甲"},
        {"key": "A", "text": "乙"},
        {"key": "B", "text": "丙"},
        {"key": "C", "text": "丁"},
    ]
    outcome = _run(payload)
    assert outcome.report.status == "errors"
    assert "A" in _issues(outcome.report, VC.OPTION_DUPLICATE)[0].message


def test_non_choice_options_dropped_with_warning():
    payload = ai_valid_payload()
    payload["questions"][4]["options"] = [{"key": "A", "text": "对"}, {"key": "B", "text": "错"}]
    outcome = _run(payload)
    warnings = _issues(outcome.report, VC.ANSWER_FORMAT)
    assert any("忽略多余选项" in w.message and w.question_index == 4 for w in warnings)
    tf = next(q for q in outcome.candidate.questions if q.qtype == "true_false" and "push" in q.stem)
    assert tf.options == []


def test_true_false_answer_variants():
    payload = ai_valid_payload()
    payload["questions"][4]["answer"] = "√"
    outcome = _run(payload)
    tf = next(q for q in outcome.candidate.questions if "push" in q.stem)
    assert tf.answer is True

    payload = ai_valid_payload()
    payload["questions"][5]["answer"] = "也许"
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.ANSWER_FORMAT)[0]
    assert issue.severity == "error" and issue.question_index == 5


def test_fill_blank_count_mismatch_and_missing_marker():
    payload = ai_valid_payload()
    payload["questions"][6]["answer"] = ["甲", "乙"]
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.BLANK_COUNT_MISMATCH)[0]
    assert issue.question_index == 6

    payload = ai_valid_payload()
    payload["questions"][6]["stem"] = payload["questions"][6]["stem"].replace("____", "什么")
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.ANSWER_FORMAT)[0]
    assert "未检测到空位标记" in issue.message


def test_calculation_answer_missing():
    payload = ai_valid_payload()
    payload["questions"][8]["answer"] = ""
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.ANSWER_MISSING)[0]
    assert issue.question_index == 8 and "缺少参考答案" in issue.message


def test_explanation_and_scoring_missing_are_errors_for_ai():
    payload = ai_valid_payload()
    payload["questions"][0]["explanation"] = ""
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.EXPLANATION_MISSING)[0]
    assert issue.severity == "error" and issue.question_index == 0

    payload = ai_valid_payload()
    payload["questions"][8]["scoring"] = []
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.SCORING_MISSING)[0]
    assert issue.severity == "error" and issue.question_index == 8


def test_fake_chunk_id_rejected_with_hint():
    payload = ai_valid_payload()
    payload["questions"][0]["source_refs"] = [{"chunk_id": "ch3_999"}]
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.SOURCE_REF_FAKE)[0]
    assert "ch3_999" in issue.message
    assert "ch3_1" in issue.hint  # 提示可用 id
    assert _issues(outcome.report, VC.SOURCE_REF_MISSING)


def test_fake_notes_line_rejected():
    payload = ai_valid_payload()
    payload["questions"][7]["source_refs"] = [{"notes_line": 99}]
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.SOURCE_REF_FAKE)[0]
    assert "不存在的备注行" in issue.message
    assert "1..4" in issue.hint


def test_unselected_bank_ref_rejected_even_if_exists():
    payload = ai_valid_payload()
    payload["questions"][0]["source_refs"] = [{"bank_candidate_id": "bc_1"}]
    outcome = _run(payload)  # selected=None：没有任何候选题被授权
    issue = _issues(outcome.report, VC.SOURCE_REF_FAKE)[0]
    assert "未选入的网络候选" in issue.message


def test_selected_bank_ref_accepted_with_label():
    snap, _bp = _base()
    bp = build_blueprint(snap, sample_constraints("hybrid"))
    selection = select_bank_questions(bp, bank_candidates())
    outcome = _run(ai_valid_payload(extra_ref_bank_id="bc_1"), snap=snap, bp=bp, selected=selection)
    assert not _issues(outcome.report, VC.SOURCE_REF_FAKE)
    with_bank = next(q for q in outcome.candidate.questions if any(r.kind == "bank" for r in q.source_refs))
    assert with_bank.source_refs[-1].label == "括号匹配练习"


def test_missing_source_refs_error():
    payload = ai_valid_payload()
    payload["questions"][0]["source_refs"] = []
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.SOURCE_REF_MISSING)[0]
    assert issue.question_index == 0


def test_duplicate_stem_detected_with_ids():
    payload = ai_valid_payload()
    payload["questions"][1]["stem"] = payload["questions"][0]["stem"]
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.DUPLICATE_QUESTION)[0]
    assert issue.severity == "error" and issue.question_index == 1
    assert outcome.report.duplicates == [["rq_001", "rq_002"]]
    assert outcome.candidate.partial is True


def test_near_duplicate_stem_is_warning():
    payload = ai_valid_payload()
    payload["questions"][1]["stem"] = payload["questions"][0]["stem"] + "补充"
    outcome = _run(payload)
    issue = _issues(outcome.report, VC.NEAR_DUPLICATE)[0]
    assert issue.severity == "warning" and issue.question_index == 1
    assert not _issues(outcome.report, VC.DUPLICATE_QUESTION)
    assert outcome.report.duplicates == []


def test_missing_question_breaks_quota_and_total():
    payload = ai_valid_payload()
    payload["questions"].pop()  # 去掉 short_answer
    outcome = _run(payload)
    report = outcome.report
    messages = [i.message for i in _issues(report, VC.QUOTA_MISMATCH)]
    assert any("简答/证明题需要 1 题，实际 0 题" in m for m in messages)
    assert report.counts_by_type["short_answer"] == 0
    assert report.total_points == 85.0
    assert _issues(report, VC.TOTAL_POINTS_MISMATCH)


def test_section_coverage_error_when_section_empty():
    payload = ai_valid_payload()
    for question in payload["questions"]:
        question["kp_ids"] = [KP_BOUNDARY]
    outcome = _run(payload)
    report = outcome.report
    assert report.status == "errors"
    section_errors = [
        i for i in _issues(report, VC.COVERAGE_MISSING) if i.severity == "error"
    ]
    assert len(section_errors) == 2
    assert any("第4章 树与二叉树" in i.message for i in section_errors)
    assert any("作业与附件" in i.message for i in section_errors)
    assert report.coverage["sections"]["第3章 栈与队列"] is True
    assert report.coverage["sections"]["第4章 树与二叉树"] is False


def test_math_notation_preserved_in_answers():
    payload = ai_valid_payload()
    payload["questions"][8]["answer"] = "中序：1,3,6,8,10。平均 O(log n)，上界记为 \\frac{n}{2}。"
    payload["questions"][8]["explanation"] = "推导见 \\frac{n}{2} 与 log n 的同阶比较。"
    outcome = _run(payload)
    calc = next(q for q in outcome.candidate.questions if q.qtype == "calculation" and "先序" in q.stem)
    assert "\\frac{n}{2}" in calc.answer
    assert "\\frac{n}{2}" in calc.explanation
    assert "\\frac{n}{2}" in json.dumps(outcome.candidate.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 混合合并：网络题 + AI 补题
# ---------------------------------------------------------------------------


def _ai_complement_payload():
    base = ai_valid_payload()
    keep = (0, 4, 5, 8, 9, 10)  # choice×1, true_false×2, calculation×2, short_answer×1
    return {"title": base["title"], "questions": [base["questions"][i] for i in keep]}


def _hybrid_base():
    snap, _bp = _base()
    bp = build_blueprint(snap, sample_constraints("hybrid"))
    selection = select_bank_questions(bp, bank_candidates())
    return snap, bp, selection


def test_merge_hybrid_complement_fills_deficits():
    snap, bp, selection = _hybrid_base()
    ai_outcome = _run(_ai_complement_payload(), snap=snap, bp=bp)
    assert ai_outcome.report.status == "errors"  # AI 单独看配额远不足
    merged = merge_ai_with_bank(selection, ai_outcome, snap, bp)
    report = merged.report
    assert report.status == "ok_with_warnings"
    assert report.repair_needed is False
    codes = Counter(i.code for i in report.issues)
    assert codes == Counter({VC.ANSWER_FORMAT: 2, VC.EXPLANATION_MISSING: 2, VC.COVERAGE_MISSING: 1})
    assert report.counts_by_type == {
        "choice": 4, "true_false": 2, "fill_blank": 2, "calculation": 2, "short_answer": 1,
    }
    assert report.total_points == 100.0
    assert report.coverage["planned"] == 8 and report.coverage["covered"] == 6
    candidate = merged.candidate
    assert candidate.partial is True  # 含“网络题缺解析”的 partial_reasons
    assert candidate.questions[0].origin == "bank"
    assert candidate.questions[-1].origin == "ai"
    assert len(candidate.questions) == 11


def test_merge_dedup_ai_vs_bank_reports_honestly():
    snap, bp, selection = _hybrid_base()
    payload = _ai_complement_payload()
    payload["questions"][0]["stem"] = "以下关于括号匹配的说法，正确的是？"  # 与 bc_1 同题干
    ai_outcome = _run(payload, snap=snap, bp=bp)
    merged = merge_ai_with_bank(selection, ai_outcome, snap, bp)
    report = merged.report
    assert report.status == "errors"
    dup_warnings = _issues(report, VC.DUPLICATE_QUESTION)
    assert dup_warnings and "已去重" in dup_warnings[0].message
    assert report.counts_by_type["choice"] == 3
    assert report.total_points == 93.0
    assert _issues(report, VC.QUOTA_MISMATCH)
    assert _issues(report, VC.TOTAL_POINTS_MISMATCH)
    deficit_warnings = _issues(report, VC.DEFICIT_REMAINING)
    assert any("仍缺 choice 1 题" in w.message for w in deficit_warnings)
    assert report.repair_needed is True  # 混合模式可经修复请求补齐
