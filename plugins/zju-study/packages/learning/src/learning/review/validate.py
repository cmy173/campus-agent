"""模型输出解析与试卷校验：受控 JSON 解析 → 结构/配额/总分/覆盖/重复/假引用校验 → 可解释报告。

- 受控兼容：直接 JSON、```json 代码块、首尾花括号截取三种形式；截断与非法 JSON 区分报错。
- 结构合法 ≠ 学术正确：报告固定标注 academic_correctness=not_verified；
  数学类仅做结构与选项一致性校验（v1 限制，见 checks_limitations）。
- 引用必须回到真实材料：chunk_id / notes_line / bank_candidate_id 只允许取给定集合，
  未知引用报 SOURCE_REF_FAKE 并给出可用 id 提示；不允许编造页码或来源。
- 修复：问题带修复 hint，由 build_repair_request 组装有限次修复请求；网络题出处不得被改写。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any

from .bank import build_bank_questions
from .blueprint import distribute_points
from .snapshot import chunk_label
from .types import (
    BANK_PROMPT_VERSION,
    GENERATE_PROMPT_VERSION,
    QUESTION_TYPE_LABELS,
    REVIEW_ENGINE_VERSION,
    BankSelection,
    MaterialSnapshot,
    PaperBlueprint,
    PaperCandidate,
    QuestionSourceRef,
    ReviewQuestion,
    ValidationCode as VC,
    ValidationIssue,
    ValidationOutcome,
    ValidationReport,
    normalize_difficulty,
    normalize_question_type,
)

_MAX_ISSUES = 300
_MAX_RAW_QUESTIONS = 200
_NEAR_DUPLICATE_RATIO = 0.92
_BLANK_RE = re.compile(r"_{2,}")
_PAREN_BLANK_RE = re.compile(r"[（(]\s*[）)]")
_TRUE_WORDS = {"true": True, "t": True, "对": True, "正确": True, "是": True, "√": True}
_FALSE_WORDS = {"false": False, "f": False, "错": False, "错误": False, "否": False, "×": False, "x": False}
_PARSE_LEVEL_CODES = {VC.OUTPUT_EMPTY, VC.OUTPUT_NOT_JSON, VC.OUTPUT_TRUNCATED, VC.SCHEMA_INVALID}

_CHECK_LIMITATIONS = [
    "校验为结构级（schema/题型/选项/答案对应/配额/总分/覆盖/重复/来源），未验证答案的学术正确性；"
    "academic_correctness=not_verified",
    "数学类题目未做数值代入与单位复核（v1 限制，仅检查结构与选项一致性）",
    "难度比例与预计用时为提示性约束，未逐题强制",
]


class _Issues:
    """问题收集器：按上限截断，避免坏输出产生海量问题。"""

    def __init__(self) -> None:
        self.items: list[ValidationIssue] = []
        self.overflow = False

    def add(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        hint: str = "",
        question_id: str | None = None,
        question_index: int | None = None,
        kp_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if len(self.items) >= _MAX_ISSUES:
            if not self.overflow:
                self.overflow = True
                self.items.append(
                    ValidationIssue(
                        code=VC.SCHEMA_INVALID,
                        severity="warning",
                        message=f"问题过多，仅列前 {_MAX_ISSUES} 条",
                    )
                )
            return
        self.items.append(
            ValidationIssue(
                code=code,
                severity=severity,
                message=message,
                hint=hint,
                question_id=question_id,
                question_index=question_index,
                kp_id=kp_id,
                details=details or {},
            )
        )


# ---------------------------------------------------------------------------
# 受控 JSON 解析（直接 JSON / 代码围栏 / 首尾花括号）
# ---------------------------------------------------------------------------


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def parse_model_output(raw_output: str | None) -> tuple[dict[str, Any] | None, list[ValidationIssue], bool]:
    """受控解析模型输出。返回 (数据, 问题, 是否疑似截断)。"""
    issues = _Issues()
    if raw_output is None or not str(raw_output).strip():
        issues.add(VC.OUTPUT_EMPTY, "error", "模型输出为空", hint="请重试或检查模型服务")
        return None, issues.items, False

    text = str(raw_output).strip()
    data = _try_json(text)
    if data is None:
        for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL):
            data = _try_json(block.strip())
            if data is not None:
                break
    if data is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            data = _try_json(text[start : end + 1])
    if data is None:
        truncated = text.count("{") > text.count("}") or (
            text.startswith(("{", "```")) and not text.rstrip().endswith(("}", "```"))
        )
        if truncated:
            issues.add(
                VC.OUTPUT_TRUNCATED,
                "error",
                "模型输出疑似被截断（JSON 不完整）",
                hint="请缩短单题篇幅/解析长度后重试，或提高输出上限；修复请求也应提醒模型压缩篇幅",
            )
        else:
            issues.add(
                VC.OUTPUT_NOT_JSON,
                "error",
                "模型输出不是合法 JSON",
                hint="仅返回 JSON（可直接输出或放在 ```json 代码块中），不要附加解释文字",
            )
        return None, issues.items, truncated

    if not isinstance(data, dict):
        issues.add(VC.SCHEMA_INVALID, "error", "模型输出顶层不是 JSON 对象", hint="返回 {\"title\", \"questions\"} 结构")
        return None, issues.items, False
    return data, issues.items, False


# ---------------------------------------------------------------------------
# 逐题归一化与引用解析
# ---------------------------------------------------------------------------


def _norm_stem(text: str) -> str:
    return re.sub(r"[\s\W]+", "", (text or "").casefold(), flags=re.UNICODE)


def _normalize_options(raw: Any, qtype: str, issues: _Issues, idx: int, origin: str) -> list[dict[str, Any]]:
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        raw = [{"key": k, "text": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        issues.add(VC.SCHEMA_INVALID, "error", f"第 {idx + 1} 题 options 不是数组", question_index=idx)
        return []
    options: list[dict[str, Any]] = []
    for i, opt in enumerate(raw):
        if isinstance(opt, dict):
            key = str(opt.get("key") or chr(65 + i)).strip().upper()
            text = str(opt.get("text") or "").strip()
        elif isinstance(opt, str):
            key, text = chr(65 + i), opt.strip()
        else:
            continue
        options.append({"key": key, "text": text})
    if qtype != "choice":
        if options:
            issues.add(
                VC.ANSWER_FORMAT,
                "warning",
                f"第 {idx + 1} 题为 {QUESTION_TYPE_LABELS.get(qtype, qtype)}，忽略多余选项",
                question_index=idx,
            )
        return []
    if len(options) < 2:
        issues.add(
            VC.OPTIONS_MISSING,
            "error",
            f"第 {idx + 1} 题选择题选项不足（{len(options)} 个，至少 2 个）",
            hint="为选择题提供至少 2 个选项（常规 4 个）后重新输出",
            question_index=idx,
        )
    elif len(options) != 4:
        issues.add(
            VC.OPTION_COUNT,
            "warning",
            f"第 {idx + 1} 题选项数为 {len(options)}（常规为 4 个）",
            question_index=idx,
        )
    keys = [o["key"] for o in options]
    duplicated = sorted({k for k in keys if keys.count(k) > 1})
    if duplicated:
        issues.add(
            VC.OPTION_DUPLICATE,
            "error",
            f"第 {idx + 1} 题选项 key 重复：{'、'.join(duplicated)}",
            hint="保证选项 key 唯一（A/B/C/D）",
            question_index=idx,
        )
    return options


def _normalize_answer(
    qtype: str, raw: Any, options: list[dict[str, Any]], stem: str, issues: _Issues, idx: int, origin: str
) -> Any:
    """按题型归一化答案；结构问题写入 issues（选择题答案必须对应选项）。"""
    if qtype == "choice":
        keys = {o["key"] for o in options}
        if raw is None or raw == "" or raw == []:
            issues.add(
                VC.ANSWER_MISSING,
                "error",
                f"第 {idx + 1} 题缺少答案",
                hint="给出正确选项字母（多选给字母数组）",
                question_index=idx,
            )
            return None
        if isinstance(raw, list):
            parts = [str(p).strip().upper() for p in raw]
        else:
            s = str(raw).strip().upper()
            parts = re.split(r"[、,，;；/\s]+", s) if re.search(r"[、,，;；/\s]", s) else list(s)
        parts = [p for p in parts if p]
        for p in parts:
            if p not in keys:
                issues.add(
                    VC.ANSWER_NOT_IN_OPTIONS,
                    "error",
                    f"第 {idx + 1} 题答案 {p!r} 不在选项中（选项：{'、'.join(sorted(keys))}）",
                    hint="答案必须是选项字母之一；若有疑问重新核对后输出",
                    question_index=idx,
                )
        return parts[0] if len(parts) == 1 else parts
    if qtype == "true_false":
        if isinstance(raw, bool):
            return raw
        s = "" if raw is None else str(raw).strip().lower()
        if s in _TRUE_WORDS:
            return True
        if s in _FALSE_WORDS:
            return False
        issues.add(
            VC.ANSWER_FORMAT,
            "error",
            f"第 {idx + 1} 题判断题答案无法识别：{raw!r}",
            hint="答案用 true/false（或 对/错）",
            question_index=idx,
        )
        return raw
    if qtype == "fill_blank":
        if raw is None or raw == "":
            issues.add(
                VC.ANSWER_MISSING,
                "error",
                f"第 {idx + 1} 题缺少答案",
                hint="按空位顺序给出答案数组",
                question_index=idx,
            )
            return None
        answers = raw if isinstance(raw, list) else [raw]
        blanks = len(_BLANK_RE.findall(stem)) + len(_PAREN_BLANK_RE.findall(stem))
        if blanks == 0:
            issues.add(
                VC.ANSWER_FORMAT,
                "warning",
                f"第 {idx + 1} 题题干未检测到空位标记（____）",
                hint="填空题请用连续下划线 ____ 标注空位",
                question_index=idx,
            )
        elif len(answers) != blanks:
            issues.add(
                VC.BLANK_COUNT_MISMATCH,
                "error",
                f"第 {idx + 1} 题空位数 {blanks} 与答案数 {len(answers)} 不一致",
                hint="每空一个答案，按空位顺序排列",
                question_index=idx,
            )
        return answers
    # calculation / short_answer
    if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []:
        issues.add(
            VC.ANSWER_MISSING,
            "error",
            f"第 {idx + 1} 题缺少参考答案",
            hint="给出完整解答，并在 scoring 中列出评分要点",
            question_index=idx,
        )
        return None
    if isinstance(raw, list):
        return "；".join(str(p) for p in raw)
    return str(raw)


def _resolve_refs(
    raw_refs: Any,
    snapshot: MaterialSnapshot,
    chunk_map: dict[str, tuple[Any, Any]],
    note_lines: int,
    allowed_bank_ids: set[str] | None,
    bank_labels: dict[str, str],
    issues: _Issues,
    idx: int,
) -> list[QuestionSourceRef]:
    """模型给出的来源 → 权威引用（未知 id 报 SOURCE_REF_FAKE 并提示可用 id）。"""
    refs: list[QuestionSourceRef] = []
    if raw_refs is None:
        raw_refs = []
    if not isinstance(raw_refs, list):
        raw_refs = [raw_refs]
    seen: set[tuple[str, str]] = set()
    allowed_sample = "、".join(sorted(chunk_map)[:8])
    for ref in raw_refs:
        if isinstance(ref, str):
            ref = {"chunk_id": ref}
        if not isinstance(ref, dict):
            continue
        cid = ref.get("chunk_id") or ref.get("chunk") or ref.get("片段id")
        if cid is not None:
            key = ("chunk", str(cid))
            if key in seen:
                continue
            seen.add(key)
            hit = chunk_map.get(str(cid))
            if hit is None:
                issues.add(
                    VC.SOURCE_REF_FAKE,
                    "error",
                    f"第 {idx + 1} 题引用了不存在的片段 id：{cid}",
                    hint=f"只能引用给定 id（示例：{allowed_sample}），不得编造来源",
                    question_index=idx,
                )
                continue
            material, chunk = hit
            refs.append(
                QuestionSourceRef(
                    kind="assignment" if material.kind in ("assignment", "attachment") else "courseware",
                    label=chunk_label(snapshot, chunk),
                    chunk_id=chunk.chunk_id,
                    material_id=material.material_id,
                )
            )
            continue
        notes_line = ref.get("notes_line") if "notes_line" in ref else ref.get("备注行")
        if notes_line is not None:
            try:
                line_no = int(notes_line)
            except (TypeError, ValueError):
                line_no = -1
            if 1 <= line_no <= note_lines:
                refs.append(QuestionSourceRef(kind="notes", label=f"备注第{line_no}行", notes_line=line_no))
            else:
                issues.add(
                    VC.SOURCE_REF_FAKE,
                    "error",
                    f"第 {idx + 1} 题引用了不存在的备注行：{notes_line!r}",
                    hint=f"备注共 {note_lines} 行，notes_line 用 1..{note_lines}",
                    question_index=idx,
                )
            continue
        bid = ref.get("bank_candidate_id") or ref.get("candidate_id") or ref.get("候选id")
        if bid is not None:
            if allowed_bank_ids is not None and str(bid) in allowed_bank_ids:
                refs.append(
                    QuestionSourceRef(
                        kind="bank",
                        label=bank_labels.get(str(bid)) or f"网络候选题 {bid}",
                        url=None,
                    )
                )
            else:
                issues.add(
                    VC.SOURCE_REF_FAKE,
                    "error",
                    f"第 {idx + 1} 题引用了未选入的网络候选：{bid}",
                    hint="只能引用消息中给出的候选 id，或改用课件片段/备注行引用",
                    question_index=idx,
                )
    return refs


def _normalize_ai_question(
    raw: Any,
    idx: int,
    blueprint: PaperBlueprint,
    snapshot: MaterialSnapshot,
    chunk_map: dict[str, tuple[Any, Any]],
    note_lines: int,
    allowed_bank_ids: set[str] | None,
    bank_labels: dict[str, str],
    issues: _Issues,
) -> ReviewQuestion | None:
    if not isinstance(raw, dict):
        issues.add(VC.SCHEMA_INVALID, "error", f"第 {idx + 1} 题不是 JSON 对象", question_index=idx)
        return None
    qtype = normalize_question_type(str(raw.get("qtype") or raw.get("type") or ""))
    if qtype is None:
        issues.add(
            VC.TYPE_UNSUPPORTED,
            "error",
            f"第 {idx + 1} 题题型不支持：{raw.get('qtype')!r}",
            hint="题型用 choice/true_false/fill_blank/calculation/short_answer（或对应中文名）",
            question_index=idx,
        )
        return None
    stem = str(raw.get("stem") or raw.get("question") or raw.get("题干") or "").strip()
    if not stem:
        issues.add(
            VC.STEM_MISSING,
            "error",
            f"第 {idx + 1} 题缺少题干",
            hint="给出完整题干后重新输出",
            question_index=idx,
        )
        return None
    options = _normalize_options(raw.get("options") or raw.get("选项"), qtype, issues, idx, "ai")
    explanation = str(
        raw.get("explanation") or raw.get("analysis") or raw.get("解析") or ""
    ).strip()
    scoring_raw = raw.get("scoring") or raw.get("scoring_points") or raw.get("评分要点") or []
    if isinstance(scoring_raw, str):
        scoring_raw = [scoring_raw] if scoring_raw.strip() else []
    scoring = [str(s).strip() for s in scoring_raw if str(s).strip()]
    kp_raw = raw.get("kp_ids") or raw.get("knowledge_points") or raw.get("知识点") or []
    if isinstance(kp_raw, str):
        kp_raw = [kp_raw]
    planned_kp_ids = {plan.kp_id for plan in blueprint.kp_plan}
    kp_ids: list[str] = []
    for kp in kp_raw:
        kp = str(kp).strip()
        if kp in planned_kp_ids:
            kp_ids.append(kp)
        elif kp:
            issues.add(
                VC.KP_UNKNOWN,
                "warning",
                f"第 {idx + 1} 题引用了规划外的知识点 {kp}，已忽略",
                hint="kp_ids 只能取蓝图中给出的 id",
                question_index=idx,
            )
    kp_names = [plan.name for plan in blueprint.kp_plan if plan.kp_id in kp_ids]
    difficulty = normalize_difficulty(raw.get("difficulty"))
    refs = _resolve_refs(
        raw.get("source_refs") or raw.get("refs") or raw.get("来源引用"),
        snapshot,
        chunk_map,
        note_lines,
        allowed_bank_ids,
        bank_labels,
        issues,
        idx,
    )
    if not refs:
        issues.add(
            VC.SOURCE_REF_MISSING,
            "error",
            f"第 {idx + 1} 题缺少可溯源的来源引用",
            hint="为每题至少引用 1 个片段 id / 备注行号 / 网络候选 id",
            question_index=idx,
        )
    return ReviewQuestion(
        question_id="",
        qtype=qtype,
        stem=stem,
        options=options,
        answer=raw.get("answer") if "answer" in raw else raw.get("答案"),
        explanation=explanation,
        scoring=scoring,
        kp_ids=kp_ids,
        kp_names=kp_names,
        difficulty=difficulty or "medium",
        points=0.0,
        origin="ai",
        source_refs=refs,
    )


# ---------------------------------------------------------------------------
# 试卷级校验与组装
# ---------------------------------------------------------------------------


def _chunk_map(snapshot: MaterialSnapshot) -> dict[str, tuple[Any, Any]]:
    return {chunk.chunk_id: (material, chunk) for material in snapshot.all_materials() for chunk in material.chunks}


def _assign_ids_and_points(questions: list[ReviewQuestion], blueprint: PaperBlueprint) -> list[ReviewQuestion]:
    """按蓝图题型顺序编号（rq_001…）并按蓝图分配每题分值（Σ 与蓝图小计一致）。"""
    order = {quota.qtype: i for i, quota in enumerate(blueprint.quotas)}
    indexed = list(enumerate(questions))
    indexed.sort(key=lambda item: (order.get(item[1].qtype, len(order)), item[0]))
    ordered = [q for _i, q in indexed]
    points_pool = {
        quota.qtype: distribute_points(quota.subtotal, quota.count) for quota in blueprint.quotas
    }
    cursor: dict[str, int] = defaultdict(int)
    for i, question in enumerate(ordered):
        question.question_id = f"rq_{i + 1:03d}"
        pool = points_pool.get(question.qtype) or []
        if cursor[question.qtype] < len(pool):
            question.points = pool[cursor[question.qtype]]
            cursor[question.qtype] += 1
        else:
            question.points = 0.0  # 超出配额的多余题
        # 题序内的稳定编号写回，便于问题定位
    return ordered


def validate_questions(
    questions: list[ReviewQuestion],
    snapshot: MaterialSnapshot,
    blueprint: PaperBlueprint,
    *,
    extra_issues: list[ValidationIssue] | None = None,
    partial_reasons: list[str] | None = None,
    repairable: bool = True,
    prompt_version: str = GENERATE_PROMPT_VERSION,
    truncated: bool = False,
    parsed_output: dict[str, Any] | None = None,
) -> ValidationOutcome:
    """对（AI/网络/混合）题目集合做统一校验并组装 PaperCandidate。"""
    issues = _Issues()
    for issue in extra_issues or []:
        issues.items.append(issue)
    partial_reasons = list(partial_reasons or [])

    # 逐题结构校验（答案/选项的归一化对网络题同样生效）
    prepared: list[ReviewQuestion] = []
    for idx, question in enumerate(questions):
        origin = question.origin
        explanation_missing = not question.explanation.strip()
        if explanation_missing:
            issues.add(
                VC.EXPLANATION_MISSING,
                "warning" if origin == "bank" else "error",
                f"第 {idx + 1} 题缺少解析",
                hint="补充解析（必填）；网络题缺解析请改用混合模式或人工补充",
                question_index=idx,
            )
        if question.qtype in ("calculation", "short_answer") and not question.scoring:
            issues.add(
                VC.SCORING_MISSING,
                "warning" if origin == "bank" else "error",
                f"第 {idx + 1} 题缺少评分要点",
                hint="在 scoring 中给出评分要点（步骤分/要点分）",
                question_index=idx,
            )
        if not question.source_refs:
            issues.add(
                VC.SOURCE_REF_MISSING,
                "error",
                f"第 {idx + 1} 题缺少来源引用",
                hint="补充课件片段/作业附件/备注行/网络候选的引用",
                question_index=idx,
            )
        options = _normalize_options(
            [dict(o) for o in question.options], question.qtype, issues, idx, origin
        )
        question.options = options
        question.answer = _normalize_answer(
            question.qtype, question.answer, options, question.stem, issues, idx, origin
        )
        prepared.append(question)

    # 重复题：规范化题干完全相同 → error；高度相似 → warning
    dup_norms: set[str] = set()
    seen_norms: dict[str, int] = {}
    known_norms: list[tuple[str, int]] = []
    for idx, question in enumerate(prepared):
        norm = _norm_stem(question.stem)
        if norm in seen_norms:
            issues.add(
                VC.DUPLICATE_QUESTION,
                "error",
                f"第 {idx + 1} 题与第 {seen_norms[norm] + 1} 题题干重复",
                hint="删除或改写为不同知识点的题目",
                question_index=idx,
            )
            dup_norms.add(norm)
            continue
        near = next(
            (
                (other_norm, other_idx)
                for other_norm, other_idx in known_norms
                if difflib.SequenceMatcher(None, norm, other_norm).ratio() >= _NEAR_DUPLICATE_RATIO
            ),
            None,
        )
        if near is not None:
            issues.add(
                VC.NEAR_DUPLICATE,
                "warning",
                f"第 {idx + 1} 题与第 {near[1] + 1} 题高度相似",
                hint="确认是否重复考察同一知识点，必要时替换",
                question_index=idx,
            )
        seen_norms[norm] = idx
        known_norms.append((norm, idx))

    # 配额与总分
    counts = Counter(q.qtype for q in prepared)
    counts_by_type = {quota.qtype: counts.get(quota.qtype, 0) for quota in blueprint.quotas}
    quota_types = {quota.qtype for quota in blueprint.quotas}
    for quota in blueprint.quotas:
        actual = counts.get(quota.qtype, 0)
        if actual != quota.count:
            issues.add(
                VC.QUOTA_MISMATCH,
                "error",
                f"{QUESTION_TYPE_LABELS.get(quota.qtype, quota.qtype)}需要 {quota.count} 题，实际 {actual} 题",
                hint="按蓝图配额精确补齐或删减（不得改变用户约束）",
            )
    for qtype, actual in counts.items():
        if qtype not in quota_types and actual:
            issues.add(
                VC.QUOTA_MISMATCH,
                "error",
                f"出现蓝图未要求的题型 {QUESTION_TYPE_LABELS.get(qtype, qtype)} × {actual}",
                hint="删除多余题型或修改蓝图配额",
            )

    # 编号与分值（在统计前完成，保证报告与候选一致）
    ordered = _assign_ids_and_points(prepared, blueprint)
    total_points = round(sum(q.points for q in ordered), 2)
    if abs(total_points - round(blueprint.total_points, 2)) > 0.001:
        issues.add(
            VC.TOTAL_POINTS_MISMATCH,
            "error",
            f"试卷实际总分 {total_points} 与蓝图 {blueprint.total_points} 不一致",
            hint="题数与每题分值必须与蓝图一致（先解决配额问题）",
        )

    # 覆盖：规划内章节必须至少 1 题（error）；知识点覆盖不足为 warning
    kp_section = {plan.kp_id: plan.section for plan in blueprint.kp_plan}
    planned_sections = list(dict.fromkeys(plan.section for plan in blueprint.kp_plan))
    covered_kps: set[str] = set()
    for question in ordered:
        covered_kps.update(kp for kp in question.kp_ids if kp in kp_section)
    missing_kp_ids = [kp for kp in kp_section if kp not in covered_kps]
    if missing_kp_ids:
        names = [plan.name for plan in blueprint.kp_plan if plan.kp_id in missing_kp_ids][:6]
        issues.add(
            VC.COVERAGE_MISSING,
            "warning",
            f"{len(missing_kp_ids)} 个规划知识点没有对应题目：{'、'.join(names)}",
            hint="为未覆盖知识点补题（或调整蓝图权重）",
        )
    covered_sections = {kp_section[kp] for kp in covered_kps}
    for section in planned_sections:
        if section not in covered_sections:
            issues.add(
                VC.COVERAGE_MISSING,
                "error",
                f"章节「{section}」没有任何题目",
                hint="为该章节至少出 1 题（用户限定的章节覆盖必须满足）",
            )

    errors = sum(1 for i in issues.items if i.severity == "error")
    warnings = sum(1 for i in issues.items if i.severity == "warning")
    status = "errors" if errors else ("ok_with_warnings" if warnings else "ok")
    duplicate_ids = [
        [q.question_id for q in ordered if _norm_stem(q.stem) == norm]
        for norm in dup_norms
    ]
    duplicate_ids = [ids for ids in duplicate_ids if len(ids) > 1]
    report = ValidationReport(
        status=status,
        issues=issues.items,
        repair_needed=bool(errors) and repairable,
        counts_by_type=counts_by_type,
        total_questions=len(ordered),
        total_points=total_points,
        coverage={
            "planned": len(kp_section),
            "covered": len(covered_kps),
            "missing_kp_ids": missing_kp_ids,
            "sections": {s: (s in covered_sections) for s in planned_sections},
            "difficulty_counts": dict(Counter(q.difficulty for q in ordered)),
        },
        duplicates=duplicate_ids,
        academic_correctness="not_verified",
        checks_limitations=list(_CHECK_LIMITATIONS),
        truncated=truncated,
        prompt_version=prompt_version,
    )

    title = blueprint.title
    candidate_warnings = list(
        dict.fromkeys(
            [
                *blueprint.warnings,
                *[w for q in ordered for w in q.warnings],
                *partial_reasons,
            ]
        )
    )
    candidate = PaperCandidate(
        candidate_id="rpc_"
        + hashlib.sha256(
            (blueprint.fingerprint + "|" + "|".join(_norm_stem(q.stem) for q in ordered)).encode("utf-8")
        ).hexdigest()[:12],
        course_id=snapshot.course_id,
        title=title,
        mode=blueprint.mode,
        questions=ordered,
        notes=snapshot.notes,
        blueprint=blueprint,
        fingerprint=blueprint.fingerprint,
        provider=snapshot.provider,
        warnings=candidate_warnings,
        partial=bool(errors) or bool(partial_reasons),
        engine_version=REVIEW_ENGINE_VERSION,
        prompt_version=prompt_version,
    )
    return ValidationOutcome(candidate=candidate, report=report, parsed_output=parsed_output)


def parse_and_validate_output(
    raw_output: str | None,
    snapshot: MaterialSnapshot,
    blueprint: PaperBlueprint,
    selected: BankSelection | None = None,
) -> ValidationOutcome:
    """AI 模式主入口：解析模型输出 → 归一化 → 校验 → PaperCandidate + 报告。"""
    data, parse_issues, truncated = parse_model_output(raw_output)
    if data is None:
        report = ValidationReport(
            status="failed",
            issues=parse_issues,
            repair_needed=True,
            truncated=truncated,
            prompt_version=GENERATE_PROMPT_VERSION,
            checks_limitations=list(_CHECK_LIMITATIONS),
        )
        return ValidationOutcome(candidate=None, report=report, parsed_output=None)

    questions_raw = data.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        issues = _Issues()
        for issue in parse_issues:
            issues.items.append(issue)
        issues.add(
            VC.SCHEMA_INVALID,
            "error",
            "缺少 questions 数组或为空",
            hint="按输出规范返回 {\"title\", \"questions\": [...]}",
        )
        report = ValidationReport(
            status="failed",
            issues=issues.items,
            repair_needed=True,
            truncated=truncated,
            prompt_version=GENERATE_PROMPT_VERSION,
            checks_limitations=list(_CHECK_LIMITATIONS),
        )
        return ValidationOutcome(candidate=None, report=report, parsed_output=data)

    if len(questions_raw) > _MAX_RAW_QUESTIONS:
        parse_issues = [
            *parse_issues,
            ValidationIssue(
                code=VC.SCHEMA_INVALID,
                severity="warning",
                message=f"模型返回题目过多（{len(questions_raw)}），仅处理前 {_MAX_RAW_QUESTIONS} 题",
            ),
        ]
        questions_raw = questions_raw[:_MAX_RAW_QUESTIONS]

    chunk_map = _chunk_map(snapshot)
    note_lines = len(snapshot.notes.splitlines()) if snapshot.notes else 0
    allowed_bank_ids: set[str] | None = None
    bank_labels: dict[str, str] = {}
    if selected is not None:
        allowed_bank_ids = {item.candidate.candidate_id for item in selected.selected}
        for item in selected.selected:
            source = item.candidate.source
            if source is not None:
                bank_labels[item.candidate.candidate_id] = source.title or source.site or "网络候选题"

    issues = _Issues()
    for issue in parse_issues:
        issues.items.append(issue)
    prepared: list[ReviewQuestion] = []
    for idx, raw in enumerate(questions_raw):
        question = _normalize_ai_question(
            raw, idx, blueprint, snapshot, chunk_map, note_lines, allowed_bank_ids, bank_labels, issues
        )
        if question is not None:
            prepared.append(question)

    outcome = validate_questions(
        prepared,
        snapshot,
        blueprint,
        extra_issues=issues.items,
        repairable=True,
        prompt_version=GENERATE_PROMPT_VERSION,
        truncated=truncated,
        parsed_output=data,
    )
    return outcome


def validate_bank_questions(
    selection: BankSelection,
    snapshot: MaterialSnapshot,
    blueprint: PaperBlueprint,
) -> ValidationOutcome:
    """纯题库模式：选题 → 校验（无模型可修复；缺题以 deficits 说明，交用户决策）。"""
    questions = build_bank_questions(selection, blueprint)
    issues: list[ValidationIssue] = []
    partial_reasons: list[str] = []
    for deficit in selection.deficits:
        issues.append(
            ValidationIssue(
                code=VC.DEFICIT_REMAINING,
                severity="warning",
                message=deficit.message,
                hint="可改用 AI/混合模式补题，或调整题数/知识点蓝图",
            )
        )
        partial_reasons.append(deficit.message)
    for question in questions:
        if not question.explanation.strip():
            issues.append(
                ValidationIssue(
                    code=VC.BANK_INCOMPLETE,
                    severity="warning",
                    message=f"网络题《{question.stem[:24]}…》缺少解析，已如实标记",
                    hint="建议人工补充解析或改用混合模式",
                )
            )
            partial_reasons.append(f"网络题缺解析：{question.stem[:24]}…")
    return validate_questions(
        questions,
        snapshot,
        blueprint,
        extra_issues=issues,
        partial_reasons=partial_reasons,
        repairable=False,
        prompt_version=BANK_PROMPT_VERSION,
    )


def merge_ai_with_bank(
    selection: BankSelection,
    ai_outcome: ValidationOutcome,
    snapshot: MaterialSnapshot,
    blueprint: PaperBlueprint,
) -> ValidationOutcome:
    """混合模式：可靠网络题 + AI 补题 → 统一去重/配额/总分校验。"""
    bank_questions = build_bank_questions(selection, blueprint)
    ai_questions = list(ai_outcome.candidate.questions) if ai_outcome.candidate is not None else []

    issues: list[ValidationIssue] = [
        issue for issue in ai_outcome.report.issues if issue.code in _PARSE_LEVEL_CODES
    ]
    kept_ai: list[ReviewQuestion] = []
    bank_norms = {_norm_stem(q.stem): q for q in bank_questions}
    for question in ai_questions:
        norm = _norm_stem(question.stem)
        duplicated = any(
            norm == bn or difflib.SequenceMatcher(None, norm, bn).ratio() >= _NEAR_DUPLICATE_RATIO
            for bn in bank_norms
        )
        if duplicated:
            issues.append(
                ValidationIssue(
                    code=VC.DUPLICATE_QUESTION,
                    severity="warning",
                    message=f"AI 补题与网络题重复《{question.stem[:24]}…》，已去重",
                    hint="补题时避开已选网络题的知识点与题干",
                )
            )
            continue
        kept_ai.append(question)

    merged = [*bank_questions, *kept_ai]
    counts = Counter(q.qtype for q in merged)
    partial_reasons: list[str] = []
    for deficit in selection.deficits:
        remaining = max(0, deficit.needed - counts.get(deficit.qtype, 0))
        if remaining > 0:
            issues.append(
                ValidationIssue(
                    code=VC.DEFICIT_REMAINING,
                    severity="warning",
                    message=f"混合补题后仍缺 {deficit.qtype} {remaining} 题（{deficit.reason}）",
                    hint="可减少题数、放宽知识点范围或再次生成补题",
                )
            )
            partial_reasons.append(f"混合补题后仍缺 {deficit.qtype} {remaining} 题")
    for question in bank_questions:
        if not question.explanation.strip():
            partial_reasons.append(f"网络题缺解析：{question.stem[:24]}…")

    return validate_questions(
        merged,
        snapshot,
        blueprint,
        extra_issues=issues,
        partial_reasons=partial_reasons,
        repairable=True,
        prompt_version=GENERATE_PROMPT_VERSION,
        truncated=ai_outcome.report.truncated,
        parsed_output=ai_outcome.parsed_output,
    )
