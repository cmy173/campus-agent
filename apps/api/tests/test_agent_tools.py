"""Independent synthetic DB: strict contracts, scoping, date precision and evidence."""
import base64
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models as m
from app.agent_bridge.tools import SPECS, ToolContext, execute, schemas
from app.agent_bridge.review_actions import job_wait_window
from app.db import Base
from app.errors import ApiError
from connectors.campus.campus_common import CampusError


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([m.Course(id="crs_a", title="微积分", term="2026秋"),
                         m.Course(id="crs_b", title="另一账号课程", term="2026秋")])
        session.flush()
        session.add_all([
            m.Assignment(id="asg_a", course_id="crs_a", title="实验报告", instructions="提交正文和数据附件", due_precision="unknown", revision=1),
            m.Assignment(id="asg_b", course_id="crs_b", title="私有作业", due_precision="unknown", revision=1),
            m.Resource(id="res_a", course_id="crs_a", kind="document", title="实验说明", version=2, parse_status="ready"),
            m.Resource(id="res_b", course_id="crs_b", kind="document", title="私有资料", version=1, parse_status="ready"),
        ])
        session.flush()
        session.add_all([
            m.ContentChunk(id="chk_a", resource_id="res_a", resource_version=2, ordinal=0, text="实验报告需包含方法和数据附件。", locator={"paragraph":1}, provenance={"kind":"synthetic"}),
            m.ContentChunk(id="chk_b", resource_id="res_b", resource_version=1, ordinal=0, text="私有内容", locator={"paragraph":1}, provenance={"kind":"synthetic"}),
            m.Job(id="job_a", type="parse", status="queued", payload={"secret":"not-in-result"}),
        ])
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def context():
    return ToolContext("local", "conv_a", frozenset({"crs_a"}), job_ids=frozenset({"job_a"}))


@pytest.mark.parametrize("reference", ["unassigned", "warning"])
def test_plan_cannot_disclose_foreign_task_in_unscheduled_or_warning(db, context, reference):
    db.add(m.LearningTask(id="private_task", course_id="crs_b", type="reading", title="私有学习任务", estimated_minutes=30))
    plan = m.Plan(id="plan_private", version=1, input_revision="synthetic", horizon_start=date(2026, 10, 13),
                  horizon_end=date(2026, 10, 19), summary={}, status="candidate")
    db.add(plan)
    db.flush()
    if reference == "unassigned":
        db.add(m.PlanUnassigned(plan_id=plan.id, task_id="private_task", reason_code="no_availability", message="私有原因"))
    else:
        db.add(m.PlanWarning(plan_id=plan.id, code="synthetic", message="私有警告", task_ids=["private_task"]))
    db.commit()
    with pytest.raises(ApiError) as error:
        execute("plans.read", {"plan_id": plan.id}, db, context)
    assert error.value.code == "SCOPE_DENIED"


def test_inventory_is_exact_and_inputs_closed():
    assert len(SPECS) == 34
    assert len({s["model_name"] for s in schemas()}) == 34
    assert {'campus.status', 'campus.query', 'campus.sync', 'campus.academics', 'campus.notice.read',
            'campus.imported.read', 'campus.webvpn.entry', 'campus.notice.import'} <= set(SPECS)
    assert 'campus.credentials.status' in SPECS
    assert 'assignments.next_by_course' in SPECS
    # The agent must be able to discover/import courses from 学在浙大 (and refresh them)
    # instead of only searching whatever happened to be imported locally.
    assert {'connections.courses', 'connections.courses.import', 'connections.sync'} <= set(SPECS)
    # Round 3: a fixed trusted document exporter and the review-paper business chain, both
    # still strict-schema and server-scoped.
    assert {'artifacts.export', 'reviews.preflight', 'reviews.generate',
            'reviews.read', 'reviews.export'} <= set(SPECS)
    assert all(s["input_schema"]["additionalProperties"] is False for s in schemas())


