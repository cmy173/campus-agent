"""B5 解析流水线测试：导入自动派发 parse → D2 解析 → ContentChunk 落库与派生字段。

- 用最小 DOCX（zip + word/document.xml，纯标准库构造）保证结果环境无关（PPTX/DOCX 解析为纯标准库）；
- 覆盖：成功落库与溯源、重放幂等（替换式写入）、坏文件如实失败（含诊断 ID）、
  needs_ocr 不写成成功、取消回退、过期版本不改动当前状态、解析护栏单一常量（PD-1）。
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

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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
    [("Heading1", "第一讲 绪论"), (None, "这是解析流水线测试的正文段落，字符总数超过二十个。")]
)


@pytest.fixture()
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    return tmp_path / "files"


@pytest.fixture(autouse=True)
def _isolation():
    """共享会话库隔离：快照既有 job/资源/文件，用例后清理新增行并还原被触动的既有 job。"""
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
        files_before = set(db.execute(select(FileRecord.id)).scalars())

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
        for file_id in [f for f in db.execute(select(FileRecord.id)).scalars() if f not in files_before]:
            db.delete(db.get(FileRecord, file_id))
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.endpoint == "POST /imports"))
        db.commit()


def _upload(client: TestClient, data: bytes, *, name: str = "lecture.docx", title: str = "解析讲义") -> dict:
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": COURSE_ID, "kind": "handout", "title": title})},
        files=[("files", (name, data, DOCX_MIME))],
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["job"]


def _drain(limit: int = 10) -> None:
    for _ in range(limit):
        if not worker.process_next_job():
            return


def _parse_job(resource_id: str):
    """本用例该资源派发的 parse 任务（排除样例种子中的 parse 任务）。"""
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        jobs = [
            j
            for j in db.execute(select(Job).where(Job.type == "parse")).scalars()
            if (j.payload or {}).get("resource_id") == resource_id
        ]
    assert len(jobs) == 1
    return jobs[0]


def _import_resource_id(client: TestClient, import_job: dict) -> str:
    body = client.get(f"/api/v1/jobs/{import_job['id']}").json()
    assert body["status"] == "succeeded", body
    refs = body["result_refs"]
    assert len(refs) == 1 and refs[0]["type"] == "resource"
    return refs[0]["id"]


def test_import_dispatches_parse_and_lands_chunks(client: TestClient, files_dir) -> None:
    import_job = _upload(client, _READY_DOCX)
    _drain()
    res_id = _import_resource_id(client, import_job)

    parse_job = _parse_job(res_id)
    assert parse_job.status == "succeeded"
    assert parse_job.payload["resource_id"] == res_id
    assert parse_job.payload["version"] == 1
    assert parse_job.result_refs == [{"type": "resource", "id": res_id}]

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "ready"
    assert res["chunk_count"] == 2
    assert res["extent_value"] is None and res["extent_unit"] is None  # docx 段落数不是契约篇幅单位
    versions = client.get(f"/api/v1/resources/{res_id}/versions").json()["items"]
    assert versions[0]["parse_status"] == "ready"

    body = client.get(f"/api/v1/resources/{res_id}/chunks").json()
    items = body["items"]
    assert body["next_cursor"] is None
    assert [c["ordinal"] for c in items] == [0, 1]
    assert items[0]["text"] == "第一讲 绪论"
    assert "正文" in items[1]["text"]
    for chunk in items:
        assert chunk["resource_id"] == res_id and chunk["resource_version"] == 1
        assert chunk["locator"]["kind"] == "text"
        assert chunk["locator"]["end_offset"] > chunk["locator"]["start_offset"] >= 0
        assert chunk["provenance"]["extractor"] == "docx-text"
        assert chunk["provenance"]["extractor_version"]  # 解析版本可解释结果差异
        assert chunk["provenance"]["job_id"] == parse_job.id  # 片段可溯源到解析任务
        assert chunk["provenance"]["fact_source"] == "platform"
        assert chunk["provenance"]["observed_at"].endswith("Z")


def test_parse_replay_replaces_chunks_idempotently(client: TestClient, files_dir) -> None:
    import_job = _upload(client, _READY_DOCX)
    _drain()
    res_id = _import_resource_id(client, import_job)
    first = client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"]
    assert len(first) == 2

    # 模拟崩溃恢复后的重放：任务重置为 queued 再执行（替换式写入应幂等，不产生重复片段）
    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        row = db.get(Job, _parse_job(res_id).id)
        row.status = "queued"
        row.started_at = None
        row.finished_at = None
        row.progress = 0.0
        db.commit()

    _drain()

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "ready" and res["chunk_count"] == 2
    second = client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"]
    assert [c["ordinal"] for c in second] == [0, 1]
    assert [c["text"] for c in second] == [c["text"] for c in first]


def test_parse_corrupt_file_reports_readable_error_with_diagnosis_id(client: TestClient, files_dir) -> None:
    import_job = _upload(client, b"PK\x03\x04corrupted", name="broken.docx", title="坏文件")
    _drain()
    res_id = _import_resource_id(client, import_job)

    parse_job = _parse_job(res_id)
    assert parse_job.status == "failed"
    error = parse_job.errors[0]
    assert error["code"] == "CORRUPT_FILE"
    assert error["retryable"] is False
    assert error["message"]
    assert error["details"]["job_id"] == parse_job.id  # 诊断 ID
    assert error["details"]["resource_id"] == res_id and error["details"]["version"] == 1

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "failed"
    assert res["chunk_count"] is None
    assert client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"] == []


def test_needs_ocr_is_failed_not_fake_success(client: TestClient, files_dir) -> None:
    import_job = _upload(client, _docx_bytes([(None, "短")]), name="scan.docx", title="扫描件")
    _drain()
    res_id = _import_resource_id(client, import_job)

    parse_job = _parse_job(res_id)
    assert parse_job.status == "failed"
    assert parse_job.errors[0]["code"] == "NEEDS_OCR"
    assert parse_job.errors[0]["retryable"] is True
    assert "OCR" in parse_job.errors[0]["message"]

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "failed"
    assert res["chunk_count"] is None
    assert client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"] == []


def test_zip_archive_stored_and_parse_skipped_not_failed(client: TestClient, files_dir) -> None:
    """A22 真实资源形态（.zip 课件包）：可入库归档；无解析器 → 终态 skipped（“该类型不解析”），不误标 failed。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "无可解析文档")
    zip_bytes = buffer.getvalue()
    resp = client.post(
        "/api/v1/imports",
        data={"payload": json.dumps({"course_id": COURSE_ID, "kind": "handout", "title": "课件包"})},
        files=[("files", ("L3课件.zip", zip_bytes, "application/zip"))],
    )
    assert resp.status_code == 202, resp.text
    _drain()
    res_id = _import_resource_id(client, resp.json()["job"])

    parse_job = _parse_job(res_id)
    assert parse_job.status == "failed"
    assert parse_job.errors[0]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert parse_job.errors[0]["retryable"] is False

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["mime_type"] == "application/zip"
    assert res["parse_status"] == "skipped"
    assert res["chunk_count"] is None
    versions = client.get(f"/api/v1/resources/{res_id}/versions").json()["items"]
    assert versions[0]["parse_status"] == "skipped"
    assert client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"] == []


