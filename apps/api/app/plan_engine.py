"""计划引擎接入（B 侧）：DB 输入 → D 计划引擎（packages/learning）→ 候选计划落库。

边界换算（契约/B 约定 ↔ D 引擎约定）：
- weekday：契约 1=周一..7=周日 → 引擎 0=周一..6=周日（减 1）；
- 日期例外：契约 windows=[] 表示当日不可用 → 引擎 unavailable=True；note 为契约扩展字段，引擎无，忽略；
- 任务状态：in_progress（学习进行中）按 pending 参与排程；archived/cancelled 不参与；
  已提交/已评分作业对应的任务保留在库但不排程（PD-3，按来源引用关联）；引用了被排除
  任务的前置依赖直接剔除（视为不再需要），引用不存在任务仍交由引擎告警；
- 计划范围：契约 horizon_start/horizon_end 为含首尾的日期，引擎展开窗口用半开区间 → end 传次日 00:00；
- 未安排缺口：契约 shortage_minutes = needed - available（DEADLINE_PASSED 按样例为 null）。
"""
from __future__ import annotations

import datetime as dt
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from learning.models import Availability as EngineAvailability
from learning.models import DateException, LearningTask as EngineTask, PlanBlock as EngineBlock
from learning.models import WeeklyWindow, WindowSpan
from learning.schedule.engine import ScheduleConfig, ScheduleOptions, generate_plan

from . import models as m
from .config import CONFIG_DEFAULTS_PATH
from .campus_schedule_constraints import read_campus_evidence, campus_blocks, subtract_campus_blocks
from .errors import ApiError
from .plan_input import compute_input_revision
from .repo import new_id, normalize_dt, refs_for

DEFAULT_HORIZON_DAYS = 14  # 缺省范围：今天起 14 天（含首尾）


def load_schedule_config() -> ScheduleConfig:
    """排程参数取自 contracts/config-defaults.json 的 schedule 段（契约 v0.1 与 D 冻结值）。"""
    try:
        with CONFIG_DEFAULTS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return ScheduleConfig()
    return ScheduleConfig.from_dict(data.get("schedule"))


def resolve_user_tz(name: str) -> dt.tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        if name == "Asia/Shanghai":
            # Windows 精简环境可能缺 tzdata；中国自 1991 年起无夏令时，固定 +08:00 等价
            return timezone(timedelta(hours=8), name=name)
        raise ApiError(400, "VALIDATION_FAILED", f"未知或不可用的时区：{name}", details={"timezone": name}) from None


def active_plan(db: Session) -> m.Plan | None:
    return db.execute(select(m.Plan).where(m.Plan.status == "active").order_by(m.Plan.id)).scalars().first()


def _availability_row(db: Session) -> m.Availability:
    row = db.get(m.Availability, 1)
    if row is None:
        raise ApiError(
            409,
            "CONFLICT",
            "尚未设置可用时间，无法生成计划（请先配置 /api/v1/preferences/availability）",
            details={"endpoint": "/api/v1/preferences/availability"},
        )
    return row


def resolve_horizon(horizon_start: date | None, horizon_end: date | None, tz: dt.tzinfo) -> tuple[date, date]:
    """补全缺省（今天起 14 天）并校验：仅给一端时另一端按其推算保持 14 天窗口。"""
    today = datetime.now(tz).date()
    if horizon_start is None and horizon_end is None:
        return today, today + timedelta(days=DEFAULT_HORIZON_DAYS - 1)
    if horizon_start is None:
        horizon_start = horizon_end - timedelta(days=DEFAULT_HORIZON_DAYS - 1)  # type: ignore[operator]
    if horizon_end is None:
        horizon_end = horizon_start + timedelta(days=DEFAULT_HORIZON_DAYS - 1)
    if horizon_start > horizon_end:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "horizon_start 不得晚于 horizon_end",
            details={"horizon_start": horizon_start.isoformat(), "horizon_end": horizon_end.isoformat()},
        )
    return horizon_start, horizon_end


