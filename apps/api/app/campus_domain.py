"""Bridge validated campus notices into the ordinary material/task/review pipeline."""
from datetime import datetime, timezone
import hashlib
from sqlalchemy import select

from . import models as m
from .errors import ApiError
from .repo import new_id
from .routers.resources import resource_to_contract
from .campus_workspace import get_campus_workspace, OWNER
from connectors.campus.connector_contracts import UserContext


def import_notice(db, course_id, record_id):
    # Campus is a single local API process; serialize source admission/versioning
    # with the same owner lock used by cache refresh and disconnect.
    with get_campus_workspace().manager.lock(OWNER):
        return _import_notice_locked(db, course_id, record_id)


def _import_notice_locked(db, course_id, record_id):
    course = db.get(m.Course, course_id)
    if course is None or course.archived_at:
        raise ApiError(404, "NOT_FOUND", "课程不存在")
    found = get_campus_workspace().manager.service.cached_college_notice(UserContext(OWNER), record_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", "校园缓存中没有该通知")
    platform, notice = found
    try:
        observed = datetime.fromisoformat((notice.fetched_at or "").replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError()
        observed = observed.astimezone(timezone.utc)
    except ValueError:
        raise ApiError(409, "CACHE_PROVENANCE_MISSING", "通知缺少有效获取时间，请先同步通知再导入") from None
    digest = "sha256:"+hashlib.sha256(notice.body.encode()).hexdigest()
    ref = db.scalars(select(m.SourceRef).where(m.SourceRef.owner_type == m.OWNER_RESOURCE,
        m.SourceRef.platform == platform, m.SourceRef.external_id == record_id)).first()
    resource = db.get(m.Resource, ref.owner_id) if ref else None
    if resource is not None and resource.course_id != course_id:
        raise ApiError(409, "COURSE_CONFLICT", "通知已属于另一课程；请读取已导入资料再决定是否调整")
    if resource is not None and resource.content_hash == digest:
        return {"resource": resource_to_contract(db, resource), "replayed": True,
                "attachments": [a.model_dump() for a in notice.attachments], "has_unread_content": notice.has_unread_content}
    now = m.utc_now()
    try:
        if resource is None:
            resource = m.Resource(id=new_id(db, m.Resource, "res"), course_id=course_id,
                kind="document", title=notice.title, version=1, content_hash=digest,
                parse_status="ready", mime_type="text/plain", size_bytes=len(notice.body.encode()),
                access_level="full", fetched_at=observed)
            db.add(resource)
            db.flush()
            ref = m.SourceRef(owner_type=m.OWNER_RESOURCE, owner_id=resource.id, platform=platform,
                external_id=record_id, canonical_url=notice.source, observed_at=observed, seq=0)
            db.add(ref)
        else:
            resource.version += 1
            resource.content_hash, resource.title = digest, notice.title
            resource.size_bytes, resource.fetched_at, resource.updated_at = len(notice.body.encode()), observed, now
            ref.observed_at = observed
        job = m.Job(id=new_id(db, m.Job, "job"), type="import", status="succeeded", progress=1,
            phase="completed", errors=[], warnings=[{"code": "UNREAD_ATTACHMENTS", "message": "仅导入通知正文；附件或图片未解析，不能推断其中要求"}] if notice.has_unread_content or notice.attachments else [],
            result_refs=[{"type": "resource", "id": resource.id}], payload={"source": "campus.notice.import", "record_id": record_id},
            started_at=now, finished_at=now)
        db.add(job)
        db.add(m.ResourceVersion(resource_id=resource.id, version=resource.version, content_hash=digest,
            mime_type="text/plain", size_bytes=resource.size_bytes, parse_status="ready", fetched_at=observed))
        for ordinal, start in enumerate(range(0, len(notice.body), 6000)):
            text = notice.body[start:start+6000]
            db.add(m.ContentChunk(id=new_id(db, m.ContentChunk, "chk"), resource_id=resource.id,
                resource_version=resource.version, ordinal=ordinal, text=text,
                locator={"kind": "text", "start_offset": start, "end_offset": start+len(text)},
                provenance={"extractor": "campus-notice-cache", "extractor_version": "v2",
                    "job_id": job.id, "fact_source": "platform", "observed_at": m.iso_utc(observed)}))
        db.commit()
        return {"resource": resource_to_contract(db, resource), "job": job.to_contract(), "replayed": False,
                "attachments": [a.model_dump() for a in notice.attachments], "has_unread_content": notice.has_unread_content}
    except Exception:
        db.rollback()
        raise
