"""D4 回放文本整理测试：字幕规范化（A10 字段形态）、主题分段保留秒数、ContentChunk、状态约定。"""

import json

import pytest

from learning.parse.types import ParseContext
from learning.samples import list_transcripts, load_sample, load_transcript, parse_inputs
from learning.taskgen import build_tasks
from learning.transcript import (
    TranscriptErrorCode,
    TranscriptFormatError,
    TranscriptLimits,
    TranscriptStatus,
    TopicConfig,
    normalize_transcript,
    segment_topics,
    transcript_chunks,
)

PLATFORM_ROWS = [
    {"BeginSec": "15", "EndSec": "19", "Text": "上一讲我们介绍了图的广度优先遍历"},
    {
        "BeginSec": 19,
        "EndSec": 26,
        "Text": "这一讲来看深度优先遍历的实现与复杂度",
        "TransText": "In this lecture we look at the implementation and complexity of depth-first traversal",
    },
]

CONTRACT_ROWS = [
    {"start_seconds": 15, "end_seconds": 19, "text": "上一讲我们介绍了图的广度优先遍历"},
    {
        "start_seconds": 19,
        "end_seconds": 26,
        "text": "这一讲来看深度优先遍历的实现与复杂度",
        "translated_text": "In this lecture we look at the implementation and complexity of depth-first traversal",
    },
]


def rec(status, minutes=90, **kwargs):
    from learning.models import Recording

    return Recording(
        id=f"rec_{status}",
        course_id="crs_demo",
        title=f"回放 {status}",
        duration_seconds=minutes * 60,
        transcript_status=status,
        **kwargs,
    )


def test_normalize_accepts_contract_and_platform_keys():
    from_contract = normalize_transcript("rec_1", CONTRACT_ROWS)
    from_platform = normalize_transcript("rec_1", PLATFORM_ROWS)

    assert from_contract.to_dict() == from_platform.to_dict()
    assert from_contract.segments[1].translated_text.startswith("In this lecture")


def test_normalize_sorts_by_start_and_keeps_seconds():
    rows = [
        {"start_seconds": 30.5, "end_seconds": 40, "text": "后出现的片段"},
        {"start_seconds": 10, "end_seconds": 20, "text": "先出现的片段"},
    ]
    transcript = normalize_transcript("rec_1", rows)

    assert [s.text for s in transcript.segments] == ["先出现的片段", "后出现的片段"]
    assert transcript.to_dict()["segments"][0]["start_seconds"] == 10
    assert transcript.to_dict()["segments"][1]["start_seconds"] == 30.5


@pytest.mark.parametrize(
    "row",
    [
        {"start_seconds": 10, "end_seconds": 20},  # 缺文本
        {"start_seconds": 10, "end_seconds": 20, "text": "   "},  # 空白文本
        {"start_seconds": -1, "end_seconds": 20, "text": "负起点"},
        {"start_seconds": 20, "end_seconds": 10, "text": "终点早于起点"},
        {"start_seconds": "abc", "end_seconds": 20, "text": "无法换算"},
        {"start_seconds": True, "end_seconds": 20, "text": "布尔不算数字"},
        {"start_seconds": 10, "end_seconds": 20, "text": "译文类型错", "translated_text": 42},
    ],
)
def test_normalize_rejects_invalid_segments(row):
    with pytest.raises(TranscriptFormatError) as excinfo:
        normalize_transcript("rec_1", [row])
    assert excinfo.value.code == TranscriptErrorCode.SEGMENT_INVALID


def test_normalize_limits_and_provenance():
    with pytest.raises(TranscriptFormatError) as excinfo:
        normalize_transcript(
            "rec_1",
            [{"start_seconds": 0, "end_seconds": 1, "text": "x"}] * 3,
            limits=TranscriptLimits(max_segments=2),
        )
    assert excinfo.value.code == TranscriptErrorCode.TOO_MANY_SEGMENTS

    with pytest.raises(TranscriptFormatError) as excinfo:
        normalize_transcript(
            "rec_1",
            [{"start_seconds": 0, "end_seconds": 7200, "text": "x"}],
            limits=TranscriptLimits(max_total_seconds=3600),
        )
    assert excinfo.value.code == TranscriptErrorCode.TRANSCRIPT_TOO_LONG

    with pytest.raises(TranscriptFormatError) as excinfo:
        normalize_transcript("rec_1", [], provenance="asr")
    assert excinfo.value.code == TranscriptErrorCode.INVALID_PROVENANCE


def test_sample_transcript_loads_and_aligns_with_a10_shape():
    assert "transcript-sample" in list_transcripts()
    transcript = load_transcript("transcript-sample")

    assert transcript.recording_id == "rec_cs261_lec04"
    assert transcript.provenance == "platform"
    assert transcript.language == "zh-CN"
    assert len(transcript.segments) == 6
    assert transcript.extractor() == "platform-subtitle"

    # A 样例顶层附带的 locator_example 等额外字段不影响加载
    raw = json.loads(
        (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "samples"
            / "transcript"
            / "transcript-sample.json"
        ).read_text(encoding="utf-8")
    )
    raw["locator_example"] = {"type": "time_range", "start_seconds": 19, "end_seconds": 26}
    from learning.transcript import Transcript

    assert Transcript.from_dict(raw).to_dict() == transcript.to_dict()


