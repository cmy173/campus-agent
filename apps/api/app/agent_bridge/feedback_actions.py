"""Feedback mutation and permanent receipt commit together; never submit school work."""
from sqlalchemy import update
from .. import models as m
from ..errors import ApiError
from ..routers.tasks import record_feedback
from ..schemas import FeedbackRequest
from .plan_actions import fingerprint


def record(db, conversation_id, request_key, arguments):
    identity = "fbreq_" + fingerprint([conversation_id, request_key])
    digest = fingerprint(arguments)
    old = db.get(m.AgentFeedbackReceipt, identity)
    if old:
        if old.request_hash != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一反馈请求不能更换内容")
        return old.result
    try:
        claimed = db.execute(update(m.LearningTask).where(m.LearningTask.id == arguments["task_id"],
            m.LearningTask.revision == arguments["expected_revision"]).values(revision=m.LearningTask.revision))
        if claimed.rowcount != 1:
            raise ApiError(409, "TASK_STALE", "任务已变化，请重新读取后反馈")
        db.expire_all()
        payload = FeedbackRequest(**{k: v for k, v in arguments.items() if k not in {"task_id", "expected_revision"}})
        result = record_feedback(db, arguments["task_id"], payload)
        db.add(m.AgentFeedbackReceipt(id=identity, conversation_id=conversation_id,
            task_id=arguments["task_id"], request_hash=digest, result=result))
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
