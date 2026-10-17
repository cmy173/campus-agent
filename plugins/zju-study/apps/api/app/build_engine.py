"""任务生成接入（B 侧）：DB 行 → D12 集成入口 learning.taskgen.build_tasks_json → 候选任务落库。

映射口径（与 D0 §6「ID 映射采用 match_key 方案」一致）：
- D 输出的 id 是稳定匹配键（t:{course}:{type}:{key}），落库到 LearningTask.match_key；
  行主键仍为不透明 tsk_ id（PATCH/计划引用/反馈都以它为准），重复生成不换 id、不造重复行。
- 既有任务行（该课程全部，含手工与归档）原样传入 existing_tasks：分流由 D 封装承担——
  未归档且有稳定键按 match_key 对齐并保留人工修正；手工任务原样保留；归档行忽略（不复活）。
  已无对应来源的任务由 D 告警（EXISTING_TASK_UNMATCHED）。
- 未变化的任务不递增 revision（计划过期判定依赖 revision 指纹）。
- 纯规则路径（D3）：不调用任何模型；warnings 原样带回 job.warnings 供前端展示。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from learning.taskgen import build_tasks_json
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import models as m
from .config import CONFIG_DEFAULTS_PATH
from .repo import new_id, normalize_dt, refs_for

_FACT_SOURCES = ("platform", "user", "inferred")


@dataclass
class BuildOutcome:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    task_count: int = 0


def load_taskgen_config() -> dict[str, Any]:
    """任务生成参数取自 contracts/config-defaults.json 的 taskgen 段（契约 v0.1 与 D 冻结值）。

    以原始 dict 供 build_tasks_json.config（D 侧 TaskGenConfig.from_dict 消费，缺省回退默认值）。
    """
    try:
        with CONFIG_DEFAULTS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    section = data.get("taskgen")
    return dict(section) if isinstance(section, dict) else {}


# ────────────────────────── DB → 契约形状 JSON（build_tasks_json payload） ──────────────────────────


def _load_resources(db: Session, course_id: str | None) -> list[dict[str, Any]]:
    assignments = select(m.Assignment.attachment_resource_ids).where(m.Assignment.archived_at.is_(None))
    if course_id is not None:
        assignments = assignments.where(m.Assignment.course_id == course_id)
    attachment_ids = {rid for ids in db.scalars(assignments) for rid in (ids or [])}
    textbooks = select(m.Textbook.resource_id).where(m.Textbook.resource_id.is_not(None))
    stmt = select(m.Resource).where(m.Resource.archived_at.is_(None), m.Resource.id.not_in(textbooks),
                                    ~m.Resource.title.startswith('教材：')).order_by(m.Resource.id)
    if course_id is not None:
        stmt = stmt.where(m.Resource.course_id == course_id)
    # 作业附件随作业处理，不再额外生成一份阅读任务。
    return [r.to_contract(refs_for(db, m.OWNER_RESOURCE, r.id)) for r in db.execute(stmt).scalars()
            if r.id not in attachment_ids]


def _load_assignments(db: Session, course_id: str | None) -> list[dict[str, Any]]:
    stmt = select(m.Assignment).where(m.Assignment.archived_at.is_(None)).order_by(m.Assignment.id)
    if course_id is not None:
        stmt = stmt.where(m.Assignment.course_id == course_id)
    return [a.to_contract(refs_for(db, m.OWNER_ASSIGNMENT, a.id)) for a in db.execute(stmt).scalars()]


def _load_recordings(db: Session, course_id: str | None) -> list[dict[str, Any]]:
    stmt = select(m.Recording).order_by(m.Recording.id)
    if course_id is not None:
        stmt = stmt.where(m.Recording.course_id == course_id)
    return [r.to_contract(refs_for(db, m.OWNER_RECORDING, r.id)) for r in db.execute(stmt).scalars()]


def _load_task_rows(db: Session, course_id: str | None) -> list[dict[str, Any]]:
    """该课程全部任务行（含手工与归档），分流由 build_tasks_json 承担（D12 §4）。

    fact_source → estimate_provenance：契约字段名与 D 模型键名的桥接；
    D 修正保留逻辑依赖它回传 provenance（否则重建会把用户/平台来源冲成 inferred）。
    """
    stmt = select(m.LearningTask).order_by(m.LearningTask.id)
    if course_id is not None:
        stmt = stmt.where(m.LearningTask.course_id == course_id)
    rows: list[dict[str, Any]] = []
    for t in db.execute(stmt).scalars():
        row = t.to_contract(refs_for(db, m.OWNER_TASK, t.id))
        row["estimate_provenance"] = row.get("fact_source") or "inferred"
        rows.append(row)
    return rows


# ────────────────────────── D 结果（契约形状 JSON）→ 落库 ──────────────────────────


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return normalize_dt(datetime.fromisoformat(value))


def _fact_source(task: dict[str, Any]) -> str:
    provenance = task.get("estimate_provenance")
    return provenance if provenance in _FACT_SOURCES else "inferred"


def _same_dt(a: datetime | None, b: datetime | None) -> bool:
    # SQLite 取回为 naive：统一经 iso_utc 归一后比较
    return m.iso_utc(a) == m.iso_utc(b)


def _replace_refs(db: Session, task_id: str, refs: list[dict[str, Any]]) -> bool:
    """任务来源引用整体替换（有变化才写）；返回是否发生变化。"""
    current = {(r.platform, r.external_id, r.canonical_url) for r in refs_for(db, m.OWNER_TASK, task_id)}
    target = {(ref.get("platform"), ref.get("external_id"), ref.get("canonical_url")) for ref in refs}
    if current == target:
        return False
    db.execute(
        delete(m.SourceRef).where(m.SourceRef.owner_type == m.OWNER_TASK, m.SourceRef.owner_id == task_id)
    )
    for seq, ref in enumerate(refs):
        observed_at = _parse_dt(ref.get("observed_at")) or m.utc_now()
        db.add(
            m.SourceRef(
                owner_type=m.OWNER_TASK,
                owner_id=task_id,
                platform=ref["platform"],
                external_id=ref["external_id"],
                canonical_url=ref.get("canonical_url"),
                observed_at=observed_at,
                seq=seq,
            )
        )
    return True


def _insert_task(db: Session, task: dict[str, Any], now: datetime) -> m.LearningTask:
    row = m.LearningTask(
        id=new_id(db, m.LearningTask, "tsk"),
        match_key=task["id"],
        course_id=task.get("course_id") or None,
        type=task["type"],
        title=task["title"],
        estimated_minutes=max(1, int(task["estimated_minutes"])),
        due_at=_parse_dt(task.get("due_at")),
        due_precision=task.get("due_precision") or "unknown",
        prerequisite_ids=[str(p) for p in task.get("prerequisite_ids") or []],
        status=task.get("status") or "pending",
        fact_source=_fact_source(task),
        revision=1,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def _update_task(db: Session, row: m.LearningTask, task: dict[str, Any], now: datetime) -> bool:
    changed = False
    due_at = _parse_dt(task.get("due_at"))
    if row.course_id != (task.get("course_id") or None):
        row.course_id = task.get("course_id") or None
        changed = True
    if row.type != task["type"]:
        row.type = task["type"]
        changed = True
    if row.title != task["title"]:
        row.title = task["title"]
        changed = True
    if row.estimated_minutes != max(1, int(task["estimated_minutes"])):
        row.estimated_minutes = max(1, int(task["estimated_minutes"]))
        changed = True
    if not _same_dt(row.due_at, due_at):
        row.due_at = due_at
        changed = True
    if row.due_precision != (task.get("due_precision") or "unknown"):
        row.due_precision = task.get("due_precision") or "unknown"
        changed = True
    prereqs = [str(p) for p in task.get("prerequisite_ids") or []]
    if list(row.prerequisite_ids or []) != prereqs:
        row.prerequisite_ids = prereqs
        changed = True
    status = task.get("status") or "pending"
    if row.status != status:
        row.status = status
        changed = True
    if row.fact_source != _fact_source(task):
        row.fact_source = _fact_source(task)
        changed = True
    if _replace_refs(db, row.id, task.get("source_refs") or []):
        changed = True
    if changed:
        row.revision += 1
        row.updated_at = now
    return changed


def run_build_tasks(db: Session, *, course_id: str | None) -> BuildOutcome:
    """执行一次任务生成并落库（不提交事务：由调用方在取得一致结果后提交）。"""
    result = build_tasks_json(
        {
            "resources": _load_resources(db, course_id),
            "assignments": _load_assignments(db, course_id),
            "recordings": _load_recordings(db, course_id),
            "existing_tasks": _load_task_rows(db, course_id),
            "config": load_taskgen_config(),
        }
    )
    outcome = BuildOutcome(warnings=list(result["warnings"]), task_count=len(result["tasks"]))
    now = m.utc_now()
    for task in result["tasks"]:
        row = db.execute(
            select(m.LearningTask).where(m.LearningTask.match_key == task["id"])
        ).scalars().first()
        if row is None:
            row = _insert_task(db, task, now)
            _replace_refs(db, row.id, task.get("source_refs") or [])
            outcome.created.append(row.id)
        elif row.archived_at is not None:
            continue  # 已归档：重建不复活、不改写
        elif _update_task(db, row, task, now):
            outcome.updated.append(row.id)
    return outcome


def enqueue_build_tasks_job(db: Session, *, course_id: str | None, reason: str) -> m.Job:
    """创建/复用任务生成任务：同课程范围已有排队/执行中任务时复用（去抖）。不提交事务。"""
    active = db.execute(
        select(m.Job).where(m.Job.type == "build_tasks", m.Job.status.in_(("queued", "running")))
    ).scalars()
    for existing in active:
        if (existing.payload or {}).get("course_id") == course_id:
            return existing
    job = m.Job(
        id=new_id(db, m.Job, "job"),
        type="build_tasks",
        status="queued",
        progress=0.0,
        errors=[],
        result_refs=[],
        payload={"course_id": course_id, "reason": reason},
    )
    db.add(job)
    return job
