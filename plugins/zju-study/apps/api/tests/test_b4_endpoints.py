"""B4 端点测试：任务取消/重试、连接启停、受控文件下载、检索、导出、重新解析。

用例自建上传/连接场地并快照清理共享测试库（jobs/资源/文件/连接/幂等键），
保持后续模块确定性；检索用例依赖种子片段（res_01a7 等，见 contracts/examples）。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import worker

COURSE_ID = "crs_3f8a21"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PLATFORM = "testb4"
CONN_ID = "conn_b4tst"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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


# 文本不包含检索用例使用的关键词（线性表/数据/二分查找），避免与种子片段互相干扰
_READY_DOCX = _docx_bytes(
    [("Heading1", "B4 讲稿"), (None, "这是 B4 端点测试的正文段落，用于验证上传、下载与解析流程。")]
)


@pytest.fixture()
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    return tmp_path / "files"


@pytest.fixture(autouse=True)
def _isolation():
    from app.db import SessionLocal
    from app.models import (
        Connection,
        ContentChunk,
        FileRecord,
        IdempotencyRecord,
        Job,
        Resource,
        ResourceVersion,
        SourceRef,
    )

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
        connections_before = {
            c.id: {col.name: getattr(c, col.name) for col in Connection.__table__.columns}
            for c in db.execute(select(Connection)).scalars()
        }
        resources_before = set(db.execute(select(Resource.id)).scalars())
        files_before = set(db.execute(select(FileRecord.id)).scalars())
        idem_before = set(db.execute(select(IdempotencyRecord.key)).scalars())

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
        for conn_id in [c.id for c in db.execute(select(Connection)).scalars() if c.id not in connections_before]:
            db.delete(db.get(Connection, conn_id))
        for conn_id, snap in connections_before.items():
            row = db.get(Connection, conn_id)
            if row is None:
                continue
            for key, value in snap.items():
                setattr(row, key, value)
        for res_id in [r for r in db.execute(select(Resource.id)).scalars() if r not in resources_before]:
            db.execute(delete(ContentChunk).where(ContentChunk.resource_id == res_id))
            db.execute(delete(ResourceVersion).where(ResourceVersion.resource_id == res_id))
            db.execute(delete(SourceRef).where(SourceRef.owner_type == "resource", SourceRef.owner_id == res_id))
            db.delete(db.get(Resource, res_id))
        for file_id in [f for f in db.execute(select(FileRecord.id)).scalars() if f not in files_before]:
            db.delete(db.get(FileRecord, file_id))
        for key in [k for k in db.execute(select(IdempotencyRecord.key)).scalars() if k not in idem_before]:
            db.delete(db.get(IdempotencyRecord, key))
        db.commit()


def _drain(limit: int = 10) -> None:
    for _ in range(limit):
        if not worker.process_next_job():
            return


def _drain_until(job_id: str, attempts: int = 20) -> str:
    from app.db import SessionLocal
    from app.models import Job

    for _ in range(attempts):
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None or job.status not in ("queued", "running"):
                return job.status if job is not None else "missing"
        assert worker.process_next_job(), "队列为空但目标任务未完成"
    raise AssertionError("任务未在预期步数内完成")


def _upload(client: TestClient, data: bytes = _READY_DOCX, *, name: str = "b4.docx", title: str = "B4 讲稿") -> dict:
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": COURSE_ID, "kind": "handout", "title": title})},
        files=[("files", (name, data, DOCX_MIME))],
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["job"]


def _job_payload(job_id: str) -> dict:
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        return dict(db.get(Job, job_id).payload or {})


def _set_payload(db_job_id: str, mutate) -> None:
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        job = db.get(Job, db_job_id)
        payload = dict(job.payload or {})
        mutate(payload)
        job.payload = payload
        db.commit()


# ────────────────────────── 取消 ──────────────────────────


def test_cancel_queued_job_and_terminal_conflict(client: TestClient) -> None:
    job_id = client.post("/api/v1/exports", json={}).json()["job"]["id"]
    cancel = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    body = cancel.json()
    assert body["status"] == "cancelled"
    assert body["cancel_requested"] is True and body["finished_at"] is not None
    # 终态任务再取消 → 409；worker 也不会执行已取消任务
    again = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"
    done = client.post("/api/v1/jobs/job_1a2b3c/cancel")
    assert done.status_code == 409
    missing = client.post("/api/v1/jobs/job_nope/cancel")
    assert missing.status_code == 404


def test_cancel_running_job_is_cooperative(client: TestClient) -> None:
    from app.db import SessionLocal
    from app.models import Job, utc_now

    job_id = client.post("/api/v1/exports", json={}).json()["job"]["id"]
    with SessionLocal() as db:  # 模拟 worker 已抢占
        job = db.get(Job, job_id)
        job.status = "running"
        job.started_at = utc_now()
        db.commit()

    resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"  # 协作式：响应可能仍为 running
    assert body["cancel_requested"] is True

    with SessionLocal() as db:  # 抢占后立即检查：不再执行处理器，直接落 cancelled
        worker.process_job(db, db.get(Job, job_id))
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "cancelled"
    assert detail["result_refs"] == []  # 处理器未跑，未产生导出文件


# ────────────────────────── 重试 ──────────────────────────


def test_retry_partial_import_only_failed_files(client: TestClient) -> None:
    job = _upload(client)
    _set_payload(job["id"], lambda p: p.__setitem__("files", p["files"] + [{"file_id": "fil_deadbeef", "name": "missing.pdf"}]))
    assert _drain_until(job["id"]) == "partial_failed"
    detail = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert [e["details"]["file_id"] for e in detail["errors"]] == ["fil_deadbeef"]
    assert len(detail["result_refs"]) == 1  # 有效文件照常产出

    retry = client.post(f"/api/v1/jobs/{job['id']}/retry", headers={"Idempotency-Key": "retry-key-0001"})
    assert retry.status_code == 202, retry.text
    new_job = retry.json()["job"]
    assert new_job["type"] == "import" and new_job["status"] == "queued"
    assert _job_payload(new_job["id"])["files"] == [{"file_id": "fil_deadbeef", "name": "missing.pdf"}]

    replay = client.post(f"/api/v1/jobs/{job['id']}/retry", headers={"Idempotency-Key": "retry-key-0001"})
    assert replay.status_code == 202 and replay.json()["job"]["id"] == new_job["id"]

    original = client.get(f"/api/v1/jobs/{job['id']}").json()  # 原任务不被修改
    assert original["status"] == "partial_failed" and len(original["result_refs"]) == 1

    assert _drain_until(new_job["id"]) == "failed"  # 重试仍失败（文件确实不存在）
    again = client.get(f"/api/v1/jobs/{new_job['id']}").json()["errors"][0]
    assert again["code"] == "NOT_FOUND"

    assert client.post(f"/api/v1/jobs/job_1a2b3c/retry").status_code == 409  # succeeded 不可重试
    assert client.post("/api/v1/jobs/job_nope/retry").status_code == 404


# ────────────────────────── 连接启停 ──────────────────────────


@pytest.fixture()
def conn_env(monkeypatch):
    from app.db import SessionLocal
    from app.models import Connection
    from app.sync_engine import _CHECK_REGISTRY

    with SessionLocal() as db:
        db.add(Connection(id=CONN_ID, platform=PLATFORM, display_name="B4 测试连接", status="ok"))
        db.commit()

    state: dict = {"result": None, "calls": 0}

    def check(connection):
        state["calls"] += 1
        assert connection == {"platform": PLATFORM, "connection_id": CONN_ID}
        return state["result"]

    monkeypatch.setitem(_CHECK_REGISTRY, PLATFORM, check)
    yield state


def test_disable_idempotent_and_enable_runs_check(client: TestClient, conn_env) -> None:
    first = client.post(f"/api/v1/connections/{CONN_ID}/disable")
    assert first.status_code == 200 and first.json()["status"] == "disabled"
    again = client.post(f"/api/v1/connections/{CONN_ID}/disable")
    assert again.status_code == 200 and again.json()["status"] == "disabled"
    assert client.post(f"/api/v1/connections/{CONN_ID}/sync").status_code == 409  # 停用后不可同步

    conn_env["result"] = {"platform": PLATFORM, "state": "ok", "checked_at": "2026-10-09T10:00:00+08:00"}
    enabled = client.post(f"/api/v1/connections/{CONN_ID}/enable")
    assert enabled.status_code == 200 and enabled.json()["status"] == "ok"
    assert conn_env["calls"] == 1

    repeat = client.post(f"/api/v1/connections/{CONN_ID}/enable")  # 非 disabled：幂等，不重复检查
    assert repeat.status_code == 200 and repeat.json()["status"] == "ok"
    assert conn_env["calls"] == 1


def test_enable_falls_back_to_error_states(client: TestClient, conn_env, monkeypatch) -> None:
    from app.sync_engine import _CHECK_REGISTRY

    client.post(f"/api/v1/connections/{CONN_ID}/disable")
    conn_env["result"] = {"platform": PLATFORM, "state": "auth_required", "checked_at": "2026-10-09T10:00:00+08:00",
                          "action": {"type": "user_login", "entry_url": "https://login.example.edu/", "message": "请重新登录"}}
    body = client.post(f"/api/v1/connections/{CONN_ID}/enable").json()
    assert body["status"] == "auth_required"
    assert body["recovery_action"]["entry_url"] == "https://login.example.edu/"

    client.post(f"/api/v1/connections/{CONN_ID}/disable")
    conn_env["result"] = {"platform": PLATFORM, "state": "weird", "checked_at": "2026-10-09T10:00:00+08:00"}
    body = client.post(f"/api/v1/connections/{CONN_ID}/enable").json()  # 非法结果：解除停用但如实落 network_error
    assert body["status"] == "network_error"
    assert body["last_error"]["code"] == "INTERNAL_ERROR"

    client.post(f"/api/v1/connections/{CONN_ID}/disable")
    monkeypatch.delitem(_CHECK_REGISTRY, PLATFORM)
    body = client.post(f"/api/v1/connections/{CONN_ID}/enable").json()  # 适配器缺失：同样不困在 disabled
    assert body["status"] == "network_error"
    assert body["last_error"]["code"] == "DEPENDENCY_UNAVAILABLE"


# ────────────────────────── 受控文件下载 ──────────────────────────


def test_files_download_roundtrip(client: TestClient, files_dir) -> None:
    job = _upload(client)
    assert _drain_until(job["id"]) == "succeeded"
    file_id = _job_payload(job["id"])["files"][0]["file_id"]

    resp = client.get(f"/api/v1/files/{file_id}")
    assert resp.status_code == 200, resp.text
    assert resp.content == _READY_DOCX
    assert resp.headers["content-type"].startswith(DOCX_MIME)
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and "b4.docx" in disposition

    assert client.get("/api/v1/files/fil_01a7v2").status_code == 404  # 种子资源无文件元数据
    assert client.get("/api/v1/files/not-a-file-id").status_code == 404

    (files_dir / file_id).unlink()  # 元数据在库但 blob 缺失
    gone = client.get(f"/api/v1/files/{file_id}")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "NOT_FOUND"


# ────────────────────────── 检索 ──────────────────────────


def test_search_finds_seed_chunks(client: TestClient) -> None:
    body = client.get("/api/v1/search", params={"q": "线性表"}).json()
    assert [h["chunk_id"] for h in body["items"]] == ["chk_01a7c0", "chk_01a7c1"]
    assert body["next_cursor"] is None
    hit = body["items"][0]
    assert set(hit) == {"chunk_id", "resource_id", "resource_version", "course_id", "resource_title", "text", "locator", "score"}
    assert hit["resource_id"] == "res_01a7" and hit["resource_version"] == 2
    assert hit["course_id"] == COURSE_ID and hit["locator"]["kind"] == "slide"
    assert hit["score"] == 1.0

    binary = client.get("/api/v1/search", params={"q": "二分查找"}).json()["items"]
    assert [h["chunk_id"] for h in binary] == ["chk_02e1c0"]
    assert binary[0]["resource_id"] == "res_02e1" and binary[0]["course_id"] == "crs_51c9d0"


def test_search_filters_pagination_and_literals(client: TestClient) -> None:
    first = client.get("/api/v1/search", params={"q": "数据", "limit": 1}).json()
    assert [h["chunk_id"] for h in first["items"]] == ["chk_03f1c0"]
    assert first["items"][0]["score"] == 2.0  # “数据结构/数据类型”两次命中
    assert first["next_cursor"]

    second = client.get("/api/v1/search", params={"q": "数据", "limit": 1, "cursor": first["next_cursor"]}).json()
    assert [h["chunk_id"] for h in second["items"]] == ["chk_01a7c0"] and second["next_cursor"]
    third = client.get("/api/v1/search", params={"q": "数据", "limit": 1, "cursor": second["next_cursor"]}).json()
    assert [h["chunk_id"] for h in third["items"]] == ["chk_01a7c1"]
    assert third["next_cursor"] is None

    assert client.get("/api/v1/search", params={"q": "数据", "course_id": "crs_51c9d0"}).json()["items"] == []
    assert len(client.get("/api/v1/search", params={"q": "线性表", "kind": "slides"}).json()["items"]) == 2
    assert client.get("/api/v1/search", params={"q": "线性表", "kind": "handout"}).json()["items"] == []
    assert client.get("/api/v1/search", params={"q": "100%"}).json()["items"] == []  # % 不作为通配符
    assert client.get("/api/v1/search", params={"q": "数据", "kind": "video"}).status_code == 400
    assert client.get("/api/v1/search", params={"q": "数据", "cursor": "!!!"}).status_code == 400


# ────────────────────────── 重新解析 ──────────────────────────


def test_reparse_dedupes_and_keeps_old_artifacts(client: TestClient, files_dir) -> None:
    job = _upload(client)
    assert _drain_until(job["id"]) == "succeeded"
    _drain()  # 导入收尾派发的 parse 任务
    res_id = client.get(f"/api/v1/jobs/{job['id']}").json()["result_refs"][0]["id"]
    assert client.get(f"/api/v1/resources/{res_id}").json()["parse_status"] == "ready"

    first = client.post(f"/api/v1/resources/{res_id}/reparse")
    assert first.status_code == 202, first.text
    parse_job = first.json()["job"]
    assert parse_job["type"] == "parse" and parse_job["status"] == "queued"
    again = client.post(f"/api/v1/resources/{res_id}/reparse")  # 去抖：复用排队中任务
    assert again.json()["job"]["id"] == parse_job["id"]

    kept = client.get(f"/api/v1/resources/{res_id}").json()  # 旧产物在新任务成功前保持可用
    assert kept["parse_status"] == "ready" and kept["chunk_count"] == 2

    assert _drain_until(parse_job["id"]) == "succeeded"
    after = client.get(f"/api/v1/resources/{res_id}").json()
    assert after["parse_status"] == "ready" and after["chunk_count"] == 2
    chunks = client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"]
    assert [c["ordinal"] for c in chunks] == [0, 1] and all(c["resource_version"] == 1 for c in chunks)

    conflict = client.post("/api/v1/resources/res_01d4/reparse")  # 种子资源无受控文件
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "CONFLICT"
    assert client.post("/api/v1/resources/res_nope/reparse").status_code == 404


# ────────────────────────── 导出 ──────────────────────────


def test_exports_creates_downloadable_file(client: TestClient, files_dir) -> None:
    resp = client.post("/api/v1/exports", json={}, headers={"Idempotency-Key": "export-key-0001"})
    assert resp.status_code == 202, resp.text
    job = resp.json()["job"]
    assert job["type"] == "export" and job["status"] == "queued"

    replay = client.post("/api/v1/exports", json={}, headers={"Idempotency-Key": "export-key-0001"})
    assert replay.status_code == 202 and replay.json()["job"]["id"] == job["id"]
    reuse = client.post("/api/v1/exports", json={"include": ["courses"]}, headers={"Idempotency-Key": "export-key-0001"})
    assert reuse.status_code == 409 and reuse.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    assert _drain_until(job["id"]) == "succeeded"
    refs = client.get(f"/api/v1/jobs/{job['id']}").json()["result_refs"]
    assert len(refs) == 1 and refs[0]["type"] == "export_file" and refs[0]["id"].startswith("fil_")

    download = client.get(f"/api/v1/files/{refs[0]['id']}")
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith("application/json")
    document = json.loads(download.content.decode("utf-8"))
    assert document["exported_at"].endswith("Z")
    sections = document["sections"]
    assert set(sections) == {"courses", "resources", "assignments", "recordings", "tasks", "plans", "feedback"}
    assert any(c["id"] == COURSE_ID for c in sections["courses"])
    assert any(r["id"] == "res_01a7" and r["chunk_count"] == 2 for r in sections["resources"])

    only = client.post("/api/v1/exports", json={"include": ["courses"]})
    assert only.status_code == 202
    only_id = only.json()["job"]["id"]
    assert _drain_until(only_id) == "succeeded"
    ref2 = client.get(f"/api/v1/jobs/{only_id}").json()["result_refs"][0]["id"]
    doc2 = json.loads(client.get(f"/api/v1/files/{ref2}").content.decode("utf-8"))
    assert set(doc2["sections"]) == {"courses"}

    bad = client.post("/api/v1/exports", json={"include": ["passwords"]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "VALIDATION_FAILED"
    bad_format = client.post("/api/v1/exports", json={"format": "csv"})
    assert bad_format.status_code == 400
