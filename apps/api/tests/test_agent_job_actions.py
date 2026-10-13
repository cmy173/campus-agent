"""Real domain queue, worker, cancellation and durable creation receipt tests."""
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app import models as m
from app.agent_bridge.tools import ToolContext, execute
from app.errors import ApiError
from app.worker import _claim_next, process_job, requeue_stale
from test_agent_constraints import workspace


def context(key="turn:build", cid="conversation"):
    return ToolContext("local", cid, frozenset({"course"}), allow_uncoursed_tasks=True, request_key=key)


def build(db, ctx=None):
    return execute("tasks.build", {"course_id": "course"}, db, ctx or context())


def test_build_receipt_survives_response_loss_and_worker_completion(workspace):
    db, _ = workspace
    db.add(m.Assignment(id="assignment", course_id="course", title="实际任务生成依据", instructions="交报告", due_precision="unknown"))
    db.commit()
    result = build(db)
    jid = result["job_id"]
    assert result["status"] == "queued" and result["data"]["job"]["status"] == "queued"
    assert db.scalar(select(func.count()).select_from(m.AgentJobRequest)) == 1
    claimed = _claim_next(db)
    assert claimed.id == jid
    process_job(db, claimed)
    assert db.get(m.Job, jid).status == "succeeded"
    assert db.scalar(select(func.count()).select_from(m.LearningTask)) > 1
    db.expire_all()
    # Even after completion, retrying the same tool call returns the old job.
    assert build(db)["job_id"] == jid
    assert db.scalar(select(func.count()).select_from(m.Job)) == 1
    read = execute("jobs.read", {"job_id": jid}, db, context())
    assert read["data"]["status"] == "succeeded" and read["freshness"]["stale"] is False
    assert "payload" not in read["data"]
    with pytest.raises(ApiError) as error:
        execute("jobs.read", {"job_id": jid}, db, context(cid="foreign"))
    assert error.value.code == "SCOPE_DENIED"


def test_same_scope_pending_jobs_reuse_but_receipts_remain_bound_to_calls(workspace):
    db, _ = workspace
    first = build(db)
    second = build(db, context("turn:second"))
    assert first["job_id"] == second["job_id"]
    assert db.scalar(select(func.count()).select_from(m.Job)) == 1
    assert db.scalar(select(func.count()).select_from(m.AgentJobRequest)) == 2
    db.add(m.Course(id="other", title="另一门课", term="2026秋"))
    db.commit()
    with pytest.raises(ApiError) as error:
        execute("tasks.build", {"course_id": "other"}, db, replace(context(), course_ids=frozenset({"course", "other"})))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("running", [False, True])
def test_actual_cancel_is_idempotent_and_running_waits_for_worker_checkpoint(workspace, running):
    db, _ = workspace
    jid = build(db)["job_id"]
    if running:
        assert _claim_next(db).id == jid
    with pytest.raises(ApiError):
        execute("jobs.cancel", {"job_id": jid}, db, context(cid="foreign"))
    result = execute("jobs.cancel", {"job_id": jid}, db, context())
    assert result["data"]["job"]["cancel_requested"] is True
    assert result["data"]["job"]["status"] == ("running" if running else "cancelled")
    assert execute("jobs.cancel", {"job_id": jid}, db, context())["data"] == result["data"]
    if running:
        # A restart retains cancel_requested, then real worker terminates it.
        assert requeue_stale(db) == 1
        job = _claim_next(db)
        process_job(db, job)
    assert execute("jobs.read", {"job_id": jid}, db, context())["data"]["status"] == "cancelled"


def test_failed_commit_rolls_back_job_and_receipt(workspace, monkeypatch):
    db, _ = workspace
    def crash():
        raise RuntimeError("synthetic lost commit")
    monkeypatch.setattr(db, "commit", crash)
    with pytest.raises(RuntimeError):
        build(db)
    assert db.scalar(select(func.count()).select_from(m.Job)) == 0
    assert db.scalar(select(func.count()).select_from(m.AgentJobRequest)) == 0
