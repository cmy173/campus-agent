"""PD-3 落地验证：已提交/已评分作业对应的任务保留在库但不排程；input_revision 涵盖提交状态变化。

裁定（协调者代裁 2026-10-10，用户授权）：保留生成、标注"平台已提交"、今日/计划页弱化、不参与排程。
B 侧职责：① 计划生成排除对应任务（按来源引用 platform+external_id 关联，不按任务类型）；
② 指纹涵盖该状态变化——翻转后旧候选应用被 PLAN_STALE 拒绝（用户修改与平台同步
两条写路径均递增作业 revision）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from test_sync_api import _drain_until, _envelope, _post_sync, sync_env  # noqa: F401  （fixture 复用）

COURSE = "crs_pd3a1"


@pytest.fixture(autouse=True)
def _isolation():
    """共享会话库隔离：快照行集合，用例后清理新增（含预览产生的计划与子表）。"""
    from app.db import SessionLocal
    from app.models import (
        Assignment,
        Course,
        LearningTask,
        Plan,
        PlanBlock,
        PlanUnassigned,
        PlanWarning,
        SourceRef,
    )

    with SessionLocal() as db:
        snap = {
            "courses": set(db.execute(select(Course.id)).scalars()),
            "assignments": set(db.execute(select(Assignment.id)).scalars()),
            "tasks": set(db.execute(select(LearningTask.id)).scalars()),
            "plans": set(db.execute(select(Plan.id)).scalars()),
            "refs": set(
                db.execute(
                    select(SourceRef.owner_type, SourceRef.owner_id, SourceRef.platform, SourceRef.external_id)
                ).all()
            ),
        }

    yield

    with SessionLocal() as db:
        for row in db.execute(select(SourceRef)).scalars():
            if (row.owner_type, row.owner_id, row.platform, row.external_id) not in snap["refs"]:
                db.delete(row)
        for row in db.execute(select(LearningTask)).scalars():
            if row.id not in snap["tasks"]:
                db.delete(row)
        for row in db.execute(select(Assignment)).scalars():
            if row.id not in snap["assignments"]:
                db.delete(row)
        new_plans = [p for p in db.execute(select(Plan.id)).scalars() if p not in snap["plans"]]
        if new_plans:
            for tbl in (PlanBlock, PlanUnassigned, PlanWarning):
                db.execute(delete(tbl).where(tbl.plan_id.in_(new_plans)))
            for pid in new_plans:
                db.delete(db.get(Plan, pid))
        for row in db.execute(select(Course)).scalars():
            if row.id not in snap["courses"]:
                db.delete(row)
        db.commit()


def _add_course(course_id: str, title: str) -> None:
    from app.db import SessionLocal
    from app.models import Course

    with SessionLocal() as db:
        db.add(Course(id=course_id, title=title))
        db.commit()


def _ref(owner_type: str, owner_id: str, external_id: str):
    from app.models import SourceRef, utc_now

    return SourceRef(
        owner_type=owner_type,
        owner_id=owner_id,
        platform="xzzd",
        external_id=external_id,
        observed_at=utc_now(),
        seq=0,
    )


def _add_assignment(assignment_id: str, *, title: str, submission_status: str, external_id: str) -> None:
    from app.db import SessionLocal
    from app.models import OWNER_ASSIGNMENT, Assignment, utc_now

    with SessionLocal() as db:
        db.add(
            Assignment(
                id=assignment_id,
                course_id=COURSE,
                title=title,
                submission_status=submission_status,
                fact_source="platform",
                observed_at=utc_now(),
            )
        )
        db.add(_ref(OWNER_ASSIGNMENT, assignment_id, external_id))
        db.commit()


def _add_task(task_id: str, *, title: str, external_id: str) -> None:
    from app.db import SessionLocal
    from app.models import OWNER_TASK, LearningTask

    with SessionLocal() as db:
        db.add(
            LearningTask(
                id=task_id,
                course_id=COURSE,
                type="assignment",
                title=title,
                estimated_minutes=20,
                status="pending",
                fact_source="platform",
            )
        )
        db.add(_ref(OWNER_TASK, task_id, external_id))
        db.commit()


def _preview(client: TestClient) -> dict:
    resp = client.post("/api/v1/plans/preview")
    assert resp.status_code == 201, resp.text
    return resp.json()["plan"]


def _live_fp(client: TestClient) -> str:
    return client.get("/api/v1/plans/current").json()["current_input_revision"]


def test_submitted_assignment_task_retained_but_not_scheduled(client: TestClient) -> None:
    _add_course(COURSE, "PD-3 课程")
    _add_assignment("asg_pd3sub", title="已提交作业", submission_status="submitted", external_id="pd3-assign-1")
    _add_assignment("asg_pd3graded", title="已评分作业", submission_status="graded", external_id="pd3-assign-3")
    _add_assignment("asg_pd3open", title="未提交作业", submission_status="not_submitted", external_id="pd3-assign-2")
    _add_task("tsk_pd3sub", title="已提交作业任务", external_id="pd3-assign-1")
    _add_task("tsk_pd3graded", title="已评分作业任务", external_id="pd3-assign-3")
    _add_task("tsk_pd3open", title="未提交作业任务", external_id="pd3-assign-2")

    plan = _preview(client)
    blocks = {b["task_id"] for b in plan["blocks"]}
    unassigned = {u["task_id"] for u in plan["unassigned"]}
    # 不排程：既无时间块，也不出现在未安排（完全退出排程输入，不占容量不告警）
    assert "tsk_pd3sub" not in blocks and "tsk_pd3sub" not in unassigned
    assert "tsk_pd3graded" not in blocks and "tsk_pd3graded" not in unassigned
    # 未提交作业照常排程（对照组：排除仅因已提交/已评分，非因来源引用本身）
    assert "tsk_pd3open" in blocks

    # 任务行保留（裁定：不删除、可复习；弱化与标注由 C 端按作业状态展示）
    resp = client.get("/api/v1/tasks", params={"course_id": COURSE, "limit": 200})
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()["items"]}
    assert {"tsk_pd3sub", "tsk_pd3graded", "tsk_pd3open"} <= ids


def test_submission_flip_invalidates_candidate_plan(client: TestClient) -> None:
    """提交状态翻转（用户路径）→ 指纹变化 → 旧候选应用被 PLAN_STALE 拒绝；重预览即排除。"""
    _add_course(COURSE, "PD-3 课程")
    _add_assignment("asg_pd3open", title="未提交作业", submission_status="not_submitted", external_id="pd3-assign-2")
    _add_task("tsk_pd3open", title="未提交作业任务", external_id="pd3-assign-2")

    plan = _preview(client)
    assert "tsk_pd3open" in {b["task_id"] for b in plan["blocks"]}
    fp_before = _live_fp(client)
    assert plan["input_revision"] == fp_before

    patched = client.patch("/api/v1/assignments/asg_pd3open", json={"submission_status": "submitted"})
    assert patched.status_code == 200, patched.text

    assert _live_fp(client) != fp_before  # 指纹涵盖提交状态变化
    stale = client.post(f"/api/v1/plans/{plan['id']}/apply")
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "PLAN_STALE"

    fresh = _preview(client)
    assert fresh["input_revision"] == _live_fp(client)
    fresh_blocks = {b["task_id"] for b in fresh["blocks"]}
    fresh_unassigned = {u["task_id"] for u in fresh["unassigned"]}
    assert "tsk_pd3open" not in fresh_blocks and "tsk_pd3open" not in fresh_unassigned


def test_platform_sync_submission_flip_changes_fingerprint(client: TestClient, sync_env) -> None:
    """平台同步路径（真实场景）：同作业 not_submitted→submitted，revision 递增且指纹变化。"""

    def item(status: str) -> dict:
        return {
            "title": "平台同步作业",
            "due_precision": "unknown",
            "submission_status": status,
            "source_ref": {
                "platform": "testsync",
                "external_id": "testsync-assign-pd3",
                "canonical_url": "https://course.example.edu/assign/pd3",
                "observed_at": "2026-10-10T10:00:00+08:00",
            },
        }

    sync_env.result = _envelope(assignments=[item("not_submitted")])
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "succeeded"
    fp_open = _live_fp(client)

    sync_env.result = _envelope(assignments=[item("submitted")], cursor_next="cur_test_2")
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "succeeded"

    assert _live_fp(client) != fp_open
