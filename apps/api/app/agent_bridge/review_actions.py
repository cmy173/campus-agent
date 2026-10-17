"""Agent 侧复习卷适配：材料发现辅助、预检、受理入队、读卷与导出。

这里不复制出卷引擎：受理链（材料选择、可用性、限额、指纹、冻结快照、入队
review_generate）全部复用 `app.review_service`，与 REST 路由是同一实现。本模块只加
Agent 需要的四件事：

1. 会话授权范围内的课程 / 材料 / provider 解析（模型不能指定 owner、密钥或扩大 scope）；
2. 可信调用 ID → 幂等回执 + job 归属（重复调用不重复扣费出卷）；
3. 读卷时按 paper/revision/snapshot 归属校验；
4. 导出复用 `review_export` 的既有 Markdown/HTML 渲染器，并通过会话文件存储交付。
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .. import review, review_export, review_service, secrets_store
from ..errors import ApiError
from .plan_actions import fingerprint as fingerprint_of

REVIEW_MODES = ("ai", "bank", "hybrid")
EXPORT_FORMATS = ("markdown", "student", "answers")
MAX_NOTES = 20_000
MAX_QUESTIONS_RETURNED = 60
MAX_STEM_CHARS = 2_000
MAX_MATERIAL_PAGE = 50


def _deny() -> None:
    raise ApiError(403, "SCOPE_DENIED", "当前会话无权访问该课程或试卷")


def course(db: Session, ctx, course_id: str) -> m.Course:
    if ctx.course_ids is not None and course_id not in ctx.course_ids:
        _deny()
    found = db.get(m.Course, course_id)
    if found is None or found.archived_at is not None:
        raise ApiError(404, "NOT_FOUND", "课程不存在或已归档")
    return found


def provider_config_id(db: Session, mode: str) -> str | None:
    """bank 不调用模型；ai/hybrid 只用服务端已保存、已启用且已存密钥的配置。"""
    if mode == "bank":
        return None
    configs = list(db.scalars(select(m.ProviderConfig).where(m.ProviderConfig.enabled.is_(True))
                              .order_by(m.ProviderConfig.created_at, m.ProviderConfig.id)))
    usable = [cfg for cfg in configs if secrets_store.has_key(cfg.id)]
    if not usable:
        raise ApiError(
            409,
            "PROVIDER_UNAVAILABLE",
            "没有可用的 AI 服务配置（需要已启用并已保存密钥）；请先在设置中配置模型，"
            "或改用 bank 模式（需已配置题库来源）",
            details={"mode": mode, "enabled_configs": len(configs)},
        )
    return usable[0].id


def ready_materials(db: Session, course_id: str) -> dict[str, Any]:
    """默认材料：该课程当前可用（已解析且有正文）的资料与作业。

    复用复习卷自己的材料清单（`review.list_materials`）逐页收集，只读取本机已授权课程
    数据；不发正文、不联网。未解析/无正文的条目留在清单里由调用方如实报告，不假装已读。
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):  # bounded paging; the material list itself is keyset-paginated
        page, cursor = review.list_materials(db, course_id, cursor, MAX_MATERIAL_PAGE)
        rows.extend(page)
        if not cursor:
            break
    selection = {"resources": [], "assignments": []}
    for item in rows:
        if item.get("kind") == "resource" and item.get("usable"):
            selection["resources"].append({"resource_id": item["id"], "version": item.get("version")})
        elif item.get("kind") == "assignment":
            attachments = [att for att in item.get("attachments") or [] if att.get("usable")]
            if item.get("usable") or attachments:
                selection["assignments"].append({"assignment_id": item["id"], "include_attachments": True})
    return {"selection": selection, "materials": rows,
            "unusable": [{"id": item.get("id"), "kind": item.get("kind"),
                          "reason": item.get("unavailable_reason") or "no_text"}
                         for item in rows if not item.get("usable") and item.get("kind") == "resource"]}


