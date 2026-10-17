"""XLSX 解析：真实 OOXML 结构下的行列文本、定位与错误码。

真实课件里存在 `.xlsx`（如「excel练习.xlsx」），它是 OOXML 压缩包，
因此用 zipfile + ElementTree 解析即可，不需要额外依赖。
"""

from __future__ import annotations

import hashlib
import io
import zipfile

from learning.parse import ParseContext, ParseErrorCode, ParseLimits, ParseStatus, detect_format, parse_document
from learning.parse.xlsx import parse_xlsx

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="成绩表" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

SHARED_STRINGS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="6" uniqueCount="6">
  <si><t>姓名</t></si><si><t>分数</t></si><si><t>张三</t></si>
  <si><t>李四</t></si><si><t>备注</t></si><si><t>缺考</t></si>
</sst>"""

SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>4</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>91</v></c></row>
    <row r="3"><c r="A3" t="s"><v>3</v></c><c r="C3" t="s"><v>5</v></c></row>
    <row r="4"></row>
  </sheetData>
</worksheet>"""


def build_xlsx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr("xl/sharedStrings.xml", SHARED_STRINGS)
        archive.writestr("xl/worksheets/sheet1.xml", SHEET)
    return buffer.getvalue()


def ctx_for(data: bytes) -> ParseContext:
    return ParseContext(
        resource_id="res_xlsx",
        resource_version=1,
        content_hash=hashlib.sha256(data).hexdigest(),
        job_id="job_xlsx",
        observed_at="2026-10-17T10:00:00+08:00",
    )


def test_detect_format_accepts_xlsx_by_extension_and_mime():
    assert detect_format("excel练习.xlsx") == "xlsx"
    assert detect_format(None, XLSX_MIME) == "xlsx"


def test_xlsx_rows_become_chunks_with_sheet_and_row_locators():
    data = build_xlsx()
    result = parse_document(data, filename="excel练习.xlsx", context=ctx_for(data))
    assert result.status == ParseStatus.READY, result.message
    assert result.format == "xlsx"
    texts = [chunk.text for chunk in result.chunks]
    assert "姓名 | 分数 | 备注" in texts
    assert "张三 | 91" in texts
    assert "李四 | 缺考" in texts          # sparse row keeps column gaps out, values stay ordered
    assert all(chunk.locator.get("sheet") == "成绩表" for chunk in result.chunks)
    rows = sorted({chunk.locator.get("row") for chunk in result.chunks})
    assert rows == [1, 2, 3]               # the empty row 4 is skipped
    assert result.stats["units"] >= 3


def test_xlsx_without_sheet_data_is_needs_ocr_not_fabricated():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml",
                         '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>')
    data = buffer.getvalue()
    result = parse_document(data, filename="empty.xlsx", context=ctx_for(data))
    # An empty sheet has no text: it must be reported as needing OCR rather than
    # silently "ready" with zero content.
    assert result.status == ParseStatus.NEEDS_OCR
    assert result.chunks == []


def test_xlsx_without_workbook_is_reported_as_corrupt():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "not a workbook")
    data = buffer.getvalue()
    result = parse_document(data, filename="bad.xlsx", context=ctx_for(data))
    assert result.status == ParseStatus.FAILED
    assert result.error_code == ParseErrorCode.CORRUPT_FILE


def test_xlsx_parser_rejects_non_zip_bytes():
    try:
        parse_xlsx(b"not a zip at all", ParseLimits(), lambda: None, filename="x.xlsx")
    except Exception as exc:  # ParseFailure
        assert getattr(exc, "code", None) == ParseErrorCode.CORRUPT_FILE
    else:  # pragma: no cover - must raise
        raise AssertionError("corrupt xlsx must raise ParseFailure")
