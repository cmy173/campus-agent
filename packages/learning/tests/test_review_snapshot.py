"""复习卷快照测试：材料归一化、缺口（gaps）、指纹敏感性与密钥过滤。"""

from __future__ import annotations

import json

import pytest
from review_fixtures import NOTES, sample_materials, sample_provider

from learning.review import (
    MaterialInput,
    ReviewError,
    ReviewErrorCode,
    build_snapshot,
    material_fingerprint,
    snapshot_overview,
)


def _material(**overrides):
    base = {
        "material_id": "m1",
        "kind": "resource",
        "title": "测试课件.pptx",
        "version": "1",
        "format": "pptx",
        "parse_status": "ready",
        "chunks": [{"ordinal": 0, "text": "正文内容"}],
    }
    base.update(overrides)
    return base


def test_build_snapshot_splits_materials_by_kind():
    snap = build_snapshot("course_ds", "数据结构与算法", sample_materials(), notes=NOTES)
    assert [m.material_id for m in snap.materials] == ["res_ch3", "res_ch4"]
    assert [m.material_id for m in snap.assignments] == ["asg_hw2"]
    assert [m.material_id for m in snap.attachments] == ["att_hw2"]
    assert snap.has_readable_text()
    assert snap.notes == NOTES  # 备注原文逐字保存（不 strip、不摘要）
    assert all(m.parse_status == "ready" for m in snap.all_materials())


def test_chunk_id_default_and_conflict():
    snap = build_snapshot("c1", "课程", [_material()])
    assert [c.chunk_id for c in snap.materials[0].chunks] == ["m1:c0000"]
    with pytest.raises(ReviewError) as err:
        build_snapshot(
            "c1",
            "课程",
            [
                _material(chunks=[{"chunk_id": "x1", "ordinal": 0, "text": "a"}]),
                _material(
                    material_id="m2",
                    chunks=[{"chunk_id": "x1", "ordinal": 0, "text": "b"}],
                ),
            ],
        )
    assert err.value.code == ReviewErrorCode.INVALID_INPUT
    assert "x1" in err.value.message


def test_invalid_inputs_rejected():
    with pytest.raises(ReviewError) as err:
        build_snapshot("", "课程", [_material()])
    assert err.value.code == ReviewErrorCode.INVALID_INPUT
    with pytest.raises(ReviewError) as err:
        build_snapshot("c1", "课程", [_material(kind="video")])
    assert err.value.code == ReviewErrorCode.INVALID_INPUT


def test_gaps_for_unready_materials():
    snap = build_snapshot(
        "c1",
        "课程",
        [
            _material(material_id="m_ocr", parse_status="needs_ocr"),
            _material(
                material_id="m_failed",
                parse_status="failed",
                limitations=["页数超过上限 300，已截断"],
            ),
            _material(material_id="m_pending", parse_status="pending"),
            _material(material_id="m_empty", chunks=[{"ordinal": 0, "text": "  "}]),
            _material(material_id="m_missing", parse_status="missing"),
            MaterialInput(
                material_id="att_x",
                kind="attachment",
                title="未关联附件.docx",
                parse_status="ready",
                chunks=[{"ordinal": 0, "text": "附件正文"}],
            ),
        ],
    )
    codes = {(g.material_id, g.code) for g in snap.gaps}
    assert ("m_ocr", "NEEDS_OCR") in codes
    assert ("m_failed", "MATERIAL_UNPARSED") in codes
    assert ("m_failed", "TRUNCATED") in codes
    assert ("m_pending", "MATERIAL_UNPARSED") in codes
    assert ("m_empty", "EMPTY_TEXT") in codes
    assert ("m_missing", "MISSING_ATTACHMENT") in codes
    assert ("att_x", "UNLINKED_ATTACHMENT") in codes
    errors = [g for g in snap.gaps if g.severity == "error"]
    assert len(errors) == 5
    severities = {g.code: g.severity for g in snap.gaps}
    assert severities["TRUNCATED"] == "warning"
    assert severities["UNLINKED_ATTACHMENT"] == "warning"


