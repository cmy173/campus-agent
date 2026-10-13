"""端到端链路测试：快照→分析→蓝图→请求→解析校验（AI）、题库→补题→合并（混合）、
纯逻辑（零网络）扫描、确定性、可序列化、密钥不外泄。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from review_fixtures import (
    NOTES,
    ai_valid_output,
    bank_candidates,
    sample_constraints,
    sample_snapshot,
    sample_provider,
)
from test_review_validate import _ai_complement_payload

from learning.review import (
    analyze_materials,
    build_blueprint,
    build_generation_request,
    merge_ai_with_bank,
    parse_and_validate_output,
    select_bank_questions,
    snapshot_overview,
    validate_bank_questions,
)

REVIEW_DIR = Path(__file__).resolve().parents[1] / "src" / "learning" / "review"
SECRET = "sk-must-not-appear-anywhere"
_NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(socket|requests|urllib|http|aiohttp|httpx|ftplib|telnetlib)\b",
    flags=re.MULTILINE,
)


def test_full_ai_chain():
    snap = sample_snapshot()
    overview = snapshot_overview(snap)
    assert overview["ready"] is True
    assert overview["notes_chars"] == len(NOTES)

    analysis = analyze_materials(snap)
    assert len(analysis.knowledge_points) == 8
    assert analysis.sections == ["第3章 栈与队列", "第4章 树与二叉树", "作业与附件"]

    bp = build_blueprint(snap, sample_constraints("ai"), analysis=analysis)
    req = build_generation_request(snap, bp, analysis=analysis)
    assert NOTES in next(m["content"] for m in req.messages if m["role"] == "user")

    outcome = parse_and_validate_output(ai_valid_output(), snap, bp)
    assert outcome.report.status == "ok_with_warnings"
    candidate = outcome.candidate
    assert candidate is not None
    assert candidate.fingerprint == bp.fingerprint == req.fingerprint
    assert candidate.notes == NOTES
    assert candidate.mode == "ai"
    assert [q.question_id for q in candidate.questions] == [f"rq_{i:03d}" for i in range(1, 12)]

    # 密钥不进任何对外结构
    blob = json.dumps(candidate.to_dict(), ensure_ascii=False) + json.dumps(overview, ensure_ascii=False)
    assert SECRET not in blob
    assert sample_provider()["api_key"] == SECRET  # fixture 自身含密钥，验证确被过滤


def test_full_hybrid_chain():
    snap = sample_snapshot()
    bp = build_blueprint(snap, sample_constraints("hybrid"))
    assert bp.search_requests  # 混合模式产生检索请求

    selection = select_bank_questions(bp, bank_candidates())
    assert [s.candidate.candidate_id for s in selection.selected] == ["bc_1", "bc_9", "bc_3", "bc_5", "bc_8"]

    req = build_generation_request(snap, bp, selected=selection, deficits=selection.deficits)
    user = next(m["content"] for m in req.messages if m["role"] == "user")
    assert "bc_1" in user and "bc_3" in user  # 候选题交给模型补题上下文

    # 纯题库模式如实报告缺口（不假装有模型修复）
    bank_outcome = validate_bank_questions(selection, snap, bp)
    assert bank_outcome.report.status == "errors"
    assert bank_outcome.report.repair_needed is False

    ai_outcome = parse_and_validate_output(
        json.dumps(_ai_complement_payload(), ensure_ascii=False), snap, bp
    )
    merged = merge_ai_with_bank(selection, ai_outcome, snap, bp)
    assert merged.report.status == "ok_with_warnings"
    assert merged.report.total_points == 100.0
    origins = {q.origin for q in merged.candidate.questions}
    assert origins == {"bank", "ai"}


def test_review_package_is_pure_logic_zero_network():
    files = sorted(REVIEW_DIR.glob("*.py"))
    assert files, f"未找到 review 包：{REVIEW_DIR}"
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in _NETWORK_IMPORT_RE.finditer(text):
            offenders.append(f"{path.name}: {match.group(0).strip()}")
    assert offenders == [], f"review 包不应导入网络库：{offenders}"


def test_chain_deterministic_across_runs():
    snap1, snap2 = sample_snapshot(), sample_snapshot()
    bp1 = build_blueprint(snap1, sample_constraints("ai"))
    bp2 = build_blueprint(snap2, sample_constraints("ai"))
    assert bp1.blueprint_id == bp2.blueprint_id
    assert bp1.fingerprint == bp2.fingerprint

    req1 = build_generation_request(snap1, bp1)
    req2 = build_generation_request(snap2, bp2)
    assert req1.messages == req2.messages
    assert req1.estimated_chars == req2.estimated_chars

    out1 = parse_and_validate_output(ai_valid_output(), snap1, bp1)
    out2 = parse_and_validate_output(ai_valid_output(), snap2, bp2)
    assert out1.candidate.candidate_id == out2.candidate.candidate_id

    sel1 = select_bank_questions(build_blueprint(snap1, sample_constraints("hybrid")), bank_candidates())
    sel2 = select_bank_questions(build_blueprint(snap2, sample_constraints("hybrid")), bank_candidates())
    assert [s.candidate.candidate_id for s in sel1.selected] == [s.candidate.candidate_id for s in sel2.selected]


def test_all_public_outputs_json_serializable():
    snap = sample_snapshot()
    bp = build_blueprint(snap, sample_constraints("ai"))
    selection = select_bank_questions(build_blueprint(snap, sample_constraints("hybrid")), bank_candidates())
    outcome = parse_and_validate_output(ai_valid_output(), snap, bp)
    req = build_generation_request(snap, bp)
    bank_outcome = validate_bank_questions(selection, snap, bp)

    for obj in (
        snapshot_overview(snap),
        bp.to_dict(),
        selection.to_dict(),
        req.to_dict(),
        outcome.to_dict(),
        bank_outcome.to_dict(),
        outcome.candidate.to_dict(),
        outcome.report.to_dict(),
    ):
        blob = json.dumps(obj, ensure_ascii=False)
        assert isinstance(json.loads(blob), dict)
    assert SECRET not in json.dumps(outcome.to_dict(), ensure_ascii=False)