def test_status_tool_descriptions_limit_personal_state_reads_to_relevant_tasks():
    specs = {item["name"]: item for item in schemas()}
    for name in ("campus.status", "connections.status"):
        description = specs[name]["description"]
        assert "普通校园知识" in description
        assert "不要读取个人" in description
        assert "仅当用户询问" in description
        assert "不要先调用本工具" in description
        assert "不能证明某项缓存存在、完整或最新" in description


def test_campus_query_and_sync_descriptions_constrain_unverified_arguments():
    for name in ("campus.query", "campus.sync"):
        description = SPECS[name][1]
        assert "不要臆造" in description
    assert "query保持空对象或省略" in SPECS["campus.query"][1]
    assert "不支持wait_seconds" in SPECS["campus.sync"][1]
    assert "直接调用一次campus.sync(capability=notices)" in SPECS["campus.sync"][1]
    assert "通知标题原文作为Markdown链接文本" in SPECS["campus.sync"][1]
    assert "source必须逐字取自对应记录" in SPECS["campus.sync"][1]


def test_college_notice_read_contract_limits_public_notice_summaries_and_returns_recovery_guidance(db, context, monkeypatch):
    description = SPECS["campus.notice.read"][1]
    assert "capability=college_notices" in description
    assert "校级notices公开摘要不支持正文读取" in description
    assert "不要调用或重试" in description

    class Service:
        def cached_college_notice(self, _user, _record_id):
            raise CampusError("UNSUPPORTED", "通知记录标识无效")

    workspace = SimpleNamespace(manager=SimpleNamespace(service=Service()))
    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: workspace)
    with pytest.raises(ApiError) as error:
        execute("campus.notice.read", {"record_id": "public-notice-id"}, db, context)
    assert error.value.code == "UNSUPPORTED"
    assert "college_notices" in error.value.message
    assert "校级 notices" in error.value.message
    assert "直接使用列表字段" in error.value.message


def test_missing_college_notice_returns_scope_specific_nonretry_guidance(db, context, monkeypatch):
    workspace = SimpleNamespace(manager=SimpleNamespace(service=SimpleNamespace(
        cached_college_notice=lambda _user, _record_id: None)))
    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: workspace)
    with pytest.raises(ApiError) as error:
        execute("campus.notice.read", {"record_id": "ckc:12345"}, db, context)
    assert error.value.code == "NOT_FOUND"
    assert "college_notices" in error.value.message
    assert "校级 notices 摘要无需读取正文" in error.value.message


def test_credentials_status_returns_only_nonsecret_state_and_never_logs_in(db, context, monkeypatch):
    class Workspace:
        login_attempts = 0
        def status(self):
            return {"browser_session_authorized": False, "connected": False,
                    "connectors": [{"platform_id": "zdbk", "connected": False}]}

    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: Workspace())
    monkeypatch.setattr("app.campus_credentials.all_status", lambda: {
        "items": [{"scope": "passport", "configured": True, "auto_relogin": True,
                   "username_hint": "32***32", "saved_at": "private-time", "password": "never-return"},
                  {"scope": "webvpn", "configured": False, "auto_relogin": False,
                   "username_hint": None, "saved_at": None}]
    })
    result = execute("campus.credentials.status", {}, db, context)
    assert result["status"] == "ok"
    assert result["coverage"] == {"scope": "local_auth_metadata", "complete": True, "missing": []}
    assert result["freshness"]["fetched_at"] is not None
    assert result["freshness"]["stale"] is False
    assert any("不验证学校端" in warning for warning in result["warnings"])
    assert result["data"]["credentials"] == [
        {"scope": "passport", "configured": True, "auto_relogin": True},
        {"scope": "webvpn", "configured": False, "auto_relogin": False},
    ]
    assert result["data"]["browser_session_authorized"] is False
    assert result["data"]["campus_workspace_connected"] is False
    assert "username_hint" not in str(result)
    assert "private-time" not in str(result)
    assert "never-return" not in str(result)


