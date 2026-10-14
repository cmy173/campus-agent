"""worker 与 job 状态机测试：端到端导入、部分失败、协作式取消、崩溃恢复、未知类型、重放幂等。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import worker

PDF_BYTES = b"%PDF-1.4\n% worker test\n"
COURSE_ID = "crs_3f8a21"


@pytest.fixture()
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    return tmp_path / "files"


@pytest.fixture(autouse=True)
def _worker_isolation():
    """共享会话库隔离：快照既有 job，用例后清理新增行并还原被触动的既有 job。"""
    from app.db import SessionLocal
    from app.models import ContentChunk, FileRecord, IdempotencyRecord, Job, Resource, ResourceVersion, SourceRef

    with SessionLocal() as db:
        jobs_before = {
            j.id: {
                "status": j.status,
                "progress": j.progress,
                "errors": list(j.errors or []),
                "result_refs": list(j.result_refs or []),
                "cancel_requested": j.cancel_requested,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
            }
            for j in db.execute(select(Job)).scalars()
        }
        resources_before = set(db.execute(select(Resource.id)).scalars())

    yield

    with SessionLocal() as db:
        for job_id in [j.id for j in db.execute(select(Job)).scalars() if j.id not in jobs_before]:
            db.delete(db.get(Job, job_id))
        for job_id, snap in jobs_before.items():
            row = db.get(Job, job_id)
            if row is None:
                continue
            for key, value in snap.items():
                setattr(row, key, value)
        for res_id in [r for r in db.execute(select(Resource.id)).scalars() if r not in resources_before]:
            db.execute(delete(ContentChunk).where(ContentChunk.resource_id == res_id))
            db.execute(delete(ResourceVersion).where(ResourceVersion.resource_id == res_id))
            db.execute(delete(SourceRef).where(SourceRef.owner_type == "resource", SourceRef.owner_id == res_id))
            db.delete(db.get(Resource, res_id))
        db.execute(delete(FileRecord))
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.endpoint == "POST /imports"))
        db.commit()


def _upload(client: TestClient, *, name: str = "worker.pdf", title: str = "worker 讲义") -> dict:
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": COURSE_ID, "kind": "handout", "title": title})},
        files=[("files", (name, PDF_BYTES, "application/pdf"))],
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["job"]


def _insert_job(
    *, job_id: str, status: str, payload: dict | None, job_type: str = "import", cancel_requested: bool = False
) -> None:
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        db.add(
            Job(
                id=job_id,
                type=job_type,
                status=status,
                progress=0.0,
                errors=[],
                result_refs=[],
                payload=payload,
                cancel_requested=cancel_requested,
            )
        )
        db.commit()


def _get_job(job_id: str):
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        return db.get(Job, job_id)


def _add_file_record(file_id: str, name: str) -> None:
    from app.db import SessionLocal
    from app.models import FileRecord

    with SessionLocal() as db:
        db.add(
            FileRecord(
                id=file_id, original_name=name, mime_type="application/pdf", size_bytes=16, content_hash="hash-" + name
            )
        )
        db.commit()


def _drain_queue(limit: int = 5) -> None:
    for _ in range(limit):
        if not worker.process_next_job():
            return


def test_worker_import_end_to_end(client: TestClient, files_dir) -> None:
    job = _upload(client)
    tmp_dir = files_dir / "tmp"
    assert not tmp_dir.exists() or list(tmp_dir.glob("*.part")) == []

    _drain_queue()

    body = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert body["status"] == "succeeded"
    assert body["progress"] == 1.0
    assert body["errors"] == []
    assert body["started_at"] is not None and body["finished_at"] is not None
    refs = body["result_refs"]
    assert len(refs) == 1 and refs[0]["type"] == "resource"
    res_id = refs[0]["id"]

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["course_id"] == COURSE_ID
    assert res["kind"] == "handout"
    assert res["title"] == "worker 讲义"
    # 导入收尾自动派发 parse；假 PDF 解析如实失败（文件损坏或依赖环境缺失），不写成成功
    assert res["parse_status"] == "failed"
    assert res["chunk_count"] is None  # 未就绪：派生字段为 null
    assert res["file_id"] is not None and res["file_id"].startswith("fil_")

    versions = client.get(f"/api/v1/resources/{res_id}/versions").json()["items"]
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["parse_status"] == "failed"

    from app.db import SessionLocal
    from app.models import FileRecord, Job

    with SessionLocal() as db:
        file_id = db.execute(select(FileRecord.id).where(FileRecord.original_name == "worker.pdf")).scalar_one()
        parse_jobs = [
            j
            for j in db.execute(select(Job).where(Job.type == "parse")).scalars()
            if (j.payload or {}).get("resource_id") == res_id
        ]
        assert len(parse_jobs) == 1
        assert parse_jobs[0].status == "failed"
        assert parse_jobs[0].errors[0]["code"] in {"CORRUPT_FILE", "DEPENDENCY_MISSING"}
    assert (files_dir / file_id).exists()
    # 任务体/资源响应不泄露本机路径
    assert str(files_dir) not in json.dumps(body, ensure_ascii=False)
    assert str(files_dir) not in json.dumps(res, ensure_ascii=False)


def test_worker_partial_failed_records_item_errors(client: TestClient) -> None:
    _add_file_record("fil_" + "a" * 32, "可用.pdf")
    _insert_job(
        job_id="job_worktestpart01",
        status="queued",
        payload={
            "course_id": COURSE_ID,
            "kind": "scan",
            "title": "混合",
            "files": [
                {"file_id": "fil_" + "a" * 32, "name": "可用.pdf"},
                {"file_id": "fil_missing0001", "name": "缺失.pdf"},
            ],
        },
    )

    _drain_queue()

    row = _get_job("job_worktestpart01")
    assert row.status == "partial_failed"
    assert row.progress == 1.0
    assert len(row.errors) == 1
    assert row.errors[0]["code"] == "NOT_FOUND"
    assert row.errors[0]["details"]["file_id"] == "fil_missing0001"
    assert [r["type"] for r in row.result_refs] == ["resource"]


def test_worker_cooperative_cancel(client: TestClient) -> None:
    _insert_job(
        job_id="job_worktestcancel1",
        status="queued",
        cancel_requested=True,
        payload={"course_id": COURSE_ID, "kind": "other", "files": [{"file_id": "fil_x", "name": "x.pdf"}]},
    )

    _drain_queue()

    row = _get_job("job_worktestcancel1")
    assert row.status == "cancelled"
    assert row.finished_at is not None
    assert row.result_refs == []
    assert row.errors == []

    from app.db import SessionLocal
    from app.models import Resource

    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(Resource).where(Resource.title == "x.pdf")).scalar() == 0


def test_worker_crash_recovery_requeues_running(client: TestClient) -> None:
    _insert_job(
        job_id="job_worktestcrash01",
        status="running",
        payload={"course_id": COURSE_ID, "kind": "other", "title": "崩溃恢复", "files": [{"file_id": "fil_x"}]},
    )
    from app.db import SessionLocal
    from app.models import Job, utc_now

    with SessionLocal() as db:
        row = db.get(Job, "job_worktestcrash01")
        row.progress = 0.5
        row.started_at = utc_now()
        row.errors = [worker.job_error("OLD", "崩溃前的残留错误")]
        db.commit()

    with SessionLocal() as db:
        recovered = worker.requeue_stale(db)
    assert recovered >= 1

    row = _get_job("job_worktestcrash01")
    assert row.status == "queued"
    assert row.started_at is None
    assert row.progress == 0.0
    assert row.errors == []
    assert row.payload["course_id"] == COURSE_ID  # 原参数保留，可重放


def test_worker_unknown_type_fails(client: TestClient) -> None:
    _insert_job(job_id="job_worktestmystery1", status="queued", payload=None, job_type="mystery")

    _drain_queue()

    row = _get_job("job_worktestmystery1")
    assert row.status == "failed"
    assert row.errors[0]["code"] == "NO_HANDLER"
    assert row.errors[0]["retryable"] is False
    assert row.finished_at is not None


def test_worker_reprocess_is_idempotent(client: TestClient, files_dir) -> None:
    job = _upload(client, name="replay.pdf", title="重放讲义")
    _drain_queue()
    first = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert first["status"] == "succeeded"
    res_id = first["result_refs"][0]["id"]

    # 模拟崩溃恢复后的整体重放：任务重置为 queued 再执行
    from app.db import SessionLocal
    from app.models import Job, Resource

    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        row.status = "queued"
        row.started_at = None
        row.finished_at = None
        row.progress = 0.0
        db.commit()

    _drain_queue()

    second = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert second["status"] == "succeeded"
    assert second["result_refs"] == [{"type": "resource", "id": res_id}]

    with SessionLocal() as db:
        file_id = db.execute(select(Resource.file_id).where(Resource.id == res_id)).scalar_one()
        same_blob = db.execute(select(Resource.id).where(Resource.file_id == file_id)).scalars().all()
    assert same_blob == [res_id]  # 同一 blob 未重复建资源
