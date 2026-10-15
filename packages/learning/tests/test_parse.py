"""D2 解析模块测试：DOCX/PPTX/PDF 结构、定位、错误码、护栏、缓存与确定性。

PDF 相关用例要求环境安装 test extra（含 pypdf）；缺依赖即失败（B15），不允许静默跳过。
"""

from __future__ import annotations

import hashlib

import pytest

import parse_fixtures as fx
from learning.parse import (
    PARSER_VERSION,
    ParseContext,
    ParseErrorCode,
    ParseLimits,
    ParseStatus,
    detect_format,
    parse_document,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def ctx_for(
    data: bytes,
    *,
    job_id: str = "job_test",
    resource_version: int = 1,
    observed_at: str = "2026-10-09T10:00:00+08:00",
) -> ParseContext:
    return ParseContext(
        resource_id="res_test",
        resource_version=resource_version,
        content_hash=hashlib.sha256(data).hexdigest(),
        job_id=job_id,
        observed_at=observed_at,
    )


def sample_docx_body() -> str:
    return "".join(
        [
            fx.docx_paragraph("第1章 数据与信息", outline_lvl=0),
            fx.docx_paragraph("数据与信息贯穿本课程的主要内容，涵盖数据的采集、清洗、分析与可视化等环节，共计数十个知识点需要逐步掌握。"),
            fx.docx_paragraph("补充说明。"),
            fx.docx_table([["指标", "数值"], ["准确率", "0.95"]], merged=True),
            fx.docx_paragraph("1.1 数据来源", outline_lvl=1),
            fx.docx_paragraph("本节介绍数据的三种主要来源：公开数据集、课程平台导出与实验采集，并说明各自的适用场景与注意事项。"),
        ]
    )


def sample_docx() -> bytes:
    return fx.docx_bytes(sample_docx_body())


def _require_pypdf():
    try:
        import pypdf  # noqa: F401
    except ImportError:
        pytest.fail(
            "测试环境缺少 pypdf——PDF 用例不允许静默跳过（B15 缺依赖即失败）。"
            "请安装测试依赖：pip install -e '.[test]'（Windows 经 .venv/Scripts/pip）"
        )


# ---------------------------------------------------------------- DOCX


def test_docx_structure_offsets_and_outline():
    result = parse_document(sample_docx(), filename="chapter.docx")
    assert result.status == ParseStatus.READY
    assert result.error_code is None
    assert result.cache == "off"
    assert result.extractor == "docx-text"
    assert result.extractor_version == PARSER_VERSION

    assert [c.kind for c in result.chunks] == ["heading", "body", "body", "table", "heading", "body"]
    assert result.chunks[0].heading_path == ("第1章 数据与信息",)
    assert result.chunks[1].heading_path == ("第1章 数据与信息",)
    assert result.chunks[3].heading_path == ("第1章 数据与信息",)
    assert result.chunks[3].text == "指标 | 数值\n准确率 | 0.95"
    assert result.chunks[4].heading_path == ("第1章 数据与信息", "1.1 数据来源")
    assert result.chunks[5].heading_path == ("第1章 数据与信息", "1.1 数据来源")

    assert [(o.level, o.title) for o in result.outline] == [(1, "第1章 数据与信息"), (2, "1.1 数据来源")]
    assert [o.ordinal for o in result.outline] == [0, 4]

    full = "\n".join(c.text for c in result.chunks)
    for c in result.chunks:
        loc = c.locator
        assert loc["kind"] == "text"
        assert full[loc["start_offset"]:loc["end_offset"]] == c.text

    assert result.stats["units"] == 5
    assert result.stats["chunks"] == 6
    assert any("表格" in lim and "合并" in lim for lim in result.limitations)


def test_docx_heading_style_fallback():
    body = fx.docx_paragraph("Styled Heading", style="Heading 2") + fx.docx_paragraph(
        "Follow-up body paragraph with plenty of characters to stand alone in the document."
    )
    result = parse_document(fx.docx_bytes(body), filename="a.docx")
    assert result.chunks[0].kind == "heading"
    assert [(o.level, o.title) for o in result.outline] == [(2, "Styled Heading")]


def test_docx_inline_whitespace_mapping():
    body = fx.docx_tab_paragraph("AB", "CD") + fx.docx_break_paragraph("第二行前", "第二行后")
    result = parse_document(fx.docx_bytes(body), filename="a.docx")
    texts = [c.text for c in result.chunks]
    assert texts == ["AB CD", "第二行前\n第二行后"]
    full = "\n".join(texts)
    for c in result.chunks:
        loc = c.locator
        assert full[loc["start_offset"]:loc["end_offset"]] == c.text


def test_docx_image_and_formula_annotated_not_extracted():
    body = (
        fx.docx_paragraph("开头段提供足够的正文字符数以通过文本阈值检查，避免误判扫描件。")
        + fx.docx_image_paragraph()
        + fx.docx_formula_paragraph("y = x^2 + 1")
    )
    result = parse_document(fx.docx_bytes(body), filename="a.docx")
    assert result.status == ParseStatus.READY
    assert all("y = x^2" not in c.text for c in result.chunks)  # 公式未提取为文本
    assert any("图片" in lim and "1 处" in lim for lim in result.limitations)
    assert any("公式" in lim for lim in result.limitations)


def test_docx_image_only_needs_ocr():
    result = parse_document(fx.docx_bytes(fx.docx_image_paragraph()), filename="scan.docx")
    assert result.status == ParseStatus.NEEDS_OCR
    assert result.error_code is None
    assert result.chunks == []
    assert "OCR" in (result.message or "")
    assert any("图片" in lim for lim in result.limitations)


def test_docx_content_control_recursion():
    body = fx.docx_sdt(fx.docx_paragraph("被结构控件包裹的段落内容足够长以便稳定断言其存在。"))
    result = parse_document(fx.docx_bytes(body), filename="a.docx")
    assert [c.text for c in result.chunks] == ["被结构控件包裹的段落内容足够长以便稳定断言其存在。"]


def test_docx_corrupt_inputs():
    assert parse_document(b"this is not a zip", filename="a.docx").error_code == ParseErrorCode.CORRUPT_FILE
    plain = fx.plain_zip({"readme.txt": "hi"})
    assert parse_document(plain, filename="a.docx").error_code == ParseErrorCode.CORRUPT_FILE
    bad_xml = fx.docx_bytes("<w:p><w:r><w:t>未闭合")
    assert parse_document(bad_xml, filename="a.docx").error_code == ParseErrorCode.CORRUPT_FILE


def test_docx_limits():
    data = sample_docx()
    limited = parse_document(data, filename="a.docx", limits=ParseLimits(max_chunks=2))
    assert len(limited.chunks) == 2
    assert any("上限 2" in lim for lim in limited.limitations)

    zero = parse_document(data, filename="a.docx", limits=ParseLimits(max_chunks=0))
    assert zero.status == ParseStatus.FAILED
    assert zero.error_code == ParseErrorCode.EMPTY_DOCUMENT

    big = fx.docx_bytes(sample_docx_body(), extra_files={"word/blob.bin": b"\x00" * 200_000})
    guarded = parse_document(big, filename="a.docx", limits=ParseLimits(max_uncompressed_bytes=1_000))
    assert guarded.error_code == ParseErrorCode.ZIP_LIMIT_EXCEEDED

    trunc = parse_document(data, filename="a.docx", limits=ParseLimits(max_chars_per_page=10))
    assert len(trunc.chunks[1].text) == 10
    assert any("截断" in lim for lim in trunc.limitations)


# ---------------------------------------------------------------- PPTX


def test_pptx_structure_notes_and_locators():
    slides = {
        1: fx.pptx_title_shape("Introduction") + fx.pptx_body_shape(
            ["Welcome to the course material with enough characters."]
        ),
        3: fx.pptx_title_shape("Chapter 1") + fx.pptx_body_shape(
            ["First point with sufficient length to be standalone.", "Second point also with enough characters here."]
        ),
    }
    notes = {3: ["Remember to review the key points before the exam."]}
    result = parse_document(fx.pptx_bytes(slides, notes=notes), filename="deck.pptx")
    assert result.status == ParseStatus.READY
    assert result.extractor == "pptx-text"
    assert [c.kind for c in result.chunks] == ["heading", "body", "heading", "body", "body", "notes"]
    assert [c.locator["slide"] for c in result.chunks] == [1, 1, 3, 3, 3, 3]
    assert all(c.locator["kind"] == "slide" for c in result.chunks)
    assert result.chunks[2].text == "Chapter 1"
    assert result.chunks[2].heading_path == ("Chapter 1",)
    assert result.chunks[5].text == "Remember to review the key points before the exam."
    assert "7" not in [c.text for c in result.chunks]  # sldNum 占位符被跳过
    assert [o.title for o in result.outline] == ["Introduction", "Chapter 1"]
    assert result.stats["units"] == 2


def test_pptx_slide_number_order_is_numeric():
    def slide(title: str) -> str:
        return fx.pptx_title_shape(title) + fx.pptx_body_shape(["Body text long enough for chunking threshold."])

    drafts = {1: slide("Slide one"), 10: slide("Slide ten"), 2: slide("Slide two")}
    result = parse_document(fx.pptx_bytes(drafts), filename="a.pptx")
    headings = [c.text for c in result.chunks if c.kind == "heading"]
    assert headings == ["Slide one", "Slide two", "Slide ten"]


def test_pptx_table_media_and_limitations():
    shapes = (
        fx.pptx_title_shape("Metrics")
        + fx.pptx_table_shape([["A", "B"], ["C", "D"]])
        + fx.pptx_pic_shape()
        + fx.pptx_formula_shape()
    )
    result = parse_document(fx.pptx_bytes({1: shapes}), filename="a.pptx")
    table = next(c for c in result.chunks if c.kind == "table")
    assert table.text == "A | B\nC | D"
    assert any("图片" in lim and "1 处" in lim for lim in result.limitations)
    assert any("公式" in lim for lim in result.limitations)
    assert any("表格" in lim for lim in result.limitations)


def test_pptx_picture_only_needs_ocr():
    result = parse_document(fx.pptx_bytes({1: fx.pptx_pic_shape()}), filename="a.pptx")
    assert result.status == ParseStatus.NEEDS_OCR
    assert result.error_code is None
    assert "OCR" in (result.message or "")


def test_pptx_corrupt_and_guards():
    assert parse_document(b"not a zip", filename="a.pptx").error_code == ParseErrorCode.CORRUPT_FILE
    assert parse_document(fx.plain_zip({"ppt/other.xml": "<x/>"}), filename="a.pptx").error_code == ParseErrorCode.CORRUPT_FILE

    slides = {n: fx.pptx_title_shape(f"Slide {n}") + fx.pptx_body_shape(["Content."]) for n in (1, 2, 3)}
    limited = parse_document(fx.pptx_bytes(slides), filename="a.pptx", limits=ParseLimits(max_pages=2))
    assert limited.error_code == ParseErrorCode.PAGE_LIMIT_EXCEEDED

    big = fx.pptx_bytes(
        {1: fx.pptx_title_shape("T") + fx.pptx_body_shape(["Body content."])},
        extra_files={"ppt/media/blob.bin": b"\x00" * 200_000},
    )
    guarded = parse_document(big, filename="a.pptx", limits=ParseLimits(max_uncompressed_bytes=1_000))
    assert guarded.error_code == ParseErrorCode.ZIP_LIMIT_EXCEEDED


# ---------------------------------------------------------------- PDF


def test_pdf_pages_locators_and_heading():
    _require_pypdf()
    body = "Machine learning studies how computers learn patterns from data without explicit rules."
    result = parse_document(fx.pdf_bytes(["1.1 Text Extraction", body]), filename="notes.pdf")
    assert result.status == ParseStatus.READY
    assert result.extractor == "pdf-text"
    assert result.stats["units"] == 2
    assert [c.locator["page"] for c in result.chunks] == [1, 2]
    assert all(c.locator["kind"] == "page" for c in result.chunks)
    assert result.chunks[0].kind == "heading"
    assert result.chunks[0].heading_path == ("1.1 Text Extraction",)
    assert result.chunks[1].kind == "body"
    assert "Text Extraction" in result.chunks[0].text
    assert any("文本层" in lim for lim in result.limitations)


def test_pdf_scanlike_needs_ocr():
    _require_pypdf()
    result = parse_document(fx.pdf_bytes([None]), filename="scan.pdf")
    assert result.status == ParseStatus.NEEDS_OCR
    assert result.error_code is None
    assert "OCR" in (result.message or "")
    assert result.stats["units"] == 1


def test_pdf_corrupt_and_encrypted():
    _require_pypdf()
    assert parse_document(b"%PDF-1.4 broken", filename="a.pdf").error_code == ParseErrorCode.CORRUPT_FILE
    encrypted = fx.encrypted_pdf_bytes(["Secret content page"])
    result = parse_document(encrypted, filename="a.pdf")
    assert result.status == ParseStatus.FAILED
    assert result.error_code == ParseErrorCode.ENCRYPTED_FILE


def test_pdf_page_limit():
    _require_pypdf()
    data = fx.pdf_bytes(["page one text", "page two text"])
    result = parse_document(data, filename="a.pdf", limits=ParseLimits(max_pages=1))
    assert result.error_code == ParseErrorCode.PAGE_LIMIT_EXCEEDED


def test_pdf_dependency_missing(monkeypatch):
    from learning.parse import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "_pypdf", None)
    result = parse_document(fx.pdf_bytes(["some text"]), filename="a.pdf")
    assert result.status == ParseStatus.FAILED
    assert result.error_code == ParseErrorCode.DEPENDENCY_MISSING


