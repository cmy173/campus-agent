"""Actual DB/engine tests for durable preview authorization and mutation receipts."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import models as m
from app.agent_bridge import plan_actions as actions
from app.agent_bridge.tools import ToolContext, execute, schemas
from app.errors import ApiError
from test_agent_constraints import workspace


def make(db, day, key="turn:call"):
    return actions.preview(db, "conversation", key, day, day, blocked=[])


def test_registered_tools_use_real_engine_and_do_not_accept_model_approval(workspace):
    db, day = workspace
    ctx = ToolContext("local", "conversation", frozenset({"course"}), allow_uncoursed_tasks=True, request_key="turn:call")
    assert execute("availability.read", {}, db, ctx)["data"]["configured"] is True
    arguments = {"period": "dates", "start_date": str(day), "end_date": str(day)}
    action = execute("plans.preview", arguments, db, ctx)["data"]["action"]
    assert action["preview"]["plan"]["blocks"]
    assert execute("plans.preview", arguments, db, ctx)["data"]["action"]["id"] == action["id"]
    with pytest.raises(ApiError) as error:
        execute("plans.apply", {"action_id": action["id"], "approved": True}, db, ctx)
    assert error.value.code == "INVALID_INPUT"
    with pytest.raises(ApiError) as error:
        execute("plans.apply", {"action_id": action["id"]}, db, ctx)
    assert error.value.code == "APPROVAL_REQUIRED"
    actions.approve(db, "conversation", action["id"])
    assert execute("plans.apply", {"action_id": action["id"]}, db, ctx)["data"]["receipt"]["plan"]["status"] == "active"
    assert {s["name"] for s in schemas() if s["effects"] == "write"} == {"plans.preview", "plans.apply", "plans.revoke", "tasks.build", "jobs.cancel", "tasks.feedback"}


def test_schedule_tools_reject_partial_scope_and_invalid_relative_dates(workspace):
    db, day = workspace
    ctx = ToolContext("local", "conversation", frozenset(), allow_uncoursed_tasks=True, request_key="turn:call")
    with pytest.raises(ApiError) as error:
        execute("plans.preview", {}, db, ctx)
    assert error.value.code == "SCOPE_DENIED"
    ctx = ToolContext("local", "conversation", frozenset({"course"}), allow_uncoursed_tasks=True, request_key="turn:call")
    with pytest.raises(ApiError) as error:
        execute("plans.preview", {"period": "tomorrow", "start_date": str(day)}, db, ctx)
    assert error.value.code == "INVALID_INPUT"
    db.delete(db.get(m.Availability, 1))
    db.commit()
    assert execute("availability.read", {}, db, ctx)["data"] == {"configured": False, "availability": None}


def test_weekday_evening_is_resolved_by_server_and_past_weekdays_are_not_moved_to_next_week():
    from datetime import date
    from app.agent_bridge.tools import _evening_blocks, PreviewArgs
    from app.plan_engine import resolve_user_tz
    args = PreviewArgs(unavailable_evenings=[3]).model_dump()
    blocked = _evening_blocks(args, date(2026, 10, 13), date(2026, 10, 18), resolve_user_tz("Asia/Shanghai"))
    assert blocked == [{"starts_at": "2026-10-14T18:00:00+08:00", "ends_at": "2026-10-15T00:00:00+08:00"}]
    with pytest.raises(ApiError) as error:
        _evening_blocks(args, date(2026, 10, 16), date(2026, 10, 18), resolve_user_tz("Asia/Shanghai"))
    assert error.value.code == "INVALID_INPUT"


def test_today_preview_leaves_time_to_inspect_before_first_new_block(workspace):
    from app.plan_engine import resolve_user_tz
    from app.repo import normalize_dt
    db, _ = workspace
    before = m.utc_now()
    today = before.astimezone(resolve_user_tz("Asia/Shanghai")).date()
    row = db.get(m.Availability, 1)
    row.weekly_windows = [{"weekday": d, "start": "00:00", "end": "23:59"} for d in range(1, 8)]
    db.commit()
    action = actions.preview(db, "conversation", "today", today, today + timedelta(days=1))
    plan = db.get(m.Plan, action["plan_id"])
    assert plan.blocks
    assert min(normalize_dt(b.starts_at) for b in plan.blocks) >= before + timedelta(minutes=9)
    actions.approve(db, "conversation", action["id"])
    assert actions.execute(db, "conversation", action["id"], "apply")["plan"]["status"] == "active"


def test_preview_is_durable_idempotent_and_requires_explicit_approval(workspace):
    db, day = workspace
    first = make(db, day)
    db.expire_all()
    assert make(db, day) == first
    assert first["preview"]["diff"]["added"]
    assert db.scalar(select(func.count()).select_from(m.Plan)) == 1
    with pytest.raises(ApiError) as error:
        actions.execute(db, "conversation", first["id"], "apply")
    assert error.value.code == "APPROVAL_REQUIRED"
    assert db.get(m.Plan, first["plan_id"]).status == "candidate"
    with pytest.raises(ApiError) as error:
        actions.preview(db, "conversation", "turn:call", day, day + timedelta(days=1), blocked=[])
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_approve_apply_replay_and_revoke_restore_previous(workspace):
    db, day = workspace
    first = make(db, day, "first")
    actions.approve(db, "conversation", first["id"])
    actions.execute(db, "conversation", first["id"], "apply")
    second = make(db, day, "second")
    actions.approve(db, "conversation", second["id"])
    result = actions.execute(db, "conversation", second["id"], "apply")
    db.expire_all()
    assert actions.execute(db, "conversation", second["id"], "apply") == result
    assert result["previous_plan_id"] == first["plan_id"]
    revoke = actions.prepare_revoke(db, "conversation", second["id"], "revoke-request")
    assert actions.prepare_revoke(db, "conversation", second["id"], "revoke-request") == revoke
    actions.approve(db, "conversation", revoke["id"])
    undone = actions.execute(db, "conversation", revoke["id"], "revoke")
    assert undone["restored_plan_id"] == first["plan_id"]
    assert actions.execute(db, "conversation", revoke["id"], "revoke") == undone
    assert db.get(m.Plan, first["plan_id"]).status == "active"


@pytest.mark.parametrize("operation", ["approve", "execute", "prepare_revoke"])
def test_other_conversation_cannot_use_action(workspace, operation):
    db, day = workspace
    action = make(db, day)
    with pytest.raises(ApiError) as error:
        args = [db, "other-conversation", action["id"]]
        if operation == "execute":
            args.append("apply")
        elif operation == "prepare_revoke":
            args.append("revoke-request")
        getattr(actions, operation)(*args)
    assert error.value.code == "SCOPE_DENIED"


@pytest.mark.parametrize("change", ["task", "candidate", "active_lock", "expired"])
def test_changes_after_acceptance_are_rejected(workspace, change):
    db, day = workspace
    first = make(db, day, "first")
    actions.approve(db, "conversation", first["id"])
    actions.execute(db, "conversation", first["id"], "apply")
    second = make(db, day, "second")
    actions.approve(db, "conversation", second["id"])
    if change == "task":
        db.get(m.LearningTask, "task").estimated_minutes = 30
    elif change == "candidate":
        db.get(m.Plan, second["plan_id"]).blocks[0].locked = True
    elif change == "active_lock":
        db.get(m.Plan, first["plan_id"]).blocks[0].locked = True
    else:
        db.get(m.AgentPlanAction, second["id"]).expires_at = m.utc_now() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(ApiError) as error:
        actions.execute(db, "conversation", second["id"], "apply")
    assert error.value.code == ("ACTION_EXPIRED" if change == "expired" else "PLAN_STALE")
    assert db.get(m.Plan, first["plan_id"]).status == "active"
    assert db.get(m.Plan, second["plan_id"]).status == "candidate"
    assert db.get(m.AgentPlanAction, second["id"]).status == "approved"


def test_domain_failure_rolls_back_plan_and_receipt_then_retry_works(workspace, monkeypatch):
    db, day = workspace
    action = make(db, day)
    actions.approve(db, "conversation", action["id"])
    original = actions.apply_plan

    def fail_after_mutation(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic crash before receipt commit")

    monkeypatch.setattr(actions, "apply_plan", fail_after_mutation)
    with pytest.raises(RuntimeError):
        actions.execute(db, "conversation", action["id"], "apply")
    assert db.get(m.Plan, action["plan_id"]).status == "candidate"
    assert db.get(m.AgentPlanAction, action["id"]).status == "approved"
    assert db.get(m.AgentPlanAction, action["id"]).result is None
    monkeypatch.setattr(actions, "apply_plan", original)
    assert actions.execute(db, "conversation", action["id"], "apply")["plan"]["status"] == "active"