def test_general_campus_status_does_not_include_saved_credential_metadata(db, context, monkeypatch):
    class Workspace:
        def status(self):
            return {"connected": False, "browser_session_authorized": False,
                    "credentials": {"passport": {"username_hint": "32***32", "saved_at": "private-time"}},
                    "last_auto_login": "private-login-time", "browser_authorization": {"grant_id": "private-grant"}}

    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: Workspace())
    result = execute("campus.status", {}, db, context)
    assert result["status"] == "ok"
    assert result["coverage"] == {"scope": "local_campus_connection_metadata", "complete": True, "missing": []}
    assert result["freshness"]["fetched_at"] is not None
    assert any("不主动验证远端连通性" in warning for warning in result["warnings"])
    assert "credentials" not in result["data"]
    assert "last_auto_login" not in result["data"]
    assert "browser_authorization" not in result["data"]
    assert "private-time" not in str(result)
    assert "private-grant" not in str(result)


def test_reviews_generate_exposes_the_explicit_new_generation_signal():
    """F2 escape hatch: "another fresh paper" is a machine-checkable argument, not prompt text.

    The tool count stays at 32; this only adds one optional field to an existing schema.
    """
    schema = next(s for s in schemas() if s["name"] == "reviews.generate")["input_schema"]
    assert schema["properties"]["new_generation"]["type"] == "boolean"
    assert schema["properties"]["new_generation"].get("default") is False
    assert "new_generation" not in schema.get("required", [])
    assert "wait_seconds" in schema["properties"], "the bounded observation window is unchanged"
    assert schema["additionalProperties"] is False


def test_cache_identity_excludes_only_declared_observation_arguments():
    """R1: the call cache identity is the business request, and only for tools that declare it.

    The regression this guards: the gateway removed `wait_seconds` when *reading* the cache but
    stored the raw arguments, so a first call that already carried a wait window could never be
    replayed under its own trusted call id (409 for byte-identical arguments). Read and write must
    therefore share one normalisation, and tools without observation arguments must keep a raw
    identity so a changed business argument still conflicts.
    """
    from app.agent_bridge.store import digest, encoded
    from app.agent_bridge.tools import OBSERVATION_ARGUMENTS, cache_arguments, observation_arguments

    # Declared where it exists, and nowhere else.
    assert observation_arguments("jobs.read") == ("wait_seconds",)
    assert observation_arguments("courses.list") == ()
    assert observation_arguments("artifacts.export") == ()
    declared = {name for name in SPECS if "wait_seconds" in SPECS[name][0].model_fields}
    assert declared == {"jobs.read", "jobs.cancel", "connections.sync", "reviews.generate"}
    assert {name for name, args in OBSERVATION_ARGUMENTS.items() if args} == declared

    # A wait window is not part of the identity; a business argument always is.
    short = cache_arguments("jobs.read", {"job_id": "job_a", "wait_seconds": 1})
    long = cache_arguments("jobs.read", {"job_id": "job_a", "wait_seconds": 14})
    assert short == long == {"job_id": "job_a"}
    assert cache_arguments("jobs.read", {"job_id": "job_b", "wait_seconds": 1}) != short
    assert cache_arguments("courses.list", {"query": "x", "wait_seconds": 1}) != cache_arguments("courses.list", {"query": "x"})
    assert digest(encoded({"name": "jobs.read", "arguments": short})) == digest(encoded({"name": "jobs.read", "arguments": long}))


