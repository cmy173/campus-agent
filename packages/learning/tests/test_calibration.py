"""D7 样例校准一致性：baseline 字段形态对齐契约 v0.1 与 xzzd 观测口径。

baseline 数值本体为虚构脱敏数据（A5b 真实登录实测未执行）；本测试固化“形态”与
“篇幅→估算”关系，不背书数值真实性。实测数值复核流程见 docs/D7-样例校准说明.md。
"""

import json
import re
from pathlib import Path

import pytest

from learning.samples import load_sample, parse_inputs
from learning.taskgen import build_tasks

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = PACKAGE_ROOT / "samples" / "baseline.json"
CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schemas"

# 固化枚举来源：contracts/schemas（契约 v0.1）。test_baseline_enums_match_contract_schemas
# 直连契约文件核对，防止本地常量与契约漂移。
KIND_ENUM = ["slides", "handout", "syllabus", "scan", "document", "link", "other"]
PARSE_STATUS_ENUM = ["pending", "processing", "ready", "failed", "skipped"]
DUE_PRECISION_ENUM = ["datetime", "date", "unknown"]
SUBMISSION_STATUS_ENUM = ["unknown", "not_submitted", "submitted", "graded"]
TRANSCRIPT_STATUS_ENUM = ["pending", "processing", "ready", "unavailable", "unknown", "failed"]
PLATFORMS = {"xzzd", "zykt", "manual"}

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OFFSET_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?\+08:00$")


def baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_resource_forms_align_with_contract():
    for resource in baseline()["resources"]:
        rid = resource["id"]
        assert resource["kind"] in KIND_ENUM, rid
        assert resource["parse_status"] in PARSE_STATUS_ENUM, rid
        assert SHA256_RE.match(resource["content_hash"]), rid
        assert isinstance(resource["mime_type"], str) and "/" in resource["mime_type"], rid
        assert isinstance(resource["size_bytes"], int) and resource["size_bytes"] > 0, rid
        assert resource["extent_unit"] in ("page", "slide"), rid
        assert isinstance(resource["extent_value"], int) and resource["extent_value"] > 0, rid
        for ref in resource["source_refs"]:
            assert ref["platform"] in PLATFORMS, rid
            assert OFFSET_DATETIME_RE.match(ref["observed_at"]), rid


def test_duplicate_and_version_hash_forms():
    """打印版与 v1 同哈希（重复文件）、v2 内容不同（同名新版本）——两条告警场景依赖该形态。"""
    by_id = {resource["id"]: resource for resource in baseline()["resources"]}
    assert (
        by_id["res_cs261_tree_print"]["content_hash"]
        == by_id["res_cs261_tree_v1"]["content_hash"]
    )
    assert (
        by_id["res_cs261_tree_v2"]["content_hash"]
        != by_id["res_cs261_tree_v1"]["content_hash"]
    )


def test_assignment_due_and_submission_forms():
    assignments = baseline()["assignments"]
    assert any(assignment["due_at"] for assignment in assignments), "应含带截止的作业"
    assert any(assignment["due_at"] is None for assignment in assignments), "应含无截止的作业"
    for assignment in assignments:
        aid = assignment["id"]
        assert assignment["due_precision"] in DUE_PRECISION_ENUM, aid
        assert assignment["submission_status"] in SUBMISSION_STATUS_ENUM, aid
        if assignment["due_at"] is None:
            assert assignment["due_precision"] == "unknown", aid
        else:
            assert assignment["due_precision"] == "datetime", aid
            assert OFFSET_DATETIME_RE.match(assignment["due_at"]), aid
            # xzzd 实测口径：截止为当日 23:59:00+08:00
            assert assignment["due_at"].endswith("T23:59:00+08:00"), aid
        ref = assignment["source_ref"]
        assert ref["platform"] in PLATFORMS, aid


def test_recording_forms():
    for recording in baseline()["recordings"]:
        rid = recording["id"]
        assert recording["transcript_status"] in TRANSCRIPT_STATUS_ENUM, rid
        assert isinstance(recording["duration_seconds"], int) and recording["duration_seconds"] > 0, rid
        # 回放来自智云课堂
        assert recording["source_ref"]["platform"] == "zykt", rid
        assert OFFSET_DATETIME_RE.match(recording["source_ref"]["observed_at"]), rid


def test_baseline_extent_estimate_relation():
    """篇幅数值与默认估算配置的关系（3 分/页、2 分/单页、回放 1:1 折算分钟）——校准时数值不漂移。"""
    inputs = parse_inputs(load_sample("baseline"))
    result = build_tasks(
        inputs["resources"], inputs["assignments"], inputs["recordings"], inputs["task_options"]
    )
    by_title = {task.title: task for task in result.tasks}
    assert by_title["第4章 树与二叉树（课件）"].estimated_minutes == 46 * 3
    assert by_title["第3章 机器学习概述（讲义）"].estimated_minutes == 30 * 2
    assert by_title["实验指导书：线性回归"].estimated_minutes == 8 * 3
    assert by_title["第4讲 树的遍历（回放）"].estimated_minutes == 5400 // 60
    assert by_title["第3讲 梯度下降（回放）"].estimated_minutes == 2700 // 60


@pytest.mark.skipif(
    not (CONTRACTS_DIR / "resource.schema.json").exists(),
    reason="契约目录不可用（包独立拷贝运行时跳过）",
)
def test_baseline_enums_match_contract_schemas():
    resource_schema = json.loads(
        (CONTRACTS_DIR / "resource.schema.json").read_text(encoding="utf-8")
    )
    common_schema = json.loads((CONTRACTS_DIR / "common.schema.json").read_text(encoding="utf-8"))
    recording_schema = json.loads(
        (CONTRACTS_DIR / "recording.schema.json").read_text(encoding="utf-8")
    )
    assignment_schema = json.loads(
        (CONTRACTS_DIR / "assignment.schema.json").read_text(encoding="utf-8")
    )
    assert KIND_ENUM == resource_schema["properties"]["kind"]["enum"]
    assert PARSE_STATUS_ENUM == resource_schema["properties"]["parse_status"]["enum"]
    assert DUE_PRECISION_ENUM == common_schema["$defs"]["due_precision"]["enum"]
    assert SUBMISSION_STATUS_ENUM == assignment_schema["properties"]["submission_status"]["enum"]
    assert TRANSCRIPT_STATUS_ENUM == recording_schema["properties"]["transcript_status"]["enum"]
    assert SHA256_RE.pattern == common_schema["$defs"]["content_hash"]["oneOf"][0]["pattern"]
