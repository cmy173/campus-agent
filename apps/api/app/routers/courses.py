"""课程与课程下挂载的资源/作业/回放（/api/v1/courses…）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import (
    OWNER_ASSIGNMENT,
    OWNER_COURSE,
    OWNER_RECORDING,
    Assignment,
    Course,
    Recording,
    Resource,
    SourceRef,
    utc_now,
)
from ..repo import DEFAULT_LIMIT, MAX_LIMIT, get_or_404, new_id, normalize_dt, page, refs_for
from ..schemas import AddSourceRefRequest, CreateCourseRequest
from .resources import resource_to_contract

router = APIRouter(tags=["courses"])


def course_to_contract(db: Session, course: Course) -> dict[str, Any]:
    return course.to_contract(refs_for(db, OWNER_COURSE, course.id))


def ensure_course(db: Session, course_id: str) -> Course:
    return get_or_404(db, Course, course_id, "课程")


@router.post("/courses", status_code=201)
def create_course(payload: CreateCourseRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    course = Course(
        id=new_id(db, Course, "crs"),
        title=payload.title,
        term=payload.term,
        teacher=payload.teacher,
    )
    db.add(course)
    db.commit()
    return course_to_contract(db, course)


@router.get("/courses")
def list_courses(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    stmt = select(Course).where(Course.archived_at.is_(None))
    rows, next_cursor = page(db, stmt, Course, cursor, limit)
    return {"items": [course_to_contract(db, c) for c in rows], "next_cursor": next_cursor}


@router.get("/courses/{course_id}")
def get_course(course_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return course_to_contract(db, ensure_course(db, course_id))


@router.get("/courses/{course_id}/resources")
def list_course_resources(course_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    ensure_course(db, course_id)
    stmt = select(Resource).where(Resource.course_id == course_id, Resource.archived_at.is_(None))
    rows = list(db.execute(stmt.order_by(Resource.id)).scalars())
    return {"items": [resource_to_contract(db, r) for r in rows], "next_cursor": None}


@router.get("/courses/{course_id}/assignments")
def list_course_assignments(course_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    ensure_course(db, course_id)
    stmt = select(Assignment).where(Assignment.course_id == course_id, Assignment.archived_at.is_(None))
    rows = list(db.execute(stmt.order_by(Assignment.id)).scalars())
    return {"items": [a.to_contract(refs_for(db, OWNER_ASSIGNMENT, a.id)) for a in rows], "next_cursor": None}


@router.get("/courses/{course_id}/recordings")
def list_course_recordings(course_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    ensure_course(db, course_id)
    stmt = select(Recording).where(Recording.course_id == course_id)
    rows = list(db.execute(stmt.order_by(Recording.id)).scalars())
    return {"items": [r.to_contract(refs_for(db, OWNER_RECORDING, r.id)) for r in rows], "next_cursor": None}


@router.post("/courses/{course_id}/source-refs")
def add_course_source_ref(course_id: str, payload: AddSourceRefRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    course = ensure_course(db, course_id)
    ref = payload.source_ref

    same = (
        db.execute(
            select(SourceRef).where(
                SourceRef.owner_type == OWNER_COURSE,
                SourceRef.owner_id == course_id,
                SourceRef.platform == ref.platform,
                SourceRef.external_id == ref.external_id,
            )
        )
        .scalars()
        .first()
    )
    if same is not None:
        return course_to_contract(db, course)

    bound = (
        db.execute(
            select(SourceRef).where(
                SourceRef.owner_type == OWNER_COURSE,
                SourceRef.platform == ref.platform,
                SourceRef.external_id == ref.external_id,
                SourceRef.owner_id != course_id,
            )
        )
        .scalars()
        .first()
    )
    if bound is not None:
        raise ApiError(
            409,
            "CONFLICT",
            "该平台来源已绑定到其他课程",
            details={"platform": ref.platform, "external_id": ref.external_id, "bound_course_id": bound.owner_id},
        )

    max_seq = db.execute(
        select(func.max(SourceRef.seq)).where(SourceRef.owner_type == OWNER_COURSE, SourceRef.owner_id == course_id)
    ).scalar()
    db.add(
        SourceRef(
            owner_type=OWNER_COURSE,
            owner_id=course_id,
            platform=ref.platform,
            external_id=ref.external_id,
            canonical_url=ref.canonical_url,
            observed_at=normalize_dt(ref.observed_at),
            seq=(max_seq if max_seq is not None else -1) + 1,
        )
    )
    course.updated_at = utc_now()
    db.commit()
    return course_to_contract(db, course)
