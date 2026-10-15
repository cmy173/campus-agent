"""Independent synthetic DB: strict contracts, scoping, date precision and evidence."""
from datetime import date, datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models as m
from app.agent_bridge.tools import SPECS, ToolContext, execute, schemas
from app.db import Base
from app.errors import ApiError


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
    assert len(SPECS) == 32
    assert len({s["model_name"] for s in schemas()}) == 32
    assert {'campus.status', 'campus.query', 'campus.sync', 'campus.academics', 'campus.notice.read',
            'campus.imported.read', 'campus.webvpn.entry', 'campus.notice.import'} <= set(SPECS)
    # The agent must be able to discover/import courses from 学在浙大 (and refresh them)
    # instead of only searching whatever happened to be imported locally.
    assert {'connections.courses', 'connections.courses.import', 'connections.sync'} <= set(SPECS)
    # Round 3: a fixed trusted document exporter and the review-paper business chain, both
    # still strict-schema and server-scoped.
    assert {'artifacts.export', 'reviews.preflight', 'reviews.generate',
            'reviews.read', 'reviews.export'} <= set(SPECS)
    assert all(s["input_schema"]["additionalProperties"] is False for s in schemas())


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
    assert result["warnings"]


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


def test_untrusted_principal_denied(db):
    with pytest.raises(ApiError) as error:
        execute("courses.list", {}, db, ToolContext("another-user", "conv_a"))
    assert error.value.code == "SCOPE_DENIED"