def test_store_digest_of_the_cache_identity_replays_after_a_wait_change():
    """R1 at the identity seam: what one gateway write fingerprints must be replayable.

    `Store.cached_call`/`cache_call` compare `digest(encoded(request))`, so the invariant is on the
    value the gateway hands them. Hash the normalised identity of a first call carrying a wait
    window, then hash the normalised identity of its replay with a different window: they must be
    the same string, while a changed business argument must not be.
    """
    from app.agent_bridge.store import digest, encoded
    from app.agent_bridge.tools import cache_arguments

    def identity(name, arguments):
        return digest(encoded({"name": name, "arguments": cache_arguments(name, arguments)}))

    assert identity("jobs.read", {"job_id": "job_a", "wait_seconds": 1}) == identity("jobs.read", {"job_id": "job_a", "wait_seconds": 14})
    assert identity("jobs.read", {"job_id": "job_a", "wait_seconds": 1}) == identity("jobs.read", {"job_id": "job_a"})
    assert identity("reviews.generate", {"course_id": "course_a", "wait_seconds": 14}) == identity("reviews.generate", {"course_id": "course_a", "wait_seconds": 0})
    assert identity("jobs.read", {"job_id": "job_b", "wait_seconds": 1}) != identity("jobs.read", {"job_id": "job_a", "wait_seconds": 1})
    assert identity("reviews.generate", {"course_id": "course_a", "new_generation": True, "wait_seconds": 1}) != identity("reviews.generate", {"course_id": "course_a", "wait_seconds": 1})
    assert identity("courses.list", {"query": "x", "wait_seconds": 1}) != identity("courses.list", {"query": "x"})


def test_ambiguous_course_query_requires_a_choice_instead_of_guessing(db, context):
    db.add(m.Course(id="crs_repeat", title="微积分", term="2025秋"))
    db.commit()
    scoped = replace(context, course_ids=frozenset({"crs_a", "crs_repeat"}))
    result = execute("courses.list", {"query": "微积分"}, db, scoped)
    assert result["status"] == "needs_user"
    assert {item["term"] for item in result["data"]["items"]} == {"2026秋", "2025秋"}
    assert result["warnings"]


def test_remaining_read_tools_return_actual_scope_and_unparsed_coverage(db, context):
    assert execute("connections.status", {}, db, context)["data"]["items"] == []
    db.get(m.Resource, "res_a").parse_status = "pending"
    db.commit()
    result = execute("materials.list", {"course_id": "crs_a"}, db, context)
    assert [item["id"] for item in result["data"]["items"]] == ["res_a"]
    assert result["status"] == "partial" and not result["coverage"]["complete"]
    assert result["freshness"]["stale"] is True


def test_read_search_hit_beyond_first_page_reports_partial_file_coverage(db, context):
    db.add(m.ContentChunk(id="late_hit", resource_id="res_a", resource_version=2, ordinal=50,
                         text="后续章节的额外要求", locator={"page": 51}, provenance={"kind": "synthetic"}))
    db.commit()
    hit = execute("materials.search", {"course_id": "crs_a", "query": "后续章节"}, db, context)["data"]["items"][0]
    result = execute("materials.read", {"resource_id": hit["resource_id"], "expected_version": hit["resource_version"], "chunk_ids": [hit["chunk_id"]]}, db, context)
    assert [i["id"] for i in result["data"]["items"]] == ["late_hit"]
    assert result["coverage"]["complete"] is False and result["status"] == "partial"
    assert result["coverage"]["scope"] == "material:res_a:v2"


def test_ready_resource_without_text_does_not_fabricate_evidence(db, context):
    db.add(m.Resource(id="empty_text", course_id="crs_a", kind="document", title="空白文件", version=1, parse_status="ready"))
    db.commit()
    with pytest.raises(ApiError) as error:
        execute("materials.read", {"resource_id": "empty_text", "expected_version": 1}, db, context)
    assert error.value.code == "NO_EVIDENCE"


def test_local_workspace_can_include_user_tasks_without_course_only_with_trusted_scope(db, context):
    db.add(m.LearningTask(id="uncoursed", course_id=None, type="reading", title="个人复习", estimated_minutes=25))
    db.commit()
    assert execute("tasks.list", {}, db, context)["data"]["items"] == []
    result = execute("tasks.list", {}, db, replace(context, allow_uncoursed_tasks=True))
    assert [i["id"] for i in result["data"]["items"]] == ["uncoursed"]
    with pytest.raises(ApiError):
        execute("tasks.list", {"allow_uncoursed_tasks": True}, db, context)


@pytest.mark.parametrize("args", [{"owner":"other"}, {"limit":-1}, {"limit":True}, {"limit":51}, {"limit":"10"}])
def test_strict_inputs(db, context, args):
    with pytest.raises(ApiError, match="严格契约"):
        execute("courses.list", args, db, context)