def normalize_materials(payload: dict[str, Any] | None, course_id: str, db: Session) -> dict[str, Any]:
    """模型给出的选择必须落在课程范围内；缺省=该课程当前可用材料。

    该课程范围内的合法 id 直接透传（由 review.resolve_selection 统一验证存在性与可读性）；
    超出课程范围或看起来属于其他课程的 id 在发正文前拒绝。
    """
    wanted = payload or {}
    resources = [item for item in wanted.get("resources") or [] if isinstance(item, dict)]
    assignments = [item for item in wanted.get("assignments") or [] if isinstance(item, dict)]
    for item in resources:
        resource_id = item.get("resource_id")
        if not isinstance(resource_id, str):
            raise ApiError(400, "INVALID_INPUT", "materials.resources 需要 resource_id")
        row = db.get(m.Resource, resource_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "资料不存在")
        if row.course_id != course_id:
            _deny()
    for item in assignments:
        assignment_id = item.get("assignment_id")
        if not isinstance(assignment_id, str):
            raise ApiError(400, "INVALID_INPUT", "materials.assignments 需要 assignment_id")
        row = db.get(m.Assignment, assignment_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "作业不存在")
        if row.course_id != course_id:
            _deny()
    if resources or assignments:
        return {"resources": resources, "assignments": assignments}
    return ready_materials(db, course_id)["selection"]


def _paper_view(paper: m.ReviewPaper) -> dict[str, Any]:
    """The accepted paper identity; the body only exists after the worker succeeds."""
    return {"paper_id": paper.id, "revision": paper.latest_revision, "mode": paper.mode, "title": paper.title}


def owned_receipt(db: Session, conversation_id: str, request_key: str) -> m.AgentReviewReceipt | None:
    return db.execute(select(m.AgentReviewReceipt).where(
        m.AgentReviewReceipt.conversation_id == conversation_id,
        m.AgentReviewReceipt.request_key == request_key,
    )).scalar_one_or_none()


def owns_paper(db: Session, conversation_id: str, paper_id: str) -> bool:
    """A paper is reachable only through this conversation's own generation receipt."""
    return db.execute(select(m.AgentReviewReceipt.id).where(
        m.AgentReviewReceipt.conversation_id == conversation_id,
        m.AgentReviewReceipt.paper_id == paper_id,
    )).first() is not None


def owns_job(db: Session, conversation_id: str, job_id: str) -> bool:
    return db.execute(select(m.AgentReviewReceipt.id).where(
        m.AgentReviewReceipt.conversation_id == conversation_id,
        m.AgentReviewReceipt.job_id == job_id,
    )).first() is not None


def business_key(course_id: str, mode: str, provider_config_id: str | None,
                 snapshot_fingerprint: str) -> str:
    """F2: the stable identity of one generation business request.

    It is the conversation (checked separately by every caller) plus course, engine mode,
    resolved provider and the frozen-material fingerprint returned by
    `review_service.resolve_accept_context`. `wait_seconds`, the trusted call id and
    `new_generation` are observation/control parameters and are deliberately excluded: the same
    business request must keep the same identity when the model only changes those.
    """
    return fingerprint_of({"course_id": course_id, "mode": mode,
                           "provider_config_id": provider_config_id,
                           "snapshot_fingerprint": snapshot_fingerprint})


def _stored_business_key(receipt: m.AgentReviewReceipt) -> str | None:
    try:
        stored = json.loads(receipt.result)
    except (TypeError, ValueError):
        return None
    return stored.get("business_key") if isinstance(stored, dict) else None


