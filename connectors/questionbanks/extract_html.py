#!/usr/bin/env python3
"""页面提取引擎：从真实网页中提取题目候选（JSON-LD 优先，正文启发式为辅）。

原则（01-A §3/§4 红线）：
  - **只提取页面真实存在的内容**；搜索摘要、页面标题绝不充当题干；
  - 选项缺失/答案缺失如实保留（选项为空、answer=None），不猜测、不补全；
  - 导航/页脚/推荐位/广告等噪声区域不产出候选；
  - 公式与图片有损时记入 attachments（"题干含图片，正文未含图内容"），不假装已理解；
  - 页面里的"忽略此前指令"等文本只当数据，本模块不做任何指令解释或工具调用。

``extract_page(html, page_url=..., source_id=..., fetched_at=...) -> ExtractionResult``
  1. JSON-LD（schema.org Question）→ 结构化候选（置信度高，locator_kind=jsonld）；
  2. 正文块启发式：题号切分 → 选项/答案/解析标记识别 → 噪声过滤 → 置信度（locator_kind=block）；
  3. 页面级合并：同题干保留字段更全者。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urljoin

from .normalize import (
    collapse_ws,
    find_latex,
    has_numbering,
    infer_type,
    options_to_list,
    resolve_and_sanitize,
    site_of,
    stem_hash,
    strip_numbering,
)
from .types import TYPE_CHOICE, TYPE_OTHER

EXTRACTOR_VERSION = "qb1"

# ---------------------------------------------------------------------------
# HTML → 文本块
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe",
              "nav", "footer", "aside", "header", "form", "select", "button"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
              "meta", "param", "source", "track", "wbr"}
_BLOCK_TAGS = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "blockquote", "pre", "dd", "dt", "figcaption",
               "table", "ul", "ol", "br", "hr", "dl", "main", "body"}

_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_JSONLD_RE = re.compile(
    r"(?is)<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
)


@dataclass
class TextBlock:
    text: str
    tag: str = ""
    images: list[str] = field(default_factory=list)     # 绝对 URL（parse_blocks 内补全）
    formulas: list[str] = field(default_factory=list)   # LaTeX/MathML 片段
    is_heading: bool = False


class _BlockParser(HTMLParser):
    """把 HTML 压平成"块级文本行"（保留图片与公式的伴随信息）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[TextBlock] = []
        self._buf: list[str] = []
        self._images: list[str] = []
        self._formulas: list[str] = []
        self._skip_depth = 0
        self._skip_stack: list[str] = []
        self._current_tag = ""
        self._in_annotation = False
        self._annotation_buf: list[str] = []

    def _flush(self) -> None:
        text = collapse_ws("".join(self._buf))
        if text or self._images or self._formulas:
            self.blocks.append(TextBlock(
                text=text,
                tag=self._current_tag,
                images=self._images[:],
                formulas=self._formulas[:],
                is_heading=self._current_tag in ("h1", "h2", "h3", "h4", "h5", "h6"),
            ))
        self._buf, self._images, self._formulas = [], [], []

    # -- HTMLParser 回调 --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag == "body" and "head" in self._skip_stack:
                # <head> 未闭合时按 HTML5 隐式闭合处理，否则整页会因跳过区永不退出而丢失
                while self._skip_stack:
                    if self._skip_stack.pop() == "head":
                        break
                self._skip_depth = len(self._skip_stack)
            else:
                if tag not in _VOID_TAGS:
                    self._skip_stack.append(tag)
                    self._skip_depth += 1
                return
        if tag in _SKIP_TAGS:
            self._skip_depth = 1
            self._skip_stack = [tag]
            return
        if tag == "img":
            src = dict(attrs).get("src") or ""
            if src:
                self._images.append(src)
            return
        if tag == "annotation":
            self._in_annotation = True
            self._annotation_buf = []
            return
        if tag == "math":
            self._buf.append(" ")
            return
        if tag in ("sub", "sup"):
            self._buf.append("^" if tag == "sup" else "_")
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._current_tag = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in _BLOCK_TAGS:
            self._flush()
            self._current_tag = ""

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if self._skip_stack and tag == self._skip_stack[-1]:
                self._skip_stack.pop()
                self._skip_depth -= 1
            return
        if tag == "annotation":
            self._in_annotation = False
            latex = collapse_ws("".join(self._annotation_buf))
            if latex:
                self._formulas.append(latex)
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._current_tag = ""

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_annotation:
            self._annotation_buf.append(data)
            return
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def parse_blocks(html: str, page_url: str) -> tuple[list[TextBlock], list[str]]:
    """HTML → 文本块列表 + 解析问题说明（"无法解析"不静默）。"""
    parser = _BlockParser()
    problems: list[str] = []
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # 坏 HTML 绝不导致整页失败
        problems.append(f"HTML 解析中断（已提取部分内容）: {type(exc).__name__}")
    blocks = parser.blocks
    for b in blocks:
        b.images = [urljoin(page_url, src) for src in b.images]
        for latex in find_latex(b.text):
            if latex not in b.formulas:
                b.formulas.append(latex)
    return blocks, problems


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------