def test_test_environment_has_pdf_dependency():
    """B15 环境完整性：测试环境必须安装完整测试依赖（缺 pypdf 即失败，而非跳过）。"""
    _require_pypdf()


# ---------------------------------------------------------------- 公共行为


def test_unsupported_empty_and_size_guards():
    assert parse_document(b"x", filename="a.zip").error_code == ParseErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert parse_document(b"x").error_code == ParseErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert parse_document(b"").error_code == ParseErrorCode.EMPTY_DOCUMENT
    result = parse_document(sample_docx(), filename="a.docx", limits=ParseLimits(max_file_bytes=10))
    assert result.error_code == ParseErrorCode.FILE_TOO_LARGE


def test_size_guard_boundary_exact_limit():
    """超大文件护栏边界：恰等于上限可解析，超出一字节即 FILE_TOO_LARGE。"""
    data = sample_docx()
    size = len(data)
    ok = parse_document(data, filename="a.docx", limits=ParseLimits(max_file_bytes=size))
    assert ok.status == ParseStatus.READY
    over = parse_document(data, filename="a.docx", limits=ParseLimits(max_file_bytes=size - 1))
    assert over.status == ParseStatus.FAILED
    assert over.error_code == ParseErrorCode.FILE_TOO_LARGE
    assert str(size) in (over.message or "")


