"""知识点提取测试：课件/作业/备注三类证据、备注要求在材料中无依据时如实警告。"""

from __future__ import annotations

from review_fixtures import (
    CHAPTER3,
    CHAPTER4,
    KP_BOUNDARY,
    KP_LOOP_QUEUE,
    KP_QUEUE,
    NOTES,
    SECTION31,
    SECTION32,
    SECTION41,
    assignment_materials,
    courseware_materials,
    sample_materials,
    sample_snapshot,
)

from learning.review import ChunkInput, MaterialInput, analyze_materials, build_snapshot, kp_id_for

EXPECTED_ORDER = [
    SECTION31,
    SECTION32,
    "循环队列",
    SECTION41,
    "实验报告",
    "队满判别",
    CHAPTER3,
    CHAPTER4,
]
EXPECTED_WEIGHTS = [3.5, 3.5, 3.5, 2.5, 2.5, 2.5, 1.0, 1.0]


def test_fixture_knowledge_points_and_weights():
    analysis = analyze_materials(sample_snapshot())
    assert [kp.name for kp in analysis.knowledge_points] == EXPECTED_ORDER
    assert [kp.weight for kp in analysis.knowledge_points] == EXPECTED_WEIGHTS
    assert {kp.kp_id for kp in analysis.knowledge_points} == {kp_id_for(n) for n in EXPECTED_ORDER}


def test_courseware_and_assignment_both_contribute():
    analysis = analyze_materials(sample_snapshot())
    by_name = {kp.name: kp for kp in analysis.knowledge_points}

    boundary = by_name[SECTION31]
    assert boundary.origin == "heading"
    assert boundary.section == CHAPTER3
    roles = {e.role for e in boundary.evidence}
    assert roles == {"courseware", "assignment", "notes"}
    assert boundary.assignment_assessed is True
    assert boundary.note_requested is True

    loop_queue = by_name["循环队列"]
    assert loop_queue.origin == "assignment"  # 仅在作业/附件中出现的词条
    assert loop_queue.section == "作业与附件"
    assert loop_queue.assignment_assessed is True
    assert any(e.role == "assignment" for e in loop_queue.evidence)

    report = by_name["实验报告"]
    assert report.origin == "assignment"
    assert report.courseware_evidence == 0

    # 章节标题知识点：未被作业/备注直接命中，权重为基础值
    assert by_name[CHAPTER3].weight == 1.0
    assert by_name[CHAPTER3].assignment_assessed is False


def test_analysis_sections_and_notes_requirements():
    analysis = analyze_materials(sample_snapshot())
    assert analysis.sections == [CHAPTER3, CHAPTER4, "作业与附件"]

    reqs = analysis.note_requirements
    assert [(r.line, r.kind) for r in reqs] == [
        (1, "content"),
        (2, "content"),
        (3, "format"),
        (4, "content"),
    ]
    assert reqs[0].matched_kp_ids == [KP_BOUNDARY]
    assert set(reqs[1].matched_kp_ids) == {KP_BOUNDARY, KP_QUEUE, KP_LOOP_QUEUE}
    assert reqs[2].matched_kp_ids == []  # 格式要求不强制匹配材料
    assert reqs[3].matched_kp_ids == []


def test_unmatched_requirement_warns_but_format_line_does_not():
    analysis = analyze_materials(sample_snapshot())
    assert len(analysis.warnings) == 1
    warning = analysis.warnings[0]
    assert "第4行" in warning
    assert "红黑树" in warning
    assert all("第3行" not in w for w in analysis.warnings)  # 格式行不产生“无依据”警告
    assert analysis.stats["unmatched_requirements"] == 1
    assert analysis.stats["notes_requirements"] == 4


def test_assignment_removal_weakens_analysis():
    with_asg = analyze_materials(sample_snapshot())
    without_asg = analyze_materials(
        build_snapshot("course_ds", "数据结构与算法", courseware_materials(), notes=NOTES)
    )
    assert any("未选择作业" in w for w in without_asg.warnings)
    with_by = {kp.name: kp for kp in with_asg.knowledge_points}
    without_by = {kp.name: kp for kp in without_asg.knowledge_points}
    assert "循环队列" in with_by and "循环队列" not in without_by  # 作业独有知识点消失
    assert without_by[SECTION31].weight < with_by[SECTION31].weight  # 作业证据带来的权重提升消失
    assert without_by[SECTION31].assignment_assessed is False
    assert "作业与附件" not in without_asg.sections


def test_courseware_removal_drops_heading_kps():
    only_asg = build_snapshot("course_ds", "数据结构与算法", assignment_materials(), notes=NOTES)
    analysis = analyze_materials(only_asg)
    names = [kp.name for kp in analysis.knowledge_points]
    assert any("未选择可读课件" in w for w in analysis.warnings)
    assert SECTION41 not in names and CHAPTER4 not in names
    assert "循环队列" in names  # 作业与附件本身仍可提炼词条


def test_term_knowledge_points_from_repeated_courseware_terms():
    material = MaterialInput(
        material_id="res_term",
        kind="resource",
        title="专题讲义.pdf",
        version="1",
        parse_status="ready",
        chunks=[
            ChunkInput(ordinal=0, text="红黑树旋转是保持平衡的关键。红黑树旋转不改变中序。"),
            ChunkInput(ordinal=1, text="红黑树旋转操作分左旋与右旋。"),
            ChunkInput(ordinal=2, text="对红黑树而言，旋转是局部操作。"),
        ],
    )
    snap = build_snapshot("c1", "课程", [material], notes="请重点考察红黑树的旋转。")
    analysis = analyze_materials(snap)
    by_name = {kp.name: kp for kp in analysis.knowledge_points}
    assert "红黑树" in by_name
    assert by_name["红黑树"].origin == "term"
    assert by_name["红黑树"].courseware_evidence >= 1
    assert by_name["红黑树"].note_requested is True  # 备注“重点考察红黑树的旋转”命中该词条
    assert by_name["红黑树"].assignment_assessed is False
    assert by_name["红黑树"].weight == 2.0  # 基础 1.0 + 备注要求 1.0（无作业证据）


def test_pure_unknown_text_falls_back_to_general_bucket():
    material = MaterialInput(
        material_id="res_x",
        kind="resource",
        title="混编.pdf",
        version="1",
        parse_status="ready",
        chunks=[ChunkInput(ordinal=0, text="xyz")],
    )
    analysis = analyze_materials(build_snapshot("c1", "课程", [material]))
    assert [kp.name for kp in analysis.knowledge_points] == ["综合复习"]
    assert analysis.knowledge_points[0].origin == "fallback"
    assert any("综合复习" in w for w in analysis.warnings)


def test_gaps_are_preserved_into_analysis():
    snap = build_snapshot(
        "c1",
        "课程",
        [
            MaterialInput(
                material_id="m_pending",
                kind="resource",
                title="未解析课件.pdf",
                parse_status="pending",
            ),
            *assignment_materials(),
        ],
        notes=NOTES,
    )
    analysis = analyze_materials(snap)
    codes = {g.code for g in analysis.gaps}
    assert "MATERIAL_UNPARSED" in codes
    assert all(g2.code != "NO_MATERIALS" for g2 in analysis.gaps)
