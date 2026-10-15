"""M2 (`reviews.*`) deterministic acceptance over the real review service and real engine.

Covers B01-B04 for the Agent path. The school side is synthetic; the review engine
(`learning.review`), the application service, the SQLite job row and the HTTP bridge are the
real ones. A local HTTP mock stands in for the paid model so no external call is made.

* B01 preflight reports gaps honestly; archived/changed material invalidates an old preflight;
* B02 generate returns a durable queued job; an independent worker completes it; the next
  request reads the real paper;
* B03 same trusted call id replay, another conversation's paper/job ids refused;
* B04 model/questionbank unavailable, partial generation, failure and cancellation report
  accurate state and never fabricate success.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app import review_deps
from app.agent_bridge import review_actions, tools
from app.errors import ApiError
from test_agent_gateway import gateway, conversation, turn, internal  # noqa: F401

COURSE = "course_a"          # the gateway fixture's course (and the conversation scope)
OTHER_COURSE = "crs_51c9d0"  # seeded contract course, deliberately outside this session's scope
NOTES = "重点考察边界条件，增加一道综合计算题。"

RES_READY = "res_synthetic_text"
RES_PENDING = "res_synthetic_pending"
ASG_READY = "asg_synthetic_text"


@pytest.fixture(autouse=True)
def synthetic_course(gateway):
    """Synthetic course material inside the fixture course. No personal data, no network.

    `res_synthetic_text` is parsed with two chunks (usable); `res_synthetic_pending` is still
    parsing (a real gap); the assignment carries instructions. Written through the fixture's
    isolated domain engine, never the development database.
    """
    _, _, _, engine = gateway
    with Session(engine) as db:
        for model, rows in (
            (m.Resource, [
                dict(id=RES_READY, course_id=COURSE, kind="document", title="合成讲义-线性表",
                     version=1, parse_status="ready", content_hash="sha256:synthetic-ready",
                     access_level="full"),
                dict(id=RES_PENDING, course_id=COURSE, kind="document", title="合成讲义-未解析",
                     version=1, parse_status="pending", content_hash="sha256:synthetic-pending",
                     access_level="full"),
            ]),
            (m.ContentChunk, [
                dict(id="chk_synthetic_1", resource_id=RES_READY, resource_version=1, ordinal=0,
                     text="线性表是由n个数据元素构成的有限序列，相邻元素之间存在序偶关系；表头无前驱，表尾无后继。",
                     locator={"paragraph": 1}, provenance={"kind": "synthetic"}),
                dict(id="chk_synthetic_2", resource_id=RES_READY, resource_version=1, ordinal=1,
                     text="顺序存储结构用一段地址连续的存储单元依次存储线性表的数据元素，插入与删除平均移动约一半元素。",
                     locator={"paragraph": 2}, provenance={"kind": "synthetic"}),
            ]),
            (m.Assignment, [
                dict(id=ASG_READY, course_id=COURSE, title="合成作业：顺序表实现",
                     instructions="实现顺序表并提交源码与实验报告。", revision=1, due_precision="unknown"),
            ]),
        ):
            for row in rows:
                if db.get(model, row["id"]) is None:
                    db.add(model(**row))
        db.commit()
    return engine


# ────────────────────────── mock model + question bank ──────────────────────────

CAPTURED: list[dict] = []
MOCK = {"mode": "happy"}  # happy | repair | no_content | http_500
FAKE = {"mode": "full", "calls": []}
_SEQ = itertools.count(1000)


def _kp_block(user: str):
    return [(m.group(1), m.group(2).strip()) for m in
            re.finditer(r"^- (kp:[0-9a-f]+) (.+?)｜章节：", user, re.M)]


def _quotas(user: str):
    return [(m.group(1), int(m.group(2))) for m in re.finditer(r"^- (\w+) × (\d+)，每题", user, re.M)]


def _deficits(user: str):
    return [(m.group(1), int(m.group(2))) for m in re.finditer(r"^- (\w+) 缺 (\d+) 题", user, re.M)]


def _chunks(user: str):
    return list(dict.fromkeys(re.findall(r"\[(chk_[A-Za-z0-9_]+) \|", user)))


def _question(qtype, kp_id, kp_name, idx, chunks, defect):
    code = f"sim{idx:04d}"
    if qtype == "choice":
        options = [{"key": k, "text": f"{kp_name}相关子项 {k}（{idx}号）"} for k in "ABCD"]
        answer, scoring = "A", []
    else:
        options = []
        answer = f"参考解答：依据{kp_name}的定义逐步推导（{idx}号，核对码 {code}）。"
        scoring = ["写出关键定义", "推导步骤完整"]
    refs = [{"chunk_id": chunks[(idx - 1) % len(chunks)]}] if chunks else [{"notes_line": 1}]
    return {"qtype": qtype, "stem": f"第{idx}题（{kp_name}）：核对码 {code}，请依据材料作答。",
            "options": options, "answer": answer,
            "explanation": "" if defect else f"解析：依据材料关于{kp_name}的表述（第{idx}号）。",
            "scoring": scoring, "kp_ids": [kp_id], "difficulty": ["easy", "medium", "hard"][idx % 3],
            "source_refs": refs}


def _generation_output(user: str, defect: bool) -> dict:
    kps = _kp_block(user)
    if not kps:
        return {"title": "空", "questions": []}
    chunks = _chunks(user)
    plan = []
    if _deficits(user):
        i = 0
        for qtype, n in _deficits(user):
            for _ in range(n):
                plan.append((qtype, *kps[i % len(kps)]))
                i += 1
    else:
        i = 0
        for qtype, n in _quotas(user):
            for _ in range(n):
                plan.append((qtype, *kps[i % len(kps)]))
                i += 1
    return {"title": "数据结构基础 复习卷（合成模型）",
            "questions": [_question(qt, kid, kn, idx, chunks, defect and idx == 3)
                          for idx, (qt, kid, kn) in enumerate(plan, start=1)]}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body):
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
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
        payload = json.loads(raw)
        CAPTURED.append({"path": self.path, "payload": payload, "headers": dict(self.headers)})
        if MOCK["mode"] == "http_500":
            self._send(500, {"error": {"message": "synthetic upstream failure"}})
            return
        messages = payload.get("messages") or []
        system = messages[0]["content"] if messages else ""
        user = messages[1]["content"] if len(messages) > 1 else ""
        if system.startswith("你是复习卷修复引擎"):
            prev = json.loads(user.split("上一轮输出（JSON）：\n", 1)[1].split("\n\n校验问题清单：", 1)[0])
            if len(prev.get("questions") or []) >= 3:
                prev["questions"][2]["explanation"] = "解析（修复轮）：已核对材料。"
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": json.dumps(prev, ensure_ascii=False)}}]})
            return
        if MOCK["mode"] == "no_content":
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": ""}}]})
            return
        self._send(200, {"choices": [{"message": {"role": "assistant",
                   "content": json.dumps(_generation_output(user, MOCK["mode"] == "repair"), ensure_ascii=False)}}]})


@pytest.fixture(scope="module")
def mock_model():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def fake_search_candidates(request, transport=None, cancellation=None, **kwargs):
    """In-process stand-in for the real bank search; keeps the real SearchOutcome contract."""
    FAKE["calls"].append({"query": request.get("query"), "sources": list(request.get("sources") or [])})
    module = review_deps.questionbank_module()
    out = []
    for item in request.get("question_types") or []:
        qtype = item["type"] if isinstance(item, dict) else item
        out.append({"candidate_id": f"cand_{next(_SEQ)}", "type": qtype,
                    "stem": f"合成题库题 {next(_SEQ)} 关于 {request.get('query')} 的 {qtype} 核对码 {next(_SEQ)}",
                    "options": [{"label": k, "text": f"选项 {k}"} for k in "ABCD"] if qtype == "choice" else [],
                    "answer": "A" if qtype == "choice" else "合成题库参考解答。",
                    "analysis": "合成题库解析。", "knowledge_point": request.get("query"),
                    "difficulty": "medium", "source_url": "https://example.edu/q/1",
                    "source_site": "example.edu", "source_title": "合成题库", "extract_confidence": 0.9})
    if FAKE["mode"] == "no_short":
        out = [item for item in out if item["type"] != "short_answer"]
    return module.SearchOutcome(status="ok", candidates=out, warnings=[], sources=[],
                                query=str(request.get("query") or ""))


@pytest.fixture(autouse=True)
def review_env(monkeypatch, tmp_path):
    """Isolated secrets + question-bank stub + zero backoff. Never touches the personal DB."""
    from app import secrets_store, worker as worker_mod

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    monkeypatch.setattr("app.secrets_store.SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(worker_mod, "_interruptible_sleep", lambda db, job, seconds: None)
    module = review_deps.questionbank_module()
    assert module is not None, "question bank module is not importable"
    monkeypatch.setattr(module, "search_candidates", fake_search_candidates)
    monkeypatch.setenv("STUDY_QUESTIONBANK_SOURCES", json.dumps(
        {"sources": [{"source_id": "src_s1", "kind": "search_service", "provider": "searxng",
                      "base_url": "http://127.0.0.1:9/searxng"}]}, ensure_ascii=False))
    CAPTURED.clear()
    FAKE["mode"] = "full"
    FAKE["calls"].clear()
    MOCK["mode"] = "happy"
    yield


def make_provider(client, port, name="复习卷合成服务"):
    created = client.post("/api/v1/ai/providers", json={"name": name, "base_url": f"http://127.0.0.1:{port}",
                                                         "model": "synthetic-chat", "timeout_seconds": 30})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    patched = client.patch(f"/api/v1/ai/providers/{pid}", json={"api_key": "sk-review-synthetic"})
    assert patched.status_code == 200, patched.text
    return pid


def request_body(provider_id=None, **over):
    body = {"course_id": COURSE, "mode": "ai", "provider_config_id": provider_id,
            "title": None, "notes": NOTES,
            "constraints": {"question_types": [{"type": "single_choice", "count": 2, "points_each": 5},
                                               {"type": "short_answer", "count": 1, "points_each": 10}],
                            "difficulty": "mixed", "total_points": 20, "duration_minutes": 45,
                            "answer_display": "separate"}}
    body.update(over)
    return body


def call(client, token, cid, name, arguments, call_id):
    return internal(client, token, "tool", {"conversation_id": cid, "name": name,
                                            "arguments": arguments, "call_id": call_id}).json()


def run_worker_job(engine, job_id):
    """Run the real worker entry point for one job in an independent DB session.

    `worker.process_job` is exactly what the standalone worker loop calls after `_claim_next`;
    only the session factory is the fixture's isolated domain engine instead of the
    process-wide one. It is responsible for the terminal `_finish` commit, so this is a real
    worker run rather than a bare handler call.
    """
    from app import worker
    with Session(engine) as db:
        job = db.get(m.Job, job_id)
        assert job is not None, "the queued job must be visible to an independent session"
        job.status = "running"
        db.commit()
        worker.process_job(db, job)
    with Session(engine) as db:
        fresh = db.get(m.Job, job_id)
        return type("JobOutcome", (), {"status": fresh.status, "errors": list(fresh.errors or []),
                                       "result_refs": list(fresh.result_refs or [])})


# ────────────────────────────── B01 ──────────────────────────────

def test_b01_preflight_reports_usable_material_and_invents_nothing(gateway, mock_model):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    result = call(client, tokens[tid], cid, "reviews.preflight", request_body(pid), "pf-1")
    assert result["error"] is None, result
    data = result["data"]
    assert data["fingerprint"] and data["can_generate"] is True
    usable = {item["resource_id"] for item in data["materials"]["items"] if item.get("kind") == "resource_text"}
    assert RES_READY in usable, "the seeded ready resource must be selectable"
    # Metadata only: preflight must not ship whole material bodies.
    for item in data["materials"]["items"]:
        assert "text" not in item or len(item.get("text") or "") == 0
    assert CAPTURED == [], "preflight must not call the model"


def test_b01_missing_or_unparsed_material_is_reported_and_blocks_generation(gateway, mock_model):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    # RES_PENDING is seeded as parse_status=pending: selecting it must not pretend it was read.
    body = request_body(pid, materials={"resources": [{"resource_id": RES_PENDING}], "assignments": []})
    result = call(client, tokens[tid], cid, "reviews.preflight", body, "pf-pending")
    assert result["status"] in {"partial", "error", "needs_user"}, result
    assert result["data"]["materials"]["missing"], "an unparsed resource must appear as a gap"
    assert result["coverage"]["complete"] is False
    assert CAPTURED == []


def test_b01_preflight_fingerprint_survives_but_material_change_requires_a_new_one(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    preflight = call(client, tokens[tid], cid, "reviews.preflight", body, "pf-fp")
    digest = preflight["data"]["fingerprint"]

    started = call(client, tokens[tid], cid, "reviews.generate", {**body, "if_match": digest}, "gen-fp")
    assert started["status"] == "queued", started

    # Same conversation, second turn: change the selected material, then try the stale digest.
    with Session(engine) as db:
        resource = db.get(m.Resource, RES_READY)
        resource.version = resource.version + 1
        db.commit()
    try:
        second = conversation(client)
        second_tid = turn(client, second, key="send-2", text="用旧预检再生成")
        stale = call(client, tokens[second_tid], second, "reviews.generate",
                     {**body, "if_match": digest, "provider_config_id": pid}, "gen-stale")
        assert stale["status"] == "error" and stale["error"]["code"] in {"PREFLIGHT_FAILED", "STALE_VERSION",
                                                                         "VALIDATION_FAILED"}
    finally:
        with Session(engine) as db:
            resource = db.get(m.Resource, RES_READY)
            resource.version = resource.version - 1
            db.commit()


# ────────────────────────────── B02 ──────────────────────────────

def test_b02_generate_enqueues_durably_and_a_real_worker_completes_it(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-1")
    assert started["status"] == "queued", started
    job_id = started["job_id"]
    paper_id = started["data"]["paper"]["paper_id"]
    assert job_id and paper_id

    # The queued job is a real committed row, visible to an independent session and worker.
    with Session(engine) as db:
        assert db.get(m.Job, job_id) is not None
        assert db.get(m.ReviewPaper, paper_id) is not None

    result = run_worker_job(engine, job_id)
    assert result.status == "succeeded", result

    polled = call(client, tokens[tid], cid, "jobs.read", {"job_id": job_id}, "poll-1")
    assert polled["error"] is None, polled
    assert polled["data"]["status"] == "succeeded"
    assert paper_id in {ref["id"] for ref in polled["data"]["result_refs"]}

    paper = call(client, tokens[tid], cid, "reviews.read", {"paper_id": paper_id}, "read-1")
    assert paper["error"] is None, paper
    assert paper["data"]["paper"]["revision"] == 1
    assert paper["data"]["questions"], "the completed paper must have questions"
    assert paper["data"]["snapshot"]["fingerprint"]
    assert CAPTURED, "the real review pipeline must have called the model"


def test_b02_export_publishes_the_paper_and_content_matches_the_frozen_snapshot(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-export")
    run_worker_job(engine, started["job_id"])
    paper_id = started["data"]["paper"]["paper_id"]

    exported = call(client, tokens[tid], cid, "reviews.export", {"paper_id": paper_id, "format": "markdown"},
                    "export-1")
    assert exported["error"] is None, exported
    file_id = exported["data"]["file"]["id"]
    body_bytes = client.get(f"/api/v1/agent/conversations/{cid}/files/{file_id}/content").content
    text = body_bytes.decode("utf-8")
    assert "复习卷" in text and "答案" in text
    paper = call(client, tokens[tid], cid, "reviews.read", {"paper_id": paper_id}, "read-2")
    stems = [q["stem"] for q in paper["data"]["questions"]]
    assert any(stem[:12] in text for stem in stems), "the export must contain the generated questions"
    assert exported["data"]["paper"]["fingerprint"] == paper["data"]["snapshot"]["fingerprint"]
    # The delivered file must be auditable on its own: the frozen snapshot id and fingerprint
    # have to appear in the exported document, not only in a separate API response.
    assert paper["data"]["snapshot"]["id"] in text, "the frozen snapshot id must be in the file"
    assert paper["data"]["snapshot"]["fingerprint"] in text, "the material fingerprint must be in the file"


def test_running_job_is_not_collapsed_into_a_queued_envelope(gateway, mock_model):
    """A claimed job must report `running` in the envelope, not `queued`.

    Regression: the terminal-state branch used to overwrite the real `running` state with
    `queued`, so a model reading only the envelope kept polling a job that was already being
    processed by an independent worker.
    """
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-running")
    job_id = started["job_id"]
    with Session(engine) as db:
        job = db.get(m.Job, job_id)
        job.status = "running"
        db.commit()

    read = call(client, tokens[tid], cid, "jobs.read", {"job_id": job_id}, "read-running")
    assert read["data"]["status"] == "running"
    assert read["status"] == "running", read
    assert read["job_id"] == job_id
    # Results stay bounded and the coverage is explicitly incomplete while running.
    assert read["coverage"]["complete"] is True

    with Session(engine) as db:
        db.get(m.Job, job_id).status = "queued"
        db.commit()
    queued = call(client, tokens[tid], cid, "jobs.read", {"job_id": job_id}, "read-queued")
    assert queued["status"] == "queued"


def test_bounded_wait_observes_a_real_terminal_job_row(gateway, mock_model):
    """`wait_seconds` observes the independent worker instead of burning model polls.

    The helper thread plays the worker's part against the *real* committed job row (real status
    transition, real `result_refs`), so this is an observation-contract test, not a fake object.
    """
    import threading
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-wait")
    job_id = started["job_id"]
    assert started["data"]["job"]["status"] == "queued"
    assert started["data"]["observed_within_call"] is False
    assert started["status"] == "queued"

    def finish():
        time.sleep(0.6)
        with Session(engine) as db:
            job = db.get(m.Job, job_id)
            job.status = "succeeded"
            job.result_refs = [{"type": "review_paper", "id": started["data"]["paper"]["paper_id"]}]
            db.commit()

    with Session(engine) as db:
        receipt = db.execute(select(m.AgentReviewReceipt).where(
            m.AgentReviewReceipt.job_id == job_id)).scalar_one()
        request_key = receipt.request_key
    context = tools.ToolContext("local", cid, course_ids=frozenset({COURSE}), request_key=request_key,
                                files=runtime.files)
    worker = threading.Thread(target=finish)
    worker.start()
    try:
        with Session(engine) as db:
            observed = tools.execute("reviews.generate", {**body, "wait_seconds": 6}, db, context)
    finally:
        worker.join(timeout=5)
    assert observed["job_id"] == job_id, "the second call must observe the same accepted job"
    assert observed["data"]["observed_within_call"] is True
    assert observed["data"]["job"]["status"] == "succeeded"
    assert observed["status"] == "ok", observed
    with Session(engine) as db:
        assert len(list(db.scalars(select(m.Job).where(m.Job.type == "review_generate")))) == 1


def test_bounded_wait_replays_within_one_turn_via_the_tool_cache(gateway, mock_model):
    """Same trusted call id + wider wait window must not enqueue a second charged job."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-cached")
    replay = call(client, tokens[tid], cid, "reviews.generate", {**body, "wait_seconds": 2}, "gen-cached")
    assert replay["job_id"] == first["job_id"]
    with Session(engine) as db:
        assert len(list(db.scalars(select(m.Job).where(m.Job.type == "review_generate")))) == 1


