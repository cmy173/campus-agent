"""复习卷导出渲染（#48）：Markdown 完整卷 / 学生版 HTML / 答案版 HTML。

安全约束（红线）：
- 全部文本经 HTML 转义；网络出处仅 http(s) 渲染为链接（防 javascript: 注入）；
- 不含密钥、服务端本地路径与调试材料；仅使用试卷契约字段与快照标题。
"""
from __future__ import annotations

import html
import re
from typing import Any

QUESTION_TYPE_LABELS = {
    "single_choice": "单选题",
    "multiple_choice": "多选题",
    "true_false": "判断题",
    "fill_blank": "填空题",
    "calculation": "计算题",
    "short_answer": "简答/证明题",
    "proof": "证明题",
}
MODE_LABELS = {"ai": "AI 生成", "bank": "网络题库", "hybrid": "混合（题库优先 + AI 补缺）"}
DIFFICULTY_LABELS = {"easy": "易", "medium": "中", "hard": "难"}
REASON_LABELS = {"initial": "首次生成", "revise": "按修改重做", "regenerate": "原参数重做"}


def _answer_text(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer
    if isinstance(answer, list):
        return "；".join(str(item) for item in answer)
    return str(answer)


def _snapshot_titles(paper: dict[str, Any]) -> dict[tuple[str, str], str]:
    titles: dict[tuple[str, str], str] = {}
    for item in (paper.get("snapshot") or {}).get("items") or []:
        if item.get("resource_id"):
            titles[("resource", str(item["resource_id"]))] = str(item.get("title") or item["resource_id"])
        if item.get("assignment_id"):
            titles[("assignment", str(item["assignment_id"]))] = str(item.get("title") or item["assignment_id"])
    return titles


def _ref_label(ref: dict[str, Any], titles: dict[tuple[str, str], str]) -> str:
    kind = ref.get("kind")
    if kind == "resource":
        title = titles.get(("resource", str(ref.get("resource_id")))) or str(ref.get("resource_id") or "课件")
        locator = ref.get("locator")
        return f"课件《{title}》" + (f"·{locator}" if locator else "")
    if kind == "assignment":
        title = titles.get(("assignment", str(ref.get("assignment_id")))) or str(ref.get("assignment_id") or "作业")
        return f"作业《{title}》"
    if kind == "note":
        return str(ref.get("locator") or "备注")
    if kind == "web":
        return str(ref.get("site_name") or "网络原题")
    return str(kind or "出处")


def _safe_url(url: Any) -> str | None:
    text = str(url or "")
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None


def _questions(paper: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(paper.get("questions") or [], key=lambda q: int(q.get("ordinal") or 0))


def _meta_pairs(paper: dict[str, Any], course_title: str | None) -> list[tuple[str, str]]:
    constraints = paper.get("constraints") or {}
    summary = paper.get("summary") or {}
    count = summary.get("question_count", len(paper.get("questions") or []))
    total = summary.get("total_points")
    pairs = [
        ("课程", course_title or str(paper.get("course_id") or "")),
        ("模式", MODE_LABELS.get(str(paper.get("mode")), str(paper.get("mode") or ""))),
        ("题数/总分", f"{count} 题 / {total if total is not None else '—'} 分"),
    ]
    if constraints.get("duration_minutes"):
        pairs.append(("建议时长", f"{constraints['duration_minutes']} 分钟"))
    reason = REASON_LABELS.get(str(paper.get("revision_reason")), str(paper.get("revision_reason") or ""))
    pairs.append(("版本", f"v{paper.get('revision')}（{reason}）"))
    # The delivered file must be auditable on its own: keep the frozen material snapshot id and
    # fingerprint next to the version so a reader can match this export back to the exact frozen
    # material without a second API call. Absent snapshot data is omitted, never invented.
    snapshot = paper.get("snapshot") or {}
    if snapshot.get("id"):
        pairs.append(("材料快照", str(snapshot["id"])))
    if snapshot.get("fingerprint"):
        pairs.append(("材料指纹", str(snapshot["fingerprint"])))
    engine_version = paper.get("engine_version") or snapshot.get("engine_version")
    if engine_version:
        pairs.append(("出卷引擎", str(engine_version)))
    pairs.append(("生成时间", str(paper.get("created_at") or "")))
    return pairs


def _question_heading(q: dict[str, Any]) -> str:
    type_label = QUESTION_TYPE_LABELS.get(str(q.get("question_type")), str(q.get("question_type") or ""))
    difficulty = DIFFICULTY_LABELS.get(str(q.get("difficulty")), "")
    parts = [type_label, f"{q.get('points')} 分"]
    if difficulty:
        parts.append(f"难度 {difficulty}")
    return " · ".join(part for part in parts if part)


def export_filename(title: str, fmt: str) -> str:
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(title or "复习卷")).strip(" ._") or "复习卷"
    suffix = {"markdown": "完整卷.md", "student": "学生版.html", "answers": "答案版.html"}[fmt]
    return f"{base}-{suffix}"


def render_markdown(paper: dict[str, Any], course_title: str | None) -> str:
    titles = _snapshot_titles(paper)
    lines: list[str] = [f"# {paper.get('title') or '复习卷'}", ""]
    for key, value in _meta_pairs(paper, course_title):
        lines.append(f"- **{key}**：{value}")
    lines.append("")
    for q in _questions(paper):
        lines.append(f"## {q.get('ordinal')}. {q.get('stem')}")
        lines.append("")
        lines.append(f"*{_question_heading(q)}*")
        lines.append("")
        for option in q.get("options") or []:
            lines.append(f"- {option.get('key')}. {option.get('text')}")
        if q.get("options"):
            lines.append("")
        lines.append(f"**答案**：{_answer_text(q.get('answer'))}")
        lines.append("")
        if q.get("explanation"):
            lines.append(f"**解析**：{q.get('explanation')}")
            lines.append("")
        grading = q.get("grading_points") or []
        if grading:
            lines.append("**评分点**：")
            lines.extend(f"- {point}" for point in grading)
            lines.append("")
        refs = q.get("source_refs") or []
        if refs:
            labels = []
            for ref in refs:
                label = _ref_label(ref, titles)
                url = _safe_url(ref.get("url"))
                if ref.get("kind") == "web" and url:
                    label = f"[{label}]({url})"
                labels.append(label)
            lines.append("**出处**：" + "；".join(labels))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_CSS = """\
:root { color-scheme: light; }
body { font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; margin: 0 auto; max-width: 820px; padding: 24px 32px; color: #1f2937; line-height: 1.7; }
h1 { font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }
h2 { font-size: 16px; margin: 22px 0 6px; }
.meta { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 20px; color: #4b5563; font-size: 13px; margin: 8px 0 18px; }
.question { border-top: 1px solid #e5e7eb; padding: 14px 0; break-inside: avoid; }
.tag { font-size: 13px; color: #6b7280; margin: 2px 0 8px; }
.options { list-style: none; padding: 0; margin: 6px 0; }
.options li { margin: 3px 0; }
.block { margin: 8px 0; padding: 8px 12px; background: #f3f4f6; border-radius: 6px; }
.block.answer { background: #ecfdf5; }
.refs { font-size: 13px; color: #6b7280; margin-top: 8px; }
a { color: #2563eb; }
@media print { body { padding: 0; } }
"""


def _refs_html(refs: list[dict[str, Any]], titles: dict[tuple[str, str], str]) -> str:
    parts = []
    for ref in refs:
        label = html.escape(_ref_label(ref, titles))
        url = _safe_url(ref.get("url"))
        if ref.get("kind") == "web" and url:
            parts.append(f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{label}</a>')
        else:
            parts.append(label)
    return f'<div class="refs">出处：{"；".join(parts)}</div>'


def _render_html(paper: dict[str, Any], course_title: str | None, *, with_answers: bool) -> str:
    titles = _snapshot_titles(paper)
    meta_html = "".join(f"<li><b>{html.escape(key)}</b>{html.escape(value)}</li>" for key, value in _meta_pairs(paper, course_title))
    blocks: list[str] = []
    for q in _questions(paper):
        parts = [
            f"<section class=\"question\">",
            f"<h2>{q.get('ordinal')}. {html.escape(str(q.get('stem') or ''))}</h2>",
            f'<div class="tag">{html.escape(_question_heading(q))}</div>',
        ]
        if q.get("options"):
            items = "".join(
                f"<li>{html.escape(str(option.get('key') or ''))}. {html.escape(str(option.get('text') or ''))}</li>"
                for option in q["options"]
            )
            parts.append(f'<ul class="options">{items}</ul>')
        if with_answers:
            answer = html.escape(_answer_text(q.get("answer")))
            parts.append(f'<div class="block answer"><b>答案：</b>{answer}</div>')
            if q.get("explanation"):
                parts.append(f'<div class="block"><b>解析：</b>{html.escape(str(q["explanation"]))}</div>')
            grading = q.get("grading_points") or []
            if grading:
                points = "；".join(html.escape(str(point)) for point in grading)
                parts.append(f'<div class="block"><b>评分点：</b>{points}</div>')
        if q.get("source_refs"):
            parts.append(_refs_html(q["source_refs"], titles))
        parts.append("</section>")
        blocks.append("".join(parts))
    title = html.escape(str(paper.get("title") or "复习卷"))
    kind = "答案版" if with_answers else "学生版"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}（{kind}）</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<h1>{title}（{kind}）</h1>\n"
        f'<ul class="meta">{meta_html}</ul>\n'
        + "\n".join(blocks) + "\n</body>\n</html>\n"
    )


def render_student_html(paper: dict[str, Any], course_title: str | None) -> str:
    return _render_html(paper, course_title, with_answers=False)


def render_answers_html(paper: dict[str, Any], course_title: str | None) -> str:
    return _render_html(paper, course_title, with_answers=True)


RENDERERS = {
    "markdown": ("text/markdown; charset=utf-8", render_markdown),
    "student": ("text/html; charset=utf-8", render_student_html),
    "answers": ("text/html; charset=utf-8", render_answers_html),
}
