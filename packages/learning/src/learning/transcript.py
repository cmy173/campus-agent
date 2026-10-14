"""D4 回放文本整理：已有字幕时间轴 → 规范化片段 / 主题分段 / ContentChunk（locator=timestamp）。

对齐 A10 约定（`connectors/samples/transcript-sample.json`）与契约 v0.1：

- 输入字幕片段：契约字段 `start_seconds/end_seconds/text/translated_text`，或平台原始形态
  `BeginSec/EndSec/Text/TransText`（数值或数字字符串；本模块做严格校验与兜底换算）。
- 转写状态（`recordings.transcript_status`）：仅 `ready` 视为有内容；其余五态一律不当作
  ready——不解析、不生成主题分段、不声称理解视频内容；`pending`/`processing` 的后续
  拉取由 A 侧负责（A10 §2，D 不自行轮询平台），本模块不发起网络请求。
- 主题分段只依据真实存在的片段边界与间隔切分，输出保留起止秒数；不虚构片段、不生成标题，
  摘要仅为原文首个片段的截断摘录。
- 自行语音转写（`provenance="self_asr"`）为可选增强：本期不实现，仅保留标注位与提取器名；
  资源可获取方式与成本上限待与用户确认（M1 任务书 D4-⑤）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .parse.types import ParsedChunk

TRANSCRIPT_VERSION = "0.1.0"

EXTRACTOR_BY_PROVENANCE = {
    "platform": "platform-subtitle",
    "self_asr": "asr",
}


class TranscriptStatus:
    """契约 recording.transcript_status 六态；D 端只把 ready 当作有内容。"""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    FAILED = "failed"

    ALL = (PENDING, PROCESSING, READY, UNAVAILABLE, UNKNOWN, FAILED)

    # A10 §2 消费分支：非 ready 状态下观看任务完成标准的备注（仅影响文案，不改变任务生成逻辑）。
    # pending/processing=等待平台更新；unavailable=确认无字幕；failed=暂不可得（A 侧重试）；
    # unknown=保守按无字幕处理（不可假设有或无）。
    NON_READY_NOTE = {
        PENDING: "（该回放字幕生成中，待更新，暂未做内容解析）",
        PROCESSING: "（该回放字幕处理中，待更新，暂未做内容解析）",
        UNAVAILABLE: "（该回放无字幕文本，未做内容解析）",
        FAILED: "（该回放字幕暂不可得，未做内容解析）",
        UNKNOWN: "（该回放字幕状态未知，按无字幕处理，未做内容解析）",
    }

    @classmethod
    def is_ready(cls, status: Any) -> bool:
        return status == cls.READY

    @classmethod
    def non_ready_note(cls, status: Any) -> str:
        """非 ready 状态的完成标准备注；未知取值保守按无字幕（不假设有字幕）。"""
        return cls.NON_READY_NOTE.get(status, cls.NON_READY_NOTE[cls.UNKNOWN])


class TranscriptErrorCode:
    SEGMENT_INVALID = "SEGMENT_INVALID"
    TOO_MANY_SEGMENTS = "TOO_MANY_SEGMENTS"
    TRANSCRIPT_TOO_LONG = "TRANSCRIPT_TOO_LONG"
    INVALID_PROVENANCE = "INVALID_PROVENANCE"


class TranscriptFormatError(Exception):
    """字幕数据格式问题（由调用方修正输入；不作为解析“失败”状态传播）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TranscriptLimits:
    max_segments: int = 5_000
    max_total_seconds: float = 4 * 3600.0


@dataclass
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    translated_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "start_seconds": _seconds_out(self.start_seconds),
            "end_seconds": _seconds_out(self.end_seconds),
            "text": self.text,
        }
        if self.translated_text is not None:
            out["translated_text"] = self.translated_text
        return out


@dataclass
class Transcript:
    recording_id: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None
    provenance: str = "platform"  # platform | self_asr（可选增强，见模块 docstring）
    resource_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recording_id": self.recording_id,
            "language": self.language,
            "provenance": self.provenance,
            "resource_version": self.resource_version,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], limits: TranscriptLimits | None = None) -> "Transcript":
        return normalize_transcript(
            data["recording_id"],
            data.get("segments") or [],
            language=data.get("language"),
            provenance=data.get("provenance", "platform"),
            resource_version=data.get("resource_version"),
            limits=limits,
        )

    def extractor(self) -> str:
        return EXTRACTOR_BY_PROVENANCE.get(self.provenance, self.provenance)


@dataclass
class TopicConfig:
    """主题分段的确定性切分阈值（不调用模型）。"""

    gap_seconds: float = 120.0
    max_topic_seconds: float = 600.0
    excerpt_chars: int = 60


@dataclass
class TopicSpan:
    ordinal: int
    start_seconds: float
    end_seconds: float
    segment_start: int  # 起始片段序号（含，0 起）
    segment_end: int  # 结束片段序号（含）
    excerpt: str  # 真实原文摘录（首个片段截断），非生成标题

    @property
    def segment_count(self) -> int:
        return self.segment_end - self.segment_start + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "start_seconds": _seconds_out(self.start_seconds),
            "end_seconds": _seconds_out(self.end_seconds),
            "segment_start": self.segment_start,
            "segment_end": self.segment_end,
            "segment_count": self.segment_count,
            "excerpt": self.excerpt,
        }


_START_KEYS = ("start_seconds", "BeginSec")
_END_KEYS = ("end_seconds", "EndSec")
_TEXT_KEYS = ("text", "Text")
_TRANSLATED_KEYS = ("translated_text", "TransText")


