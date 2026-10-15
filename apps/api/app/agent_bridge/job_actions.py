"""Actual job creation and ownership survive a lost response or journal restart."""
from sqlalchemy import select
from .. import models as m
from ..build_engine import enqueue_build_tasks_job
from ..errors import ApiError
from ..routers.jobs import cancel_job_mutation
from .plan_actions import fingerprint


def owns(db, conversation_id, job_id, connection_ids=None, *, course_ids=None):
    """Ownership receipt, plus the platform-sync scope rule.

    `tasks.build` jobs carry a per-conversation receipt. A platform sync job belongs to a
    *connection* rather than to one course, so it is owned when the conversation is
    authorized for that connection — the same grant that let the agent start it.
    `connection_ids=None` means the conversation has no per-connection restriction.

    A `review_generate` job is a charged, model-calling job: it is owned only through this
    conversation's own `reviews.generate` receipt, never by knowing its id.
    """
    if db.scalar(select(m.AgentJobRequest.id).where(m.AgentJobRequest.conversation_id == conversation_id,
                                                   m.AgentJobRequest.job_id == job_id)) is not None:
        return True
    if db.scalar(select(m.AgentReviewReceipt.id).where(m.AgentReviewReceipt.conversation_id == conversation_id,
                                                       m.AgentReviewReceipt.job_id == job_id)) is not None:
        return True
    job = db.get(m.Job, job_id)
    if job is None or getattr(job, "type", None) != "sync":
        return False  # unknown ids fail closed instead of leaking existence through a 404
    connection_id = (job.payload or {}).get("connection_id")
    if not connection_id:
        return False
    if course_ids is not None and not set(job.payload.get('course_ids') or []).issubset(course_ids):
        return False
    if connection_ids is None:
        # No per-connection restriction for this conversation: the job must still name a
        # connection that actually exists in the workspace.
        return db.get(m.Connection, connection_id) is not None
    return connection_id in connection_ids


def build(db, conversation_id, request_key, course_id):
    identity = "req_" + fingerprint([conversation_id, request_key, "tasks.build"])
    digest = fingerprint({"course_id": course_id})
    existing = db.get(m.AgentJobRequest, identity)
    if existing:
        if existing.request_hash != digest:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一生成请求不能更换课程")
        return db.get(m.Job, existing.job_id).to_contract()
    try:
        job = enqueue_build_tasks_job(db, course_id=course_id, reason="agent")
        db.flush()
        db.add(m.AgentJobRequest(id=identity, conversation_id=conversation_id, request_hash=digest,
                                course_id=course_id, job_id=job.id))
        db.commit()
        return job.to_contract()
    except Exception:
        db.rollback()
        raise


def cancel(db, conversation_id, job_id, connection_ids=None, *, course_ids=None):
    if not owns(db, conversation_id, job_id, connection_ids, course_ids=course_ids):
        raise ApiError(403, "SCOPE_DENIED", "只能停止当前会话发起的后台任务")
    job = db.get(m.Job, job_id)
    if job.status == "cancelled" or (job.status == "running" and job.cancel_requested):
        return job.to_contract()
    return cancel_job_mutation(db, job_id)
