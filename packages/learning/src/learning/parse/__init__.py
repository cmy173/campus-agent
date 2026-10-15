"""D2 基础解析：文本型 PDF / PPTX / DOCX → ContentChunk（对齐 contracts v0.1）。

对外唯一入口 `parse_document`（纯函数；不访问网络与数据库）：

- 输入文件字节 + 可选文件名/MIME + `ParseContext`（资源与任务溯源）+ `ParseLimits` + 缓存目录；
- 输出 `ParseResult`：status（ready / needs_ocr / failed）、chunks（含 locator 与 heading_path）、
  outline、limitations、stats；失败时给出明确错误码（见 types.ParseErrorCode）；
- 缓存：键含 content_hash + 解析器版本（+ 第三方库版本与影响内容的限制项），仅缓存成功结果；
  provenance（job_id/observed_at）每次调用现盖，不复用旧任务的来源信息。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import cache as _cache
from .docx import parse_docx
from .pdf import lib_version as pdf_lib_version
from .pdf import parse_pdf
from .pptx import parse_pptx
from .types import (
    PARSER_VERSION,
    OutlineEntry,
    ParseContext,
    ParseErrorCode,
    ParseFailure,
    ParseLimits,
    ParseResult,
    ParseStatus,
    ParsedChunk,
)

__all__ = [
    "PARSER_VERSION",
    "OutlineEntry",
    "ParseContext",
    "ParseErrorCode",
    "ParseFailure",
    "ParseLimits",
    "ParseResult",
    "ParseStatus",
    "ParsedChunk",
    "detect_format",
    "parse_document",
]

_MIME_TO_FORMAT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
_EXT_TO_FORMAT = {".pdf": "pdf", ".pptx": "pptx", ".docx": "docx"}

_PARSERS = {"pdf": parse_pdf, "pptx": parse_pptx, "docx": parse_docx}


def detect_format(filename: str | None = None, mime_type: str | None = None) -> str | None:
    if mime_type and mime_type.split(";")[0].strip().lower() in _MIME_TO_FORMAT:
        return _MIME_TO_FORMAT[mime_type.split(";")[0].strip().lower()]
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in _EXT_TO_FORMAT:
            return _EXT_TO_FORMAT[suffix]
    return None


def _lib_version(fmt: str) -> str:
    return pdf_lib_version() if fmt == "pdf" else "stdlib"


def parse_document(
    data: bytes,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    context: ParseContext | None = None,
    limits: ParseLimits | None = None,
    cache_dir: str | Path | None = None,
) -> ParseResult:
    limits = limits or ParseLimits()
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def check_deadline() -> None:
        if time.monotonic() - started >= limits.timeout_seconds:
            raise ParseFailure(
                ParseErrorCode.PARSE_TIMEOUT,
                f"解析超过时限 {limits.timeout_seconds} 秒，已中止（可收紧输入或调大 limits.timeout_seconds）",
            )

    def failed(code: str, message: str, fmt: str | None = None) -> ParseResult:
        return ParseResult(
            status=ParseStatus.FAILED,
            format=fmt,
            error_code=code,
            message=message,
            stats={"elapsed_ms": elapsed_ms(), "from_cache": False},
        )

    if not data:
        return failed(ParseErrorCode.EMPTY_DOCUMENT, "文件内容为空，无法解析")
    if len(data) > limits.max_file_bytes:
        return failed(
            ParseErrorCode.FILE_TOO_LARGE,
            f"文件 {len(data)} 字节超过上限 {limits.max_file_bytes} 字节",
        )
    fmt = detect_format(filename, mime_type)
    if fmt is None:
        return failed(
            ParseErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "不支持的文件类型（当前支持文本型 PDF / PPTX / DOCX；文件名与 MIME 均不可识别）",
        )

    content_hash = context.content_hash if context and context.content_hash else hashlib.sha256(data).hexdigest()
    cache_dir_path = Path(cache_dir) if cache_dir is not None else None
    key = _cache.cache_key(content_hash, fmt, _lib_version(fmt), limits.cache_shape())
    if cache_dir_path is not None:
        cached = _cache.load(cache_dir_path, key)
        if cached is not None:
            cached.cache = "hit"
            cached.stats["from_cache"] = True
            return cached
    cache_state = "miss" if cache_dir_path is not None else "off"

    try:
        output = _PARSERS[fmt](data, limits, check_deadline)
    except ParseFailure as failure:
        return failed(failure.code, failure.message, fmt)
    except Exception as exc:  # 第三方解析器的未预期异常：不崩溃，给出可诊断结果
        return failed(ParseErrorCode.INTERNAL_ERROR, f"解析过程中出现未预期错误：{exc!r}", fmt)

    status = ParseStatus.NEEDS_OCR if output.needs_ocr else ParseStatus.READY
    message = None
    if output.needs_ocr:
        message = "未提取到文本（疑似扫描件/图片型文档），需要 OCR 才能获得文字内容；未按失败处理"
    elif not output.chunks:
        return failed(ParseErrorCode.EMPTY_DOCUMENT, "文档解析后没有可用的文本片段", fmt)

    result = ParseResult(
        status=status,
        format=fmt,
        message=message,
        chunks=output.chunks,
        outline=output.outline,
        limitations=output.limitations,
        extractor=f"{fmt}-text",
        extractor_version=PARSER_VERSION,
        cache=cache_state,
        stats={
            "units": output.units,
            "units_label": {"pdf": "page", "pptx": "slide", "docx": "paragraph"}[fmt],
            "chars": output.chars,
            "chunks": len(output.chunks),
            "elapsed_ms": elapsed_ms(),
            "from_cache": False,
        },
    )
    if cache_dir_path is not None:
        _cache.save(cache_dir_path, key, result)
    return result
