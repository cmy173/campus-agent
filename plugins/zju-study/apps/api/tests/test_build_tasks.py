"""B13 任务生成接入测试：worker build_tasks 处理器、幂等与修正保留、自动派发、告警可展示。

覆盖验收：① 资源/作业/回放三类来源 + 既有任务 → 候选 LearningTask 落库、估算正确；
② match_key 幂等——重复触发不造重复行、无变化不递增 revision；③ 用户修正（耗时/状态）保留；
④ parse 成功后自动派发（限定课程）+ 手动 POST /tasks/build 两条路径，parse 失败不派发；
⑤ job.warnings 携带 D 的码值/文案（DUPLICATE_CONTENT 样例 + job 契约 schema 校验）。
另覆盖：归档任务不复活、课程范围隔离与全局构建、同范围去抖复用、未知课程 404。
"""
from __future__ import annotations

import copy
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import worker
from contract_support import validate_contract

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PDF_BYTES = b"%PDF-1.4\n% build_tasks test\n"

_JOB_FIELDS = (
    "status", "progress", "errors", "result_refs", "warnings", "payload",
    "cancel_requested", "started_at", "finished_at",
)
_TASK_FIELDS = (
    "course_id", "type", "title", "estimated_minutes", "due_at", "due_precision",
    "prerequisite_ids", "status", "fact_source", "match_key", "revision",
    "archived_at", "created_at", "updated_at",
)


def _docx_bytes(paragraphs: list[tuple[str | None, str]]) -> bytes:
    """最小 DOCX：D2 的 docx 解析器只读取 word/document.xml。"""
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
    [("Heading1", "B13 自动派发章节"), (None, "任务生成接入测试的正文段落，字符总数超过二十个。")]
)


@pytest.fixture()
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    return tmp_path / "files"


