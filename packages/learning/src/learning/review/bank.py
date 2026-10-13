"""网络题库选题：A 的候选题 → 满足蓝图的选题 + 缺题配额与原因。

- 只挑与蓝图知识点匹配且题型对应的候选；没有模型时不替未知题猜答案。
- 缺答案的候选不当作完整题：记入 deficits（原因 answer_missing），交 B/C 决定补题或切换模式。
- 来源保留为网络原题（origin=bank）；如候选已标明改编，保留改编标记与说明。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .types import (
    QUESTION_TYPES,
    BankCandidate,
    BankSelection,
    Deficit,
    KpPlan,
    PaperBlueprint,
    RejectedCandidate,
    ReviewError,
    ReviewErrorCode,
    SelectedQuestion,
    normalize_difficulty,
)

_NEAR_DUPLICATE_RATIO = 0.92
_LOW_CONFIDENCE = 0.5
_QUOTA_TYPES = set(QUESTION_TYPES)


def _norm_stem(text: str) -> str:
    return re.sub(r"[\s\W]+", "", (text or "").casefold(), flags=re.UNICODE)


def _coerce_candidate(raw: Any) -> BankCandidate:
    if isinstance(raw, BankCandidate):
        return raw
    if isinstance(raw, dict):
        return BankCandidate.from_dict(raw)
    raise ReviewError(ReviewErrorCode.INVALID_INPUT, f"无法识别的候选题输入：{type(raw).__name__}")


def _contains(text_cf: str, term: str) -> bool:
    term_cf = term.casefold()
    if not term_cf:
        return False
    if re.search(r"[a-z]", term_cf):
        return re.search(rf"(?<![a-z0-9_]){re.escape(term_cf)}(?![a-z0-9_])", text_cf) is not None
    return term_cf in text_cf


def _score_candidate(candidate: BankCandidate, kp: KpPlan) -> float:
    """候选与知识点计划的匹配度（0 = 不匹配；≥2 = 命中 kp_hints 或题干含知识点名）。"""
    stem_cf = candidate.stem.casefold()
    score = 0.0
    kp_norm = re.sub(r"[\s\W]+", "", kp.name.casefold(), flags=re.UNICODE)
    for hint in candidate.kp_hints:
        hint_norm = re.sub(r"[\s\W]+", "", hint.casefold(), flags=re.UNICODE)
        if hint_norm and (hint_norm == kp_norm or kp_norm in hint_norm or hint_norm in kp_norm):
            score = max(score, 3.0)
    if _contains(stem_cf, kp.name):
        score = max(score, 2.0)
    return score


def _answer_status(candidate: BankCandidate) -> str:
    answer = candidate.answer
    empty = answer is None or (isinstance(answer, str) and not answer.strip()) or (
        isinstance(answer, list) and not answer
    )
    if empty:
        return "answer_missing"
    explanation = (candidate.explanation or "").strip()
    if not explanation:
        return "explanation_missing"
    return "complete"


def select_bank_questions(
    blueprint: PaperBlueprint,
    candidates: list[Any],
) -> BankSelection:
    """按蓝图从候选池选题：匹配 → 去重 → 配额；缺题给出可读原因（不假装搜到题）。"""
    selection = BankSelection()
    kp_by_id = {plan.kp_id: plan for plan in blueprint.kp_plan}
    quota = {q.qtype: q.count for q in blueprint.quotas}

    # 1) 归一化 + 基础校验
    pool: list[tuple[BankCandidate, float, list[str]]] = []
    seen_ids: set[str] = set()
    type_matched_counts: dict[str, int] = {t: 0 for t in quota}
    type_any_counts: dict[str, int] = {t: 0 for t in quota}
    for raw in candidates:
        candidate = _coerce_candidate(raw)
        if not candidate.candidate_id or candidate.candidate_id in seen_ids:
            selection.rejected.append(
                RejectedCandidate(
                    candidate_id=candidate.candidate_id or "(无 ID)",
                    reason="INVALID",
                    detail="候选 ID 缺失或重复",
                )
            )
            continue
        seen_ids.add(candidate.candidate_id)
        if not candidate.stem or not candidate.stem.strip():
            selection.rejected.append(
                RejectedCandidate(candidate_id=candidate.candidate_id, reason="INVALID", detail="题干为空")
            )
            continue
        if candidate.qtype not in _QUOTA_TYPES:
            selection.rejected.append(
                RejectedCandidate(
                    candidate_id=candidate.candidate_id,
                    reason="UNSUPPORTED_TYPE",
                    detail=f"题型 {candidate.qtype!r} 不在支持范围（{'、'.join(QUESTION_TYPES)}）",
                )
            )
            continue
        if candidate.qtype not in quota:
            selection.rejected.append(
                RejectedCandidate(
                    candidate_id=candidate.candidate_id,
                    reason="NO_QUOTA",
                    detail=f"蓝图未要求 {candidate.qtype} 题型",
                )
            )
            continue
        type_any_counts[candidate.qtype] = type_any_counts.get(candidate.qtype, 0) + 1
        best_kp: KpPlan | None = None
        best_score = 0.0
        matched: list[tuple[float, KpPlan]] = []
        for plan in kp_by_id.values():
            score = _score_candidate(candidate, plan)
            if score > 0:
                matched.append((score, plan))
            if score > best_score:
                best_score, best_kp = score, plan
        matched.sort(key=lambda item: (-item[0], item[1].kp_id))
        if best_kp is None or best_score < 1.0:
            selection.rejected.append(
                RejectedCandidate(
                    candidate_id=candidate.candidate_id,
                    reason="NO_MATCH",
                    detail="未匹配到蓝图知识点",
                )
            )
            continue
        type_matched_counts[candidate.qtype] = type_matched_counts.get(candidate.qtype, 0) + 1
        pool.append((candidate, best_score, [plan.kp_id for _s, plan in matched[:4]]))

    # 2) 去重：同一候选题只保留匹配度最高的一条（先按分数稳定排序再扫）
    pool.sort(key=lambda item: (-item[1], -(item[0].extraction_confidence or 0), item[0].candidate_id))
    kept: list[tuple[BankCandidate, float, list[str]]] = []
    stems: list[str] = []
    for candidate, score, kp_ids in pool:
        stem_norm = _norm_stem(candidate.stem)
        duplicate_of: str | None = None
        for other_norm, other_id in stems:
            if stem_norm == other_norm or difflib.SequenceMatcher(
                None, stem_norm, other_norm
            ).ratio() >= _NEAR_DUPLICATE_RATIO:
                duplicate_of = other_id
                break
        if duplicate_of is not None:
            selection.rejected.append(
                RejectedCandidate(
                    candidate_id=candidate.candidate_id,
                    reason="DUPLICATE",
                    detail=f"与候选 {duplicate_of} 题干重复",
                )
            )
            continue
        kept.append((candidate, score, kp_ids))
        stems.append((stem_norm, candidate.candidate_id))

    # 3) 配额：按题型选入；先按知识点预算分散（pass1），不足再放开（pass2）
    accepted_ids: set[str] = set()
    per_type_count: dict[str, int] = {t: 0 for t in quota}
    per_kp_type: dict[tuple[str, str], int] = {}

    def _accept(candidate: BankCandidate, score: float, kp_ids: list[str], respect_kp_budget: bool) -> bool:
        qtype = candidate.qtype
        if per_type_count[qtype] >= quota[qtype]:
            return False
        if respect_kp_budget and kp_ids:
            primary = kp_ids[0]
            budget = kp_by_id[primary].question_budget if primary in kp_by_id else 0
            if per_kp_type.get((qtype, primary), 0) >= budget:
                return False
        if _answer_status(candidate) == "answer_missing":
            return False  # 缺答案不作为完整题（记入缺口，由 B/C 决定补题或切换模式）
        if candidate.usage_note:
            selection.warnings.append(
                f"候选 {candidate.candidate_id} 标注使用限制：{candidate.usage_note}"
            )
        if candidate.extraction_confidence is not None and candidate.extraction_confidence < _LOW_CONFIDENCE:
            selection.warnings.append(
                f"候选 {candidate.candidate_id} 提取置信度较低（{candidate.extraction_confidence}），建议人工核对"
            )
        status = _answer_status(candidate)
        reasons = [f"与知识点《{kp_by_id[kp].name}》匹配" for kp in kp_ids[:1] if kp in kp_by_id]
        if candidate.adapted:
            reasons.append("候选为改编题（保留改编标记）")
        selection.selected.append(
            SelectedQuestion(
                candidate=candidate,
                match_score=score,
                matched_kp_ids=kp_ids,
                answer_status=status,
                reasons=reasons,
            )
        )
        return True

    for candidate, score, kp_ids in kept:  # pass1：尊重知识点预算
        if _accept(candidate, score, kp_ids, respect_kp_budget=True):
            accepted_ids.add(candidate.candidate_id)
            per_type_count[candidate.qtype] += 1
            if kp_ids:
                key = (candidate.qtype, kp_ids[0])
                per_kp_type[key] = per_kp_type.get(key, 0) + 1
    for candidate, score, kp_ids in kept:  # pass2：放开知识点预算，优先凑满配额
        if candidate.candidate_id in accepted_ids:
            continue
        if _accept(candidate, score, kp_ids, respect_kp_budget=False):
            accepted_ids.add(candidate.candidate_id)
            per_type_count[candidate.qtype] += 1

    for candidate, _score, _kp_ids in kept:
        if candidate.candidate_id not in accepted_ids:
            selection.rejected.append(
                RejectedCandidate(
                    candidate_id=candidate.candidate_id,
                    reason="QUOTA_FULLED",
                    detail="配额已满或缺少答案，未选入本轮试卷",
                )
            )

    # 4) 缺题配额与原因（按题型汇总；不虚构补足）
    for qtype in QUESTION_TYPES:
        if qtype not in quota:
            continue
        needed = quota[qtype]
        have = per_type_count[qtype]
        if have >= needed:
            continue
        if type_any_counts.get(qtype, 0) == 0:
            reason, message = "no_candidates", f"未检索到 {qtype} 候选题，可切换出题模式或调整蓝图"
        elif type_matched_counts.get(qtype, 0) == 0:
            reason, message = "no_knowledge_match", f"{qtype} 候选均未匹配蓝图知识点，可修改检索词或增补材料"
        elif any(
            s.candidate.qtype == qtype and s.answer_status == "answer_missing" for s in selection.selected
        ):
            reason, message = "answer_missing", f"{qtype} 候选缺少答案，已如实标记，需补题或改用 AI/混合模式"
        else:
            reason, message = (
                "insufficient",
                f"{qtype} 完整候选不足（需要 {needed}，可用 {have}），可减少题数或切换模式",
            )
        selection.deficits.append(
            Deficit(
                qtype=qtype,
                needed=needed,
                available_complete=have,
                reason=reason,
                message=message,
            )
        )
        selection.warnings.append(message)
    return selection


def build_bank_questions(selection: BankSelection, blueprint: PaperBlueprint) -> list[Any]:
    """选题 → ReviewQuestion 草稿（题目 id/分值在 validate 阶段统一分配）。"""
    from .types import QuestionSourceRef, ReviewQuestion

    kp_names = {plan.kp_id: plan.name for plan in blueprint.kp_plan}
    questions: list[ReviewQuestion] = []
    for item in selection.selected:
        candidate = item.candidate
        source_refs = []
        if candidate.source is not None:
            label = candidate.source.title or candidate.source.site or "网络题目"
            if candidate.source.locator:
                label = f"{label}（{candidate.source.locator}）"
            source_refs.append(
                QuestionSourceRef(
                    kind="bank",
                    label=label,
                    url=candidate.source.url,
                    site=candidate.source.site,
                )
            )
        warnings: list[str] = []
        if item.answer_status == "explanation_missing":
            warnings.append("网络原题缺解析，建议人工核对或改用混合模式补解析")
        if candidate.adapted:
            warnings.append(f"改编题：{candidate.adaptation_note or '原出处见来源链接'}")
        questions.append(
            ReviewQuestion(
                question_id="",  # validate 统一编号
                qtype=candidate.qtype,
                stem=candidate.stem,
                options=[dict(o) for o in candidate.options],
                answer=candidate.answer,
                explanation=candidate.explanation or "",
                scoring=[],
                kp_ids=[kp for kp in item.matched_kp_ids if kp in kp_names],
                kp_names=[kp_names[kp] for kp in item.matched_kp_ids if kp in kp_names],
                difficulty=normalize_difficulty(candidate.difficulty) or "medium",
                points=0.0,  # validate 统一按蓝图分配
                origin="bank",
                source_refs=source_refs,
                bank_candidate_id=candidate.candidate_id,
                adapted=candidate.adapted,
                adaptation_note=candidate.adaptation_note,
                warnings=warnings,
            )
        )
    return questions
