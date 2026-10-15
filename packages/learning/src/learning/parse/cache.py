"""解析结果缓存：键 = content_hash + 解析器版本（+ 第三方库版本 + 影响内容的限制项）。

只缓存成功结果（ready/needs_ocr）；失败不缓存（下次可因环境变化重试）。
缓存文件仅存“内容派生”字段；provenance/任务号在每次调用时现盖。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .types import OutlineEntry, ParsedChunk, ParseResult, PARSER_VERSION


def cache_key(content_hash: str, fmt: str, lib_version: str, limits_shape: str) -> str:
    raw = f"{content_hash}|{fmt}|{PARSER_VERSION}|{lib_version}|{limits_shape}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load(cache_dir: Path, key: str) -> ParseResult | None:
    path = cache_dir / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    return _from_payload(data)


def save(cache_dir: Path, key: str, result: ParseResult) -> None:
    payload = {
        "status": result.status,
        "format": result.format,
        "extractor": result.extractor,
        "extractor_version": result.extractor_version,
        "limitations": result.limitations,
        "outline": [
            {"level": o.level, "title": o.title, "ordinal": o.ordinal, "locator": o.locator}
            for o in result.outline
        ],
        "chunks": [
            {
                "ordinal": c.ordinal,
                "text": c.text,
                "locator": c.locator,
                "kind": c.kind,
                "heading_path": list(c.heading_path),
            }
            for c in result.chunks
        ],
        "stats": {k: v for k, v in result.stats.items() if k not in {"elapsed_ms", "from_cache"}},
    }
    path = cache_dir / f"{key}.json"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # 缓存失败不影响解析结果


def _from_payload(data: dict) -> ParseResult:
    return ParseResult(
        status=data["status"],
        format=data.get("format"),
        extractor=data.get("extractor"),
        extractor_version=data.get("extractor_version", PARSER_VERSION),
        limitations=list(data.get("limitations", [])),
        outline=[
            OutlineEntry(level=o["level"], title=o["title"], ordinal=o["ordinal"], locator=o["locator"])
            for o in data.get("outline", [])
        ],
        chunks=[
            ParsedChunk(
                ordinal=c["ordinal"],
                text=c["text"],
                locator=c["locator"],
                kind=c.get("kind", "body"),
                heading_path=tuple(c.get("heading_path", [])),
            )
            for c in data.get("chunks", [])
        ],
        stats=dict(data.get("stats", {})),
    )
