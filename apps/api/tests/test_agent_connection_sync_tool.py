"""Agent-initiated platform sync: the capability that was missing for 学在浙大.

Synthetic, in-memory SQLite only: no network, no real connection, no school request.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models as m
from app.agent_bridge import tools
from app.agent_bridge.tools import ToolContext
from app.db import Base
from app.errors import ApiError
from app.models import Connection, Course, Job, SourceRef, utc_now

XZZD, ZYKT = "conn_xzzd", "conn_zykt"
COURSE = "course_math"


class _NullRegistry:
    def __getattr__(self, name):
        raise AssertionError("the tool must not touch the network: " + name)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Connection(id=XZZD, platform="xzzd", display_name="学在浙大", status="ok"))
    session.add(Connection(id=ZYKT, platform="zykt", display_name="智云课堂", status="auth_required"))
    session.add(Course(id=COURSE, title="高等数学"))
    session.add(SourceRef(owner_type="course", owner_id=COURSE, platform="xzzd",
                          external_id="platform-course-1", observed_at=utc_now()))
    session.commit()
    yield session
    session.close()


def ctx(**over):
    values = {"principal": "local", "conversation_id": "conv_sync", "course_ids": frozenset({COURSE}),
              "connection_ids": frozenset({XZZD, ZYKT}), "allow_uncoursed_tasks": True, "request_key": "turn:call"}
    values.update(over)
    return ToolContext(**values)


def enqueued(db):
    return list(db.execute(select(Job).where(Job.type == "sync")).scalars())


def test_a_connected_platform_is_synced_instead_of_answering_from_an_empty_cache(db):
    result = tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx())
    assert result["status"] == "queued"
    assert result["job_id"] and result["error"] is None
    assert result["data"]["jobs"][0]["platform"] == "xzzd"
    jobs = enqueued(db)
    assert len(jobs) == 1
    # The job is the same reviewed read-only sync the UI enqueues, scoped to bound courses.
    assert jobs[0].payload == {"connection_id": XZZD, "course_ids": [COURSE], "full": False, "kinds": None}
    assert any("jobs.read" in warning for warning in result["warnings"])


def test_a_second_call_while_one_is_active_reuses_the_job(db):
    first = tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx())
    second = tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx(request_key="turn:call2"))
    assert second["job_id"] == first["job_id"]
    assert second["data"]["jobs"][0]["reused"] is True
    assert len(enqueued(db)) == 1


def test_assignment_only_sync_records_a_narrow_scope_and_does_not_reuse_full_sync(db):
    first = tools.execute("connections.sync", {"platform": "xzzd", "kinds": ["assignments"]}, db, ctx())
    job = enqueued(db)[0]
    assert job.payload["kinds"] == ["assignments"]
    assert first["data"]["jobs"][0]["status"] == "queued"
    with pytest.raises(ApiError) as error:
        tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx(request_key="turn:full"))
    assert error.value.code == "SYNC_SCOPE_CONFLICT"
    assert len(enqueued(db)) == 1


def test_no_argument_refreshes_every_usable_platform_and_reports_the_one_needing_login(db):
    result = tools.execute("connections.sync", {}, db, ctx())
    assert result["status"] == "queued"
    assert [job["platform"] for job in result["data"]["jobs"]] == ["xzzd"]
    assert result["data"]["stats"]["requires_login"] == ["zykt"]
    assert any("zykt" in warning for warning in result["warnings"])


def test_a_dead_platform_session_asks_for_one_manual_login_instead_of_faking_data(db):
    db.query(Connection).filter(Connection.id == XZZD).update({"status": "auth_required"})
    db.commit()
    with pytest.raises(ApiError) as error:
        tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx())
    assert error.value.code == "AUTH_REQUIRED" and error.value.status_code == 409
    assert enqueued(db) == []


def test_a_connection_outside_the_conversation_grant_is_denied(db):
    with pytest.raises(ApiError) as error:
        tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx(connection_ids=frozenset()))
    assert error.value.code == "SCOPE_DENIED"
    assert enqueued(db) == []


def test_the_agent_can_read_and_cancel_the_sync_job_it_started(db):
    started = tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx())
    read = tools.execute("jobs.read", {"job_id": started["job_id"]}, db, ctx())
    assert read["data"]["status"] == "queued" and read["job_id"] == started["job_id"]
    cancelled = tools.execute("jobs.cancel", {"job_id": started["job_id"]}, db, ctx())
    assert cancelled["data"]["job"]["status"] in {"cancelled", "queued"}


def test_an_unrelated_sync_job_stays_out_of_scope(db):
    other = Job(id="job_other", type="sync", status="queued", errors=[], result_refs=[],
                payload={"connection_id": "conn_someone_else", "course_ids": [COURSE]})
    db.add(other)
    db.commit()
    with pytest.raises(ApiError) as error:
        tools.execute("jobs.read", {"job_id": "job_other"}, db, ctx())
    assert error.value.code == "SCOPE_DENIED"


def test_wait_seconds_returns_the_terminal_state_when_the_worker_finishes(db):
    started = tools.execute("connections.sync", {"platform": "xzzd", "wait_seconds": 3}, db, ctx(request_key="turn:wait"))
    # No worker runs here, so the bounded wait returns the still-queued job instead of hanging.
    assert started["data"]["jobs"][0]["status"] == "queued"
    assert started["status"] == "queued"


class _FakeAdapter:
    """Stands in for the 学在浙大 platform adapter: discovery only, no network."""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def list_courses(self, cursor=None, term=None):
        self.calls.append({"cursor": cursor, "term": term})
        return {"items": list(self.items), "next_cursor": None}


@pytest.fixture()
def adapter(monkeypatch):
    from app import connectors_registry
    fake = _FakeAdapter([
        {"external_id": "zc-1001", "title": "高等数学", "term": "2026秋", "teacher": "张老师"},
        {"external_id": "zc-1002", "title": "数据结构", "term": "2026秋", "teacher": "李老师"},
    ])
    monkeypatch.setattr(connectors_registry, "protocol_adapter", lambda platform, connection_id: fake)
    return fake


def test_the_agent_can_discover_the_courses_that_exist_on_the_platform(db, adapter):
    result = tools.execute("connections.courses", {"platform": "xzzd"}, db, ctx())
    assert result["status"] == "ok" and result["error"] is None
    assert [item["external_id"] for item in result["data"]["courses"]] == ["zc-1001", "zc-1002"]
    assert {item["platform"] for item in result["data"]["courses"]} == {"xzzd"}
    assert result["evidence"] and result["evidence"][0]["source_id"] == "zc-1001"


def test_the_agent_can_bind_platform_courses_so_they_can_be_synced(db, adapter):
    assert db.scalars(select(Course)).all() == [db.get(Course, COURSE)]
    result = tools.execute("connections.courses.import", {"platform": "xzzd", "all": True}, db, ctx())
    assert result["error"] is None
    titles = {item["title"] for item in result["data"]["courses"]}
    assert titles == {"高等数学", "数据结构"}
    bound = db.scalars(select(SourceRef).where(SourceRef.platform == "xzzd")).all()
    assert {ref.external_id for ref in bound} >= {"zc-1001", "zc-1002"}
    assert any("connections.sync" in warning for warning in result["warnings"])
    # Re-importing the same platform courses must reuse the existing local rows.
    again = tools.execute("connections.courses.import", {"platform": "xzzd", "all": True}, db, ctx())
    assert {item["id"] for item in again["data"]["courses"]} == {item["id"] for item in result["data"]["courses"]}


def test_importing_an_unknown_course_id_reports_it_instead_of_inventing_a_course(db, adapter):
    result = tools.execute("connections.courses.import",
                           {"platform": "xzzd", "external_ids": ["zc-1001", "zc-missing"]}, db, ctx())
    assert [item["title"] for item in result["data"]["courses"]] == ["高等数学"]
    assert any("zc-missing" in warning for warning in result["warnings"])
    assert db.scalars(select(SourceRef).where(SourceRef.external_id == "zc-missing")).all() == []


def test_import_requires_an_explicit_selection(db, adapter):
    with pytest.raises(ApiError) as error:
        tools.execute("connections.courses.import", {"platform": "xzzd"}, db, ctx())
    assert error.value.code == "INVALID_INPUT"


def test_discovery_outside_the_conversation_grant_is_denied(db, adapter):
    with pytest.raises(ApiError) as error:
        tools.execute("connections.courses", {"platform": "xzzd"}, db, ctx(connection_ids=frozenset()))
    assert error.value.code == "SCOPE_DENIED"


def test_import_accepts_the_platform_adapter_shape_keyed_by_course_id(db, monkeypatch):
    """Regression: the real adapter returns platform course records keyed by `id`.

    Assuming `external_id` here raised a KeyError, which surfaced as HTTP 500 from
    /internal/tool and aborted the agent's whole bootstrap.
    """
    from app import connectors_registry
    platform_items = [
        {"id": "99395", "title": "高等数学", "term": "2026秋", "teacher": "张老师",
         "source_refs": [{"platform": "xzzd", "external_id": "99395", "observed_at": utc_now()}]},
        {"id": "100685", "title": "数据结构", "term": "2026秋", "teacher": "李老师",
         "source_refs": [{"platform": "xzzd", "external_id": "100685", "observed_at": utc_now()}]},
    ]
    monkeypatch.setattr(connectors_registry, "protocol_adapter",
                        lambda platform, connection_id: _FakeAdapter(platform_items))
    discovered = tools.execute("connections.courses", {"platform": "xzzd"}, db, ctx())
    assert [item["id"] for item in discovered["data"]["courses"]] == ["99395", "100685"]
    imported = tools.execute("connections.courses.import", {"platform": "xzzd", "all": True}, db, ctx())
    assert imported["status"] != "error"
    assert {item["title"] for item in imported["data"]["courses"]} == {"高等数学", "数据结构"}
    assert {ref.external_id for ref in db.scalars(select(SourceRef).where(SourceRef.platform == "xzzd")).all()} >= {
        "99395", "100685"}
    # Now that courses are bound, the sync the agent asks for next can actually run.
    synced = tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx(request_key="turn:after-import"))
    assert synced["status"] == "queued" and synced["error"] is None


def test_a_rejected_caller_parameter_never_blocks_the_platform(db):
    """Regression: asking for a filter the platform rejects marked the connection blocked."""
    from app.sync_engine import AdapterError, record_check_failure
    record_check_failure(db.get(Connection, XZZD),
                        AdapterError("VALIDATION_FAILED", "term filter not supported", retryable=False))
    db.commit()
    connection = db.get(Connection, XZZD)
    assert connection.status == "ok", "a caller-parameter error says nothing about the connection"
    assert connection.last_error["code"] == "VALIDATION_FAILED"
    # A genuine permission refusal still blocks it.
    record_check_failure(db.get(Connection, XZZD), AdapterError("PERMISSION_DENIED", "denied", retryable=False))
    db.commit()
    assert db.get(Connection, XZZD).status == "blocked"


def test_the_discovery_tool_does_not_offer_an_unsupported_term_filter():
    for name in ("connections.courses", "connections.courses.import"):
        spec = next(item for item in tools.schemas() if item["name"] == name)
        assert "term" not in spec["input_schema"]["properties"]
        assert "cursor" in spec["input_schema"]["properties"]


def test_the_platform_sync_tool_is_registered_with_a_bound_write_effect():
    spec = next(item for item in tools.schemas() if item["name"] == "connections.sync")
    assert spec["model_name"] == "campus_connections_sync"
    assert spec["effects"] == "write"  # it changes local data and calls the school's read APIs
    assert spec["input_schema"]["additionalProperties"] is False
    assert set(spec["input_schema"]["properties"]) == {"platform", "connection_id", "kinds", "wait_seconds"}
