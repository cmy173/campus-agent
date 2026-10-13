"""分块装配：序、标题层级栈、大纲条目、片段数上限（各解析器共用）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import OutlineEntry, ParsedChunk


@dataclass
class ParserOutput:
    chunks: list[ParsedChunk]
    outline: list[OutlineEntry]
    limitations: list[str] = field(default_factory=list)
    units: int = 0
    chars: int = 0
    needs_ocr: bool = False


class ChunkBuilder:
    """按出现顺序追加片段；标题维护层级栈（片段 heading_path 含自身/最近祖先标题）。"""

    def __init__(self, max_chunks: int) -> None:
        self.chunks: list[ParsedChunk] = []
        self.outline: list[OutlineEntry] = []
        self.overflow = False
        self._max_chunks = max_chunks
        self._stack: list[tuple[int, str]] = []

    def add(
        self,
        text: str,
        locator: dict[str, Any],
        *,
        kind: str = "body",
        heading_level: int | None = None,
    ) -> ParsedChunk | None:
        if len(self.chunks) >= self._max_chunks:
            self.overflow = True
            return None
        if heading_level is not None:
            while self._stack and self._stack[-1][0] >= heading_level:
                self._stack.pop()
            self._stack.append((heading_level, text))
            kind = "heading"
        chunk = ParsedChunk(
            ordinal=len(self.chunks),
            text=text,
            locator=locator,
            kind=kind,
            heading_path=tuple(title for _, title in self._stack),
        )
        self.chunks.append(chunk)
        if kind == "heading":
            self.outline.append(
                OutlineEntry(level=heading_level or 1, title=text, ordinal=chunk.ordinal, locator=dict(locator))
            )
        return chunk

    def finish(self, *, units: int, chars: int, needs_ocr: bool = False) -> ParserOutput:
        limitations: list[str] = []
        if self.overflow:
            limitations.append(f"片段数超过上限 {self._max_chunks}，后续内容已截断")
        return ParserOutput(
            chunks=self.chunks,
            outline=self.outline,
            limitations=limitations,
            units=units,
            chars=chars,
            needs_ocr=needs_ocr,
        )