@pytest.fixture(autouse=True)
def _isolation():
    """共享会话库隔离：快照 job/任务/来源引用全字段，用例后清理新增行并还原被触动行。"""
    from app.db import SessionLocal
    from app.models import (
        Assignment,
        ContentChunk,
        Course,
        FileRecord,
        IdempotencyRecord,
        Job,
        LearningTask,
        Recording,
        Resource,
        ResourceVersion,
        SourceRef,
        TaskFeedback,
    )

    with SessionLocal() as db:
        jobs_before = {
            j.id: {f: copy.deepcopy(getattr(j, f)) for f in _JOB_FIELDS}
            for j in db.execute(select(Job)).scalars()
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
        courses_before = set(db.execute(select(Course.id)).scalars())
        assignments_before = set(db.execute(select(Assignment.id)).scalars())
        recordings_before = set(db.execute(select(Recording.id)).scalars())
        resources_before = set(db.execute(select(Resource.id)).scalars())
        files_before = set(db.execute(select(FileRecord.id)).scalars())
        feedback_before = set(db.execute(select(TaskFeedback.id)).scalars())
        idem_before = set(db.execute(select(IdempotencyRecord.key)).scalars())

    yield

    with SessionLocal() as db:
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


# ────────────────────────── 数据构造 ──────────────────────────


def _add_course(course_id: str, title: str) -> None:
    from app.db import SessionLocal
    from app.models import Course

    with SessionLocal() as db:
        db.add(Course(id=course_id, title=title))
        db.commit()


def _source_ref(owner_type: str, owner_id: str, ref: tuple[str, str, str]):
    from app.models import SourceRef, utc_now

    platform, external_id, canonical_url = ref
    return SourceRef(
        owner_type=owner_type,
        owner_id=owner_id,
        platform=platform,
        external_id=external_id,
        canonical_url=canonical_url,
        observed_at=utc_now(),
        seq=0,
    )


def _add_resource(
    resource_id: str,
    course_id: str,
    *,
    title: str,
    kind: str = "handout",
    version: int = 1,
    content_hash: str | None = None,
    extent_value: int | None = None,
    extent_unit: str | None = None,
    parse_status: str = "ready",
    ref: tuple[str, str, str] | None = None,
) -> None:
    from app.db import SessionLocal
    from app.models import OWNER_RESOURCE, Resource, utc_now

    with SessionLocal() as db:
        db.add(
            Resource(
                id=resource_id,
                course_id=course_id,
                kind=kind,
                title=title,
                version=version,
                content_hash=content_hash,
                parse_status=parse_status,
                extent_value=extent_value,
                extent_unit=extent_unit,
                fetched_at=utc_now(),
            )
        )
        if ref is not None:
            db.add(_source_ref(OWNER_RESOURCE, resource_id, ref))
        db.commit()


def _add_assignment(
    assignment_id: str,
    course_id: str,
    *,
    title: str,
    due_at: datetime | None = None,
    due_precision: str = "unknown",
    submission_status: str = "not_submitted",
    ref: tuple[str, str, str] | None = None,
) -> None:
    from app.db import SessionLocal
    from app.models import OWNER_ASSIGNMENT, Assignment, utc_now

    with SessionLocal() as db:
        db.add(
            Assignment(
                id=assignment_id,
                course_id=course_id,
                title=title,
                due_at=due_at,
                due_precision=due_precision,
                submission_status=submission_status,
                observed_at=utc_now(),
            )
        )
        if ref is not None:
            db.add(_source_ref(OWNER_ASSIGNMENT, assignment_id, ref))
        db.commit()


def _add_recording(
    recording_id: str,
    course_id: str,
    *,
    title: str,
    duration_seconds: int | None = None,
    transcript_status: str = "none",
    ref: tuple[str, str, str] | None = None,
) -> None:
    from app.db import SessionLocal
    from app.models import OWNER_RECORDING, Recording, utc_now

    with SessionLocal() as db:
        db.add(
            Recording(
                id=recording_id,
                course_id=course_id,
                title=title,
                duration_seconds=duration_seconds,
                transcript_status=transcript_status,
                observed_at=utc_now(),
            )
        )
        if ref is not None:
            db.add(_source_ref(OWNER_RECORDING, recording_id, ref))
        db.commit()


# ────────────────────────── 请求辅助 ──────────────────────────


def _trigger_build(client: TestClient, course_id: str | None = None) -> dict:
    if course_id is None:
        resp = client.post("/api/v1/tasks/build")
    else:
        resp = client.post("/api/v1/tasks/build", json={"course_id": course_id})
    assert resp.status_code == 202, resp.text
    return resp.json()["job"]


def _drain(limit: int = 20) -> None:
    for _ in range(limit):
        if not worker.process_next_job():
            return


def _get_job(client: TestClient, job_id: str) -> dict:
    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _course_tasks(client: TestClient, course_id: str, *, include_archived: bool = True) -> list[dict]:
    resp = client.get(
        "/api/v1/tasks",
        params={"course_id": course_id, "include_archived": "true" if include_archived else "false", "limit": 200},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _build_jobs_for(course_id: str):
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        return [
            j
            for j in db.execute(select(Job).where(Job.type == "build_tasks")).scalars()
            if (j.payload or {}).get("course_id") == course_id
        ]


# ────────────────────────── 用例 ──────────────────────────


def test_manual_build_generates_tasks_from_all_sources(client: TestClient) -> None:
    """① + ⑤：手动触发后三类来源各生成一条任务，估算/匹配键/来源引用正确，job 契约校验通过。"""
    course = "crs_b13a1"
    _add_course(course, "B13 三类来源课程")
    _add_resource(
        "res_b13r1",
        course,
        title="B13 讲义一",
        extent_value=30,
        extent_unit="page",
        content_hash="hash-b13-1",
        ref=("moodle", "m1", "https://moodle.example/mod/resource?id=1"),
    )
    _add_assignment(
        "asg_b13a1",
        course,
        title="B13 作业一",
        due_at=datetime(2026, 10, 20, 15, 59, tzinfo=timezone.utc),
        due_precision="datetime",
        ref=("moodle", "a1", "https://moodle.example/mod/assign?id=1"),
    )
    _add_recording(
        "rcd_b13r1",
        course,
        title="B13 回放一",
        duration_seconds=3600,
        ref=("moodle", "rec1", "https://moodle.example/mod/recording?id=1"),
    )

    job = _trigger_build(client, course)
    assert job["type"] == "build_tasks"
    assert job["status"] == "queued"
    _drain()

    body = _get_job(client, job["id"])
    assert body["status"] == "succeeded", body
    assert body["progress"] == 1.0
    assert body["warnings"] == []
    assert len(body["result_refs"]) == 3
    assert all(ref["type"] == "task" for ref in body["result_refs"])
    validate_contract(body, "job.schema.json")

    items = _course_tasks(client, course)
    assert len(items) == 3
    for task in items:
        assert task["id"].startswith("tsk_")
        assert task["archived_at"] is None
    by_type = {t["type"]: t for t in items}
    assert set(by_type) == {"reading", "assignment", "watching"}

    reading = by_type["reading"]
    assert reading["title"] == "B13 讲义一"
    assert reading["estimated_minutes"] == 90  # 30 页 × 3 分钟/页
    assert reading["match_key"] == f"t:{course}:reading:B13-讲义一"
    assert reading["due_at"] is None
    assert [ref["external_id"] for ref in reading["source_refs"]] == ["m1"]

    assignment = by_type["assignment"]
    assert assignment["estimated_minutes"] == 90  # 作业无用户估算 → 类型默认值
    assert assignment["due_at"] == "2026-10-20T15:59:00Z"
    assert assignment["due_precision"] == "datetime"

    watching = by_type["watching"]
    assert watching["estimated_minutes"] == 60  # 3600 秒 ÷ 60
    assert watching["match_key"] == f"t:{course}:watching:rec1"


def test_build_rerun_is_idempotent(client: TestClient) -> None:
    """②：同输入重复触发→相同 match_key 集合、行数不变、无变化不递增 revision。"""
    course = "crs_b13a2"
    _add_course(course, "B13 幂等课程")
    _add_resource("res_b13r2", course, title="B13 幂等讲义", extent_value=10, extent_unit="page", content_hash="hash-b13-2")

    _trigger_build(client, course)
    _drain()
    before = _course_tasks(client, course)
    assert len(before) == 1

    second = _trigger_build(client, course)
    _drain()
    body = _get_job(client, second["id"])
    assert body["status"] == "succeeded"
    assert body["result_refs"] == []  # 无新建、无更新
    assert body["warnings"] == []

    after = _course_tasks(client, course)
    assert [t["id"] for t in after] == [t["id"] for t in before]
    assert [t["match_key"] for t in after] == [t["match_key"] for t in before]
    assert after[0]["estimated_minutes"] == before[0]["estimated_minutes"] == 30
    assert after[0]["revision"] == before[0]["revision"]
    assert after[0]["updated_at"] == before[0]["updated_at"]


def test_user_corrections_preserved_on_rebuild(client: TestClient) -> None:
    """③：人工改耗时/推进状态后重跑，估算与完成状态不被覆盖。"""
    course = "crs_b13a3"
    _add_course(course, "B13 修正保留课程")
    _add_resource("res_b13r3", course, title="B13 修正讲义", extent_value=30, extent_unit="page", content_hash="hash-b13-3")

    _trigger_build(client, course)
    _drain()
    (task,) = _course_tasks(client, course)
    assert task["estimated_minutes"] == 90

    patched = client.patch(f"/api/v1/tasks/{task['id']}", json={"estimated_minutes": 15})
    assert patched.status_code == 200, patched.text
    patched = client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "in_progress"})
    assert patched.status_code == 200, patched.text
    revision_after_patch = patched.json()["revision"]

    job = _trigger_build(client, course)
    _drain()
    body = _get_job(client, job["id"])
    assert body["status"] == "succeeded"
    assert body["result_refs"] == []  # 修正后的值即当前值，无需改写

    (task_after,) = _course_tasks(client, course)
    assert task_after["estimated_minutes"] == 15
    assert task_after["status"] == "in_progress"
    assert task_after["revision"] == revision_after_patch