def test_bounded_wait_never_claims_success_while_still_running(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    result = call(client, tokens[tid], cid, "reviews.generate", {**body, "wait_seconds": 1}, "gen-short")
    assert result["data"]["job"]["status"] in {"queued", "running"}
    assert result["data"]["observed_within_call"] is False
    assert result["status"] == "queued", result


def test_jobs_read_wait_covers_a_slow_worker_without_a_polling_loop(gateway, mock_model):
    """F2: covering a 15–45 s worker must cost a couple of reads, not a polling loop.

    Two `jobs.read(wait_seconds=...)` calls observe the same real job row; the second one sees the
    terminal state that a helper writes mid-wait. No model request is involved.
    """
    import threading
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-read-wait")
    job_id = started["job_id"]
    assert started["data"]["job"]["status"] == "queued"

    first = call(client, tokens[tid], cid, "jobs.read", {"job_id": job_id, "wait_seconds": 1}, "read-short")
    assert first["data"]["status"] in {"queued", "running"}
    assert first["data"].get("payload") is None, "the job contract must not start shipping its payload"

    def finish():
        time.sleep(0.6)
        with Session(engine) as db:
            job = db.get(m.Job, job_id)
            job.status = "succeeded"
            job.result_refs = [{"type": "review_paper", "id": started["data"]["paper"]["paper_id"]}]
            db.commit()

    worker = threading.Thread(target=finish)
    worker.start()
    try:
        observed = call(client, tokens[tid], cid, "jobs.read", {"job_id": job_id, "wait_seconds": 6}, "read-long")
    finally:
        worker.join(timeout=5)
    assert observed["data"]["status"] == "succeeded", observed
    assert observed["error"] is None
    with Session(engine) as db:
        assert len(list(db.scalars(select(m.Job).where(m.Job.type == "review_generate")))) == 1


def test_jobs_read_wait_cannot_spin_past_its_own_cap(gateway, mock_model):
    """The observation window is anchored to the job's age: once spent, a read stops waiting."""
    from datetime import datetime, timedelta, timezone
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-read-cap")
    job_id = started["job_id"]
    with Session(engine) as db:
        job = db.get(m.Job, job_id)
        job.created_at = datetime.now(timezone.utc) - timedelta(seconds=review_actions.MAX_JOB_OBSERVE_SECONDS + 5)
        db.commit()
        assert review_actions.job_wait_window(db, {"id": job_id, "type": "review_generate"}, 14) == 0
    began = time.monotonic()
    result = call(client, tokens[tid], cid, "jobs.read", {"job_id": job_id, "wait_seconds": 14}, "read-capped")
    assert time.monotonic() - began < 5, "a spent observation window must not wait again"
    assert result["data"]["status"] in {"queued", "running"}


def test_jobs_read_does_not_wait_on_other_job_types():
    """A sync/parse job keeps its plain-read contract; only generation jobs observe."""
    from sqlalchemy import create_engine
    from app.db import Base
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(m.Job(id="job_parse", type="parse", status="queued", payload={"secret": "not-in-result"}))
        db.commit()
        assert review_actions.job_wait_window(db, {"id": "job_parse", "type": "parse"}, 14) == 0
        context = tools.ToolContext("local", "conv_parse", job_ids=frozenset({"job_parse"}))
        result = tools.execute("jobs.read", {"job_id": "job_parse", "wait_seconds": 14}, db, context)
        assert result["data"]["status"] == "queued"
        assert "payload" not in result["data"] and "secret" not in str(result)
    engine.dispose()


# ────────────────────────────── B03 ──────────────────────────────

def test_b03_same_call_id_replays_without_a_second_charged_job(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-replay")
    replay = call(client, tokens[tid], cid, "reviews.generate", body, "gen-replay")
    assert replay["job_id"] == first["job_id"]
    assert replay["data"]["paper"]["paper_id"] == first["data"]["paper"]["paper_id"]
    with Session(engine) as db:
        jobs = list(db.scalars(select(m.Job).where(m.Job.type == "review_generate")))
    assert len(jobs) == 1, "a replayed generation must not enqueue a second charged job"

    # A changed argument set for the same trusted call id is refused by the tool-loop cache
    # (HTTP 409) instead of silently enqueueing a second charged generation.
    conflict = client.post("/api/v1/agent/internal/tool", headers={"Authorization": "Bearer " + tokens[tid]},
                           json={"conversation_id": cid, "name": "reviews.generate",
                                 "arguments": {**body, "notes": "换了备注"}, "call_id": "gen-replay"})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with Session(engine) as db:
        jobs = list(db.scalars(select(m.Job).where(m.Job.type == "review_generate")))
    assert len(jobs) == 1, "a conflicted replay must not enqueue a second charged job"


def test_b03_receipt_layer_also_blocks_a_changed_replay_without_http(gateway, mock_model):
    """The receipt check is server-side, not only the per-turn tool cache."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    context = tools.ToolContext("local", cid, course_ids=frozenset({COURSE}), request_key="turn-x:call-y",
                                files=runtime.files)
    with Session(engine) as db:
        first = tools.execute("reviews.generate", body, db, context)
        with pytest.raises(ApiError) as exc:
            tools.execute("reviews.generate", {**body, "notes": "换了备注"}, db, context)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert first["job_id"]


def test_b03_another_conversation_cannot_read_or_export_this_paper(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-owner")
    run_worker_job(engine, started["job_id"])
    paper_id, job_id = started["data"]["paper"]["paper_id"], started["job_id"]

    other = conversation(client)
    other_tid = turn(client, other, key="other-1")
    read = call(client, tokens[other_tid], other, "reviews.read", {"paper_id": paper_id}, "read-other")
    assert read["status"] == "error" and read["error"]["code"] == "SCOPE_DENIED"
    exported = call(client, tokens[other_tid], other, "reviews.export", {"paper_id": paper_id}, "export-other")
    assert exported["status"] == "error" and exported["error"]["code"] == "SCOPE_DENIED"
    polled = call(client, tokens[other_tid], other, "jobs.read", {"job_id": job_id}, "poll-other")
    assert polled["status"] == "error" and polled["error"]["code"] == "SCOPE_DENIED"


def test_b03_unauthorized_course_is_refused_and_never_enqueues(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)  # scoped to course_a only
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, course_id=OTHER_COURSE,
                        materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    result = call(client, tokens[tid], cid, "reviews.preflight", body, "pf-other-course")
    assert result["status"] == "error" and result["error"]["code"] == "SCOPE_DENIED"
    generated = call(client, tokens[tid], cid, "reviews.generate", body, "gen-other-course")
    assert generated["status"] == "error" and generated["error"]["code"] == "SCOPE_DENIED"
    with Session(engine) as db:
        assert db.execute(select(m.Job).where(m.Job.type == "review_generate")).first() is None


# ────────────────────────────── B04 ──────────────────────────────

def test_b04_model_failure_is_reported_as_failure_not_success(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    MOCK["mode"] = "http_500"
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-500")
    result = run_worker_job(engine, started["job_id"])
    assert result.status == "failed"
    polled = call(client, tokens[tid], cid, "jobs.read", {"job_id": started["job_id"]}, "poll-500")
    assert polled["status"] == "error", polled
    assert polled["error"]["code"] in {"PROVIDER_AUTH_FAILED", "DEPLOYMENT_FAILED", "JOB_FAILED",
                                       "DEPENDENCY_UNAVAILABLE", "PROVIDER_UNREACHABLE"}
    paper = call(client, tokens[tid], cid, "reviews.read",
                 {"paper_id": started["data"]["paper"]["paper_id"]}, "read-500")
    assert paper["status"] == "error" and paper["error"]["code"] == "NOT_FOUND"


def test_b04_empty_model_output_never_becomes_a_finished_paper(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    MOCK["mode"] = "no_content"
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-empty")
    result = run_worker_job(engine, started["job_id"])
    assert result.status == "failed"
    exported = call(client, tokens[tid], cid, "reviews.export",
                    {"paper_id": started["data"]["paper"]["paper_id"]}, "export-empty")
    assert exported["status"] == "error" and exported["error"]["code"] == "NOT_FOUND"


def test_b04_partial_generation_publishes_the_real_partial_paper(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    FAKE["mode"] = "no_short"  # bank mode deficit: one declaration cannot be filled
    body = request_body(pid, mode="bank", provider_config_id=None,
                        materials={"resources": [{"resource_id": RES_READY}], "assignments": []},
                        constraints={"question_types": [{"type": "single_choice", "count": 2, "points_each": 5},
                                                        {"type": "short_answer", "count": 2, "points_each": 10}],
                                     "difficulty": "mixed", "total_points": 30, "duration_minutes": 45,
                                     "answer_display": "separate"})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-partial")
    assert started["status"] == "queued", started
    result = run_worker_job(engine, started["job_id"])
    assert result.status == "partial_failed", result
    polled = call(client, tokens[tid], cid, "jobs.read", {"job_id": started["job_id"]}, "poll-partial")
    assert polled["status"] == "partial", polled
    assert polled["coverage"]["complete"] is False
    paper = call(client, tokens[tid], cid, "reviews.read",
                 {"paper_id": started["data"]["paper"]["paper_id"]}, "read-partial")
    assert paper["error"] is None and paper["data"]["questions"]


def test_b04_cancellation_before_the_worker_leaves_no_revision_and_no_fake_completion(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-cancel")
    cancelled = call(client, tokens[tid], cid, "jobs.cancel", {"job_id": started["job_id"]}, "cancel-1")
    assert cancelled["error"] is None, cancelled
    assert cancelled["data"]["job"]["status"] == "cancelled"

    paper_id = started["data"]["paper"]["paper_id"]
    with Session(engine) as db:
        assert db.get(m.ReviewPaper, paper_id).latest_revision == 0
        assert db.get(m.ReviewPaperRevision, (paper_id, 1)) is None
    read = call(client, tokens[tid], cid, "reviews.read", {"paper_id": paper_id}, "read-cancelled")
    assert read["status"] == "error" and read["error"]["code"] == "NOT_FOUND"
    exported = call(client, tokens[tid], cid, "reviews.export", {"paper_id": paper_id}, "export-cancelled")
    assert exported["status"] == "error" and exported["error"]["code"] == "NOT_FOUND"


def test_b04_no_usable_material_means_no_generation(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = request_body(pid, materials={"resources": [{"resource_id": RES_PENDING}], "assignments": []})
    preflight = call(client, tokens[tid], cid, "reviews.preflight", body, "pf-locked")
    assert preflight["data"]["can_generate"] is False
    assert preflight["data"]["materials"]["missing"]
    generated = call(client, tokens[tid], cid, "reviews.generate", body, "gen-locked")
    assert generated["status"] == "error"
    assert generated["error"]["code"] in {"NO_EVIDENCE", "VALIDATION_FAILED"}
    with Session(engine) as db:
        assert db.execute(select(m.Job).where(m.Job.type == "review_generate")).first() is None


# ─────────── F2: stable business identity of one generation request ───────────

def review_row_counts(engine):
    """Observed row counts, not a description: jobs/papers/receipts are the charged state."""
    with Session(engine) as db:
        return {"jobs": len(list(db.scalars(select(m.Job).where(m.Job.type == "review_generate")))),
                "papers": len(list(db.scalars(select(m.ReviewPaper)))),
                "receipts": len(list(db.scalars(select(m.AgentReviewReceipt))))}


def reviewed_body(pid):
    return request_body(pid, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})


def test_f2_a_new_call_id_with_the_same_request_reuses_the_in_flight_job(gateway, mock_model):
    """The accepted real-model trace t2-1-ff0459e9: same business request, new model call id.

    The second call must not accept (or charge) a second generation: it has to return the
    original job/paper identity with its real state and a machine-readable reuse marker.
    """
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-first")
    assert first["error"] is None, first
    assert first["data"]["reused"] is False, "a first acceptance is not a reuse"
    assert first["status"] == "queued", first
    assert review_row_counts(engine) == {"jobs": 1, "papers": 1, "receipts": 1}

    # A different call id and a different wait window: same business request, still in flight.
    second = call(client, tokens[tid], cid, "reviews.generate", {**body, "wait_seconds": 1}, "gen-f2-second")
    assert second["error"] is None, second
    assert second["data"]["reused"] is True
    assert second["data"]["reused_reason"] == "identical_request_in_flight"
    assert second["job_id"] == first["job_id"], "the second call must report the original job"
    assert second["data"]["job"]["id"] == first["job_id"]
    assert second["data"]["paper"]["paper_id"] == first["data"]["paper"]["paper_id"]
    assert second["data"]["job"]["status"] in {"queued", "running"}
    assert second["status"] in {"queued", "running"}, second
    assert second["data"]["observed_within_call"] is False
    assert review_row_counts(engine) == {"jobs": 1, "papers": 1, "receipts": 1}, \
        "an identical in-flight request must not accept a second charged job"

    # The single accepted job is still the real one the worker completes.
    assert run_worker_job(engine, first["job_id"]).status == "succeeded"
    read = call(client, tokens[tid], cid, "reviews.read",
                {"paper_id": first["data"]["paper"]["paper_id"]}, "read-f2-first")
    assert read["error"] is None and read["data"]["questions"]


def test_f2_an_explicit_new_generation_is_a_machine_checkable_signal(gateway, mock_model):
    """`new_generation=true` is the explicit business operation; nothing relies on prompt text."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-new-1")
    extra = call(client, tokens[tid], cid, "reviews.generate", {**body, "new_generation": True}, "gen-new-2")
    assert extra["error"] is None, extra
    assert extra["data"]["reused"] is False, "an explicitly new operation must not be collapsed"
    assert extra["job_id"] != first["job_id"]
    assert review_row_counts(engine) == {"jobs": 2, "papers": 2, "receipts": 2}

    # An implicit repeat after the explicit one reuses the newest operation for that identity.
    again = call(client, tokens[tid], cid, "reviews.generate", body, "gen-new-3")
    assert again["data"]["reused"] is True
    assert again["job_id"] == extra["job_id"]
    assert review_row_counts(engine) == {"jobs": 2, "papers": 2, "receipts": 2}


def test_f2_failed_prior_job_still_allows_regeneration(gateway, mock_model):
    """Recovery must not be blocked by the dedupe: a terminal failure allows a fresh job."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    MOCK["mode"] = "http_500"
    failed = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-failed")
    assert run_worker_job(engine, failed["job_id"]).status == "failed"
    assert review_row_counts(engine) == {"jobs": 1, "papers": 1, "receipts": 1}

    MOCK["mode"] = "happy"
    retry = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-retry")
    assert retry["error"] is None, retry
    assert retry["data"]["reused"] is False, "a failed prior job must not block a real retry"
    assert retry["job_id"] != failed["job_id"]
    assert review_row_counts(engine) == {"jobs": 2, "papers": 2, "receipts": 2}

    assert run_worker_job(engine, retry["job_id"]).status == "succeeded"
    read = call(client, tokens[tid], cid, "reviews.read",
                {"paper_id": retry["data"]["paper"]["paper_id"]}, "read-f2-retry")
    assert read["error"] is None and read["data"]["questions"]


def test_f2_cancelled_prior_job_still_allows_regeneration(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-cancel")
    cancelled = call(client, tokens[tid], cid, "jobs.cancel", {"job_id": first["job_id"]}, "cancel-f2")
    assert cancelled["data"]["job"]["status"] == "cancelled", cancelled

    retry = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-after-cancel")
    assert retry["error"] is None, retry
    assert retry["data"]["reused"] is False
    assert retry["job_id"] != first["job_id"]
    assert review_row_counts(engine) == {"jobs": 2, "papers": 2, "receipts": 2}


def test_f2_completed_identical_request_is_reused_not_rebilled(gateway, mock_model):
    """A repeated identical request returns the finished paper instead of paying again."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-done")
    assert run_worker_job(engine, first["job_id"]).status == "succeeded"

    replay = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-done-again")
    assert replay["error"] is None, replay
    assert replay["data"]["reused"] is True
    assert replay["data"]["reused_reason"] == "identical_request_completed"
    assert replay["job_id"] == first["job_id"]
    assert replay["data"]["job"]["status"] == "succeeded", "the reused job reports its true state"
    assert replay["data"]["paper"]["revision"] == 1, "the reused paper identity is the real revision"
    assert replay["status"] == "ok", replay
    assert review_row_counts(engine) == {"jobs": 1, "papers": 1, "receipts": 1}
    read = call(client, tokens[tid], cid, "reviews.read",
                {"paper_id": replay["data"]["paper"]["paper_id"]}, "read-f2-done")
    assert read["error"] is None and read["data"]["questions"]


