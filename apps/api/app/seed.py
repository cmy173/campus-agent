"""开发数据装载：把 contracts/examples/api 的契约样例写入本地数据库（清空重灌）。

用法：
    python -m alembic upgrade head   # 建表（或由本脚本自动 create_all）
    python -m app.seed

注意：会清空数据库中现有数据，仅用于开发/演示环境。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import models as m
from .config import EXAMPLES_DIR
from .db import Base, SessionLocal, engine
from .plan_input import compute_input_revision

API_DIR = Path(EXAMPLES_DIR) / "api"


def _load(name: str) -> dict[str, Any]:
    with (API_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _add_refs(db: Session, owner_type: str, owner_id: str, refs: list[dict] | None) -> None:
    for seq, ref in enumerate(refs or []):
        db.add(
            m.SourceRef(
                owner_type=owner_type,
                owner_id=owner_id,
                platform=ref["platform"],
                external_id=ref["external_id"],
                canonical_url=ref.get("canonical_url"),
                observed_at=_dt(ref["observed_at"]),
                seq=seq,
            )
        )


def seed_all(db: Session) -> dict[str, int]:
    for model in (
        m.PlanWarning,
        m.PlanUnassigned,
        m.PlanBlock,
        m.Plan,
        m.IdempotencyRecord,
        m.Job,
        m.SourceRef,
        m.ContentChunk,
        m.ResourceVersion,
        m.Resource,
        m.LearningTask,
        m.Assignment,
        m.Recording,
        m.Connection,
        m.Availability,
        m.Course,
    ):
        db.execute(delete(model))

    counts: dict[str, int] = {}

    courses = _load("courses.json")["items"]
    for c in courses:
        db.add(
            m.Course(
                id=c["id"],
                title=c["title"],
                term=c["term"],
                teacher=c["teacher"],
                archived_at=_dt(c["archived_at"]),
                created_at=_dt(c["created_at"]),
                updated_at=_dt(c["updated_at"]),
            )
        )
        _add_refs(db, m.OWNER_COURSE, c["id"], c["source_refs"])
    counts["courses"] = len(courses)

    resources = _load("resources.json")["items"]
    for r in resources:
        db.add(
            m.Resource(
                id=r["id"],
                course_id=r["course_id"],
                kind=r["kind"],
                title=r["title"],
                version=r["version"],
                content_hash=r["content_hash"],
                parse_status=r["parse_status"],
                mime_type=r["mime_type"],
                size_bytes=r["size_bytes"],
                access_level=r["access_level"],
                file_id=r["file_id"],
                extent_value=r["extent_value"],
                extent_unit=r["extent_unit"],
                fetched_at=_dt(r["fetched_at"]),
                source_updated_at=_dt(r["source_updated_at"]),
                archived_at=_dt(r["archived_at"]),
                created_at=_dt(r["created_at"]),
                updated_at=_dt(r["updated_at"]),
            )
        )
        _add_refs(db, m.OWNER_RESOURCE, r["id"], r["source_refs"])
    counts["resources"] = len(resources)

    versions = _load("resource-versions.json")["items"]
    for v in versions:
        db.add(
            m.ResourceVersion(
                resource_id=v["resource_id"],
                version=v["version"],
                content_hash=v["content_hash"],
                mime_type=v["mime_type"],
                size_bytes=v["size_bytes"],
                file_id=v["file_id"],
                extent_value=v["extent_value"],
                extent_unit=v["extent_unit"],
                fetched_at=_dt(v["fetched_at"]),
                parse_status=v["parse_status"],
                source_updated_at=_dt(v["source_updated_at"]),
                created_at=_dt(v["created_at"]),
            )
        )
    counts["resource_versions"] = len(versions)

    chunks = _load("content-chunks.json")["items"]
    for c in chunks:
        db.add(
            m.ContentChunk(
                id=c["id"],
                resource_id=c["resource_id"],
                resource_version=c["resource_version"],
                ordinal=c["ordinal"],
                text=c["text"],
                locator=c["locator"],
                provenance=c["provenance"],
                created_at=_dt(c["created_at"]),
            )
        )
    counts["content_chunks"] = len(chunks)

    assignments = _load("assignments.json")["items"]
    for a in assignments:
        db.add(
            m.Assignment(
                id=a["id"],
                course_id=a["course_id"],
                title=a["title"],
                instructions=a["instructions"],
                due_at=_dt(a["due_at"]),
                due_precision=a["due_precision"],
                submission_status=a["submission_status"],
                fact_source=a["fact_source"],
                confidence=a["confidence"],
                attachment_resource_ids=a["attachment_resource_ids"],
                revision=a["revision"],
                observed_at=_dt(a["observed_at"]),
                archived_at=_dt(a["archived_at"]),
                created_at=_dt(a["created_at"]),
                updated_at=_dt(a["updated_at"]),
            )
        )
        if a.get("source_ref"):
            _add_refs(db, m.OWNER_ASSIGNMENT, a["id"], [a["source_ref"]])
    counts["assignments"] = len(assignments)

    recordings = _load("recordings.json")["items"]
    for r in recordings:
        db.add(
            m.Recording(
                id=r["id"],
                course_id=r["course_id"],
                title=r["title"],
                started_at=_dt(r["started_at"]),
                duration_seconds=r["duration_seconds"],
                canonical_url=r["canonical_url"],
                transcript_status=r["transcript_status"],
                subtitle_languages=r["subtitle_languages"],
                transcript_next_check_at=_dt(r["transcript_next_check_at"]),
                fact_source=r["fact_source"],
                observed_at=_dt(r["observed_at"]),
                created_at=_dt(r["created_at"]),
                updated_at=_dt(r["updated_at"]),
            )
        )
        if r.get("source_ref"):
            _add_refs(db, m.OWNER_RECORDING, r["id"], [r["source_ref"]])
    counts["recordings"] = len(recordings)

    tasks = _load("tasks.json")["items"]
    for t in tasks:
        db.add(
            m.LearningTask(
                id=t["id"],
                course_id=t["course_id"],
                type=t["type"],
                title=t["title"],
                estimated_minutes=t["estimated_minutes"],
                due_at=_dt(t["due_at"]),
                due_precision=t["due_precision"],
                prerequisite_ids=t["prerequisite_ids"],
                status=t["status"],
                fact_source=t["fact_source"],
                match_key=t["match_key"],
                revision=t["revision"],
                archived_at=_dt(t["archived_at"]),
                created_at=_dt(t["created_at"]),
                updated_at=_dt(t["updated_at"]),
            )
        )
        _add_refs(db, m.OWNER_TASK, t["id"], t["source_refs"])
    counts["learning_tasks"] = len(tasks)

    plans = _load("plans.json")["items"]
    for p in plans:
        db.add(
            m.Plan(
                id=p["id"],
                version=p["version"],
                status=p["status"],
                input_revision=p["input_revision"],
                horizon_start=_date(p["horizon_start"]),
                horizon_end=_date(p["horizon_end"]),
                summary=p["summary"],
                based_on_plan_id=p["based_on_plan_id"],
                superseded_by=p["superseded_by"],
                created_at=_dt(p["created_at"]),
                applied_at=_dt(p["applied_at"]),
                revoked_at=_dt(p["revoked_at"]),
            )
        )
        for b in p["blocks"]:
            db.add(
                m.PlanBlock(
                    id=b["id"],
                    plan_id=p["id"],
                    task_id=b["task_id"],
                    starts_at=_dt(b["starts_at"]),
                    ends_at=_dt(b["ends_at"]),
                    locked=b["locked"],
                )
            )
        for u in p["unassigned"]:
            db.add(
                m.PlanUnassigned(
                    plan_id=p["id"],
                    task_id=u["task_id"],
                    reason_code=u["reason_code"],
                    shortage_minutes=u["shortage_minutes"],
                    message=u["message"],
                    suggestion=u["suggestion"],
                )
            )
        for w in p["warnings"]:
            db.add(
                m.PlanWarning(
                    plan_id=p["id"],
                    code=w["code"],
                    message=w["message"],
                    task_ids=w["task_ids"],
                    details=w.get("details"),
                )
            )
    counts["plans"] = len(plans)

    capabilities = _load("capabilities-xzzd.json")
    connections = _load("connections.json")["items"]
    for c in connections:
        db.add(
            m.Connection(
                id=c["id"],
                platform=c["platform"],
                display_name=c["display_name"],
                status=c["status"],
                last_checked_at=_dt(c["last_checked_at"]),
                last_sync_at=_dt(c["last_sync_at"]),
                last_error=c["last_error"],
                recovery_action=c["recovery_action"],
                capabilities=capabilities if capabilities["connection_id"] == c["id"] else None,
                created_at=_dt(c["created_at"]),
                updated_at=_dt(c["updated_at"]),
            )
        )
    counts["connections"] = len(connections)

    jobs = _load("jobs.json")["items"]
    for j in jobs:
        db.add(
            m.Job(
                id=j["id"],
                type=j["type"],
                status=j["status"],
                progress=j["progress"],
                errors=j["errors"],
                warnings=j.get("warnings", []),
                result_refs=j["result_refs"],
                cancel_requested=j["cancel_requested"],
                phase=j.get("phase"),
                created_at=_dt(j["created_at"]),
                started_at=_dt(j["started_at"]),
                finished_at=_dt(j["finished_at"]),
            )
        )
    counts["jobs"] = len(jobs)

    availability = _load("availability.json")
    db.add(
        m.Availability(
            id=1,
            timezone=availability["timezone"],
            weekly_windows=availability["weekly_windows"],
            date_exceptions=availability["date_exceptions"],
            daily_limit_minutes=availability["daily_limit_minutes"],
            updated_at=_dt(availability["updated_at"]),
        )
    )
    counts["availability"] = 1

    # 样例中的 input_revision 只是示意值；装载后统一重算为当前数据指纹，保证 /plans/current 自洽
    db.flush()
    revision = compute_input_revision(db)
    for plan in db.execute(select(m.Plan)).scalars():
        plan.input_revision = revision

    db.commit()
    return counts


def main() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        counts = seed_all(db)
    print("已装载契约样例数据：" + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
