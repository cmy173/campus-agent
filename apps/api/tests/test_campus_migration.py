from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.campus_workspace import CampusWorkspace, CacheStore
from app.campus_migration import inventory, import_legacy, rollback_import, read_imported_document, imported_content
from app.errors import ApiError


LEGACY_SCHEMA = """
CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT, password TEXT);
CREATE TABLE passport_accounts(owner TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, csrf TEXT, created INTEGER);
CREATE TABLE campus_credentials(owner TEXT PRIMARY KEY, secret TEXT);
CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, action TEXT, result TEXT);
CREATE TABLE documents(id TEXT PRIMARY KEY, owner TEXT, name TEXT, kind TEXT, sha256 TEXT,
 content BLOB, status TEXT, error TEXT, warnings TEXT, created_at TEXT);
CREATE TABLE chunks(id TEXT PRIMARY KEY, document TEXT, position INTEGER, locator TEXT, text TEXT);
CREATE TABLE tasks(id TEXT PRIMARY KEY, document TEXT, chunk TEXT, title TEXT, due TEXT, raw_date TEXT,
 state TEXT, reviewed INTEGER, evidence TEXT, time_precision TEXT);
CREATE TABLE document_sources(id TEXT PRIMARY KEY, owner TEXT, origin TEXT, college TEXT,
 body_sha256 TEXT, attachments TEXT, unread TEXT);
CREATE TABLE notice_analyses(owner TEXT, result TEXT, status TEXT, diagnostics TEXT);
CREATE TABLE campus_snapshots(owner TEXT PRIMARY KEY, snapshot TEXT);
CREATE TABLE connector_cache(owner TEXT NOT NULL, platform TEXT NOT NULL, capability TEXT NOT NULL,
 scope TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(owner,platform,capability,scope));
"""


@pytest.fixture
def legacy(tmp_path):
    """Frozen historical schema, created by the test itself.

    The retired legacy `Store` is not imported anywhere in the product or tests; the
    historical format is asserted as a literal so it stays reproducible after removal.
    """
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO users VALUES('owner-a','do-not-copy-name','do-not-copy-password')")
        db.execute("INSERT INTO users VALUES('owner-b','other','other-password')")
        for owner in ("owner-a", "owner-b"):
            document = "doc-"+owner
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)", (document, owner, "课程资料.pdf", "pdf", "hash", b"%PDF-original-"+owner.encode(), "ready", None, '[]', "2026-10-13T12:00:00+08:00"))
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?)", ("chunk-"+owner, document, 1, "page:2", "原始片段 "+owner))
            db.execute("INSERT INTO tasks(id,document,chunk,title,due,raw_date,state,reviewed,evidence,time_precision) VALUES(?,?,?,?,?,?,?,?,?,?)", ("task-"+owner, document, "chunk-"+owner, "提交报告", None, "下周", "pending", 0, '[{"locator":"page:2","quote":"下周"}]', "unknown"))
        db.execute("INSERT INTO campus_snapshots VALUES('owner-a',?)", (json.dumps({"profile": None, "courses": None, "grades": None}),))
        db.execute("INSERT INTO sessions VALUES('SECRET-SESSION','owner-a','SECRET-CSRF',100000000000)")
    return path


def test_inventory_is_read_only_and_does_not_expose_secrets(legacy):
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    result = inventory(legacy)
    assert result["owners"] == ["owner-a", "owner-b"]
    assert result["counts"]["documents"] == 2
    assert "campus_credentials" in result["excluded"]
    assert "do-not-copy" not in json.dumps(result)
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_reentrant_exact_mapping_and_rollback(legacy, tmp_path):
    store = CacheStore(tmp_path / "new.sqlite3")
    first = import_legacy(legacy, store, "owner-a", include_school_cache=True)
    assert not first["replayed"]
    assert import_legacy(legacy, store, "owner-a", include_school_cache=True)["replayed"]
    doc = read_imported_document(store, first["batch_id"], "doc-owner-a")
    assert doc["chunks"][0]["locator"] == "page:2"
    assert doc["tasks"][0]["due"] is None
    assert doc["tasks"][0]["time_precision"] == "unknown"
    assert not doc["tasks"][0]["reviewed"]
    assert imported_content(store, first["batch_id"], "doc-owner-a")[0] == b"%PDF-original-owner-a"
    with pytest.raises(ApiError):
        read_imported_document(store, first["batch_id"], "doc-owner-b")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM imported_records WHERE table_name='sessions'").fetchone()[0] == 0
        assert db.execute("SELECT owner FROM campus_snapshots").fetchone()[0] == "local"
    assert rollback_import(store, first["batch_id"])["removed"]
    assert not rollback_import(store, first["batch_id"])["removed"]
    with store.connect() as db:
        assert not db.execute("SELECT * FROM campus_snapshots").fetchall()
    assert not import_legacy(legacy, store, "owner-a", include_school_cache=True)["replayed"]


