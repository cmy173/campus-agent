#!/usr/bin/env python3
"""候选归一、去重、匹配理由与来源 URL 清洗（纯函数，离线可测）。

职责边界：
  - 只做**文本形态**归一，不改写语义；答案缺失保持 None，绝不猜测；
  - 来源 URL 保留可追溯性，但剔除敏感查询参数（token/session 等）与片段；
  - match_reason 只引用可以核对的证据（关键词命中、题型匹配），不编造相关性。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..redact import SENSITIVE_KEYS
from .types import (
    CANONICAL_TYPES,
    TYPE_CHOICE,
    TYPE_FILL_BLANK,
    TYPE_JUDGMENT,
    TYPE_OTHER,
)

# 断参数名（大小写不敏感）：命中即从 source_url 剔除其值，保留其余参数用于回溯
_URL_SENSITIVE_PARAMS = SENSITIVE_KEYS | {"code", "key", "sign", "signature", "sid", "t", "ts"}

_WS_RE = re.compile(r"[ \t\u3000]+")
_BLANK_RE = re.compile(r"(_{2,}|＿{2,})")
_OPTION_LABEL_RE = re.compile(r"^([A-Ha-h])\s*[.、．)）:：]\s*(.*)$", re.S)
_NUMBERING_RE = re.compile(r"^\s*(?:\d{1,2}\s*[、.．)）]|第\s*\d{1,3}\s*题|Q(?:uestion)?\s*\d{1,3}\s*[.、．:：)）]|[（(]\s*\d{1,3}\s*[)）]|[一二三四五六七八九十]{1,2}\s*[、.．)）])", re.I)
_ANSWER_PREFIX_RE = re.compile(r"^\s*(?:参考)?答案\s*[:：]?\s*|^\s*【答案】\s*|^\s*\[答案\]\s*", re.I)
_ANALYSIS_PREFIX_RE = re.compile(r"^\s*(?:答案解析|试题解析|解析|分析|解答|explanation|analysis)\s*[:：]?\s*", re.I)
_LATEX_INLINE_RE = re.compile(r"(\$\$?[^$]{1,200}\$\$?|\\\([^)]{1,200}\\\)|\\\[[^\]]{1,200}\\\])")


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()


def strip_numbering(text: str) -> str:
    """剥离题干开头的题号标记（"1." / "第2题" / "（3）" / "一、"）。"""
    return _NUMBERING_RE.sub("", text, count=1).strip()


_HAS_NUMBERING = _NUMBERING_RE


def has_numbering(text: str) -> bool:
    return bool(_NUMBERING_RE.match(text))


def strip_answer_prefix(text: str) -> str:
    return _ANSWER_PREFIX_RE.sub("", text, count=1).strip()


def strip_analysis_prefix(text: str) -> str:
    return _ANALYSIS_PREFIX_RE.sub("", text, count=1).strip()


def normalize_stem(text: str) -> str:
    """归一题干：折叠空白并剥离题号（用于去重与稳定 ID；不改写其余内容）。"""
    return collapse_ws(strip_numbering(collapse_ws(text)))


def find_latex(text: str) -> list[str]:
    """提取文本中的 LaTeX 片段（$...$ / \\(...\\) / \\[...\\]），最多 5 条。"""
    out: list[str] = []
    for m in _LATEX_INLINE_RE.finditer(text):
        out.append(m.group(1))
        if len(out) >= 5:
            break
    return out


def options_to_list(raw: Any) -> list[dict[str, str]]:
    """把多种选项形态归一为 [{"label": "A", "text": "..."}]。

    支持：{"A": "..."} dict、["A. xxx", "B. xxx"]、[{"label","text"}]、
    [{"key","value"}]、["xxx","yyy"]（顺序标号 A..）。
    无法识别的内容原样保留为文本，不丢弃、不猜测。
    """
    items: list[dict[str, str]] = []
    if isinstance(raw, dict):
        for i, (k, v) in enumerate(raw.items()):
            label = str(k).strip().upper()[:1]
            if not label.isalpha():
                label = chr(ord("A") + i) if i < 26 else str(i + 1)
            items.append({"label": label, "text": collapse_ws(str(v))})
        return items
    if isinstance(raw, (list, tuple)):
        for i, entry in enumerate(raw):
            fallback_label = chr(ord("A") + i) if i < 26 else str(i + 1)
            if isinstance(entry, dict):
                label = str(entry.get("label") or entry.get("key") or fallback_label).strip()
                text = entry.get("text") or entry.get("value") or ""
                items.append({"label": label.upper()[:1] if label[:1].isalpha() else fallback_label,
                              "text": collapse_ws(str(text))})
            else:
                text = collapse_ws(str(entry))
                m = _OPTION_LABEL_RE.match(text)
                if m:
                    items.append({"label": m.group(1).upper(), "text": collapse_ws(m.group(2))})
                else:
                    items.append({"label": fallback_label, "text": text})
        return items
    if isinstance(raw, str) and raw.strip():
        # 整段文本（"A. xx B. yy"）不在此猜测切分，交由 HTML 提取层逐行处理
        return []
    return []


_JUDGMENT_OPTION_TEXTS = {"正确", "错误", "对", "错", "是", "否", "true", "false", "t", "f"}


def is_judgment_options(options: list[dict[str, str]]) -> bool:
    if len(options) != 2:
        return False
    texts = {o.get("text", "").strip().lower() for o in options}
    return texts <= _JUDGMENT_OPTION_TEXTS and len(texts & _JUDGMENT_OPTION_TEXTS) == 2


def infer_type(
    stem: str,
    options: list[dict[str, str]],
    *,
    declared: Optional[str] = None,
) -> str:
    """题型推断：优先上游声明；否则按明确证据（选项/填空/动词）判定，否则 other。"""
    if declared:
        d = declared.strip().lower()
        mapping = {
            "choice": TYPE_CHOICE, "选择题": TYPE_CHOICE, "单选": TYPE_CHOICE, "多选": TYPE_CHOICE,
            "judgment": TYPE_JUDGMENT, "判断题": TYPE_JUDGMENT, "true_false": TYPE_JUDGMENT,
            "fill_blank": TYPE_FILL_BLANK, "填空题": TYPE_FILL_BLANK, "blank": TYPE_FILL_BLANK,
            "calculation": "calculation", "计算题": "calculation",
            "short_answer": "short_answer", "简答题": "short_answer",
            "proof": "proof", "证明题": "proof",
        }
        if d in mapping:
            return mapping[d]
    if is_judgment_options(options):
        return TYPE_JUDGMENT
    if len(options) >= 2:
        return TYPE_CHOICE
    if re.search(r"(判断|对错)", stem):
        return TYPE_JUDGMENT
    if _BLANK_RE.search(stem) or "填空" in stem:
        return TYPE_FILL_BLANK
    if re.search(r"(计算|求解|求值|求下列|计算下列)", stem):
        return "calculation"
    if re.search(r"(证明|求证)", stem):
        return "proof"
    if re.search(r"(简述|论述|试述|说明|写出|列举|阐述|说明下列)", stem):
        return "short_answer"
    return TYPE_OTHER


def canonicalize_type_filter(types: list[str]) -> list[str]:
    out: list[str] = []
    for t in types or []:
        c = infer_type("", [], declared=t)
        if c in CANONICAL_TYPES and c not in out:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 来源 URL 清洗
# ---------------------------------------------------------------------------

def sanitize_source_url(url: str) -> str:
    """剔除敏感查询参数与片段；相对 URL 原样（无法补 base 时保留路径形态）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _URL_SENSITIVE_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def resolve_and_sanitize(base_url: str, href: str) -> str:
    return sanitize_source_url(urljoin(base_url, href))


