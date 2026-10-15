"""复习卷应用服务（CR008）：HTTP 路由与 Agent 工具共用的业务链。

本轮只把出卷受理链从 router 抽到这里，其他路由保持原样。移动是逐字提取，行为不变：

- `resolve_accept_context`：约束/题型支持、provider 校验、材料选择与可用性、限额与指纹；
- `check_preview`：If-Match 指纹一致性（旧预检不能偷换新快照）；
- `freeze_snapshot` / `enqueue_job`：冻结快照行与 202 review_generate 作业；
- `preflight`：无副作用预检返回。

调用方负责事务：router 与 tool 各自决定何时 commit 与写幂等记录。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import review, review_deps, review_engine, secrets_store
from .errors import ApiError, not_found
from .models import Course, Job, ProviderConfig, ReviewMaterialSnapshot, ReviewPaper
from .repo import new_id


def require_review_engine() -> str:
    """D 出卷引擎必须在位（缺失 502，不伪造结果）；返回引擎版本。"""
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


def resolve_accept_context(
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


def check_preview(ctx: dict[str, Any], if_match: str | None) -> None:
    if if_match is not None and if_match.strip('"') != ctx["fingerprint"]:
        raise ApiError(409, "PREFLIGHT_FAILED", "资料或备注已变化，请重新预览后生成")


def require_bank_configured(ctx: dict[str, Any]) -> None:
    if not ctx["bank"]["usable"]:
        raise ApiError(
            400,
            "BANK_NOT_CONFIGURED",
            "bank 模式需要至少一个可用题库来源，当前未配置（可改用 ai/hybrid 模式或配置题库来源）",
            details={"bank": ctx["bank"]},
        )


def freeze_snapshot(db: Session, course_id: str, ctx: dict[str, Any]) -> ReviewMaterialSnapshot:
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


def enqueue_job(
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


def preflight_view(
    db: Session,
    course: Course,
    mode: str,
    provider_config_id: str | None,
    selection: dict[str, Any],
    notes: str,
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """无副作用预检：不创建快照、不入队、不发正文。"""
    ctx = resolve_accept_context(db, course, mode, provider_config_id, selection, notes, constraints)
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
            "notes_chars": len(notes),
            "notes_max_chars": review.NOTES_MAX_CHARS,
            "material_chars": ctx["total_chars"],
            "material_max_chars": review.MATERIAL_MAX_CHARS,
            "question_count_total": sum(q["count"] for q in quotas),
            "question_count_max": review.QUESTION_COUNT_MAX,
        },
        "bank": ctx["bank"],
        "warnings": review.gap_warnings(ctx["missing"], notes, ctx["bank"]),
    }


def accept_generation(
    db: Session,
    course: Course,
    *,
    mode: str,
    provider_config_id: str | None,
    selection: dict[str, Any],
    notes: str,
    constraints: dict[str, Any],
    title: str | None,
    if_match: str | None,
) -> tuple[ReviewPaper, ReviewMaterialSnapshot, Job]:
    """受理出卷：校验（含 If-Match）→ 冻结快照 → 入队 job。调用方负责 commit。"""
    ctx = resolve_accept_context(db, course, mode, provider_config_id, selection, notes, constraints)
    check_preview(ctx, if_match)
    if mode == "bank":
        require_bank_configured(ctx)
    paper_title = (title or "").strip() or f"{course.title} 复习卷"
    paper = ReviewPaper(
        id=new_id(db, ReviewPaper, "pap"),
        course_id=course.id,
        mode=mode,
        title=paper_title,
        latest_revision=0,
    )
    db.add(paper)
    snapshot = freeze_snapshot(db, course.id, ctx)
    job = enqueue_job(
        db,
        paper=paper,
        snapshot=snapshot,
        revision=1,
        revision_reason="initial",
        based_on_revision=None,
        provider_config_id=provider_config_id,
        title=paper_title,
    )
    db.flush()
    return paper, snapshot, job