def normalize_transcript(
    recording_id: str,
    raw_segments: list[dict[str, Any]],
    *,
    language: str | None = None,
    provenance: str = "platform",
    resource_version: int | None = None,
    limits: TranscriptLimits | None = None,
) -> Transcript:
    """校验并规范化字幕片段：接受契约字段或平台原始字段（BeginSec/EndSec/Text/TransText）。"""
    if not recording_id:
        raise TranscriptFormatError(TranscriptErrorCode.SEGMENT_INVALID, "缺少 recording_id")
    if provenance not in EXTRACTOR_BY_PROVENANCE:
        raise TranscriptFormatError(
            TranscriptErrorCode.INVALID_PROVENANCE,
            f"未知的 provenance：{provenance!r}（仅支持 {sorted(EXTRACTOR_BY_PROVENANCE)}）",
        )
    effective_limits = limits or TranscriptLimits()
    if len(raw_segments) > effective_limits.max_segments:
        raise TranscriptFormatError(
            TranscriptErrorCode.TOO_MANY_SEGMENTS,
            f"字幕片段数 {len(raw_segments)} 超过上限 {effective_limits.max_segments}",
        )

    segments: list[TranscriptSegment] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise TranscriptFormatError(
                TranscriptErrorCode.SEGMENT_INVALID, f"片段 #{index} 不是对象：{raw!r}"
            )
        start = _coerce_seconds(_pick(raw, _START_KEYS), index, "start")
        end = _coerce_seconds(_pick(raw, _END_KEYS), index, "end")
        text = _pick(raw, _TEXT_KEYS)
        if not isinstance(text, str) or not text.strip():
            raise TranscriptFormatError(
                TranscriptErrorCode.SEGMENT_INVALID, f"片段 #{index} 文本为空或缺失"
            )
        translated = _pick(raw, _TRANSLATED_KEYS, required=False)
        if translated is not None and (not isinstance(translated, str) or not translated.strip()):
            raise TranscriptFormatError(
                TranscriptErrorCode.SEGMENT_INVALID, f"片段 #{index} translated_text 无效：{translated!r}"
            )
        if start < 0 or end < start:
            raise TranscriptFormatError(
                TranscriptErrorCode.SEGMENT_INVALID,
                f"片段 #{index} 起止秒数无效：start={start:g}, end={end:g}（要求 0 ≤ start ≤ end）",
            )
        segments.append(
            TranscriptSegment(
                start_seconds=start,
                end_seconds=end,
                text=text,
                translated_text=translated,
            )
        )

    segments.sort(key=lambda s: s.start_seconds)
    if segments and segments[-1].end_seconds > effective_limits.max_total_seconds:
        raise TranscriptFormatError(
            TranscriptErrorCode.TRANSCRIPT_TOO_LONG,
            f"字幕时间轴末尾 {segments[-1].end_seconds:g} 秒超过上限 {effective_limits.max_total_seconds:g} 秒",
        )
    return Transcript(
        recording_id=recording_id,
        segments=segments,
        language=language,
        provenance=provenance,
        resource_version=resource_version,
    )


def segment_topics(transcript: Transcript, config: TopicConfig | None = None) -> list[TopicSpan]:
    """确定性地把字幕切分为主题分段：间隔 > gap_seconds 或时长 > max_topic_seconds 即分段。"""
    effective = config or TopicConfig()
    segments = transcript.segments
    if not segments:
        return []

    spans: list[TopicSpan] = []

    def make_span(start_idx: int, end_idx: int) -> TopicSpan:
        excerpt = segments[start_idx].text.strip()
        if len(excerpt) > effective.excerpt_chars:
            excerpt = excerpt[: effective.excerpt_chars] + "…"
        return TopicSpan(
            ordinal=len(spans),
            start_seconds=segments[start_idx].start_seconds,
            end_seconds=segments[end_idx].end_seconds,
            segment_start=start_idx,
            segment_end=end_idx,
            excerpt=excerpt,
        )

    start_idx = 0
    for idx in range(1, len(segments)):
        prev, cur = segments[idx - 1], segments[idx]
        gap = cur.start_seconds - prev.end_seconds
        would_exceed = (
            cur.end_seconds - segments[start_idx].start_seconds
        ) > effective.max_topic_seconds
        if gap > effective.gap_seconds or would_exceed:
            spans.append(make_span(start_idx, idx - 1))
            start_idx = idx
    spans.append(make_span(start_idx, len(segments) - 1))
    return spans


def transcript_chunks(transcript: Transcript, context: Any = None) -> list[ParsedChunk]:
    """逐片段输出 ContentChunk（locator 为契约的 timestamp 形态，保留起止秒数）。

    `context` 为 `learning.parse.types.ParseContext`（B 提供资源溯源）；文本片段只含原文字幕
    （content-chunk schema 无译文位；译文保留在 `Transcript.segments[].translated_text`）。
    """
    return [
        ParsedChunk(
            ordinal=index,
            text=segment.text,
            locator={
                "kind": "timestamp",
                "start_seconds": _seconds_out(segment.start_seconds),
                "end_seconds": _seconds_out(segment.end_seconds),
            },
        )
        for index, segment in enumerate(transcript.segments)
    ]


def _pick(raw: dict[str, Any], keys: tuple[str, ...], *, required: bool = True) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    if required:
        raise TranscriptFormatError(
            TranscriptErrorCode.SEGMENT_INVALID, f"片段缺少字段 {'/'.join(keys)}"
        )
    return None


def _coerce_seconds(value: Any, index: int, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise TranscriptFormatError(
            TranscriptErrorCode.SEGMENT_INVALID, f"片段 #{index} {label} 秒数无效：{value!r}"
        )
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise TranscriptFormatError(
        TranscriptErrorCode.SEGMENT_INVALID, f"片段 #{index} {label} 秒数无法换算：{value!r}"
    )


def _seconds_out(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value
