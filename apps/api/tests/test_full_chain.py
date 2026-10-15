"""B11 全链路集成测试：导入 → 解析 → 生成任务（B13）→ 计划预览 → 应用 → 反馈 → 导出下载 + PLAN_STALE。

一条端到端用例走通 M1 主链路；隔离夹具快照/还原 job、任务、来源引用、资源族、课程、
计划族（含块/未安排/告警）等被触动的行，保证不污染共享会话库（test_plans_api 等文件
依赖种子数据的原始状态）。
"""
from __future__ import annotations

import copy
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import worker

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_JOB_FIELDS = (
    "status", "progress", "errors", "result_refs", "warnings", "payload",
    "cancel_requested", "started_at", "finished_at",
)
_TASK_FIELDS = (
    "course_id", "type", "title", "estimated_minutes", "due_at", "due_precision",
    "prerequisite_ids", "status", "fact_source", "match_key", "revision",
    "archived_at", "created_at", "updated_at",
)
_PLAN_FIELDS = (
    "version", "status", "input_revision", "horizon_start", "horizon_end", "summary",
    "based_on_plan_id", "superseded_by", "created_at", "applied_at", "revoked_at",
)


def _docx_bytes(paragraphs: list[tuple[str | None, str]]) -> bytes:
    parts: list[str] = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        parts.append(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>' + "".join(parts) + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


_READY_DOCX = _docx_bytes(
    [("Heading1", "B11 全链路章节"), (None, "全链路集成测试的正文段落，字符总数超过二十个字符。")]
)


@pytest.fixture()
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    return tmp_path / "files"


@pytest.fixture(autouse=True)
def _isolation():
    """共享会话库隔离：快照被触动的表，用例后清理新增行并还原既有行（含计划族与 job.warnings）。"""
    from app.db import SessionLocal
    from app.models import (
        Assignment,
        ContentChunk,
        Course,
        FileRecord,
        IdempotencyRecord,
        Job,
        LearningTask,
        Plan,
        PlanBlock,
        PlanUnassigned,
        PlanWarning,
        Recording,
        Resource,
        ResourceVersion,
        SourceRef,
        TaskFeedback,
    )

    with SessionLocal() as db:
        jobs_before = {
            j.id: {f: copy.deepcopy(getattr(j, f)) for f in _JOB_FIELDS} for j in db.execute(select(Job)).scalars()
        }
        tasks_before = {
            t.id: {f: copy.deepcopy(getattr(t, f)) for f in _TASK_FIELDS}
            for t in db.execute(select(LearningTask)).scalars()
        }
        refs_before = {
            (r.owner_type, r.owner_id, r.platform, r.external_id): {
                "canonical_url": r.canonical_url,
                "observed_at": r.observed_at,
                "seq": r.seq,
            }
            for r in db.execute(select(SourceRef)).scalars()
        }
        plans_before = {
            p.id: {f: copy.deepcopy(getattr(p, f)) for f in _PLAN_FIELDS} for p in db.execute(select(Plan)).scalars()
        }
        blocks_before = {
            (b.plan_id, b.id): {
                "task_id": b.task_id,
                "starts_at": b.starts_at,
                "ends_at": b.ends_at,
                "locked": b.locked,
            }
            for b in db.execute(select(PlanBlock)).scalars()
        }
        unassigned_before = {
            (u.plan_id, u.task_id): {
                "reason_code": u.reason_code,
                "shortage_minutes": u.shortage_minutes,
                "message": u.message,
                "suggestion": u.suggestion,
            }
            for u in db.execute(select(PlanUnassigned)).scalars()
        }
        warnings_before = {
            w.id: {
                "plan_id": w.plan_id,
                "code": w.code,
                "message": w.message,
                "task_ids": copy.deepcopy(list(w.task_ids or [])),
                "details": copy.deepcopy(w.details),
            }
            for w in db.execute(select(PlanWarning)).scalars()
        }
        courses_before = set(db.execute(select(Course.id)).scalars())
        assignments_before = set(db.execute(select(Assignment.id)).scalars())
        recordings_before = set(db.execute(select(Recording.id)).scalars())
        resources_before = set(db.execute(select(Resource.id)).scalars())
        files_before = set(db.execute(select(FileRecord.id)).scalars())
        feedback_before = set(db.execute(select(TaskFeedback.id)).scalars())
        idem_before = set(db.execute(select(IdempotencyRecord.key)).scalars())

    yield

    with SessionLocal() as db:
        for warn in list(db.execute(select(PlanWarning)).scalars()):
            if warn.id not in warnings_before:
                db.delete(warn)
        for warn_id, snapshot in warnings_before.items():
            row = db.get(PlanWarning, warn_id)
            if row is not None:
                row.plan_id = snapshot["plan_id"]
                row.code = snapshot["code"]
                row.message = snapshot["message"]
                row.task_ids = snapshot["task_ids"]
                row.details = snapshot["details"]
        for item in list(db.execute(select(PlanUnassigned)).scalars()):
            key = (item.plan_id, item.task_id)
            if key not in unassigned_before:
                db.delete(item)
            else:
                snapshot = unassigned_before[key]
                for field, value in snapshot.items():
                    setattr(item, field, value)
        for block in list(db.execute(select(PlanBlock)).scalars()):
            key = (block.plan_id, block.id)
            if key not in blocks_before:
                db.delete(block)
            else:
                snapshot = blocks_before[key]
                for field, value in snapshot.items():
                    setattr(block, field, value)
        for plan in list(db.execute(select(Plan)).scalars()):
            if plan.id not in plans_before:
                db.delete(plan)
        for plan_id, snapshot in plans_before.items():
            row = db.get(Plan, plan_id)
            if row is not None:
                for field, value in snapshot.items():
                    setattr(row, field, value)
        for job in list(db.execute(select(Job)).scalars()):
            if job.id not in jobs_before:
                db.delete(job)
        for job_id, snapshot in jobs_before.items():
            row = db.get(Job, job_id)
            if row is not None:
                for field, value in snapshot.items():
                    setattr(row, field, value)
        for task in list(db.execute(select(LearningTask)).scalars()):
            if task.id not in tasks_before:
                db.delete(task)
        for task_id, snapshot in tasks_before.items():
            row = db.get(LearningTask, task_id)
            if row is not None:
                for field, value in snapshot.items():
                    setattr(row, field, value)
        for ref in list(db.execute(select(SourceRef)).scalars()):
            key = (ref.owner_type, ref.owner_id, ref.platform, ref.external_id)
            if key not in refs_before:
                db.delete(ref)
            else:
                snapshot = refs_before[key]
                ref.canonical_url = snapshot["canonical_url"]
                ref.observed_at = snapshot["observed_at"]
                ref.seq = snapshot["seq"]
        for res_id in [r for r in db.execute(select(Resource.id)).scalars() if r not in resources_before]:
            db.execute(delete(ContentChunk).where(ContentChunk.resource_id == res_id))
            db.execute(delete(ResourceVersion).where(ResourceVersion.resource_id == res_id))
            db.delete(db.get(Resource, res_id))
        for model, before in ((Assignment, assignments_before), (Recording, recordings_before), (Course, courses_before)):
            for obj_id in [i for i in db.execute(select(model.id)).scalars() if i not in before]:
                db.delete(db.get(model, obj_id))
        for file_id in [f for f in db.execute(select(FileRecord.id)).scalars() if f not in files_before]:
            db.delete(db.get(FileRecord, file_id))
        for feedback_id in [f for f in db.execute(select(TaskFeedback.id)).scalars() if f not in feedback_before]:
            db.delete(db.get(TaskFeedback, feedback_id))
        for key in [k for k in db.execute(select(IdempotencyRecord.key)).scalars() if k not in idem_before]:
            db.delete(db.get(IdempotencyRecord, key))
        db.commit()


def _add_course(course_id: str, title: str) -> None:
    from app.db import SessionLocal
    from app.models import Course

    with SessionLocal() as db:
        db.add(Course(id=course_id, title=title))
        db.commit()


def _drain(limit: int = 20) -> None:
    for _ in range(limit):
        if not worker.process_next_job():
            return


def _get_job(client: TestClient, job_id: str) -> dict:
    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _course_tasks(client: TestClient, course_id: str) -> list[dict]:
    resp = client.get(
        "/api/v1/tasks", params={"course_id": course_id, "include_archived": "true", "limit": 200}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_full_chain_import_to_export_with_plan_stale(client: TestClient, files_dir) -> None:
    """单条主链路：导入→解析→生成任务→预览→应用→反馈→导出下载；随后输入变化触发 PLAN_STALE。"""
    course = "crs_b11c01"
    _add_course(course, "B11 全链路课程")

    # 1) 导入 DOCX
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": course, "kind": "handout", "title": "B11 全链路讲义"})},
        files=[("files", ("b11.docx", _READY_DOCX, DOCX_MIME))],
    )
    assert resp.status_code == 202, resp.text
    import_job_id = resp.json()["job"]["id"]

    # 2) worker 走完 导入 → 解析 → 生成任务（B13 自动派发）
    _drain()
    import_body = _get_job(client, import_job_id)
    assert import_body["status"] == "succeeded", import_body
    resource_id = import_body["result_refs"][0]["id"]

    listed = client.get("/api/v1/jobs", params={"type": "build_tasks", "limit": 200})
    assert listed.status_code == 200, listed.text
    assert any(item["id"] != "job_9h1k2m" and item["status"] == "succeeded" for item in listed.json()["items"])

    tasks = _course_tasks(client, course)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["type"] == "reading"
    assert task["estimated_minutes"] == 60
    assert task["match_key"] == f"t:{course}:reading:B11-全链路讲义"

    # 3) 计划预览：新任务被排入时间块（合计时长=估算分钟数）
    preview = client.post("/api/v1/plans/preview")
    assert preview.status_code == 201, preview.text
    plan = preview.json()["plan"]
    assert plan["status"] == "candidate"
    my_blocks = [b for b in plan["blocks"] if b["task_id"] == task["id"]]
    assert my_blocks, f"任务未排入计划：{plan['unassigned']}"
    minutes = sum((_parse(b["ends_at"]) - _parse(b["starts_at"])).total_seconds() / 60 for b in my_blocks)
    assert minutes == 60

    # 4) 应用候选：旧生效计划被取代
    applied = client.post(f"/api/v1/plans/{plan['id']}/apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["plan"]["status"] == "active"
    assert applied.json()["previous_plan_id"] is not None

    # 5) 学习反馈：任务完成、建议重排（生效计划存在且输入已变）
    feedback = client.post(
        f"/api/v1/tasks/{task['id']}/feedback",
        json={"outcome": "completed", "actual_minutes": 55, "difficulty": 3},
    )
    assert feedback.status_code == 201, feedback.text
    feedback_body = feedback.json()
    feedback_id = feedback_body["feedback"]["id"]
    assert feedback_body["task"]["status"] == "completed"
    assert feedback_body["replan_suggested"] is True
    current = client.get("/api/v1/plans/current").json()
    assert current["current_input_revision"] != plan["input_revision"]

    # 6) 导出（默认全部类别）→ 7) 经 GET /files/{id} 下载核对
    export = client.post("/api/v1/exports", json={})
    assert export.status_code == 202, export.text
    export_job_id = export.json()["job"]["id"]
    _drain()
    export_body = _get_job(client, export_job_id)
    assert export_body["status"] == "succeeded", export_body
    file_refs = [ref for ref in export_body["result_refs"] if ref["type"] == "export_file"]
    assert len(file_refs) == 1

    download = client.get(f"/api/v1/files/{file_refs[0]['id']}")
    assert download.status_code == 200, download.text
    document = download.json()
    sections = document["sections"]
    assert {t["id"] for t in sections["tasks"]} >= {task["id"]}
    assert {f["id"] for f in sections["feedback"]} >= {feedback_id}
    assert {p["id"] for p in sections["plans"]} >= {plan["id"]}
    assert {c["id"] for c in sections["courses"]} >= {course}
    assert {r["id"] for r in sections["resources"]} >= {resource_id}
    assert sections["courses"] and any(c["id"] == course for c in sections["courses"])

    # 8) PLAN_STALE：预览候选后输入再变化，旧候选应用被拒；重新预览后可应用
    stale_candidate = client.post("/api/v1/plans/preview").json()["plan"]
    created = client.post(
        "/api/v1/tasks",
        json={"course_id": course, "title": "B11 新增手工任务", "estimated_minutes": 20},
    )
    assert created.status_code == 201, created.text

    stale = client.post(f"/api/v1/plans/{stale_candidate['id']}/apply")
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "PLAN_STALE"
    assert error["details"]["plan_id"] == stale_candidate["id"]
    assert error["details"]["input_revision"] == stale_candidate["input_revision"]
    assert error["details"]["current_input_revision"] != stale_candidate["input_revision"]

    fresh = client.post("/api/v1/plans/preview").json()["plan"]
    assert fresh["input_revision"] == error["details"]["current_input_revision"]
    reapply = client.post(f"/api/v1/plans/{fresh['id']}/apply")
    assert reapply.status_code == 200, reapply.text
    assert reapply.json()["plan"]["status"] == "active"
