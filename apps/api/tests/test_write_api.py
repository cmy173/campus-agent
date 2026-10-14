"""写端点测试：任务 CRUD（If-Match）、归档、锁定、反馈幂等、课程来源绑定、课程/作业创建与修正、可用时间。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from contract_support import validate_contract


def _create(client: TestClient, **overrides) -> dict:
    payload = {"title": "测试任务", "estimated_minutes": 30, **overrides}
    resp = client.post("/api/v1/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_task_minimal(client: TestClient) -> None:
    task = _create(client)
    assert task["id"].startswith("tsk_")
    assert task["status"] == "pending"
    assert task["fact_source"] == "user"
    assert task["match_key"] is None
    assert task["due_at"] is None
    assert task["due_precision"] == "unknown"
    assert task["revision"] == 1
    assert task["prerequisite_ids"] == []
    assert task["source_refs"] == []
    assert task["created_at"].endswith("Z")


def test_create_task_due_inference(client: TestClient) -> None:
    task = _create(client, due_at="2026-11-01T20:00:00+08:00")
    assert task["due_precision"] == "datetime"
    assert task["due_at"] == "2026-11-01T12:00:00Z"


def test_create_task_validation(client: TestClient) -> None:
    resp = client.post("/api/v1/tasks", json={"title": "x", "estimated_minutes": 30, "due_precision": "date"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    resp2 = client.post(
        "/api/v1/tasks",
        json={"title": "x", "estimated_minutes": 30, "due_at": "2026-11-01T00:00:00Z", "due_precision": "unknown"},
    )
    assert resp2.status_code == 400

    resp3 = client.post("/api/v1/tasks", json={"title": "x", "estimated_minutes": 30, "course_id": "crs_nope"})
    assert resp3.status_code == 404

    resp4 = client.post(
        "/api/v1/tasks", json={"title": "x", "estimated_minutes": 30, "prerequisite_ids": ["tsk_nope"]}
    )
    assert resp4.status_code == 400
    assert resp4.json()["error"]["details"]["missing_prerequisite_ids"] == ["tsk_nope"]


def test_patch_task_if_match(client: TestClient) -> None:
    task = _create(client, title="待改任务")
    tid = task["id"]

    conflict = client.patch(f"/api/v1/tasks/{tid}", json={"title": "改了"}, headers={"If-Match": "999"})
    assert conflict.status_code == 412
    assert conflict.json()["error"]["code"] == "REVISION_MISMATCH"

    ok = client.patch(f"/api/v1/tasks/{tid}", json={"title": "改了", "status": "in_progress"}, headers={"If-Match": '"1"'})
    assert ok.status_code == 200
    body = ok.json()
    assert body["title"] == "改了"
    assert body["status"] == "in_progress"
    assert body["revision"] == 2

    stale = client.patch(f"/api/v1/tasks/{tid}", json={"title": "再改"}, headers={"If-Match": "1"})
    assert stale.status_code == 412

    cleared = client.patch(f"/api/v1/tasks/{tid}", json={"due_at": None}).json()
    assert cleared["due_at"] is None
    assert cleared["due_precision"] == "unknown"


def test_patch_task_due_pair(client: TestClient) -> None:
    task = _create(client, title="截止校验", due_at="2026-11-02T10:00:00+08:00")
    tid = task["id"]
    resp = client.patch(f"/api/v1/tasks/{tid}", json={"due_at": None, "due_precision": "datetime"})
    assert resp.status_code == 400


def test_delete_task_affects_listing(client: TestClient) -> None:
    task = _create(client, title="待归档任务")
    tid = task["id"]
    assert client.delete(f"/api/v1/tasks/{tid}").status_code == 204

    default = client.get("/api/v1/tasks").json()["items"]
    assert all(t["id"] != tid for t in default)
    with_archived = client.get("/api/v1/tasks", params={"include_archived": True}).json()["items"]
    archived = next(t for t in with_archived if t["id"] == tid)
    assert archived["archived_at"].endswith("Z")
    assert archived["revision"] == 2

    assert client.delete(f"/api/v1/tasks/{tid}").status_code == 204


def test_lock_task_in_active_plan(client: TestClient) -> None:
    # 不假设具体是哪个生效计划（计划引擎接入后会被预览/应用替换）：从当前计划中动态取块
    current = client.get("/api/v1/plans/current").json()["plan"]
    block = current["blocks"][0]
    task_id = block["task_id"]

    resp = client.post(f"/api/v1/tasks/{task_id}/lock", json={"locked": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["id"] == task_id
    plan = body["plan"]
    assert plan["status"] == "active"
    mine = [b for b in plan["blocks"] if b["task_id"] == task_id]
    assert mine and all(b["locked"] is True for b in mine)

    unlock = client.post(
        f"/api/v1/tasks/{task_id}/lock",
        json={"locked": False, "starts_at": block["starts_at"]},
    )
    assert unlock.status_code == 200
    target = next(b for b in unlock.json()["plan"]["blocks"] if b["task_id"] == task_id and b["starts_at"] == block["starts_at"])
    assert target["locked"] is False

    missing_block = client.post(
        f"/api/v1/tasks/{task_id}/lock",
        json={"locked": True, "starts_at": "2030-01-01T00:00:00Z"},
    )
    assert missing_block.status_code == 404

    # 已归档且已取消的样例任务不会出现在任何新计划的块里
    not_in_plan = client.post("/api/v1/tasks/tsk_5a2b63/lock", json={"locked": True})
    assert not_in_plan.status_code == 404


def test_feedback_completes_task_and_idempotent_replay(client: TestClient) -> None:
    task = _create(client, title="反馈任务")
    tid = task["id"]
    headers = {"Idempotency-Key": "test-feedback-key-0001"}
    body = {"outcome": "completed", "actual_minutes": 25, "difficulty": 3, "note": "还行"}

    first = client.post(f"/api/v1/tasks/{tid}/feedback", json=body, headers=headers)
    assert first.status_code == 201
    data = first.json()
    assert data["feedback"]["outcome"] == "completed"
    assert data["task"]["status"] == "completed"
    assert data["task"]["revision"] == 2
    assert data["replan_suggested"] is True
    assert data["current_input_revision"].startswith("fp_")

    replay = client.post(f"/api/v1/tasks/{tid}/feedback", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == data

    reused = client.post(f"/api/v1/tasks/{tid}/feedback", json={**body, "outcome": "partial"}, headers=headers)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    short_key = client.post(f"/api/v1/tasks/{tid}/feedback", json=body, headers={"Idempotency-Key": "short"})
    assert short_key.status_code == 400


def test_feedback_conflicts(client: TestClient) -> None:
    cancelled = client.post("/api/v1/tasks/tsk_5a2b63/feedback", json={"outcome": "completed"})
    assert cancelled.status_code == 409
    assert cancelled.json()["error"]["code"] == "CONFLICT"

    task = _create(client, title="归档后反馈")
    client.delete(f"/api/v1/tasks/{task['id']}")
    archived = client.post(f"/api/v1/tasks/{task['id']}/feedback", json={"outcome": "partial"})
    assert archived.status_code == 409


def test_add_course_source_ref(client: TestClient) -> None:
    new_ref = {
        "source_ref": {
            "platform": "manual",
            "external_id": "manual-course-0001",
            "canonical_url": None,
            "observed_at": "2026-10-09T12:00:00Z",
        }
    }
    resp = client.post("/api/v1/courses/crs_51c9d0/source-refs", json=new_ref)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_refs"][-1]["platform"] == "manual"

    again = client.post("/api/v1/courses/crs_51c9d0/source-refs", json=new_ref)
    assert again.status_code == 200
    assert len(again.json()["source_refs"]) == len(body["source_refs"])

    conflict = client.post(
        "/api/v1/courses/crs_51c9d0/source-refs",
        json={"source_ref": {"platform": "xzzd", "external_id": "xzzd-course-10001", "observed_at": "2026-10-09T12:00:00Z"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"
    assert conflict.json()["error"]["details"]["bound_course_id"] == "crs_3f8a21"

    missing_course = client.post("/api/v1/courses/crs_nope/source-refs", json=new_ref)
    assert missing_course.status_code == 404


# ────────────────────────── 课程/作业创建与修正（C2 偏差整改） ──────────────────────────


def test_create_course(client: TestClient) -> None:
    resp = client.post("/api/v1/courses", json={"title": "手动创建课程", "term": "2026 秋", "teacher": "王老师"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    validate_contract(body, "course.schema.json")
    assert body["id"].startswith("crs_")
    assert body["title"] == "手动创建课程"
    assert body["term"] == "2026 秋" and body["teacher"] == "王老师"
    assert body["source_refs"] == []
    assert body["archived_at"] is None

    minimal = client.post("/api/v1/courses", json={"title": "最小课程"})
    assert minimal.status_code == 201
    assert minimal.json()["term"] is None and minimal.json()["teacher"] is None

    bad = client.post("/api/v1/courses", json={"title": "x", "extra": 1})
    assert bad.status_code == 400
    error = bad.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert any(f["field"] == "extra" for f in error["details"]["fields"])


def test_validation_failed_fields_shape(client: TestClient) -> None:
    resp = client.post("/api/v1/courses", json={})
    assert resp.status_code == 400
    fields = resp.json()["error"]["details"]["fields"]
    assert fields[0]["field"] == "title"
    assert fields[0]["reason"]


def test_create_assignment(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/assignments",
        json={
            "course_id": "crs_3f8a21",
            "title": "手动作业",
            "instructions": "见附件",
            "due_at": "2026-11-05T23:59:00+08:00",
            "due_precision": "datetime",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    validate_contract(body, "assignment.schema.json")
    assert body["id"].startswith("asg_")
    assert body["fact_source"] == "user"
    assert body["revision"] == 1
    assert body["due_at"] == "2026-11-05T15:59:00Z"
    assert body["due_precision"] == "datetime"
    assert body["source_ref"] is None
    assert body["submission_status"] == "unknown"


def test_create_assignment_validation(client: TestClient) -> None:
    missing_course = client.post("/api/v1/assignments", json={"course_id": "crs_nope", "title": "x"})
    assert missing_course.status_code == 404

    no_precision = client.post(
        "/api/v1/assignments",
        json={"course_id": "crs_3f8a21", "title": "x", "due_at": "2026-11-05T23:59:00+08:00"},
    )
    assert no_precision.status_code == 400
    assert no_precision.json()["error"]["code"] == "VALIDATION_FAILED"

    bad_pair = client.post(
        "/api/v1/assignments",
        json={"course_id": "crs_3f8a21", "title": "x", "due_at": "2026-11-05T23:59:00+08:00", "due_precision": "unknown"},
    )
    assert bad_pair.status_code == 400

    missing_due = client.post(
        "/api/v1/assignments",
        json={"course_id": "crs_3f8a21", "title": "x", "due_precision": "datetime"},
    )
    assert missing_due.status_code == 400

    no_due = client.post("/api/v1/assignments", json={"course_id": "crs_3f8a21", "title": "无截止作业"})
    assert no_due.status_code == 201
    assert no_due.json()["due_precision"] == "unknown"
    assert no_due.json()["due_at"] is None


def test_patch_assignment_partial_and_if_match(client: TestClient) -> None:
    created = client.post("/api/v1/assignments", json={"course_id": "crs_3f8a21", "title": "待修正作业"}).json()
    aid = created["id"]

    conflict = client.patch(f"/api/v1/assignments/{aid}", json={"title": "改"}, headers={"If-Match": "9"})
    assert conflict.status_code == 412
    error = conflict.json()["error"]
    assert error["code"] == "REVISION_MISMATCH"
    assert error["details"] == {"target": aid, "expected_revision": 9, "current_revision": 1}

    ok = client.patch(
        f"/api/v1/assignments/{aid}",
        json={"title": "修正标题", "submission_status": "submitted"},
        headers={"If-Match": '"1"'},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["title"] == "修正标题"
    assert body["submission_status"] == "submitted"
    assert body["revision"] == 2
    assert body["fact_source"] == "user"

    set_instr = client.patch(f"/api/v1/assignments/{aid}", json={"instructions": "先写这个"})
    assert set_instr.status_code == 200 and set_instr.json()["instructions"] == "先写这个"
    cleared = client.patch(f"/api/v1/assignments/{aid}", json={"instructions": None})
    assert cleared.status_code == 200 and cleared.json()["instructions"] is None

    null_title = client.patch(f"/api/v1/assignments/{aid}", json={"title": None})
    assert null_title.status_code == 400
    null_precision = client.patch(f"/api/v1/assignments/{aid}", json={"due_precision": None})
    assert null_precision.status_code == 400


def test_patch_assignment_due_pair(client: TestClient) -> None:
    created = client.post(
        "/api/v1/assignments",
        json={"course_id": "crs_3f8a21", "title": "截止修正", "due_at": "2026-12-01T20:00:00+08:00", "due_precision": "datetime"},
    ).json()
    aid = created["id"]

    implicit = client.patch(f"/api/v1/assignments/{aid}", json={"due_at": "2026-12-02T20:00:00+08:00"})
    assert implicit.status_code == 400  # 有值必须显式 precision（不猜测）

    explicit = client.patch(
        f"/api/v1/assignments/{aid}",
        json={"due_at": "2026-12-02T20:00:00+08:00", "due_precision": "datetime"},
    )
    assert explicit.status_code == 200
    assert explicit.json()["due_at"] == "2026-12-02T12:00:00Z"

    cleared = client.patch(f"/api/v1/assignments/{aid}", json={"due_at": None})
    assert cleared.status_code == 200
    assert cleared.json()["due_at"] is None and cleared.json()["due_precision"] == "unknown"

    unknown_with_due = client.patch(
        f"/api/v1/assignments/{aid}",
        json={"due_at": "2026-12-03T20:00:00+08:00", "due_precision": "unknown"},
    )
    assert unknown_with_due.status_code == 400

    precision_without_due = client.patch(
        f"/api/v1/assignments/{aid}", json={"due_at": None, "due_precision": "datetime"}
    )
    assert precision_without_due.status_code == 400


# ────────────────────────── 可用时间（C2 偏差整改） ──────────────────────────

_AVAILABILITY_KEYS = ("timezone", "weekly_windows", "date_exceptions", "daily_limit_minutes")


def test_put_availability_replaces_and_roundtrips(client: TestClient) -> None:
    before = client.get("/api/v1/preferences/availability").json()

    payload = {
        "timezone": "Asia/Shanghai",
        "weekly_windows": [{"weekday": 3, "start": "08:00", "end": "09:30"}],
        "date_exceptions": [{"date": "2026-12-25", "windows": [], "note": "维护日"}],
        "daily_limit_minutes": 120,
    }
    resp = client.put("/api/v1/preferences/availability", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    validate_contract(body, "availability.schema.json")
    assert body["weekly_windows"] == payload["weekly_windows"]
    assert body["date_exceptions"] == payload["date_exceptions"]
    assert body["daily_limit_minutes"] == 120

    after = client.get("/api/v1/preferences/availability").json()
    assert after["weekly_windows"] == payload["weekly_windows"]
    assert after["daily_limit_minutes"] == 120

    restored = client.put("/api/v1/preferences/availability", json={k: before[k] for k in _AVAILABILITY_KEYS})
    assert restored.status_code == 200
    assert restored.json()["weekly_windows"] == before["weekly_windows"]


def test_put_availability_validation(client: TestClient) -> None:
    base = {"timezone": "Asia/Shanghai", "weekly_windows": [], "date_exceptions": [], "daily_limit_minutes": None}

    bad_weekday = client.put(
        "/api/v1/preferences/availability", json={**base, "weekly_windows": [{"weekday": 8, "start": "19:00", "end": "21:00"}]}
    )
    assert bad_weekday.status_code == 400

    bad_order = client.put(
        "/api/v1/preferences/availability", json={**base, "weekly_windows": [{"weekday": 1, "start": "21:00", "end": "19:00"}]}
    )
    assert bad_order.status_code == 400

    bad_time = client.put(
        "/api/v1/preferences/availability", json={**base, "weekly_windows": [{"weekday": 1, "start": "25:00", "end": "26:00"}]}
    )
    assert bad_time.status_code == 400

    missing_field = client.put(
        "/api/v1/preferences/availability",
        json={"timezone": "Asia/Shanghai", "weekly_windows": [], "date_exceptions": []},
    )
    assert missing_field.status_code == 400


def test_availability_defaults_when_unset_then_put_recreates(client: TestClient) -> None:
    from app.db import SessionLocal
    from app.models import Availability

    with SessionLocal() as db:
        row = db.get(Availability, 1)
        snapshot = {k: row.to_contract()[k] for k in _AVAILABILITY_KEYS} if row is not None else None
        if row is not None:
            db.delete(row)
            db.commit()

    try:
        default = client.get("/api/v1/preferences/availability")
        assert default.status_code == 200
        body = default.json()
        assert body["timezone"] == "Asia/Shanghai"
        assert body["weekly_windows"] == [] and body["date_exceptions"] == []
        assert body["daily_limit_minutes"] is None

        created = client.put(
            "/api/v1/preferences/availability",
            json={"timezone": "Asia/Shanghai", "weekly_windows": [], "date_exceptions": [], "daily_limit_minutes": None},
        )
        assert created.status_code == 200
        assert created.json()["daily_limit_minutes"] is None
    finally:
        if snapshot is not None:
            assert client.put("/api/v1/preferences/availability", json=snapshot).status_code == 200
