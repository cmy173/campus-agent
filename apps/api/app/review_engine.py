"""复习卷生成适配层（CR008 #47）：契约结构 ←→ D 引擎（learning.review）数据映射。

- 输入侧（B→D）：把受理时冻结的快照行（items/notes/constraints）转成
  `build_snapshot` 材料输入与 `BlueprintConstraints`；分片正文由 worker 从
  `content_chunks` 复读并核对（拼接口径必须与冻结的 item.text 一致，防受理后漂移）。
- 输出侧（D→B）：把 D 的蓝图/候选/校验报告转成契约结构
  （paper-blueprint / review-question / Warning / Summary）。
- 题型映射：single_choice→choice、proof→short_answer（别名归一）；**multiple_choice
  引擎不支持——受理前如实 400，不静默丢弃**（契约类型表大于引擎支持面）。
- 分值口径：用户显式 points_each 由 B 在适配层落实（D 蓝图只按总分分配），
  「用户指定值不被引擎改变」由本层保证。
- 纯函数、零网络零 DB；密钥与本地路径不出现在任何返回值中。
"""
from __future__ import annotations

from typing import Any

# 契约题型 → D 引擎题型（D QUESTION_TYPES：choice/true_false/fill_blank/calculation/short_answer）
CONTRACT_TO_ENGINE_TYPE = {
    "single_choice": "choice",
    "true_false": "true_false",
    "fill_blank": "fill_blank",
    "calculation": "calculation",
    "short_answer": "short_answer",
    "proof": "short_answer",
}
ENGINE_TO_CONTRACT_TYPE = {
    "choice": "single_choice",
    "true_false": "true_false",
    "fill_blank": "fill_blank",
    "calculation": "calculation",
    "short_answer": "short_answer",
}
# D 题型 → A 题库题型过滤名（types.py CANONICAL_TYPES）
ENGINE_TO_QUESTIONBANK_TYPE = {
    "choice": "choice",
    "true_false": "judgment",
    "fill_blank": "fill_blank",
    "calculation": "calculation",
    "short_answer": "short_answer",
}
UNSUPPORTED_CONTRACT_TYPES = ("multiple_choice",)

MISSING_REASON_LABELS = {
    "not_parsed": "尚未解析",
    "parse_failed": "解析失败",
    "type_skipped": "该类型不解析",
    "no_text": "无可读文本",
    "chunk_missing": "部分片段缺失",
    "attachment_absent": "作业附件缺失",
    "not_accessible": "平台无下载权限",
}

_ANSWER_EMPTY = "（未提供答案）"
_EXPLANATION_EMPTY = "（未提供解析）"
_NO_SOURCE = "（未给出可读出处）"


def unsupported_contract_types(constraints: dict[str, Any]) -> list[str]:
    return [
        str(q.get("type"))
        for q in constraints.get("question_types") or []
        if str(q.get("type")) in UNSUPPORTED_CONTRACT_TYPES
    ]


