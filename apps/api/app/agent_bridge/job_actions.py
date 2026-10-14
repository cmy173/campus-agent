"""Actual job creation and ownership survive a lost response or journal restart."""
from sqlalchemy import select
from .. import models as m
from ..build_engine import enqueue_build_tasks_job
from ..errors import ApiError
from ..routers.jobs import cancel_job_mutation
from .plan_actions import fingerprint


def owns(db, conversation_id, job_id):
    return db.scalar(select(m.AgentJobRequest.id).where(m.AgentJobRequest.conversation_id == conversation_id,
                                                       m.AgentJobRequest.job_id == job_id)) is not None


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


def cancel(db, conversation_id, job_id):
    if not owns(db, conversation_id, job_id):
        raise ApiError(403, "SCOPE_DENIED", "只能停止当前会话发起的后台任务")
    job = db.get(m.Job, job_id)
    if job.status == "cancelled" or (job.status == "running" and job.cancel_requested):
        return job.to_contract()
    return cancel_job_mutation(db, job_id)
