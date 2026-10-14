"""计划输入指纹（current_input_revision）：B 侧负责计算，D 的计划引擎只消费。

指纹覆盖所有会影响排程的本地输入：任务关键字段、可用时间、作业提交状态（PD-3：
已提交/已评分作业对应的任务被排除排程，状态翻转必须使旧计划过期）。
格式 fp_<10 hex>（与契约样例一致）。
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Assignment, Availability, LearningTask, iso_utc


def compute_input_revision(db: Session) -> str:
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
    parts.append("availability|" + (iso_utc(availability.updated_at) if availability else ""))
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:10]
    return f"fp_{digest}"
