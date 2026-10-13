"""复习卷领域逻辑（CR008）：材料清单、选择解析（快照构建）、fingerprint、限制校验、预检组件。

- 材料正文一律用真实片段（`content_chunks`）拼接，不以标题/文件名冒充正文；
- 缺失如实列出（`missing`），不静默排除；备注逐字节保留，不 trim/不截断；
- fingerprint 公式与契约冻结一致（contracts/README.md 复习卷边界）：
  sha256 over 规范化 JSON {course_id, mode, engine_version, notes_sha256, constraints,
  materials:[{kind,id,version,content_hash,chunk_ids,chars}]}，
  sort_keys=True, separators=(",",":"), ensure_ascii=False（UTF-8）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import ApiError, not_found
from .models import Assignment, ContentChunk, Course, ProviderConfig, Resource, ResourceVersion, iso_utc
from .repo import decode_cursor, encode_cursor

NOTES_MAX_CHARS = 20000
MATERIAL_MAX_CHARS = 200000
QUESTION_COUNT_MAX = 100

_UNUSABLE_MESSAGES = {
    "not_parsed": "尚未解析；可先对该文件执行解析后重试。",
    "parse_failed": "解析失败；可重新解析该文件后重试。",
    "type_skipped": "该类型文件不参与正文解析（如压缩包）。",
    "no_text": "解析产物无可读文本（如扫描页）。",
    "chunk_missing": "所选片段不存在或不属于所选版本；请重新选择片段。",
    "attachment_absent": "作业附件缺失或未获取；可重新同步该作业。",
    "not_accessible": "平台仅预览、无下载权限，无法获取正文；可在平台内查看或手动上传可读版本。",
}


def _validation_error(fields: list[dict[str, Any]]) -> ApiError:
    return ApiError(400, "VALIDATION_FAILED", "请求参数校验失败", details={"fields": fields})


def resource_usability(access_level: str, parse_status: str, chunk_count: int) -> tuple[bool, str | None]:
    """资源可用性判定（权限受限优先于解析状态，与 review-materials 样例一致）。"""
    if access_level != "full":
        return False, "not_accessible"
    if parse_status == "failed":
        return False, "parse_failed"
    if parse_status == "skipped":
        return False, "type_skipped"
    if parse_status != "ready":
        return False, "not_parsed"
    if chunk_count == 0:
        return False, "no_text"
    return True, None


def notes_payload(text: str) -> dict[str, Any]:
    """备注原文 + sha256 + 字符数（逐字节保留，不 trim）。"""
    return {
        "text": text,
        "sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "chars": len(text),
    }


def compute_fingerprint(
    course_id: str,
    mode: str,
    engine_version: str,
    notes: dict[str, Any],
    constraints: dict[str, Any],
    items: list[dict[str, Any]],
) -> str:
    """冻结公式（B/D 一致）：材料/备注/约束/引擎版本任一变化必须改变结果。"""
    payload = {
        "course_id": course_id,
        "mode": mode,
        "engine_version": engine_version,
        "notes_sha256": notes["sha256"],
        "constraints": constraints,
        "materials": [
            {
                "kind": it["kind"],
                "id": it["resource_id"] or it["assignment_id"],
                "version": it["version"],
                "content_hash": it["content_hash"],
                "chunk_ids": list(it["chunk_ids"]),
                "chars": it["chars"],
            }
            for it in items
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_review_limits(notes_text: str, constraints: dict[str, Any], total_chars: int) -> None:
    """备注/材料/题量上限与总分一致性（超限 400，不静默截断；details 含 max_chars）。"""
    fields: list[dict[str, Any]] = []
    if len(notes_text) > NOTES_MAX_CHARS:
        fields.append({
            "field": "notes",
            "reason": f"备注 {len(notes_text)} 字符，超过上限 {NOTES_MAX_CHARS}（不静默截断，请精简后重试）",
            "max_chars": NOTES_MAX_CHARS,
        })
    if total_chars > MATERIAL_MAX_CHARS:
        fields.append({
            "field": "materials",
            "reason": f"材料正文合计 {total_chars} 字符，超过上限 {MATERIAL_MAX_CHARS}（请减少所选片段）",
            "max_chars": MATERIAL_MAX_CHARS,
        })
    quotas = constraints.get("question_types") or []
    total = sum(int(q.get("count") or 0) for q in quotas)
    if total > QUESTION_COUNT_MAX:
        fields.append({
            "field": "constraints.question_types",
            "reason": f"题数合计 {total} 超过单卷上限 {QUESTION_COUNT_MAX}",
            "max_count": QUESTION_COUNT_MAX,
        })
    total_points = constraints.get("total_points")
    if total_points is not None and quotas and all(q.get("points_each") is not None for q in quotas):
        summed = sum(int(q["count"]) * int(q["points_each"]) for q in quotas)
        if summed != total_points:
            fields.append({
                "field": "constraints.total_points",
                "reason": (
                    f"各题型分值合计 {summed} 与总分 {total_points} 不一致"
                    "（请调整 points_each，或置 null 由引擎分配）"
                ),
            })
    if fields:
        raise _validation_error(fields)


def ensure_usable_selection(items: list[dict[str, Any]], missing: list[dict[str, Any]]) -> None:
    """资料不足（无任何可用正文）在受理前 400，details 列出缺口；不假装已解析。"""
    if items:
        return
    if missing:
        reason = "所选材料均无可用正文（详见 missing 缺口清单）"
    else:
        reason = "未选择任何材料（resources/assignments 均为空）"
    raise ApiError(
        400,
        "VALIDATION_FAILED",
        "资料不足，无法出卷",
        details={"fields": [{"field": "materials", "reason": reason}], "missing": missing},
    )


def _chunk_stats(db: Session, resource_ids: list[str]) -> dict[tuple[str, int], tuple[int, int]]:
    """(resource_id, version) → (chunk_count, chars)（一次查询，Python 侧按版本归组）。"""
    if not resource_ids:
        return {}
    rows = db.execute(
        select(ContentChunk).where(ContentChunk.resource_id.in_(resource_ids))
    ).scalars()
    stats: dict[tuple[str, int], tuple[int, int]] = {}
    for chunk in rows:
        key = (chunk.resource_id, chunk.resource_version)
        count, chars = stats.get(key, (0, 0))
        stats[key] = (count + 1, chars + len(chunk.text))
    # chars 与正文拼接口径一致（片段间以 \n 连接，与 material item 的 len(text) 相同）
    return {key: (count, chars + max(0, count - 1)) for key, (count, chars) in stats.items()}


def _resource_item(
    res: Resource,
    chunk_count: int,
    chars: int,
) -> dict[str, Any]:
    usable, reason = resource_usability(res.access_level, res.parse_status, chunk_count)
    ready = res.parse_status == "ready"
    return {
        "id": res.id,
        "kind": "resource",
        "title": res.title,
        "course_id": res.course_id,
        "updated_at": iso_utc(res.updated_at),
        "usable": usable,
        "unavailable_reason": reason,
        "parse_status": res.parse_status,
        "version": res.version,
        "chunk_count": chunk_count if ready else None,
        "chars": chars if usable else None,
        "due_at": None,
        "due_precision": None,
        "attachments": [],
    }


def _attachment_item(res: Resource, chunk_count: int, chars: int) -> dict[str, Any]:
    usable, reason = resource_usability(res.access_level, res.parse_status, chunk_count)
    ready = res.parse_status == "ready"
    return {
        "resource_id": res.id,
        "title": res.title,
        "parse_status": res.parse_status,
        "version": res.version,
        "chunk_count": chunk_count if ready else None,
        "chars": chars if usable else None,
        "usable": usable,
        "unavailable_reason": reason,
    }


def _missing_attachment(attachment_id: str) -> dict[str, Any]:
    return {
        "resource_id": attachment_id,
        "title": attachment_id,
        "parse_status": None,
        "version": None,
        "chunk_count": None,
        "chars": None,
        "usable": False,
        "unavailable_reason": "attachment_absent",
    }


def list_materials(
    db: Session, course_id: str, cursor: str | None, limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    """课程复习材料清单：课件/附件（resource）在前、作业（assignment）在后，组内按 ID 升序 keyset 分页。"""
    resources = list(
        db.execute(
            select(Resource)
            .where(Resource.course_id == course_id, Resource.archived_at.is_(None))
            .order_by(Resource.id)
        ).scalars()
    )
    assignments = list(
        db.execute(
            select(Assignment)
            .where(Assignment.course_id == course_id, Assignment.archived_at.is_(None))
            .order_by(Assignment.id)
        ).scalars()
    )
    resource_map = {r.id: r for r in resources}
    attachment_ids = [
        rid for asg in assignments for rid in (asg.attachment_resource_ids or []) if rid not in resource_map
    ]
    extra_resources: dict[str, Resource] = {}
    if attachment_ids:
        extra_resources = {
            r.id: r
            for r in db.execute(select(Resource).where(Resource.id.in_(attachment_ids))).scalars()
        }
    all_resource_ids = [r.id for r in resources] + list(extra_resources.keys())
    stats = _chunk_stats(db, all_resource_ids)

    def _stats_for(res: Resource) -> tuple[int, int]:
        return stats.get((res.id, res.version), (0, 0))

    merged: list[tuple[int, str, dict[str, Any]]] = []
    for res in resources:
        count, chars = _stats_for(res)
        merged.append((0, res.id, _resource_item(res, count, chars)))
    for asg in assignments:
        attachments = []
        for rid in asg.attachment_resource_ids or []:
            att = resource_map.get(rid) or extra_resources.get(rid)
            if att is None or att.course_id != course_id or att.archived_at is not None:
                attachments.append(_missing_attachment(rid))
                continue
            count, chars = _stats_for(att)
            attachments.append(_attachment_item(att, count, chars))
        instructions = asg.instructions or ""
        usable = bool(instructions)
        merged.append((1, asg.id, {
            "id": asg.id,
            "kind": "assignment",
            "title": asg.title,
            "course_id": asg.course_id,
            "updated_at": iso_utc(asg.updated_at),
            "usable": usable,
            "unavailable_reason": None if usable else "no_text",
            "parse_status": None,
            "version": None,
            "chunk_count": None,
            "chars": len(instructions) if usable else None,
            "due_at": iso_utc(asg.due_at) if asg.due_at is not None else None,
            "due_precision": asg.due_precision,
            "attachments": attachments,
        }))

    if cursor is not None:
        raw = decode_cursor(cursor)
        rank_text, _, last_id = raw.partition("|")
        try:
            after_rank = int(rank_text)
        except ValueError:
            raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor}) from None
        if not last_id:
            raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor})
        merged = [m for m in merged if (m[0], m[1]) > (after_rank, last_id)]
    rows = merged[: limit + 1]
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(f"{rows[-1][0]}|{rows[-1][1]}")
    return [m[2] for m in rows], next_cursor


def _selected_version_row(db: Session, res: Resource, version: int) -> ResourceVersion | None:
    if version == res.version:
        return None  # 当前版本以 Resource 行为准
    row = db.get(ResourceVersion, (res.id, version))
    if row is None:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "请求参数校验失败",
            details={"fields": [{"field": "materials", "reason": f"资源 {res.id} 无版本 {version}（引用不存在）"}]},
        )
    return row


def _chunks_for(db: Session, resource_id: str, version: int) -> list[ContentChunk]:
    return list(
        db.execute(
            select(ContentChunk)
            .where(ContentChunk.resource_id == resource_id, ContentChunk.resource_version == version)
            .order_by(ContentChunk.ordinal, ContentChunk.id)
        ).scalars()
    )


def _missing_entry(
    kind: str, resource_id: str | None, assignment_id: str | None, title: str, reason: str, message: str | None = None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "resource_id": resource_id,
        "assignment_id": assignment_id,
        "title": title,
        "reason": reason,
        "message": message or _UNUSABLE_MESSAGES[reason],
    }


def _material_item_from_chunks(
    kind: str,
    resource_id: str | None,
    assignment_id: str | None,
    title: str,
    version: int | None,
    content_hash: str | None,
    chunks: list[ContentChunk],
) -> dict[str, Any]:
    # 按 (ordinal, id) 排序（chk_ 后缀为随机十六进制，不能按 id 字面排序冒充阅读顺序）
    ordered = sorted(chunks, key=lambda c: (c.ordinal, c.id))
    chunk_ids = [c.id for c in ordered]
    text = "\n".join(c.text for c in ordered)
    return {
        "kind": kind,
        "resource_id": resource_id,
        "assignment_id": assignment_id,
        "title": title,
        "version": version,
        "content_hash": content_hash,
        "chunk_ids": chunk_ids,
        "locators": [{"chunk_id": c.id, "locator": c.locator} for c in ordered],
        "chars": len(text),
        "text": text,
    }


def resolve_selection(
    db: Session, course_id: str, selection: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按请求选择解析出（items, missing）：item=将发送的正文原文；missing=选中但无法纳入的缺口。"""
    items: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen_resources: set[tuple[str, int | None]] = set()
    for ref in selection.get("resources") or []:
        resource_id = ref["resource_id"]
        dedupe_key = (resource_id, ref.get("version"))
        if dedupe_key in seen_resources:
            continue
        seen_resources.add(dedupe_key)
        res = db.get(Resource, resource_id)
        if res is None:
            raise not_found("资源", resource_id)
        if res.course_id != course_id:
            raise _validation_error([{"field": "materials.resources", "reason": f"资源 {resource_id} 不属于该课程"}])
        if res.archived_at is not None:
            raise _validation_error([{"field": "materials.resources", "reason": f"资源 {resource_id} 已归档，不可用于出卷"}])
        version = ref.get("version") or res.version
        if ref.get("version") is not None and ref["version"] > res.version:
            raise _validation_error([
                {"field": "materials.resources", "reason": f"资源 {resource_id} 当前版本为 {res.version}，请求版本 {ref['version']} 不存在"}
            ])
        version_row = _selected_version_row(db, res, version)
        parse_status = version_row.parse_status if version_row is not None else res.parse_status
        content_hash = (version_row.content_hash if version_row is not None else res.content_hash)
        usable, reason = resource_usability(res.access_level, parse_status, 1)
        if not usable and reason != "no_text":
            missing.append(_missing_entry("resource", res.id, None, res.title, reason))
            continue
        chunks = _chunks_for(db, res.id, version)
        if not chunks:
            missing.append(_missing_entry("resource", res.id, None, res.title, "no_text"))
            continue
        requested = ref.get("chunk_ids")
        if requested is None:
            selected = chunks
        else:
            wanted = set(requested)
            selected = [c for c in chunks if c.id in wanted]
            absent = wanted - {c.id for c in selected}
            if absent:
                missing.append(_missing_entry(
                    "resource", res.id, None, res.title, "chunk_missing",
                    message=f"所选片段中有 {len(absent)} 个不存在或不属于所选版本（已按可用片段纳入）。",
                ))
        if not selected:
            continue
        items.append(_material_item_from_chunks(
            "resource_text", res.id, None, res.title, version, content_hash, selected,
        ))
    seen_assignments: set[str] = set()
    for ref in selection.get("assignments") or []:
        assignment_id = ref["assignment_id"]
        if assignment_id in seen_assignments:
            continue
        seen_assignments.add(assignment_id)
        asg = db.get(Assignment, assignment_id)
        if asg is None:
            raise not_found("作业", assignment_id)
        if asg.course_id != course_id:
            raise _validation_error([{"field": "materials.assignments", "reason": f"作业 {assignment_id} 不属于该课程"}])
        if asg.archived_at is not None:
            raise _validation_error([{"field": "materials.assignments", "reason": f"作业 {assignment_id} 已归档，不可用于出卷"}])
        instructions = asg.instructions or ""
        if not instructions:
            missing.append(_missing_entry("assignment", None, asg.id, asg.title, "no_text"))
        else:
            items.append({
                "kind": "assignment_text",
                "resource_id": None,
                "assignment_id": asg.id,
                "title": asg.title,
                "version": None,
                "content_hash": None,
                "chunk_ids": [],
                "locators": [],
                "chars": len(instructions),
                "text": instructions,
            })
        if ref.get("include_attachments", True):
            for rid in asg.attachment_resource_ids or []:
                att = db.get(Resource, rid)
                if att is None or att.course_id != course_id or att.archived_at is not None:
                    missing.append(_missing_entry("attachment", rid, asg.id, rid, "attachment_absent"))
                    continue
                att_usable, att_reason = resource_usability(att.access_level, att.parse_status, 1)
                if not att_usable and att_reason != "no_text":
                    missing.append(_missing_entry("attachment", att.id, asg.id, att.title, att_reason))
                    continue
                chunks = _chunks_for(db, att.id, att.version)
                if not chunks:
                    missing.append(_missing_entry("attachment", att.id, asg.id, att.title, "no_text"))
                    continue
                items.append(_material_item_from_chunks(
                    "attachment_text", att.id, asg.id, f"{att.title}（作业附件）",
                    att.version, att.content_hash, chunks,
                ))
    return items, missing