def site_of(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return host


# ---------------------------------------------------------------------------
# 稳定 ID 与去重
# ---------------------------------------------------------------------------

def stem_hash(stem: str, options: list[dict[str, str]]) -> str:
    payload = normalize_stem(stem) + "\u0001" + "\u0001".join(
        f"{o.get('label', '')}:{collapse_ws(o.get('text', ''))}" for o in options
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_id(stem: str, options: list[dict[str, str]], source_url: str) -> str:
    """稳定候选 ID：同一题干+来源 URL 多次抓取恒定（B 幂等键友好）。"""
    payload = stem_hash(stem, options) + "|" + sanitize_source_url(source_url)
    return "qb_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def dedupe_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按（归一题干+选项）去重；保留首次出现（来源顺序优先）。

    返回 (保留列表, 被去重记录列表)。被去重条目不静默丢弃——由引擎汇入 warnings。
    """
    seen: dict[str, dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for cand in candidates:
        key = stem_hash(cand.get("stem", ""), cand.get("options", []))
        if key in seen:
            first = seen[key]
            dropped.append({
                "stem": cand.get("stem", "")[:80],
                "source_id": cand.get("source_id"),
                "source_url": cand.get("source_url"),
                "kept_candidate_id": first.get("candidate_id"),
                "dropped_candidate_id": cand.get("candidate_id"),
            })
            continue
        seen[key] = cand
        kept.append(cand)
    return kept, dropped


# ---------------------------------------------------------------------------
# 匹配理由
# ---------------------------------------------------------------------------

def _term_hit(term: str, haystack_lower: str) -> bool:
    if not term:
        return False
    t = term.strip().lower()
    if not t:
        return False
    if t.isascii():
        # ASCII 术语按词边界，避免 "art" 命中 "start"
        return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", haystack_lower) is not None
    return t in haystack_lower


def match_reason_and_score(
    candidate: dict[str, Any],
    keywords: list[str],
    *,
    type_filter: list[str],
) -> tuple[str, float, list[str]]:
    """基于可核对证据计算 match_reason 与排序分。

    返回 (match_reason 文本, 分数 0..1, 命中关键词列表)。分数只用于排序，不对外宣称相关性。
    """
    text = " ".join(
        [candidate.get("stem", ""), candidate.get("knowledge_point") or ""]
        + [o.get("text", "") for o in candidate.get("options", [])]
    ).lower()
    hits = [k for k in keywords if _term_hit(k, text)]
    parts: list[str] = []
    score = 0.0
    if hits:
        parts.append("命中关键词：" + "、".join(hits[:6]))
        score += 0.6 * (len(hits) / max(len(keywords), 1)) + 0.2
    else:
        parts.append("未命中查询关键词（来源站内结果）")
    if type_filter and candidate.get("type") in type_filter:
        parts.append(f"题型匹配：{candidate.get('type')}")
        score += 0.1
    score += 0.2 * float(candidate.get("extract_confidence") or 0.0)
    return "；".join(parts), round(min(score, 1.0), 4), hits