def test_course_scope_filters_before_pagination(db, context):
    result = execute("courses.list", {}, db, context)
    assert [i["id"] for i in result["data"]["items"]] == ["crs_a"]
    assert result["coverage"]["complete"] is False
    assert result["freshness"]["stale"] is True


@pytest.mark.parametrize("name,args", [
    ("materials.list", {"course_id":"crs_b"}),
    ("assignments.list", {"course_id":"crs_b"}),
    ("materials.read", {"resource_id":"res_b","expected_version":1}),
    ("jobs.read", {"job_id":"job_private"}),
])
def test_unauthorized_objects_are_denied(db, context, name, args):
    with pytest.raises(ApiError) as error:
        execute(name, args, db, context)
    assert error.value.code == "SCOPE_DENIED"


def test_unknown_deadline_kept_in_date_window(db, context):
    result = execute("assignments.list", {"due_before":"2026-10-14T00:00:00+08:00"}, db, context)
    assert [i["id"] for i in result["data"]["items"]] == ["asg_a"]
    assert result["data"]["items"][0]["due_at"] is None
    assert result["data"]["items"][0]["due_precision"] == "unknown"
    assert "instructions" not in result["data"]["items"][0]
    assert result["warnings"]


def test_assignment_instructions_are_loaded_only_when_requested(db, context):
    compact = execute("assignments.list", {}, db, context)
    detailed = execute("assignments.list", {"include_instructions": True}, db, context)
    compact_item = next(item for item in compact["data"]["items"] if item["id"] == "asg_a")
    detailed_item = next(item for item in detailed["data"]["items"] if item["id"] == "asg_a")
    assert "instructions" not in compact_item
    assert detailed_item["instructions"] == "提交正文和数据附件"


def test_assignment_due_soon_sort_and_cursor_keep_stable_order(db, context):
    db.add_all([
        m.Assignment(id="asg_due_late", course_id="crs_a", title="晚截止", due_at=datetime(2026, 11, 2, tzinfo=timezone.utc), due_precision="minute"),
        m.Assignment(id="asg_due_soon", course_id="crs_a", title="早截止", due_at=datetime(2026, 11, 1, tzinfo=timezone.utc), due_precision="minute"),
    ])
    foreign = db.get(m.Assignment, "asg_b")
    foreign.due_at = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    foreign.due_precision = "minute"
    db.flush()
    first = execute("assignments.list", {"order_by": "due_soon", "limit": 2}, db, context)
    assert [item["id"] for item in first["data"]["items"]] == ["asg_due_soon", "asg_due_late"]
    assert [item["course_title"] for item in first["data"]["items"]] == ["微积分", "微积分"]
    assert [item["course_term"] for item in first["data"]["items"]] == ["2026秋", "2026秋"]
    assert first["data"]["next_cursor"], first["data"]
    second = execute("assignments.list", {"order_by": "due_soon", "limit": 2,
        "cursor": first["data"]["next_cursor"]}, db, context)
    assert [item["id"] for item in second["data"]["items"]] == ["asg_a"]
    assert second["data"]["next_cursor"] is None


def test_assignment_next_by_course_selects_next_due_and_paginates_grouped_rows(db, context):
    db.add_all([
        m.Assignment(id="asg_due_late", course_id="crs_a", title="晚截止", due_at=datetime(2026, 11, 2, tzinfo=timezone.utc), due_precision="minute"),
        m.Assignment(id="asg_due_soon", course_id="crs_a", title="早截止", due_at=datetime(2026, 11, 1, tzinfo=timezone.utc), due_precision="minute"),
    ])
    foreign = db.get(m.Assignment, "asg_b")
    foreign.due_at = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    foreign.due_precision = "minute"
    db.flush()
    scoped = replace(context, course_ids=frozenset({"crs_a", "crs_b"}))
    first = execute("assignments.next_by_course", {"limit": 1}, db, scoped)
    assert [item["id"] for item in first["data"]["items"]] == ["asg_due_soon"]
    assert first["data"]["items"][0]["course_title"] == "微积分"
    assert first["data"]["items"][0]["due_at_local"] == "2026-11-01T08:00:00+08:00"
    assert first["data"]["items"][0]["course_status"] == "next_due"
    assert first["data"]["items"][0]["unknown_deadline_count"] == 1
    assert first["data"]["next_cursor"]
    second = execute("assignments.next_by_course", {"limit": 1,
        "cursor": first["data"]["next_cursor"]}, db, scoped)
    assert second["data"]["items"][0]["course_title"] == "另一账号课程"
    assert second["data"]["items"][0]["course_status"] == "next_due"
    assert second["data"]["items"][0]["assignment_title"] == "私有作业"
    assert second["data"]["next_cursor"] is None
    assert any("本机缓存" in item for item in second["warnings"])