def latest_business_receipt(db: Session, conversation_id: str, course_id: str,
                            key: str) -> m.AgentReviewReceipt | None:
    """Newest generation receipt in this conversation with the same frozen business identity.

    Conversation-scoped and course-scoped: another conversation's identical request can never be
    reused (or leak its paper/job id), which preserves the existing ownership rule.
    """
    rows = db.execute(select(m.AgentReviewReceipt).where(
        m.AgentReviewReceipt.conversation_id == conversation_id,
        m.AgentReviewReceipt.course_id == course_id,
    ).order_by(m.AgentReviewReceipt.created_at.desc(),
               m.AgentReviewReceipt.id.desc())).scalars()
    for receipt in rows:
        if _stored_business_key(receipt) == key:
            return receipt
    return None


def reuse_reason(db: Session, receipt: m.AgentReviewReceipt) -> str | None:
    """Why the existing generation can be returned instead of accepting a new charged job.

    `None` means the prior job is terminal-and-not-reusable (failed/cancelled/partial_failed, or
    the row is gone): a new generation is then allowed so recovery still works. "Reuse" is not a
    claim of success — the caller reads the real job row and reports its true status.
    """
    job = db.get(m.Job, receipt.job_id)
    return None if job is None else REUSABLE_JOB_STATES.get(job.status)


def _public(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _INTERNAL_RESULT_KEYS}


def reused_payload(db: Session, receipt: m.AgentReviewReceipt, reason: str) -> dict[str, Any]:
    """Return the already-accepted generation with its *current* real state, marked as reused."""
    from ..routers import jobs as jobs_router

    stored = json.loads(receipt.result)
    payload = _public(stored)
    payload["job"] = jobs_router.get_job(receipt.job_id, db)
    paper = db.get(m.ReviewPaper, receipt.paper_id)
    if paper is not None:
        payload["paper"] = _paper_view(paper)
    payload["reused"] = True
    payload["reused_reason"] = reason
    return payload


TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled", "partial_failed"}

# F2: a prior generation with the same business identity may be reused instead of accepting a
# second charged job. "In flight" (queued/running) must never start a second billed generation;
# an already succeeded identical request is reused too, so a repeated model call returns the
# real paper instead of paying for the same paper twice. The caller has to ask explicitly
# (`new_generation=true`) for a genuinely new paper. A failed/cancelled/partial_failed prior job
# is NOT reused: recovery has to be able to start a fresh generation without a special flag.
REUSABLE_JOB_STATES = {"queued": "identical_request_in_flight",
                       "running": "identical_request_in_flight",
                       "succeeded": "identical_request_completed"}

# Only the frozen business identity is persisted inside the receipt result. It is stripped from
# the model-facing response: it is an internal dedupe key, not a new tool capability.
_INTERNAL_RESULT_KEYS = ("business_key",)

# Measured budget: the domain tool call is fenced at 20s by the local runtime, so the wait window
# has to stay clearly below it. Real review generation with the configured provider took
# ~14–16s end to end (worker logs), so 14s is the largest window that still leaves room for the
# bridge round trip without risking a tool timeout that would hide the real state.
MAX_JOB_WAIT_SECONDS = 14


def _job_age_seconds(db: Session, job_id: str) -> float | None:
    job = db.get(m.Job, job_id)
    created = getattr(job, "created_at", None)
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def wait_window_seconds(db: Session, job_id: str, requested: int) -> int:
    """F2: bound the observation window by the job's own age, not by the call count.

    `wait_seconds` is an observation window on an already-accepted job, so a repeated call must
    not be able to reset it and keep one turn spinning: the effective window is the smaller of
    the caller's request and what remains of the server-side per-job window measured from the
    job's acceptance time. A fresh acceptance therefore still gets the full window; a reused
    job gets only the remainder, and 0 once the window is spent (the caller reads the real row
    with `jobs.read` instead of waiting again).
    """
    return _remaining_window(db, job_id, requested, MAX_JOB_WAIT_SECONDS)


