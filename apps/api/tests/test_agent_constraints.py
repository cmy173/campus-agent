"""Actual schedule engine proves overrides, shortages and locked conflicts."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import models as m
from app.db import Base
from app.errors import ApiError
from app.plan_engine import preview_candidate
from app.plan_input import compute_input_revision
from app.repo import normalize_dt

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def workspace():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        day = datetime.now(TZ).date() + timedelta(days=1)
        db.add(m.Course(id="course", title="合成课", term="2026秋"))
        db.flush()
        db.add(m.LearningTask(id="task", course_id="course", type="reading", title="学习材料", estimated_minutes=120, due_precision="unknown", status="pending"))
        db.add(m.Availability(id=1, timezone="Asia/Shanghai", weekly_windows=[{"weekday": day.isoweekday(), "start": "18:00", "end": "22:00"}], date_exceptions=[], daily_limit_minutes=120))
        db.commit()
        yield db, day
    engine.dispose()


def window(day, start="19:00", end="21:00"):
    return {"starts_at": f"{day}T{start}:00+08:00", "ends_at": f"{day}T{end}:00+08:00"}


def test_blocked_evening_moves_actual_schedule_without_changing_permanent_preferences(workspace):
    db, day = workspace
    original = db.get(m.Availability, 1).to_contract()
    fingerprint = compute_input_revision(db)
    candidate = preview_candidate(db, day, day, True, temporary_blocked=[window(day)])
    assert candidate.blocks
    for block in candidate.blocks:
        start, end = normalize_dt(block.starts_at).astimezone(TZ), normalize_dt(block.ends_at).astimezone(TZ)
        assert not (start < datetime.combine(day, time(21), TZ) and end > datetime.combine(day, time(19), TZ))
    assert sum((block.ends_at - block.starts_at).total_seconds() / 60 for block in candidate.blocks) == 120
    assert db.get(m.Availability, 1).to_contract() == original
    assert compute_input_revision(db) == fingerprint == candidate.input_revision


def test_insufficient_time_is_reported_instead_of_overbooking(workspace):
    db, day = workspace
    candidate = preview_candidate(db, day, day, True, temporary_blocked=[window(day, "18:00", "21:30")])
    assert candidate.unassigned and candidate.unassigned[0].task_id == "task"
    assert sum((b.ends_at - b.starts_at).total_seconds() / 60 for b in candidate.blocks) <= 30
    assert candidate.unassigned[0].reason_code in {"INSUFFICIENT_TOTAL_TIME", "FRAGMENTED_WINDOW"}


@pytest.mark.parametrize("blocked", [
    [{"starts_at": "2026-10-13T19:00:00", "ends_at": "2026-10-13T20:00:00"}],
    [{"starts_at": "invalid", "ends_at": "invalid"}],
    [{"starts_at": "2026-10-13T19:00:30+08:00", "ends_at": "2026-10-13T20:00:00+08:00"}],
])
def test_invalid_temporary_dates_are_rejected(workspace, blocked):
    db, day = workspace
    with pytest.raises(ApiError) as error:
        preview_candidate(db, day, day, True, temporary_blocked=blocked)
    assert error.value.code == "INVALID_INPUT"
    assert db.scalar(select(func.count()).select_from(m.Plan)) == 0


def test_temporary_windows_can_be_given_without_inventing_or_persisting_preferences(workspace):
    db, day = workspace
    db.delete(db.get(m.Availability, 1))
    db.commit()
    with pytest.raises(ApiError):
        preview_candidate(db, day, day, True)
    candidate = preview_candidate(db, day, day, True, temporary_windows=[{"weekday": day.isoweekday(), "start": "18:00", "end": "21:00"}])
    assert candidate.blocks and db.get(m.Availability, 1) is None


def test_candidate_and_children_can_rollback_with_future_atomic_authorization_receipt(workspace):
    db, day = workspace
    candidate = preview_candidate(db, day, day, True, temporary_blocked=[], commit=False)
    identity = candidate.id
    assert candidate.blocks
    db.rollback()
    assert db.get(m.Plan, identity) is None
    assert db.scalar(select(func.count()).select_from(m.PlanBlock)) == 0


def test_locked_conflict_requires_user_resolution_not_silent_unlocking(workspace):
    db, day = workspace
    plan = m.Plan(id="active", version=1, status="active", input_revision=compute_input_revision(db),
                  horizon_start=day, horizon_end=day, summary={})
    db.add(plan)
    db.flush()
    db.add(m.PlanBlock(plan_id=plan.id, id="locked", task_id="task", starts_at=normalize_dt(datetime.combine(day, time(19), TZ)), ends_at=normalize_dt(datetime.combine(day, time(20), TZ)), locked=True))
    db.commit()
    with pytest.raises(ApiError) as error:
        preview_candidate(db, day, day, True, temporary_blocked=[window(day)])
    assert error.value.code == "LOCKED_CONSTRAINT_CONFLICT"
    assert db.scalar(select(func.count()).select_from(m.Plan)) == 1
    assert db.get(m.Plan, "active").status == "active"


def test_apply_and_revoke_share_caller_transaction(workspace):
    from app.plan_mutations import apply_plan, revoke_plan
    db, day = workspace
    first = preview_candidate(db, day, day, True)
    first_id = first.id
    apply_plan(db, first_id)
    second = preview_candidate(db, day, day, True)
    second_id = second.id
    result = apply_plan(db, second_id, commit=False)
    assert result["previous_plan_id"] == first_id
    assert db.get(m.Plan, first_id).status == "superseded"
    db.rollback()
    assert db.get(m.Plan, first_id).status == "active"
    assert db.get(m.Plan, second_id).status == "candidate"
    apply_plan(db, second_id)
    result = revoke_plan(db, second_id, commit=False)
    assert result["restored_plan_id"] == first_id
    db.rollback()
    assert db.get(m.Plan, second_id).status == "active"
    assert db.get(m.Plan, first_id).status == "superseded"
    revoke_plan(db, second_id)
    assert db.get(m.Plan, first_id).status == "active"
    assert db.get(m.Plan, second_id).status == "revoked"
