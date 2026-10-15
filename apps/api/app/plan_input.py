"""计划输入指纹（current_input_revision）：B 侧负责计算，D 的计划引擎只消费。

指纹覆盖所有会影响排程的本地输入：任务关键字段、可用时间、固定日历（生效计划锁定块）、
课表与核验校历缓存证据/可靠性、作业提交状态（PD-3：已提交/已评分作业对应的任务
被排除排程，状态翻转必须使旧计划过期）。校园输入仅查缓存，不自动访问学校或浏览器。
格式 fp_<10 hex>（与契约样例一致）。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Assignment, Availability, LearningTask, Plan, iso_utc
from .campus_schedule_constraints import CampusEvidence, read_campus_evidence


def compute_input_revision(db: Session, *, campus_evidence: CampusEvidence | None = None) -> str:
    parts: list[str] = []
    stmt = select(LearningTask).order_by(LearningTask.id)
    for t in db.execute(stmt).scalars():
        prereqs = ",".join(str(p) for p in (t.prerequisite_ids or []))
        parts.append(
            f"{t.id}|{t.revision}|{t.status}|{iso_utc(t.due_at)}|{t.due_precision}"
            f"|{t.estimated_minutes}|{iso_utc(t.archived_at)}|{prereqs}"
        )
    # PD-3：作业提交状态/修订变化影响排程输入（对应任务排除与否随之变化）
    for a in db.execute(select(Assignment).order_by(Assignment.id)).scalars():
        parts.append(f"assignment|{a.id}|{a.revision}|{a.submission_status}|{iso_utc(a.archived_at)}")
    availability = db.get(Availability, 1)
    parts.append("availability|" + json.dumps(availability.to_contract() if availability else None,
                                            sort_keys=True, ensure_ascii=False))
    # Fixed calendar evidence is the active plan's locked blocks in the current
    # schema. Do not hash plan/block identities: applying a candidate that keeps
    # the same locked instants must not make its own revision stale.
    active = db.execute(select(Plan).where(Plan.status == "active").order_by(Plan.id)).scalars().first()
    fixed = sorted({(b.task_id, iso_utc(b.starts_at), iso_utc(b.ends_at))
                    for b in active.blocks if b.locked}) if active else []
    parts.append("fixed_calendar|" + json.dumps(fixed, ensure_ascii=False))
    evidence = campus_evidence if campus_evidence is not None else read_campus_evidence()
    parts.append("campus|" + json.dumps(evidence.revision_payload(), sort_keys=True, ensure_ascii=False))
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:10]
    return f"fp_{digest}"
