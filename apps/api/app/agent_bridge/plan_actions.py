"""Conversation-bound previews and atomic receipts. Approval is a browser-only service.

No model parameter grants approval. Callers must establish browser ownership before
approve(); tool execution may only consume an already approved action.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import models as m
from ..errors import ApiError
from ..plan_engine import active_plan, preview_candidate
from ..plan_input import compute_input_revision
from ..plan_mutations import apply_plan, revoke_plan
from ..repo import normalize_dt


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _snapshot(plan):
    return plan.to_contract() if plan is not None else None


def _owned(db, conversation_id, action_id):
    action = db.get(m.AgentPlanAction, action_id)
    if action is None or action.conversation_id != conversation_id:
        raise ApiError(403, "SCOPE_DENIED", "当前会话无权使用该计划动作")
    return action


def view(action):
    return {"id": action.id, "kind": action.kind, "status": action.status,
            "plan_id": action.plan_id, "preview": action.payload,
            "expires_at": m.iso_utc(action.expires_at), "result": action.result}


def _existing(db, conversation_id, key, kind, request):
    identity = "act_" + fingerprint([conversation_id, key, kind])
    digest = fingerprint(request)
    found = db.get(m.AgentPlanAction, identity)
    if found is not None and found.request_hash != digest:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同的计划参数")
    return identity, digest, found


def _blocks(plan):
    return plan["blocks"] if plan else []


def _block_key(block):
    return fingerprint({k: block[k] for k in ("task_id", "starts_at", "ends_at", "locked")})


def _diff(before, after):
    old = {_block_key(b): b for b in _blocks(before)}
    new = {_block_key(b): b for b in _blocks(after)}
    return {"added": [b for k, b in new.items() if k not in old],
            "removed": [b for k, b in old.items() if k not in new],
            "retained": [b for k, b in new.items() if k in old]}


def _titles(db, *plans):
    ids = {b["task_id"] for plan in plans for b in _blocks(plan)}
    return dict(db.execute(select(m.LearningTask.id, m.LearningTask.title).where(m.LearningTask.id.in_(ids))).all())


def preview(db: Session, conversation_id: str, request_key: str, start, end, *, blocked=None, windows=None):
    request = {"start": start.isoformat(), "end": end.isoformat(), "blocked": blocked, "windows": windows}
    identity, digest, found = _existing(db, conversation_id, request_key, "apply", request)
    if found is not None:
        return view(found)
    before = _snapshot(active_plan(db))
    # Leave time to inspect and accept today's plan instead of creating a block
    # in the current minute that is already stale when the next turn starts.
    plan = preview_candidate(db, start, end, True, temporary_blocked=blocked, temporary_windows=windows,
                             not_before=m.utc_now() + timedelta(minutes=10), commit=False)
    after = _snapshot(plan)
    action = m.AgentPlanAction(id=identity, conversation_id=conversation_id, kind="apply", request_hash=digest,
        plan_id=plan.id, status="pending", expires_at=m.utc_now() + timedelta(minutes=30),
        payload={"plan": after, "active_plan": before, "constraints": request, "diff": _diff(before, after),
                 "input_revision": plan.input_revision, "task_titles": _titles(db, before, after)}, result=None)
    db.add(action)
    db.commit()
    return view(action)


def prepare_revoke(db: Session, conversation_id: str, applied_action_id: str, request_key: str):
    applied = _owned(db, conversation_id, applied_action_id)
    if applied.kind != "apply" or applied.status != "completed":
        raise ApiError(409, "CONFLICT", "只能撤销本会话已经应用的方案")
    identity, digest, found = _existing(db, conversation_id, request_key, "revoke", {"applied_action_id": applied_action_id})
    if found is not None:
        return view(found)
    plan = db.get(m.Plan, applied.plan_id)
    if plan is None or plan.status != "active":
        raise ApiError(409, "PLAN_STALE", "该方案已不再生效，不能撤销其他安排")
    restored_id = applied.result["previous_plan_id"]
    restored = _snapshot(db.get(m.Plan, restored_id)) if restored_id else None
    before = _snapshot(plan)
    action = m.AgentPlanAction(id=identity, conversation_id=conversation_id, kind="revoke", request_hash=digest,
        plan_id=plan.id, status="pending", expires_at=m.utc_now() + timedelta(minutes=30),
        payload={"plan": before, "active_plan": before, "restore_plan": restored,
                 "diff": _diff(before, restored), "input_revision": compute_input_revision(db),
                 "task_titles": _titles(db, before, restored)}, result=None)
    db.add(action)
    db.commit()
    return view(action)


def _validate(db, action):
    if normalize_dt(action.expires_at) <= m.utc_now():
        raise ApiError(409, "ACTION_EXPIRED", "方案确认时间已过，请重新预览")
    plan = db.get(m.Plan, action.plan_id)
    if (fingerprint(_snapshot(plan)) != fingerprint(action.payload["plan"])
            or fingerprint(_snapshot(active_plan(db))) != fingerprint(action.payload["active_plan"])
            or compute_input_revision(db) != action.payload["input_revision"]):
        raise ApiError(409, "PLAN_STALE", "方案、锁定安排或任务输入已变化，请重新预览")
    if action.kind == "revoke":
        saved = action.payload["restore_plan"]
        current = _snapshot(db.get(m.Plan, saved["id"])) if saved else None
        if fingerprint(current) != fingerprint(saved):
            raise ApiError(409, "PLAN_STALE", "将恢复的原方案已变化，请重新检查")
    else:
        for block in action.payload["diff"]["added"]:
            if normalize_dt(datetime.fromisoformat(block["starts_at"].replace("Z", "+00:00"))) < m.utc_now():
                raise ApiError(409, "PLAN_STALE", "方案中的新增安排已开始，请重新预览")


def approve(db: Session, conversation_id: str, action_id: str):
    """Only call after authenticating a direct browser acceptance of this action ID."""
    action = _owned(db, conversation_id, action_id)
    if action.status == "completed":
        return view(action)
    _validate(db, action)
    if action.status == "pending":
        db.execute(update(m.AgentPlanAction).where(m.AgentPlanAction.id == action.id,
            m.AgentPlanAction.status == "pending").values(status="approved", approved_at=m.utc_now()))
        db.commit()
        db.refresh(action)
    return view(action)


def execute(db: Session, conversation_id: str, action_id: str, kind: str):
    action = _owned(db, conversation_id, action_id)
    if action.kind != kind:
        raise ApiError(400, "INVALID_INPUT", "动作类型不匹配")
    if action.status == "completed":
        return action.result
    if action.status != "approved":
        raise ApiError(403, "APPROVAL_REQUIRED", "请先由用户接受这个具体方案")
    try:
        # Claim and domain mutation share a DB transaction. A crash rolls both back.
        claimed = db.execute(update(m.AgentPlanAction).where(m.AgentPlanAction.id == action.id,
            m.AgentPlanAction.status == "approved").values(status="executing"))
        if claimed.rowcount != 1:
            raise ApiError(409, "ACTION_BUSY", "该动作正在执行，请读取其结果后重试")
        db.expire_all()
        action = _owned(db, conversation_id, action_id)
        _validate(db, action)
        result = (apply_plan if kind == "apply" else revoke_plan)(db, action.plan_id, commit=False)
        action.result = result
        action.status = "completed"
        action.completed_at = m.utc_now()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
