"""Offline auth batch continuations: synthetic adapters + private in-memory SQLite only."""
from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import worker
from app.db import Base, get_db
from app.errors import ApiError, error_response
from app.models import Connection, Course, Job, Resource, SourceRef, SyncCursor, utc_now
from app.routers import connections, jobs
from app.sync_engine import AdapterError, _CHECK_REGISTRY, _REGISTRY

PLATFORM = "authbatch"
CONNECTION = "conn_authbatch"
COURSES = ["course_A", "course_B", "course_C"]
PDF = b"%PDF-1.4\n% synthetic attachment\n"
DIGEST = "sha256:" + hashlib.sha256(PDF).hexdigest()


class BatchAdapter:
    def __init__(self):
        self.calls = []
        self.fetch_calls = []
        self.expired_course = "course_B"
        self.expired_resource = None
        self.check_state = "ok"
        self.resources = {}
        self.protocol_factory = lambda **_: self
        self.before_course = None
        self.before_fetch = None

    def __call__(self, connection, request):
        course = request["course_id"]
        self.calls.append(dict(request))
        if self.before_course:
            self.before_course(course)
        if course == self.expired_course:
            raise AdapterError("AUTH_EXPIRED", "synthetic expired", retryable=True)
        # Deliberately return no old resources after cursor advancement: only the
        # durable attachment phase, not another incremental sync, can recover them.
        resources = self.resources.get(course, []) if request["since_cursor"] is None else []
        return {
            "platform": PLATFORM, "connection_id": CONNECTION, "course_id": course,
            "mode": "incremental", "cursor": {"previous": request["since_cursor"], "next": "cursor:" + course},
            "upserts": {"resources": resources, "assignments": [], "recordings": []},
            "deletions": [], "errors": [], "stats": {}, "observed_at": "2026-10-15T00:00:00Z",
        }

    def check(self, _):
        return {"platform": PLATFORM, "state": self.check_state, "checked_at": "2026-10-15T00:00:00Z"}

    def fetch_resource_bytes(self, external_id):
        self.fetch_calls.append(external_id)
        if self.before_fetch:
            self.before_fetch(external_id)
        if external_id == self.expired_resource:
            raise AdapterError("AUTH_EXPIRED", "synthetic file expired", retryable=False)
        return {"status": "ok", "filename": external_id + ".pdf", "size": len(PDF),
                "sha256": DIGEST, "data_base64": base64.b64encode(PDF).decode()}


def resource_item(external_id):
    return {"kind": "handout", "title": external_id + ".pdf", "version": 1,
            "content_hash": DIGEST, "access_level": "full", "mime_type": "application/pdf",
            "source_ref": {"platform": PLATFORM, "external_id": external_id,
                           "observed_at": "2026-10-15T00:00:00Z"}}