def test_parse_cancel_keeps_previous_state_and_no_chunks(client: TestClient, files_dir) -> None:
    import_job = _upload(client, _READY_DOCX)
    assert worker.process_next_job()  # 先执行 import（最早入队），parse 排队待执行
    res_id = _import_resource_id(client, import_job)

    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        row = db.get(Job, _parse_job(res_id).id)
        assert row.status == "queued"
        row.cancel_requested = True
        db.commit()

    assert worker.process_next_job()  # 执行 parse：取消检查点中止

    parse_job = _parse_job(res_id)
    assert parse_job.status == "cancelled"
    assert parse_job.finished_at is not None

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "pending"  # 回退到此前状态（不是 processing）
    assert res["chunk_count"] is None
    assert client.get(f"/api/v1/resources/{res_id}/chunks").json()["items"] == []


def test_parse_of_superseded_version_keeps_current_state(client: TestClient, files_dir) -> None:
    """版本号已前进的解析任务：如实产出所请求版本的产物，但不改动当前版本的 parse_status。"""
    import_job = _upload(client, _READY_DOCX)
    _drain()
    res_id = _import_resource_id(client, import_job)

    from app.db import SessionLocal
    from app.models import Job, Resource, ResourceVersion

    with SessionLocal() as db:
        resource = db.get(Resource, res_id)
        resource.version = 2
        resource.parse_status = "pending"
        db.add(
            ResourceVersion(
                resource_id=res_id,
                version=2,
                content_hash=resource.content_hash,
                mime_type=resource.mime_type,
                size_bytes=resource.size_bytes,
                file_id=resource.file_id,
                fetched_at=resource.fetched_at,
                parse_status="pending",
            )
        )
        db.add(
            Job(
                id="job_parseteststale1",
                type="parse",
                status="queued",
                progress=0.0,
                errors=[],
                result_refs=[],
                payload={"resource_id": res_id, "version": 1, "reason": "test"},
            )
        )
        db.commit()

    _drain()

    with SessionLocal() as db:
        stale_job = db.get(Job, "job_parseteststale1")
        assert stale_job.status == "succeeded"
        v1 = db.execute(
            select(ResourceVersion).where(ResourceVersion.resource_id == res_id, ResourceVersion.version == 1)
        ).scalar_one()
        assert v1.parse_status == "ready"
        assert db.get(Resource, res_id).parse_status == "pending"
    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "pending"
    assert res["chunk_count"] is None  # 当前版本 v2 未就绪