def test_segment_topics_split_on_gap_and_keep_real_boundaries():
    transcript = load_transcript("transcript-sample")
    spans = segment_topics(transcript)

    assert len(spans) == 2
    first, second = spans
    assert (first.start_seconds, first.end_seconds) == (15, 33)
    assert (first.segment_start, first.segment_end) == (0, 2)
    assert (second.start_seconds, second.end_seconds) == (200, 380)
    assert (second.segment_start, second.segment_end) == (3, 5)

    # 边界与摘要必须来自真实片段，不得虚构
    for span in spans:
        assert span.start_seconds == transcript.segments[span.segment_start].start_seconds
        assert span.end_seconds == transcript.segments[span.segment_end].end_seconds
        assert transcript.segments[span.segment_start].text.strip().startswith(span.excerpt.rstrip("…"))
    assert first.to_dict()["segment_count"] == 3


def test_segment_topics_split_on_max_duration():
    rows = [
        {"start_seconds": i * 100, "end_seconds": i * 100 + 90, "text": f"片段{i}"}
        for i in range(6)
    ]
    transcript = normalize_transcript("rec_1", rows)

    spans = segment_topics(transcript, TopicConfig(gap_seconds=120, max_topic_seconds=300))
    assert [(s.start_seconds, s.end_seconds) for s in spans] == [
        (0, 290),
        (300, 590),
    ]
    assert all(s.segment_count >= 1 for s in spans)


def test_segment_topics_empty_transcript():
    transcript = normalize_transcript("rec_1", [])
    assert segment_topics(transcript) == []
    assert transcript_chunks(transcript) == []


def test_transcript_chunks_use_timestamp_locator_and_provenance():
    transcript = load_transcript("transcript-sample")
    context = ParseContext(
        resource_id="res_cs261_rec04",
        resource_version=1,
        content_hash="sha256:" + "0" * 64,
        job_id="job_1",
        observed_at="2026-10-09T10:00+08:00",
    )
    chunks = transcript_chunks(transcript, context)

    assert [c.ordinal for c in chunks] == list(range(6))
    assert chunks[0].locator == {"kind": "timestamp", "start_seconds": 15, "end_seconds": 19}
    assert chunks[1].locator["start_seconds"] == 19
    assert chunks[1].text == "这一讲来看深度优先遍历的实现与复杂度"

    payload = chunks[1].to_dict(context, transcript.extractor(), "0.1.0")
    assert payload["locator"]["kind"] == "timestamp"
    assert payload["resource_id"] == "res_cs261_rec04"
    assert payload["resource_version"] == 1
    assert payload["provenance"]["extractor"] == "platform-subtitle"
    assert payload["provenance"]["fact_source"] == "platform"
    assert set(payload) == {
        "ordinal", "text", "locator", "kind", "heading_path", "resource_id",
        "resource_version", "provenance",
    }


def test_transcript_pipeline_is_deterministic():
    transcript = load_transcript("transcript-sample")
    first = (transcript.to_dict(), [s.to_dict() for s in segment_topics(transcript)])
    second = (load_transcript("transcript-sample").to_dict(), [s.to_dict() for s in segment_topics(load_transcript("transcript-sample"))])
    assert first == second


def test_status_ready_is_the_only_content_state():
    for status in TranscriptStatus.ALL:
        assert TranscriptStatus.is_ready(status) is (status == "ready")
    assert TranscriptStatus.is_ready("none") is False
    assert TranscriptStatus.is_ready(None) is False


def test_taskgen_only_ready_transcripts_are_treated_as_content():
    statuses = ("pending", "processing", "unknown", "unavailable", "failed")
    recs = [rec(status) for status in statuses] + [rec("ready")]
    result = build_tasks([], [], recs)
    by_title = {t.title: t for t in result.tasks}

    assert result.warnings == []
    for status in statuses:
        task = by_title[f"回放 {status}"]
        assert task.type == "watching"
        assert task.estimated_minutes == 90  # 仅按时长（5400 秒）
        assert task.completion_criteria.endswith(TranscriptStatus.non_ready_note(status))

    # pending/processing 是“等待更新”而非“无字幕”：不推断字幕不存在
    for status in ("pending", "processing"):
        note = by_title[f"回放 {status}"].completion_criteria
        assert "待更新" in note and "无字幕" not in note

    ready_task = by_title["回放 ready"]
    assert "未做内容解析" not in ready_task.completion_criteria
    assert ready_task.estimated_minutes == 90


def test_non_ready_note_matches_a10_section2_branches():
    note = TranscriptStatus.non_ready_note
    assert "待更新" in note("pending") and "无字幕" not in note("pending")
    assert "待更新" in note("processing") and "无字幕" not in note("processing")
    assert "无字幕" in note("unavailable")
    assert "暂不可得" in note("failed")
    assert "状态未知" in note("unknown")
    # 未知取值保守按无字幕处理，不假设有字幕
    assert note("nonsense") == note("unknown")


def test_baseline_recordings_use_contract_status_values():
    inputs = parse_inputs(load_sample("baseline"))
    statuses = [r.transcript_status for r in inputs["recordings"]]
    assert statuses == ["pending", "unknown"]
    pending = next(r for r in inputs["recordings"] if r.transcript_status == "pending")
    assert pending.subtitle_languages == []
    assert pending.transcript_next_check_at == "2026-10-10T10:00+08:00"
