"""复习卷路由（CR008）：材料清单/预检（#46）、生成与修订受理（#47）、查询与导出（#48）。

- GET /courses/{course_id}/review-materials：课程课件/附件与作业（含附件）的可用性清单；
- POST /review-papers/preflight：无副作用预检（不发正文、不启动生成）；
- POST /review-papers/generate：受理出卷（冻结快照 + 快照行落库 + 202 review_generate 作业，Idempotency-Key 保护）；
- POST /review-papers/{paper_id}/revisions：重做/修订（未提供字段复用上一版；revision=上一版+1）；
- GET /review-papers：列表（仅已生成成功的卷，created_at 倒序 keyset 分页，course_id/mode 过滤）；
- GET /review-papers/{paper_id}：取指定/最新版本（历史版本连原快照一起返回）；
- GET /review-papers/{paper_id}/revisions：版本索引（revision 倒序游标分页）；
- GET /review-papers/{paper_id}/export：服务端渲染导出（markdown 完整卷 / student 学生版 / answers 答案版）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import review, review_deps, review_engine, review_export, secrets_store
from ..db import get_db
from ..errors import ApiError, not_found
from ..models import (
    Course,
    Job,
    ProviderConfig,
    ReviewMaterialSnapshot,
    ReviewPaper,
    ReviewPaperRevision,
)
from ..repo import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    decode_cursor,
    decode_int_cursor,
    encode_cursor,
    get_or_404,
    idempotency_lookup,
    idempotency_store,
    new_id,
    validate_idempotency_key,
)
from ..schemas import ReviewRequestIn, ReviewRevisionRequest

LOG = logging.getLogger("app.review_papers")

router = APIRouter(tags=["review-papers"])

ENDPOINT_GENERATE = "POST /review-papers/generate"
ENDPOINT_REVISE = "POST /review-papers/revisions"


def require_review_engine() -> str:
    """D 出卷引擎必须在位（缺失 502，不伪造结果）；返回引擎版本（REVIEW_ENGINE_VERSION）。"""
    module = review_deps.review_engine_module()
    version = None
    if module is not None:
        version = getattr(module, "REVIEW_ENGINE_VERSION", None) or getattr(module, "ENGINE_VERSION", None)
    if not version:
        raise ApiError(
            502,
            "DEPENDENCY_UNAVAILABLE",
            "出卷引擎模块未接入（learning.review / STUDY_REVIEW_ENGINE_MODULE），无法出卷",
        )
    return str(version)


def require_questionbank_capabilities() -> dict[str, Any]:
    """A 题库模块必须在位（bank/hybrid 缺失 502）；返回 get_questionbank_capabilities 结果。"""
    module = review_deps.questionbank_module()
    if module is None:
        raise ApiError(
            502,
            "DEPENDENCY_UNAVAILABLE",
            "题库模块未接入（connectors.questionbanks / STUDY_QUESTIONBANK_MODULE），无法使用 bank/hybrid 模式",
        )
    try:
        return module.get_questionbank_capabilities(review_deps.questionbank_sources_config())
    except Exception as exc:  # noqa: BLE001 —— 依赖内部故障如实上报
        LOG.error("题库能力查询失败：%s: %s", type(exc).__name__, exc)
        raise ApiError(502, "DEPENDENCY_UNAVAILABLE", "题库模块能力查询失败，请检查模块与来源配置") from exc


def resolve_provider_block(db: Session, mode: str, provider_config_id: str | None) -> dict[str, Any]:
    """预检/生成/修订共用的 provider 校验：ai/hybrid 须有配置且已存密钥；bank 须为 null。"""
    if mode == "bank":
        if provider_config_id is not None:
            raise ApiError(
                400,
                "VALIDATION_FAILED",
                "请求参数校验失败",
                details={"fields": [{"field": "provider_config_id", "reason": "bank 模式不调用模型，provider_config_id 须为 null"}]},
            )
        return review.empty_provider_summary()
    if not provider_config_id:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "请求参数校验失败",
            details={"fields": [{"field": "provider_config_id", "reason": "AI/混合模式必须选择 AI 服务配置"}]},
        )
    cfg = db.get(ProviderConfig, provider_config_id)
    if cfg is None:
        raise not_found("AI 服务配置", provider_config_id)
    if not secrets_store.has_key(cfg.id):
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "请求参数校验失败",
            details={"fields": [{"field": "provider_config_id", "reason": "该配置尚未保存密钥；请先填写并测试连接"}]},
        )
    return review.provider_summary(cfg, True)


def _resolve_accept_context(
    db: Session,
    course: Course,
    mode: str,
    provider_config_id: str | None,
    selection: dict[str, Any],
    notes: str,
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """预检/生成/修订共用受理链（只读；冻结/落库由调用方执行）。"""
    unsupported = review_engine.unsupported_contract_types(constraints)
    if unsupported:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "请求参数校验失败",
            details={"fields": [{
                "field": "constraints.question_types",
                "reason": f"题型暂不被出卷引擎支持：{'、'.join(unsupported)}（请改用支持的题型）",
            }]},
        )
    provider_block = resolve_provider_block(db, mode, provider_config_id)
    items, missing = review.resolve_selection(db, course.id, selection)
    review.ensure_usable_selection(items, missing)
    chars = review.total_chars(items)
    review.validate_review_limits(notes, constraints, chars)
    engine_version = require_review_engine()
    capabilities = require_questionbank_capabilities() if mode in ("bank", "hybrid") else None
    bank = review.bank_summary(mode, capabilities)
    notes_pl = review.notes_payload(notes)
    fingerprint = review.compute_fingerprint(course.id, mode, engine_version, notes_pl, constraints, items)
    return {
        "items": items,
        "missing": missing,
        "total_chars": chars,
        "constraints": constraints,
        "notes": notes_pl,
        "provider": provider_block,
        "engine_version": engine_version,
        "bank": bank,
        "fingerprint": fingerprint,
    }


def _check_preview(ctx: dict[str, Any], if_match: str | None) -> None:
    if if_match is not None and if_match.strip('"') != ctx["fingerprint"]:
        raise ApiError(409, "PREFLIGHT_FAILED", "资料或备注已变化，请重新预览后生成")


def _require_bank_configured(ctx: dict[str, Any]) -> None:
    if not ctx["bank"]["usable"]:
        raise ApiError(
            400,
            "BANK_NOT_CONFIGURED",
            "bank 模式需要至少一个可用题库来源，当前未配置（可改用 ai/hybrid 模式或配置题库来源）",
            details={"bank": ctx["bank"]},
        )


def _freeze_snapshot(db: Session, course_id: str, ctx: dict[str, Any]) -> ReviewMaterialSnapshot:
    snapshot = ReviewMaterialSnapshot(
        id=new_id(db, ReviewMaterialSnapshot, "snp"),
        course_id=course_id,
        engine_version=ctx["engine_version"],
        fingerprint=ctx["fingerprint"],
        constraints=ctx["constraints"],
        notes=ctx["notes"],
        items=ctx["items"],
        missing=ctx["missing"],
        total_chars=ctx["total_chars"],
    )
    db.add(snapshot)
    return snapshot


def _enqueue_review_job(
    db: Session,
    *,
    paper: ReviewPaper,
    snapshot: ReviewMaterialSnapshot,
    revision: int,
    revision_reason: str,
    based_on_revision: int | None,
    provider_config_id: str | None,
    title: str,
) -> Job:
    provider = db.get(ProviderConfig, provider_config_id) if provider_config_id else None
    job = Job(
        id=new_id(db, Job, "job"),
        type="review_generate",
        status="queued",
        progress=0.0,
        errors=[],
        result_refs=[],
        payload={
            "paper_id": paper.id,
            "snapshot_id": snapshot.id,
            "revision": revision,
            "revision_reason": revision_reason,
            "based_on_revision": based_on_revision,
            "mode": paper.mode,
            "provider_config_id": provider_config_id,
            "provider_snapshot": review.provider_summary(provider, True) if provider else None,
            "title": title,
        },
    )
    db.add(job)
    return job


def _selection_from_snapshot(snapshot: ReviewMaterialSnapshot) -> dict[str, Any]:
    """上一版快照 items → 等价选择（修订缺省复用：按当前解析结果重新冻结）。

    附件正文（attachment_text）不能伪装成资源重选：其 kind/assignment_id 是身份的一部分
    （compute_fingerprint 含 kind），必须经由所属作业的 include_attachments 重建，否则材料
    身份/fingerprint 漂移，未变更的修订会被误判为 revise。
    """
    items = snapshot.items or []
    attachments_by_assignment = {
        it.get("assignment_id") for it in items if it.get("kind") == "attachment_text"
    }
    resources: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("kind")
        if kind == "resource_text":
            resources.append({
                "resource_id": item.get("resource_id"),
                "version": item.get("version"),
                "chunk_ids": list(item.get("chunk_ids") or []),
            })
        elif kind == "assignment_text":
            asg_id = item.get("assignment_id")
            if asg_id is None:
                continue
            assignments.append({
                "assignment_id": asg_id,
                "include_attachments": asg_id in attachments_by_assignment,
            })
    return {"resources": resources, "assignments": assignments}


def _item_identity(items: list[dict[str, Any]]) -> list[tuple]:
    return [
        (it.get("kind"), it.get("resource_id"), it.get("assignment_id"),
         it.get("version"), tuple(it.get("chunk_ids") or []))
        for it in items
    ]


@router.get("/questionbanks/capabilities", tags=["questionbanks"])
def questionbank_capabilities() -> dict[str, Any]:
    raw = require_questionbank_capabilities()
    return review.questionbank_capabilities_view(raw)


@router.get("/courses/{course_id}/review-materials")
def list_review_materials(
    course_id: str,
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    get_or_404(db, Course, course_id, "课程")
    items, next_cursor = review.list_materials(db, course_id, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@router.post("/review-papers/preflight")
def preflight_review_paper(request: ReviewRequestIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    course = get_or_404(db, Course, request.course_id, "课程")
    constraints = request.constraints.model_dump()
    ctx = _resolve_accept_context(
        db, course, request.mode, request.provider_config_id,
        request.materials.model_dump(), request.notes, constraints,
    )
    quotas = constraints["question_types"]
    return {
        "fingerprint": ctx["fingerprint"],
        "materials": {
            "items": ctx["items"],
            "missing": ctx["missing"],
            "notes": ctx["notes"],
            "total_chars": ctx["total_chars"],
        },
        "provider": ctx["provider"],
        "limits": {
            "notes_chars": len(request.notes),
            "notes_max_chars": review.NOTES_MAX_CHARS,
            "material_chars": ctx["total_chars"],
            "material_max_chars": review.MATERIAL_MAX_CHARS,
            "question_count_total": sum(q["count"] for q in quotas),
            "question_count_max": review.QUESTION_COUNT_MAX,
        },
        "bank": ctx["bank"],
        "warnings": review.gap_warnings(ctx["missing"], request.notes, ctx["bank"]),
    }


@router.post("/review-papers/generate", status_code=202)
def generate_review_paper(
    request: ReviewRequestIn,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    validate_idempotency_key(idempotency_key)
    fingerprint = request.model_dump()
    replay = idempotency_lookup(db, idempotency_key, ENDPOINT_GENERATE, fingerprint)
    if replay is not None:
        status_code, payload = replay
        return JSONResponse(status_code=status_code, content=payload)

    course = get_or_404(db, Course, request.course_id, "课程")
    constraints = request.constraints.model_dump()
    ctx = _resolve_accept_context(
        db, course, request.mode, request.provider_config_id,
        request.materials.model_dump(), request.notes, constraints,
    )
    _check_preview(ctx, if_match)
    if request.mode == "bank":
        _require_bank_configured(ctx)
    title = (request.title or "").strip() or f"{course.title} 复习卷"
    paper = ReviewPaper(
        id=new_id(db, ReviewPaper, "pap"),
        course_id=course.id,
        mode=request.mode,
        title=title,
        latest_revision=0,
    )
    db.add(paper)
    snapshot = _freeze_snapshot(db, course.id, ctx)
    job = _enqueue_review_job(
        db,
        paper=paper,
        snapshot=snapshot,
        revision=1,
        revision_reason="initial",
        based_on_revision=None,
        provider_config_id=request.provider_config_id,
        title=title,
    )
    db.flush()
    payload: dict[str, Any] = {"job": job.to_contract()}
    idempotency_store(db, idempotency_key, ENDPOINT_GENERATE, fingerprint, 202, payload)
    db.commit()
    return payload


@router.post("/review-papers/{paper_id}/revisions", status_code=202)
def create_review_paper_revision(
    paper_id: str,
    request: ReviewRevisionRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    validate_idempotency_key(idempotency_key)
    fingerprint = {"paper_id": paper_id, **request.model_dump()}
    replay = idempotency_lookup(db, idempotency_key, ENDPOINT_REVISE, fingerprint)
    if replay is not None:
        status_code, payload = replay
        return JSONResponse(status_code=status_code, content=payload)

    paper = db.get(ReviewPaper, paper_id)
    if paper is None:
        raise not_found("复习卷", paper_id)
    if paper.latest_revision < 1:
        raise ApiError(
            409,
            "CONFLICT",
            "该试卷尚未成功生成，暂不能重做（请等待首次生成完成或重新发起生成）",
            details={"paper_id": paper_id, "latest_revision": paper.latest_revision},
        )
    prev = db.get(ReviewPaperRevision, (paper_id, paper.latest_revision))
    prev_snapshot = db.get(ReviewMaterialSnapshot, prev.snapshot_id) if prev else None
    if prev is None or prev_snapshot is None:
        raise ApiError(500, "INTERNAL_ERROR", "试卷历史版本数据缺失，无法重做", details={"paper_id": paper_id})

    mode = paper.mode
    provider_config_id = (
        request.provider_config_id if request.provider_config_id is not None
        else (prev.provider or {}).get("config_id")
    )
    title = (request.title.strip() if request.title is not None else "") or prev.title
    selection = (
        request.materials.model_dump() if request.materials is not None
        else _selection_from_snapshot(prev_snapshot)
    )
    notes = request.notes if request.notes is not None else str((prev_snapshot.notes or {}).get("text") or "")
    constraints = request.constraints.model_dump() if request.constraints is not None else dict(prev.constraints or {})

    course = get_or_404(db, Course, paper.course_id, "课程")
    ctx = _resolve_accept_context(db, course, mode, provider_config_id, selection, notes, constraints)
    _check_preview(ctx, if_match)
    if mode == "bank":
        _require_bank_configured(ctx)

    prev_items = list(prev_snapshot.items or [])
    prev_notes_text = str((prev_snapshot.notes or {}).get("text") or "")
    materials_changed = _item_identity(ctx["items"]) != _item_identity(prev_items)
    notes_changed = notes != prev_notes_text
    constraints_changed = constraints != dict(prev_snapshot.constraints or {})
    provider_changed = (provider_config_id or None) != ((prev.provider or {}).get("config_id") or None)
    title_changed = title != prev.title
    revision_reason = "revise" if any(
        (materials_changed, notes_changed, constraints_changed, provider_changed, title_changed)
    ) else "regenerate"

    snapshot = _freeze_snapshot(db, course.id, ctx)
    job = _enqueue_review_job(
        db,
        paper=paper,
        snapshot=snapshot,
        revision=prev.revision + 1,
        revision_reason=revision_reason,
        based_on_revision=prev.revision,
        provider_config_id=provider_config_id,
        title=title,
    )
    db.flush()
    payload: dict[str, Any] = {"job": job.to_contract()}
    idempotency_store(db, idempotency_key, ENDPOINT_REVISE, fingerprint, 202, payload)
    db.commit()
    return payload


# ────────────────────────── 查询与导出（#48） ──────────────────────────


def _revision_row(
    db: Session, paper: ReviewPaper, revision: int | None
) -> ReviewPaperRevision | None:
    """指定版本行；revision=None 取最新（latest_revision<1 → None）。"""
    target = paper.latest_revision if revision is None else revision
    if target is None or target < 1:
        return None
    return db.get(ReviewPaperRevision, (paper.id, int(target)))


def _paper_payload(db: Session, paper: ReviewPaper, row: ReviewPaperRevision) -> dict[str, Any]:
    snapshot = db.get(ReviewMaterialSnapshot, row.snapshot_id)
    if snapshot is None:
        raise ApiError(500, "INTERNAL_ERROR", "试卷快照数据缺失，无法读取", details={"paper_id": paper.id})
    return row.to_paper_contract(paper, snapshot)


def _paper_or_404(db: Session, paper_id: str) -> ReviewPaper:
    paper = db.get(ReviewPaper, paper_id)
    if paper is None:
        raise not_found("复习卷", paper_id)
    return paper


@router.get("/review-papers")
def list_review_papers(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    course_id: str | None = Query(default=None),
    mode: str | None = Query(default=None, pattern="^(ai|bank|hybrid)$"),
) -> dict[str, Any]:
    stmt = select(ReviewPaper).where(ReviewPaper.latest_revision >= 1)
    if course_id is not None:
        stmt = stmt.where(ReviewPaper.course_id == course_id)
    if mode is not None:
        stmt = stmt.where(ReviewPaper.mode == mode)
    if cursor is not None:
        created_text, _, paper_id = decode_cursor(cursor).partition("|")
        try:
            created = datetime.fromisoformat(created_text)
        except ValueError:
            raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor}) from None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        stmt = stmt.where(or_(
            ReviewPaper.created_at < created,
            and_(ReviewPaper.created_at == created, ReviewPaper.id < paper_id),
        ))
    rows = list(db.execute(
        stmt.order_by(ReviewPaper.created_at.desc(), ReviewPaper.id.desc()).limit(limit + 1)
    ).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        created = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc)
        next_cursor = encode_cursor(f"{created.astimezone(timezone.utc).isoformat()}|{last.id}")
    items = []
    for paper in rows:
        row = _revision_row(db, paper, None)
        if row is None:  # latest_revision>=1 但行缺失 = 数据损坏，不静默丢卷
            raise ApiError(500, "INTERNAL_ERROR", "试卷历史版本数据缺失", details={"paper_id": paper.id})
        items.append(_paper_payload(db, paper, row))
    return {"items": items, "next_cursor": next_cursor}


@router.get("/review-papers/{paper_id}")
def get_review_paper(
    paper_id: str,
    db: Session = Depends(get_db),
    revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    paper = _paper_or_404(db, paper_id)
    row = _revision_row(db, paper, revision)
    if row is None:
        if revision is None:
            raise ApiError(404, "NOT_FOUND", "该试卷尚未生成成功（无可用版本）", details={"paper_id": paper_id})
        raise not_found("试卷版本", f"{paper_id} 第 {revision} 版")
    return _paper_payload(db, paper, row)


@router.get("/review-papers/{paper_id}/revisions")
def list_review_paper_revisions(
    paper_id: str,
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    _paper_or_404(db, paper_id)
    stmt = select(ReviewPaperRevision).where(ReviewPaperRevision.paper_id == paper_id)
    if cursor is not None:
        stmt = stmt.where(ReviewPaperRevision.revision < decode_int_cursor(cursor))
    rows = list(db.execute(
        stmt.order_by(ReviewPaperRevision.revision.desc()).limit(limit + 1)
    ).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(str(rows[-1].revision))
    return {"items": [row.to_index_contract() for row in rows], "next_cursor": next_cursor}


@router.get("/review-papers/{paper_id}/export")
def export_review_paper(
    paper_id: str,
    db: Session = Depends(get_db),
    format: str = Query(default="markdown", pattern="^(markdown|student|answers)$"),
    revision: int | None = Query(default=None, ge=1),
) -> Response:
    paper = _paper_or_404(db, paper_id)
    row = _revision_row(db, paper, revision)
    if row is None:
        if revision is None:
            raise ApiError(404, "NOT_FOUND", "该试卷尚未生成成功（无可用版本）", details={"paper_id": paper_id})
        raise not_found("试卷版本", f"{paper_id} 第 {revision} 版")
    payload = _paper_payload(db, paper, row)
    course = db.get(Course, paper.course_id)
    media_type, renderer = review_export.RENDERERS[format]
    body = renderer(payload, course.title if course is not None else None)
    filename = review_export.export_filename(payload.get("title") or paper.title, format)
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "review-paper"
    disposition = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(content=body, media_type=media_type, headers={"Content-Disposition": disposition})
