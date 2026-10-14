"""个人使用闭环：真实本地 HTTP 桥接 + 隔离数据库，平台响应为明确的合成样例。"""
import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import config, worker, connectors_registry, sync_engine
from app.bootstrap import ensure_connections
from app.db import Base, get_db
from app.main import app
from app.models import Connection, Resource, Job


def pptx_bytes(slides=6):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for i in range(1, slides + 1):
            archive.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:sp><p:txBody><a:p><a:r><a:t>第{i}张课件：这是用于验证真实文件链路的合成正文。</a:t></a:r></a:p></p:txBody></p:sp></p:cSld></p:sld>')
    return out.getvalue()


def archive_bytes(name, data):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, data)
    return out.getvalue()


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'personal.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(sync_engine, "_REGISTRY", {})
    monkeypatch.setattr(sync_engine, "_CHECK_REGISTRY", {})
    monkeypatch.setattr(connectors_registry, "PROTOCOL_FACTORIES", {})
    monkeypatch.setattr(connectors_registry, "REGISTERED", {})
    monkeypatch.delenv("STUDY_WORKER_INPROC", raising=False)
    def db_override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = db_override
    with sessions() as db:
        ensure_connections(db)
        ensure_connections(db)
        assert len(list(db.execute(select(Connection)).scalars())) == 2
    try:
        with TestClient(app) as client:
            yield client, sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def drain():
    for _ in range(100):
        if not worker.process_next_job():
            return
    raise AssertionError("队列未能收敛")


def test_zykt_discovery_scope_crosses_api_and_http_bridge(local_client, monkeypatch):
    from connectors.browser.session_runner import ConnectorService, start_http_server
    client, _ = local_client
    received = []
    class Sessions:
        def session(self): return object()
        def cookies_snapshot(self): return []
    class Adapter:
        def list_courses(self, cursor=None, term=None):
            received.append((cursor, term))
            return {"items": [], "next_cursor": None, "errors": []}
    service = ConnectorService(Sessions(), adapter_factory=lambda *_: Adapter())
    service.platform = "zykt"
    server = start_http_server(service, "127.0.0.1", 0)
    try:
        monkeypatch.setenv("ZYKT_BRIDGE_URL", f"http://127.0.0.1:{server.server_port}")
        response = client.get("/api/v1/connections/conn_zykt/courses?term=all&cursor=all:recent:2:abc")
        assert response.status_code == 200, response.text
        assert received == [("all:recent:2:abc", "all")]
    finally:
        server.shutdown()
        server.server_close()


