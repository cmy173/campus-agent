"""B3/B12 同步与连接检查通道测试：端点受理/去抖/状态校验 + worker 消费 sync-result 信封 + check 应用。

用例自建 testsync 平台连接与课程（不触种子数据）；夹具快照新增行并在用例后全部清理，
保持共享测试库对后续模块确定性。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import config
from app.sync_engine import AdapterError, _CHECK_REGISTRY, _REGISTRY

PLATFORM = "testsync"
CONN_ID = "conn_test01"
COURSE_ID = "crs_test01"
COURSE_ID2 = "crs_test02"
ENTRY = "https://course.example.edu/"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
PDF_BYTES = b"%PDF-1.4\n% sync attach test\n"


class FakeAdapter:
    """可编程适配器：sync result 为信封 dict、callable(connection, request) 或 Exception；check 同理。"""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict]] = []
        self.result = None
        self.check_calls: list[dict] = []
        self.check_result = None

    def __call__(self, connection, request):
        self.calls.append((connection, request))
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result(connection, request)
        return self.result

    def check(self, connection):
        self.check_calls.append(connection)
        if isinstance(self.check_result, Exception):
            raise self.check_result
        if callable(self.check_result):
            return self.check_result(connection)
        return self.check_result


def _envelope(
    course_id: str = COURSE_ID,
    *,
    resources=None,
    assignments=None,
    recordings=None,
    deletions=None,
    errors=None,
    cursor_next: str | None = "cur_test_1",
    mode: str = "incremental",
) -> dict:
    return {
        "platform": PLATFORM,
        "connection_id": CONN_ID,
        "course_id": course_id,
        "mode": mode,
        "cursor": {"previous": None, "next": cursor_next},
        "upserts": {
            "resources": resources or [],
            "assignments": assignments or [],
            "recordings": recordings or [],
        },
        "deletions": deletions or [],
        "errors": errors or [],
        "stats": {"scanned": 0, "upserted": 0, "deleted": 0, "failed": 0},
        "observed_at": "2026-10-09T10:00:00+08:00",
    }


def _resource_item(external_id: str = "testsync-file-1", *, title: str = "测试讲义.pdf", content_hash: str = HASH_A, version: int = 1) -> dict:
    return {
        "kind": "handout",
        "title": title,
        "source_ref": {
            "platform": PLATFORM,
            "external_id": external_id,
            "canonical_url": f"{ENTRY}file/{external_id}",
            "observed_at": "2026-10-09T10:00:00+08:00",
        },
        "version": version,
        "content_hash": content_hash,
        "size_bytes": 123456,
        "mime_type": "application/pdf",
        "access_level": "full",
        "source_updated_at": "2026-10-08T09:00:00+08:00",
    }


def _drain_until(job_id: str, attempts: int = 20) -> str:
    from app import worker
    from app.db import SessionLocal
    from app.models import Job

    for _ in range(attempts):
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None or job.status not in ("queued", "running"):
                return job.status if job is not None else "missing"
        assert worker.process_next_job(), "队列为空但目标任务未完成"
    raise AssertionError("任务未在预期步数内完成")


@pytest.fixture()
def sync_env(monkeypatch):
    """建 testsync 场地 + 快照清理；返回可编程假适配器（已注册）。"""
    from app.db import SessionLocal
    from app.models import (
        Assignment,
        Connection,
        Course,
        FileRecord,
        IdempotencyRecord,
        Job,
        Recording,
        Resource,
        ResourceVersion,
        SourceRef,
        SyncCursor,
        utc_now,
    )

    def snapshot(db):
        return {
            "jobs": {j.id: {c.name: getattr(j, c.name) for c in Job.__table__.columns} for j in db.execute(select(Job)).scalars()},
            "resources": set(db.execute(select(Resource.id)).scalars()),
            "versions": set(db.execute(select(ResourceVersion.resource_id, ResourceVersion.version)).all()),
            "assignments": set(db.execute(select(Assignment.id)).scalars()),
            "recordings": set(db.execute(select(Recording.id)).scalars()),
            "refs": set(
                db.execute(select(SourceRef.owner_type, SourceRef.owner_id, SourceRef.platform, SourceRef.external_id)).all()
            ),
            "cursors": set(db.execute(select(SyncCursor.connection_id, SyncCursor.course_id)).all()),
            "files": set(db.execute(select(FileRecord.id)).scalars()),
            "idem": set(db.execute(select(IdempotencyRecord.key)).scalars()),
        }

    with SessionLocal() as db:
        snap = snapshot(db)
        if db.get(Connection, CONN_ID) is None:
            db.add(Connection(id=CONN_ID, platform=PLATFORM, display_name="测试同步连接", status="ok"))
        if db.get(Course, COURSE_ID) is None:
            db.add(Course(id=COURSE_ID, title="测试同步课程"))
        course_ref = db.execute(
            select(SourceRef).where(
                SourceRef.owner_type == "course",
                SourceRef.owner_id == COURSE_ID,
                SourceRef.platform == PLATFORM,
                SourceRef.external_id == "testsync-course-1",
            )
        ).scalars().first()
        if course_ref is None:
            db.add(
                SourceRef(
                    owner_type="course",
                    owner_id=COURSE_ID,
                    platform=PLATFORM,
                    external_id="testsync-course-1",
                    canonical_url=f"{ENTRY}course/1",
                    observed_at=utc_now(),
                )
            )
        db.commit()

    adapter = FakeAdapter()
    monkeypatch.setitem(_REGISTRY, PLATFORM, adapter)
    monkeypatch.setitem(_CHECK_REGISTRY, PLATFORM, adapter.check)
    yield adapter

    with SessionLocal() as db:
        for job in db.execute(select(Job)).scalars():
            if job.id in snap["jobs"]:
                for col, value in snap["jobs"][job.id].items():
                    setattr(job, col, value)
            else:
                db.delete(job)
        for row in db.execute(select(Resource)).scalars():
            if row.id not in snap["resources"]:
                db.delete(row)
        for row in db.execute(select(ResourceVersion)).scalars():
            if (row.resource_id, row.version) not in snap["versions"]:
                db.delete(row)
        for row in db.execute(select(Assignment)).scalars():
            if row.id not in snap["assignments"]:
                db.delete(row)
        for row in db.execute(select(Recording)).scalars():
            if row.id not in snap["recordings"]:
                db.delete(row)
        for row in db.execute(select(SourceRef)).scalars():
            if (row.owner_type, row.owner_id, row.platform, row.external_id) not in snap["refs"]:
                db.delete(row)
        for row in db.execute(select(SyncCursor)).scalars():
            if (row.connection_id, row.course_id) not in snap["cursors"]:
                db.delete(row)
        for row in db.execute(select(FileRecord)).scalars():
            if row.id not in snap["files"]:
                db.delete(row)
        for row in db.execute(select(IdempotencyRecord)).scalars():
            if row.key not in snap["idem"]:
                db.delete(row)
        conn = db.get(Connection, CONN_ID)
        if conn is not None:
            db.delete(conn)
        for cid in (COURSE_ID, COURSE_ID2):
            course = db.get(Course, cid)
            if course is not None:
                db.delete(course)
        db.commit()


def _post_sync(client: TestClient, *, body: dict | None = None, key: str | None = None, conn: str = CONN_ID):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/v1/connections/{conn}/sync", json=body or {}, headers=headers)


def _post_check(client: TestClient, *, conn: str = CONN_ID):
    return client.post(f"/api/v1/connections/{conn}/check")


def _state(
    state: str,
    *,
    platform: str = PLATFORM,
    checked_at: str = "2026-10-09T10:00:00+08:00",
    action: dict | None = None,
    detail: str | None = None,
) -> dict:
    return {"platform": platform, "state": state, "checked_at": checked_at, "action": action, "detail": detail}


def test_sync_endpoint_accepts_and_worker_applies(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.result = _envelope(resources=[_resource_item()])
    resp = _post_sync(client)
    assert resp.status_code == 202, resp.text
    job = resp.json()["job"]
    assert job["type"] == "sync" and job["status"] == "queued" and job["progress"] == 0.0

    assert _drain_until(job["id"]) == "succeeded"
    detail = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert detail["status"] == "succeeded" and detail["progress"] == 1.0
    refs = detail["result_refs"]
    assert refs and refs[0]["type"] == "resource"

    resource = client.get(f"/api/v1/resources/{refs[0]['id']}").json()
    assert resource["course_id"] == COURSE_ID
    assert resource["version"] == 1 and resource["content_hash"] == HASH_A
    assert resource["parse_status"] == "pending" and resource["file_id"] is None
    assert resource["source_refs"][0]["external_id"] == "testsync-file-1"

    versions = client.get(f"/api/v1/resources/{refs[0]['id']}/versions").json()["items"]
    assert [v["version"] for v in versions] == [1]

    conn = client.get(f"/api/v1/connections/{CONN_ID}").json()
    assert conn["status"] == "ok" and conn["last_sync_at"] is not None

    from app.db import SessionLocal
    from app.models import SyncCursor

    with SessionLocal() as db:
        cursor = db.get(SyncCursor, (CONN_ID, COURSE_ID))
        assert cursor is not None and cursor.cursor == "cur_test_1" and cursor.mode == "incremental"
    # 适配器收到的触发参数形状（B→A 边界）
    call_conn, call_req = sync_env.calls[0]
    assert call_conn == {"platform": PLATFORM, "connection_id": CONN_ID}
    assert call_req == {"course_id": COURSE_ID, "since_cursor": None, "full": False}


def test_sync_envelope_errors_are_isolated(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.result = _envelope(
        resources=[_resource_item()],
        errors=[
            {
                "code": "PERMISSION_DENIED",
                "message": "无法获取该资源的元数据",
                "retryable": False,
                "details": {"kind": "resource", "external_id": "testsync-file-9"},
                "request_id": "req_upstream_1",
            }
        ],
    )
    resp = _post_sync(client)
    job_id = resp.json()["job"]["id"]
    assert _drain_until(job_id) == "partial_failed"
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert len(detail["errors"]) == 1
    entry = detail["errors"][0]
    assert entry["code"] == "PERMISSION_DENIED" and entry["retryable"] is False
    assert "request_id" not in entry  # ErrorInfo 不含信封专有字段
    assert len(detail["result_refs"]) == 1  # 单项失败不抹除有效结果


def test_sync_idempotent_replay_and_active_dedupe(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.result = _envelope(resources=[_resource_item()])
    first = _post_sync(client, key="sync-key-0001")
    assert first.status_code == 202
    job_id = first.json()["job"]["id"]

    replay = _post_sync(client, key="sync-key-0001")
    assert replay.status_code == 202 and replay.json()["job"]["id"] == job_id

    dedupe = _post_sync(client)  # 无幂等键，活跃任务去抖
    assert dedupe.status_code == 202 and dedupe.json()["job"]["id"] == job_id

    other_key = _post_sync(client, body={"full": True}, key="sync-key-0002")
    assert other_key.status_code == 202 and other_key.json()["job"]["id"] == job_id

    reuse = _post_sync(client, body={"full": True}, key="sync-key-0001")
    assert reuse.status_code == 409
    assert reuse.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_sync_disabled_and_auth_required_conflicts(client: TestClient, sync_env: FakeAdapter) -> None:
    from app.db import SessionLocal
    from app.models import Connection

    with SessionLocal() as db:
        conn = db.get(Connection, CONN_ID)
        conn.status = "disabled"
        db.commit()
    resp = _post_sync(client)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"

    with SessionLocal() as db:
        conn = db.get(Connection, CONN_ID)
        conn.status = "auth_required"
        conn.recovery_action = {"type": "user_login", "entry_url": ENTRY, "message": "请重新登录"}
        db.commit()
    resp = _post_sync(client)
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "AUTH_EXPIRED" and error["retryable"] is False
    assert error["details"]["action"]["entry_url"] == ENTRY


def test_sync_adapter_missing_is_dependency_error(client: TestClient, sync_env: FakeAdapter, monkeypatch) -> None:
    monkeypatch.delitem(_REGISTRY, PLATFORM, raising=False)
    resp = _post_sync(client)
    assert resp.status_code == 202
    job_id = resp.json()["job"]["id"]
    assert _drain_until(job_id) == "failed"
    error = client.get(f"/api/v1/jobs/{job_id}").json()["errors"][0]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE" and error["retryable"] is False


def test_sync_auth_expired_marks_connection(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.result = AdapterError(
        "AUTH_EXPIRED",
        "登录已失效",
        retryable=False,
        action={"type": "user_login", "entry_url": ENTRY, "message": "请在浏览器中重新登录"},
    )
    resp = _post_sync(client)
    job_id = resp.json()["job"]["id"]
    assert _drain_until(job_id) == "failed"
    error = client.get(f"/api/v1/jobs/{job_id}").json()["errors"][0]
    assert error["code"] == "AUTH_EXPIRED" and error["retryable"] is False
    assert error["details"]["action"]["entry_url"] == ENTRY

    conn = client.get(f"/api/v1/connections/{CONN_ID}").json()
    assert conn["status"] == "auth_required"
    assert conn["recovery_action"]["entry_url"] == ENTRY
    # 失效后再触发 → 409 AUTH_EXPIRED（不进入任务）
    again = _post_sync(client)
    assert again.status_code == 409 and again.json()["error"]["code"] == "AUTH_EXPIRED"


def test_sync_version_bump_on_content_change(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.result = _envelope(resources=[_resource_item()])
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "succeeded"
    rid = client.get(f"/api/v1/jobs/{job_id}").json()["result_refs"][0]["id"]

    sync_env.result = _envelope(resources=[_resource_item(content_hash=HASH_B)], cursor_next="cur_test_2")
    job2 = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job2) == "succeeded"
    resource = client.get(f"/api/v1/resources/{rid}").json()
    assert resource["version"] == 2 and resource["content_hash"] == HASH_B
    versions = client.get(f"/api/v1/resources/{rid}/versions").json()["items"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["content_hash"] == HASH_B and versions[0]["file_id"] is None

    sync_env.result = _envelope(resources=[_resource_item(content_hash=HASH_B)], cursor_next="cur_test_3")
    job3 = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job3) == "succeeded"
    assert client.get(f"/api/v1/resources/{rid}").json()["version"] == 2  # 同指纹不产生新版本

    from app.db import SessionLocal
    from app.models import SyncCursor

    with SessionLocal() as db:
        assert db.get(SyncCursor, (CONN_ID, COURSE_ID)).cursor == "cur_test_3"
    # 第二次同步携带上次游标
    assert sync_env.calls[1][1]["since_cursor"] == "cur_test_1"
    assert sync_env.calls[2][1]["since_cursor"] == "cur_test_2"


def test_sync_deletion_requires_explicit_signal(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.result = _envelope(resources=[_resource_item("testsync-file-1"), _resource_item("testsync-file-2", title="第二份.pdf")])
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "succeeded"
    refs = client.get(f"/api/v1/jobs/{job_id}").json()["result_refs"]
    by_ext = {}
    for ref in refs:
        detail = client.get(f"/api/v1/resources/{ref['id']}").json()
        by_ext[detail["source_refs"][0]["external_id"]] = detail["id"]
    rid1, rid2 = by_ext["testsync-file-1"], by_ext["testsync-file-2"]

    sync_env.result = _envelope(
        deletions=[
            {"kind": "resource", "external_id": "testsync-file-1", "reason": "explicit_delete_signal", "observed_at": "2026-10-09T11:00:00+08:00"},
            {"kind": "resource", "external_id": "testsync-file-2", "reason": "not_seen", "observed_at": "2026-10-09T11:00:00+08:00"},
            {"kind": "resource", "external_id": "testsync-file-404", "reason": "explicit_delete_signal", "observed_at": "2026-10-09T11:00:00+08:00"},
        ],
        cursor_next="cur_test_2",
    )
    job2 = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job2) == "succeeded"

    gone = client.get(f"/api/v1/resources/{rid1}").json()
    alive = client.get(f"/api/v1/resources/{rid2}").json()
    assert gone["archived_at"] is not None
    assert alive["archived_at"] is None  # 非显式信号绝不产生删除


def test_sync_upload_attaches_resource_version(client: TestClient, sync_env: FakeAdapter, tmp_path, monkeypatch) -> None:
    """A9：适配器经 POST /imports 交付文件 → 按 source_ref 挂到同步资源（新版本）。"""
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    sync_env.result = _envelope(resources=[_resource_item()])
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "succeeded"
    rid = client.get(f"/api/v1/jobs/{job_id}").json()["result_refs"][0]["id"]

    payload = {
        "course_id": COURSE_ID,
        "kind": "handout",
        "title": "测试讲义",
        "source_ref": {
            "platform": PLATFORM,
            "external_id": "testsync-file-1",
            "canonical_url": f"{ENTRY}file/testsync-file-1",
            "observed_at": "2026-10-09T11:30:00+08:00",
        },
    }
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps(payload)},
        files=[("files", ("讲义.pdf", PDF_BYTES, "application/pdf"))],
    )
    assert resp.status_code == 202, resp.text
    import_job = resp.json()["job"]["id"]
    assert _drain_until(import_job) == "succeeded"

    resource = client.get(f"/api/v1/resources/{rid}").json()
    assert resource["version"] == 2 and resource["file_id"] is not None
    assert resource["source_refs"][0]["external_id"] == "testsync-file-1"
    versions = client.get(f"/api/v1/resources/{rid}/versions").json()["items"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["file_id"] == resource["file_id"]

    again = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps(payload)},
        files=[("files", ("讲义v2.pdf", PDF_BYTES, "application/pdf"))],
    )
    assert again.status_code == 202
    assert _drain_until(again.json()["job"]["id"]) == "succeeded"
    assert client.get(f"/api/v1/resources/{rid}").json()["version"] == 2  # 同内容不重复计版本


def test_sync_course_level_isolation(client: TestClient, sync_env: FakeAdapter) -> None:
    from app.db import SessionLocal
    from app.models import Course, SourceRef, utc_now

    with SessionLocal() as db:
        if db.get(Course, COURSE_ID2) is None:
            db.add(Course(id=COURSE_ID2, title="测试同步课程二"))
        db.add(
            SourceRef(
                owner_type="course", owner_id=COURSE_ID2, platform=PLATFORM,
                external_id="testsync-course-2", canonical_url=f"{ENTRY}course/2", observed_at=utc_now(),
            )
        )
        db.commit()

    def result_for(_conn, request):
        if request["course_id"] == COURSE_ID2:
            raise AdapterError("PERMISSION_DENIED", "无权访问该课程", retryable=False)
        return _envelope(course_id=COURSE_ID, resources=[_resource_item()])

    sync_env.result = result_for
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "partial_failed"
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert [e["code"] for e in detail["errors"]] == ["PERMISSION_DENIED"]
    assert detail["errors"][0]["details"]["course_id"] == COURSE_ID2
    assert len(detail["result_refs"]) == 1  # 失败的课程不抹除成功课程结果


def test_sync_retry_only_failed_courses(client: TestClient, sync_env: FakeAdapter) -> None:
    """B4 重试：partial_failed 同步任务以失败课程为作用域重发新任务，原任务不动。"""
    from app.db import SessionLocal
    from app.models import Course, Job, SourceRef, utc_now

    with SessionLocal() as db:
        if db.get(Course, COURSE_ID2) is None:
            db.add(Course(id=COURSE_ID2, title="测试同步课程二"))
        db.add(
            SourceRef(
                owner_type="course", owner_id=COURSE_ID2, platform=PLATFORM,
                external_id="testsync-course-2", canonical_url=f"{ENTRY}course/2", observed_at=utc_now(),
            )
        )
        db.commit()

    def result_for(_conn, request):
        if request["course_id"] == COURSE_ID2:
            raise AdapterError("PERMISSION_DENIED", "无权访问该课程", retryable=False)
        return _envelope(course_id=COURSE_ID, resources=[_resource_item()])

    sync_env.result = result_for
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "partial_failed"

    retry = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry.status_code == 202, retry.text
    new_job = retry.json()["job"]
    assert new_job["type"] == "sync" and new_job["status"] == "queued"

    with SessionLocal() as db:
        assert db.get(Job, new_job["id"]).payload["course_ids"] == [COURSE_ID2]
        original = db.get(Job, job_id)
        assert original.status == "partial_failed" and len(original.result_refs) == 1  # 原任务不被修改

    # 重试任务只访问失败课程
    before = len(sync_env.calls)
    assert _drain_until(new_job["id"]) == "failed"  # 该课程持续无权限
    assert [c[1]["course_id"] for c in sync_env.calls[before:]] == [COURSE_ID2]
    error = client.get(f"/api/v1/jobs/{new_job['id']}").json()["errors"][0]
    assert error["code"] == "PERMISSION_DENIED"

    # 失败任务可再次重试（链路闭合）
    assert client.post(f"/api/v1/jobs/{new_job['id']}/retry").status_code == 202


def test_resolve_module_spec_function_and_protocol_class(tmp_path, monkeypatch) -> None:
    """STUDY_SYNC_ADAPTERS 两种 entry 形态：B 原生函数 与 A3 协议类（桥接为统一调用面）。"""
    (tmp_path / "temp_adapter_ok.py").write_text(
        """