def test_archived_task_not_revived(client: TestClient) -> None:
    """②：已归档任务在重建时不复活、不改写，也不产生新的重复行。"""
    course = "crs_b13a4"
    _add_course(course, "B13 归档课程")
    _add_resource("res_b13r4", course, title="B13 归档讲义", extent_value=10, extent_unit="page", content_hash="hash-b13-4")

    _trigger_build(client, course)
    _drain()
    (task,) = _course_tasks(client, course)
    assert client.delete(f"/api/v1/tasks/{task['id']}").status_code == 204
    assert _course_tasks(client, course, include_archived=False) == []

    job = _trigger_build(client, course)
    _drain()
    body = _get_job(client, job["id"])
    assert body["status"] == "succeeded"
    assert body["result_refs"] == []

    items = _course_tasks(client, course)
    assert len(items) == 1
    assert items[0]["id"] == task["id"]
    assert items[0]["archived_at"] is not None


def test_duplicate_content_warning_visible(client: TestClient) -> None:
    """⑤：同内容文件合并为一条任务，DUPLICATE_CONTENT 告警带码值/文案/诊断细节并过契约校验。"""
    course = "crs_b13a5"
    _add_course(course, "B13 重复内容课程")
    _add_resource(
        "res_b13d1",
        course,
        title="B13 重复A",
        content_hash="same-hash-b13",
        extent_value=5,
        extent_unit="page",
        ref=("moodle", "d1", "https://moodle.example/mod/resource?id=d1"),
    )
    _add_resource(
        "res_b13d2",
        course,
        title="B13 重复B",
        content_hash="same-hash-b13",
        extent_value=5,
        extent_unit="page",
        ref=("moodle", "d2", "https://moodle.example/mod/resource?id=d2"),
    )

    job = _trigger_build(client, course)
    _drain()
    body = _get_job(client, job["id"])
    assert body["status"] == "succeeded", body

    warnings = body["warnings"]
    assert len(warnings) == 1
    (warn,) = warnings
    assert warn["code"] == "DUPLICATE_CONTENT"
    assert "B13 重复A" in warn["message"] and "B13 重复B" in warn["message"]
    assert warn["task_ids"] == []
    assert warn["details"]["kept_resource_id"] == "res_b13d1"
    assert warn["details"]["skipped_resource_id"] == "res_b13d2"
    validate_contract(body, "job.schema.json")

    items = _course_tasks(client, course)
    assert len(items) == 1
    assert items[0]["estimated_minutes"] == 15
    assert {ref["external_id"] for ref in items[0]["source_refs"]} == {"d1", "d2"}