def test_rollback_preserves_newer_cache(legacy, tmp_path):
    store = CacheStore(tmp_path / "new.sqlite3")
    result = import_legacy(legacy, store, "owner-a", include_school_cache=True)
    with store.connect() as db:
        db.execute("UPDATE campus_snapshots SET snapshot='new observation'")
    with pytest.raises(ValueError, match="changed"):
        rollback_import(store, result["batch_id"])
    with store.connect() as db:
        assert db.execute("SELECT snapshot FROM campus_snapshots").fetchone()[0] == "new observation"
        assert db.execute("SELECT count(*) FROM import_batches").fetchone()[0] == 1


def test_unknown_tables_and_destination_identity_do_not_silently_disappear(legacy, tmp_path):
    store = CacheStore(tmp_path / "new.sqlite3")
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE personal_notes(id TEXT)")
    with pytest.raises(ValueError, match="personal_notes"):
        import_legacy(legacy, store, "owner-a")
    with sqlite3.connect(legacy) as db:
        db.execute("DROP TABLE personal_notes")
    with store.connect() as db:
        db.execute("INSERT INTO campus_snapshots VALUES('local','existing student')")
    with pytest.raises(ValueError, match="differs"):
        import_legacy(legacy, store, "owner-a", include_school_cache=True)
    with store.connect() as db:
        assert not db.execute("SELECT * FROM import_batches").fetchall()
    assert import_legacy(legacy, store, "owner-a")["counts"]["documents"] == 1


class CookieClient:
    def __init__(self):
        from http.cookiejar import CookieJar
        self.jar = CookieJar()
        self.passport_authenticated = False
        self.eta_authenticated = False
        self.closed = False
        self.authenticated = False

    def start_operation(self): pass
    def connect_zdbk(self):
        from connectors.campus.campus_common import CampusError
        if not any(c.name == "iPlanetDirectoryPro" and c.value == "controlled-cas" for c in self.jar):
            raise CampusError("AUTH_REQUIRED", "controlled authentication missing")
        self.authenticated = True
    def profiles(self, expected):
        return [{"data": {"student_id": "controlled-student", "college": "竺可桢学院"}, "source": "本科教务网 · 个人信息", "url": "https://zdbk.zju.edu.cn/jwglxt/"}], []
    def close(self): self.closed = True


def test_private_browser_cookie_scope_and_failed_adoption_keeps_session(tmp_path):
    workspace = CampusWorkspace(tmp_path / "workspace.sqlite3", CookieClient)
    try:
        cookies = [{"name": "iPlanetDirectoryPro", "value": "controlled-cas", "domain": ".zju.edu.cn", "path": "/", "expires": -1, "secure": True, "httpOnly": True},
                   {"name": "unrelated-secret", "value": "DO-NOT-COPY", "domain": "example.com", "path": "/", "expires": -1}]
        status = workspace.adopt_browser_cookies(cookies)
        assert status["connected"]
        assert "controlled-cas" not in json.dumps(status)
        client = workspace.manager.states["local"]["client"]
        assert len(list(client.jar)) == 1
        cookie = next(iter(client.jar))
        assert cookie.domain == "zjuam.zju.edu.cn" and not cookie.domain_specified
        with pytest.raises(ApiError): workspace.adopt_browser_cookies([])
        assert workspace.manager.states["local"]["client"] is client
        assert not client.closed
        reopened = CampusWorkspace(workspace.store.path, CookieClient)
        assert not reopened.status()["connected"]
        assert reopened.status()["snapshot"]["profile"]["data"]["student_id"] == "controlled-student"
        reopened.close()
    finally:
        workspace.close()


def test_migrated_query_returns_real_auth_required_not_empty_success(tmp_path):
    workspace = CampusWorkspace(tmp_path / "workspace.sqlite3", CookieClient)
    try:
        result = workspace.query("grades", {}, refresh=True)
        assert result["error"]["code"] == "AUTH_REQUIRED"
        assert result["freshness"] == "unknown"
        assert not result["records"]
        with pytest.raises(Exception):
            workspace.query("courses", {"owner": "other"})
    finally:
        workspace.close()


