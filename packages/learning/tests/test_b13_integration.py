"""D12 · B13 集成契约测试：契约形状 JSON 入/出、match_key 幂等、人工修正保留、warnings 供 C 展示。

输入行按 `apps/api/app/models.py` 的 `to_contract()` 输出形状手工构造（Z 结尾时间、tsk_ 主键
+ match_key、多余键），验证薄封装 `build_tasks_json`（taskgen.py）的集成语义：
① B 行原样可消费（无需手工转换）；
② 重复触发（把上一轮结果按持久化形状传回）不产生重复任务，任务集合逐字段一致；
③ 人工修正（完成状态/耗时/完成标准/拆分策略/优先级/前置）不被再生成覆盖，平台派生字段（截止）随来源更新；
④ 未归档人工任务原样保留、不再匹配告警；已归档任务不复活；
⑤ 七类生成告警码齐备、文案非空、结果 JSON 可序列化（C 直接展示）；
⑥ 包导入不带入数据库/HTTP 依赖（纯函数包）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from learning.taskgen import build_tasks_json

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
OBSERVED = "2026-10-10T08:00:00Z"


def b_ref(external_id: str) -> dict:
    return {
        "platform": "xzzd",
        "external_id": external_id,
        "canonical_url": f"https://example.invalid/{external_id}",
        "observed_at": OBSERVED,
    }


def b_resource(**over) -> dict:
    row = {
        "id": "res_slides_1",
        "course_id": "crs_1",
        "kind": "slides",
        "title": "第1讲 课件",
        "source_refs": [b_ref("att_1")],
        "version": 1,
        "content_hash": SHA_A,
        "parse_status": "ready",
        "mime_type": "application/pdf",
        "size_bytes": 1893000,
        "access_level": "full",
        "file_id": "fil_1",
        "extent_value": 30,
        "extent_unit": "page",
        "chunk_count": 12,
        "fetched_at": OBSERVED,
        "source_updated_at": None,
        "archived_at": None,
        "created_at": OBSERVED,
        "updated_at": OBSERVED,
    }
    row.update(over)
    return row


def b_assignment(**over) -> dict:
    row = {
        "id": "asg_hw1",
        "course_id": "crs_1",
        "title": "作业1",
        "instructions": None,
        "due_at": "2026-10-12T15:59:00Z",
        "due_precision": "datetime",
        "submission_status": "unknown",
        "fact_source": "platform",
        "confidence": None,
        "source_ref": b_ref("hw_1"),
        "attachment_resource_ids": [],
        "revision": 1,
        "observed_at": OBSERVED,
        "archived_at": None,
        "created_at": OBSERVED,
        "updated_at": OBSERVED,
    }
    row.update(over)
    return row


def b_recording(**over) -> dict:
    row = {
        "id": "rec_1",
        "course_id": "crs_1",
        "title": "第1讲 回放",
        "started_at": "2026-10-09T10:00:00Z",
        "duration_seconds": 5400,
        "canonical_url": None,
        "transcript_status": "ready",
        "subtitle_languages": ["zh-CN"],
        "transcript_next_check_at": None,
        "fact_source": "platform",
        "source_ref": b_ref("video_1"),
        "observed_at": OBSERVED,
        "created_at": OBSERVED,
        "updated_at": OBSERVED,
    }
    row.update(over)
    return row


def d_task(task_id: str, title: str, **over) -> dict:
    """build_tasks 输出形状的一条任务（用于构造既有行）。"""
    task = {
        "id": task_id,
        "course_id": "crs_1",
        "type": "reading",
        "title": title,
        "estimated_minutes": 60,
        "due_at": None,
        "due_precision": "unknown",
        "prerequisite_ids": [],
        "source_refs": [],
        "status": "pending",
        "completion_criteria": None,
        "estimate_provenance": "inferred",
        "estimate_confidence": None,
        "splittable": None,
        "priority": None,
    }
    task.update(over)
    return task


def b_task_row(task: dict, *, seq: int = 1, **over) -> dict:
    """把任务转成 B 落库行形状：tsk_ 不透明主键 + match_key 稳定键。"""
    row = dict(task)
    row["id"] = f"tsk_{seq:03d}"
    row["match_key"] = task["id"]
    row["fact_source"] = "user" if task["estimate_provenance"] == "user" else "inferred"
    row["revision"] = 1
    row["archived_at"] = None
    row["created_at"] = OBSERVED
    row["updated_at"] = OBSERVED
    row.update(over)
    return row


def base_payload(**over) -> dict:
    payload = {
        "resources": [b_resource()],
        "assignments": [b_assignment()],
        "recordings": [b_recording()],
    }
    payload.update(over)
    return payload


def test_wrapper_consumes_contract_rows_without_conversion():
    result = build_tasks_json(base_payload())

    assert result["warnings"] == []
    by_title = {t["title"]: t for t in result["tasks"]}
    assert set(by_title) == {"第1讲 课件", "作业1", "第1讲 回放"}

    reading = by_title["第1讲 课件"]
    assert reading["id"] == "t:crs_1:reading:第1讲-课件"
    assert reading["estimated_minutes"] == 90  # 30 页 × 3 分钟
    assert reading["source_refs"][0]["external_id"] == "att_1"

    hw = by_title["作业1"]
    assert hw["id"] == "t:crs_1:assignment:hw_1"  # 有 source_ref 时按 external_id 稳定
    assert hw["due_at"] == "2026-10-12T15:59:00Z"  # 契约时间格式原样透传
    assert hw["estimated_minutes"] == 90

    rec = by_title["第1讲 回放"]
    assert rec["id"] == "t:crs_1:watching:video_1"
    assert rec["estimated_minutes"] == 90  # 5400 秒
    assert "待更新" not in rec["completion_criteria"]  # ready 字幕无等待备注


def test_repeated_generation_is_idempotent_with_existing_rows():
    first = build_tasks_json(base_payload())
    stored_rows = [b_task_row(t, seq=i) for i, t in enumerate(first["tasks"], start=1)]
    second = build_tasks_json(base_payload(existing_tasks=stored_rows))

    assert [t["id"] for t in second["tasks"]] == [t["id"] for t in first["tasks"]]
    assert second["tasks"] == first["tasks"]  # 逐字段一致：不产生重复任务、不漂移
    assert second["warnings"] == []


def test_user_corrections_survive_regeneration():
    first = build_tasks_json(base_payload())
    by_title = {t["title"]: t for t in first["tasks"]}
    reading, hw_id = by_title["第1讲 课件"], by_title["作业1"]["id"]

    corrected = b_task_row(
        reading,
        seq=1,
        estimated_minutes=42,
        estimate_provenance="user",
        status="completed",
        completion_criteria="自定：先做题后读书",
        splittable=True,
        priority=5,
        prerequisite_ids=[hw_id],
    )
    second = build_tasks_json(
        base_payload(
            assignments=[b_assignment(due_at="2026-10-20T15:59:00Z")],
            existing_tasks=[corrected],
        )
    )
    merged = {t["title"]: t for t in second["tasks"]}["第1讲 课件"]
    assert merged["estimated_minutes"] == 42  # 人工耗时不被覆盖
    assert merged["estimate_provenance"] == "user"
    assert merged["status"] == "completed"  # 完成状态不被覆盖
    assert merged["completion_criteria"] == "自定：先做题后读书"
    assert merged["splittable"] is True
    assert merged["priority"] == 5
    assert merged["prerequisite_ids"] == [hw_id]
    # 平台派生字段（截止时间）随来源更新，不被"保留"语义冻结
    assert {t["title"]: t for t in second["tasks"]}["作业1"]["due_at"] == "2026-10-20T15:59:00Z"


def test_manual_task_kept_and_archived_task_not_resurrected():
    first = build_tasks_json(base_payload())
    reading = {t["title"]: t for t in first["tasks"]}["第1讲 课件"]

    manual = {
        "id": "tsk_manual",
        "match_key": None,  # B 侧手工任务：只需主键，无稳定键
        "course_id": "crs_1",
        "type": "review",
        "title": "自建复习",
        "estimated_minutes": 30,
        "due_at": None,
        "due_precision": "unknown",
        "prerequisite_ids": [],
        "source_refs": [],
        "status": "pending",
        "fact_source": "user",
        "revision": 1,
        "archived_at": None,
        "created_at": OBSERVED,
        "updated_at": OBSERVED,
    }
    archived = b_task_row(reading, seq=7, status="completed", archived_at="2026-10-10T09:00:00Z")

    second = build_tasks_json(base_payload(existing_tasks=[manual, archived]))
    by_id = {t["id"]: t for t in second["tasks"]}

    assert by_id["tsk_manual"]["title"] == "自建复习"  # 人工任务原样保留
    assert second["warnings"] == []  # 不产生"未匹配"告警
    regenerated = by_id[reading["id"]]
    assert regenerated["status"] == "pending"  # 归档行未参与合并（不复活）
    assert regenerated["estimated_minutes"] == reading["estimated_minutes"]


def mega_payload() -> dict:
    """一次性触发全部七类生成告警的场景。"""
    return {
        "resources": [
            b_resource(id="res_v1", title="同名资料", version=1, content_hash=SHA_A),
            b_resource(id="res_v2", title="同名资料", version=2, content_hash=SHA_B),
            b_resource(id="res_dup", title="另一份资料", content_hash=SHA_A),
            b_resource(
                id="res_char",
                title="字符计量资料",
                content_hash=None,
                extent_value=9000,
                extent_unit="character",
            ),
        ],
        "assignments": [b_assignment()],
        "recordings": [b_recording(id="rec_nodur", title="缺时长回放", duration_seconds=None)],
        "existing_tasks": [b_task_row(d_task("t:crs_1:reading:已移除", "已移除"), seq=9)],
        "user_tasks": [
            {
                "id": "t:crs_1:reading:同名资料",
                "course_id": "crs_1",
                "type": "reading",
                "title": "同名资料",
                "estimated_minutes": 10,
            }
        ],
        "hints": [{"match": {"course_id": "crs_1", "type": "reading", "title": "不存在的资料"}}],
    }


def test_all_warning_codes_have_display_ready_messages():
    result = build_tasks_json(mega_payload())

    codes = {w["code"] for w in result["warnings"]}
    expected = {
        "DUPLICATE_CONTENT",
        "TASK_VERSION_UPDATED",
        "UNSUPPORTED_EXTENT",
        "MISSING_DURATION",
        "DUPLICATE_TASK_ID",
        "EXISTING_TASK_UNMATCHED",
        "HINT_UNRESOLVED",
    }
    assert expected <= codes

    for warning in result["warnings"]:
        assert isinstance(warning["message"], str) and warning["message"].strip()
        assert isinstance(warning["task_ids"], list)
    dup = next(w for w in result["warnings"] if w["code"] == "DUPLICATE_CONTENT")
    assert dup["details"]["skipped_resource_id"] == "res_dup"
    assert dup["details"]["kept_resource_id"] == "res_v1"
    json.dumps(result, ensure_ascii=False)  # C 展示前序列化不抛错

    # 同一 payload 重复调用：输出逐字节一致（确定性支撑幂等语义）
    again = build_tasks_json(mega_payload())
    assert json.dumps(again, ensure_ascii=False) == json.dumps(result, ensure_ascii=False)


_FORBIDDEN_DEPENDENCIES = ["sqlalchemy", "requests", "httpx", "fastapi"]


def test_package_import_pulls_no_db_or_http_dependencies():
    """干净解释器中导入 learning 全部模块：不得引入数据库/HTTP 依赖（纯函数包）。

    必须在子进程（干净解释器）中验证：同一 pytest 进程里 apps/api 等套件会先导入
    这些库，进程内 sys.modules 断言必然假阳性失败（测试隔离缺陷，2026-10-10 修复）。
    """
    modules = [
        "learning",
        "learning.taskgen",
        "learning.models",
        "learning.samples",
        "learning.timeutil",
        "learning.transcript",
        "learning.estimate",
        "learning.model",
        "learning.schedule",
        "learning.schedule.engine",
        "learning.schedule.revise",
        "learning.schedule.validate",
        "learning.parse",
        "learning.parse.pdf",
        "learning.parse.docx",
        "learning.parse.pptx",
    ]
    lines = ["import importlib, sys"]
    lines += [f"importlib.import_module({name!r})" for name in modules]
    lines.append(f"forbidden = set({_FORBIDDEN_DEPENDENCIES!r}) & set(sys.modules)")
    lines.append("assert not forbidden, f'learning 导入链引入了运行时依赖: {sorted(forbidden)}'")
    lines.append("print('IMPORT-CHAIN-OK')")
    code = "\n".join(lines)

    src = str(Path(__file__).resolve().parent.parent / "src")
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"干净解释器导入检查失败（returncode={proc.returncode}）：\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "IMPORT-CHAIN-OK" in proc.stdout  # 证明断言确实执行到（而非提前退出）


def test_import_chain_guard_meta_detects_forbidden_module():
    """元测试：检测表达式对注入的禁用模块必须报警——防止"检查永真/失效"。"""
    code = (
        "import sys\n"
        "sys.modules['fastapi'] = object()\n"
        f"forbidden = set({_FORBIDDEN_DEPENDENCIES!r}) & set(sys.modules)\n"
        "assert forbidden == {'fastapi'}, ('检测逻辑失效', sorted(forbidden))\n"
        "print('META-OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0 and "META-OK" in proc.stdout, (
        f"元测试失败（returncode={proc.returncode}）：\n{proc.stdout}\n{proc.stderr}"
    )
