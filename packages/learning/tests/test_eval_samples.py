"""D3 解析评估：对 samples/evaluation/ 的人工标注文档核对正文与页码/幻灯片位置。

标注文件 labels.json 为人工标注（对照自编源文档撰写，独立于解析器输出）；
本测试用 parse_document 实际输出对照标注，差异即评测发现（见 docs/D3-解析评估报告.md）。
评估结论仅针对这 5 份自编样例，不泛化到真实课程材料。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from learning.parse import ParseContext, parse_document

EVAL_DIR = Path(__file__).resolve().parent.parent / "samples" / "evaluation"
LABELS = json.loads((EVAL_DIR / "labels.json").read_text(encoding="utf-8"))

_EXTS = {".docx", ".pptx", ".pdf"}


def _parse(entry: dict):
    data = (EVAL_DIR / entry["file"]).read_bytes()
    ctx_cfg = LABELS["context"]
    context = ParseContext(
        resource_id=f"res_eval_{entry['file']}",
        resource_version=ctx_cfg["resource_version"],
        content_hash=hashlib.sha256(data).hexdigest(),
        job_id=ctx_cfg["job_id"],
        observed_at=ctx_cfg["observed_at"],
    )
    return parse_document(data, filename=entry["file"], context=context)


def _match(chunks: list, locator: dict) -> list:
    return [c for c in chunks if all(c.locator.get(k) == v for k, v in locator.items())]


@pytest.mark.parametrize("entry", LABELS["documents"], ids=[e["file"] for e in LABELS["documents"]])
def test_document_matches_hand_label(entry: dict) -> None:
    path = EVAL_DIR / entry["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], "样例字节与标注不符"

    result = _parse(entry)
    exp = entry["expect"]

    assert result.status == exp["status"]
    assert result.stats["units"] == exp["units"]
    assert result.stats["units_label"] == exp["units_label"]
    assert len(result.chunks) == exp["chunk_count"]
    assert "\n".join(c.text for c in result.chunks) == exp["full_text"]

    if exp.get("message_contains"):
        assert exp["message_contains"] in (result.message or "")
    else:
        assert result.message is None

    for anchor in exp["anchors"]:
        matches = _match(result.chunks, anchor["locator"])
        assert matches, f"定位 {anchor['locator']} 处未找到片段"
        chunk = next((c for c in matches if anchor["contains"] in c.text), None)
        assert chunk is not None, f"定位 {anchor['locator']} 处未找到包含 {anchor['contains']!r} 的片段"
        if "kind" in anchor:
            assert chunk.kind == anchor["kind"], f"{anchor['contains']!r} 的类型不符"
        if "heading_path" in anchor:
            assert list(chunk.heading_path) == anchor["heading_path"]
        if "heading_level" in anchor:
            assert any(
                o.level == anchor["heading_level"] and anchor["contains"] in o.title for o in result.outline
            ), f"大纲中缺少层级为 {anchor['heading_level']} 的 {anchor['contains']!r}"

    for locator in exp.get("no_chunks_at", []):
        assert not _match(result.chunks, locator), f"定位 {locator} 处不应有片段"

    for text in exp.get("absent", []):
        assert all(text not in c.text for c in result.chunks), f"{text!r} 不应出现在任何片段文本中"

    for fragment in exp.get("limitations_contains", []):
        assert any(fragment in lim for lim in result.limitations), f"限制说明缺少 {fragment!r}"

    assert all(c.locator.get("kind") in {"page", "slide", "text"} for c in result.chunks), "定位类型必须对齐契约"


def test_reparse_is_deterministic() -> None:
    for entry in LABELS["documents"]:
        first = _parse(entry)
        second = _parse(entry)
        shape = lambda r: [(c.ordinal, c.text, c.locator, c.kind, list(c.heading_path)) for c in r.chunks]
        assert shape(first) == shape(second)
        assert first.status == second.status
        assert first.limitations == second.limitations


def test_labels_cover_exactly_the_sample_files() -> None:
    on_disk = sorted(p.name for p in EVAL_DIR.iterdir() if p.suffix in _EXTS)
    in_labels = sorted(e["file"] for e in LABELS["documents"])
    assert on_disk == in_labels
    assert 3 <= len(in_labels) <= 5, "D3 评估小样本应为 3~5 份"
