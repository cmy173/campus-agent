# -*- coding: utf-8 -*-
"""复习卷引擎 B 调用示例（可直接运行）。

模型网络调用位置以「模拟响应」占位，B 接入时替换为模型网关调用即可；
密钥获取/超时/取消/重试/持久化均在 B 侧，本引擎为零网络纯逻辑。

运行：
    cd packages/learning
    PYTHONPATH=src python examples/review_b_usage.py
"""
import json

from learning.review import (
    REVIEW_ENGINE_VERSION,
    BlueprintConstraints,
    analyze_materials,
    build_blueprint,
    build_generation_request,
    build_snapshot,
    parse_and_validate_output,
    select_bank_questions,
)

TYPES = {"choice": 2, "true_false": 1, "fill_blank": 1, "calculation": 1, "short_answer": 1}


def _section_kps(bp):
    seen = {}
    for plan in bp.kp_plan:
        seen.setdefault(plan.section, plan.kp_id)
    return list(seen.values())


def main():
    # 1) B 把已解析的 Resource/Assignment 行 + 用户备注组装为快照素材
    snapshot = build_snapshot(
        course_id="course_demo",
        course_title="数据结构与算法",
        materials=[
            {
                "material_id": "res_1", "kind": "resource", "title": "第1章 绪论.pptx",
                "version": "2", "format": "pptx", "parse_status": "ready",
                "chunks": [
                    {"chunk_id": "ch1_1", "ordinal": 0, "kind": "heading", "text": "第1章 绪论",
                     "locator": {"kind": "slide", "slide": 1}, "heading_path": ["第1章 绪论"]},
                    {"chunk_id": "ch1_2", "ordinal": 1,
                     "text": "算法的五个特性：有穷性、确定性、可行性、输入与输出。时间复杂度的渐进记号包括 O、Ω 与 Θ。",
                     "locator": {"kind": "slide", "slide": 2}, "heading_path": ["第1章 绪论"]},
                ],
            },
            {
                "material_id": "res_2", "kind": "resource", "title": "第2章 线性表.pdf",
                "version": "1", "format": "pdf", "parse_status": "ready",
                "chunks": [
                    {"chunk_id": "ch2_1", "ordinal": 0, "kind": "heading", "text": "第2章 线性表",
                     "locator": {"kind": "page", "page": 1}, "heading_path": ["第2章 线性表"]},
                    {"chunk_id": "ch2_2", "ordinal": 1,
                     "text": "顺序表按下标随机访问为 O(1)；链表插入删除不需移动元素。栈是后进先出结构，队列是先进先出结构。",
                     "locator": {"kind": "page", "page": 2}, "heading_path": ["第2章 线性表"]},
                ],
            },
            {
                "material_id": "asg_1", "kind": "assignment", "title": "作业1：线性表",
                "version": "2026-10-10", "format": "text", "parse_status": "ready",
                "chunks": [
                    {"chunk_id": "hw_1", "ordinal": 0,
                     "text": "作业要求：实现顺序表的插入与删除并分析时间复杂度，给出栈与队列的典型应用。",
                     "locator": {"kind": "text", "start_offset": 0, "end_offset": 40}},
                ],
            },
        ],
        notes="重点考察时间复杂度的渐进记号。\n答案单独显示。",
        provider={"provider_id": "prov_1", "protocol": "openai_chat", "model": "local-qwen",
                  "base_url": "http://127.0.0.1:8080/v1"},  # 密钥由 B 保管，不进入本引擎
    )
    analysis = analyze_materials(snapshot)
    print("engine:", REVIEW_ENGINE_VERSION)
    print("kps:", [kp.name for kp in analysis.knowledge_points[:8]])

    # 2) 蓝图 + 生成请求（消息可直接交给 B 的模型网关）
    constraints = BlueprintConstraints.from_dict(
        {"mode": "ai", "types": TYPES, "total_points": 50}
    )
    bp = build_blueprint(snapshot, constraints, analysis=analysis)
    req = build_generation_request(snapshot, bp, analysis=analysis)
    print("blueprint:", bp.blueprint_id, "questions:", bp.total_questions, "points:", bp.total_points)
    print("request chars:", req.estimated_chars, "chunks:", req.included_chunk_ids)

    # ---- B：把 req.messages 交给用户配置的模型（此处用模拟响应替代真实调用）----
    ks = _section_kps(bp)

    def kp(i):
        return [ks[i % len(ks)]]

    raw = json.dumps({"title": "数据结构与算法模拟卷", "questions": [
        {"qtype": "choice", "stem": "时间复杂度渐进记号 O 通常表示（ ）。",
         "options": [{"key": "A", "text": "上界"}, {"key": "B", "text": "下界"},
                     {"key": "C", "text": "精确界"}, {"key": "D", "text": "均摊界"}],
         "answer": "A", "explanation": "O 记法给出渐进上界。", "scoring": [], "kp_ids": kp(0),
         "difficulty": "easy", "source_refs": [{"chunk_id": "ch1_2"}]},
        {"qtype": "true_false", "stem": "队列是先进先出结构。", "options": [], "answer": True,
         "explanation": "课件说明队列先进先出、栈后进先出。", "scoring": [], "kp_ids": kp(1),
         "difficulty": "easy", "source_refs": [{"chunk_id": "ch2_2"}]},
        {"qtype": "fill_blank", "stem": "队列的操作特性是 ____（先进先出/后进先出）。",
         "options": [], "answer": ["先进先出"], "explanation": "队尾入队、队头出队。", "scoring": [],
         "kp_ids": kp(2), "difficulty": "easy", "source_refs": [{"chunk_id": "ch2_2"}, {"notes_line": 1}]},
        {"qtype": "choice", "stem": "顺序表按下标访问元素的时间复杂度是（ ）。",
         "options": [{"key": "A", "text": "O(1)"}, {"key": "B", "text": "O(log n)"},
                     {"key": "C", "text": "O(n)"}, {"key": "D", "text": "O(n^2)"}],
         "answer": "A", "explanation": "顺序表随机访问按下标直接定位。", "scoring": [], "kp_ids": kp(3),
         "difficulty": "easy", "source_refs": [{"chunk_id": "ch2_2"}]},
        {"qtype": "calculation", "stem": "分析顺序表插入操作的平均时间复杂度，并说明与链表的差异。",
         "options": [], "answer": "顺序表插入平均移动一半元素，时间复杂度 O(n)；链表插入本身 O(1)，但定位前驱为 O(n)。",
         "explanation": "按移动元素次数求期望。", "scoring": ["给出平均移动次数（2分）", "复杂度结论（2分）"],
         "kp_ids": kp(4), "difficulty": "medium",
         "source_refs": [{"chunk_id": "ch2_2"}, {"chunk_id": "hw_1"}]},
        {"qtype": "short_answer", "stem": "结合作业要求，说明栈与队列的典型应用，并指出各章节的考察要点。",
         "options": [], "answer": "栈用于括号匹配/表达式求值；队列用于缓冲与调度；渐进记号用于复杂度描述。",
         "explanation": "综合课件与作业要求。", "scoring": ["栈应用（2分）", "队列应用（2分）", "复杂度表述（2分）"],
         "kp_ids": list(ks), "difficulty": "medium",
         "source_refs": [{"chunk_id": "hw_1"}, {"notes_line": 2}]},
    ]}, ensure_ascii=False)

    # 3) 校验（B 在 report.repair_needed 时按 report.repair_rounds_max 调用 build_repair_request 重试）
    outcome = parse_and_validate_output(raw, snapshot, bp)
    report = outcome.report
    errors = [i for i in report.issues if i.severity == "error"]
    warnings = [i for i in report.issues if i.severity == "warning"]
    print("status:", report.status, "errors:", len(errors), "warnings:", len(warnings))
    print("warning codes:", sorted({i.code for i in warnings}))
    if errors:
        for issue in errors:
            print("  ERROR", issue.code, issue.message.encode("unicode_escape")[:80])
    cand = outcome.candidate
    print("candidate:", cand.candidate_id, "questions:", len(cand.questions),
          "points:", report.total_points, "partial:", cand.partial)
    print("first ref:", cand.questions[0].source_refs[0].label)

    # ---- 4) 混合模式：A 的候选（connectors.questionbanks 平铺形状）----
    candidates = [
        {
            "candidate_id": "qb_" + "0" * 24,
            "stem": "顺序表按下标访问的时间复杂度是？",
            "type": "choice",
            "options": [{"label": "A", "text": "O(1)"}, {"label": "B", "text": "O(log n)"},
                        {"label": "C", "text": "O(n)"}, {"label": "D", "text": "O(n^2)"}],
            "answer": "A", "analysis": "顺序表随机访问按下标直接定位。",
            "knowledge_point": "第2章 线性表", "difficulty": "easy",
            "source_url": "https://example.com/q/1", "source_site": "example题库",
            "source_locator": {"page_title": "线性表练习"}, "fetched_at": "2026-10-11T09:00:00+08:00",
            "origin": "online", "license_note": "仅供个人学习", "extract_confidence": 0.9,
        },
    ]
    constraints_h = BlueprintConstraints.from_dict(
        {"mode": "hybrid", "types": TYPES, "total_points": 50}
    )
    bp_h = build_blueprint(snapshot, constraints_h, analysis=analysis)
    selection = select_bank_questions(bp_h, candidates)
    print("hybrid selected:", [(s.candidate.candidate_id, s.candidate.qtype, round(s.match_score, 2))
                               for s in selection.selected])
    print("hybrid rejected:", [(r.candidate_id, r.reason) for r in selection.rejected])
    print("hybrid deficits:", [(d.qtype, d.reason) for d in selection.deficits])


if __name__ == "__main__":
    main()
