"""Conservative cross-platform course identity and non-destructive consolidation."""
import re
import unicodedata
from sqlalchemy import select, update
from .db import Base
from .models import Course, SourceRef, Job, SyncCursor, OWNER_COURSE, utc_now


def text_key(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))


def term_key(value):
    value = text_key(value)
    match = re.fullmatch(r"(\d{4}-\d{4})(秋冬|秋|冬|春夏|春|夏|1|2)", value)
    if match:
        return match[1] + (":1" if match[2] in ("秋冬", "秋", "冬", "1") else ":2")
    return value


def metadata_compatible(course, term, teacher):
    return (not course.term or not term or term_key(course.term) == term_key(term)) and (
        not course.teacher or not teacher or text_key(course.teacher) == text_key(teacher))


def course_platforms(db, course_id):
    return set(db.execute(select(SourceRef.platform).where(
        SourceRef.owner_type == OWNER_COURSE, SourceRef.owner_id == course_id)).scalars())


def matching_course(db, platform, title, term, teacher):
    candidates = [course for course in db.execute(select(Course).where(Course.archived_at.is_(None))).scalars()
                  if text_key(course.title) == text_key(title)
                  and platform not in course_platforms(db, course.id)
                  and course_platforms(db, course.id)
                  and metadata_compatible(course, term, teacher)]
    return candidates[0] if len(candidates) == 1 else None


def enrich_course(course, term, teacher):
    # Sparse platform responses must never erase existing metadata.
    if not course.term and term:
        course.term = term
    if not course.teacher and teacher:
        course.teacher = teacher


def consolidate(db, target, duplicate):
    """Move live materials, preserve immutable snapshots and the old course record."""
    enrich_course(target, duplicate.term, duplicate.teacher)
    refs = list(db.execute(select(SourceRef).where(SourceRef.owner_type == OWNER_COURSE,
                                                  SourceRef.owner_id == duplicate.id)).scalars())
    for ref in refs:
        ref.owner_id = target.id
    for cursor in list(db.execute(select(SyncCursor).where(SyncCursor.course_id == duplicate.id)).scalars()):
        existing = db.get(SyncCursor, (cursor.connection_id, target.id))
        if existing is None:
            cursor.course_id = target.id
        else:
            if cursor.updated_at > existing.updated_at:
                existing.cursor, existing.mode, existing.updated_at = cursor.cursor, cursor.mode, cursor.updated_at
            db.delete(cursor)
    db.flush()
    for table in Base.metadata.sorted_tables:
        if "course_id" in table.c and table.name not in ("sync_cursors", "review_material_snapshots"):
            db.execute(update(table).where(table.c.course_id == duplicate.id).values(course_id=target.id))
    for job in db.execute(select(Job).where(Job.status.in_(("failed", "partial_failed", "cancelled")))).scalars():
        payload = dict(job.payload or {})
        changed = False
        if payload.get("course_id") == duplicate.id:
            payload["course_id"] = target.id
            changed = True
        if duplicate.id in (payload.get("course_ids") or []):
            payload["course_ids"] = list(dict.fromkeys(target.id if value == duplicate.id else value for value in payload["course_ids"]))
            changed = True
        if changed:
            job.payload = payload
    duplicate.archived_at = utc_now()
    target.updated_at = utc_now()
    db.flush()


def repair_duplicate_courses(db):
    # Do not change the mapping underneath an executing/queued job.
    if db.execute(select(Job.id).where(Job.status.in_(("queued", "running"))).limit(1)).first():
        return []
    courses = list(db.execute(select(Course).where(Course.archived_at.is_(None))).scalars())
    repaired = []
    for duplicate in courses:
        if duplicate.archived_at or course_platforms(db, duplicate.id) != {"zykt"}:
            continue
        target = matching_course(db, "zykt", duplicate.title, duplicate.term, duplicate.teacher)
        if target and "xzzd" in course_platforms(db, target.id):
            consolidate(db, target, duplicate)
            repaired.append((duplicate.id, target.id))
    return repaired
