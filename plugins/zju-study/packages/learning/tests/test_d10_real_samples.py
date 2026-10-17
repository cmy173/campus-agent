"""D10 · 真实脱敏样例（A16）冻结快照校准测试。

样例来源：智能体 A `connectors/samples/real-2026-10-10/`（A5b 只读实测，-30 天平移、
ID 虚构化，见 PROVENANCE.md）；本包内为只读复制快照 `samples/real-2026-10-10/`。

校准结论（详见 docs/D10-真实样例校准报告.md）：
- **无法校准** minutes_per_unit 与类型兜底时长——样例缺少两类校准输入：
  ① 页数/幻灯片数：平台元数据无此字段，extent 恒 null（需 D2 解析真实文件或用户标注）；
  ② 实际学习耗时观测：无任何 FeedbackRecord。不伪造校准结论。
- 本文件把样例中可复现的**实际观察**钉为断言（快照哈希、规模、字段形态、extent
  休眠行为、>64MB 护栏命中集、截止格式与北京时间分布）。样例更新或行为漂移时，
  本文件必须显式更新，不允许静默漂移。

PD-1/PD-2/PD-3 落地（coord v1.8 裁定，2026-10-10）后，本文件中的相关断言同步更新：
护栏默认 30MB→64MB（42.9MB PDF 不再超限）；mp4 映射 reading→watching；
已提交作业 platform_submitted=True（保留生成、不排程）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from learning.models import Assignment, Resource
from learning.parse import ParseLimits
from learning.taskgen import build_tasks_json

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "samples" / "real-2026-10-10"

# 冻结快照内容哈希：样例被改动（无论有意无意）立即报警，须走显式更新流程
SNAPSHOT_SHA256 = {
    "resources.json": "3ba97965663bc851a450251c19b74945aac0b3dbed7d3f51bc1c2e5f36a60950",
    "assignments.json": "e03f5b994db23920b61be8715b5f1e67d2b850767673c559bd3babf3a9d31678",
}

DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
BEIJING = timezone(timedelta(hours=8))


def load_snapshot() -> tuple[list[dict], list[dict]]:
    resources = json.loads((SNAPSHOT_DIR / "resources.json").read_text(encoding="utf-8"))["resources"]
    assignments = json.loads((SNAPSHOT_DIR / "assignments.json").read_text(encoding="utf-8"))["assignments"]
    return resources, assignments


def real_payload() -> dict:
    resources, assignments = load_snapshot()
    return {"resources": resources, "assignments": assignments}


def test_snapshot_files_frozen_by_hash():
    for name, expected in SNAPSHOT_SHA256.items():
        actual = hashlib.sha256((SNAPSHOT_DIR / name).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{name} 已偏离 D10 冻结快照（实际 {actual}）——如为有意的样例更新，"
            f"需显式更新本文件与 docs/D10-真实样例校准报告.md 中的指纹与观察值"
        )


def test_snapshot_accepted_by_domain_models():
    resources, assignments = load_snapshot()
    assert len(resources) == 28
    assert len(assignments) == 62

    for row in resources:  # 逐行过 from_dict，契约形状（多余键忽略）全部可消费
        parsed = Resource.from_dict(row)
        assert parsed.id and parsed.course_id and parsed.kind and parsed.title
    for row in assignments:
        parsed = Assignment.from_dict(row)
        assert parsed.id and parsed.course_id and parsed.title

    # 尺寸/许可分布（PROVENANCE §2 口径）：
    assert Counter(r["kind"] for r in resources) == {"document": 22, "other": 4, "slides": 2}
    assert Counter(r["access_level"] for r in resources) == {"full": 23, "preview_only": 5}
    assert Counter(r["course_id"] for r in resources) == {"crs_5a1b01": 21, "crs_5a1b02": 7}
    assert Counter(a["course_id"] for a in assignments) == {"crs_5a1b01": 8, "crs_5a1b02": 54}
    assert len({r["title"] for r in resources}) == 28  # 标题唯一（无同课同名族）
    assert {r["version"] for r in resources} == {1}


def test_generation_on_real_snapshot_is_deterministic():
    first = build_tasks_json(real_payload())
    second = build_tasks_json(real_payload())
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )

    tasks = first["tasks"]
    assert len(tasks) == 90
    # PD-2 后：22 document + 2 slides → 24 reading；4 个 mp4（kind=other）→ watching
    assert Counter(t["type"] for t in tasks) == {"reading": 24, "watching": 4, "assignment": 62}
    assert len({t["id"] for t in tasks}) == 90
    assert first["warnings"] == []


def test_extent_dormant_resource_tasks_fall_back_without_warning():
    """真实样例 extent 恒 null → 页/张估算路径休眠：24 阅读 + 4 观看任务静默兜底 60 分钟。

    注意：unit 为 null 时走静默兜底（无 UNSUPPORTED_EXTENT 告警，该告警仅针对
    未知单位）；故用户侧看不到"耗时是默认值"的提示——这正是无法用本样例校准
    minutes_per_unit 的直接证据（缺 ① 页码/张数）。若 B 将来用 D2 解析回填
    extent_value/unit，本断言须显式更新。
    """
    resources, _ = load_snapshot()
    assert all(r["extent_value"] is None and r["extent_unit"] is None for r in resources)
    assert all(r["mime_type"] is None and r["content_hash"] is None for r in resources)

    result = build_tasks_json(real_payload())
    readings = [t for t in result["tasks"] if t["type"] == "reading"]
    assert len(readings) == 24
    assert {t["estimated_minutes"] for t in readings} == {60}  # TaskGenConfig 默认 reading=60
    assert {t["estimate_provenance"] for t in readings} == {"inferred"}
    # PD-2：4 个 mp4（kind=other、mime 为 null、扩展名识别）→ watching，兜底 watching=60
    watchings = [t for t in result["tasks"] if t["type"] == "watching"]
    assert len(watchings) == 4
    assert {t["estimated_minutes"] for t in watchings} == {60}
    assert {t["completion_criteria"] for t in watchings} == {"观看视频资料并记录要点"}
    assert "UNSUPPORTED_EXTENT" not in {w["code"] for w in result["warnings"]}


def test_assignment_tasks_pinned_no_duration_evidence():
    """62 条作业全部为规则兜底 90 分钟——无语义输入（无附件、无说明），无耗时观测。"""
    _, assignments = load_snapshot()
    assert all(not a["attachment_resource_ids"] for a in assignments)
    assert all(a["instructions"] is None for a in assignments)

    result = build_tasks_json(real_payload())
    hw = [t for t in result["tasks"] if t["type"] == "assignment"]
    assert len(hw) == 62
    assert {t["estimated_minutes"] for t in hw} == {90}
    assert {t["estimate_provenance"] for t in hw} == {"inferred"}
    assert {t["estimate_confidence"] for t in hw} == {None}


def test_due_shape_matches_datetime_assumption():
    """62/62 due_precision=datetime，ISO8601 UTC `Z`+毫秒；北京时间分布见下。

    PROVENANCE §3.1：15:59:00.000Z ≡ 北京 23:59（当日截止语义）；另有 6 条 13:15Z
    （北京 21:15）。与 D7 对齐的契约写法一致——此维度**未观察到偏差**。
    """
    _, assignments = load_snapshot()
    assert all(a["due_precision"] == "datetime" for a in assignments)
    assert all(a["due_at"] is not None for a in assignments)
    assert all(DUE_RE.match(a["due_at"]) for a in assignments)

    beijing_hhmm = Counter(
        datetime.strptime(a["due_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        .replace(tzinfo=timezone.utc)
        .astimezone(BEIJING)
        .strftime("%H:%M")
        for a in assignments
    )
    assert beijing_hhmm == {"23:59": 56, "21:15": 6}


def test_size_guard_observation_four_videos_over_64mb_default():
    """观察（PD-1 落地后）：默认解析护栏 ParseLimits.max_file_bytes=64MB。

    仍超限的仅剩 4 个 preview_only 视频（411–867MB，kind=other，本就不入解析链）；
    原 42.9MB 的 full 许可 PDF（res_r10a019）在新护栏下不再被 FILE_TOO_LARGE 拒收。
    """
    resources, _ = load_snapshot()
    limit = ParseLimits().max_file_bytes
    assert limit == 64_000_000  # PD-1（coord v1.8 裁定）：30MB → 64MB

    over = sorted(r["id"] for r in resources if r["size_bytes"] > limit)
    assert over == ["res_r10a001", "res_r10a002", "res_r10a003", "res_r10a006"]
    over_rows = [r for r in resources if r["id"] in over]
    assert all(r["title"].endswith(".mp4") and r["access_level"] == "preview_only" for r in over_rows)

    pdf = next(r for r in resources if r["id"] == "res_r10a019")
    assert pdf["access_level"] == "full"
    assert pdf["size_bytes"] == 42_879_434
    assert pdf["size_bytes"] <= limit  # PD-1 目标：可下载资源不再被护栏拒收
    assert min(r["size_bytes"] for r in resources) == 15743
    assert max(r["size_bytes"] for r in resources) == 867519546


def test_video_mapping_and_submitted_flags_pinned():
    """PD-2/PD-3 落地后的真实样例观察（曾为 D10 上报的产品偏差，coord v1.8 已裁定）：
    ① 4 个 mp4（kind=other、mime=null）→ watching 任务（ID 含 watching 段，估算走 watching 口径）；
    ② 3 条已提交作业（asg_r10b20/27/28）仍生成 assignment 任务（保留复习价值），
       但 platform_submitted=True；其余作业为 False。due_at 等平台事实未被改动。
    """
    result = build_tasks_json(real_payload())
    watchings = [t for t in result["tasks"] if t["type"] == "watching"]
    assert sorted(t["id"] for t in watchings) == [
        "t:crs_5a1b01:watching:课程资料-A-01-01.mp4",
        "t:crs_5a1b01:watching:课程资料-A-01-02.mp4",
        "t:crs_5a1b01:watching:课程资料-A-01-03.mp4",
        "t:crs_5a1b01:watching:课程资料-A-04-01.mp4",
    ]

    _, assignments = load_snapshot()
    submitted = sorted(a["id"] for a in assignments if a["submission_status"] == "submitted")
    assert submitted == ["asg_r10b20", "asg_r10b27", "asg_r10b28"]
    by_id = {t["id"]: t for t in result["tasks"]}
    for task_id in (
        "t:crs_5a1b02:assignment:xzzd-assign-r10b20",
        "t:crs_5a1b02:assignment:xzzd-assign-r10b27",
        "t:crs_5a1b02:assignment:xzzd-assign-r10b28",
    ):
        assert by_id[task_id]["platform_submitted"] is True
        assert by_id[task_id]["status"] == "pending"  # 学习状态与平台提交状态分离
    flagged = sum(1 for t in result["tasks"] if t["platform_submitted"])
    assert flagged == 3
    # due_at 等平台事实原样透传（未被 PD-3 改写）
    assert by_id["t:crs_5a1b02:assignment:xzzd-assign-r10b20"]["due_at"] == next(
        a["due_at"] for a in assignments if a["id"] == "asg_r10b20"
    )