def test_no_materials_gap():
    snap = build_snapshot("c1", "课程", [])
    assert any(g.code == "NO_MATERIALS" and g.severity == "error" for g in snap.gaps)
    assert not snap.has_readable_text()


def test_fingerprint_changes_with_notes_materials_version():
    base = build_snapshot("course_ds", "数据结构与算法", sample_materials(), notes=NOTES)
    same = build_snapshot("course_ds", "数据结构与算法", sample_materials(), notes=NOTES)
    assert base.fingerprint == same.fingerprint
    assert material_fingerprint(base) == base.fingerprint

    notes_changed = build_snapshot("course_ds", "数据结构与算法", sample_materials(), notes=NOTES + "\n补充一条")
    assert notes_changed.fingerprint != base.fingerprint

    text_changed = sample_materials()
    text_changed[0].chunks[1].text = text_changed[0].chunks[1].text + "（修订）"
    assert build_snapshot("course_ds", "数据结构与算法", text_changed, notes=NOTES).fingerprint != base.fingerprint

    version_changed = sample_materials()
    version_changed[0].version = "3"
    assert build_snapshot("course_ds", "数据结构与算法", version_changed, notes=NOTES).fingerprint != base.fingerprint


def test_provider_sanitized_and_key_never_in_snapshot():
    snap = build_snapshot(
        "course_ds",
        "数据结构与算法",
        sample_materials(),
        notes=NOTES,
        provider=sample_provider(),
    )
    assert "api_key" not in snap.provider
    assert snap.provider.get("model") == "local-qwen"
    dumped = json.dumps(snap.to_dict(), ensure_ascii=False)
    assert "sk-must-not-appear-anywhere" not in dumped
    assert "sk-must-not-appear-anywhere" not in json.dumps(snapshot_overview(snap), ensure_ascii=False)


def test_provider_url_query_and_nested_secrets_dropped():
    snap = build_snapshot(
        "c1",
        "课程",
        [_material()],
        provider={
            "base_url": "https://model.example.com/v1?sig=temporary-signature",
            "headers": {"Authorization": "Bearer zzz", "X-Trace": "t1"},
            "token": "tok-123",
        },
    )
    assert snap.provider["base_url"] == "https://model.example.com/v1"
    assert "Authorization" not in snap.provider["headers"]
    assert snap.provider["headers"]["X-Trace"] == "t1"
    assert "token" not in snap.provider
    assert any("查询参数" in w for w in snap.warnings)

    # 密钥/临时签名不参与指纹：同一非密钥配置下，敏感项被丢弃后指纹一致
    clean = build_snapshot(
        "c1",
        "课程",
        [_material()],
        provider={"base_url": "https://model.example.com/v1", "headers": {"X-Trace": "t1"}},
    )
    assert snap.fingerprint == clean.fingerprint

    # 模型地址本身变化 → 新指纹（配置变化可被 B 察觉）
    other = build_snapshot(
        "c1", "课程", [_material()], provider={"base_url": "https://other.example.com/v1"}
    )
    assert other.fingerprint != clean.fingerprint


def test_snapshot_overview_ready_and_entries():
    snap = build_snapshot("course_ds", "数据结构与算法", sample_materials(), notes=NOTES)
    overview = snapshot_overview(snap)
    assert overview["ready"] is True
    assert overview["has_notes"] is True
    assert overview["notes_chars"] == len(NOTES)
    assert overview["materials"][0]["locator_kinds"] == ["slide"]
    assert overview["materials"][1]["locator_kinds"] == ["page"]
    assert overview["attachments"][0]["linked_assignment_id"] == "asg_hw2"

    pending = build_snapshot("c1", "课程", [_material(parse_status="pending")])
    assert snapshot_overview(pending)["ready"] is False