def test_assignment_next_by_course_reports_every_scoped_course_with_cache_status(db, context):
    db.add(m.Course(id="crs_c", title="无作业课程", term="2026秋"))
    db.flush()
    restricted = execute("assignments.next_by_course", {"limit": 50}, db, context)
    assert {item["course_id"] for item in restricted["data"]["items"]} == {"crs_a"}
    scoped = replace(context, course_ids=frozenset({"crs_a", "crs_b", "crs_c"}))
    result = execute("assignments.next_by_course", {"limit": 50}, db, scoped)
    items = {item["course_id"]: item for item in result["data"]["items"]}
    assert set(items) == {"crs_a", "crs_b", "crs_c"}
    assert items["crs_a"]["course_status"] == "unknown_deadline_only"
    assert items["crs_b"]["course_status"] == "unknown_deadline_only"
    assert items["crs_c"]["course_status"] == "no_cached_assignments"
    assert all(item["assignment_title"] is None and item["due_at_local"] is None
               for item in items.values())


def test_assignment_next_by_course_rejects_cursor_from_previous_contract(db, context):
    old_cursor = base64.urlsafe_b64encode(b"k:2026-11-01T00:00:00+00:00|assignment-id").decode()
    with pytest.raises(ApiError) as error:
        execute("assignments.next_by_course", {"cursor": old_cursor}, db, context)
    assert error.value.code == "VALIDATION_FAILED"


def test_assignment_schema_explains_timezone_and_narrow_pagination():
    schema = next(item for item in schemas() if item["name"] == "assignments.list")
    props = schema["input_schema"]["properties"]
    assert "带时区的 ISO-8601" in props["due_after"]["description"]
    assert "next_cursor" in props["cursor"]["description"]
    assert props["include_instructions"]["default"] is False
    assert "due_soon" in props["order_by"]["description"]
    assert "按截止时间升序" in schema["description"]
    assert props["limit"]["default"] == 20
    specialized = next(item for item in schemas() if item["name"] == "assignments.next_by_course")
    assert "unknown_deadline_only" in specialized["description"]
    assert "无需再调用courses.list或assignments.list" in specialized["description"]


def test_courses_schema_does_not_invite_assignment_course_name_lookup():
    schema = next(item for item in schemas() if item["name"] == "courses.list")
    assert "不要仅为把 assignments.list 返回的 course_id 转成课程名而调用" in schema["description"]
    assert "assignments.list 已逐项返回 course_title 和 course_term" in schema["description"]


def test_date_must_have_timezone(db, context):
    with pytest.raises(ApiError) as error:
        execute("assignments.list", {"due_before":"2026-10-14T00:00:00"}, db, context)
    assert error.value.code == "INVALID_INPUT"


def test_search_then_read_carries_real_evidence(db, context):
    hit = execute("materials.search", {"course_id":"crs_a", "query":"实验报告"}, db, context)
    assert hit["evidence"][0]["source_id"] == "chk_a"
    result = execute("materials.read", {"resource_id":"res_a", "expected_version":2}, db, context)
    assert "数据附件" in result["data"]["items"][0]["text"]
    assert result["evidence"][0]["version"] == 2


