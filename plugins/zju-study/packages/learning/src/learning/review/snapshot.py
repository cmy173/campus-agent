"""复习卷快照：选定资料 + 备注原文 + 非密钥配置 → 冻结快照与 fingerprint。

- 快照只增不改：原材料更新不会覆盖历史快照；新版本生成新快照与新 fingerprint。
- fingerprint 参与：材料版本、选中范围（片段 id/正文 hash/定位）、作业与附件、备注原文、
  出题约束、引擎版本；与 B 统一采用 sha256(canonical JSON) 计算。
- 定位标签（第N页/第N张幻灯片）由本模块渲染，供提示词与导出复用，不编造来源。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .types import (
    MATERIAL_KINDS,
    REVIEW_ENGINE_VERSION,
    ChunkInput,
    MaterialGap,
    MaterialInput,
    MaterialSnapshot,
    ReviewChunk,
    ReviewError,
    ReviewErrorCode,
    SnapshotMaterial,
)

# 材料解析状态（B 侧 Resource.parse_status 与本快照口径的并集）
SNAPSHOT_PARSE_STATES = ("ready", "needs_ocr", "failed", "pending", "missing")

_SENSITIVE_KEY_RE = re.compile(
    r"(api[-_]?key|secret|token|cookie|auth|password|signature|signed|credential)", re.IGNORECASE
)
_URL_KEYS = ("base_url", "url", "endpoint", "host_url")
_TRUNCATION_MARKS = ("截断", "truncated", "超过", "省略")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_provider_config(provider: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """过滤快照中的非密钥配置：丢弃敏感键；URL 类值去掉查询串（可能含临时签名）。

    返回 (sanitized, warnings)。密钥存储由 B 负责；本函数是纵深防御，不能替代密钥保管。
    """
    warnings: list[str] = []

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if _SENSITIVE_KEY_RE.search(str(k)):
                    continue
                if str(k).lower() in _URL_KEYS and isinstance(v, str) and "?" in v:
                    out[str(k)] = v.split("?", 1)[0]
                    warnings.append(f"provider 配置项 {k} 的查询参数已从快照移除（可能含签名）")
                else:
                    out[str(k)] = _walk(v)
            return out
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(dict(provider or {})), warnings


def _coerce_chunk(raw: Any) -> ChunkInput:
    if isinstance(raw, ChunkInput):
        return raw
    if isinstance(raw, dict):
        return ChunkInput.from_dict(raw)
    # 兼容 learning.parse.types.ParsedChunk 与 ContentChunk 行对象
    locator = dict(getattr(raw, "locator", None) or {})
    heading_path = tuple(getattr(raw, "heading_path", None) or ())
    return ChunkInput(
        ordinal=int(getattr(raw, "ordinal", 0)),
        text=str(getattr(raw, "text", "")),
        chunk_id=getattr(raw, "chunk_id", None) or getattr(raw, "id", None),
        locator=locator,
        kind=str(getattr(raw, "kind", "body") or "body"),
        heading_path=heading_path,
    )


def _coerce_material(raw: Any) -> MaterialInput:
    if isinstance(raw, MaterialInput):
        return raw
    if isinstance(raw, dict):
        return MaterialInput.from_dict(raw)
    raise ReviewError(ReviewErrorCode.INVALID_INPUT, f"无法识别的材料输入：{type(raw).__name__}")


def _normalize_parse_status(status: str) -> str:
    if status == "parsed":
        return "ready"
    if status in SNAPSHOT_PARSE_STATES:
        return status
    return "pending"


def _material_gaps(material: SnapshotMaterial) -> list[MaterialGap]:
    gaps: list[MaterialGap] = []
    readable = [c for c in material.chunks if c.text.strip()]
    if material.parse_status == "needs_ocr":
        gaps.append(
            MaterialGap(
                code="NEEDS_OCR",
                severity="error",
                message=f"《{material.title}》疑似扫描件/图片型文档，未提取到正文，需先 OCR 或更换资料",
                material_id=material.material_id,
                title=material.title,
            )
        )
    elif material.parse_status == "failed":
        detail = material.limitations[0] if material.limitations else "解析失败"
        gaps.append(
            MaterialGap(
                code="MATERIAL_UNPARSED",
                severity="error",
                message=f"《{material.title}》解析失败（{detail}），不能作为出卷依据",
                material_id=material.material_id,
                title=material.title,
            )
        )
    elif material.parse_status == "pending":
        gaps.append(
            MaterialGap(
                code="MATERIAL_UNPARSED",
                severity="error",
                message=f"《{material.title}》尚未解析，请先完成解析再出卷",
                material_id=material.material_id,
                title=material.title,
            )
        )
    elif material.parse_status == "missing":
        gaps.append(
            MaterialGap(
                code="MISSING_ATTACHMENT",
                severity="error",
                message=f"《{material.title}》文件缺失，无法读取正文",
                material_id=material.material_id,
                title=material.title,
            )
        )
    elif not readable:
        gaps.append(
            MaterialGap(
                code="EMPTY_TEXT",
                severity="error",
                message=f"《{material.title}》已解析但正文为空（可能全部为图片/公式），不能据此出题",
                material_id=material.material_id,
                title=material.title,
            )
        )
    for limitation in material.limitations:
        if any(mark in limitation.lower() for mark in _TRUNCATION_MARKS):
            gaps.append(
                MaterialGap(
                    code="TRUNCATED",
                    severity="warning",
                    message=f"《{material.title}》存在截断/丢失范围：{limitation}",
                    material_id=material.material_id,
                    title=material.title,
                )
            )
    if material.kind == "attachment" and not material.linked_assignment_id:
        gaps.append(
            MaterialGap(
                code="UNLINKED_ATTACHMENT",
                severity="warning",
                message=f"附件《{material.title}》未标注所属作业，上下文关联可能不完整",
                material_id=material.material_id,
                title=material.title,
            )
        )
    return gaps


def build_snapshot(
    course_id: str,
    course_title: str,
    materials: list[Any],
    notes: str = "",
    provider: dict[str, Any] | None = None,
) -> MaterialSnapshot:
    """归一化材料（dict / MaterialInput / 兼容 ParsedChunk 的片段）→ 冻结快照。

    - parse_status 与片段缺口的判定在本层完成；资料不足如实记入 gaps（error 级会阻止出卷）。
    - 备注原文逐字保存（不 strip、不摘要）；长度上限在 build_generation_request 强制。
    """
    if not course_id:
        raise ReviewError(ReviewErrorCode.INVALID_INPUT, "course_id 不能为空")

    snapshot = MaterialSnapshot(course_id=course_id, course_title=course_title, notes=notes)
    seen_chunk_ids: dict[str, str] = {}
    sanitized, provider_warnings = sanitize_provider_config(provider)
    snapshot.provider = sanitized
    snapshot.warnings.extend(provider_warnings)

    for raw in materials:
        item = _coerce_material(raw)
        kind = item.kind if item.kind in MATERIAL_KINDS else None
        if kind is None:
            raise ReviewError(
                ReviewErrorCode.INVALID_INPUT,
                f"未知材料类型：{item.kind!r}（应为 {'/'.join(MATERIAL_KINDS)}）",
                details={"material_id": item.material_id},
            )
        chunks: list[ReviewChunk] = []
        for ci in item.chunks:
            chunk = ci if isinstance(ci, ChunkInput) else _coerce_chunk(ci)
            chunk_id = chunk.chunk_id or f"{item.material_id}:c{chunk.ordinal:04d}"
            if chunk_id in seen_chunk_ids:
                raise ReviewError(
                    ReviewErrorCode.INVALID_INPUT,
                    f"片段 id 重复：{chunk_id}（材料 {seen_chunk_ids[chunk_id]} 与 {item.material_id}）",
                    details={"chunk_id": chunk_id},
                )
            seen_chunk_ids[chunk_id] = item.material_id
            chunks.append(
                ReviewChunk(
                    chunk_id=chunk_id,
                    material_id=item.material_id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    locator=dict(chunk.locator or {}),
                    kind=chunk.kind,
                    heading_path=tuple(chunk.heading_path or ()),
                )
            )
        material = SnapshotMaterial(
            material_id=item.material_id,
            kind=kind,
            title=item.title,
            version=item.version,
            format=item.format,
            parse_status=_normalize_parse_status(item.parse_status),
            limitations=list(item.limitations or []),
            chunks=chunks,
            linked_assignment_id=item.linked_assignment_id,
            updated_at=item.updated_at,
        )
        if kind == "resource":
            snapshot.materials.append(material)
        elif kind == "assignment":
            snapshot.assignments.append(material)
        else:
            snapshot.attachments.append(material)
        snapshot.gaps.extend(_material_gaps(material))

    if not snapshot.all_materials():
        snapshot.gaps.append(
            MaterialGap(
                code="NO_MATERIALS",
                severity="error",
                message="未提供任何课件/作业/附件，无法出卷",
            )
        )

    snapshot.fingerprint = material_fingerprint(snapshot)
    return snapshot


def material_fingerprint(snapshot: MaterialSnapshot, constraints: dict[str, Any] | None = None) -> str:
    """材料+备注+生成配置+约束+引擎版本的确定性指纹（sha256 十六进制）。

    约束变化（题数/难度/章节等）会改变指纹 → B 可据此判定“新修订”；备注变化必然改变指纹。
    """
    payload: dict[str, Any] = {
        "engine_version": REVIEW_ENGINE_VERSION,
        "course_id": snapshot.course_id,
        "notes_sha256": sha256_text(snapshot.notes),
        "notes_length": len(snapshot.notes),
        "provider": snapshot.provider,
        "materials": [
            {
                "material_id": m.material_id,
                "kind": m.kind,
                "version": m.version,
                "parse_status": m.parse_status,
                "chunks": [
                    {
                        "chunk_id": c.chunk_id,
                        "ordinal": c.ordinal,
                        "text_sha256": sha256_text(c.text),
                        "locator": c.locator,
                        "heading_path": list(c.heading_path),
                    }
                    for c in m.chunks
                ],
            }
            for m in snapshot.all_materials()
        ],
    }
    if constraints is not None:
        payload["constraints"] = constraints
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def locator_label(locator: dict[str, Any] | None, ordinal: int) -> str:
    """定位 → 可读中文标签（不编造：无定位信息时退回片段序号）。"""
    loc = dict(locator or {})
    kind = loc.get("kind")
    if kind == "page" and loc.get("page"):
        return f"第{loc['page']}页"
    if kind == "slide" and loc.get("slide"):
        return f"第{loc['slide']}张幻灯片"
    if kind == "timestamp":
        start = loc.get("start")
        if start is not None:
            return f"时间轴 {start}"
    if kind == "text" and loc.get("start_offset") is not None:
        return f"字符偏移 {loc['start_offset']}-{loc.get('end_offset')}"
    return f"片段{ordinal}"


def chunk_label(snapshot: MaterialSnapshot, chunk: ReviewChunk) -> str:
    """片段 → 引用标签：课件标注页码/幻灯片，作业/附件标注所属材料。"""
    material = next((m for m in snapshot.all_materials() if m.material_id == chunk.material_id), None)
    title = material.title if material else chunk.material_id
    kind = material.kind if material else "resource"
    prefix = {"resource": "课件", "assignment": "作业", "attachment": "附件"}.get(kind, "材料")
    return f"{prefix}《{title}》{locator_label(chunk.locator, chunk.ordinal)}"


def snapshot_overview(snapshot: MaterialSnapshot) -> dict[str, Any]:
    """给 B 的 GET /courses/{id}/review-materials：可浏览的材料与准备状态。"""

    def _entry(m: SnapshotMaterial) -> dict[str, Any]:
        locator_kinds = sorted({str(c.locator.get("kind")) for c in m.chunks if c.locator.get("kind")})
        return {
            "material_id": m.material_id,
            "kind": m.kind,
            "title": m.title,
            "version": m.version,
            "format": m.format,
            "parse_status": m.parse_status,
            "chunk_count": len(m.chunks),
            "readable_chars": m.readable_chars,
            "locator_kinds": locator_kinds,
            "limitations": list(m.limitations),
            "linked_assignment_id": m.linked_assignment_id,
        }

    errors = [g.to_dict() for g in snapshot.gaps if g.severity == "error"]
    return {
        "course_id": snapshot.course_id,
        "course_title": snapshot.course_title,
        "fingerprint": snapshot.fingerprint,
        "ready": snapshot.has_readable_text() and not errors,
        "materials": [_entry(m) for m in snapshot.materials],
        "assignments": [_entry(m) for m in snapshot.assignments],
        "attachments": [_entry(m) for m in snapshot.attachments],
        "notes_chars": len(snapshot.notes),
        "has_notes": bool(snapshot.notes),
        "gaps": [g.to_dict() for g in snapshot.gaps],
        "warnings": list(snapshot.warnings),
    }