def test_f2_a_changed_business_request_is_not_deduped(gateway, mock_model):
    """The identity must stay business-sensitive: different notes means a different request."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    first = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-a")
    other = call(client, tokens[tid], cid, "reviews.generate",
                 {**body, "notes": "只考察线性表的顺序存储与插入删除。"}, "gen-f2-b")
    assert other["error"] is None, other
    assert other["data"]["reused"] is False
    assert other["job_id"] != first["job_id"]
    assert review_row_counts(engine) == {"jobs": 2, "papers": 2, "receipts": 2}


def test_f2_a_reused_generation_cannot_reset_the_wait_window(gateway, mock_model):
    """`wait_seconds` is anchored to the job's acceptance time, not to the number of calls."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    started = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f2-wait")
    job_id = started["job_id"]
    with Session(engine) as db:
        assert review_actions.wait_window_seconds(db, job_id, 14) >= 13
        db.get(m.Job, job_id).created_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        db.commit()
        assert review_actions.wait_window_seconds(db, job_id, 14) == 0, \
            "a job older than the server window must not grant a fresh wait"

    spent = call(client, tokens[tid], cid, "reviews.generate", {**body, "wait_seconds": 14}, "gen-f2-wait-2")
    assert spent["data"]["reused"] is True
    assert spent["data"]["job"]["status"] == "queued"
    assert spent["data"]["observed_within_call"] is False
    assert review_row_counts(engine) == {"jobs": 1, "papers": 1, "receipts": 1}


