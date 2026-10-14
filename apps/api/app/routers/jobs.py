"""后台任务（/api/v1/jobs…）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import Job, ReviewPaper, utc_now
from ..repo import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    decode_cursor,
    encode_cursor,
    get_or_404,
    idempotency_lookup,
    idempotency_store,
    new_id,
    validate_idempotency_key,
)

router = APIRouter(tags=["jobs"])

JOB_STATUSES = ("queued", "running", "succeeded", "partial_failed", "failed", "cancelled")
JOB_TYPES = ("import", "parse", "sync", "export", "build_tasks", "review_generate")
TERMINAL_STATUSES = ("succeeded", "partial_failed", "failed", "cancelled")


def _accepted(job: Job) -> dict[str, Any]:
    return {"job": job.to_contract()}


@router.get("/jobs")
def list_jobs(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    status: str | None = Query(default=None, pattern="^(" + "|".join(JOB_STATUSES) + ")$"),
    type: str | None = Query(default=None, pattern="^(" + "|".join(JOB_TYPES) + ")$"),
) -> dict[str, Any]:
    stmt = select(Job)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    if type is not None:
        stmt = stmt.where(Job.type == type)
    if cursor is not None:
        created_text, _, job_id = decode_cursor(cursor).partition("|")
        try:
            created = datetime.fromisoformat(created_text)
        except ValueError:
            raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor}) from None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        stmt = stmt.where(or_(Job.created_at < created, and_(Job.created_at == created, Job.id < job_id)))
    rows = list(db.execute(stmt.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        created = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc)
        next_cursor = encode_cursor(f"{created.astimezone(timezone.utc).isoformat()}|{last.id}")
    return {"items": [j.to_contract() for j in rows], "next_cursor": next_cursor}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = get_or_404(db, Job, job_id, "后台任务")
    return job.to_contract()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return cancel_job_mutation(db, job_id)


def cancel_job_mutation(db: Session, job_id: str, *, commit: bool = True) -> dict[str, Any]:
    """协作式取消：排队中直接落 cancelled；执行中仅置 cancel_requested（响应可能仍为 running）。"""
    job = get_or_404(db, Job, job_id, "后台任务")
    if job.status in TERMINAL_STATUSES:
        raise ApiError(
            409,
            "CONFLICT",
            f"任务已结束（{job.status}），无法取消",
            details={"job_id": job_id, "status": job.status},
        )
    claimed = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="cancelled", cancel_requested=True, finished_at=utc_now())
    ).rowcount
    if claimed == 0:  # 已开跑（或其他状态）：交给处理器在取消检查点中止
        db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(cancel_requested=True))
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(job)
    return job.to_contract()


def _retry_payload(db: Session, job: Job) -> dict[str, Any]:
    """构造重试参数：partial_failed 仅保留失败条目（导入的失败文件/同步的失败课程），无法定位时原样全量。"""
    payload = dict(job.payload or {})
    if job.type == "review_generate" and job.status == "partial_failed":
        paper = db.get(ReviewPaper, payload.get("paper_id"))
        if paper is not None and paper.latest_revision >= int(payload.get("revision") or 1):
            payload.update(revision=paper.latest_revision + 1, revision_reason="regenerate",
                           based_on_revision=paper.latest_revision)
        return payload
    if job.status != "partial_failed":
        return payload
    item_key = {"import": "file_id", "sync": "course_id"}.get(job.type)
    if item_key is None:
        return payload
    failed = {
        entry["details"].get(item_key)
        for entry in (job.errors or [])
        if isinstance(entry, dict) and isinstance(entry.get("details"), dict)
    }
    failed.discard(None)
    if not failed:
        return payload
    if job.type == "import" and isinstance(payload.get("files"), list):
        kept = [f for f in payload["files"] if isinstance(f, dict) and f.get("file_id") in failed]
        if kept:
            payload["files"] = kept
    elif job.type == "sync" and isinstance(payload.get("course_ids"), list):
        kept = [c for c in payload["course_ids"] if c in failed]
        if kept:
            payload["course_ids"] = kept
    return payload


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """以原参数重新发起一个新任务；原任务记录不被修改。仅 failed/partial_failed 可用。"""
    validate_idempotency_key(idempotency_key)
    job = get_or_404(db, Job, job_id, "后台任务")
    if job.status not in ("failed", "partial_failed"):
        raise ApiError(
            409,
            "CONFLICT",
            f"仅失败的任务可重试（当前 {job.status}）",
            details={"job_id": job_id, "status": job.status},
        )
    endpoint = f"POST /jobs/{job_id}/retry"
    fingerprint = {"job_id": job_id}
    replay = idempotency_lookup(db, idempotency_key, endpoint, fingerprint)
    if replay is not None:
        status_code, payload = replay
        return JSONResponse(status_code=status_code, content=payload)

    new_job = Job(
        id=new_id(db, Job, "job"),
        type=job.type,
        status="queued",
        progress=0.0,
        errors=[],
        result_refs=[],
        payload=_retry_payload(db, job),
    )
    db.add(new_job)
    db.flush()
    payload = _accepted(new_job)
    idempotency_store(db, idempotency_key, endpoint, fingerprint, 202, payload)
    db.commit()
    return payload