def total_chars(items: list[dict[str, Any]]) -> int:
    return sum(int(it["chars"]) for it in items)


def provider_summary(cfg: ProviderConfig, has_api_key: bool) -> dict[str, Any]:
    parts = urlsplit(cfg.base_url)
    return {
        "config_id": cfg.id,
        "name": cfg.name,
        "protocol": cfg.protocol,
        "base_url": cfg.base_url,
        "host": parts.netloc,
        "model": cfg.model,
        "has_api_key": has_api_key,
    }


def empty_provider_summary() -> dict[str, Any]:
    return {
        "config_id": None,
        "name": None,
        "protocol": None,
        "base_url": None,
        "host": None,
        "model": None,
        "has_api_key": False,
    }


_BANK_SOURCE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "src_http_search",
        "name": "自建检索服务（HTTP）",
        "kind": "search_service",
        "engine_kinds": ("search_service",),
        "requires_config": True,
        "searchable": True,
        "fetchable": True,
        "answer_available": False,
        "formula_support": "unknown",
        "notes": "未配置服务地址前不可用；配置后仅发送提炼检索词，不上传课件/作业/备注原文。",
    },
    {
        "id": "src_questionbank_api",
        "name": "示例题库 API",
        "kind": "questionbank_api",
        "engine_kinds": ("bank_api",),
        "requires_config": True,
        "searchable": True,
        "fetchable": True,
        "answer_available": True,
        "formula_support": "text",
        "notes": "按上游 API 文档转换字段；许可与使用限制以来源页声明为准。",
    },
    {
        "id": "src_user_url",
        "name": "用户提供链接导入",
        "kind": "user_url",
        "engine_kinds": ("url_import",),
        "requires_config": False,
        "searchable": False,
        "fetchable": True,
        "answer_available": False,
        "formula_support": "unknown",
        "notes": "单页/文档导入；不自动扫站、不绕过登录与验证码。",
    },
]