_EDU_TYPE_MAP = {
    "multiple choice": "choice", "multiplechoice": "choice", "single choice": "choice",
    "true/false": "judgment", "truefalse": "judgment", "boolean": "judgment",
    "fill in the blank": "fill_blank", "fillintheblank": "fill_blank",
    "short answer": "short_answer", "shortanswer": "short_answer",
    "calculation": "calculation", "proof": "proof",
}


def _walk_jsonld(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _answer_text(node: Any) -> Optional[str]:
    if isinstance(node, dict):
        text = node.get("text") or node.get("name")
        return collapse_ws(str(text)) if text else None
    if isinstance(node, str) and node.strip():
        return collapse_ws(node)
    return None


def _jsonld_is_question(node: dict[str, Any]) -> bool:
    ntype = node.get("@type") or node.get("type") or ""
    types = [str(t).lower() for t in ntype] if isinstance(ntype, list) else [str(ntype).lower()]
    return "question" in types


def _from_jsonld_node(node: dict[str, Any], index: int) -> Optional[dict[str, Any]]:
    stem = collapse_ws(str(node.get("name") or node.get("text") or ""))
    if not stem:
        return None
    answer = _answer_text(node.get("acceptedAnswer")) or _answer_text(node.get("suggestedAnswer"))
    edu = node.get("eduQuestionType") or node.get("learningResourceType") or ""
    declared = _EDU_TYPE_MAP.get(str(edu).strip().lower()) if edu else None
    raw_options = node.get("suggestedAnswer") if isinstance(node.get("suggestedAnswer"), list) else None
    options = options_to_list(raw_options) if raw_options else []
    analysis = None
    if isinstance(node.get("acceptedAnswer"), dict):
        analysis = _answer_text(node["acceptedAnswer"].get("comment"))
    return {"stem": strip_numbering(stem), "answer": answer, "analysis": analysis,
            "options": options, "declared_type": declared, "confidence_base": 0.85,
            "locator_kind": "jsonld", "locator_index": index,
            "images": [], "formulas": []}


def extract_jsonld(html: str) -> tuple[list[dict[str, Any]], list[str]]:
    """从 JSON-LD 提取候选；解析失败记录问题说明（不静默丢弃）。"""
    found: list[dict[str, Any]] = []
    problems: list[str] = []
    for idx, m in enumerate(_JSONLD_RE.finditer(html)):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            problems.append(f"JSON-LD 块 #{idx} 不是合法 JSON（已跳过）")
            continue
        for node in _walk_jsonld(payload):
            if isinstance(node, dict) and _jsonld_is_question(node):
                cand = _from_jsonld_node(node, len(found))
                if cand:
                    found.append(cand)
    return found, problems


# ---------------------------------------------------------------------------
# 启发式区域提取
# ---------------------------------------------------------------------------

_QUESTION_START_RE = re.compile(
    r"^(?:"
    r"\d{1,2}\s*[、.．)）](?=[^\d]|$)"
    r"|第\s*\d{1,3}\s*题[.、．:：]?"
    r"|Q(?:uestion)?\s*\d{1,3}\s*[.、．:：)）]"
    r"|[（(]\s*\d{1,3}\s*[)）]"
    r"|[一二三四五六七八九十]{1,2}\s*[、.．)）]"
    r")\s*",
    re.I,
)
_OPTION_START_RE = re.compile(r"^([A-Ha-h])\s*[、.．)）:：]\s*(.*)$", re.S)
_OPTION_PAREN_RE = re.compile(r"^[（(]\s*([A-Ha-h])\s*[)）]\s*(.*)$", re.S)
_ANSWER_LINE_RE = re.compile(
    r"^(?:参考答案|正确答案|答案与解析|答案|answer)\s*[:：]?\s*(.*)$", re.I)
_ANALYSIS_LINE_RE = re.compile(
    r"^(?:答案解析|试题解析|解析|分析|解答|explanation|analysis)\s*[:：]?\s*(.*)$", re.I)
_MARKER_INLINE_RE = re.compile(
    r"[【\[](答案|解析|参考答案|答案解析|考点|解答)[】\]]\s*[:：]?", re.I)

# 噪声特征（导航/推荐/版权/登录引导）；仅在**短行**上判定，避免误伤含此类词的正常题干
_NOISE_PATTERNS = re.compile(
    r"(相关推荐|相关阅读|热门推荐|猜你喜欢|上一篇|下一篇|返回列表|返回首页|版权|"
    r"登录后|请登录|注册账号|扫码|下载APP|关注我们|联系我们|免责声明|举报|"
    r"阅读全文|点击查看|更多精彩|阅读更多|查看全部|展开全文|广告位|赞助商|"
    r"previous|next|related posts|read more|copyright|all rights reserved)",
    re.I,
)
_QUESTION_HINT_VERB_RE = re.compile(
    r"(简述|论述|试述|说明|写出|列举|阐述|计算|求解|求值|证明|求证|判断|选择|填空|下列|以下|"
    r"试分析|试比较|求)")
_QUESTION_MARK_RE = re.compile(r"[？?]\s*$")


def _strip_markers(text: str) -> str:
    """把行内【答案】/【解析】标记转为"答案：/解析："行首形态，便于统一识别。"""
    return _MARKER_INLINE_RE.sub(lambda m: f"{m.group(1)}：", text)


def _is_noise_line(text: str) -> bool:
    return bool(_NOISE_PATTERNS.search(text)) and len(text) < 40 and not has_numbering(text)


def _extract_region_fields(region: list[TextBlock]) -> dict[str, Any]:
    """一题区域（块序列）→ stem/options/answer/analysis/图片/公式（状态机）。"""
    texts = [_strip_markers(b.text.strip()) for b in region]
    images: list[str] = []
    formulas: list[str] = []
    for b in region:
        images.extend(b.images)
        formulas.extend(b.formulas)

    stem_parts: list[str] = []
    options: list[dict[str, str]] = []
    answer: Optional[str] = None
    analysis: Optional[str] = None
    phase = "stem"
    current_opt: Optional[dict[str, str]] = None
    i, n = 0, len(region)

    def _option_image_note(block: TextBlock) -> None:
        if block.images and current_opt is not None:
            current_opt["text"] = collapse_ws(current_opt["text"] + " [图片]")

    while i < n:
        text = texts[i]
        block = region[i]
        if not text:
            _option_image_note(block)
            i += 1
            continue
        m_ans = _ANSWER_LINE_RE.match(text)
        m_ana = _ANALYSIS_LINE_RE.match(text)
        m_opt = _OPTION_START_RE.match(text) or _OPTION_PAREN_RE.match(text)

        if m_ans and (phase != "stem" or stem_parts):
            if current_opt is not None:
                options.append(current_opt)
                current_opt = None
            body = collapse_ws(m_ans.group(1))
            if not body and i + 1 < n:
                nxt = texts[i + 1]
                if nxt and not _ANALYSIS_LINE_RE.match(nxt) and not (
                        _OPTION_START_RE.match(nxt) or _OPTION_PAREN_RE.match(nxt)):
                    body = collapse_ws(nxt)
                    i += 1
            answer = collapse_ws(f"{answer} {body}") if answer else (body or None)
            phase = "tail"
            i += 1
            continue
        if m_ana and (phase != "stem" or stem_parts):
            if current_opt is not None:
                options.append(current_opt)
                current_opt = None
            body = collapse_ws(m_ana.group(1))
            analysis = collapse_ws(f"{analysis} {body}") if analysis else (body or None)
            phase = "tail"
            i += 1
            continue
        if m_opt and phase in ("stem", "options"):
            if current_opt is not None:
                options.append(current_opt)
            current_opt = {"label": m_opt.group(1).upper(), "text": collapse_ws(m_opt.group(2))}
            _option_image_note(block)
            phase = "options"
            i += 1
            continue
        if phase == "options" and current_opt is not None and len(current_opt["text"]) < 400:
            current_opt["text"] = collapse_ws(current_opt["text"] + " " + text)
            _option_image_note(block)
            i += 1
            continue
        if phase == "tail":
            if analysis is not None and len(analysis) < 800:
                analysis = collapse_ws(analysis + " " + text)
            elif answer is not None and len(answer) < 200:
                answer = collapse_ws(answer + " " + text)
            i += 1
            continue
        if phase == "stem":
            stem_parts.append(strip_numbering(text) if not stem_parts else text)
            _option_image_note(block)
            i += 1
            continue
        # phase == "options" 且选项文本已过长：丢弃残余行
        i += 1

    if current_opt is not None:
        options.append(current_opt)
    stem = collapse_ws(" ".join(stem_parts))
    if len(stem) > 800:
        stem = stem[:800] + "…（题干过长已截断）"
    return {"stem": stem, "options": options, "answer": answer,
            "analysis": analysis, "images": images, "formulas": formulas}


def _score_region(stem: str, options: list[dict[str, str]], answer: Optional[str],
                  has_answer_marker: bool, numbered: bool) -> tuple[float, bool]:
    """置信度与"是否保留"判定：保留必须有明确证据（选项/答案/问号/动词），不靠猜。"""
    score = 0.30
    if numbered:
        score += 0.12
    if len(options) >= 2:
        score += 0.20
    if answer is not None or has_answer_marker:
        score += 0.15
    if _QUESTION_MARK_RE.search(stem):
        score += 0.08
    if _QUESTION_HINT_VERB_RE.search(stem):
        score += 0.10
    if 6 <= len(stem) <= 400:
        score += 0.05
    question_like = (
        len(options) >= 2
        or answer is not None
        or has_answer_marker
        or bool(_QUESTION_MARK_RE.search(stem))
        or bool(_QUESTION_HINT_VERB_RE.search(stem))
    )
    return min(score, 0.98), question_like


def extract_from_blocks(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    """正文块启发式提取：按题号切分区域 → 解析字段 → 噪声过滤 → 置信度。"""
    regions: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    for b in blocks:
        text = b.text.strip()
        if not text and not b.images:
            continue
        if _is_noise_line(text):
            continue
        if _QUESTION_START_RE.match(text):
            if current:
                regions.append(current)
            current = [b]
            continue
        if not current:
            continue  # 题号前的导语/介绍块不进入区域
        current.append(b)
    if current:
        regions.append(current)

    results: list[dict[str, Any]] = []
    for region in regions:
        parsed = _extract_region_fields(region)
        stem = parsed.get("stem", "")
        if not stem:
            continue
        if _NOISE_PATTERNS.search(stem) and len(stem) < 40:
            continue
        numbered = has_numbering(region[0].text.strip())
        has_answer_marker = any(
            _ANSWER_LINE_RE.match(_strip_markers(b.text.strip()))
            for b in region if b.text.strip()
        )
        confidence, keep = _score_region(stem, parsed.get("options", []),
                                         parsed.get("answer"), has_answer_marker, numbered)
        if not keep or confidence < 0.35:
            continue
        results.append({
            "stem": stem,
            "options": parsed.get("options", []),
            "answer": parsed.get("answer"),
            "analysis": parsed.get("analysis"),
            "images": parsed.get("images", []),
            "formulas": parsed.get("formulas", []),
            "confidence_base": confidence,
            "locator_kind": "block",
            "locator_index": len(results),
        })
    return results


# ---------------------------------------------------------------------------
# 页面级整合
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # 提取过程说明（含无法解析项）

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": self.candidates, "notes": self.notes}


def looks_like_login(html: str) -> bool:
    """登录/认证页识别：密码输入框为强信号；登录+账号/密码关键词为辅助信号。"""
    lowered = html.lower()
    if re.search(r"type\s*=\s*[\"']password[\"']", lowered):
        return True
    if re.search(r"(登录|登陆|sign\s*in|log\s*in)", lowered) and re.search(
            r"(用户名|账号|密码|username|password)", lowered):
        return True
    return False


def merge_candidates(primary: list[dict[str, Any]],
                     secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并 JSON-LD 与启发式候选：同题干合并字段（JSON-LD 缺失字段用启发式补全）。"""
    seen: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for cand in primary + secondary:
        key = stem_hash(cand.get("stem", ""), cand.get("options", []))
        if key in seen:
            keep = seen[key]
            if not keep.get("options") and cand.get("options"):
                keep["options"] = cand["options"]
            if not keep.get("answer") and cand.get("answer"):
                keep["answer"] = cand["answer"]
            if not keep.get("analysis") and cand.get("analysis"):
                keep["analysis"] = cand["analysis"]
            continue
        seen[key] = cand
        out.append(cand)
    return out


def extract_page(
    html: str,
    *,
    page_url: str,
    source_id: str,
    fetched_at: str,
    type_filter: Optional[list[str]] = None,
) -> ExtractionResult:
    """单页提取入口：产出与 01-A §4 字段对齐的候选 dict（不含抓取与转录）。"""
    result = ExtractionResult()
    jsonld_cands, jsonld_problems = extract_jsonld(html)
    result.notes.extend(jsonld_problems)
    blocks, block_problems = parse_blocks(html, page_url)
    result.notes.extend(block_problems)
    heuristic_cands = extract_from_blocks(blocks)

    merged = merge_candidates(jsonld_cands, heuristic_cands)
    page_title = ""
    m = _TITLE_RE.search(html)
    if m:
        page_title = collapse_ws(re.sub(r"<[^>]+>", "", m.group(1)))

    site = site_of(page_url)
    safe_url = resolve_and_sanitize(page_url, page_url)
    final: list[dict[str, Any]] = []
    for cand in merged:
        stem = cand.get("stem", "")
        options = cand.get("options", [])
        confidence = float(cand.get("confidence_base", 0.5))
        qtype = infer_type(stem, options, declared=cand.get("declared_type"))
        if type_filter and qtype not in type_filter:
            continue
        attachments: list[dict[str, Any]] = []
        for img in cand.get("images", [])[:10]:
            attachments.append({"kind": "image", "url": resolve_and_sanitize(page_url, img),
                                "note": "题干区域含图片，正文未含图内容"})
        for latex in cand.get("formulas", [])[:10]:
            attachments.append({"kind": "formula", "text": latex})
        if (len(options) < 2 and qtype in (TYPE_CHOICE, TYPE_OTHER)
                and re.search(r"(下列|以下)", stem) and _QUESTION_HINT_VERB_RE.search(stem)):
            attachments.append({"kind": "note",
                                "note": "题干含「下列/以下」但未提取到选项；选项可能为图片或未按标记排版，需人工核对"})
            confidence = max(confidence - 0.1, 0.05)
        if qtype == TYPE_OTHER and not (
                cand.get("answer") or options or attachments or _QUESTION_MARK_RE.search(stem)):
            continue  # "other" 且无任何佐证（无选项/答案/附件/问号）：不可用候选
        candidate: dict[str, Any] = {
            "stem": stem,
            "type": qtype,
            "options": options,
            "answer": cand.get("answer"),
            "analysis": cand.get("analysis"),
            "knowledge_point": None,
            "difficulty": None,
            "source_url": safe_url,
            "source_site": site,
            "source_locator": {
                "kind": cand.get("locator_kind", "block"),
                "index": cand.get("locator_index"),
                "page_title": page_title,
            },
            "fetched_at": fetched_at,
            "origin": "online",
            "license_note": "来源页面未声明许可；默认仅供个人学习使用，保留来源链接",
            "source_id": source_id,
            "attachments": attachments,
            "extractor_version": EXTRACTOR_VERSION,
            "extract_confidence": round(min(confidence, 0.98), 3),
        }
        final.append(candidate)
    result.candidates = final
    return result
