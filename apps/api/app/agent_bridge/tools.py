"""Read-only domain tools. Identity and scope are supplied by the gateway, never the LLM.

The existing database is a single-person workspace. Scope restricts that local
workspace further; it does not turn the original database into a multiuser store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..errors import ApiError
from ..repo import page, refs_for
from ..routers import connections, courses, jobs, plans, resources, search, tasks
from ..plan_engine import resolve_user_tz
from . import plan_actions, job_actions, feedback_actions


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Input):
    pass


class Pagination(Input):
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=20, ge=1, le=50)


class ConnectionArgs(Input):
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)


class CourseArgs(Pagination):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    term: str | None = Field(default=None, min_length=1, max_length=64)


class MaterialArgs(Pagination):
    course_id: str = Field(min_length=1, max_length=128)


class BuildArgs(Input):
    course_id: str = Field(min_length=1, max_length=128)


class FeedbackArgs(Input):
    task_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    outcome: Literal["completed", "partial", "skipped"]
    actual_minutes: int | None = Field(default=None, ge=1, le=1440)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=2000)


class SearchArgs(Pagination):
    query: str = Field(min_length=1, max_length=500)
    course_id: str | None = Field(default=None, min_length=1, max_length=128)


class ReadArgs(Pagination):
    resource_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    chunk_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)


class AssignmentArgs(Pagination):
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    due_before: str | None = Field(default=None, max_length=64)
    due_after: str | None = Field(default=None, max_length=64)


class TaskArgs(Pagination):
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = None
    due_before: str | None = Field(default=None, max_length=64)


class PlanArgs(Input):
    plan_id: str | None = Field(default=None, min_length=1, max_length=128)


class JobArgs(Input):
    job_id: str = Field(min_length=1, max_length=128)


class BlockedTime(Input):
    starts_at: str = Field(max_length=64)
    ends_at: str = Field(max_length=64)


class TemporaryWindow(Input):
    weekday: int = Field(ge=1, le=7)
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class PreviewArgs(Input):
    period: Literal["this_week", "tomorrow", "dates"] = "this_week"
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    blocked: list[BlockedTime] = Field(default_factory=list, max_length=64)
    temporary_windows: list[TemporaryWindow] | None = Field(default=None, max_length=56)
    unavailable_evenings: list[Annotated[int, Field(ge=1, le=7)]] = Field(default_factory=list, max_length=7,
        description="本周不可用的晚上，ISO 星期1至7；服务端按18:00至次日00:00转换并在预览中明示。仅适用于this_week。")


class ActionArgs(Input):
    action_id: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True)
class ToolContext:
    principal: str
    conversation_id: str
    course_ids: frozenset[str] | None = None
    connection_ids: frozenset[str] | None = None
    job_ids: frozenset[str] = frozenset()
    allow_uncoursed_tasks: bool = False
    request_key: str | None = None


SPECS: dict[str, tuple[type[Input], str]] = {
    "connections.status": (ConnectionArgs, "读取已连接平台与缓存状态，不发起登录或同步。"),
    "courses.list": (CourseArgs, "按名称或学期查本地课程；多个匹配需要澄清。"),
    "materials.list": (MaterialArgs, "列出课程资料与解析状态；元数据不表示已读正文。"),
    "materials.search": (SearchArgs, "在已解析的当前版本正文里检索原文依据。当前为子串检索。"),
    "materials.read": (ReadArgs, "读取指定版本原文片段，保留位置和未覆盖内容。"),
    "assignments.list": (AssignmentArgs, "读取学校作业事实，保留提交状态和未知截止。"),
    "tasks.list": (TaskArgs, "读取本地学习任务。完成学习不等于学校平台提交。"),
    "plans.read": (PlanArgs, "读取生效或指定计划、锁定时间块与输入版本。"),
    "jobs.read": (JobArgs, "读取当前会话拥有的后台任务状态与结果引用。"),
    "availability.read": (Empty, "读取长期可用时间；未配置时请用户提供本次可用窗口，不得编造。"),
    "plans.preview": (PreviewArgs, "生成本次安排预览和差异，不生效。本周/明天由服务器按用户时区计算；临时约束不改长期偏好。"),
    "plans.apply": (ActionArgs, "应用用户已在可信界面接受的具体动作；模型不能自行授权。返回持久化操作回执。"),
    "plans.revoke": (ActionArgs, "执行用户已接受的撤销动作，恢复前版；需使用撤销动作 ID，不能使用任意计划 ID。"),
    "tasks.build": (BuildArgs, "从指定课程的已导入资料生成本地学习任务，返回真实后台 job；规则生成不调用模型，重复请求复用。"),
    "jobs.cancel": (JobArgs, "协作停止本会话发起的任务；running 且 cancel_requested 只表示请求停止，不能宣称已完成。"),
    "tasks.feedback": (FeedbackArgs, "仅记录用户明确报告的本地学习结果，先读任务版本；未报告的时间/难度留空。不修改学校提交状态。"),
}


def schemas() -> list[dict[str, Any]]:
    return [{"name": name, "model_name": "campus_" + name.replace(".", "_"),
             "description": description, "input_schema": schema.model_json_schema(),
             "effects": "write" if name in {"plans.preview", "plans.apply", "plans.revoke", "tasks.build", "jobs.cancel", "tasks.feedback"} else "read", "version": 1}
            for name, (schema, description) in SPECS.items()]


def _deny() -> None:
    raise ApiError(403, "SCOPE_DENIED", "当前会话无权读取该对象")


def _course(db: Session, ctx: ToolContext, course_id: str) -> None:
    if ctx.course_ids is not None and course_id not in ctx.course_ids:
        _deny()
    courses.ensure_course(db, course_id)


def _scoped(stmt, column, ctx):
    return stmt.where(column.in_(ctx.course_ids)) if ctx.course_ids is not None else stmt


def _whole_workspace(db, ctx):
    required = set(db.scalars(select(m.Course.id).where(m.Course.archived_at.is_(None))))
    required.update(db.scalars(select(m.LearningTask.course_id).where(m.LearningTask.archived_at.is_(None), m.LearningTask.course_id.is_not(None))))
    if not ctx.allow_uncoursed_tasks or ctx.course_ids is None or not required.issubset(ctx.course_ids):
        raise ApiError(403, "SCOPE_DENIED", "安排需要完整本地工作区范围；课程变化后请新建会话")
    # Retained locks and warnings may refer to archived tasks outside today's task
    # query. Reuse the read adapter's complete plan-reference scope validation.
    execute("plans.read", {}, db, ctx)


def _preview_dates(args, tz):
    today = datetime.now(tz).date()
    try:
        if args["period"] == "dates":
            start, end = date.fromisoformat(args["start_date"] or ""), date.fromisoformat(args["end_date"] or "")
        else:
            if args["start_date"] is not None or args["end_date"] is not None:
                raise ValueError()
            start = today + timedelta(days=1) if args["period"] == "tomorrow" else today
            end = start if args["period"] == "tomorrow" else today + timedelta(days=6 - today.weekday())
        if start < today or end < start or (end - start).days > 30:
            raise ValueError()
        return start, end
    except ValueError:
        raise ApiError(400, "INVALID_INPUT", "请给出今天起最多 31 天的有效安排范围；相对日期不能同时指定绝对日期") from None


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("missing timezone")
        return result.astimezone(timezone.utc)
    except ValueError:
        raise ApiError(400, "INVALID_INPUT", "日期时间必须包含有效时区") from None


def _evening_blocks(args, start, end, tz):
    days = args["unavailable_evenings"]
    if not days:
        return []
    if args["period"] != "this_week" or len(set(days)) != len(days):
        raise ApiError(400, "INVALID_INPUT", "星期晚间约束只接受本周、且星期不可重复")
    monday = start - timedelta(days=start.weekday())
    blocks = []
    for weekday in sorted(days):
        day = monday + timedelta(days=weekday - 1)
        if day < start or day > end:
            raise ApiError(400, "INVALID_INPUT", "指定的本周晚间已过去，请说明是否指下周")
        first = datetime.combine(day, datetime.min.time(), tz) + timedelta(hours=18)
        last = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
        blocks.append({"starts_at": first.isoformat(), "ends_at": last.isoformat()})
    return blocks


def _evidence(item: dict) -> dict:
    entity_id = item.get("chunk_id") or item.get("id") or item.get("resource_id")
    source_refs = item.get("source_refs") or []
    return {"source_id": str(entity_id), "entity_id": item.get("resource_id") or entity_id,
            "source_system": source_refs[0]["platform"] if source_refs else "local",
            "version": item.get("resource_version", item.get("version", item.get("revision"))),
            "locator": item.get("locator"),
            "observed_at": source_refs[0].get("observed_at") if source_refs else None}


def _envelope(data: dict, *, scope: str, evidence: list[dict] | None = None,
              missing: list[str] | None = None, warnings: list[str] | None = None) -> dict:
    missing = list(missing or [])
    cursor = data.get("next_cursor")
    if cursor:
        missing.append("存在未读取的后续分页")
    evidence = evidence or []
    times = [e["observed_at"] for e in evidence if e.get("observed_at")]
    fetched_at = min(times) if times else None
    stale = (fetched_at is None or _time(fetched_at) < datetime.now(timezone.utc) - timedelta(hours=24))
    return {"status": "partial" if missing else "ok", "data": data, "evidence": evidence,
            "coverage": {"scope": scope, "complete": not missing, "missing": missing},
            "freshness": {"fetched_at": fetched_at, "stale": stale},
            "warnings": list(warnings or []), "next_cursor": cursor, "job_id": None, "error": None}


def execute(name: str, arguments: dict, db: Session, ctx: ToolContext) -> dict:
    if ctx.principal != "local" or not ctx.conversation_id:
        _deny()
    if name not in SPECS:
        raise ApiError(400, "UNSUPPORTED", "未注册的校园工具")
    try:
        args = SPECS[name][0].model_validate(arguments).model_dump()
    except ValidationError:
        raise ApiError(400, "INVALID_INPUT", "工具参数不符合严格契约") from None
    if args.get("course_id"):
        _course(db, ctx, args["course_id"])
    data: dict
    evidence: list[dict] = []
    warnings: list[str] = []
    missing: list[str] = []
    scope = "local_workspace"

    if name == "tasks.feedback":
        task = db.get(m.LearningTask, args["task_id"])
        if task is None or (ctx.course_ids is not None and task.course_id not in ctx.course_ids
                            and not (task.course_id is None and ctx.allow_uncoursed_tasks)):
            _deny()
        if not ctx.request_key:
            raise ApiError(403, "RUNTIME_DENIED", "反馈需要可信调用标识")
        data = feedback_actions.record(db, ctx.conversation_id, ctx.request_key, args)
        warnings.append("这是本地学习反馈，学校提交状态未改变")
        evidence = [_evidence(data["task"]) | {"observed_at": datetime.now(timezone.utc).isoformat()}]
    elif name == "tasks.build":
        if not ctx.request_key:
            raise ApiError(403, "RUNTIME_DENIED", "生成任务需要可信调用标识")
        data = {"job": job_actions.build(db, ctx.conversation_id, ctx.request_key, args["course_id"])}
        warnings.append("生成请求已受理；完成与否须读取真实 job 状态")
    elif name == "jobs.cancel":
        data = {"job": job_actions.cancel(db, ctx.conversation_id, args["job_id"])}
        warnings.append("停止本轮对话与停止后台任务是两个操作；running 状态仍需等待取消检查点")
    elif name in {"availability.read", "plans.preview", "plans.apply", "plans.revoke"}:
        _whole_workspace(db, ctx)
        availability = db.get(m.Availability, 1)
        if name == "availability.read":
            data = {"configured": availability is not None, "availability": availability.to_contract() if availability else None}
        elif name == "plans.preview":
            if not ctx.request_key:
                raise ApiError(403, "RUNTIME_DENIED", "预览需要可信调用标识")
            tz = resolve_user_tz(availability.timezone if availability else "Asia/Shanghai")
            start, end = _preview_dates(args, tz)
            evenings = _evening_blocks(args, start, end, tz)
            data = {"action": plan_actions.preview(db, ctx.conversation_id, ctx.request_key, start, end,
                    blocked=args["blocked"] + evenings, windows=args["temporary_windows"])}
            if evenings:
                warnings.append("本次预览将‘晚上’按18:00至次日00:00排除；可在接受前提供更精确的时间")
            warnings.append("预览尚未生效；请检查差异与未分配任务后接受方案")
        else:
            data = {"receipt": plan_actions.execute(db, ctx.conversation_id, args["action_id"], name.split(".")[1])}
            warnings.append("这是操作回执；当前安排请读取 plans.read")
        evidence = [{"source_id": name, "entity_id": ctx.conversation_id, "source_system": "local",
                     "version": None, "locator": None, "observed_at": datetime.now(timezone.utc).isoformat()}]
    elif name == "connections.status":
        cid = args["connection_id"]
        if cid and ctx.connection_ids is not None and cid not in ctx.connection_ids:
            _deny()
        data = ({"items": [connections.get_connection(cid, db)], "next_cursor": None}
                if cid else connections.list_connections(db))
        if ctx.connection_ids is not None:
            data["items"] = [i for i in data["items"] if i["id"] in ctx.connection_ids]
    elif name == "courses.list":
        stmt = _scoped(select(m.Course).where(m.Course.archived_at.is_(None)), m.Course.id, ctx)
        if args["query"]:
            stmt = stmt.where(m.Course.title.contains(args["query"], autoescape=True))
        if args["term"]:
            stmt = stmt.where(m.Course.term == args["term"])
        rows, cursor = page(db, stmt, m.Course, args["cursor"], args["limit"])
        data = {"items": [courses.course_to_contract(db, c) for c in rows], "next_cursor": cursor}
        if args["query"] and (len(rows) > 1 or cursor):
            warnings.append("课程名称匹配多个对象，请按学期或课程ID确认后继续，不要自行选择其中一门")
    elif name == "materials.list":
        stmt = select(m.Resource).where(m.Resource.course_id == args["course_id"], m.Resource.archived_at.is_(None))
        rows, cursor = page(db, stmt, m.Resource, args["cursor"], args["limit"])
        data = {"items": [resources.resource_to_contract(db, r) for r in rows], "next_cursor": cursor}
        if any(r.parse_status != "ready" for r in rows):
            missing.append("部分资料正文尚未解析")
    elif name == "materials.search":
        # Search with an explicit course scope so unauthorized results never reach the caller.
        if ctx.course_ids is not None and not args["course_id"]:
            raise ApiError(400, "INVALID_INPUT", "受限会话检索时需指定课程")
        data = search.search(db=db, q=args["query"], course_id=args["course_id"], kind=None,
                             cursor=args["cursor"], limit=args["limit"])
        if not data["items"]:
            warnings.append("没有找到正文依据；不能据此推断要求不存在")
    elif name == "materials.read":
        resource = db.get(m.Resource, args["resource_id"])
        if resource is None or resource.archived_at is not None:
            raise ApiError(404, "NOT_FOUND", "原文不存在或已归档")
        _course(db, ctx, resource.course_id)
        if resource.version != args["expected_version"]:
            raise ApiError(409, "STALE_VERSION", "资料版本已变化，请重新检索")
        if resource.parse_status != "ready":
            raise ApiError(409, "NO_EVIDENCE", "当前资料尚无可读取正文")
        if args["chunk_ids"] is not None:
            if args["cursor"] is not None:
                raise ApiError(400, "INVALID_INPUT", "指定片段读取不能同时使用分页游标")
            requested = set(args["chunk_ids"])
            chunks = list(db.scalars(select(m.ContentChunk).where(m.ContentChunk.resource_id == resource.id,
                          m.ContentChunk.resource_version == resource.version, m.ContentChunk.id.in_(requested))
                          .order_by(m.ContentChunk.ordinal, m.ContentChunk.id)))
            if {c.id for c in chunks} != requested:
                raise ApiError(400, "INVALID_INPUT", "指定片段不属于本次读取的资料范围")
            data = {"items": [c.to_contract() for c in chunks], "next_cursor": None}
        else:
            data = resources.list_resource_chunks(resource.id, db, args["cursor"], args["limit"])
        data["resource"] = resources.resource_to_contract(db, resource)
        if not data["resource"]["chunk_count"]:
            raise ApiError(409, "NO_EVIDENCE", "当前版本没有可读取的正文片段")
        if len(data["items"]) < data["resource"]["chunk_count"]:
            missing.append("仅返回指定或分页片段，尚未覆盖资料全文")
        scope = f"material:{resource.id}:v{resource.version}"
        if sum(len(i["text"]) for i in data["items"]) > 24000:
            raise ApiError(400, "BUDGET_EXCEEDED", "正文超出单次读取预算，请缩小片段范围")
    elif name == "assignments.list":
        stmt = _scoped(select(m.Assignment).where(m.Assignment.archived_at.is_(None)), m.Assignment.course_id, ctx)
        if args["course_id"]:
            stmt = stmt.where(m.Assignment.course_id == args["course_id"])
        before, after = _time(args["due_before"]), _time(args["due_after"])
        if before and after and after > before:
            raise ApiError(400, "INVALID_INPUT", "日期范围起点晚于终点")
        # Unknown dates stay visible alongside any date-filtered result.
        if before:
            stmt = stmt.where(or_(m.Assignment.due_at.is_(None), m.Assignment.due_at <= before))
        if after:
            stmt = stmt.where(or_(m.Assignment.due_at.is_(None), m.Assignment.due_at >= after))
        rows, cursor = page(db, stmt, m.Assignment, args["cursor"], args["limit"])
        data = {"items": [a.to_contract(refs_for(db, m.OWNER_ASSIGNMENT, a.id)) for a in rows], "next_cursor": cursor}
        if any(a.due_precision == "unknown" for a in rows):
            warnings.append("包含截止时间未知的作业，请核对平台原文")
    elif name == "tasks.list":
        stmt = select(m.LearningTask).where(m.LearningTask.archived_at.is_(None))
        if ctx.course_ids is not None:
            allowed = m.LearningTask.course_id.in_(ctx.course_ids)
            if ctx.allow_uncoursed_tasks:
                allowed = or_(allowed, m.LearningTask.course_id.is_(None))
            stmt = stmt.where(allowed)
        if args["course_id"]:
            stmt = stmt.where(m.LearningTask.course_id == args["course_id"])
        if args["status"]:
            stmt = stmt.where(m.LearningTask.status == args["status"])
        before = _time(args["due_before"])
        if before:
            stmt = stmt.where(or_(m.LearningTask.due_at.is_(None), m.LearningTask.due_at <= before))
        rows, cursor = page(db, stmt, m.LearningTask, args["cursor"], args["limit"])
        data = {"items": [tasks.task_to_contract(db, t) for t in rows], "next_cursor": cursor}
        warnings.append("本地任务状态不代表学校作业提交状态")
    elif name == "plans.read":
        data = plans.get_plan(args["plan_id"], db) if args["plan_id"] else plans.get_current_plan(db)
        plan = data if args["plan_id"] else data.get("plan")
        task_ids = {b["task_id"] for b in (plan or {}).get("blocks", [])}
        task_ids.update(i["task_id"] for i in (plan or {}).get("unassigned", []))
        task_ids.update(tid for w in (plan or {}).get("warnings", []) for tid in w.get("task_ids", []))
        for task_id in task_ids:
            task = db.get(m.LearningTask, task_id)
            if task is None or (ctx.course_ids is not None and task.course_id not in ctx.course_ids
                                and not (task.course_id is None and ctx.allow_uncoursed_tasks)):
                _deny()
    elif name == "jobs.read":
        if args["job_id"] not in ctx.job_ids and not job_actions.owns(db, ctx.conversation_id, args["job_id"]):
            _deny()
        data = jobs.get_job(args["job_id"], db)
        # Payload is deliberately excluded by the existing job contract.
    else:
        raise AssertionError(name)

    if "items" in data and name != "connections.status":
        evidence = [_evidence(i | {"source_refs": data["resource"]["source_refs"]}) for i in data["items"]] if name == "materials.read" else [_evidence(i) for i in data["items"]]
    # Workspace queries cannot prove the school data feed is complete.
    if name in {"assignments.list", "materials.list", "courses.list"}:
        missing.append("范围仅为本机已导入数据，未证明学校平台全量覆盖")
    if args.get("course_id"):
        scope = "course:" + args["course_id"]
    result = _envelope(data, scope=scope, evidence=evidence, missing=missing, warnings=warnings)
    if name == "courses.list" and args["query"] and (len(data["items"]) > 1 or data["next_cursor"]):
        result["status"] = "needs_user"
    if name in {"tasks.build", "jobs.cancel", "jobs.read"}:
        job = data.get("job", data)
        result["job_id"] = job["id"]
        result["freshness"] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "stale": False}
        if job["status"] in {"queued", "running"}:
            result["status"] = "queued"
    return result