def test_needs_ocr_threshold_boundary():
    """needs_ocr 与失败/就绪的边界：可提取文本低于阈值（20 字符）→ needs_ocr 且非失败；达到阈值 → ready。

    三格式（DOCX/PPTX/PDF）同规则；needs_ocr 一律 error_code=None、文案含 OCR、不作为失败处理。
    """
    short = parse_document(fx.docx_bytes(fx.docx_paragraph("a" * 19)), filename="a.docx")
    assert short.status == ParseStatus.NEEDS_OCR
    assert short.error_code is None
    assert "OCR" in (short.message or "")

    exact = parse_document(fx.docx_bytes(fx.docx_paragraph("a" * 20)), filename="a.docx")
    assert exact.status == ParseStatus.READY
    assert exact.error_code is None

    pptx_short = parse_document(fx.pptx_bytes({1: fx.pptx_body_shape(["y" * 19])}), filename="a.pptx")
    assert pptx_short.status == ParseStatus.NEEDS_OCR
    pptx_exact = parse_document(fx.pptx_bytes({1: fx.pptx_body_shape(["y" * 20])}), filename="a.pptx")
    assert pptx_exact.status == ParseStatus.READY

    _require_pypdf()
    pdf_short = parse_document(fx.pdf_bytes(["x" * 19]), filename="a.pdf")
    assert pdf_short.status == ParseStatus.NEEDS_OCR
    pdf_exact = parse_document(fx.pdf_bytes(["x" * 20]), filename="a.pdf")
    assert pdf_exact.status == ParseStatus.READY


