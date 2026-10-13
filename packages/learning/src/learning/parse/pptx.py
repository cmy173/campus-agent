"""PPTX 解析（zipfile + ElementTree，纯标准库）。

- 幻灯片号从 1 开始（locator: slide）；标题占位符（title/ctrTitle）作为 1 级标题；
- 表格按行以 " | " 连接单元格提取（合并单元格结构可能不完整，必要时以注记说明）；
- 图片不提取内容；公式（a14:m / m:oMath）计数并注记；
- 演讲者备注（notesSlide）作为 kind="notes" 片段附加到对应幻灯片；
- 全部幻灯片均无可提取文本（图片型/扫描）→ needs_ocr，不作为失败。
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from typing import Callable
from xml.etree import ElementTree as ET

from .assemble import ChunkBuilder, ParserOutput
from .chunking import cap_text
from .types import ParseErrorCode, ParseFailure, ParseLimits

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A14 = "http://schemas.microsoft.com/office/drawing/2010/main"

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
MIN_TEXT_CHARS = 20

_TABLE_ROW_SEP = " | "


def _paragraph_text(para: ET.Element) -> str:
    return "".join(t.text or "" for t in para.iter(f"{{{A}}}t")).strip()


def _shape_paragraphs(shape: ET.Element) -> list[str]:
    texts: list[str] = []
    for para in shape.findall(f"./{{{P}}}txBody/{{{A}}}p"):
        text = _paragraph_text(para)
        if text:
            texts.append(text)
    return texts


def _table_text(table: ET.Element) -> str:
    lines: list[str] = []
    for row in table.findall(f"{{{A}}}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{{{A}}}tc"):
            cell_parts = [_paragraph_text(p) for p in cell.findall(f".//{{{A}}}p")]
            cells.append(" ".join(part for part in cell_parts if part))
        line = _TABLE_ROW_SEP.join(cells).strip()
        if line.strip(_TABLE_ROW_SEP).strip():
            lines.append(line)
    return "\n".join(lines)


def parse_pptx(data: bytes, limits: ParseLimits, check_deadline: Callable[[], None]) -> ParserOutput:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise ParseFailure(ParseErrorCode.CORRUPT_FILE, "PPTX 无法读取（文件可能损坏）") from None
    with archive:
        total_uncompressed = sum(item.file_size for item in archive.infolist())
        if total_uncompressed > limits.max_uncompressed_bytes:
            raise ParseFailure(
                ParseErrorCode.ZIP_LIMIT_EXCEEDED,
                f"PPTX 解压后总量 {total_uncompressed} 字节超过上限 {limits.max_uncompressed_bytes}",
            )
        slides: list[tuple[int, str]] = []
        for name in archive.namelist():
            match = _SLIDE_RE.match(name)
            if match:
                slides.append((int(match.group(1)), name))
        if not slides:
            raise ParseFailure(
                ParseErrorCode.CORRUPT_FILE,
                "PPTX 中未找到幻灯片（ppt/slides/slideN.xml），文件可能不是有效演示文稿",
            )
        slides.sort()
        if len(slides) > limits.max_pages:
            raise ParseFailure(
                ParseErrorCode.PAGE_LIMIT_EXCEEDED,
                f"幻灯片数 {len(slides)} 超过上限 {limits.max_pages}",
            )

        builder = ChunkBuilder(limits.max_chunks)
        total_chars = 0
        image_count = 0
        formula_count = 0
        table_count = 0
        truncated_slides: list[int] = []
        for number, name in slides:
            check_deadline()
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                raise ParseFailure(ParseErrorCode.CORRUPT_FILE, f"幻灯片 {number} 的 XML 无法解析") from None
            locator = {"kind": "slide", "slide": number}
            slide_chars = 0
            for element in root.iter():
                if element.tag == f"{{{P}}}sp":
                    placeholder = element.find(f"./{{{P}}}nvSpPr/{{{P}}}nvPr/{{{P}}}ph")
                    ph_type = placeholder.get("type", "body") if placeholder is not None else None
                    is_title = ph_type in {"title", "ctrTitle"}
                    for text in _shape_paragraphs(element):
                        remaining = max(0, limits.max_chars_per_page - slide_chars)
                        text, truncated = cap_text(text, remaining)
                        if truncated:
                            truncated_slides.append(number)
                        if not text:
                            continue
                        slide_chars += len(text)
                        total_chars += len(text)
                        builder.add(text, locator, heading_level=1 if is_title else None)
                elif element.tag == f"{{{A}}}tbl":
                    text = _table_text(element)
                    if text:
                        table_count += 1
                        slide_chars += len(text)
                        total_chars += len(text)
                        builder.add(text, locator, kind="table")
                elif element.tag == f"{{{P}}}pic":
                    image_count += 1
                elif element.tag in (f"{{{A14}}}m", f"{{{A}}}oMath"):
                    formula_count += 1
            for text in _notes_texts(archive, number):
                remaining = max(0, limits.max_chars_per_page - slide_chars)
                text, _ = cap_text(text, remaining)
                if text:
                    total_chars += len(text)
                    builder.add(text, locator, kind="notes")

    output = builder.finish(units=len(slides), chars=total_chars, needs_ocr=total_chars < MIN_TEXT_CHARS)
    if image_count:
        output.limitations.append(f"演示文稿含 {image_count} 处图片，未提取图像内容（可按幻灯片号回看原文）")
    if formula_count:
        output.limitations.append(f"演示文稿含 {formula_count} 处公式，未能可靠提取为文本，内容可能缺失")
    if table_count:
        output.limitations.append(
            f"演示文稿含 {table_count} 个表格，已按行以“{_TABLE_ROW_SEP.strip()}”连接单元格提取；合并单元格结构可能不完整"
        )
    if truncated_slides:
        output.limitations.append("部分幻灯片文本超过单张上限被截断（幻灯片号：" +
                                  "、".join(str(n) for n in sorted(set(truncated_slides))) + "）")
    return output


def _notes_texts(archive: zipfile.ZipFile, slide_number: int) -> list[str]:
    """按关系文件定位 notesSlide（无则返回空）。跳过幻灯片编号占位符。"""
    rels_name = f"ppt/slides/_rels/slide{slide_number}.xml.rels"
    try:
        rels = ET.fromstring(archive.read(rels_name))
    except (KeyError, ET.ParseError):
        return []
    target = None
    for rel in rels:
        if rel.get("Type", "").endswith("/notesSlide"):
            target = rel.get("Target")
            break
    if not target:
        return []
    note_name = posixpath.normpath(posixpath.join("ppt/slides", target))
    try:
        notes_root = ET.fromstring(archive.read(note_name))
    except (KeyError, ET.ParseError):
        return []
    texts: list[str] = []
    for shape in notes_root.iter(f"{{{P}}}sp"):
        placeholder = shape.find(f"./{{{P}}}nvSpPr/{{{P}}}nvPr/{{{P}}}ph")
        ph_type = placeholder.get("type") if placeholder is not None else None
        if ph_type == "sldNum":
            continue
        texts.extend(_shape_paragraphs(shape))
    return texts