# Bounded observation of a long real review generation. Measured worker wall time is 15–45 s and
# varies, so a single tool call can never cover it inside the 20 s fence. The per-job window is
# capped here so covering that latency costs a couple of `jobs.read(wait_seconds=14)` calls
# instead of a polling loop; once it is spent the call returns the real (still running) state.
MAX_JOB_OBSERVE_SECONDS = 45
MAX_SYNC_OBSERVE_SECONDS = 60
REVIEW_JOB_TYPES = {"review_generate"}


def _remaining_window(db: Session, job_id: str, requested: int, cap: int) -> int:
    window = max(0, min(int(requested), MAX_JOB_WAIT_SECONDS))
    if window <= 0:
        return 0
    age = _job_age_seconds(db, job_id)
    if age is None:
        return window
    return 0 if age >= cap else max(0, min(window, math.ceil(cap - age)))


def job_wait_window(db: Session, job: dict[str, Any], requested: int) -> int:
    """Bounded observation window per owned background-job family.

    Generation jobs retain a 45-second total window. Campus sync gets a bounded 60-second
    total window so a single jobs.read call can wait for intervals instead of burning
    model calls on immediate status polls.
    Parse/build and other jobs stay immediate reads.
    """
    if not requested or requested <= 0:
        return 0
    job_id = job.get("id")
    if not isinstance(job_id, str):
        return 0
    if job.get("type") in REVIEW_JOB_TYPES:
        cap = MAX_JOB_OBSERVE_SECONDS
    elif job.get("type") == "sync":
        cap = MAX_SYNC_OBSERVE_SECONDS
    else:
        return 0
    return _remaining_window(db, job_id, requested, cap)


def await_job(db: Session, ctx, job_id: str, seconds: int) -> dict[str, Any]:
    """Bounded, same-call observation of a real background job.

    A review job is produced by an independent worker process, so "accepted" and "finished" are
    different events. Polling it from the model burns the turn's model-request budget (~2s per
    round trip) even though the work is server-side. Waiting here is bounded well under the
    domain tool timeout, re-reads the committed row from the database (never a cached dict), and
    still returns `running` when the worker needs longer — acceptance is not reported as success.
    """
    from ..routers import jobs as jobs_router
    from ..errors import ApiError

    if not owns_job(db, ctx.conversation_id, job_id):
        _deny()
    current = jobs_router.get_job(job_id, db)
    if seconds <= 0 or current["status"] in TERMINAL_JOB_STATES:
        return current
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.4)
        db.expire_all()  # the worker writes from another process/session
        current = jobs_router.get_job(job_id, db)
        if current["status"] in TERMINAL_JOB_STATES:
            break
    return current


def paper_row(db: Session, conversation_id: str, paper_id: str, revision: int | None) -> tuple[m.ReviewPaper, m.ReviewPaperRevision]:
    if not owns_paper(db, conversation_id, paper_id):
        _deny()
    paper = db.get(m.ReviewPaper, paper_id)
    if paper is None:
        raise ApiError(404, "NOT_FOUND", "复习卷不存在")
    target = paper.latest_revision if revision is None else int(revision)
    if target is None or target < 1:
        raise ApiError(404, "NOT_FOUND", "该试卷尚未生成成功（无可用版本）",
                       details={"paper_id": paper_id, "job_status": "queued_or_failed"})
    row = db.get(m.ReviewPaperRevision, (paper_id, target))
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"试卷版本 v{target} 不存在")
    return paper, row


def paper_payload(db: Session, paper: m.ReviewPaper, row: m.ReviewPaperRevision) -> dict[str, Any]:
    snapshot = db.get(m.ReviewMaterialSnapshot, row.snapshot_id)
    if snapshot is None:
        raise ApiError(500, "INTERNAL_ERROR", "试卷快照数据缺失，无法读取")
    return row.to_paper_contract(paper, snapshot)