def test_detect_format_and_mime_precedence():
    assert detect_format("NOTES.PDF") == "pdf"
    assert detect_format(None, "application/pdf; charset=binary") == "pdf"
    assert detect_format("a.docx") == "docx"
    assert detect_format("a.pptx") == "pptx"
    assert detect_format("a.txt") is None
    assert detect_format(None) is None
    result = parse_document(sample_docx(), filename="blob.bin", mime_type=DOCX_MIME)
    assert result.status == ParseStatus.READY


def test_parse_timeout():
    result = parse_document(sample_docx(), filename="a.docx", limits=ParseLimits(timeout_seconds=0))
    assert result.status == ParseStatus.FAILED
    assert result.error_code == ParseErrorCode.PARSE_TIMEOUT


def test_unexpected_parser_error_wrapped(monkeypatch):
    import learning.parse as parse_mod

    def boom(data, limits, check_deadline):
        raise RuntimeError("boom")

    monkeypatch.setitem(parse_mod._PARSERS, "docx", boom)
    result = parse_document(sample_docx(), filename="a.docx")
    assert result.error_code == ParseErrorCode.INTERNAL_ERROR
    assert "boom" in (result.message or "")


def test_result_shape_for_b_integration():
    data = sample_docx()
    ctx = ctx_for(data)
    result = parse_document(data, filename="a.docx", context=ctx)
    payload = result.to_dict(ctx)
    chunk = payload["chunks"][0]
    assert {"resource_id", "resource_version", "ordinal", "text", "locator", "provenance"} <= set(chunk)
    assert chunk["resource_id"] == "res_test"
    assert chunk["resource_version"] == 1
    assert chunk["provenance"]["extractor"] == "docx-text"
    assert chunk["provenance"]["extractor_version"] == PARSER_VERSION
    assert chunk["provenance"]["job_id"] == "job_test"
    assert chunk["provenance"]["fact_source"] == "platform"
    assert payload["outline"][0]["title"] == "第1章 数据与信息"