def test_browser_discovery_sync_fetch_parse_and_personal_plan(local_client, tmp_path, monkeypatch):
    from connectors.browser.session_runner import ConnectorService, start_http_server
    client, sessions = local_client
    stamp = "2026-10-11T00:00:00Z"
    data = pptx_bytes()
    download = tmp_path / "staged"
    download.mkdir()
    (download / "lesson.pptx").write_bytes(data)
    class Sessions:
        def session(self): return object()
        def cookies_snapshot(self): return []
    class Adapter:
        def check_connection(self):
            return {"platform": "xzzd", "state": "ok", "checked_at": stamp, "action": None, "detail": None}
        def list_courses(self, cursor=None, term=None):
            return {"items": [{"id": "12345", "title": "合成课程", "term": "2026秋冬", "teacher": "合成教师",
                               "source_refs": [{"platform": "xzzd", "external_id": "12345", "canonical_url": None, "observed_at": stamp}],
                               "archived_at": None, "created_at": stamp, "updated_at": stamp}], "next_cursor": None, "errors": []}
        def sync_course(self, course_id, since_cursor=None, kinds=None):
            return {"platform": "xzzd", "connection_id": "conn_xzzd", "course_id": course_id,
                    "mode": "resync", "cursor": {"previous": since_cursor, "next": "next"},
                    "upserts": {"resources": [{"kind": "slides", "title": "合成课件", "content_hash": None,
                                               "mime_type": None, "extent_value": None, "extent_unit": None,
                                               "source_updated_at": stamp, "source_ref": {
                                                   "platform": "xzzd", "external_id": "file-1", "canonical_url": None, "observed_at": stamp}}],
                                "assignments": [], "recordings": []},
                    "deletions": [], "errors": [], "stats": {"scanned": 1, "upserted": 1, "deleted": 0, "failed": 0}, "observed_at": stamp}
        def fetch_resource(self, resource_id):
            return {"status": "ok", "handle": {"kind": "local_file", "ref": "lesson.pptx", "expires_at": stamp,
                                               "sha256": f"sha256:{hashlib.sha256(data).hexdigest()}", "size": len(data)}, "denial": None}
    service = ConnectorService(Sessions(), adapter_factory=lambda *_: Adapter(), download_dir=download, rate_limit_rps=1000)
    service._resources["file-1"] = {"file_name": "lesson.pptx"}
    server = start_http_server(service, "127.0.0.1", 0)
    monkeypatch.setenv("XZZD_BRIDGE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        discovered = client.get("/api/v1/connections/conn_xzzd/courses")
        assert discovered.status_code == 200, discovered.text
        selected = {"courses": [{"external_id": "12345", "title": discovered.json()["items"][0]["title"]}]}
        first = client.post("/api/v1/connections/conn_xzzd/courses/import", json=selected).json()
        second = client.post("/api/v1/connections/conn_xzzd/courses/import", json=selected).json()
        assert first == second
        course_id = first["items"][0]["id"]
        assert course_id != "12345"
        assert len(client.get("/api/v1/courses").json()["items"]) == 1
        assert client.post("/api/v1/connections/conn_xzzd/check").json()["status"] == "ok"
        for _ in range(2):
            response = client.post("/api/v1/connections/conn_xzzd/sync")
            assert response.status_code == 202, response.text
            job_id = response.json()["job"]["id"]
            drain()
            assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "succeeded", client.get(f"/api/v1/jobs/{job_id}").text
        resources = client.get(f"/api/v1/courses/{course_id}/resources").json()["items"]
        assert len(resources) == 1
        resource = resources[0]
        assert resource["parse_status"] == "ready"
        assert resource["extent_value"] == 6 and resource["extent_unit"] == "slide"
        assert resource["version"] == 1 and resource["mime_type"].endswith("presentation")
        assert client.get(f"/api/v1/files/{resource['file_id']}").content == data
        tasks = client.get("/api/v1/tasks").json()["items"]
        assert len(tasks) == 1 and tasks[0]["estimated_minutes"] != 60
        availability = {"timezone": "Asia/Shanghai", "weekly_windows": [{"weekday": i, "start": "19:00", "end": "20:00"} for i in range(1, 8)],
                        "date_exceptions": [], "daily_limit_minutes": 60}
        assert client.put("/api/v1/preferences/availability", json=availability).status_code == 200
        preview = client.post("/api/v1/plans/preview", json={"horizon_start": "2026-10-12", "horizon_end": "2026-10-18"})
        assert preview.status_code == 201, preview.text
        plan = preview.json()["plan"]
        assert plan["blocks"] and not plan["unassigned"]
        assert client.post(f"/api/v1/plans/{plan['id']}/apply").status_code == 200
    finally:
        server.shutdown(); server.server_close()


def test_zip_extracts_children_reparse_is_idempotent(local_client):
    client, sessions = local_client
    course = client.post("/api/v1/courses", json={"title": "ZIP合成课程"}).json()
    archive = archive_bytes("chapters/lesson.pptx", pptx_bytes())
    imported = client.post("/api/v1/imports", data={"payload": json.dumps({"course_id": course["id"], "kind": "handout"})},
                           files=[("files", ("课程资料.zip", archive, "application/zip"))])
    assert imported.status_code == 202, imported.text
    drain()
    rows = client.get(f"/api/v1/courses/{course['id']}/resources").json()["items"]
    assert len(rows) == 2
    parent = next(r for r in rows if r["mime_type"] == "application/zip")
    child = next(r for r in rows if r["mime_type"] != "application/zip")
    assert parent["parse_status"] == "skipped" and child["parse_status"] == "ready"
    assert child["extent_value"] == 6
    assert client.post(f"/api/v1/resources/{parent['id']}/reparse").status_code == 202
    drain()
    assert len(client.get(f"/api/v1/courses/{course['id']}/resources").json()["items"]) == 2
    assert len(client.get("/api/v1/tasks").json()["items"]) == 1


@pytest.mark.parametrize("name", ["../outside.pptx", "/outside.pptx", "C:/outside.pptx"])
def test_zip_unsafe_paths_fail_before_importing_members(local_client, name):
    client, sessions = local_client
    course = client.post("/api/v1/courses", json={"title": "ZIP安全用例"}).json()
    archive = archive_bytes(name, pptx_bytes())
    client.post("/api/v1/imports", data={"payload": json.dumps({"course_id": course["id"], "kind": "handout"})},
                files=[("files", ("unsafe.zip", archive, "application/zip"))])
    drain()
    rows = client.get(f"/api/v1/courses/{course['id']}/resources").json()["items"]
    assert len(rows) == 1 and rows[0]["parse_status"] == "failed"
    with sessions() as db:
        errors = [e for j in db.execute(select(Job)).scalars() for e in (j.errors or [])]
    assert any(e["code"] == "CORRUPT_FILE" for e in errors)
