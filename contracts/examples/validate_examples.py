#!/usr/bin/env python3
"""校验 contracts/examples/ 全部样例：Schema 符合性 + 跨文件引用一致性。

用法：python contracts/examples/validate_examples.py
依赖：jsonschema（4.18+，含 referencing）。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, EMPTY_REGISTRY

HERE = pathlib.Path(__file__).resolve().parent
CONTRACTS = HERE.parent
SCHEMAS = CONTRACTS / "schemas"


def load(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_registry() -> Registry:
    pairs = []
    for p in sorted(SCHEMAS.glob("*.schema.json")):
        resource = Resource.from_contents(load(p), default_specification=DRAFT202012)
        pairs.append((p.name, resource))
    return EMPTY_REGISTRY.with_resources(pairs)


def resolve_target(doc, target: str):
    if target == "root":
        return [doc]
    if target.endswith("[*]"):
        key = target[:-3]
        if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
            raise ValueError(f"目标 {target} 不存在或不是数组")
        return doc[key]
    raise ValueError(f"无法识别的 target：{target}")


def _minutes_between(start: str, end: str) -> int:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return int(delta.total_seconds() // 60)


def check_references() -> list[str]:
    problems: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    courses = load(HERE / "api" / "courses.json")["items"]
    resources = load(HERE / "api" / "resources.json")["items"]
    tasks = load(HERE / "api" / "tasks.json")["items"]
    plans = load(HERE / "api" / "plans.json")["items"]
    assignments = load(HERE / "api" / "assignments.json")["items"]
    recordings = load(HERE / "api" / "recordings.json")["items"]
    jobs = load(HERE / "api" / "jobs.json")["items"]
    chunks = load(HERE / "api" / "content-chunks.json")["items"]

    course_ids = {c["id"] for c in courses}
    resource_ids = {r["id"] for r in resources}
    task_ids = {t["id"] for t in tasks}
    plan_ids = {p["id"] for p in plans}
    job_ids = {j["id"] for j in jobs}

    for r in resources:
        expect(r["course_id"] in course_ids, f"resource {r['id']}: course_id {r['course_id']} 不在 courses.json")
    for t in tasks:
        if t["course_id"] is not None:
            expect(t["course_id"] in course_ids, f"task {t['id']}: course_id {t['course_id']} 不在 courses.json")
        for pid in t["prerequisite_ids"]:
            expect(pid in task_ids, f"task {t['id']}: prerequisite {pid} 不在 tasks.json")
        expect(t["id"] not in t["prerequisite_ids"], f"task {t['id']}: 前置依赖不能指向自身")
    for a in assignments:
        expect(a["course_id"] in course_ids, f"assignment {a['id']}: course_id {a['course_id']} 不在 courses.json")
        for rid in a["attachment_resource_ids"]:
            expect(rid in resource_ids, f"assignment {a['id']}: 附件 {rid} 不在 resources.json")
    for rec in recordings:
        expect(rec["course_id"] in course_ids, f"recording {rec['id']}: course_id {rec['course_id']} 不在 courses.json")
    for p in plans:
        block_tasks = set()
        block_ids = set()
        scheduled = locked = 0
        for b in p["blocks"]:
            expect(b["task_id"] in task_ids, f"plan {p['id']}: block task {b['task_id']} 不在 tasks.json")
            expect(b["id"] not in block_ids, f"plan {p['id']}: block id {b['id']} 重复")
            block_ids.add(b["id"])
            block_tasks.add(b["task_id"])
            minutes = _minutes_between(b["starts_at"], b["ends_at"])
            expect(minutes > 0, f"plan {p['id']}: block {b['id']} ends_at 不晚于 starts_at")
            scheduled += minutes
            if b["locked"]:
                locked += minutes
        for u in p["unassigned"]:
            expect(u["task_id"] in task_ids, f"plan {p['id']}: unassigned task {u['task_id']} 不在 tasks.json")
            expect(u["task_id"] not in block_tasks, f"plan {p['id']}: task {u['task_id']} 同时在 blocks 与 unassigned")
        if p["based_on_plan_id"] is not None:
            expect(p["based_on_plan_id"] in plan_ids, f"plan {p['id']}: based_on_plan_id 不在 plans.json")
        if p["superseded_by"] is not None:
            expect(p["superseded_by"] in plan_ids, f"plan {p['id']}: superseded_by 不在 plans.json")
        s = p["summary"]
        expect(s["scheduled_minutes"] == scheduled, f"plan {p['id']}: summary.scheduled_minutes 应为 {scheduled}")
        expect(s["locked_minutes"] == locked, f"plan {p['id']}: summary.locked_minutes 应为 {locked}")
        expect(s["scheduled_task_count"] == len(block_tasks), f"plan {p['id']}: summary.scheduled_task_count 应为 {len(block_tasks)}")
        expect(s["unassigned_task_count"] == len(p["unassigned"]), f"plan {p['id']}: summary.unassigned_task_count 应为 {len(p['unassigned'])}")
    for j in jobs:
        for ref in j["result_refs"]:
            if ref["type"] == "resource":
                expect(ref["id"] in resource_ids, f"job {j['id']}: result_ref resource {ref['id']} 不在 resources.json")
    resource_by_id = {r["id"]: r for r in resources}
    chunk_counts: dict[str, int] = {}
    for c in chunks:
        expect(c["resource_id"] in resource_ids, f"chunk {c['id']}: resource_id {c['resource_id']} 不在 resources.json")
        expect(c["provenance"]["job_id"] in job_ids, f"chunk {c['id']}: provenance.job_id 不在 jobs.json")
        if c["resource_id"] in resource_by_id:
            r = resource_by_id[c["resource_id"]]
            expect(
                c["resource_version"] == r["version"],
                f"chunk {c['id']}: resource_version {c['resource_version']} 不是 {r['id']} 的当前版本 {r['version']}",
            )
        chunk_counts[c["resource_id"]] = chunk_counts.get(c["resource_id"], 0) + 1
    for r in resources:
        if r["chunk_count"] is not None and r["id"] in chunk_counts:
            expect(
                r["chunk_count"] == chunk_counts[r["id"]],
                f"resource {r['id']}: chunk_count 应为 {chunk_counts[r['id']]}（按 content-chunks.json 全量枚举）",
            )
    check_review_samples(problems)
    return problems


def check_review_samples(problems: list[str]) -> None:
    """复习卷样例（providers/review-materials/preflight/review-paper/revisions/jobs/题库）一致性。"""
    import hashlib

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    def is_sha256(value: str) -> bool:
        return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71

    def sha(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def check_text_chars(label: str, obj: dict) -> None:
        expect(
            obj["chars"] == len(obj["text"]),
            f"{label}: chars {obj['chars']} 应与原文长度 {len(obj['text'])} 一致",
        )

    courses = load(HERE / "api" / "courses.json")["items"]
    resources = load(HERE / "api" / "resources.json")["items"]
    assignments = load(HERE / "api" / "assignments.json")["items"]
    chunks = load(HERE / "api" / "content-chunks.json")["items"]
    jobs = load(HERE / "api" / "jobs.json")["items"]
    providers = load(HERE / "api" / "providers.json")["items"]
    materials = load(HERE / "api" / "review-materials.json")["items"]
    preflight = load(HERE / "api" / "review-preflight.json")
    paper = load(HERE / "api" / "review-paper.json")
    revisions = load(HERE / "api" / "review-revisions.json")["items"]
    candidates = load(HERE / "adapter" / "bank-candidates.json")["candidates"]

    course_ids = {c["id"] for c in courses}
    resource_by_id = {r["id"]: r for r in resources}
    assignment_by_id = {a["id"]: a for a in assignments}
    provider_ids = {p["id"] for p in providers}
    candidate_by_id = {c["id"]: c for c in candidates}
    chunk_by_id = {c["id"]: c for c in chunks}
    chunks_by_resource: dict[str, list[dict]] = {}
    for c in chunks:
        chunks_by_resource.setdefault(c["resource_id"], []).append(c)

    def joined_text(resource_id: str) -> str:
        ordered = sorted(chunks_by_resource.get(resource_id, []), key=lambda c: c["ordinal"])
        return "\n".join(c["text"] for c in ordered)

    # ---- ProviderConfig：密钥不得出现在样例 ----
    for p in providers:
        for field in p:
            expect(
                field not in ("api_key", "key", "secret", "token"),
                f"provider {p['id']}: 样例不得包含密钥字段 {field}",
            )
        expect(
            p["base_url"].startswith("https://") or "127.0.0.1" in p["base_url"] or "localhost" in p["base_url"],
            f"provider {p['id']}: 非本机地址必须为 https（样例协议）",
        )

    # ---- ReviewMaterialItem ----
    for item in materials:
        expect(item["course_id"] in course_ids, f"review-material {item['id']}: course_id 不存在")
        if item["usable"]:
            expect(item["unavailable_reason"] is None, f"review-material {item['id']}: usable=true 时不应有原因")
            expect(isinstance(item["chars"], int) and item["chars"] >= 0, f"review-material {item['id']}: 可用材料应有 chars")
        else:
            expect(item["unavailable_reason"] is not None, f"review-material {item['id']}: usable=false 必须有不可用原因")
        if item["kind"] == "resource":
            expect(item["id"] in resource_by_id, f"review-material {item['id']}: 资源不存在")
            expect(item["parse_status"] is not None, f"review-material {item['id']}: resource 应有 parse_status")
            expect(item["due_at"] is None and item["due_precision"] is None, f"review-material {item['id']}: resource 不应有作业字段")
            if item["parse_status"] == "ready":
                expected = len(joined_text(item["id"]))
                expect(item["chars"] == expected, f"review-material {item['id']}: chars 应为全部片段正文 {expected}")
        else:
            expect(item["id"] in assignment_by_id, f"review-material {item['id']}: 作业不存在")
            expect(item["parse_status"] is None and item["version"] is None, f"review-material {item['id']}: assignment 无解析状态")
            for att in item["attachments"]:
                expect(att["resource_id"] in resource_by_id, f"review-material {item['id']}: 附件 {att['resource_id']} 不存在")
                expect(
                    att["resource_id"] in assignment_by_id[item["id"]]["attachment_resource_ids"],
                    f"review-material {item['id']}: 附件 {att['resource_id']} 未挂在该作业下",
                )

    # ---- Preflight ----
    pf_materials = preflight["materials"]
    snapshot_chunk_ids: set[str] = set()
    for it in pf_materials["items"]:
        check_text_chars(f"preflight item {it['title']}", it)
        if it["resource_id"] is not None:
            expect(it["resource_id"] in resource_by_id, f"preflight: 资源 {it['resource_id']} 不存在")
            if it["chunk_ids"]:
                expect(len(it["locators"]) == len(it["chunk_ids"]), f"preflight: {it['title']} locators 应与 chunk_ids 对应")
                for cid in it["chunk_ids"]:
                    expect(
                        cid in chunk_by_id and chunk_by_id[cid]["resource_id"] == it["resource_id"],
                        f"preflight: 片段 {cid} 不属于 {it['resource_id']}",
                    )
        if it["assignment_id"] is not None:
            expect(it["assignment_id"] in assignment_by_id, f"preflight: 作业 {it['assignment_id']} 不存在")
    check_text_chars("preflight notes", pf_materials["notes"])
    expect(pf_materials["notes"]["sha256"] == sha(pf_materials["notes"]["text"]), "preflight notes.sha256 与原文不一致")
    expect(
        pf_materials["total_chars"] == sum(i["chars"] for i in pf_materials["items"]),
        "preflight total_chars 与 items 不一致",
    )
    for m in pf_materials["missing"]:
        if m["resource_id"] is not None:
            expect(m["resource_id"] in resource_by_id, f"preflight missing: 资源 {m['resource_id']} 不存在")
        if m["assignment_id"] is not None:
            expect(m["assignment_id"] in assignment_by_id, f"preflight missing: 作业 {m['assignment_id']} 不存在")
    if preflight["provider"]["config_id"] is not None:
        expect(preflight["provider"]["config_id"] in provider_ids, "preflight provider 不在 providers.json")
    expect(is_sha256(preflight["fingerprint"]), "preflight fingerprint 格式不正确")

    # ---- ReviewPaper ----
    expect(paper["course_id"] in course_ids, f"review-paper: course {paper['course_id']} 不存在")
    expect(paper["based_on_revision"] is None or paper["based_on_revision"] < paper["revision"], "review-paper: based_on_revision 应小于 revision")
    snap = paper["snapshot"]
    expect(snap["course_id"] == paper["course_id"], "review-paper: 快照 course_id 不一致")
    expect(is_sha256(snap["fingerprint"]), "review-paper: fingerprint 格式不正确")
    expect(preflight["fingerprint"] == snap["fingerprint"], "preflight fingerprint 应与 review-paper 快照一致（同一请求）")
    check_text_chars("review-paper notes", snap["notes"])
    expect(snap["notes"]["sha256"] == sha(snap["notes"]["text"]), "review-paper notes.sha256 与原文不一致")
    expect(snap["total_chars"] == sum(i["chars"] for i in snap["items"]), "review-paper: total_chars 与 items 不一致")
    for it in snap["items"]:
        check_text_chars(f"review-paper item {it['title']}", it)
        if it["resource_id"] is not None:
            expect(it["resource_id"] in resource_by_id, f"review-paper: 资源 {it['resource_id']} 不存在")
            expect(it["text"] == joined_text(it["resource_id"]), f"review-paper: {it['title']} 正文应与片段按序拼接一致")
            for cid in it["chunk_ids"]:
                expect(
                    cid in chunk_by_id and chunk_by_id[cid]["resource_id"] == it["resource_id"],
                    f"review-paper: 片段 {cid} 不属于 {it['resource_id']}",
                )
                expect(chunk_by_id[cid]["resource_version"] == it["version"], f"review-paper: 片段 {cid} 版本与快照不符")
                snapshot_chunk_ids.add(cid)
        if it["assignment_id"] is not None:
            expect(it["assignment_id"] in assignment_by_id, f"review-paper: 作业 {it['assignment_id']} 不存在")

    blueprint = paper["blueprint"]
    constraint_quotas = {q["type"]: q for q in paper["constraints"]["question_types"]}
    for tq in blueprint["type_quotas"]:
        cq = constraint_quotas.get(tq["type"])
        expect(cq is not None and cq["count"] == tq["count"], f"blueprint: 题型配额 {tq['type']} 与 constraints 不一致")
        if cq and cq["points_each"] is not None:
            expect(tq["points_total"] == cq["count"] * cq["points_each"], f"blueprint: {tq['type']} 分值合计与每题分值不一致")
    slot_counts: dict[str, int] = {}
    for sec in blueprint["sections"]:
        for slot in sec["question_slots"]:
            slot_counts[slot["type"]] = slot_counts.get(slot["type"], 0) + slot["count"]
        for kp in sec["knowledge_points"]:
            for ev in kp["evidence"]:
                if ev["chunk_id"] is not None:
                    expect(ev["chunk_id"] in snapshot_chunk_ids, f"blueprint: 证据片段 {ev['chunk_id']} 不在材料快照")
                if ev["kind"] == "note":
                    expect(
                        ev["note_ref"] is not None and ev["note_ref"] in snap["notes"]["text"],
                        "blueprint: note 证据应指向备注原文子串",
                    )
    expect(slot_counts == {t: q["count"] for t, q in constraint_quotas.items()}, "blueprint: 题位合计应与 constraints 一致")
    expect(blueprint["total_points"] == paper["constraints"]["total_points"], "blueprint: total_points 与 constraints 不一致")
    expect(sum(blueprint["difficulty_quota"].values()) == len(paper["questions"]), "blueprint: 难度配额合计应为题目总数")

    questions = paper["questions"]
    question_ids = {q["id"] for q in questions}
    expect(len(question_ids) == len(questions), "review-paper: 题目 ID 重复")
    expect([q["ordinal"] for q in questions] == list(range(1, len(questions) + 1)), "review-paper: 题号应从 1 连续")
    summary = paper["summary"]
    expect(summary["question_count"] == len(questions), "summary.question_count 与题目数不一致")
    expect(summary["total_points"] == sum(q["points"] for q in questions), "summary.total_points 与题目分值不一致")
    type_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    snapshot_resources = {it["resource_id"]: set(it["chunk_ids"]) for it in snap["items"] if it["resource_id"]}
    snapshot_assignments = {it["assignment_id"] for it in snap["items"] if it["assignment_id"]}
    for q in questions:
        type_counts[q["question_type"]] = type_counts.get(q["question_type"], 0) + 1
        difficulty_counts[q["difficulty"]] = difficulty_counts.get(q["difficulty"], 0) + 1
        origin_counts[q["origin"]["kind"]] = origin_counts.get(q["origin"]["kind"], 0) + 1
        expect(len(q["source_refs"]) >= 1, f"question {q['id']}: 至少一项出处")
        for ref in q["source_refs"]:
            if ref["kind"] == "resource":
                expect(
                    ref["resource_id"] in snapshot_resources and ref["chunk_id"] in snapshot_resources[ref["resource_id"]],
                    f"question {q['id']}: 出处片段不在材料快照",
                )
            elif ref["kind"] == "assignment":
                expect(ref["assignment_id"] in snapshot_assignments, f"question {q['id']}: 出处作业不在材料快照")
            elif ref["kind"] == "note":
                notes_text = str((snap.get("notes") or {}).get("text") or "")
                line_match = re.fullmatch(r"备注第(\d+)行", str(ref.get("locator") or ""))
                expect(
                    line_match is not None
                    and 1 <= int(line_match.group(1)) <= max(1, len(notes_text.split("\n"))),
                    f"question {q['id']}: 备注出处应指向备注原文行（locator=备注第N行 且 N 在行数内）",
                )
                expect(
                    all(ref[f] is None for f in ("resource_id", "chunk_id", "assignment_id", "url", "site_name")),
                    f"question {q['id']}: 备注出处不得携带资源/作业/URL 字段",
                )
            elif ref["kind"] == "web":
                expect(
                    isinstance(ref["url"], str) and ref["url"].startswith("https://"),
                    f"question {q['id']}: 网络出处应有真实原题 URL",
                )
            else:
                expect(False, f"question {q['id']}: 未知出处类型 {ref['kind']!r}")
        if q["origin"]["kind"] in ("bank", "adapted_bank"):
            candidate = candidate_by_id.get(q["origin"]["candidate_id"])
            expect(candidate is not None, f"question {q['id']}: 候选 {q['origin']['candidate_id']} 不在 bank-candidates.json")
            if candidate is not None:
                expect(
                    any(ref["url"] == candidate["source"]["url"] for ref in q["source_refs"]),
                    f"question {q['id']}: 网络题出处应与候选来源 URL 一致",
                )
    expect(type_counts == summary["type_counts"], "summary.type_counts 与题目不一致")
    expect(difficulty_counts == summary["difficulty_counts"], "summary.difficulty_counts 与题目不一致")
    expect(origin_counts == summary["origin_counts"], "summary.origin_counts 与题目不一致")
    for w in paper["warnings"]:
        for qid in w["question_ids"]:
            expect(qid in question_ids, f"warning {w['code']}: 题目 {qid} 不存在")

    # ---- 版本历史 ----
    seen_revisions: set[int] = set()
    for rv in revisions:
        expect(rv["paper_id"] == paper["id"], f"revision {rv['revision']}: paper_id 与样例试卷不一致")
        expect(rv["revision"] not in seen_revisions, f"revision {rv['revision']}: 版本号重复")
        seen_revisions.add(rv["revision"])
        expect(is_sha256(rv["fingerprint"]), f"revision {rv['revision']}: fingerprint 格式不正确")
    current = next((rv for rv in revisions if rv["revision"] == paper["revision"]), None)
    expect(current is not None, "版本索引应包含当前 revision")
    if current is not None:
        expect(current["fingerprint"] == snap["fingerprint"], "当前 revision 的 fingerprint 应与快照一致")
        expect(current["summary"] == paper["summary"], "当前 revision 的 summary 应与试卷一致")

    # ---- 后台任务产物引用 ----
    for j in jobs:
        for ref in j["result_refs"]:
            if ref["type"] == "review_paper":
                expect(ref["id"] == paper["id"], f"job {j['id']}: review_paper 产物 {ref['id']} 不在样例中")
    expect(
        any(j["type"] == "review_generate" and j.get("phase") for j in jobs),
        "jobs.json 应包含带 phase 阶段的 review_generate 样例",
    )


def main() -> int:
    problems: list[str] = []
    manifest = load(HERE / "manifest.json")
    registry = build_registry()
    checked = 0
    for entry in manifest["examples"]:
        file_label = entry["file"]
        try:
            doc = load(HERE / entry["file"])
            schema = load(HERE / entry["schema"])
        except FileNotFoundError as exc:
            problems.append(f"{file_label}: 文件缺失（{exc.filename}）")
            continue
        if isinstance(doc, dict) and "items" in doc and "next_cursor" not in doc:
            problems.append(f"{file_label}: 列表响应缺少 next_cursor（分页信封）")
        try:
            items = resolve_target(doc, entry["target"])
        except ValueError as exc:
            problems.append(f"{file_label}: {exc}")
            continue
        validator = Draft202012Validator(schema, registry=registry)
        for i, item in enumerate(items):
            errors = sorted(validator.iter_errors(item), key=lambda e: list(e.path))
            for err in errors[:5]:
                location = "/".join(str(part) for part in err.path)
                problems.append(f"{file_label}[{i}]{' @' + location if location else ''}: {err.message}")
            checked += 1
    problems.extend(check_references())
    if problems:
        print(f"样例校验失败：{len(problems)} 个问题")
        for p in problems:
            print(f" - {p}")
        return 1
    print(f"样例校验通过：{len(manifest['examples'])} 个文件、{checked} 个对象，跨文件引用一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
