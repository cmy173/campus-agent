"""文本块切分与标题启发式（PDF/DOCX 共用；纯函数、确定性）。

口径（写在文档与测试里，不做未验证的夸大）：
- 块切分：先按空行分段；过短块（< min_chars）向后合并；过长块（> max_chars）按行边界切分。
- 标题启发式：单行、≤40 字符、非句末标点结尾，且匹配中英文常见编号样式；
  仅为启发式，识别失败只影响层级展示，不丢正文（正文与定位始终保留）。
"""

from __future__ import annotations

import re

MIN_BLOCK_CHARS = 40
MAX_BLOCK_CHARS = 1500
MAX_HEADING_CHARS = 40

_HEADING_RULES: list[tuple[re.Pattern[str], int | None]] = [
    (re.compile(r"^第[一二三四五六七八九十百零\d]+[章篇部]"), 1),
    (re.compile(r"^第[一二三四五六七八九十百零\d]+[节讲]"), 2),
    (re.compile(r"^(\d+(?:\.\d+)+)[\s、]+?\S"), None),  # 1.1 / 2.3.1：层级=点数+1
    (re.compile(r"^\d+[\s、]+\S"), 1),
    (re.compile(r"^[一二三四五六七八九十]+[、.．]\s*\S"), 1),
]

_SENTENCE_END = "。！？!?;；，,"


def split_blocks(text: str) -> list[str]:
    """按空行切块；每块去掉首尾空白；丢弃空块。"""
    blocks: list[str] = []
    for raw in re.split(r"\n\s*\n", text):
        block = raw.strip()
        if block:
            blocks.append(block)
    return blocks


def _is_heading_block(block: str) -> bool:
    return "\n" not in block and detect_heading(block) is not None


def _merge_blocks(blocks: list[str], min_chars: int = MIN_BLOCK_CHARS) -> list[str]:
    """过短非标题块向后合并；标题块独立保留（不被合并吞并）。"""
    merged: list[str] = []
    buffer = ""
    for block in blocks:
        if _is_heading_block(block):
            if buffer:
                merged.append(buffer)
                buffer = ""
            merged.append(block)
            continue
        buffer = block if not buffer else buffer + "\n" + block
        if len(buffer) >= min_chars:
            merged.append(buffer)
            buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


def split_long(block: str, max_chars: int = MAX_BLOCK_CHARS) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    parts: list[str] = []
    buffer = ""
    for line in block.split("\n"):
        if buffer and len(buffer) + len(line) + 1 > max_chars:
            parts.append(buffer)
            buffer = line
        else:
            buffer = line if not buffer else buffer + "\n" + line
    if buffer:
        parts.append(buffer)
    # 极端长单行：硬切（保持确定性）
    final: list[str] = []
    for part in parts:
        while len(part) > max_chars:
            final.append(part[:max_chars])
            part = part[max_chars:]
        if part:
            final.append(part)
    return final


def detect_heading(line: str) -> int | None:
    """返回标题层级（1 起）或 None。仅单行、短行、非句末标点结尾参与判定。"""
    stripped = line.strip()
    if not stripped or "\n" in stripped or len(stripped) > MAX_HEADING_CHARS:
        return None
    if stripped[-1] in _SENTENCE_END:
        return None
    for pattern, level in _HEADING_RULES:
        match = pattern.match(stripped)
        if match:
            if level is None:
                return match.group(1).count(".") + 1
            return level
    return None


def cap_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def chunk_text_blocks(text: str) -> list[str]:
    """PDF/通用文本 → 块序列（切分+标题保留合并+长块拆分）。"""
    blocks = split_blocks(text)
    blocks = _merge_blocks(blocks)
    result: list[str] = []
    for block in blocks:
        result.extend(split_long(block))
    return result