def questionbank_capabilities_view(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A 模块原始能力 → 契约 catalog（三种来源模式；configured 动态，未配置不得显示“已连接”）。"""
    entries = (raw or {}).get("sources") or []
    configured_engine_kinds = {str(e.get("kind") or "") for e in entries if e.get("configured")}
    sources: list[dict[str, Any]] = []
    for spec in _BANK_SOURCE_CATALOG:
        configured = True if not spec["requires_config"] else any(
            k in configured_engine_kinds for k in spec["engine_kinds"]
        )
        sources.append({
            "id": spec["id"],
            "name": spec["name"],
            "kind": spec["kind"],
            "configured": configured,
            "requires_config": spec["requires_config"],
            "searchable": spec["searchable"],
            "fetchable": spec["fetchable"],
            "answer_available": spec["answer_available"],
            "formula_support": spec["formula_support"],
            "notes": spec["notes"],
        })
    return {"sources": sources}


def bank_summary(mode: str, capabilities: dict[str, Any] | None) -> dict[str, Any]:
    """题库可用性摘要（ai 模式不咨询题库；bank/hybrid 无可用来源 usable=false 并给出建议）。"""
    if mode == "ai":
        return {
            "mode": "ai",
            "usable": False,
            "configured_sources": 0,
            "message": "当前为 AI 模式，不使用题库检索。",
        }
    if capabilities is None:
        configured = 0
        usable = False
    else:
        entries = capabilities.get("sources") or []
        configured = sum(1 for e in entries if e.get("configured"))
        usable = bool(capabilities.get("configured")) and configured > 0
    message = None if usable else (
        "未配置题库来源：bank/hybrid 模式需要至少一个可用来源"
        "（可通过 STUDY_QUESTIONBANK_SOURCES 配置检索服务或 URL 导入列表），或改用 ai 模式。"
    )
    return {"mode": mode, "usable": usable, "configured_sources": configured, "message": message}


def gap_warnings(missing: list[dict[str, Any]], notes_text: str, bank: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if missing:
        detail = "、".join(f"{m['title']}（{m['reason']}，未纳入发送内容）" for m in missing)
        warnings.append({
            "code": "MATERIAL_GAP",
            "message": f"{len(missing)} 项所选材料无可用正文：{detail}。",
        })
    if not notes_text:
        warnings.append({
            "code": "NOTES_EMPTY",
            "message": "未填写备注；出卷将仅依据所选材料。",
        })
    if not bank["usable"] and bank["mode"] in ("bank", "hybrid"):
        warnings.append({
            "code": "BANK_NOT_CONFIGURED",
            "message": "题库未配置：bank/hybrid 模式无可用来源；bank 模式将被拒绝，hybrid 将仅用本地生成并如实标注。",
        })
    return warnings