@pytest.fixture()
def batch(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(Connection(id=CONNECTION, platform=PLATFORM, display_name="synthetic", status="ok"))
    for course in COURSES:
        db.add(Course(id=course, title=course))
        db.add(SourceRef(owner_type="course", owner_id=course, platform=PLATFORM,
                         external_id=course, observed_at=utc_now()))
    db.add(Job(id="job_batch", type="sync", status="queued", errors=[], result_refs=[],
               payload={"connection_id": CONNECTION, "course_ids": list(COURSES), "full": False}))
    db.commit()
    adapter = BatchAdapter()
    monkeypatch.setitem(_REGISTRY, PLATFORM, adapter)
    monkeypatch.setitem(_CHECK_REGISTRY, PLATFORM, adapter.check)
    monkeypatch.setattr(worker, "enqueue_build_tasks_job", lambda *a, **k: None)
    monkeypatch.setattr("app.config.FILES_DIR", tmp_path / "files")
    runtime_calls = []
    from app.agent_bridge import runtime
    monkeypatch.setattr(runtime, "get_runtime", lambda: SimpleNamespace(
        resume_authenticated_tasks=lambda: runtime_calls.append("resume")))
    api = FastAPI()
    api.add_exception_handler(ApiError, lambda request, exc: error_response(
        request, exc.status_code, exc.code, exc.message, exc.retryable, exc.details))
    api.include_router(connections.router, prefix="/api/v1")
    api.include_router(jobs.router, prefix="/api/v1")
    api.dependency_overrides[get_db] = lambda: db
    with TestClient(api) as client:
        yield SimpleNamespace(db=db, adapter=adapter, client=client, runtime_calls=runtime_calls,
                              job=lambda: db.get(Job, "job_batch"))
    db.close()
    engine.dispose()


def run_sync(batch, job_id="job_batch"):
    job = batch.db.get(Job, job_id)
    assert job.status == "queued"
    job.status = "running"
    batch.db.commit()
    worker.process_job(batch.db, job)
    batch.db.refresh(job)
    return job


def check(batch):
    response = batch.client.post(f"/api/v1/connections/{CONNECTION}/check")
    assert response.status_code == 200, response.text
    return response.json()


def test_expired_batch_persists_B_and_untouched_C_and_retry_scope(batch):
    job = run_sync(batch)
    assert job.status == "partial_failed"
    assert [r["course_id"] for r in batch.adapter.calls] == COURSES[:2]
    assert job.payload["pending_course_ids"] == COURSES[1:]
    assert job.payload["remaining_courses"] == COURSES[1:]
    assert job.phase == "auth_required"
    assert job.errors[0]["code"] == "AUTH_EXPIRED" and job.errors[0]["retryable"] is False
    payload = jobs._retry_payload(batch.db, job)
    assert payload["course_ids"] == COURSES[1:]
    assert payload["pending_course_ids"] == COURSES[1:]
    assert batch.db.get(SyncCursor, (CONNECTION, COURSES[0])).cursor == "cursor:course_A"


def test_successful_check_resumes_only_B_C_same_job_and_is_idempotent(batch):
    run_sync(batch)
    batch.adapter.expired_course = None
    assert check(batch)["status"] == "ok"
    assert batch.job().status == "queued"
    assert check(batch)["status"] == "ok"
    assert len(list(batch.db.scalars(select(Job).where(Job.type == "sync")))) == 1
    assert run_sync(batch).status == "succeeded"
    assert [r["course_id"] for r in batch.adapter.calls] == [*COURSES[:2], *COURSES[1:]]
    assert batch.job().payload["pending_course_ids"] == []
    assert check(batch)["status"] == "ok"
    assert batch.job().status == "succeeded"
    assert len(batch.runtime_calls) == 3


def test_still_expired_check_and_false_positive_ok_stop_without_auth_loop(batch):
    run_sync(batch)
    batch.adapter.check_state = "auth_required"
    check(batch)
    assert batch.job().status == "partial_failed" and len(batch.adapter.calls) == 2
    assert not batch.runtime_calls
    batch.adapter.check_state = "ok"
    check(batch)
    assert run_sync(batch).status == "partial_failed"
    assert batch.job().payload["pending_course_ids"] == COURSES[1:]
    assert len(batch.adapter.calls) == 3
    assert worker.resume_auth_sync_jobs(batch.db, batch.db.get(Connection, CONNECTION)) == 0
    assert batch.job().status == "partial_failed"


def test_worker_restart_continues_checkpoint_without_repeating_A(batch):
    batch.adapter.expired_course = None
    def crash(course):
        if course == COURSES[1]:
            raise KeyboardInterrupt("synthetic worker shutdown")
    batch.adapter.before_course = crash
    with pytest.raises(KeyboardInterrupt):
        run_sync(batch)
    assert batch.job().payload["pending_course_ids"] == COURSES[1:]
    assert worker.requeue_stale(batch.db) == 1
    batch.adapter.before_course = None
    assert run_sync(batch).status == "succeeded"
    assert [r["course_id"] for r in batch.adapter.calls].count(COURSES[0]) == 1


def test_check_committed_while_paused_job_running_then_restart_continues(batch):
    run_sync(batch)
    batch.job().status = "running"  # AUTH checkpoint committed before _finish.
    batch.db.commit()
    batch.adapter.expired_course = None
    check(batch)  # Hook must not mutate running status.
    assert batch.job().status == "running"
    assert worker.requeue_stale(batch.db) == 1
    assert run_sync(batch).status == "succeeded"
    assert [r["course_id"] for r in batch.adapter.calls] == [*COURSES[:2], *COURSES[1:]]


def test_check_finish_race_requeues_terminal_checkpoint_without_restart(batch):
    run_sync(batch)
    batch.job().status = "running"
    batch.db.commit()
    batch.adapter.expired_course = None
    check(batch)
    assert batch.job().status == "running"
    worker._finish(batch.db, batch.job(), "partial_failed", list(batch.job().errors))
    assert batch.job().status == "queued"
    assert run_sync(batch).status == "succeeded"
    assert [r["course_id"] for r in batch.adapter.calls] == [*COURSES[:2], *COURSES[1:]]


def test_worker_restart_recovers_postcheck_hook_gap(batch):
    run_sync(batch)
    # Successful validated check already committed; process died before the hook.
    batch.db.get(Connection, CONNECTION).status = "ok"
    batch.db.commit()
    batch.adapter.expired_course = None
    assert worker.requeue_stale(batch.db) == 1
    assert run_sync(batch).status == "succeeded"
    assert [r["course_id"] for r in batch.adapter.calls] == [*COURSES[:2], *COURSES[1:]]


def test_queued_continuation_does_not_probe_auth_required_connection(batch):
    run_sync(batch)
    batch.job().status = "queued"  # Simulate a previously queued retry/restart.
    batch.db.commit()
    assert run_sync(batch).status == "partial_failed"
    assert len(batch.adapter.calls) == 2
    assert batch.job().payload["pending_course_ids"] == COURSES[1:]


def test_disabled_scope_and_invalid_ancestry_are_not_resumed(batch):
    run_sync(batch)
    connection = batch.db.get(Connection, CONNECTION)
    connection.status = "disabled"
    batch.db.commit()
    assert worker.resume_auth_sync_jobs(batch.db, connection) == 0
    assert batch.client.post(f"/api/v1/connections/{CONNECTION}/check").status_code == 409
    connection.status = "ok"
    batch.db.add(Connection(id="conn_other", platform="other", display_name="other", status="ok"))
    batch.db.add(Job(id="job_foreign_parent", type="sync", status="failed",
                     payload={"connection_id": "conn_other", "course_ids": list(COURSES)}))
    batch.job().payload = {**batch.job().payload, "parent_job_id": "job_foreign_parent"}
    batch.db.commit()
    assert worker.resume_auth_sync_jobs(batch.db, connection) == 0
    assert batch.job().status == "partial_failed"


def test_manual_retry_ancestry_resumes_only_leaf_without_duplicate_parent(batch):
    run_sync(batch)
    response = batch.client.post("/api/v1/jobs/job_batch/retry")
    assert response.status_code == 202
    leaf_id = response.json()["job"]["id"]
    leaf = batch.db.get(Job, leaf_id)
    assert leaf.payload["parent_job_id"] == "job_batch"
    assert leaf.payload["course_ids"] == COURSES[1:]
    assert run_sync(batch, leaf_id).status == "failed"  # No authenticated check yet.
    assert len(batch.adapter.calls) == 2
    batch.adapter.expired_course = None
    check(batch)
    assert batch.job().status == "partial_failed"
    assert batch.db.get(Job, leaf_id).status == "queued"
    check(batch)
    assert run_sync(batch, leaf_id).status == "succeeded"
    assert [r["course_id"] for r in batch.adapter.calls] == [*COURSES[:2], *COURSES[1:]]


def test_attachment_auth_retains_later_files_and_courses_without_repeat(batch):
    batch.adapter.expired_course = None
    batch.adapter.resources[COURSES[0]] = [resource_item(name) for name in ("file_A", "file_B", "file_C")]
    batch.adapter.expired_resource = "file_B"
    assert run_sync(batch).status == "partial_failed"
    job = batch.job()
    assert job.payload["pending_course_ids"] == COURSES
    checkpoint = job.payload["course_checkpoints"][COURSES[0]]
    assert checkpoint["phase"] == "attachments"
    assert [r["external_id"] for r in checkpoint["remaining_resources"]] == ["file_B", "file_C"]
    assert batch.adapter.fetch_calls == ["file_A", "file_B"]
    assert batch.db.get(SyncCursor, (CONNECTION, COURSES[0])).cursor == "cursor:course_A"
    batch.adapter.expired_resource = None
    check(batch)
    check(batch)
    assert run_sync(batch).status == "succeeded"
    assert batch.adapter.fetch_calls == ["file_A", "file_B", "file_B", "file_C"]
    assert [r["course_id"] for r in batch.adapter.calls] == COURSES
    assert all(r.file_id for r in batch.db.scalars(select(Resource)))
    assert len(list(batch.db.scalars(select(Job).where(Job.type == "parse")))) == 3


def test_attachment_committed_before_crash_is_not_downloaded_twice(batch, monkeypatch):
    batch.adapter.expired_course = None
    batch.adapter.resources[COURSES[0]] = [resource_item("file_A"), resource_item("file_B")]
    original = worker._fetch_synced_document
    def crash_after_commit(*args):
        original(*args)
        raise KeyboardInterrupt("synthetic crash after attachment commit")
    monkeypatch.setattr(worker, "_fetch_synced_document", crash_after_commit)
    with pytest.raises(KeyboardInterrupt):
        run_sync(batch)
    assert worker.requeue_stale(batch.db) == 1
    monkeypatch.setattr(worker, "_fetch_synced_document", original)
    assert run_sync(batch).status == "succeeded"
    assert batch.adapter.fetch_calls == ["file_A", "file_B"]
    assert [r["course_id"] for r in batch.adapter.calls] == COURSES


def test_enable_validated_ok_resumes_disabled_auth_checkpoint(batch):
    run_sync(batch)
    batch.db.get(Connection, CONNECTION).status = "disabled"
    batch.db.commit()
    batch.adapter.expired_course = None
    response = batch.client.post(f"/api/v1/connections/{CONNECTION}/enable")
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert batch.job().status == "queued" and batch.runtime_calls == ["resume"]
    assert run_sync(batch).status == "succeeded"


def test_cancelled_pending_job_is_never_resumed(batch):
    run_sync(batch)
    batch.job().cancel_requested = True
    batch.db.commit()
    check(batch)
    assert batch.job().status == "partial_failed" and len(batch.adapter.calls) == 2


def test_failure_in_sync_resume_does_not_undo_check_or_skip_runtime_hook(batch, monkeypatch):
    run_sync(batch)
    def broken_resume(*_):
        raise RuntimeError("synthetic helper unavailable")
    monkeypatch.setattr(worker, "resume_auth_sync_jobs", broken_resume)
    assert check(batch)["status"] == "ok"
    assert batch.db.get(Connection, CONNECTION).status == "ok"
    assert batch.job().status == "partial_failed" and batch.runtime_calls == ["resume"]


def test_attachment_factory_auth_keeps_phase_and_unattempted_files(batch):
    batch.adapter.expired_course = None
    batch.adapter.resources[COURSES[0]] = [resource_item("file_A"), resource_item("file_B")]
    def expired_factory(**_):
        raise AdapterError("AUTH_EXPIRED", "synthetic factory expired", retryable=False)
    batch.adapter.protocol_factory = expired_factory
    assert run_sync(batch).status == "partial_failed"
    assert batch.job().payload["pending_course_ids"] == COURSES
    assert len(batch.job().payload["course_checkpoints"][COURSES[0]]["remaining_resources"]) == 2
    batch.adapter.protocol_factory = lambda **_: batch.adapter
    check(batch)
    assert run_sync(batch).status == "succeeded"
    assert batch.adapter.fetch_calls == ["file_A", "file_B"]
    assert [r["course_id"] for r in batch.adapter.calls] == COURSES


def test_disable_between_attachments_stops_and_retains_remaining(batch):
    batch.adapter.expired_course = None
    batch.adapter.resources[COURSES[0]] = [resource_item("file_A"), resource_item("file_B")]
    def disable(external_id):
        if external_id == "file_A":
            batch.db.get(Connection, CONNECTION).status = "disabled"
            batch.db.commit()
    batch.adapter.before_fetch = disable
    assert run_sync(batch).status == "failed"
    assert batch.adapter.fetch_calls == ["file_A"]
    assert [r["external_id"] for r in batch.job().payload["course_checkpoints"][COURSES[0]]["remaining_resources"]] == ["file_B"]
    assert worker.resume_auth_sync_jobs(batch.db, batch.db.get(Connection, CONNECTION)) == 0


def test_hook_committed_success_and_runtime_failure_is_safe(batch, monkeypatch):
    run_sync(batch)
    seen = []
    original = worker.resume_auth_sync_jobs
    def resume(db, connection):
        with Session(db.bind) as independent:
            assert independent.get(Connection, connection.id).status == "ok"
        seen.append(connection.id)
        return original(db, connection)
    monkeypatch.setattr(worker, "resume_auth_sync_jobs", resume)
    from app.agent_bridge import runtime
    def broken_runtime():
        raise RuntimeError("synthetic unavailable runtime")
    monkeypatch.setattr(runtime, "get_runtime", broken_runtime)
    batch.adapter.check_state = "network_error"
    assert check(batch)["status"] == "network_error" and not seen
    batch.adapter.check_state = "ok"
    assert check(batch)["status"] == "ok"
    assert seen == [CONNECTION] and batch.job().status == "queued"
