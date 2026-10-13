"""D2 测试用最小文档夹具（直接构造字节，不依赖真实文件）。

- DOCX：zip + word/document.xml（本解析器只读该部件，缺其他部件也能解析）；
- PPTX：zip + ppt/slides/slideN.xml（+ _rels 与 notesSlide 用于备注）；
- PDF：手工构造未压缩内容流的文本型 PDF。文本限 ASCII——手工构造含中文的 PDF
  需要嵌入字体，不可行；中文正文路径由 DOCX/PPTX 夹具与真实样例覆盖（见 D3）。
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape as _esc

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A14_NS = "http://schemas.microsoft.com/office/drawing/2010/main"


def _zip_bytes(files: dict[str, bytes | str], extra: dict[str, bytes | str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in {**files, **(extra or {})}.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def plain_zip(files: dict[str, bytes | str]) -> bytes:
    """普通 zip（用于“是 zip 但不是目标文档”的损坏场景）。"""
    return _zip_bytes(files)


# ---------------------------------------------------------------- DOCX


def docx_bytes(body_xml: str, extra_files: dict[str, bytes | str] | None = None) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}" xmlns:m="{M_NS}"><w:body>{body_xml}</w:body></w:document>'
    )
    return _zip_bytes({"word/document.xml": document.encode("utf-8")}, extra_files)


def docx_paragraph(text: str, *, style: str | None = None, outline_lvl: int | None = None) -> str:
    ppr = ""
    if style is not None or outline_lvl is not None:
        inner = ""
        if style is not None:
            inner += f'<w:pStyle w:val="{style}"/>'
        if outline_lvl is not None:
            inner += f'<w:outlineLvl w:val="{outline_lvl}"/>'
        ppr = f"<w:pPr>{inner}</w:pPr>"
    run = f'<w:r><w:t xml:space="preserve">{_esc(text)}</w:t></w:r>' if text else ""
    return f"<w:p>{ppr}{run}</w:p>"


def docx_tab_paragraph(left: str, right: str) -> str:
    return f"<w:p><w:r><w:t>{_esc(left)}</w:t><w:tab/><w:t>{_esc(right)}</w:t></w:r></w:p>"


def docx_break_paragraph(before: str, after: str) -> str:
    return f"<w:p><w:r><w:t>{_esc(before)}</w:t><w:br/><w:t>{_esc(after)}</w:t></w:r></w:p>"


def docx_image_paragraph() -> str:
    return "<w:p><w:r><w:drawing/></w:r></w:p>"


def docx_formula_paragraph(value: str = "x^2") -> str:
    return f"<w:p><m:oMath><m:r><m:t>{_esc(value)}</m:t></m:r></m:oMath></w:p>"


def docx_table(rows: list[list[str]], *, merged: bool = False) -> str:
    trs = []
    for r_i, row in enumerate(rows):
        tcs = []
        for c_i, cell in enumerate(row):
            tcpr = ""
            if merged and r_i == 0 and c_i == 0:
                tcpr = '<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
            tcs.append(f"<w:tc>{tcpr}{docx_paragraph(cell)}</w:tc>")
        trs.append("<w:tr>" + "".join(tcs) + "</w:tr>")
    return "<w:tbl>" + "".join(trs) + "</w:tbl>"


def docx_sdt(wrapped_xml: str) -> str:
    """结构控件（w:sdt）包裹的内容，验证按文档顺序递归展开。"""
    return f"<w:sdt><w:sdtPr/><w:sdtContent>{wrapped_xml}</w:sdtContent></w:sdt>"


# ---------------------------------------------------------------- PPTX


def _slide_xml(shapes: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:a14="{A14_NS}">'
        f"<p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"
    ).encode("utf-8")


def pptx_title_shape(text: str) -> str:
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr>'
        '<p:ph type="title"/></p:nvPr></p:nvSpPr>'
        f'<p:txBody><a:bodyPr/><a:p><a:r><a:t>{_esc(text)}</a:t></a:r></a:p></p:txBody></p:sp>'
    )


def pptx_body_shape(paragraphs: list[str]) -> str:
    paras = "".join(f"<a:p><a:r><a:t>{_esc(p)}</a:t></a:r></a:p>" for p in paragraphs)
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content Placeholder 2"/><p:cNvSpPr/><p:nvPr>'
        '<p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
        f"<p:txBody><a:bodyPr/>{paras}</p:txBody></p:sp>"
    )


def pptx_pic_shape() -> str:
    return (
        '<p:pic><p:nvPicPr><p:cNvPr id="4" name="Picture 3"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        '<p:blipFill><a:blip r:embed="rId2"/></p:blipFill><p:spPr/></p:pic>'
    )


def pptx_formula_shape() -> str:
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="Formula 4"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/ole">'
        f'<a14:m><m:oMath xmlns:m="{M_NS}"/></a14:m>'
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


def pptx_table_shape(rows: list[list[str]]) -> str:
    trs = []
    for row in rows:
        tcs = "".join(
            f'<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{_esc(cell)}</a:t></a:r></a:p></a:txBody></a:tc>'
            for cell in row
        )
        trs.append(f"<a:tr>{tcs}</a:tr>")
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="6" name="Table 5"/>'
        "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        f"<a:tbl>{''.join(trs)}</a:tbl>"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


def _notes_rels_xml(slide_number: int) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{R_NS}/notesSlide" '
        f'Target="../notesSlides/notesSlide{slide_number}.xml"/>'
        "</Relationships>"
    ).encode("utf-8")


def _notes_slide_xml(paragraphs: list[str]) -> bytes:
    sld_num = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Number Placeholder 1"/><p:cNvSpPr/><p:nvPr>'
        '<p:ph type="sldNum" sz="quarter" idx="12"/></p:nvPr></p:nvSpPr>'
        "<p:txBody><a:bodyPr/><a:p><a:r><a:t>7</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    body = (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr/><p:nvPr>'
        '<p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:txBody><a:bodyPr/>'
        + "".join(f"<a:p><a:r><a:t>{_esc(p)}</a:t></a:r></a:p>" for p in paragraphs)
        + "</p:txBody></p:sp>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:notes xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree>{sld_num}{body}</p:spTree></p:cSld></p:notes>'
    ).encode("utf-8")


def pptx_bytes(
    slides: dict[int, str],
    *,
    notes: dict[int, list[str]] | None = None,
    extra_files: dict[str, bytes | str] | None = None,
) -> bytes:
    files: dict[str, bytes | str] = {}
    for number, shapes in slides.items():
        files[f"ppt/slides/slide{number}.xml"] = _slide_xml(shapes)
        if notes and number in notes:
            files[f"ppt/slides/_rels/slide{number}.xml.rels"] = _notes_rels_xml(number)
            files[f"ppt/notesSlides/notesSlide{number}.xml"] = _notes_slide_xml(notes[number])
    return _zip_bytes(files, extra_files)


# ---------------------------------------------------------------- PDF


def _content_stream(text: str | None) -> str:
    if text is None:  # 纯图形页：填充矩形，无文本层（模拟扫描页）
        return "0.9 g 100 100 400 500 re f"
    lines = text.split("\n")
    escaped = [ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)") for ln in lines]
    parts = ["BT /F1 12 Tf 72 720 Td 16 TL"]
    for i, ln in enumerate(escaped):
        if i:
            parts.append("T*")
        parts.append(f"({ln}) Tj")
    parts.append("ET")
    return "\n".join(parts)


def pdf_bytes(page_texts: list[str | None]) -> bytes:
    """构造文本型 PDF；None 表示纯图形页。文本限 ASCII（Helvetica，无需嵌入字体）。"""
    n = len(page_texts)
    font_num = 3 + 2 * n
    objects: dict[int, bytes] = {}
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("ascii")
    for i, content in enumerate(page_texts):
        page_num = 3 + 2 * i
        content_num = page_num + 1
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode("ascii")
        payload = _content_stream(content).encode("latin-1")
        objects[content_num] = (
            b"<< /Length " + str(len(payload)).encode("ascii") + b" >>\nstream\n" + payload + b"\nendstream"
        )
    objects[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in range(1, font_num + 1):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("ascii") + objects[num] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {font_num + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for num in range(1, font_num + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {font_num + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    return bytes(out)


def encrypted_pdf_bytes(page_texts: list[str], password: str = "open-sesame") -> bytes:
    """用 pypdf 输出带口令的 PDF（测试环境必须安装 pypdf：test extra，缺依赖即失败）。"""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes(page_texts)))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
