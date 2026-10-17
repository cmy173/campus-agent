"""DOCX 解析（zipfile + ElementTree，纯标准库）。

- 段落 → 片段；locator 为 text 偏移（对“全文拼接串”的字符偏移，全文=各片段以 "\\n" 连接）；
- 标题层级：w:outlineLvl 优先，其次段落样式（Heading N / 标题 N / 数字 1-6）——启发式；
- 表格 → 单片段（行内单元格以 " | " 连接，合并单元格结构可能不完整，注记说明）；
- 图片、公式（OMML）不提取内容，计数并注记；纯图片文档 → needs_ocr。
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Callable
from xml.etree import ElementTree as ET

from .assemble import ChunkBuilder, ParserOutput
from .types import ParseErrorCode, ParseFailure, ParseLimits

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

MIN_TEXT_CHARS = 20

_STYLE_HEADING_RE = re.compile(r"^(?:heading|标题)[\s_-]*([1-6])$", re.IGNORECASE)
_TABLE_ROW_SEP = " | "


def _paragraph_text(para: ET.Element) -> str:
    parts: list[str] = []
    for node in para.iter():
        if node.tag == f"{{{W}}}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{{{W}}}tab":
            parts.append(" ")
        elif node.tag == f"{{{W}}}br":
            parts.append("\n")
    return "".join(parts).strip()


def _heading_level(para: ET.Element) -> int | None:
    ppr = para.find(f"{{{W}}}pPr")
    if ppr is None:
        return None
    outline = ppr.find(f"{{{W}}}outlineLvl")
    if outline is not None:
        value = outline.get(f"{{{W}}}val")
        if value is not None and value.isdigit():
            return int(value) + 1
    style = ppr.find(f"{{{W}}}pStyle")
    if style is not None:
        value = (style.get(f"{{{W}}}val") or "").strip()
        match = _STYLE_HEADING_RE.match(value)
        if match:
            return int(match.group(1))
        if value in {"1", "2", "3", "4", "5", "6"}:  # 中文 Word 模板的标题样式 ID
            return int(value)
    return None


def _table_text(table: ET.Element) -> tuple[str, bool]:
    merged = False
    lines: list[str] = []
    for row in table.findall(f"{{{W}}}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{{{W}}}tc"):
            tcpr = cell.find(f"{{{W}}}tcPr")
            if tcpr is not None and (
                tcpr.find(f"{{{W}}}gridSpan") is not None or tcpr.find(f"{{{W}}}vMerge") is not None
            ):
                merged = True
            texts = [_paragraph_text(p) for p in cell.findall(f"{{{W}}}p")]
            cells.append(" ".join(part for part in texts if part))
        line = _TABLE_ROW_SEP.join(cells).strip()
        if line.strip(_TABLE_ROW_SEP).strip():
            lines.append(line)
    return "\n".join(lines), merged


def _body_blocks(body: ET.Element) -> list[tuple[str, ET.Element]]:
    """按文档顺序取段落与表格（递归展开 w:sdt 包装）。"""
    blocks: list[tuple[str, ET.Element]] = []
    for child in body:
        tag = child.tag
        if tag == f"{{{W}}}p":
            blocks.append(("p", child))
        elif tag == f"{{{W}}}tbl":
            blocks.append(("tbl", child))
        elif tag == f"{{{W}}}sdt":
            content = child.find(f"{{{W}}}sdtContent")
            if content is not None:
                blocks.extend(_body_blocks(content))
    return blocks


def parse_docx(data: bytes, limits: ParseLimits, check_deadline: Callable[[], None]) -> ParserOutput:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise ParseFailure(ParseErrorCode.CORRUPT_FILE, "DOCX 无法读取（文件可能损坏）") from None
    with archive:
        total_uncompressed = sum(item.file_size for item in archive.infolist())
        if total_uncompressed > limits.max_uncompressed_bytes:
            raise ParseFailure(
                ParseErrorCode.ZIP_LIMIT_EXCEEDED,
                f"DOCX 解压后总量 {total_uncompressed} 字节超过上限 {limits.max_uncompressed_bytes}",
            )
        try:
            root = ET.fromstring(archive.read("word/document.xml"))
        except KeyError:
            raise ParseFailure(
                ParseErrorCode.CORRUPT_FILE,
                "DOCX 缺少 word/document.xml，文件可能不是有效 Word 文档",
            ) from None
        except ET.ParseError:
            raise ParseFailure(ParseErrorCode.CORRUPT_FILE, "word/document.xml XML 无法解析") from None

        body = root.find(f"{{{W}}}body")
        if body is None:
            raise ParseFailure(ParseErrorCode.CORRUPT_FILE, "DOCX 缺少 body 结构")

        builder = ChunkBuilder(limits.max_chunks)
        total_chars = 0
        offset = 0
        paragraph_count = 0
        masked = False
        table_count = 0
        merged_tables = False
        for block_kind, element in _body_blocks(body):
            check_deadline()
            if block_kind == "p":
                text = _paragraph_text(element)
                if not text:
                    continue
                paragraph_count += 1
                level = _heading_level(element)
                if len(text) > limits.max_chars_per_page:
                    text = text[: limits.max_chars_per_page]
                    masked = True
                chunk_kind = "body"
            else:
                text, merged = _table_text(element)
                if not text:
                    continue
                table_count += 1
                merged_tables = merged_tables or merged
                level = None
                chunk_kind = "table"
            locator = {"kind": "text", "start_offset": offset, "end_offset": offset + len(text)}
            chunk = builder.add(text, locator, kind=chunk_kind, heading_level=level)
            if chunk is not None:
                offset = locator["end_offset"] + 1  # 全文拼接以 "\n" 连接
            total_chars += len(text)

        image_count = len(list(root.iter(f"{{{W}}}drawing"))) + len(list(root.iter(f"{{{W}}}pict")))
        formula_count = len(list(root.iter(f"{{{M}}}oMath")))

    output = builder.finish(units=paragraph_count, chars=total_chars, needs_ocr=total_chars < MIN_TEXT_CHARS)
    if image_count:
        output.limitations.append(f"文档含 {image_count} 处图片，未提取图像内容（可按文本偏移回看原文）")
    if formula_count:
        output.limitations.append(f"文档含 {formula_count} 处公式（OMML），未能可靠提取为文本，正文可能缺公式内容")
    if table_count:
        note = f"文档含 {table_count} 个表格，已按行以“{_TABLE_ROW_SEP.strip()}”连接单元格提取；"
        note += "合并/跨行单元格结构可能不完整" if merged_tables else "结构信息可能不完整"
        output.limitations.append(note)
    if masked:
        output.limitations.append(f"个别段落超过 {limits.max_chars_per_page} 字符被截断")
    return output
