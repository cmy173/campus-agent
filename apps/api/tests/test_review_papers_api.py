"""复习卷后端闭环测试（#47 流水线 + #48 查询/导出）：隔离库 + 本地 HTTP 模型 mock（捕获请求逐字核对）。

真实/模拟边界（如实标注）：
- 真实执行：FastAPI 受理/幂等、材料快照冻结落库、review_generate 状态机（阶段/取消/重试/重放）、
  D 引擎 learning.review 全链（分析→蓝图→模型消息构建→校验→有限修复→混合合并→契约适配）、
  secrets_store、单提交持久化（修订 + latest_revision + job 产物）。
- 模拟服务：模型=本地 HTTP mock（断言备注/材料正文逐字进入请求、密钥仅出现在请求头）；
  A 题库检索=进程内替身（真实 A 网络检索的传输默认拒绝私网地址属安全设计，不在本套件范围）。
- 隔离：密钥目录指向 per-test tmp；快照密钥恒等（crs_3f8a21/res_01a7 等来自 seed 契约样例，
  只读不改）；用例新建的行（jobs/providers/papers/revisions/snapshots/idempotency）用例后删除。
- 重试退避在测试中置零（app.worker._interruptible_sleep）仅为提速；退避/取消语义由冒烟脚本覆盖。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import worker as worker_mod
from app.db import SessionLocal
from app.models import (
    IdempotencyRecord,
    Job,
    ProviderConfig,
    ReviewMaterialSnapshot,
    ReviewPaper,
    ReviewPaperRevision,
)
from contract_support import schema_errors

C = "crs_3f8a21"
NOTES = "重点考察边界条件，增加一道综合计算题，答案单独显示。\n第二章链表部分也要覆盖，出一道头插法相关的题。"
L1 = "线性表是由n个数据元素构成的有限序列，相邻元素之间存在序偶关系。表头无前驱，表尾无后继。"
L2 = "顺序存储结构用一段地址连续的存储单元依次存储线性表的数据元素，插入与删除平均移动约一半元素。"
A1 = "附录A：单链表的头插法与尾插法对比。头插法建表时间复杂度O(n)，但元素逆序；尾插法保持原序。"
A2 = "本次作业要求实现含头结点的单链表，完成插入、删除、查找与就地逆置四个函数。"
ASG_A102_TEXT = "实现顺序表并提交源码与实验报告。"
GOLD = "sha256:558ccbada7569a59bcd34659913c800ceb5c695af4b81d91f78ffb892cf2d811"
API_KEY = "sk-review-47-secret"

BODY_AI = {
    "course_id": C,
    "mode": "ai",
    "provider_config_id": None,  # 各用例覆盖
    "title": None,
    "materials": {
        "resources": [
            {"resource_id": "res_01a7", "version": None, "chunk_ids": None},
            {"resource_id": "res_01d4"},
        ],
        "assignments": [{"assignment_id": "asg_a102", "include_attachments": True}],
    },
    "notes": NOTES,
    "constraints": {
        "question_types": [
            {"type": "single_choice", "count": 4, "points_each": 5},
            {"type": "calculation", "count": 2, "points_each": 20},
            {"type": "short_answer", "count": 2, "points_each": 15},
        ],
        "difficulty": "mixed",
        "total_points": 90,
        "duration_minutes": 90,
        "answer_display": "separate",
    },
}

# ══════════════════════ 模型 mock（本地 HTTP，捕获请求） ══════════════════════

CAPTURED: list[dict] = []
ATTEMPTS: dict[str, int] = {}
MOCK_MODE = {"scenario": "happy"}  # happy | repair
STEM_TAILS = [
    "试比较两种实现的适用场景并说明理由",
    "判断给定序列是否满足定义并说明依据",
    "归纳核心操作的执行步骤顺序",
    "分析平均与最坏情况的复杂度差异",
    "指出实现时最常见的边界错误",
    "说明结论成立所需的推导前提",
    "结合实例解释前驱与后继关系的处理",
    "给出反例说明常见误解",
    "列出关键不变式并解释作用",
    "总结工程实践中的取舍依据",
]


def _parse_kp_block(user: str) -> list[tuple[str, str, str]]:
    return [
        (m.group(1), m.group(2).strip(), m.group(3).strip())
        for m in re.finditer(r"^- (kp:[0-9a-f]+) (.+?)｜章节：(.+?)｜建议 \d+ 题", user, re.M)
    ]


def _parse_quotas(user: str) -> list[tuple[str, int]]:
    return [(m.group(1), int(m.group(2))) for m in re.finditer(r"^- (\w+) × (\d+)，每题", user, re.M)]


def _parse_deficits(user: str) -> list[tuple[str, int]]:
    return [(m.group(1), int(m.group(2))) for m in re.finditer(r"^- (\w+) 缺 (\d+) 题", user, re.M)]


def _parse_chunks(user: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\[(chk_[A-Za-z0-9_]+) \|", user)))


def _question(qtype: str, kp_id: str, kp_name: str, idx: int, chunks: list[str], defect: bool) -> dict:
    tail = STEM_TAILS[(idx - 1) % len(STEM_TAILS)]
    if qtype == "choice":
        options = [{"key": k, "text": f"{kp_name}相关子项 {k}（第{idx}题）"} for k in "ABCD"]
        answer = "A"
        scoring = []
    else:
        options = []
        answer = f"参考解答：依据{kp_name}的定义与操作步骤逐步推导（第{idx}题）。"
        scoring = ["写出关键定义", "推导步骤完整", "结论正确"]
    if idx % 3 == 1 and chunks:
        source_refs = [{"chunk_id": chunks[(idx - 1) % len(chunks)]}]
    else:
        source_refs = [{"notes_line": 1 if idx % 2 else 2}]
    return {
        "qtype": qtype,
        "stem": f"第{idx}题（{kp_name}）：{tail}。",
        "options": options,
        "answer": answer,
        "explanation": "" if defect else f"解析：依据材料中关于{kp_name}的表述给出（第{idx}题）。",
        "scoring": scoring,
        "kp_ids": [kp_id],
        "difficulty": ["easy", "medium", "hard"][idx % 3],
        "source_refs": source_refs,
    }


def build_generation_output(user: str, *, defect: bool) -> dict:
    kps = _parse_kp_block(user)
    if not kps:
        return {"title": "空", "questions": []}
    chunks = _parse_chunks(user)
    deficits = _parse_deficits(user)
    plan: list[tuple[str, str, str]] = []
    if deficits:  # hybrid：只补缺口（按缺口块给出的数量逐类出题）
        i = 0
        for qtype, n in deficits:
            for _ in range(n):
                kp_id, kp_name, _section = kps[i % len(kps)]
                plan.append((qtype, kp_id, kp_name))
                i += 1
    else:  # ai：按配额全量出题
        i = 0
        for qtype, n in _parse_quotas(user):
            for _ in range(n):
                kp_id, kp_name, _section = kps[i % len(kps)]
                plan.append((qtype, kp_id, kp_name))
                i += 1
    questions = [
        _question(qtype, kp_id, kp_name, idx, chunks, defect and idx == 3)
        for idx, (qtype, kp_id, kp_name) in enumerate(plan, start=1)
    ]
    return {"title": "数据结构基础 复习卷（mock）", "questions": questions}


def build_repair_output(user: str) -> dict:
    prev = user.split("上一轮输出（JSON）：\n", 1)[1].split("\n\n校验问题清单：", 1)[0]
    data = json.loads(prev)
    questions = data.get("questions") or []
    if len(questions) >= 3:
        questions[2]["explanation"] = "解析（修复轮补充）：已核对材料并给出解析。"
    return data


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _send(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) or b"{}"
        payload = json.loads(raw)
        CAPTURED.append({"path": self.path, "payload": payload, "headers": dict(self.headers)})
        ATTEMPTS[self.path] = ATTEMPTS.get(self.path, 0) + 1
        if self.path.startswith("/r401"):
            self._send(401, {"error": {"message": "unauthorized", "type": "auth_error"}})
            return
        if self.path.startswith("/r429") and ATTEMPTS[self.path] <= 2:
            self._send(429, {"error": {"message": "rate limited", "type": "rate_limit"}})
            return
        if self.path.startswith("/slow"):
            time.sleep(2.0)  # 超过用例设置的 1s 读超时（见 _force_timeout）
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": "late"}}]})
            return
        messages = payload.get("messages") or []
        system = messages[0]["content"] if messages else ""
        user = messages[1]["content"] if len(messages) > 1 else ""
        if system.startswith("你是复习卷修复引擎"):
            content = json.dumps(build_repair_output(user), ensure_ascii=False)
        else:
            content = json.dumps(
                build_generation_output(user, defect=(MOCK_MODE["scenario"] == "repair")),
                ensure_ascii=False,
            )
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})


@pytest.fixture(scope="module")
def mock_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


# ══════════════════════ A 检索替身（进程内包裹 search_candidates） ══════════════════════

FAKE = {"mode": "full", "calls": [], "_seq": itertools.count(1000)}
_FAKE_TYPE_TAIL = {"choice": "要点辨析", "calculation": "计算演练", "short_answer": "简答演练"}


def _fake_candidate(idx: int, qtype: str, kp_name: str) -> dict:
    # 题干要足够互异：D 按 SequenceMatcher≥0.92 判近似重复，套同一模板会整批被判重复；
    # 用 sha256 前 20 位十六进制做每候选唯一的“核对码”，保证任意两题干相似度远低于阈值。
    code = hashlib.sha256(f"rvw47-cand-{idx}".encode()).hexdigest()[:20]
    return {
        "candidate_id": f"cand_{idx:02d}",
        "type": qtype,
        "stem": f"网络题（{_FAKE_TYPE_TAIL.get(qtype, qtype)}）第{idx}号：关于{kp_name}的表述辨析，核对码 {code}，请依据题目要求选出正确项。",
        "options": [{"label": k, "text": f"公开题库选项 {k}（{idx}号）"} for k in "ABCD"] if qtype == "choice" else [],
        "answer": "A" if qtype == "choice" else f"公开题库参考解答（{idx}号）。",
        "analysis": f"公开题库解析：依据题库原文给出（{idx}号）。",
        "knowledge_point": kp_name,
        "difficulty": "medium",
        "source_url": f"https://question-bank.example.edu/q/{idx}",
        "source_site": "question-bank.example.edu",
        "source_title": f"公开题库 · {kp_name}",
        "extract_confidence": 0.9,
    }


def fake_search_candidates(request, transport=None, cancellation=None, **kwargs):
    """替身：按 FAKE.mode 返回候选；保持真实签名（transport/cancellation 可见）。"""
    FAKE["calls"].append({
        "query": request.get("query"),
        "question_types": list(request.get("question_types") or []),
        "sources": list(request.get("sources") or []),
        "cancellation_callable": callable(cancellation),
    })
    mode = FAKE["mode"]
    cands: list[dict] = []
    if mode == "full":
        for qtype in [*(request.get("question_types") or []), "short_answer"]:
            cands.append(_fake_candidate(next(FAKE["_seq"]), qtype, request["query"]))
    elif mode == "no_short":
        for qtype in request.get("question_types") or []:
            cands.append(_fake_candidate(next(FAKE["_seq"]), qtype, request["query"]))
    elif mode == "hybrid_one":
        if not FAKE["calls"][:-1]:  # 仅首个请求返回 1 道 choice 候选，其余为空 → 制造缺口
            cands.append(_fake_candidate(next(FAKE["_seq"]), "choice", request["query"]))
    return SimpleNamespace(status="ok", candidates=cands, warnings=[], sources=[])


# ══════════════════════ 隔离夹具 ══════════════════════

@pytest.fixture(autouse=True)
def review_env(monkeypatch, tmp_path):
    """密钥目录隔离 + 检索替身 + 退避置零 + 用例行清理（绝不触碰开发库 var/personal.db）。"""
    from app import review_deps

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    monkeypatch.setattr("app.secrets_store.SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(worker_mod, "_interruptible_sleep", lambda db, job, seconds: None)
    qb_mod = review_deps.questionbank_module()
    assert qb_mod is not None, "A 题库模块未接入（connectors.questionbanks 导入失败）"
    monkeypatch.setattr(qb_mod, "search_candidates", fake_search_candidates)
    monkeypatch.setenv(
        "STUDY_QUESTIONBANK_SOURCES",
        json.dumps({"sources": [{"source_id": "src_s1", "kind": "search_service", "provider": "searxng",
                                 "base_url": "http://127.0.0.1:9/searxng"}]}, ensure_ascii=False),
    )
    CAPTURED.clear()
    ATTEMPTS.clear()
    MOCK_MODE["scenario"] = "happy"
    FAKE.update(mode="full")
    FAKE["calls"].clear()
    FAKE["_seq"] = itertools.count(1000)

    def _ids(model, model_attr: str = "id"):
        with SessionLocal() as db:
            return set(db.execute(select(getattr(model, model_attr))).scalars())

    before = {
        "jobs": _ids(Job),
        "providers": _ids(ProviderConfig),
        "papers": _ids(ReviewPaper),
        "snapshots": _ids(ReviewMaterialSnapshot),
        "idem": _ids(IdempotencyRecord, model_attr="key"),
    }
    with SessionLocal() as db:
        before["revisions"] = set(db.execute(select(ReviewPaperRevision.paper_id, ReviewPaperRevision.revision)).all())

    yield

    with SessionLocal() as db:
        new_revisions = [
            (p, r) for p, r in db.execute(select(ReviewPaperRevision.paper_id, ReviewPaperRevision.revision)).all()
            if (p, r) not in before["revisions"]
        ]
        for p, r in new_revisions:
            db.delete(db.get(ReviewPaperRevision, (p, r)))
        for pid in [x for x in db.execute(select(ReviewPaper.id)).scalars() if x not in before["papers"]]:
            db.delete(db.get(ReviewPaper, pid))
        for sid in [x for x in db.execute(select(ReviewMaterialSnapshot.id)).scalars() if x not in before["snapshots"]]:
            db.delete(db.get(ReviewMaterialSnapshot, sid))
        for jid in [x for x in db.execute(select(Job.id)).scalars() if x not in before["jobs"]]:
            db.delete(db.get(Job, jid))
        for pid in [x for x in db.execute(select(ProviderConfig.id)).scalars() if x not in before["providers"]]:
            db.delete(db.get(ProviderConfig, pid))
        for iid in [x for x in db.execute(select(IdempotencyRecord.key)).scalars() if x not in before["idem"]]:
            db.delete(db.get(IdempotencyRecord, iid))
        db.commit()


# ══════════════════════ helpers ══════════════════════

def _make_provider(client: TestClient, port: int, name: str, path: str = "", timeout: int = 30) -> str:
    r = client.post("/api/v1/ai/providers", json={
        "name": name, "base_url": f"http://127.0.0.1:{port}{path}",
        "model": "mock-chat-1", "timeout_seconds": timeout,
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.patch(f"/api/v1/ai/providers/{pid}", json={"api_key": API_KEY})
    assert r.status_code == 200, r.text
    return pid


def test_stale_material_preview_is_rejected_before_job(client: TestClient, mock_port: int) -> None:
    from app.models import Assignment
    pid = _make_provider(client, mock_port, "预览一致性")
    body = {**BODY_AI, "provider_config_id": pid}
    preview = client.post("/api/v1/review-papers/preflight", json=body)
    assert preview.status_code == 200, preview.text
    digest = preview.json()["fingerprint"]
    with SessionLocal() as db:
        assignment = db.get(Assignment, "asg_a102")
        original = assignment.instructions
        assignment.instructions = original + "\n新增要求：说明边界条件。"
        db.commit()
    try:
        r = client.post("/api/v1/review-papers/generate", json=body,
                        headers={"Idempotency-Key": "preview-stale-test", "If-Match": digest})
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "PREFLIGHT_FAILED"
        assert CAPTURED == []
    finally:
        with SessionLocal() as db:
            db.get(Assignment, "asg_a102").instructions = original
            db.commit()


def test_active_provider_cannot_be_changed_or_cleared(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "运行中配置")
    _generate(client, {**BODY_AI, "provider_config_id": pid}, "provider-busy-test")
    assert client.patch(f"/api/v1/ai/providers/{pid}", json={"model": "different"}).status_code == 409
    assert client.delete(f"/api/v1/ai/providers/{pid}/key").status_code == 409
    assert client.get(f"/api/v1/ai/providers/{pid}").json()["has_api_key"] is True


def test_partial_review_retry_generates_new_revision(client: TestClient, mock_port: int) -> None:
    FAKE["mode"] = "no_short"
    body = {**BODY_AI, "mode": "bank", "provider_config_id": None}
    job_id, paper_id = _generate(client, body, "partial-retry-initial")
    assert worker_mod.process_next_job() is True
    assert _get_job(job_id)["row"]["status"] == "partial_failed"
    FAKE["mode"] = "full"
    retry = client.post(f"/api/v1/jobs/{job_id}/retry", headers={"Idempotency-Key": "partial-retry-second"})
    assert retry.status_code == 202, retry.text
    assert worker_mod.process_next_job() is True
    assert _get_job(retry.json()["job"]["id"])["row"]["status"] == "succeeded"
    current = client.get(f"/api/v1/review-papers/{paper_id}").json()
    previous = client.get(f"/api/v1/review-papers/{paper_id}?revision=1").json()
    assert current["revision"] == 2
    assert len(current["questions"]) > len(previous["questions"])


def test_provider_drift_cannot_redirect_queued_materials(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "配置快照")
    job_id, _ = _generate(client, {**BODY_AI, "provider_config_id": pid}, "provider-drift-test")
    with SessionLocal() as db:
        db.get(ProviderConfig, pid).base_url = "http://127.0.0.1:1/v1"
        db.commit()
    assert worker_mod.process_next_job() is True
    job = _get_job(job_id)["row"]
    assert job["status"] == "failed"
    assert any(e["code"] == "PROVIDER_CHANGED" for e in job["errors"])
    assert CAPTURED == []


def _force_timeout(pid: str, seconds: int = 1) -> None:
    """直接调小超时到 1s（API 校验下限 5s 的测试缝隙）：仅改数值，调用路径仍走 requests 真超时。"""
    with SessionLocal() as db:
        cfg = db.get(ProviderConfig, pid)
        cfg.timeout_seconds = seconds
        db.commit()


def _get_job(job_id: str) -> dict:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        return {"row": {c: getattr(job, c) for c in (
            "id", "type", "status", "phase", "progress", "errors", "warnings", "result_refs",
            "cancel_requested", "payload")}, "contract": job.to_contract()}


def _get_paper(paper_id: str) -> dict:
    with SessionLocal() as db:
        paper = db.get(ReviewPaper, paper_id)
        return {
            "latest_revision": paper.latest_revision,
            "mode": paper.mode,
            "title": paper.title,
            "revisions": [
                {"paper_id": r.paper_id, "revision": r.revision, "revision_reason": r.revision_reason,
                 "based_on_revision": r.based_on_revision, "snapshot_id": r.snapshot_id,
                 "fingerprint": r.fingerprint, "provider": r.provider, "warnings": r.warnings,
                 "questions": list(r.questions or []), "summary": r.summary}
                for r in db.execute(select(ReviewPaperRevision)
                                    .where(ReviewPaperRevision.paper_id == paper_id)
                                    .order_by(ReviewPaperRevision.revision)).scalars()
            ],
        }


def _generate(client: TestClient, body: dict, key: str) -> tuple[str, str]:
    r = client.post("/api/v1/review-papers/generate", json=body, headers={"Idempotency-Key": key})
    assert r.status_code == 202, r.text
    job_id = r.json()["job"]["id"]
    return job_id, _get_job(job_id)["row"]["payload"]["paper_id"]


def _warnings_of(job_row: dict) -> dict[str, dict]:
    return {w.get("code"): w for w in job_row["warnings"] or []}


def _by_type(questions: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for q in questions:
        out.setdefault(q["question_type"], []).append(q)
    return out


def _drain(limit: int = 3) -> None:
    for _ in range(limit):
        if not worker_mod.process_next_job():
            return


# ══════════════════════ #47 流水线 ══════════════════════

def test_ai_golden_path_verbatim_requests_and_fingerprint(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "出卷 mock 网关")
    body = {**BODY_AI, "provider_config_id": pid}
    job_id, paper_id = _generate(client, body, "rvw-happy")

    r2 = client.post("/api/v1/review-papers/generate", json=body, headers={"Idempotency-Key": "rvw-happy"})
    assert r2.status_code == 202 and r2.json()["job"]["id"] == job_id  # 幂等重放同一作业

    assert worker_mod.process_next_job() is True
    job = _get_job(job_id)
    assert job["row"]["status"] == "succeeded", json.dumps(job["row"]["errors"], ensure_ascii=False)
    assert job["row"]["result_refs"] == [{"type": "review_paper", "id": paper_id}]
    assert job["row"]["progress"] == 1.0
    assert not schema_errors(job["contract"], "job.schema.json")

    # 模型恰 1 次调用；材料/备注逐字进入请求；密钥仅经请求头
    assert len(CAPTURED) == 1, [x["path"] for x in CAPTURED]
    req = CAPTURED[0]
    msgs = req["payload"]["messages"]
    user_text = msgs[1]["content"]
    assert msgs[0]["content"].startswith("你是复习卷出题引擎")
    assert "优先使用本课程课件/PPT与作业正文" in msgs[0]["content"]
    assert "不默认照搬原题" in msgs[0]["content"]
    assert NOTES in user_text
    assert L1 in user_text and L2 in user_text and "chk_01a7c0" in user_text
    assert ASG_A102_TEXT in user_text and A1 in user_text and A2 in user_text
    assert req["payload"]["max_tokens"] == 32768
    assert req["payload"]["model"] == "mock-chat-1" and req["payload"]["stream"] is False
    assert req["headers"].get("Authorization") == f"Bearer {API_KEY}"

    paper = _get_paper(paper_id)
    assert paper["latest_revision"] == 1
    rev = paper["revisions"][0]
    assert rev["revision_reason"] == "initial" and rev["based_on_revision"] is None
    assert rev["fingerprint"] == GOLD, rev["fingerprint"]
    qs = rev["questions"]
    assert len(qs) == 8
    by_type = _by_type(qs)
    assert [len(by_type.get(t, [])) for t in ("single_choice", "calculation", "short_answer")] == [4, 2, 2]
    pts = {t: sum(q["points"] for q in v) for t, v in by_type.items()}
    assert pts == {"single_choice": 20, "calculation": 40, "short_answer": 30}
    assert all(q["validation"]["status"] == "valid" for q in qs)
    assert [q["ordinal"] for q in qs] == list(range(1, 9))
    assert all(q["source_refs"] and q["source_refs"][0]["kind"] in ("resource", "assignment", "note") for q in qs)

    w = _warnings_of(job["row"])
    assert "MATERIAL_GAP" in w and w["MATERIAL_GAP"]["details"].get("missing_reason") == "not_accessible"
    assert "REPAIR_APPLIED" not in w and "DEFICIT_UNFILLED" not in w
    assert all(x.get("task_ids") == [] for x in job["row"]["warnings"])

    # 响应体契约 + 密钥零泄漏
    contract = client.get(f"/api/v1/review-papers/{paper_id}").json()
    assert not schema_errors(contract, "review-paper.schema.json")
    for q in contract["questions"]:
        assert not schema_errors(q, "review-question.schema.json")
    assert rev["provider"] == {"config_id": pid, "name": "出卷 mock 网关",
                               "protocol": "openai_chat_completions",
                               "base_url": f"http://127.0.0.1:{mock_port}/v1", "model": "mock-chat-1"}
    blob = json.dumps(contract, ensure_ascii=False) + json.dumps(job["contract"], ensure_ascii=False)
    assert API_KEY not in blob


def test_repair_round_fixes_missing_explanation(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "修复网关")
    MOCK_MODE["scenario"] = "repair"
    body = {**BODY_AI, "provider_config_id": pid}
    job_id, paper_id = _generate(client, body, "rvw-repair")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    assert job["row"]["status"] == "succeeded", json.dumps(job["row"]["errors"], ensure_ascii=False)
    assert len(CAPTURED) == 2
    assert CAPTURED[1]["payload"]["messages"][0]["content"].startswith("你是复习卷修复引擎")
    repair_user = CAPTURED[1]["payload"]["messages"][1]["content"]
    assert "校验问题清单：" in repair_user and "EXPLANATION_MISSING" in repair_user

    rev = _get_paper(paper_id)["revisions"][0]
    assert rev["fingerprint"] == GOLD
    q3 = rev["questions"][2]
    assert q3["validation"]["status"] == "repaired"
    assert all(q["validation"]["status"] in ("valid", "repaired") for q in rev["questions"])
    w = _warnings_of(job["row"])
    assert w["REPAIR_APPLIED"]["details"].get("repair_rounds") == 1
    assert w["REPAIR_APPLIED"]["details"].get("question_ids") == [q3["id"]]


def test_provider_401_no_retry(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "401 网关", "/r401")
    job_id, paper_id = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-401-no-retry")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    err = (job["row"]["errors"] or [{}])[0]
    assert job["row"]["status"] == "failed"
    assert err.get("code") == "PROVIDER_AUTH_FAILED" and err.get("retryable") is False
    assert err.get("details", {}).get("attempts") == 1
    assert len(CAPTURED) == 1  # 401 永不重试
    assert _get_paper(paper_id)["latest_revision"] == 0  # 不留修订


def test_provider_429_bounded_retry_then_success(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "限流网关", "/r429")
    job_id, paper_id = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-429-retry")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    assert job["row"]["status"] == "succeeded", json.dumps(job["row"]["errors"], ensure_ascii=False)
    assert len(CAPTURED) == 3  # 1 + 2 次重试上限
    assert _get_paper(paper_id)["latest_revision"] == 1


def test_provider_timeout_retry_exhausted(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "超时网关", "/slow")
    _force_timeout(pid, 1)
    job_id, paper_id = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-slow")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    err = (job["row"]["errors"] or [{}])[0]
    assert job["row"]["status"] == "failed"
    assert err.get("code") == "PROVIDER_TIMEOUT" and err.get("retryable") is True
    assert err.get("details", {}).get("attempts") == 3
    assert len(CAPTURED) == 3
    assert _get_paper(paper_id)["latest_revision"] == 0


def test_cancel_before_worker_leaves_no_revision(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "取消网关")
    job_id, paper_id = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-cancel")
    with SessionLocal() as db:
        j = db.get(Job, job_id)
        j.cancel_requested = True
        db.commit()

    assert worker_mod.process_next_job() is True
    job = _get_job(job_id)
    assert job["row"]["status"] == "cancelled"
    assert job["row"]["result_refs"] == []
    paper = _get_paper(paper_id)
    assert paper["latest_revision"] == 0 and paper["revisions"] == []


def test_revision_regenerate_reuses_snapshot_and_fingerprint(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "修订网关")
    body = {**BODY_AI, "provider_config_id": pid}
    _, paper_id = _generate(client, body, "rvw-rev-a")
    assert worker_mod.process_next_job() is True

    r = client.post(f"/api/v1/review-papers/{paper_id}/revisions", json={},
                    headers={"Idempotency-Key": "rvw-rev-b"})
    assert r.status_code == 202, r.text
    job2_id = r.json()["job"]["id"]
    payload = _get_job(job2_id)["row"]["payload"]
    assert payload["revision"] == 2 and payload["revision_reason"] == "regenerate"
    assert payload["based_on_revision"] == 1
    assert worker_mod.process_next_job() is True

    rev2 = _get_paper(paper_id)["revisions"][1]
    assert rev2["revision_reason"] == "regenerate" and rev2["based_on_revision"] == 1
    assert rev2["fingerprint"] == GOLD  # 未变更材料：快照重建后指纹不漂移


def test_revision_with_notes_change_marks_revise(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "改写网关")
    body = {**BODY_AI, "provider_config_id": pid}
    _, paper_id = _generate(client, body, "rvw-rev-c")
    assert worker_mod.process_next_job() is True

    r = client.post(f"/api/v1/review-papers/{paper_id}/revisions",
                    json={"notes": "只保留第一章内容。"},
                    headers={"Idempotency-Key": "rvw-rev-d"})
    assert r.status_code == 202, r.text
    payload = _get_job(r.json()["job"]["id"])["row"]["payload"]
    assert payload["revision_reason"] == "revise" and payload["revision"] == 2
    assert worker_mod.process_next_job() is True

    rev2 = _get_paper(paper_id)["revisions"][1]
    assert rev2["revision_reason"] == "revise"
    assert rev2["fingerprint"] != GOLD  # 备注变更 → 快照指纹随 notes_sha256 变化


def test_bank_mode_full_quota_no_model_call(client: TestClient, mock_port: int) -> None:
    FAKE["mode"] = "full"
    body = {**BODY_AI, "mode": "bank", "provider_config_id": None}
    job_id, paper_id = _generate(client, body, "rvw-bank")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    assert job["row"]["status"] == "succeeded", json.dumps(job["row"]["errors"], ensure_ascii=False)
    assert CAPTURED == []  # bank 不调用模型
    assert len(FAKE["calls"]) >= 1
    assert all(c["cancellation_callable"] for c in FAKE["calls"])
    assert all(c["sources"] and c["sources"][0]["source_id"] == "src_s1" for c in FAKE["calls"])

    rev = _get_paper(paper_id)["revisions"][0]
    assert len(rev["questions"]) == 8
    assert all(q["origin"]["kind"] == "bank" for q in rev["questions"])
    assert all(q["source_refs"][0]["kind"] == "web" and q["source_refs"][0]["url"]
               and q["source_refs"][0]["site_name"] == "question-bank.example.edu" for q in rev["questions"])
    assert all(v is None for v in rev["provider"].values())
    w = _warnings_of(job["row"])
    assert "DEFICIT_UNFILLED" not in w


def test_bank_deficit_partial_failed_saves_questions(client: TestClient, mock_port: int) -> None:
    FAKE["mode"] = "no_short"
    body = {**BODY_AI, "mode": "bank", "provider_config_id": None}
    job_id, paper_id = _generate(client, body, "rvw-bank-def")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    codes = [e.get("code") for e in job["row"]["errors"] or []]
    assert job["row"]["status"] == "partial_failed" and "QUOTA_MISMATCH" in codes
    w = _warnings_of(job["row"])
    assert w.get("DEFICIT_UNFILLED", {}).get("details", {}).get("qtype") == "short_answer"
    assert w.get("DEFICIT_UNFILLED", {}).get("details", {}).get("reason") == "no_candidates"
    # 如实：已出题仍落库（缺口不伪造）
    qs = _get_paper(paper_id)["revisions"][0]["questions"]
    assert len(qs) == 6
    assert all(q["origin"]["kind"] == "bank" and q["question_type"] != "short_answer" for q in qs)


def test_hybrid_merges_bank_gap_without_repair_round(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "混合网关")
    FAKE["mode"] = "hybrid_one"
    body = {**BODY_AI, "mode": "hybrid", "provider_config_id": pid}
    job_id, paper_id = _generate(client, body, "rvw-hybrid")
    assert worker_mod.process_next_job() is True

    job = _get_job(job_id)
    assert job["row"]["status"] == "succeeded", json.dumps(job["row"]["errors"], ensure_ascii=False)
    assert len(CAPTURED) == 1  # 配额缺口由题库合并补齐，不触发修复轮
    gen_user = CAPTURED[0]["payload"]["messages"][1]["content"]
    assert "缺 3 题" in gen_user and "网络题（要点辨析）" in gen_user  # 缺口块 + 网络候选随请求发出

    rev = _get_paper(paper_id)["revisions"][0]
    origins = [q["origin"]["kind"] for q in rev["questions"]]
    assert len(origins) == 8 and origins.count("bank") == 1 and origins.count("ai") == 7
    by_type = _by_type(rev["questions"])
    assert [len(by_type.get(t, [])) for t in ("single_choice", "calculation", "short_answer")] == [4, 2, 2]
    w = _warnings_of(job["row"])
    assert "DEFICIT_UNFILLED" not in w


# ══════════════════════ #48 查询 / 导出 ══════════════════════

def test_query_list_detail_revisions(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "查询网关")
    _, p1 = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-query-1")
    assert worker_mod.process_next_job() is True
    _, p2 = _generate(client, {**BODY_AI, "provider_config_id": pid, "title": "查询卷二"}, "rvw-query-2")
    assert worker_mod.process_next_job() is True
    _, p3 = _generate(client, {**BODY_AI, "mode": "bank", "provider_config_id": None}, "rvw-query-3")
    assert worker_mod.process_next_job() is True
    # 取消的卷不应出现在列表/详情
    job4, p4 = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-query-4")
    with SessionLocal() as db:
        j = db.get(Job, job4)
        j.cancel_requested = True
        db.commit()
    assert worker_mod.process_next_job() is True

    r = client.get("/api/v1/review-papers", params={"course_id": C, "limit": 100})
    assert r.status_code == 200
    items = r.json()["items"]
    listed = {it["id"] for it in items}
    assert {p1, p2, p3} <= listed and p4 not in listed
    created = [it["created_at"] for it in items]
    assert created == sorted(created, reverse=True)
    assert not schema_errors(items[0], "review-paper.schema.json")
    assert client.get("/api/v1/review-papers/pap_nope").status_code == 404
    assert client.get(f"/api/v1/review-papers/{p4}").status_code == 404

    page1 = client.get("/api/v1/review-papers", params={"course_id": C, "limit": 2}).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = client.get("/api/v1/review-papers",
                       params={"course_id": C, "limit": 2, "cursor": page1["next_cursor"]}).json()
    ids_page = {it["id"] for it in page1["items"]} | {it["id"] for it in page2["items"]}
    assert ids_page == {p1, p2, p3}
    bank_only = client.get("/api/v1/review-papers", params={"mode": "bank", "limit": 100}).json()["items"]
    assert [it["id"] for it in bank_only] == [p3]
    bad = client.get("/api/v1/review-papers", params={"mode": "bogus"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "VALIDATION_FAILED"

    # 详情：缺省最新；指定 v1 可回溯且指纹=GOLD
    d_default = client.get(f"/api/v1/review-papers/{p1}")
    assert d_default.status_code == 200 and d_default.json()["revision"] == 1
    d1 = d_default.json()
    assert d1["snapshot"]["fingerprint"] == GOLD and len(d1["questions"]) == 8
    assert client.get(f"/api/v1/review-papers/{p1}", params={"revision": 99}).status_code == 404

    # 追加修订后：详情缺省=v2；revisions 倒序 + 游标
    r = client.post(f"/api/v1/review-papers/{p1}/revisions", json={}, headers={"Idempotency-Key": "rvw-query-5"})
    assert r.status_code == 202
    assert worker_mod.process_next_job() is True
    assert client.get(f"/api/v1/review-papers/{p1}").json()["revision"] == 2
    rv = client.get(f"/api/v1/review-papers/{p1}/revisions", params={"limit": 100}).json()["items"]
    assert [it["revision"] for it in rv] == [2, 1]
    assert not schema_errors(rv[0], "review-paper-revision.schema.json")
    rv1 = client.get(f"/api/v1/review-papers/{p1}/revisions", params={"limit": 1}).json()
    rv2 = client.get(f"/api/v1/review-papers/{p1}/revisions",
                     params={"limit": 1, "cursor": rv1["next_cursor"]}).json()
    assert [rv1["items"][0]["revision"], rv2["items"][0]["revision"]] == [2, 1]


def test_export_formats_and_filename(client: TestClient, mock_port: int) -> None:
    pid = _make_provider(client, mock_port, "导出网关")
    _, paper_id = _generate(client, {**BODY_AI, "provider_config_id": pid}, "rvw-export")
    assert worker_mod.process_next_job() is True

    r = client.get(f"/api/v1/review-papers/{paper_id}/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    cd = r.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in cd and quote("完整卷.md") in cd
    md = r.text
    assert all(k in md for k in ("答案", "解析", "出处", "头插法"))
    assert API_KEY not in md and "\\" not in md and "file://" not in md

    student = client.get(f"/api/v1/review-papers/{paper_id}/export", params={"format": "student"})
    assert student.status_code == 200 and student.headers["content-type"].startswith("text/html")
    text = student.text
    assert "答案：" not in text and "解析：" not in text and "评分点：" not in text and "头插法" in text

    answers = client.get(f"/api/v1/review-papers/{paper_id}/export", params={"format": "answers"})
    assert answers.status_code == 200 and all(k in answers.text for k in ("答案：", "解析：", "评分点："))

    rev1 = client.get(f"/api/v1/review-papers/{paper_id}/export",
                      params={"format": "student", "revision": 1})
    assert rev1.status_code == 200 and "（学生版）" in rev1.text
    assert client.get("/api/v1/review-papers/pap_nope/export").status_code == 404
    bad = client.get(f"/api/v1/review-papers/{paper_id}/export", params={"format": "pdf"})
    assert bad.status_code == 400