def preflight(db: Session, ctx, args: dict) -> dict[str, Any]:
    found = course(db, ctx, args["course_id"])
    mode = args["mode"]
    config_id = provider_config_id(db, mode) if not args.get("provider_config_id") else args["provider_config_id"]
    selection = normalize_materials(args.get("materials"), found.id, db)
    view = review_service.preflight_view(db, found, mode, config_id, selection, args["notes"], args["constraints"])
    missing = view["materials"].get("missing") or []
    return {"view": view, "selection": selection, "missing": missing, "course": found,
            "provider_config_id": config_id}


def generate(db: Session, ctx, args: dict) -> dict[str, Any]:
    if not getattr(ctx, "request_key", None):
        raise ApiError(403, "RUNTIME_DENIED", "生成复习卷需要可信调用标识")
    found = course(db, ctx, args["course_id"])
    mode = args["mode"]
    config_id = args.get("provider_config_id") or provider_config_id(db, mode)
    selection = normalize_materials(args.get("materials"), found.id, db)
    request = {"course_id": found.id, "mode": mode, "provider_config_id": config_id,
               "title": args.get("title"), "notes": args["notes"], "constraints": args["constraints"],
               "materials": selection, "if_match": args.get("if_match"),
               "new_generation": bool(args.get("new_generation"))}
    # `wait_seconds` is an observation window, not part of the business request: the same
    # generation replayed with a different wait length is still the same accepted job.
    identity = fingerprint_of(request)
    existing = owned_receipt(db, ctx.conversation_id, ctx.request_key)
    if existing is not None:
        if existing.request_hash != identity:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一出卷调用不能更换课程/模式/材料/配额；请使用新的调用")
        return reused_payload(db, existing, "same_call_id")

    # F2: the same conversation + same course/frozen-material/constraint identity must not accept
    # a second charged job just because the model used a new call id. The resolved context is a
    # read-only validation pass (the same one `accept_generation` uses), so a rejected request
    # still fails before anything is enqueued and the identity is the real frozen fingerprint.
    resolved = review_service.resolve_accept_context(db, found, mode, config_id, selection,
                                                     args["notes"], args["constraints"])
    review_service.check_preview(resolved, args.get("if_match"))
    key = business_key(found.id, mode, config_id, resolved["fingerprint"])
    if not args.get("new_generation"):
        prior = latest_business_receipt(db, ctx.conversation_id, found.id, key)
        reason = reuse_reason(db, prior) if prior is not None else None
        if reason is not None:
            return reused_payload(db, prior, reason)
    try:
        paper, _snapshot, job = review_service.accept_generation(
            db, found, mode=mode, provider_config_id=config_id, selection=selection,
            notes=args["notes"], constraints=args["constraints"], title=args.get("title"),
            if_match=args.get("if_match"),
        )
        # The receipt, the snapshot and the queued job are one commit: an accepted review job
        # that survives the request must be replayable and owner-checkable, and a failed commit
        # must not leave a charged job behind. `business_key` is stored with the receipt so a
        # later identical request can find this accepted generation instead of paying again.
        payload = {"job": job.to_contract(), "paper": _paper_view(paper), "selection": selection,
                   "fingerprint": _snapshot.fingerprint, "provider_config_id": config_id, "mode": mode,
                   "reused": False,
                   "business_key": business_key(found.id, mode, config_id, _snapshot.fingerprint)}
        receipt = m.AgentReviewReceipt(
            id="rvr_" + uuid.uuid4().hex, conversation_id=ctx.conversation_id, request_key=ctx.request_key,
            request_hash=identity, course_id=found.id, paper_id=paper.id, job_id=job.id,
            result=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            created_at=datetime.now(timezone.utc))
        db.add(receipt)
        db.commit()
        return _public(payload)
    except Exception:
        db.rollback()
        raise


