"""解析模块的类型与常量（对齐 contracts v0.1：content-chunk.schema.json）。

- `ParsedChunk` 只承载“内容派生”字段（文本、定位、层级）；来源溯源（job_id/observed_at 等）
  由 `parse_document` 在返回时按当次 `ParseContext` 现盖，避免缓存复用旧任务信息。
- `locator` 形状与契约 Locator 一致：page / slide / text（start_offset/end_offset 为
  全文拼接字符串（片段以 "\\n" 连接）中的字符偏移）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PARSER_VERSION = "0.1.0"


class ParseStatus:
    READY = "ready"
    NEEDS_OCR = "needs_ocr"  # 疑似扫描件/图片型文档：未提取到文本，需 OCR；不作为失败处理
    FAILED = "failed"


class ParseErrorCode:
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    CORRUPT_FILE = "CORRUPT_FILE"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    PAGE_LIMIT_EXCEEDED = "PAGE_LIMIT_EXCEEDED"
    ZIP_LIMIT_EXCEEDED = "ZIP_LIMIT_EXCEEDED"
    PARSE_TIMEOUT = "PARSE_TIMEOUT"
    ENCRYPTED_FILE = "ENCRYPTED_FILE"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass
class ParseLimits:
    """解析资源护栏（防超大/超时/解压炸弹；可由 B 按环境收紧）。"""

    # PD-1（coord v1.8 裁定，2026-10-10）：30MB → 64MB。真实样例中唯一“可下载但超限”的
    # 42.9MB PDF 在旧默认值下会被永久拒绝 FILE_TOO_LARGE；本机单用户场景按 64MB 护栏。
    # 注意：B 侧 worker 实际传入 MAX_UPLOAD_BYTES（默认 100MB），二者口径由 B 统一（已转达）。
    max_file_bytes: int = 64_000_000
    max_pages: int = 300  # PDF 页数 / PPTX 幻灯片数上限
    max_chunks: int = 5_000
    max_uncompressed_bytes: int = 200_000_000  # zip 解压总量护栏（pptx/docx）
    max_chars_per_page: int = 200_000  # 单页/单张字符上限（超出截断并记注记）
    timeout_seconds: float = 60.0

    def cache_shape(self) -> str:
        """参与缓存键、会影响内容的限制项（超时/大小护栏不影响内容，不入键）。"""
        return f"pages={self.max_pages};chars={self.max_chars_per_page};chunks={self.max_chunks}"


@dataclass
class ParseContext:
    """调用方（B）提供的溯源上下文；contracts provenance 的 job_id/observed_at 由此传入。"""

    resource_id: str
    resource_version: int
    content_hash: str
    job_id: str
    observed_at: str  # ISO 8601 带偏移量


@dataclass
class OutlineEntry:
    level: int
    title: str
    ordinal: int
    locator: dict[str, Any]


@dataclass
class ParsedChunk:
    ordinal: int
    text: str
    locator: dict[str, Any]
    kind: str = "body"  # body | heading | table | notes
    heading_path: tuple[str, ...] = ()

    def to_dict(self, context: ParseContext | None, extractor: str, extractor_version: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ordinal": self.ordinal,
            "text": self.text,
            "locator": dict(self.locator),
            "kind": self.kind,
            "heading_path": list(self.heading_path),
        }
        if context is not None:
            out["resource_id"] = context.resource_id
            out["resource_version"] = context.resource_version
            out["provenance"] = {
                "extractor": extractor,
                "extractor_version": extractor_version,
                "job_id": context.job_id,
                "fact_source": "platform",
                "observed_at": context.observed_at,
            }
        return out


@dataclass
class ParseResult:
    status: str
    format: str | None = None
    error_code: str | None = None
    message: str | None = None
    chunks: list[ParsedChunk] = field(default_factory=list)
    outline: list[OutlineEntry] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    extractor: str | None = None
    extractor_version: str = PARSER_VERSION
    cache: str = "off"  # off | miss | hit
    stats: dict[str, Any] = field(default_factory=dict)

    def chunk_dicts(self, context: ParseContext | None) -> list[dict[str, Any]]:
        return [c.to_dict(context, self.extractor or "", self.extractor_version) for c in self.chunks]

    def to_dict(self, context: ParseContext | None = None) -> dict[str, Any]:
        return {
            "status": self.status,
            "format": self.format,
            "error_code": self.error_code,
            "message": self.message,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "cache": self.cache,
            "limitations": list(self.limitations),
            "outline": [
                {"level": o.level, "title": o.title, "ordinal": o.ordinal, "locator": dict(o.locator)}
                for o in self.outline
            ],
            "chunks": self.chunk_dicts(context),
            "stats": dict(self.stats),
        }


class ParseFailure(Exception):
    """解析中止（文件内容类问题）；由 parse_document 转为 failed 结果。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