def _map_availability(row: m.Availability) -> EngineAvailability:
    weekly = [
        WeeklyWindow(weekday=int(w["weekday"]) - 1, start=w["start"], end=w["end"])  # ISO 1-7 → 0-6
        for w in (row.weekly_windows or [])
    ]
    exceptions: list[DateException] = []
    for e in row.date_exceptions or []:
        windows = e.get("windows")
        if windows == []:
            exceptions.append(DateException(date=e["date"], unavailable=True))  # 空数组=当日不可用
        elif windows:
            exceptions.append(
                DateException(
                    date=e["date"],
                    windows=[WindowSpan(start=w["start"], end=w["end"]) for w in windows],
                )
            )
        else:
            exceptions.append(DateException(date=e["date"]))
    return EngineAvailability(
        timezone=row.timezone,
        weekly_windows=weekly,
        date_exceptions=exceptions,
        daily_limit_minutes=row.daily_limit_minutes,
    )


def _submitted_assignment_refs(db: Session) -> set[tuple[str, str]]:
    """已提交/已评分且未归档作业的来源键 (platform, external_id)——PD-3 不排程判定的关联依据。"""
    keys: set[tuple[str, str]] = set()
    stmt = select(m.Assignment).where(
        m.Assignment.archived_at.is_(None),
        m.Assignment.submission_status.in_(("submitted", "graded")),
    )
    for a in db.execute(stmt).scalars():
        for ref in refs_for(db, m.OWNER_ASSIGNMENT, a.id):
            keys.add((ref.platform, ref.external_id))
    return keys


def _excluded_task_ids(db: Session, rows: list[m.LearningTask]) -> set[str]:
    """不参与排程的任务集合：已归档/已取消 + 已提交/已评分作业对应的任务（PD-3）。

    PD-3 关联按来源引用（platform+external_id 与作业来源一致）而非任务类型——
    任务类型由 D 侧映射，同源任务无论何类型都代表同一平台事实；任务行保留（可复习），
    仅不占排程容量。graded（已评分）同样视为平台已提交。
    """
    excluded = {t.id for t in rows if t.archived_at is not None or t.status == "cancelled"}
    submitted_refs = _submitted_assignment_refs(db)
    if submitted_refs:
        for t in rows:
            if t.id in excluded:
                continue
            if any((r.platform, r.external_id) in submitted_refs for r in refs_for(db, m.OWNER_TASK, t.id)):
                excluded.add(t.id)
    return excluded


def _map_tasks(rows: list[m.LearningTask], excluded: set[str], tz: dt.tzinfo) -> list[EngineTask]:
    mapped: list[EngineTask] = []
    for t in rows:
        if t.id in excluded:
            continue
        due_at = None
        if t.due_at is not None:
            due_at = normalize_dt(t.due_at).astimezone(tz).isoformat(timespec="minutes")
        mapped.append(
            EngineTask(
                id=t.id,
                course_id=t.course_id or "",
                type=t.type,
                title=t.title,
                estimated_minutes=t.estimated_minutes,
                due_at=due_at,
                due_precision=t.due_precision,
                prerequisite_ids=[p for p in (t.prerequisite_ids or []) if p not in excluded],
                status="pending" if t.status == "in_progress" else t.status,
            )
        )
    return mapped


def _locked_blocks(active: m.Plan | None, excluded: set[str]) -> list[EngineBlock]:
    """生效计划的锁定块；PD-3 被排除任务（如已提交作业）的锁定块不再占用时间容量。"""
    if active is None:
        return []
    return [
        EngineBlock(
            id=b.id,
            task_id=b.task_id,
            starts_at=m.iso_utc(b.starts_at),
            ends_at=m.iso_utc(b.ends_at),
            locked=True,
        )
        for b in active.blocks
        if b.locked and b.task_id not in excluded
    ]


def _shortage_minutes(item) -> int | None:
    if item.reason_code == "DEADLINE_PASSED" or item.needed_minutes is None or item.available_minutes is None:
        return None
    return max(0, item.needed_minutes - item.available_minutes)