def read(db: Session, ctx, args: dict) -> dict[str, Any]:
    paper, row = paper_row(db, ctx.conversation_id, args["paper_id"], args.get("revision"))
    payload = paper_payload(db, paper, row)
    questions = sorted(payload.get("questions") or [], key=lambda item: int(item.get("ordinal") or 0))
    truncated = len(questions) > MAX_QUESTIONS_RETURNED
    shown = []
    for question in questions[:MAX_QUESTIONS_RETURNED]:
        item = dict(question)
        for key in ("stem", "explanation"):
            text = item.get(key)
            if isinstance(text, str) and len(text) > MAX_STEM_CHARS:
                item[key] = text[:MAX_STEM_CHARS] + "…（已截断，完整内容请导出）"
        shown.append(item)
    # F4: `ReviewPaperRevision.to_paper_contract()` returns the business primary key as `id`.
    # The adapter used to read `paper_id`, so `reviews.read.data.paper.paper_id` was always null
    # and the model could not feed the id it just read into `reviews.export`. Map the real key
    # and fall back to the ORM identity row so the id is never silently dropped.
    paper_view = {key: payload.get(key) for key in ("revision", "revision_reason", "title",
                                                    "course_id", "mode", "created_at")}
    paper_view["paper_id"] = payload.get("id") or paper.id
    return {"paper": paper_view,
            "summary": payload.get("summary"), "constraints": payload.get("constraints"),
            "snapshot": {"id": (payload.get("snapshot") or {}).get("id"),
                         "fingerprint": (payload.get("snapshot") or {}).get("fingerprint"),
                         "items": [_snapshot_item(item) for item in (payload.get("snapshot") or {}).get("items") or []],
                         "missing": (payload.get("snapshot") or {}).get("missing") or []},
            "questions": shown, "question_count": len(questions), "truncated": truncated,
            "warnings": payload.get("warnings") or []}


def _snapshot_item(item: dict[str, Any]) -> dict[str, Any]:
    """Snapshot items can carry whole material bodies; expose identity and sizes only."""
    return {key: item.get(key) for key in ("kind", "resource_id", "assignment_id", "version", "title",
                                           "chunk_ids", "locator", "chars", "total_chars")
            if item.get(key) is not None}


def export(db: Session, ctx, files, args: dict) -> dict[str, Any]:
    paper, row = paper_row(db, ctx.conversation_id, args["paper_id"], args.get("revision"))
    payload = paper_payload(db, paper, row)
    found = db.get(m.Course, paper.course_id)
    fmt = args["format"]
    media_type, renderer = review_export.RENDERERS[fmt]
    body = renderer(payload, found.title if found is not None else None)
    name = review_export.export_filename(payload.get("title") or paper.title, fmt)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name)[:200]
    key = f"{ctx.request_key}:review-export:{fmt}:v{row.revision}"
    try:
        saved = files.save(ctx.conversation_id, _Bytes(body), name, "output", key,
                           f"review_paper:{paper.id}:v{row.revision}:{fmt}")
    except ApiError as exc:
        raise ApiError(exc.status_code, "REVIEW_EXPORT_FAILED",
                       "试卷已读取但文件未能发布到本会话文件区；未交付，可重试同一调用") from exc
    return {"file": saved, "paper": {"paper_id": paper.id, "revision": row.revision, "mode": paper.mode,
                                     "fingerprint": payload.get("snapshot", {}).get("fingerprint")},
            "format": fmt, "media_type": media_type, "size": len(body), "warnings": payload.get("warnings") or []}



class _Bytes:
    """A minimal BinaryIO over already-rendered content (no temporary file, no path).

    The review renderers return text; the session file store always reads binary. Encoding
    here keeps one code path for markdown/HTML instead of guessing at each call site.
    """

    def __init__(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            raise ApiError(500, "REVIEW_EXPORT_FAILED", "复习卷渲染器返回了无法交付的内容类型")
        self._data = bytes(data)
        self._at = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            block, self._at = self._data[self._at:], len(self._data)
            return block
        block = self._data[self._at:self._at + size]
        self._at += len(block)
        return block

    def close(self) -> None:
        self._at = len(self._data)
