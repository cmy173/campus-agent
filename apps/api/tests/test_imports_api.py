"""导入通道测试：multipart 上传、幂等重放、类型/大小检查、本地路径不外泄。"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

PDF_BYTES = b"%PDF-1.4\n% test pdf content\n"
PPTX_BYTES = b"PK\x03\x04" + b"\x00" * 32
COURSE_ID = "crs_3f8a21"


@pytest.fixture()
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    return tmp_path / "files"


@pytest.fixture(autouse=True)
def _clean_import_rows():
    """导入测试在共享会话库上新增 job/file/幂等记录，用例结束清理以保持其他测试确定性。"""
    yield
    from sqlalchemy import delete

    from app.db import SessionLocal
    from app.models import FileRecord, IdempotencyRecord, Job

    with SessionLocal() as db:
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.endpoint == "POST /imports"))
        db.execute(delete(Job).where(Job.type == "import", Job.status == "queued"))
        db.execute(delete(FileRecord))
        db.commit()


def _post(
    client: TestClient,
    *,
    payload: dict | None = None,
    files: list | None = None,
    key: str | None = None,
    raw_payload: str | None = None,
):
    data = {"payload": raw_payload if raw_payload is not None else json.dumps(payload or {})}
    multipart = files if files is not None else [("files", ("讲义.pdf", PDF_BYTES, "application/pdf"))]
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/v1/imports", data=data, files=multipart, headers=headers)


def _file_rows(name: str) -> list:
    from app.db import SessionLocal
    from app.models import FileRecord

    with SessionLocal() as db:
        return list(db.execute(select(FileRecord).where(FileRecord.original_name == name)).scalars())


def test_import_creates_queued_job(client: TestClient, files_dir) -> None:
    resp = _post(client, payload={"course_id": COURSE_ID, "kind": "handout", "title": "第一讲讲义"})
    assert resp.status_code == 202, resp.text
    job = resp.json()["job"]
    assert job["id"].startswith("job_")
    assert job["type"] == "import"
    assert job["status"] == "queued"
    assert job["progress"] == 0.0
    assert job["errors"] == []
    assert job["result_refs"] == []
    assert job["cancel_requested"] is False
    assert job["started_at"] is None and job["finished_at"] is None
    # 本地路径不外泄
    assert str(files_dir) not in resp.text
    # blob 落盘 + 元数据入库 + 临时文件清理
    rows = _file_rows("讲义.pdf")
    assert len(rows) == 1
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", rows[0].content_hash)  # 契约 common.schema.json#/$defs/content_hash
    assert (files_dir / rows[0].id).exists()
    assert list((files_dir / "tmp").glob("*.part")) == []


def test_import_multi_files(client: TestClient, files_dir) -> None:
    files = [
        ("files", ("讲义甲.pdf", PDF_BYTES, "application/pdf")),
        ("files", ("课件乙.pptx", PPTX_BYTES, "application/vnd.openxmlformats-officedocument.presentationml.presentation")),
    ]
    resp = _post(client, payload={"course_id": COURSE_ID, "kind": "slides"}, files=files)
    assert resp.status_code == 202, resp.text
    assert len(_file_rows("讲义甲.pdf")) == 1
    assert len(_file_rows("课件乙.pptx")) == 1


def test_import_idempotent_replay(client: TestClient, files_dir) -> None:
    key = "import-replay-key-0001"
    body = {"course_id": COURSE_ID, "kind": "document", "title": "重放测试"}
    files = [("files", ("重放.pdf", PDF_BYTES + b"replay", "application/pdf"))]
    r1 = _post(client, payload=body, key=key, files=files)
    r2 = _post(client, payload=body, key=key, files=files)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json() == r2.json()
    assert len(_file_rows("重放.pdf")) == 1


def test_import_key_reused_conflict(client: TestClient, files_dir) -> None:
    key = "import-conflict-key-01"
    r1 = _post(client, payload={"course_id": COURSE_ID, "kind": "document", "title": "标题甲"}, key=key)
    assert r1.status_code == 202
    r2 = _post(client, payload={"course_id": COURSE_ID, "kind": "document", "title": "标题乙"}, key=key)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # 失败请求不留临时文件
    assert list((files_dir / "tmp").glob("*.part")) == []


def test_import_validation(client: TestClient, files_dir) -> None:
    bad_json = _post(client, raw_payload="{not json")
    assert bad_json.status_code == 400
    assert bad_json.json()["error"]["code"] == "VALIDATION_FAILED"

    missing_course = _post(client, payload={"course_id": "crs_nope", "kind": "handout"})
    assert missing_course.status_code == 404
    assert missing_course.json()["error"]["code"] == "NOT_FOUND"

    bad_kind = _post(client, payload={"course_id": COURSE_ID, "kind": "video"})
    assert bad_kind.status_code == 400

    link_kind = _post(client, payload={"course_id": COURSE_ID, "kind": "link"})
    assert link_kind.status_code == 400

    no_files = client.post("/api/v1/imports", data={"payload": json.dumps({"course_id": COURSE_ID, "kind": "handout"})})
    assert no_files.status_code == 400
    assert no_files.json()["error"]["code"] == "VALIDATION_FAILED"

    short_key = _post(client, payload={"course_id": COURSE_ID, "kind": "handout"}, key="short")
    assert short_key.status_code == 400


def test_import_file_type_checks(client: TestClient, files_dir) -> None:
    exe = _post(client, payload={"course_id": COURSE_ID, "kind": "document"}, files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))])
    assert exe.status_code == 415
    assert exe.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    fake_pdf = _post(client, payload={"course_id": COURSE_ID, "kind": "document"}, files=[("files", ("fake.pdf", b"hello world", "application/pdf"))])
    assert fake_pdf.status_code == 415

    empty_pdf = _post(client, payload={"course_id": COURSE_ID, "kind": "document"}, files=[("files", ("empty.pdf", b"", "application/pdf"))])
    assert empty_pdf.status_code == 415

    assert list((files_dir / "tmp").glob("*.part")) == []


def test_import_zip_archive_accepted_with_magic_check(client: TestClient, files_dir) -> None:
    """A22 真实资源形态（.zip 课件包）可入库归档；魔数与 OOXML 同规（PK），伪 ZIP 仍 415。"""
    zip_bytes = b"PK\x03\x04" + b"\x00" * 32
    ok = _post(
        client,
        payload={"course_id": COURSE_ID, "kind": "document"},
        files=[("files", ("课件包.zip", zip_bytes, "application/zip"))],
    )
    assert ok.status_code == 202, ok.text
    rows = _file_rows("课件包.zip")
    assert len(rows) == 1 and rows[0].mime_type == "application/zip"

    fake = _post(
        client,
        payload={"course_id": COURSE_ID, "kind": "document"},
        files=[("files", ("fake.zip", b"not a zip", "application/zip"))],
    )
    assert fake.status_code == 415
    assert fake.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert len(_file_rows("fake.zip")) == 0


def test_import_size_limit(client: TestClient, files_dir, monkeypatch) -> None:
    monkeypatch.setattr("app.config.MAX_UPLOAD_BYTES", 16)
    resp = _post(client, payload={"course_id": COURSE_ID, "kind": "document"}, files=[("files", ("big.pdf", PDF_BYTES, "application/pdf"))])
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert list((files_dir / "tmp").glob("*.part")) == []