def test_new_campus_tools_read_same_workspace_and_validate_model_scope(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import campus_workspace
    from app.agent_bridge.tools import execute, ToolContext, schemas
    workspace = CampusWorkspace(tmp_path / "workspace.sqlite3", CookieClient)
    monkeypatch.setattr(campus_workspace, "get_campus_workspace", lambda: workspace)
    try:
        with Session(create_engine("sqlite:///:memory:")) as db:
            context = ToolContext("local", "controlled-conversation")
            result = execute("campus.sync", {"capability": "grades"}, db, context)
            assert result["status"] == "error"
            assert result["error"]["code"] == "AUTH_REQUIRED"
            with pytest.raises(ApiError): execute("campus.query", {"capability": "grades", "owner": "other"}, db, context)
            with pytest.raises(ApiError): execute("campus.status", {}, db, ToolContext("other", "conversation"))
            entry = execute("campus.webvpn.entry", {"service": "cas"}, db, context)
            assert entry["data"]["url"].startswith("https://webvpn.zju.edu.cn/https/")
            assert not entry["data"]["direct_connector_uses_webvpn"]
        spec = next(s for s in schemas() if s["name"] == "campus.sync")
        assert spec["effects"] == "write" and spec["input_schema"]["additionalProperties"] is False
    finally: workspace.close()


def test_new_router_uses_gateway_identity_and_bad_query_returns_400(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import campus
    from app.errors import error_response
    workspace = CampusWorkspace(tmp_path / "workspace.sqlite3", CookieClient)
    monkeypatch.setattr(campus, "get_campus_workspace", lambda: workspace)
    app = FastAPI()
    owner_calls = []
    def owner(): owner_calls.append(True); return "trusted-local-browser"
    campus.install_campus_router(app, owner)
    @app.exception_handler(ApiError)
    async def handler(request, exc): return error_response(request, exc.status_code, exc.code, exc.message, exc.retryable, exc.details)
    try:
        client = TestClient(app)
        assert client.get("/api/v1/agent/campus/status").json()["school"] == "浙江大学"
        invalid = client.post("/api/v1/agent/campus/query", json={"capability": "courses", "query": {"owner": "other"}})
        assert invalid.status_code == 400
        assert client.post("/api/v1/agent/campus/sync", json={"capability": "grades"}).json()["error"]["code"] == "AUTH_REQUIRED"
        assert len(owner_calls) == 3
    finally: workspace.close()


def test_notice_to_material_read_and_real_task_engine_is_versioned(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app import campus_workspace, models as m
    from app.db import Base
    from app.agent_bridge.tools import execute, ToolContext
    from app.build_engine import run_build_tasks
    workspace = CampusWorkspace(tmp_path / "workspace.sqlite3", CookieClient)
    monkeypatch.setattr(campus_workspace, "get_campus_workspace", lambda: workspace)
    engine = create_engine("sqlite:///"+(tmp_path / "domain.sqlite3").as_posix())
    Base.metadata.create_all(engine)
    source_url = "https://webplus.zju.edu.cn/_s597/2026/1010/c78853a1234567/page.psp"
    body = "课程报告：请阅读材料并提交报告。截止日期未知。"
    def cache(text, observed="2026-10-10T12:00:00+08:00"):
        notice = {"id": "ckc:1234567", "college": "竺可桢学院", "title": "课程报告", "published_at": "2026-10-10",
            "body": text, "body_sha256": hashlib.sha256(text.encode()).hexdigest(), "source": source_url,
            "fetched_at": observed, "has_unread_content": True, "attachments": []}
        workspace.manager.capability_repository.write("local", "ckc_public", "college_notices", '{}', {"records": [notice]})
    cache(body)
    try:
        with Session(engine) as db:
            db.add(m.Course(id="crs_controlled", title="测试课程")); db.commit()
            ctx = ToolContext("local", "controlled-conversation", frozenset({"crs_controlled"}))
            result = execute("campus.notice.import", {"course_id": "crs_controlled", "record_id": "ckc:1234567"}, db, ctx)
            resource = result["data"]["resource"]
            assert resource["parse_status"] == "ready" and resource["chunk_count"] == 1
            assert resource["source_refs"][0]["observed_at"] == "2026-10-10T04:00:00Z"
            assert result["data"]["has_unread_content"]
            assert result["data"]["job"]["warnings"][0]["code"] == "UNREAD_ATTACHMENTS"
            read = execute("materials.read", {"resource_id": resource["id"], "expected_version": 1}, db, ctx)
            assert read["data"]["items"][0]["text"] == body
            replay = execute("campus.notice.import", {"course_id": "crs_controlled", "record_id": "ckc:1234567"}, db, ctx)
            assert replay["data"]["replayed"]
            outcome = run_build_tasks(db, course_id="crs_controlled")
            db.commit()
            assert outcome.task_count > 0
            cache(body+" 补充阅读要求。", "2026-10-11T12:00:00+08:00")
            changed = execute("campus.notice.import", {"course_id": "crs_controlled", "record_id": "ckc:1234567"}, db, ctx)
            assert changed["data"]["resource"]["version"] == 2
            assert len(list(db.scalars(select(m.ResourceVersion)))) == 2
            assert len(list(db.scalars(select(m.ContentChunk)))) == 2
            with pytest.raises(ApiError, match="版本"):
                execute("materials.read", {"resource_id": resource["id"], "expected_version": 1}, db, ctx)
    finally:
        workspace.close(); engine.dispose()

