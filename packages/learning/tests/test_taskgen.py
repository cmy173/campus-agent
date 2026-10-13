"""任务生成测试：稳定 ID、重复合并、版本合并、用户修正保留、提示解析。"""

from learning.models import LearningTask
from learning.samples import load_sample, parse_inputs
from learning.taskgen import TaskGenOptions, TaskHint, build_tasks


def baseline_inputs():
    return parse_inputs(load_sample("baseline"))


def build(inp):
    return build_tasks(inp["resources"], inp["assignments"], inp["recordings"], inp["task_options"])


def test_stable_ids_and_expected_count():
    inp = baseline_inputs()
    first = build(inp)
    second = build(inp)
    assert first.to_dict() == second.to_dict()
    # 3 份去重后资料 + 5 项作业 + 2 条回放 + 1 个用户任务 = 11
    assert len(first.tasks) == 11
    assert len({t.id for t in first.tasks}) == 11


def test_duplicate_content_and_version_merge():
    inp = baseline_inputs()
    result = build(inp)
    codes = [w.code for w in result.warnings]
    assert "DUPLICATE_CONTENT" in codes
    assert "TASK_VERSION_UPDATED" in codes
    tree = next(t for t in result.tasks if t.title == "第4章 树与二叉树（课件）")
    assert tree.estimated_minutes == 138  # v2：46 页 × 3 分钟/页
    assert len(tree.source_refs) == 3  # v1、v2、打印版来源全部保留
    print_tasks = [t for t in result.tasks if "打印版" in t.title]
    assert print_tasks == []


def test_due_facts_not_guessed():
    inp = baseline_inputs()
    result = build(inp)
    overdue = next(t for t in result.tasks if t.title == "作业3：线性回归实现")
    assert overdue.due_at == "2026-10-08T23:59:00+08:00"
    assert overdue.due_precision == "datetime"
    no_due = next(t for t in result.tasks if t.title == "课程设计提案")
    assert no_due.due_at is None
    assert no_due.due_precision == "unknown"


def test_hints_add_prerequisites():
    inp = baseline_inputs()
    result = build(inp)
    tree = next(t for t in result.tasks if t.title == "第4章 树与二叉树（课件）")
    hw4 = next(t for t in result.tasks if "作业4" in t.title)
    assert hw4.prerequisite_ids == [tree.id]
    ml = next(t for t in result.tasks if "机器学习概述" in t.title)
    quiz = next(t for t in result.tasks if "概率基础测验" in t.title)
    assert quiz.prerequisite_ids == [ml.id]


def test_recording_without_ready_transcript_uses_duration():
    inp = baseline_inputs()
    result = build(inp)
    rec = next(t for t in result.tasks if "树的遍历（回放）" in t.title)
    assert rec.type == "watching"
    assert rec.estimated_minutes == 90  # 5400 秒
    # baseline 该回放为 pending：仅按时长估算，完成标准注明字幕待更新
    assert rec.completion_criteria and "待更新" in rec.completion_criteria


def test_estimate_provenance_user_vs_inferred():
    inp = baseline_inputs()
    result = build(inp)
    hw4 = next(t for t in result.tasks if "作业4" in t.title)
    assert hw4.estimated_minutes == 120 and hw4.estimate_provenance == "user"
    hw5 = next(t for t in result.tasks if "作业5" in t.title)
    assert hw5.estimated_minutes == 90 and hw5.estimate_provenance == "inferred"


def test_user_overrides_preserved_on_rebuild():
    inp = baseline_inputs()
    first = build(inp)
    tree = next(t for t in first.tasks if t.title == "第4章 树与二叉树（课件）")
    edited = tree.to_dict()
    edited["estimated_minutes"] = 200
    edited["estimate_provenance"] = "user"
    edited["status"] = "completed"

    options = inp["task_options"]
    options.existing_tasks = [LearningTask.from_dict(edited)]
    second = build(inp)
    tree2 = next(t for t in second.tasks if t.title == "第4章 树与二叉树（课件）")
    assert tree2.estimated_minutes == 200
    assert tree2.status == "completed"
    assert tree2.estimate_provenance == "user"


def test_unresolved_hint_warns():
    options = TaskGenOptions(
        hints=[TaskHint(match={"course_id": "crs_x", "type": "reading", "title": "不存在"}, estimate_minutes=10)]
    )
    result = build_tasks([], [], [], options)
    assert result.tasks == []
    assert [w.code for w in result.warnings] == ["HINT_UNRESOLVED"]


def test_duplicate_rerun_is_idempotent():
    """重复“解析/生成”不制造重复任务（验收 1 的模块侧）。"""
    inp = baseline_inputs()
    first = build(inp)
    options = inp["task_options"]
    options.existing_tasks = list(first.tasks)
    second = build(inp)
    assert [t.id for t in second.tasks] == [t.id for t in first.tasks]
    assert len(second.tasks) == len(first.tasks)
