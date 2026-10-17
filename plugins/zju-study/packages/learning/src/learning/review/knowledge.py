"""知识点提取：课件/作业/附件/备注 → 知识点与证据关系（纯启发式，可复现）。

- 事实与推断分离：只有出现在正文中的词条/标题才成为知识点；与资料无关的备注要求
  记入警告，不强行编出“课内依据”。
- 证据分角色：courseware（课件出现）/ assignment（作业实际考察）/ notes（备注要求）。
- 作业频率只作参考，不据此推断考试范围（出现在作业中仅标记 assignment_assessed）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .snapshot import chunk_label
from .types import (
    Evidence,
    KnowledgePoint,
    MaterialSnapshot,
    MaterialsAnalysis,
    NoteRequirement,
    SnapshotMaterial,
)

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#\-]{1,30}")

# n-gram 边界停用字符（避免“的了/是和”这类片段成为“知识点”）
_STOPCHARS = set("的了吗呢吧啊是和与或在有为对于把被这那其之也都很就才又而及等或可要不将会上中下前后时")

_LATIN_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "is", "of", "to", "in", "on",
    "or", "an", "as", "by", "be", "it", "if", "at", "we", "can", "not", "no", "yes", "all",
    "ppt", "pdf", "docx", "page", "slide", "chapter", "figure", "table", "example", "exercise",
}

_NOTES_CONTENT_MARKS = (
    "考察", "重点", "难点", "掌握", "理解", "熟悉", "涉及", "章", "知识点", "计算", "证明",
    "判断", "选择", "简答", "填空", "定义", "定理", "条件", "综合", "应用", "分析",
)
_NOTES_FORMAT_MARKS = (
    "答案单独", "分开显示", "排版", "格式", "导出", "打印", "显示", "只需", "仅输出", "json",
    "markdown", "字体", "序号", "页面",
)
_NOTES_REQUIREMENT_VERBS = ("请", "需要", "必须", "重点", "应", "要")

# 词条黑名单：作为“知识点”无区分度或纯流程词（避免“分析/附件”这类跨材料高频词成为 KP）
_TERM_BLOCKLIST = {
    "分析", "要求", "说明", "附件", "作业", "报告", "测试", "时间", "过程",
    "方法", "问题", "内容", "操作", "序列", "使用", "情况", "实现",
}

TERM_MIN_COUNT = 3
TERM_MIN_CHUNKS = 2
TERM_TOP_N = 12
ASSIGNMENT_TERM_TOP_N = 8


def _reference_only(text: str) -> bool:
    text = re.sub(r"本周的作业是|本次的作业是|请同学们按时完成作业|请按时完成作业", "", text)
    remaining = re.sub(r"第[一二三四五六七八九十百零〇0-9]+章|练习题|习题|作业|完成|请做|题|第|的|和|与|及|共|道", "", text)
    return not re.search(r"[A-Za-z\u4e00-\u9fff]", remaining)

_SNIPPET_CHARS = 80


def _norm(text: str) -> str:
    return re.sub(r"[\s\W]+", "", text.casefold(), flags=re.UNICODE)


def kp_id_for(name: str) -> str:
    return "kp:" + hashlib.sha256(_norm(name).encode("utf-8")).hexdigest()[:10]


def _snippet(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:_SNIPPET_CHARS] + ("…" if len(flat) > _SNIPPET_CHARS else "")


def _contains_term(text_cf: str, term: str) -> bool:
    term_cf = term.casefold()
    if re.search(r"[a-z]", term_cf):
        return re.search(rf"(?<![a-z0-9_]){re.escape(term_cf)}(?![a-z0-9_])", text_cf) is not None
    return term_cf in text_cf


def _heading_keywords(name: str) -> list[str]:
    """标题 → 关键词（含 3-4 字子词条）：让“重点考察边界条件”这类备注能对上标题下的知识点。"""
    keywords = [name.strip()]
    for run in _CJK_RUN_RE.findall(name):
        if len(run) <= 4:
            # 单字片段（“第”“章”“节”等编号词）不构成关键词
            if len(run) >= 2 and run[0] not in _STOPCHARS and run[-1] not in _STOPCHARS:
                keywords.append(run)
            continue
        for n in (4, 3):
            for i in range(len(run) - n + 1):
                gram = run[i : i + n]
                if gram[0] in _STOPCHARS or gram[-1] in _STOPCHARS:
                    continue
                keywords.append(gram)
    keywords.extend(_latin_terms(name))
    return list(dict.fromkeys(k for k in keywords if k))[:10]


def _cjk_terms(text: str) -> list[str]:
    terms: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        for n in (2, 3, 4):
            for i in range(0, len(run) - n + 1):
                gram = run[i : i + n]
                if gram[0] in _STOPCHARS or gram[-1] in _STOPCHARS:
                    continue
                if len(set(gram)) == 1:  # 叠字片段无信息量
                    continue
                terms.append(gram)
    return terms


def _latin_terms(text: str) -> list[str]:
    out: list[str] = []
    for token in _LATIN_TOKEN_RE.findall(text):
        lowered = token.casefold().strip("-+.#_")
        if len(lowered) < 2 or lowered in _LATIN_STOPWORDS or lowered.isdigit():
            continue
        out.append(lowered)
    return out


def _top_terms(
    chunk_texts: list[str],
    min_count: int,
    min_chunks: int,
    top_n: int,
    taken: set[str],
) -> list[tuple[str, int, int]]:
    """跨片段词频统计：返回 [(term, count, chunk_hits)]，过滤冗余与已占用词条。"""
    counts: dict[str, int] = {}
    hits: dict[str, int] = {}
    for text in chunk_texts:
        per_chunk: set[str] = set()
        for term in [*_cjk_terms(text), *_latin_terms(text)]:
            counts[term] = counts.get(term, 0) + 1
            per_chunk.add(term)
        for term in per_chunk:
            hits[term] = hits.get(term, 0) + 1
    candidates = [
        (term, count, hits[term])
        for term, count in counts.items()
        if count >= min_count
        and hits[term] >= min_chunks
        and term not in taken
        and term not in _TERM_BLOCKLIST
    ]
    candidates.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))
    kept: list[tuple[str, int, int]] = []
    for term, count, hit in candidates:
        if any(term in k[0] for k in kept):
            continue  # 更短片段已被更长词条覆盖（且频次不更高）
        kept.append((term, count, hit))
        if len(kept) >= top_n:
            break
    return kept


class _KpBuilder:
    def __init__(self, snapshot: MaterialSnapshot) -> None:
        self.snapshot = snapshot
        self.kps: list[KnowledgePoint] = []
        self._by_id: dict[str, KnowledgePoint] = {}

    def add(
        self,
        name: str,
        origin: str,
        section: str,
        keywords: list[str] | None = None,
    ) -> KnowledgePoint | None:
        name = name.strip()
        if not name:
            return None
        kp_id = kp_id_for(name)
        if kp_id in self._by_id:
            return self._by_id[kp_id]
        kp = KnowledgePoint(
            kp_id=kp_id,
            name=name,
            origin=origin,
            section=section or "(未分节)",
            keywords=list(dict.fromkeys(keywords or [name])),
        )
        self.kps.append(kp)
        self._by_id[kp_id] = kp
        return kp

    def find(self, text: str) -> list[KnowledgePoint]:
        text_cf = text.casefold()
        matched: list[KnowledgePoint] = []
        for kp in self.kps:
            if _norm(kp.name) and _norm(kp.name) in _norm(text):
                matched.append(kp)
                continue
            if any(_contains_term(text_cf, kw) for kw in kp.keywords if len(kw) >= 2):
                matched.append(kp)
        return matched


def _section_of(chunk_heading: tuple[str, ...], fallback: str) -> str:
    return chunk_heading[0] if chunk_heading else fallback


def _courseware_materials(snapshot: MaterialSnapshot) -> list[SnapshotMaterial]:
    return list(snapshot.materials)


def _assignment_materials(snapshot: MaterialSnapshot) -> list[SnapshotMaterial]:
    return [*snapshot.assignments, *snapshot.attachments]


def analyze_materials(snapshot: MaterialSnapshot) -> MaterialsAnalysis:
    """材料 → 知识点（含证据角色）+ 备注要求 + 缺口/警告。

    纯函数；不发起网络或模型调用；不编造来源。无正文时返回空知识点并保留 gaps，
    由蓝图层以 MATERIAL_UNPARSED 明确失败。
    """
    analysis = MaterialsAnalysis()
    analysis.gaps = list(snapshot.gaps)

    courseware = [m for m in _courseware_materials(snapshot) if any(c.text.strip() for c in m.chunks)]
    assignments = [m for m in _assignment_materials(snapshot) if any(c.text.strip() for c in m.chunks)]

    if not courseware:
        analysis.warnings.append("未选择可读课件：出题依据仅为作业与备注，建议补充课件")
    if not assignments:
        analysis.warnings.append("未选择作业：试卷仅覆盖课件，建议补选作业以覆盖作业技能")

    builder = _KpBuilder(snapshot)

    # 1) 标题知识点（文档顺序；标题即“出现在课件中的内容”，不是按文件名猜测）
    for material in courseware:
        for chunk in material.chunks:
            if chunk.kind == "heading" and chunk.text.strip():
                section = _section_of(chunk.heading_path, chunk.text.strip())
                kp = builder.add(
                    chunk.text.strip(), "heading", section, keywords=_heading_keywords(chunk.text.strip())
                )
                if kp is not None and not any(e.chunk_id == chunk.chunk_id for e in kp.evidence):
                    kp.evidence.append(
                        Evidence(
                            role="courseware",
                            label=chunk_label(snapshot, chunk),
                            material_id=material.material_id,
                            chunk_id=chunk.chunk_id,
                            snippet=_snippet(chunk.text),
                        )
                    )
                    kp.courseware_evidence += 1

    # 2) 高频词条知识点（正文 n-gram / 拉丁词，跨片段出现；与标题去重）
    heading_names = {_norm(kp.name) for kp in builder.kps}
    courseware_body = [c.text for m in courseware for c in m.chunks if c.text.strip() and c.kind != "heading"]
    for term, _count, _hits in _top_terms(
        courseware_body, TERM_MIN_COUNT, TERM_MIN_CHUNKS, TERM_TOP_N, taken=set()
    ):
        if any(_norm(term) in hn or hn in _norm(term) for hn in heading_names if hn):
            continue
        section = "(未分节)"
        for material in courseware:
            found = next((c for c in material.chunks if _contains_term(c.text.casefold(), term)), None)
            if found is not None:
                section = _section_of(found.heading_path, section)
                kp = builder.add(term, "term", section, keywords=[term])
                if kp is not None:
                    kp.evidence.append(
                        Evidence(
                            role="courseware",
                            label=chunk_label(snapshot, found),
                            material_id=material.material_id,
                            chunk_id=found.chunk_id,
                            snippet=_snippet(found.text),
                        )
                    )
                    kp.courseware_evidence += 1
                break

    # 3) 作业证据：作业实际考察的知识点（匹配课件知识点；不匹配的成为“作业独有”知识点）
    assignment_texts: list[tuple[SnapshotMaterial, Any]] = [
        (m, c) for m in assignments for c in m.chunks if c.text.strip()
    ]
    for kp in list(builder.kps):
        for material, chunk in assignment_texts:
            if _contains_term(chunk.text.casefold(), kp.name) or any(
                _contains_term(chunk.text.casefold(), kw) for kw in kp.keywords if len(kw) >= 2
            ):
                kp.assignment_assessed = True
                if sum(1 for e in kp.evidence if e.role == "assignment") < 2:
                    kp.evidence.append(
                        Evidence(
                            role="assignment",
                            label=chunk_label(snapshot, chunk),
                            material_id=material.material_id,
                            chunk_id=chunk.chunk_id,
                            snippet=_snippet(chunk.text),
                        )
                    )
                break

    # 4) 作业独有知识点（仅出现在作业/附件中的词条）
    # 只有章节/题号的作业通知是范围线索，没有实际题面，不能凭“练习题”构造知识点。
    assignment_corpus = [c.text for _m, c in assignment_texts if not _reference_only(c.text)]
    existing = {_norm(kp.name) for kp in builder.kps}
    for term, _count, _hits in _top_terms(
        assignment_corpus, 2, 1, ASSIGNMENT_TERM_TOP_N, taken=set()
    ):
        if any(_norm(term) in name or name in _norm(term) for name in existing if name):
            continue
        chunk = next((c for _m, c in assignment_texts if _contains_term(c.text.casefold(), term)), None)
        if chunk is None:
            continue
        material = next(m for m, c in assignment_texts if c is chunk)
        kp = builder.add(term, "assignment", "作业与附件", keywords=[term])
        if kp is not None:
            kp.assignment_assessed = True
            kp.evidence.append(
                Evidence(
                    role="assignment",
                    label=chunk_label(snapshot, chunk),
                    material_id=material.material_id,
                    chunk_id=chunk.chunk_id,
                    snippet=_snippet(chunk.text),
                )
            )

    # 5) 备注要求：内容要求匹配知识点；格式要求不强制找依据；其他行静默尝试匹配
    for line_no, raw_line in enumerate(snapshot.notes.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        is_format = any(mark in lowered for mark in _NOTES_FORMAT_MARKS)
        is_content = any(mark in line for mark in _NOTES_CONTENT_MARKS) or (
            any(verb in line for verb in _NOTES_REQUIREMENT_VERBS) and not is_format
        )
        if is_content:
            kind = "content"
        elif is_format:
            kind = "format"
        else:
            kind = "other"
        requirement = NoteRequirement(line=line_no, text=line, kind=kind)
        if kind in ("content", "other"):
            matched = builder.find(line)
            requirement.matched_kp_ids = [kp.kp_id for kp in matched]
            for kp in matched:
                kp.note_requested = True
                kp.evidence.append(
                    Evidence(
                        role="notes",
                        label=f"备注第{line_no}行",
                        notes_line=line_no,
                        snippet=_snippet(line),
                    )
                )
            if not matched and kind == "content":
                analysis.warnings.append(
                    f"备注第{line_no}行的要求未在材料中找到依据（将按原文发送并交由模型判断）：{_snippet(line)}"
                )
        analysis.note_requirements.append(requirement)

    # 6) 兜底：有正文却提不出任何知识点时，给通用“综合复习”桶（不按文件名猜内容）
    if not builder.kps and (courseware or assignments):
        kp = builder.add("综合复习", "fallback", "(未分节)", keywords=[])
        if kp is not None and courseware:
            first_chunk = next(c for m in courseware for c in m.chunks if c.text.strip())
            kp.evidence.append(
                Evidence(
                    role="courseware",
                    label=chunk_label(snapshot, first_chunk),
                    material_id=first_chunk.material_id,
                    chunk_id=first_chunk.chunk_id,
                    snippet=_snippet(first_chunk.text),
                )
            )
            kp.courseware_evidence += 1
        analysis.warnings.append("未能从材料中提取具体知识点，已退化为“综合复习”单一范围")

    # 7) 权重：作业考察与备注要求提升优先级（用于题量分配，不放宽用户约束）
    for kp in builder.kps:
        weight = 1.0
        if kp.assignment_assessed:
            weight += 1.5
        if kp.note_requested:
            weight += 1.0
        weight += min(1.0, 0.2 * max(0, kp.courseware_evidence - 1))
        kp.weight = round(weight, 2)

    order_index = {kp.kp_id: i for i, kp in enumerate(builder.kps)}
    analysis.knowledge_points = sorted(
        builder.kps, key=lambda kp: (-kp.weight, order_index[kp.kp_id])
    )

    sections: list[str] = []
    for material in courseware:
        for chunk in material.chunks:
            if not chunk.text.strip():
                continue
            section = _section_of(chunk.heading_path, "(未分节)")
            if section not in sections:
                sections.append(section)
    if any(kp.origin == "assignment" for kp in analysis.knowledge_points):
        sections.append("作业与附件")
    analysis.sections = sections

    analysis.stats = {
        "courseware_chunks": sum(len(m.chunks) for m in courseware),
        "assignment_chunks": sum(len(m.chunks) for m in assignments),
        "knowledge_points": len(analysis.knowledge_points),
        "assignment_only_kps": sum(1 for kp in analysis.knowledge_points if kp.origin == "assignment"),
        "notes_requirements": len(analysis.note_requirements),
        "unmatched_requirements": sum(
            1 for r in analysis.note_requirements if r.kind == "content" and not r.matched_kp_ids
        ),
    }
    return analysis