def sync_course(connection, request):
    return {"kind": "native", "connection": connection, "request": request}


class ProtocolAdapter:
    def __init__(self, *, platform, connection_id):
        self.platform = platform
        self.connection_id = connection_id

    def sync_course(self, course_id, since_cursor=None, kinds=None):
        return {"kind": "protocol", "course_id": course_id, "since_cursor": since_cursor, "conn": self.connection_id}
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        config, "SYNC_ADAPTERS", {"fnplat": "temp_adapter_ok:sync_course", "clsplat": "temp_adapter_ok:ProtocolAdapter"}
    )
    from app.sync_engine import resolve_sync_adapter

    native = resolve_sync_adapter("fnplat")
    out = native({"platform": "fnplat", "connection_id": "conn_x"}, {"course_id": "crs_x", "since_cursor": None, "full": False})
    assert out["kind"] == "native" and out["connection"]["connection_id"] == "conn_x"

    bridged = resolve_sync_adapter("clsplat")
    out2 = bridged({"platform": "clsplat", "connection_id": "conn_y"}, {"course_id": "crs_y", "since_cursor": "cur_z", "full": False})
    assert out2 == {"kind": "protocol", "course_id": "crs_y", "since_cursor": "cur_z", "conn": "conn_y"}


def test_protocol_adapter_error_is_translated(tmp_path, monkeypatch) -> None:
    """A3 协议错误（.error 字典）转译为 B 侧 AdapterError（含 recovery action）。"""
    (tmp_path / "temp_adapter_err.py").write_text(
        """
class _ProtocolError(Exception):
    def __init__(self):
        super().__init__("AUTH_EXPIRED")
        self.error = {
            "code": "AUTH_EXPIRED",
            "message": "登录已失效",
            "retryable": False,
            "details": {
                "platform": "remote",
                "action": {"type": "user_login", "entry_url": "https://entry.example.edu/", "message": "请重新登录"},
            },
        }


class ProtocolAdapter:
    def __init__(self, *, platform, connection_id):
        pass

    def sync_course(self, course_id, since_cursor=None, kinds=None):
        raise _ProtocolError()
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(config, "SYNC_ADAPTERS", {"errplat": "temp_adapter_err:ProtocolAdapter"})
    from app.sync_engine import AdapterError, resolve_sync_adapter

    fn = resolve_sync_adapter("errplat")
    with pytest.raises(AdapterError) as excinfo:
        fn({"platform": "errplat", "connection_id": "conn_e"}, {"course_id": "crs_e", "since_cursor": None, "full": False})
    assert excinfo.value.code == "AUTH_EXPIRED"
    assert excinfo.value.retryable is False
    assert excinfo.value.action["entry_url"] == "https://entry.example.edu/"


# ────────────────────── B12 连接检查（CR001 降级口径） ──────────────────────


def test_check_applies_ok_state(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.check_result = _state("ok")
    resp = _post_check(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["last_error"] is None and body["recovery_action"] is None
    assert body["last_checked_at"] == "2026-10-09T02:00:00Z"  # +08:00 归一为 UTC
    assert body["entry_url"] is None  # testsync 未收录平台入口常量
    assert sync_env.check_calls == [{"platform": PLATFORM, "connection_id": CONN_ID}]


def test_check_auth_required_sets_recovery(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.check_result = _state(
        "auth_required",
        action={"type": "user_login", "entry_url": "https://login.example.edu/", "message": "请重新登录"},
        detail="HTTP 401（登录态失效）",
    )
    body = _post_check(client).json()
    assert body["status"] == "auth_required"
    assert body["recovery_action"]["entry_url"] == "https://login.example.edu/"
    error = body["last_error"]
    assert error["code"] == "AUTH_EXPIRED" and error["retryable"] is False
    assert error["message"] == "HTTP 401（登录态失效）"
    assert error["details"]["action"]["entry_url"] == "https://login.example.edu/"


def test_check_auth_required_without_action_keeps_no_fake_recovery(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.check_result = _state("auth_required")
    body = _post_check(client).json()
    assert body["status"] == "auth_required"
    assert body["recovery_action"] is None  # testsync 无入口常量，不编造 action
    assert body["last_error"]["retryable"] is False


def test_check_adapter_error_marks_connection(client: TestClient, sync_env: FakeAdapter) -> None:
    sync_env.check_result = AdapterError("NETWORK_ERROR", "上游不可达", retryable=True)
    body = _post_check(client).json()
    assert body["status"] == "network_error" and body["recovery_action"] is None
    assert body["last_error"]["code"] == "NETWORK_ERROR" and body["last_error"]["retryable"] is True

    sync_env.check_result = AdapterError(
        "AUTH_EXPIRED",
        "登录已失效",
        retryable=False,
        action={"type": "user_login", "entry_url": "https://login.example.edu/", "message": "请重新登录"},
    )
    body2 = _post_check(client).json()
    assert body2["status"] == "auth_required"
    assert body2["recovery_action"]["entry_url"] == "https://login.example.edu/"


def test_check_invalid_result_is_internal_error_and_state_kept(client: TestClient, sync_env: FakeAdapter) -> None:
    bads = [
        None,
        {"platform": PLATFORM, "state": "ok"},  # 缺 checked_at
        _state("ok", platform="other"),
        _state("weird"),
        _state("ok", checked_at="not-a-date"),
    ]
    for bad in bads:
        sync_env.check_result = bad
        resp = _post_check(client)
        assert resp.status_code == 500, f"{bad!r} -> {resp.status_code}"
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert client.get(f"/api/v1/connections/{CONN_ID}").json()["status"] == "ok"  # 非法结果不改状态


def test_check_disabled_conflict(client: TestClient, sync_env: FakeAdapter) -> None:
    from app.db import SessionLocal
    from app.models import Connection

    with SessionLocal() as db:
        db.get(Connection, CONN_ID).status = "disabled"
        db.commit()
    resp = _post_check(client)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert sync_env.check_calls == []  # 未触发适配器


def test_check_adapter_missing_is_dependency_error(client: TestClient, sync_env: FakeAdapter, monkeypatch) -> None:
    monkeypatch.delitem(_CHECK_REGISTRY, PLATFORM)
    resp = _post_check(client)
    assert resp.status_code == 200
    assert resp.json()["status"] == "service_unavailable"
    error = resp.json()["last_error"]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["retryable"] is False
    assert error["details"]["platform"] == PLATFORM


def test_check_404_unknown_connection(client: TestClient, sync_env: FakeAdapter) -> None:
    resp = _post_check(client, conn="conn_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_resolve_check_adapter_module_spec(tmp_path, monkeypatch) -> None:
    """检查面模块加载：类桥接 check_connection；spec 指名入口不可作检查面时退回模块级函数。"""
    (tmp_path / "temp_check_adapter.py").write_text(
        """
class ProtocolAdapter:
    def __init__(self, *, platform, connection_id):
        self.platform = platform
        self.connection_id = connection_id

    def check_connection(self):
        return {"via": "class", "conn": self.connection_id}


def check_connection(connection):
    return {"via": "module_fn", "conn": connection["connection_id"]}
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        config,
        "SYNC_ADAPTERS",
        {"cplat": "temp_check_adapter:ProtocolAdapter", "fplat": "temp_check_adapter:sync_course"},
    )
    from app.sync_engine import resolve_check_adapter

    bridged = resolve_check_adapter("cplat")
    assert bridged({"platform": "cplat", "connection_id": "conn_y"}) == {"via": "class", "conn": "conn_y"}

    fallback = resolve_check_adapter("fplat")
    assert fallback({"platform": "fplat", "connection_id": "conn_z"}) == {"via": "module_fn", "conn": "conn_z"}