def test_parse_success_auto_dispatches_build(client: TestClient, files_dir) -> None:
    """④：导入→解析成功后在课程范围内自动派发 build_tasks（reason=parse），任务随即落库。"""
    course = "crs_b13a6"
    _add_course(course, "B13 自动派发课程")
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": course, "kind": "handout", "title": "B13 自动派发讲义"})},
        files=[("files", ("b13.docx", _READY_DOCX, DOCX_MIME))],
    )
    assert resp.status_code == 202, resp.text
    import_job_id = resp.json()["job"]["id"]
    _drain()

    import_body = _get_job(client, import_job_id)
    assert import_body["status"] == "succeeded", import_body

    build_jobs = _build_jobs_for(course)
    assert len(build_jobs) == 1
    build_job = build_jobs[0]
    assert build_job.status == "succeeded"
    assert (build_job.payload or {}).get("reason") == "parse"
    assert any(ref.get("type") == "task" for ref in (build_job.result_refs or []))

    items = _course_tasks(client, course)
    assert len(items) == 1
    assert items[0]["title"] == "B13 自动派发讲义"
    assert items[0]["type"] == "reading"
    assert items[0]["estimated_minutes"] == 60  # DOCX 无篇幅信息 → 类型默认值


def test_parse_failure_does_not_dispatch_build(client: TestClient, files_dir) -> None:
    """④：解析失败不派发任务生成（避免把估算锁定在默认值），课程内无任务。"""
    course = "crs_b13a7"
    _add_course(course, "B13 解析失败课程")
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": course, "kind": "handout", "title": "B13 坏文件"})},
        files=[("files", ("broken.pdf", PDF_BYTES, "application/pdf"))],
    )
    assert resp.status_code == 202, resp.text
    import_job_id = resp.json()["job"]["id"]
    _drain()

    import_body = _get_job(client, import_job_id)
    (res_ref,) = import_body["result_refs"]
    res_id = res_ref["id"]

    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        parse_jobs = [
            j
            for j in db.execute(select(Job).where(Job.type == "parse")).scalars()
            if (j.payload or {}).get("resource_id") == res_id
        ]
    assert len(parse_jobs) == 1
    assert parse_jobs[0].status == "failed"

    assert _build_jobs_for(course) == []
    assert _course_tasks(client, course) == []