# ─────────── F4: the read adapter must expose the real paper primary key ───────────

def test_f4_read_paper_id_is_directly_usable_for_export(gateway, mock_model):
    """`reviews.read.data.paper.paper_id` must be the real key and work as export's argument."""
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    pid = make_provider(client, mock_model)
    body = reviewed_body(pid)

    accepted = call(client, tokens[tid], cid, "reviews.generate", body, "gen-f4")
    assert run_worker_job(engine, accepted["job_id"]).status == "succeeded"

    read = call(client, tokens[tid], cid, "reviews.read",
                {"paper_id": accepted["data"]["paper"]["paper_id"]}, "read-f4")
    assert read["error"] is None, read
    read_paper_id = read["data"]["paper"]["paper_id"]
    assert read_paper_id is not None, "reviews.read must not drop the business primary key"
    assert read_paper_id == accepted["data"]["paper"]["paper_id"]

    # The source contract returns the key as `id`; the adapter maps it to the one shared name.
    with Session(engine) as db:
        paper = db.get(m.ReviewPaper, read_paper_id)
        revision = db.get(m.ReviewPaperRevision, (read_paper_id, read["data"]["paper"]["revision"]))
        contract = revision.to_paper_contract(paper, db.get(m.ReviewMaterialSnapshot, revision.snapshot_id))
    assert contract["id"] == read_paper_id
    assert contract.get("paper_id") is None, "the raw contract has no paper_id key to read"

    exported = call(client, tokens[tid], cid, "reviews.export",
                    {"paper_id": read_paper_id, "format": "markdown"}, "export-f4")
    assert exported["error"] is None, exported
    assert exported["data"]["paper"]["paper_id"] == read_paper_id
    assert exported["data"]["paper"]["revision"] == read["data"]["paper"]["revision"]
    assert exported["data"]["file"]["id"]


# ─────────────────── direct service layer (no HTTP, no model) ───────────────────

def test_review_actions_refuse_a_foreign_paper_without_http(gateway, mock_model):
    """Ownership is enforced by the service layer, not only by the HTTP bridge."""
    client, runtime, tokens, engine = gateway
    from app.agent_bridge.files import ConversationFiles
    from app.agent_bridge.store import Store
    from app.agent_bridge.limits import RunLimits
    import tempfile
    from pathlib import Path

    store = Store(Path(tempfile.mkdtemp()) / "journal.sqlite", RunLimits())
    cid = store.create("local", {"course_ids": []})["id"]
    context = tools.ToolContext("local", cid, files=ConversationFiles(store))
    with Session(engine) as db:
        with pytest.raises(ApiError) as exc:
            review_actions.read(db, context, {"paper_id": "pap_does_not_exist"})
    assert exc.value.code == "SCOPE_DENIED"
    # The same ownership rule blocks a real paper that belongs to another conversation.
    with Session(engine) as db:
        assert review_actions.owns_paper(db, "someone-else", "pap_any") is False
