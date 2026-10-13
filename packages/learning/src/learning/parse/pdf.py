"""文本型 PDF 解析（pypdf；可选依赖，未安装时返回 DEPENDENCY_MISSING 而非崩溃）。

限制说明（如实呈现）：按文本层提取，图像/公式/复杂表格的图形与版式不进入文本；
页码定位始终保留，可回看原文。无可提取文本（疑似扫描件）→ needs_ocr，不作为失败。
"""

from __future__ import annotations

import io
from typing import Callable

from .assemble import ChunkBuilder, ParserOutput
from .chunking import cap_text, chunk_text_blocks, detect_heading
from .types import ParseErrorCode, ParseFailure, ParseLimits

try:  # 可选依赖
    import pypdf as _pypdf
except ImportError:  # pragma: no cover - 环境相关
    _pypdf = None

MIN_TEXT_CHARS = 20  # 低于此判为“无可提取文本”

PDF_STANDING_LIMITATION = "PDF 按文本层提取：图像、公式与复杂表格的图形/版式不在文本中，未尝试还原；可按页码回看原文。"


def lib_version() -> str:
    return getattr(_pypdf, "__version__", "missing")


def parse_pdf(data: bytes, limits: ParseLimits, check_deadline: Callable[[], None]) -> ParserOutput:
    if _pypdf is None:
        raise ParseFailure(
            ParseErrorCode.DEPENDENCY_MISSING,
            "解析 PDF 需要 pypdf（pip install 'zju-learning[parse]'），当前环境未安装",
        )
    try:
        reader = _pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:  # pypdf 对损坏文件抛多种异常
        raise ParseFailure(ParseErrorCode.CORRUPT_FILE, f"PDF 无法读取（文件可能损坏）：{exc}") from None
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise ParseFailure(ParseErrorCode.ENCRYPTED_FILE, "PDF 已加密，无法解析（不尝试绕过密码）")

    try:
        pages = reader.pages
        page_count = len(pages)
    except Exception as exc:
        raise ParseFailure(ParseErrorCode.CORRUPT_FILE, f"PDF 页结构无法读取：{exc}") from None
    if page_count > limits.max_pages:
        raise ParseFailure(
            ParseErrorCode.PAGE_LIMIT_EXCEEDED,
            f"PDF 页数 {page_count} 超过上限 {limits.max_pages}",
        )

    builder = ChunkBuilder(limits.max_chunks)
    total_chars = 0
    failed_pages: list[int] = []
    truncated_pages: list[int] = []
    for number, page in enumerate(pages, start=1):
        check_deadline()
        try:
            text = page.extract_text() or ""
        except Exception:
            failed_pages.append(number)
            text = ""
        text, truncated = cap_text(text.strip(), limits.max_chars_per_page)
        if truncated:
            truncated_pages.append(number)
        total_chars += len(text)
        for block in chunk_text_blocks(text):
            level = detect_heading(block) if "\n" not in block else None
            builder.add(block, {"kind": "page", "page": number}, heading_level=level)

    output = builder.finish(units=page_count, chars=total_chars, needs_ocr=total_chars < MIN_TEXT_CHARS)
    output.limitations.insert(0, PDF_STANDING_LIMITATION)
    if failed_pages:
        output.limitations.append(
            "以下页面文本提取失败（可能为图形页/扫描页）："
            + "、".join(str(n) for n in failed_pages)
        )
    if truncated_pages:
        output.limitations.append(
            f"{len(truncated_pages)} 页文本超过单页上限 {limits.max_chars_per_page} 字符被截断"
        )
    return output