def test_determinism_same_bytes_same_chunks():
    data = sample_docx()
    first = parse_document(data, filename="a.docx")
    second = parse_document(data, filename="a.docx")
    assert first.chunk_dicts(None) == second.chunk_dicts(None)


# ---------------------------------------------------------------- 缓存


def test_cache_hit_and_provenance_restamp(tmp_path):
    data = sample_docx()
    cache_dir = tmp_path / "pcache"
    ctx1 = ctx_for(data, job_id="job_first")
    first = parse_document(data, filename="a.docx", context=ctx1, cache_dir=cache_dir)
    assert first.cache == "miss"
    assert first.stats["from_cache"] is False
    assert all(d["provenance"]["job_id"] == "job_first" for d in first.chunk_dicts(ctx1))

    ctx2 = ctx_for(data, job_id="job_second", observed_at="2026-10-09T11:30:00+08:00")
    second = parse_document(data, filename="a.docx", context=ctx2, cache_dir=cache_dir)
    assert second.cache == "hit"
    assert second.stats["from_cache"] is True
    assert [c.text for c in second.chunks] == [c.text for c in first.chunks]

    # 缓存体只存内容派生字段：不含任何任务号；返回时按当次上下文现盖
    payload = next(cache_dir.glob("*.json")).read_text(encoding="utf-8")
    assert "job_first" not in payload and "job_second" not in payload
    restamped = second.chunk_dicts(ctx2)
    assert all(d["provenance"]["job_id"] == "job_second" for d in restamped)
    assert all(d["provenance"]["observed_at"] == "2026-10-09T11:30:00+08:00" for d in restamped)


def test_cache_invalidation_limits_and_parser_version(tmp_path, monkeypatch):
    data = sample_docx()
    cache_dir = tmp_path / "pcache"
    first = parse_document(data, filename="a.docx", cache_dir=cache_dir)
    assert first.cache == "miss"

    tighter = parse_document(data, filename="a.docx", cache_dir=cache_dir, limits=ParseLimits(max_chunks=1000))
    assert tighter.cache == "miss"  # 影响内容的限制项参与缓存键

    monkeypatch.setattr("learning.parse.cache.PARSER_VERSION", "9.9.9")
    bumped = parse_document(data, filename="a.docx", cache_dir=cache_dir)
    assert bumped.cache == "miss"  # 解析器版本参与缓存键


def test_cache_not_written_on_failure(tmp_path):
    cache_dir = tmp_path / "pcache"
    result = parse_document(b"not a zip", filename="a.docx", cache_dir=cache_dir)
    assert result.status == ParseStatus.FAILED
    assert list(cache_dir.glob("*.json")) == []


def test_cache_key_follows_content_hash_not_filename(tmp_path):
    """缓存键随内容哈希变化：同名不同内容不得误复用；内容相同换名可命中。"""
    cache_dir = tmp_path / "pcache"
    first = parse_document(sample_docx(), filename="a.docx", cache_dir=cache_dir)
    assert first.cache == "miss"

    other = fx.docx_bytes(fx.docx_paragraph("另一份完全不同的内容，用于验证缓存键随内容变化。"))
    second = parse_document(other, filename="a.docx", cache_dir=cache_dir)
    assert second.cache == "miss"
    assert [c.text for c in second.chunks] != [c.text for c in first.chunks]

    renamed = parse_document(sample_docx(), filename="b.docx", cache_dir=cache_dir)
    assert renamed.cache == "hit"  # 键不含文件名
    assert [c.text for c in renamed.chunks] == [c.text for c in first.chunks]


def test_cache_stores_needs_ocr_results(tmp_path):
    """needs_ocr 属成功结果（非失败），应入缓存且复用后状态不变。"""
    cache_dir = tmp_path / "pcache"
    data = fx.docx_bytes(fx.docx_image_paragraph())
    first = parse_document(data, filename="scan.docx", cache_dir=cache_dir)
    assert first.status == ParseStatus.NEEDS_OCR
    assert first.cache == "miss"
    assert len(list(cache_dir.glob("*.json"))) == 1

    second = parse_document(data, filename="scan.docx", cache_dir=cache_dir)
    assert second.cache == "hit"
    assert second.status == ParseStatus.NEEDS_OCR
    assert second.error_code is None