def test_parse_limit_is_single_config_constant_and_fails_truthfully(
    client: TestClient, files_dir, monkeypatch
) -> None:
    """PD-1：解析护栏取 config.MAX_PARSE_BYTES 单一常量（worker 调用时读取，可收紧验证）。

    默认 256MiB，独立于平台下载与文件上传；
    上传通道（MAX_UPLOAD_BYTES，默认 1GiB）独立于解析上限：收紧后文件仍可上传，
    解析如实失败 FILE_TOO_LARGE（非重试型），不伪造成功。
    """
    from app import config

    assert config.MAX_PARSE_BYTES == 256 * 1024 * 1024
    assert config.MAX_UPLOAD_BYTES >= config.MAX_PARSE_BYTES
    assert worker._parse_limits().max_file_bytes == config.MAX_PARSE_BYTES

    monkeypatch.setattr("app.config.MAX_PARSE_BYTES", 16)

    import_job = _upload(client, _READY_DOCX)  # 16 字节护栏下仍可上传（上传通道未变）
    _drain()
    res_id = _import_resource_id(client, import_job)

    parse_job = _parse_job(res_id)
    assert parse_job.status == "failed"
    assert parse_job.errors[0]["code"] == "FILE_TOO_LARGE"
    assert parse_job.errors[0]["retryable"] is False

    res = client.get(f"/api/v1/resources/{res_id}").json()
    assert res["parse_status"] == "failed" and res["chunk_count"] is None


def test_parse_limit_env_override(monkeypatch) -> None:
    """PD-1：STUDY_MAX_PARSE_MB 环境变量覆盖（MiB→字节）。"""
    import importlib

    from app import config

    monkeypatch.setenv("STUDY_MAX_PARSE_MB", "5")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.MAX_PARSE_BYTES == 5 * 1024 * 1024
    finally:
        monkeypatch.delenv("STUDY_MAX_PARSE_MB")
        importlib.reload(config)