def test_scope_isolation_global_build_and_invalid_course(client: TestClient) -> None:
    """课程范围隔离；全局构建补全其余课程；未知课程 404。"""
    course_a = "crs_b13a8"
    course_b = "crs_b13b8"
    _add_course(course_a, "B13 范围课程A")
    _add_course(course_b, "B13 范围课程B")
    _add_resource("res_b13a8", course_a, title="B13 范围讲义A", extent_value=10, extent_unit="page", content_hash="hash-b13-a8")
    _add_resource("res_b13b8", course_b, title="B13 范围讲义B", extent_value=8, extent_unit="slide", content_hash="hash-b13-b8")

    _trigger_build(client, course_a)
    _drain()
    assert len(_course_tasks(client, course_a)) == 1
    assert _course_tasks(client, course_b) == []

    job = _trigger_build(client, None)  # 全局（course_id 缺省）
    _drain()
    body = _get_job(client, job["id"])
    assert body["status"] == "succeeded", body

    items_b = _course_tasks(client, course_b)
    assert len(items_b) == 1
    assert items_b[0]["estimated_minutes"] == 16  # 8 幻灯片 × 2 分钟/页
    assert len(_course_tasks(client, course_a)) == 1  # 范围构建的成果不被全局构建重复

    resp = client.post("/api/v1/tasks/build", json={"course_id": "crs_b13_nope"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_build_trigger_debounces_active_job(client: TestClient) -> None:
    """同课程范围已有排队任务时复用而不重复入队，执行后仅一条 build_tasks 记录。"""
    course = "crs_b13a9"
    _add_course(course, "B13 去抖课程")
    _add_resource("res_b13a9", course, title="B13 去抖讲义", extent_value=5, extent_unit="page", content_hash="hash-b13-a9")

    first = _trigger_build(client, course)
    second = _trigger_build(client, course)
    assert first["id"] == second["id"]
    _drain()

    build_jobs = _build_jobs_for(course)
    assert len(build_jobs) == 1
    assert build_jobs[0].status == "succeeded"
    assert len(_course_tasks(client, course)) == 1


def test_watching_type_accepted_by_write_api_and_contract_aligned(client: TestClient) -> None:
    """PD-2：watching（回放/视频类）在 B 层全链路无白名单阻碍、枚举三方一致。

    B 落库原样透传（build_engine 不设类型白名单；recording 来源的 watching 已由
    用例①覆盖）；此处验证写 API（POST/PATCH）接受 watching、响应过契约校验，
    且 schemas.TaskType == 契约枚举（未遗漏），job 类型与任务类型正交（JOB_TYPES 无需含 watching）。
    """
    from typing import get_args

    from app import config
    from app.routers.jobs import JOB_TYPES
    from app.schemas import TaskType

    enum = json.loads(
        (config.REPO_ROOT / "contracts" / "schemas" / "learning-task.schema.json").read_text(encoding="utf-8")
    )["properties"]["type"]["enum"]
    assert list(get_args(TaskType)) == enum
    assert "watching" in enum
    assert set(JOB_TYPES).isdisjoint(enum)  # job 类型（import/parse/...）与任务类型正交

    course = "crs_pd2a1"
    _add_course(course, "PD-2 观看类课程")
    resp = client.post(
        "/api/v1/tasks",
        json={"course_id": course, "type": "watching", "title": "PD-2 回放观看", "estimated_minutes": 45},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["type"] == "watching"
    validate_contract(created, "learning-task.schema.json")

    patched = client.patch(f"/api/v1/tasks/{created['id']}", json={"type": "watching", "estimated_minutes": 30})
    assert patched.status_code == 200, patched.text
    assert patched.json()["type"] == "watching" and patched.json()["estimated_minutes"] == 30