def engine_type_counts(constraints: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for quota in constraints.get("question_types") or []:
        engine = CONTRACT_TO_ENGINE_TYPE.get(str(quota.get("type")))
        if engine is None:
            continue
        counts[engine] = counts.get(engine, 0) + int(quota.get("count") or 0)
    return counts


def effective_points(constraints: dict[str, Any]) -> dict[str, float]:
    """用户显式 points_each（契约题型 → 分值）；未指定的题型不在此表（按引擎分配）。"""
    out: dict[str, float] = {}
    for quota in constraints.get("question_types") or []:
        if quota.get("points_each") is not None:
            out[str(quota.get("type"))] = float(quota["points_each"])
    return out


def engine_difficulty(constraints: dict[str, Any]) -> dict[str, float] | None:
    level = constraints.get("difficulty")
    if level in ("easy", "medium", "hard"):
        return {level: 1.0}
    return None  # mixed / null → 引擎按材料推断


def engine_constraints(constraints: dict[str, Any], mode: str, title: str | None) -> dict[str, Any]:
    """契约 Constraints + 请求级 mode → BlueprintConstraints.from_dict 输入（不含题型分值的分摊口径）。

    mode 是请求顶层字段（不在 constraints JSON 里）：bank/hybrid 决定 D 是否产出检索请求，必须显式传入。
    """
    return {
        "mode": mode,
        "types": engine_type_counts(constraints),
        "total_points": constraints.get("total_points"),
        "total_minutes": constraints.get("duration_minutes"),
        "difficulty": engine_difficulty(constraints),
        "sections": [],
        "title": title,
    }


# ────────────────────────── 输入侧：快照 → D ──────────────────────────


def attachment_material_id(assignment_id: str, resource_id: str) -> str:
    return f"att::{assignment_id}::{resource_id}"


def split_material_id(material_id: str) -> tuple[str | None, str]:
    """att::asg::res → (asg, res)；普通 id → (None, id)。"""
    if material_id.startswith("att::"):
        parts = material_id.split("::")
        if len(parts) == 3:
            return parts[1], parts[2]
    return None, material_id


def material_inputs(
    items: list[dict[str, Any]],
    chunk_rows: dict[str, Any],
    formats: dict[str, str | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """快照 items → D build_snapshot 材料输入；返回 (materials, drift 清单)。

    drift（分片缺失或正文与冻结文本不一致）必须由调用方按失败处理，不得静默发送漂移内容。
    """
    materials: list[dict[str, Any]] = []
    drift: list[str] = []
    for item in items:
        kind = item.get("kind")
        title = str(item.get("title") or "")
        if kind == "assignment_text":
            text = str(item.get("text") or "")
            if not text:
                drift.append(title)
                continue
            materials.append({
                "material_id": item.get("assignment_id"),
                "kind": "assignment",
                "title": title,
                "version": "1",
                "format": "text",
                "parse_status": "ready",
                "chunks": [{
                    "chunk_id": f"asg_{item.get('assignment_id')}_text",
                    "ordinal": 0,
                    "text": text,
                    "locator": {"kind": "text", "start_offset": 0, "end_offset": len(text)},
                }],
            })
            continue
        # resource_text / attachment_text：逐片段复读并核对
        chunk_ids = [str(cid) for cid in item.get("chunk_ids") or []]
        texts: list[str] = []
        chunks: list[dict[str, Any]] = []
        for cid in chunk_ids:
            row = chunk_rows.get(cid)
            if row is None:
                drift.append(title)
                texts = []
                break
            texts.append(row.text)
            chunks.append({
                "chunk_id": cid,
                "ordinal": int(row.ordinal),
                "text": row.text,
                "locator": dict(row.locator or {}),
            })
        if not texts and chunk_ids:
            continue  # 已计入 drift
        joined = "\n".join(texts)
        if joined != str(item.get("text") or ""):
            drift.append(title)
            continue
        resource_id = str(item.get("resource_id"))
        if kind == "attachment_text":
            assignment_id = item.get("assignment_id")
            material_id = attachment_material_id(str(assignment_id), resource_id)
            d_kind = "attachment"
        else:
            material_id = resource_id
            d_kind = "resource"
        materials.append({
            "material_id": material_id,
            "kind": d_kind,
            "title": title,
            "version": str(item.get("version")) if item.get("version") is not None else "1",
            "format": formats.get(resource_id) or "text",
            "parse_status": "ready",
            "chunks": chunks,
        })
    return materials, drift


def provider_for_engine(provider_block: dict[str, Any] | None) -> dict[str, Any] | None:
    """仅传非密钥字段给引擎（引擎内部还会再过滤；密钥保管在 B）。"""
    if not provider_block or not provider_block.get("config_id"):
        return None
    return {
        "provider_id": provider_block.get("config_id"),
        "protocol": provider_block.get("protocol"),
        "model": provider_block.get("model"),
        "base_url": provider_block.get("base_url"),
    }


def provider_snapshot(provider_block: dict[str, Any] | None) -> dict[str, Any]:
    """契约 ProviderSnapshot（5 字段，不含 host/has_api_key 等预检展示字段）。"""
    block = provider_block or {}
    return {
        "config_id": block.get("config_id"),
        "name": block.get("name"),
        "protocol": block.get("protocol"),
        "base_url": block.get("base_url"),
        "model": block.get("model"),
    }


def locator_text(locator: dict[str, Any] | None) -> str | None:
    """content-chunk Locator → 可读定位（幻灯片/页/字符区间/时间戳）。"""
    if not locator:
        return None
    kind = locator.get("kind")
    if kind == "slide" and locator.get("slide") is not None:
        return f"第{locator['slide']}张幻灯片"
    if kind == "page" and locator.get("page") is not None:
        return f"第{locator['page']}页"
    if kind == "text":
        start, end = locator.get("start_offset"), locator.get("end_offset")
        if start is not None and end is not None:
            return f"字符 {start}–{end}"
    if kind in ("timestamp", "time") and locator.get("start_seconds") is not None:
        return f"{locator['start_seconds']}s 起"
    return None


def chunk_locator_map(items: list[dict[str, Any]]) -> dict[str, str | None]:
    """chunk_id → 可读定位（供题目来源 locator 回填）。"""
    out: dict[str, str | None] = {}
    for item in items:
        for entry in item.get("locators") or []:
            out[str(entry.get("chunk_id"))] = locator_text(entry.get("locator"))
    return out


# ────────────────────────── 输出侧：D 蓝图 → 契约 ──────────────────────────


def _largest_remainder(total: int, weights: list[float]) -> list[int]:
    """按权重把 total 分配到各槽（最大余数法，确定性：余数相同按序号）。"""
    if total <= 0 or not weights:
        return [0] * len(weights)
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * len(weights)
        weight_sum = float(len(weights))
    raw = [total * w / weight_sum for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def adapt_blueprint(
    blueprint: dict[str, Any],
    analysis: dict[str, Any],
    questions: list[dict[str, Any]],
    mode: str,
    missing: list[dict[str, Any]],
    notes_text: str,
) -> dict[str, Any]:
    """D PaperBlueprint + 实际题目 → 契约 paper-blueprint（type_quotas/difficulty_quota 与题目一致）。"""
    kp_by_id = {str(kp.get("kp_id")): kp for kp in analysis.get("knowledge_points") or []}

    # 章节序：D blueprint.sections 优先，补齐 kp_plan 中出现的节
    section_titles: list[str] = []
    for name in blueprint.get("sections") or []:
        if str(name) not in section_titles:
            section_titles.append(str(name))
    for plan in blueprint.get("kp_plan") or []:
        name = str(plan.get("section") or "")
        if name and name not in section_titles:
            section_titles.append(name)
    if not section_titles:
        section_titles = ["综合复习"]

    # 按章节分组 kp（仅保留有证据的 kp，证据为空不硬编）
    # 契约 KnowledgePoint.weight ∈ [0,1]（相对权重）；D 的 kp_plan.weight 为原始分值 → 按全蓝图占比归一
    raw_weights: list[float] = []
    for plan in blueprint.get("kp_plan") or []:
        value = _as_float_or_none(plan.get("weight"))
        raw_weights.append(value or 0.0)
    weight_total = sum(raw_weights)
    section_kps: dict[str, list[dict[str, Any]]] = {name: [] for name in section_titles}
    for plan, raw_weight in zip(blueprint.get("kp_plan") or [], raw_weights):
        name = str(plan.get("section") or section_titles[0])
        analysis_kp = kp_by_id.get(str(plan.get("kp_id"))) or {}
        evidence = _evidence_list(analysis_kp, notes_text)
        if not evidence:
            continue
        weight = round(raw_weight / weight_total, 4) if weight_total > 0 and raw_weight > 0 else None
        section_kps.setdefault(name, []).append({
            "name": str(plan.get("name") or analysis_kp.get("name") or ""),
            "weight": weight,
            "evidence": evidence,
        })

    # 题型槽位按章节权重（kp 预算）分配
    quotas = blueprint.get("quotas") or []
    section_weights = [float(len(section_kps.get(name) or []) or 1) for name in section_titles]
    slots_by_section: dict[str, list[dict[str, Any]]] = {name: [] for name in section_titles}
    slot_no = 1
    for quota in quotas:
        engine_type = str(quota.get("qtype"))
        contract_type = ENGINE_TO_CONTRACT_TYPE.get(engine_type)
        count = int(quota.get("count") or 0)
        if contract_type is None or count <= 0:
            continue
        per_section = _largest_remainder(count, section_weights)
        points_each = max(1, int(round(float(quota.get("points_each") or 1))))
        for name, part in zip(section_titles, per_section):
            if part <= 0:
                continue
            slots_by_section[name].append({
                "slot_id": f"slot-{slot_no}",
                "type": contract_type,
                "count": part,
                "points_each": points_each,
                "difficulty": None,
                "source_plan": mode,
            })
            slot_no += 1

    # 实际题目口径（契约：type_quotas/难度分布与题目清单一致）
    type_counts: dict[str, int] = {}
    type_points: dict[str, int] = {}
    difficulty_counts = {"easy": 0, "medium": 0, "hard": 0}
    for question in questions:
        qtype = str(question.get("question_type"))
        points = int(question.get("points") or 1)
        type_counts[qtype] = type_counts.get(qtype, 0) + 1
        type_points[qtype] = type_points.get(qtype, 0) + points
        difficulty = str(question.get("difficulty") or "medium")
        if difficulty in difficulty_counts:
            difficulty_counts[difficulty] += 1
        else:
            difficulty_counts["medium"] += 1

    sections = [
        {
            "id": f"sec-{idx + 1}",
            "title": name,
            "knowledge_points": section_kps.get(name) or [],
            "question_slots": slots_by_section.get(name) or [],
        }
        for idx, name in enumerate(section_titles)
    ]
    gaps = _gap_list(blueprint.get("gaps") or [], missing)
    return {
        "sections": sections,
        "type_quotas": [
            {"type": qtype, "count": type_counts[qtype], "points_total": type_points[qtype]}
            for qtype in type_counts
        ],
        "difficulty_quota": difficulty_counts,
        "total_points": sum(type_points.values()),
        "estimated_minutes": max(1, int(blueprint.get("estimated_minutes") or 1)),
        "gaps": gaps,
    }


def _as_float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _evidence_list(analysis_kp: dict[str, Any], notes_text: str) -> list[dict[str, Any]]:
    notes_lines = notes_text.split("\n") if notes_text else []
    evidence: list[dict[str, Any]] = []
    for item in analysis_kp.get("evidence") or []:
        role = str(item.get("role") or "")
        material_id = item.get("material_id")
        if role == "courseware":
            _, resource_id = split_material_id(str(material_id or ""))
            evidence.append({
                "kind": "courseware",
                "resource_id": resource_id or None,
                "chunk_id": item.get("chunk_id"),
                "assignment_id": None,
                "note_ref": None,
            })
        elif role == "assignment":
            parent_id, resource_id = split_material_id(str(material_id or ""))
            evidence.append({
                "kind": "homework",
                "resource_id": resource_id if parent_id else None,
                "chunk_id": item.get("chunk_id") if parent_id else None,
                "assignment_id": parent_id or material_id,
                "note_ref": None,
            })
        elif role == "notes":
            line_no = item.get("notes_line")
            note_ref = None
            if isinstance(line_no, int) and 1 <= line_no <= len(notes_lines):
                note_ref = notes_lines[line_no - 1]
            elif line_no is not None:
                note_ref = f"备注第{line_no}行"
            evidence.append({
                "kind": "note",
                "resource_id": None,
                "chunk_id": None,
                "assignment_id": None,
                "note_ref": note_ref,
            })
    return evidence


def _gap_list(engine_gaps: list[Any], missing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for text in engine_gaps:
        message = text if isinstance(text, str) else str((text or {}).get("message") or text)
        gaps.append({"kind": "material_missing", "message": message, "knowledge_point": None})
    for item in missing:
        title = str(item.get("title") or "")
        reason = MISSING_REASON_LABELS.get(str(item.get("reason")), str(item.get("reason") or ""))
        gaps.append({
            "kind": "material_missing",
            "message": f"{title}：{reason}，未纳入本次出卷。",
            "knowledge_point": None,
        })
    return gaps


# ────────────────────────── 输出侧：D 题目/报告 → 契约 ──────────────────────────


def _stringify_answer(answer: Any) -> str:
    if isinstance(answer, bool):
        return "正确" if answer else "错误"
    if isinstance(answer, list):
        return "；".join(str(a) for a in answer)
    if answer is None:
        return ""
    return str(answer)


def _map_source_ref(ref: dict[str, Any], locators: dict[str, str | None]) -> dict[str, Any]:
    kind = str(ref.get("kind") or "")
    chunk_id = ref.get("chunk_id")
    if kind == "courseware":
        assignment_id, resource_id = split_material_id(str(ref.get("material_id") or ""))
        return {
            "kind": "resource",
            "resource_id": resource_id or None,
            "chunk_id": chunk_id,
            "assignment_id": assignment_id,
            "url": None,
            "site_name": None,
            "locator": locators.get(str(chunk_id)) if chunk_id else None,
        }
    if kind == "assignment":
        parent_id, resource_id = split_material_id(str(ref.get("material_id") or ""))
        return {
            "kind": "resource" if parent_id else "assignment",
            "resource_id": resource_id if parent_id else None,
            "chunk_id": chunk_id if parent_id else None,
            "assignment_id": parent_id or ref.get("material_id"),
            "url": None,
            "site_name": None,
            "locator": locators.get(str(chunk_id)) if parent_id and chunk_id else None,
        }
    if kind == "notes":
        line_no = ref.get("notes_line")
        return {
            "kind": "note",
            "resource_id": None,
            "chunk_id": None,
            "assignment_id": None,
            "url": None,
            "site_name": None,
            "locator": f"备注第{line_no}行" if line_no is not None else "备注",
        }
    # bank（网络候选）→ 契约 web
    return {
        "kind": "web",
        "resource_id": None,
        "chunk_id": None,
        "assignment_id": None,
        "url": ref.get("url"),
        "site_name": ref.get("site"),
        "locator": None,
    }


def adapt_questions(
    candidate: dict[str, Any],
    report: dict[str, Any],
    question_prefix: str,
    locators: dict[str, str | None],
    points_override: dict[str, float],
) -> list[dict[str, Any]]:
    """D PaperCandidate → 契约 review-question 列表（修复标记由 mark_repaired 回填）。"""
    issues_by_qid: dict[str, list[dict[str, Any]]] = {}
    for issue in report.get("issues") or []:
        qid = issue.get("question_id")
        if qid:
            issues_by_qid.setdefault(str(qid), []).append(issue)

    questions: list[dict[str, Any]] = []
    for idx, raw in enumerate(candidate.get("questions") or [], start=1):
        qid = str(raw.get("question_id") or f"rq_{idx:03d}")
        contract_type = ENGINE_TO_CONTRACT_TYPE.get(str(raw.get("qtype")))
        if contract_type is None:
            continue  # 引擎类型不可映射：跳过而不是冒充（引擎自身校验会拦）
        messages: list[str] = [str(w) for w in raw.get("warnings") or []]
        messages.extend(
            f"[{issue.get('code')}] {issue.get('message')}" for issue in issues_by_qid.get(qid, [])
        )
        answer = _stringify_answer(raw.get("answer"))
        explanation = str(raw.get("explanation") or "").strip()
        incomplete = False
        if not answer:
            answer = _ANSWER_EMPTY
            incomplete = True
            messages.append("未给出可解析的答案（如实标注 incomplete）")
        if not explanation:
            explanation = _EXPLANATION_EMPTY
            incomplete = True
            messages.append("未提供解析（如实标注 incomplete）")
        source_refs = [_map_source_ref(ref, locators) for ref in raw.get("source_refs") or []]
        if not source_refs:
            source_refs = [{
                "kind": "note", "resource_id": None, "chunk_id": None, "assignment_id": None,
                "url": None, "site_name": None, "locator": None,
            }]
            messages.append(_NO_SOURCE)
        if incomplete:
            status = "incomplete"
        elif raw.get("_repaired"):
            status = "repaired"
        elif messages:
            status = "low_confidence"
        else:
            status = "valid"
        origin_kind = str(raw.get("origin") or "ai")
        if origin_kind == "bank":
            origin = {
                "kind": "adapted_bank" if raw.get("adapted") else "bank",
                "candidate_id": raw.get("bank_candidate_id"),
                "note": raw.get("adaptation_note"),
            }
        else:
            origin = {"kind": "ai", "candidate_id": None, "note": None}
        override = points_override.get(contract_type)
        if override is not None:
            points = max(1, int(round(override)))
        else:
            points = max(1, int(round(float(raw.get("points") or 0))))
        options = [
            {"key": str(o.get("key")), "text": str(o.get("text"))}
            for o in raw.get("options") or []
            if o.get("key") is not None and o.get("text") is not None
        ]
        difficulty = str(raw.get("difficulty") or "medium")
        if difficulty not in ("easy", "medium", "hard"):
            difficulty = "medium"
        questions.append({
            "id": f"{question_prefix}{idx:02d}",
            "ordinal": idx,
            "question_type": contract_type,
            "stem": str(raw.get("stem") or "（题干缺失）"),
            "options": options,
            "answer": answer,
            "explanation": explanation,
            "grading_points": [str(p) for p in raw.get("scoring") or []],
            "knowledge_points": [str(k) for k in raw.get("kp_names") or []],
            "difficulty": difficulty,
            "points": points,
            "origin": origin,
            "source_refs": source_refs,
            "validation": {"status": status, "messages": messages},
            "_engine_question_id": qid,
        })
    return questions


def mark_repaired(before_candidate: dict[str, Any] | None, after_questions: list[dict[str, Any]]) -> list[str]:
    """对比修复前后逐题内容，返回内容变化的契约题目 ID 列表（用于 REPAIR_APPLIED 告警）。"""
    if not before_candidate:
        return []
    before_map = {
        str(q.get("question_id")): q for q in before_candidate.get("questions") or []
    }
    changed: list[str] = []
    for question in after_questions:
        engine_qid = question.get("_engine_question_id")
        old = before_map.get(str(engine_qid))
        if old is None:
            changed.append(question["id"])
            continue
        if _question_content(old) != _question_content(question):
            question["validation"]["status"] = "repaired"
            question["validation"]["messages"].append("经有限修复轮后内容有变化")
            changed.append(question["id"])
    return changed


def _question_content(question: dict[str, Any]) -> tuple:
    """题目内容指纹（忽略展示态字段；分值/ID 不参与，答案两侧同口径字符串化）。"""
    return (
        str(question.get("stem") or ""),
        _stringify_answer(question.get("answer")),
        str(question.get("explanation") or ""),
        tuple(str(o.get("text")) for o in question.get("options") or []),
        tuple(str(p) for p in question.get("scoring") or question.get("grading_points") or []),
    )


def finalize_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉内部字段，得到可直接持久化/外发的契约题目对象。"""
    out: list[dict[str, Any]] = []
    for question in questions:
        clean = {k: v for k, v in question.items() if not k.startswith("_")}
        out.append(clean)
    return out


# ────────────────────────── 告警与摘要 ──────────────────────────


def adapt_warnings(
    report: dict[str, Any],
    candidate: dict[str, Any] | None,
    questions: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    deficits: list[dict[str, Any]],
    selection_warnings: list[dict[str, Any]],
    repair_rounds: int,
    repaired_ids: list[str],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    qid_of = {str(q.get("_engine_question_id")): q.get("id") for q in questions}

    if missing:
        detail = "；".join(
            f"{m.get('title')}：{MISSING_REASON_LABELS.get(str(m.get('reason')), m.get('reason'))}"
            for m in missing
        )
        if len(missing) == 1:
            details: dict[str, Any] | None = {
                "missing_reason": missing[0].get("reason"),
                "resource_id": missing[0].get("resource_id"),
                "assignment_id": missing[0].get("assignment_id"),
            }
        else:
            details = {"items": [
                {
                    "missing_reason": m.get("reason"),
                    "resource_id": m.get("resource_id"),
                    "assignment_id": m.get("assignment_id"),
                }
                for m in missing
            ]}
        warnings.append({
            "code": "MATERIAL_GAP",
            "message": f"{len(missing)} 项所选材料无可用正文（{detail}），未纳入本次出卷。",
            "question_ids": [],
            "details": details,
        })

    for deficit in deficits:
        warnings.append({
            "code": "DEFICIT_UNFILLED",
            "message": str(deficit.get("message") or "题库补题缺口未填满"),
            "question_ids": [],
            "details": {
                "qtype": deficit.get("qtype"),
                "needed": deficit.get("needed"),
                "available_complete": deficit.get("available_complete"),
                "reason": deficit.get("reason"),
            },
        })

    for item in selection_warnings or []:
        warnings.append({
            "code": str(item.get("code") or "BANK_WARNING"),
            "message": str(item.get("message") or ""),
            "question_ids": [],
            "details": item.get("details"),
        })

    for issue in report.get("issues") or []:
        if issue.get("severity") != "warning":
            continue
        if str(issue.get("code")) == "DEFICIT_REMAINING":
            continue  # 已由 deficits 结构化表达
        qid = issue.get("question_id")
        warnings.append({
            "code": str(issue.get("code") or "ENGINE_WARNING"),
            "message": str(issue.get("message") or ""),
            "question_ids": [qid_of[str(qid)]] if qid and str(qid) in qid_of else [],
            "details": {"hint": issue.get("hint")} if issue.get("hint") else None,
        })

    if candidate:
        for text in candidate.get("warnings") or []:
            warnings.append({
                "code": "ENGINE_WARNING",
                "message": str(text),
                "question_ids": [],
                "details": None,
            })

    low_conf = [q["id"] for q in questions if q.get("validation", {}).get("status") == "low_confidence"]
    if low_conf:
        warnings.append({
            "code": "LOW_CONFIDENCE",
            "message": f"{len(low_conf)} 道题带有低置信度提示，建议人工复核后使用。",
            "question_ids": low_conf,
            "details": None,
        })

    incomplete = [q["id"] for q in questions if q.get("validation", {}).get("status") == "incomplete"]
    if incomplete:
        warnings.append({
            "code": "INCOMPLETE_QUESTIONS",
            "message": f"{len(incomplete)} 道题缺少答案或解析（如实标注 incomplete），不建议直接用于考试。",
            "question_ids": incomplete,
            "details": None,
        })

    if repair_rounds > 0:
        warnings.append({
            "code": "REPAIR_APPLIED",
            "message": f"经 {repair_rounds} 轮有限修复，{len(repaired_ids)} 道题内容有变化，请人工复核修复结果。"
            if repaired_ids else f"经 {repair_rounds} 轮有限修复。",
            "question_ids": repaired_ids,
            "details": {"repair_rounds": repair_rounds},
        })
    return warnings


def report_error_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    """D 报告 error 级问题 → job errors 条目（partial_failed 场景如实列出失败项）。"""
    entries: list[dict[str, Any]] = []
    for issue in report.get("issues") or []:
        if issue.get("severity") == "error":
            entry: dict[str, Any] = {
                "code": str(issue.get("code") or "ENGINE_ERROR"),
                "message": str(issue.get("message") or ""),
                "retryable": False,
            }
            details: dict[str, Any] = {}
            if issue.get("hint"):
                details["hint"] = issue["hint"]
            if issue.get("question_id"):
                details["question_id"] = issue["question_id"]
            if details:
                entry["details"] = details
            entries.append(entry)
    return entries


def adapt_summary(questions: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    total_points = 0
    for question in questions:
        qtype = str(question.get("question_type"))
        type_counts[qtype] = type_counts.get(qtype, 0) + 1
        difficulty = str(question.get("difficulty") or "medium")
        difficulty_counts[difficulty] = difficulty_counts.get(difficulty, 0) + 1
        origin_kind = str((question.get("origin") or {}).get("kind") or "ai")
        origin_counts[origin_kind] = origin_counts.get(origin_kind, 0) + 1
        total_points += int(question.get("points") or 0)
    minutes = 0
    for question in questions:
        minutes += {"single_choice": 2, "true_false": 1, "fill_blank": 2,
                    "calculation": 8, "short_answer": 10}.get(str(question.get("question_type")), 2)
    return {
        "question_count": len(questions),
        "total_points": total_points,
        "estimated_minutes": max(1, minutes) if questions else 0,
        "type_counts": type_counts,
        "difficulty_counts": difficulty_counts,
        "origin_counts": origin_counts,
    }


def question_id_prefix(paper_id: str) -> str:
    suffix = paper_id[4:] if paper_id.startswith("pap_") else paper_id
    return f"qst_{suffix}"


def build_bank_searches(
    blueprint: dict[str, Any], course_title: str, sources: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """D 蓝图 search_requests → A search_candidates 请求（按知识点合并题型，控制调用数）。"""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for request in blueprint.get("search_requests") or []:
        key = str(request.get("kp_id") or request.get("query") or "")
        bucket = merged.get(key)
        if bucket is None:
            bucket = {
                "query": str(request.get("query") or ""),
                "keywords": [],
                "course_hint": course_title,
                "question_types": [],
                "count": 0,
            }
            merged[key] = bucket
            order.append(key)
        bucket["count"] += int(request.get("count") or 1)
        a_type = ENGINE_TO_QUESTIONBANK_TYPE.get(str(request.get("qtype") or ""))
        if a_type and a_type not in bucket["question_types"]:
            bucket["question_types"].append(a_type)
    requests: list[dict[str, Any]] = []
    for key in order[: max(0, limit)]:
        bucket = merged[key]
        requests.append({
            "query": bucket["query"],
            "keywords": bucket["keywords"],
            "course_hint": bucket["course_hint"],
            "question_types": bucket["question_types"],
            "max_candidates": min(30, max(10, bucket["count"] * 4)),
            "sources": sources,
        })
    return requests