def preview_candidate(
    db: Session,
    horizon_start: date | None,
    horizon_end: date | None,
    respect_locked: bool,
    *,
    temporary_blocked: list[dict] | None = None,
    temporary_windows: list[dict] | None = None,
    not_before: datetime | None = None,
    commit: bool = True,
) -> m.Plan:
    """生成候选计划并落库（status=candidate；不改变当前生效计划）。"""
    availability_row = db.get(m.Availability, 1)
    if availability_row is None:
        if temporary_windows is None:
            availability_row = _availability_row(db)
        else:
            availability_row = m.Availability(id=1, timezone="Asia/Shanghai", weekly_windows=[], date_exceptions=[], daily_limit_minutes=None)
    tz = resolve_user_tz(availability_row.timezone)
    start, end = resolve_horizon(horizon_start, horizon_end, tz)
    active = active_plan(db)

    horizon_start_dt = datetime.combine(start, dt.time.min, tz)
    horizon_end_dt = datetime.combine(end + timedelta(days=1), dt.time.min, tz)  # 含 end 当天
    reference_time = (not_before or datetime.now(timezone.utc)).astimezone(tz).isoformat(timespec="minutes")

    # Expansion and fingerprint consume the same cache snapshot; apply/current
    # re-read it through compute_input_revision and reject changed/expired facts.
    campus_evidence = read_campus_evidence()
    occupied, campus_warnings = campus_blocks(campus_evidence, horizon_start_dt, horizon_end_dt)
    revision = compute_input_revision(db, campus_evidence=campus_evidence)
    rows = list(db.execute(select(m.LearningTask).order_by(m.LearningTask.id)).scalars())
    excluded = _excluded_task_ids(db, rows)
    options = ScheduleOptions(
        horizon_start=horizon_start_dt.isoformat(timespec="minutes"),
        horizon_end=horizon_end_dt.isoformat(timespec="minutes"),
        input_revision=revision,
        reference_time=reference_time,
        locked_blocks=_locked_blocks(active, excluded) if respect_locked else [],
        config=load_schedule_config(),
    )
    if temporary_blocked is not None or temporary_windows is not None:
        from .agent_bridge.constraints import temporary_availability
        availability_row = temporary_availability(availability_row, start, end, tz, temporary_blocked or [],
                                                 temporary_windows, options.locked_blocks)
    if occupied:
        availability_row = subtract_campus_blocks(availability_row, start, end, tz, occupied, options.locked_blocks)
    result = generate_plan(_map_tasks(rows, excluded, tz), _map_availability(availability_row), options)

    max_version = db.execute(select(func.max(m.Plan.version))).scalar() or 0
    plan = m.Plan(
        id=new_id(db, m.Plan, "pln"),
        version=max_version + 1,
        status="candidate",
        input_revision=revision,
        horizon_start=start,
        horizon_end=end,
        summary=dict(result.summary),
        based_on_plan_id=active.id if active is not None else None,
        created_at=m.utc_now(),
    )
    db.add(plan)
    db.flush()
    for b in result.blocks:
        db.add(
            m.PlanBlock(
                plan_id=plan.id,
                id=b.id,
                task_id=b.task_id,
                starts_at=normalize_dt(datetime.fromisoformat(b.starts_at)),
                ends_at=normalize_dt(datetime.fromisoformat(b.ends_at)),
                locked=bool(b.locked),
            )
        )
    for u in result.unassigned:
        db.add(
            m.PlanUnassigned(
                plan_id=plan.id,
                task_id=u.task_id,
                reason_code=u.reason_code,
                shortage_minutes=_shortage_minutes(u),
                message=u.message,
                suggestion=u.suggestion,
            )
        )
    for w in result.warnings:
        db.add(
            m.PlanWarning(
                plan_id=plan.id,
                code=w.code,
                message=w.message,
                task_ids=w.task_ids,
                details=w.details,
            )
        )
    for warning in campus_warnings:
        db.add(m.PlanWarning(plan_id=plan.id, **warning))
    if commit:
        db.commit()
    else:
        db.flush()
    return plan