def test_stale_material_rejected(db, context):
    with pytest.raises(ApiError) as error:
        execute("materials.read", {"resource_id":"res_a", "expected_version":1}, db, context)
    assert error.value.code == "STALE_VERSION"


def test_foreign_chunk_cannot_be_used_as_evidence(db, context):
    with pytest.raises(ApiError):
        execute("materials.read", {"resource_id":"res_a", "expected_version":2, "chunk_ids":["chk_b"]}, db, context)


def test_empty_search_does_not_claim_absence_of_requirements(db, context):
    result = execute("materials.search", {"course_id":"crs_a", "query":"不存在关键字"}, db, context)
    assert result["data"]["items"] == []
    assert result["warnings"]


def test_job_does_not_expose_payload(db, context):
    result = execute("jobs.read", {"job_id":"job_a"}, db, context)
    assert result["data"]["status"] == "queued"
    assert "payload" not in result["data"]
    assert "secret" not in str(result)


def test_jobs_read_waits_in_tool_call_for_owned_campus_sync(monkeypatch, db, context):
    sync = m.Job(id="job_sync_wait", type="sync", status="running", payload=None)
    db.add(sync)
    db.flush()
    reads = []
    states = iter(("running", "running", "succeeded"))
    def get_job(job_id, session):
        reads.append(job_id)
        return {"id": job_id, "type": "sync", "status": next(states), "errors": [], "result_refs": []}
    monkeypatch.setattr("app.agent_bridge.tools.jobs.get_job", get_job)
    monkeypatch.setattr("app.agent_bridge.tools.time.sleep", lambda _seconds: None)
    sync_context = ToolContext("local", "conv_a", frozenset({"crs_a"}),
                               job_ids=frozenset({"job_sync_wait"}))
    result = execute("jobs.read", {"job_id": "job_sync_wait", "wait_seconds": 14}, db, sync_context)
    assert result["data"]["status"] == "succeeded"
    assert reads == ["job_sync_wait"] * 3


def test_jobs_read_omitted_wait_defaults_to_bounded_observation_for_sync(monkeypatch, db, context):
    sync = m.Job(id="job_sync_default_wait", type="sync", status="running", payload=None)
    db.add(sync)
    db.flush()
    reads = []
    states = iter(("running", "succeeded"))

    def get_job(job_id, session):
        reads.append(job_id)
        return {"id": job_id, "type": "sync", "status": next(states), "errors": [], "result_refs": []}

    monkeypatch.setattr("app.agent_bridge.tools.jobs.get_job", get_job)
    monkeypatch.setattr("app.agent_bridge.tools.time.sleep", lambda _seconds: None)
    sync_context = ToolContext("local", "conv_a", frozenset({"crs_a"}),
                               job_ids=frozenset({"job_sync_default_wait"}))
    result = execute("jobs.read", {"job_id": "job_sync_default_wait"}, db, sync_context)
    assert result["data"]["status"] == "succeeded"
    assert reads == ["job_sync_default_wait"] * 2


def test_jobs_read_explicit_zero_stays_immediate(monkeypatch, db, context):
    sync = m.Job(id="job_sync_now", type="sync", status="running", payload=None)
    db.add(sync)
    db.flush()
    reads = []

    def get_job(job_id, session):
        reads.append(job_id)
        return {"id": job_id, "type": "sync", "status": "running", "errors": [], "result_refs": []}

    monkeypatch.setattr("app.agent_bridge.tools.jobs.get_job", get_job)
    sync_context = ToolContext("local", "conv_a", frozenset({"crs_a"}),
                               job_ids=frozenset({"job_sync_now"}))
    result = execute("jobs.read", {"job_id": "job_sync_now", "wait_seconds": 0}, db, sync_context)
    assert result["data"]["status"] == "running"
    assert reads == ["job_sync_now"]


def test_sync_observation_wait_budget_is_shared_from_job_acceptance(db):
    created = datetime.now(timezone.utc) - timedelta(seconds=57)
    sync = m.Job(id="job_sync_cap", type="sync", status="running", created_at=created)
    db.add(sync)
    db.flush()
    window = job_wait_window(db, sync.to_contract(), 14)
    assert 0 <= window <= 4
    sync.created_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db.flush()
    assert job_wait_window(db, sync.to_contract(), 14) == 0


