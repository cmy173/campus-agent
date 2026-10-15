from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app import models as m
from app.agent_bridge.tools import ToolContext, execute
from app.errors import ApiError
from test_agent_constraints import workspace


def context():
    return ToolContext("local", "conversation", frozenset({"course"}), allow_uncoursed_tasks=True, request_key="turn:feedback")


def feedback(db, arguments=None, ctx=None):
    return execute("tasks.feedback", arguments or {"task_id": "task", "expected_revision": 1, "outcome": "completed"}, db, ctx or context())


def test_feedback_is_durable_and_does_not_submit_school_assignment(workspace):
    db, _ = workspace
    db.add(m.Assignment(id="assignment", course_id="course", title="学校作业", due_precision="unknown", submission_status="not_submitted"))
    db.commit()
    result = feedback(db)
    assert result["data"]["task"]["status"] == "completed"
    assert result["data"]["task"]["revision"] == 2
    assert result["data"]["feedback"]["actual_minutes"] is None
    db.expire_all()
    assert feedback(db)["data"] == result["data"]
    assert db.scalar(select(func.count()).select_from(m.TaskFeedback)) == 1
    assert db.get(m.Assignment, "assignment").submission_status == "not_submitted"
    with pytest.raises(ApiError) as error:
        feedback(db, {"task_id": "task", "expected_revision": 1, "outcome": "partial"})
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("kind", ["stale", "scope", "extra", "archived"])
def test_invalid_feedback_does_not_write(workspace, kind):
    db, _ = workspace
    args = {"task_id": "task", "expected_revision": 1, "outcome": "completed"}
    ctx = context()
    if kind == "stale":
        args["expected_revision"] = 7
    elif kind == "scope":
        ctx = replace(ctx, course_ids=frozenset())
    elif kind == "extra":
        args["platform_submitted"] = True
    else:
        db.get(m.LearningTask, "task").archived_at = m.utc_now()
        db.commit()
    with pytest.raises(ApiError) as error:
        feedback(db, args, ctx)
    assert error.value.code == {"stale": "TASK_STALE", "scope": "SCOPE_DENIED", "extra": "INVALID_INPUT", "archived": "CONFLICT"}[kind]
    assert db.scalar(select(func.count()).select_from(m.TaskFeedback)) == 0
    assert db.get(m.LearningTask, "task").status == "pending"


def test_failure_before_commit_rolls_back_feedback_task_and_receipt(workspace, monkeypatch):
    db, _ = workspace
    def fail():
        raise RuntimeError("synthetic commit failure")
    monkeypatch.setattr(db, "commit", fail)
    with pytest.raises(RuntimeError):
        feedback(db)
    assert db.scalar(select(func.count()).select_from(m.TaskFeedback)) == 0
    assert db.scalar(select(func.count()).select_from(m.AgentFeedbackReceipt)) == 0
    assert db.get(m.LearningTask, "task").status == "pending"
