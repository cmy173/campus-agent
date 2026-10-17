"""XLSX 解析（zipfile + ElementTree，纯标准库）。

真实课件里存在 `.xlsx`（如「excel练习.xlsx」）：它是 OOXML 压缩包，与 DOCX 同构，
因此不需要额外依赖，也不应被当作「格式不支持」拒之门外。

- 每个工作表的每一行 → 一个 body 片段，单元格以 " | " 连接（与 DOCX 表格口径一致）；
- locator 记录 sheet 名与行号，便于回答时标注出处；
- 公式只取缓存值（`<v>`），不计算公式；空行跳过；
- 可读文本过少（纯图表/图片表）→ needs_ocr，由上层如实标注，不臆造内容。
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Callable
from xml.etree import ElementTree as ET

from .assemble import ChunkBuilder, ParserOutput
from .types import ParseErrorCode, ParseFailure, ParseLimits

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

MIN_TEXT_CHARS = 20
_CELL_SEP = " | "
_MAX_ROWS_PER_SHEET = 5000  # 防御：畸形表的行数上限，避免内存放大


def _text_of(node: ET.Element) -> str:
    """所有 `t` 子节点文本拼接（共享/内联字符串）。"""
    parts = [t.text for t in node.iter(f"{{{S}}}t") if t.text]
    return "".join(parts).strip()


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    return [_text_of(si) for si in root.findall(f"{{{S}}}si")]


def _column_index(cell_ref: str | None) -> int:
    """A1 → 0、B1 → 1、AA1 → 26；缺引用时排到最后（保持出现顺序）。"""
    letters = re.match(r"([A-Za-z]+)", cell_ref or "")
    if not letters:
        return 10**6
    index = 0
    for char in letters.group(1).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """按 workbook 顺序返回 (sheet 名, worksheet 部件路径)；失败时退回单表。"""
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return []
    targets: dict[str, str] = {}
    for rel in rels:
        rel_id, target = rel.get("Id"), rel.get("Target") or ""
        if rel_id and target:
            target = target.lstrip("/")
            targets[rel_id] = target if target.startswith("xl/") else "xl/" + target
    parts: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{{{S}}}sheet"):
        part = targets.get(sheet.get(f"{{{R}}}id") or "")
        if part:
            parts.append((str(sheet.get("name") or "sheet"), part))
    return parts


def _row_values(row: ET.Element, shared: list[str]) -> list[str]:
    """返回该行按列顺序排列的非空单元格文本。"""
    cells: list[tuple[int, str]] = []
    for cell in row.findall(f"{{{S}}}c"):
        cell_type = cell.get("t")
        value = ""
        if cell_type == "s":
            raw = cell.find(f"{{{S}}}v")
            text = (raw.text or "").strip() if raw is not None else ""
            if text.isdigit() and 0 <= int(text) < len(shared):
                value = shared[int(text)]
        elif cell_type == "inlineStr":
            inline = cell.find(f"{{{S}}}is")
            if inline is not None:
                value = _text_of(inline)
        else:
            raw = cell.find(f"{{{S}}}v")
            if raw is not None and raw.text:
                value = raw.text.strip()
        cells.append((_column_index(cell.get("r")), " ".join(value.split())))
    ordered = [text for _, text in sorted(cells, key=lambda item: item[0])]
    return [text for text in ordered if text]


def parse_xlsx(
    data: bytes,
    limits: ParseLimits,
    check_deadline: Callable[[], None],
    *,
    filename: str | None = None,
) -> ParserOutput:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ParseFailure(ParseErrorCode.CORRUPT_FILE, "XLSX 无法读取（文件可能损坏）") from None
    with archive:
        names = set(archive.namelist())
        total_uncompressed = sum(m.file_size for m in archive.infolist() if not m.is_dir())
        if total_uncompressed > limits.max_uncompressed_bytes:
            raise ParseFailure(
                ParseErrorCode.ZIP_LIMIT_EXCEEDED,
                f"XLSX 解压后总量 {total_uncompressed} 字节超过上限 {limits.max_uncompressed_bytes}",
            )
        if "xl/workbook.xml" not in names:
            raise ParseFailure(ParseErrorCode.CORRUPT_FILE, "XLSX 缺少 xl/workbook.xml，文件可能不是有效表格")
        shared = _shared_strings(archive)
        parts = _sheet_parts(archive) or [("sheet", "xl/worksheets/sheet1.xml")]
        builder = ChunkBuilder(max_chunks=limits.max_chunks)
        rows_seen = 0
        chars = 0
        for sheet_name, part in parts:
            check_deadline()
            if part not in names:
                continue
            try:
                root = ET.fromstring(archive.read(part))
            except ET.ParseError:
                raise ParseFailure(ParseErrorCode.CORRUPT_FILE, f"工作表无法解析：{sheet_name}") from None
            for row_no, row in enumerate(root.iter(f"{{{S}}}row"), start=1):
                if row_no > _MAX_ROWS_PER_SHEET:
                    break
                check_deadline()
                values = _row_values(row, shared)
                if not values:
                    continue
                text = _CELL_SEP.join(values)
                locator: dict[str, Any] = {"sheet": sheet_name, "row": row_no}
                builder.add(text, locator)
                rows_seen += 1
                chars += len(text)
        needs_ocr = chars < MIN_TEXT_CHARS
        return builder.finish(units=rows_seen, chars=chars, needs_ocr=needs_ocr)