def test_untrusted_principal_denied(db):
    with pytest.raises(ApiError) as error:
        execute("courses.list", {}, db, ToolContext("another-user", "conv_a"))
    assert error.value.code == "SCOPE_DENIED"


# --- campus.query/sync envelope: usable stale cache is partial data, not a failure ---
from app.agent_bridge.tools import _campus_query_envelope  # noqa: E402


def _campus_data(*, error=None, records=None, completeness="partial", freshness="stale"):
    return {"capability": "courses", "error": error, "records": records or [],
            "completeness": completeness, "freshness": freshness,
            "fetched_at": "2026-10-16T18:06:48+08:00", "platform": "zdbk", "source": "courses",
            "warnings": [], "next_cursor": None, "last_attempt_at": None}


def test_campus_query_auth_error_with_cached_records_is_partial_not_error():
    envelope = _campus_query_envelope(_campus_data(
        error={"code": "AUTH_REQUIRED", "message": "需要登录"}, records=[{"id": "course_1", "name": "线性代数"}]))
    assert envelope["status"] == "partial"
    assert envelope["error"]["code"] == "AUTH_REQUIRED"          # recovery info kept
    assert envelope["data"]["records"] == [{"id": "course_1", "name": "线性代数"}]
    assert envelope["coverage"]["complete"] is False
    assert envelope["freshness"]["stale"] is True
    assert any("本地缓存" in warning for warning in envelope["warnings"])
    assert envelope["evidence"][0]["source_id"] == "course_1"


def test_campus_query_auth_error_without_records_remains_error():
    envelope = _campus_query_envelope(_campus_data(error={"code": "AUTH_REQUIRED", "message": "需要登录"}))
    assert envelope["status"] == "error"
    assert envelope["data"]["records"] == []
    assert envelope["warnings"] == []


def test_campus_query_complete_cache_without_error_is_ok():
    envelope = _campus_query_envelope(_campus_data(completeness="complete", freshness="fresh"))
    assert envelope["status"] == "ok"
    assert envelope["coverage"]["complete"] is True
    assert envelope["freshness"]["stale"] is False


def _fake_timetable_workspace(calls):
    class Fake:
        def timetable_records(self):
            calls.append("timetable_records")
            return {"records": [{"name": "常微分方程", "weekday": "1"}], "completeness": "partial",
                    "freshness": "fresh", "fetched_at": "2026-10-17T13:46:43+08:00",
                    "platform": "zdbk", "source": "zdbk-timetable", "warnings": [],
                    "next_cursor": None, "error": None}

        def sync_timetable(self, *, allow_recovery=True):
            calls.append(("sync_timetable", allow_recovery))
            return self.timetable_records()

        def query(self, *args, **kwargs):
            calls.append("connector_query")
            raise AssertionError("connector path must not run when the timetable snapshot has records")

        def status(self):
            return {"connected": True}
    return Fake()


def test_campus_courses_tools_prefer_the_migrated_timetable_snapshot(db, monkeypatch):
    import app.campus_workspace as campus_workspace
    from app.agent_bridge import tools as tools_module

    calls: list = []
    monkeypatch.setattr(campus_workspace, "get_campus_workspace", lambda: _fake_timetable_workspace(calls))
    ctx = ToolContext("local", "conv_snapshot", frozenset(), allow_uncoursed_tasks=True, request_key="turn:call")
    result = execute("campus.query", {"capability": "courses", "query": {}}, db, ctx)
    assert result["status"] == "partial"
    assert result["data"]["records"][0]["name"] == "常微分方程"
    assert calls == ["timetable_records"]

    calls.clear()
    synced = execute("campus.sync", {"capability": "courses", "query": {}}, db, ctx)
    assert synced["data"]["records"][0]["name"] == "常微分方程"
    assert calls == [("sync_timetable", True), "timetable_records"]
