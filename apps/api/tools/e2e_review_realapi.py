"""#50 真实本地 API 端到端：#47+#48 复习卷全链（真实 uvicorn + 真实 worker 进程内线程 + 真实 HTTP）。

真实/模拟边界（如实标注）：
- 真实：uvicorn 服务进程、HTTP 全链（requests 客户端）、SQLite 独立库 + alembic 迁移 + seed、
  worker（STUDY_WORKER_INPROC=1 进程内线程，与 README「本地开发便捷模式」一致）、
  provider 配置/密钥存储（DPAPI 文件，STUDY_SECRETS_DIR 独立目录）、D 引擎全链、导出渲染。
- 模拟：模型服务=本脚本内嵌的本地 HTTP mock（用户可填任意 API 地址之一种；逐字断言收到的材料/备注、
  密钥仅经请求头）；A 题库网络检索不在本脚本范围（bank/hybrid 由 pytest 套件经进程内替身覆盖）。
运行（脚本自建隔离库 var/b50-e2e.db，每次重建；不触碰任何既有库）：
    cd apps/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python tools/e2e_review_realapi.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

API_DIR = Path(__file__).resolve().parents[1]  # apps/api
REPO = API_DIR.parents[1]  # 仓库根
PORT = 8317
BASE = f"http://127.0.0.1:{PORT}/api/v1"
DB_FILE = REPO / "var" / "b50-e2e.db"
DB_URL = f"sqlite:///{DB_FILE.as_posix()}"
SECRETS_DIR = REPO / "var" / "b50-secrets"
OUT_DIR = REPO / "var" / "b50-e2e-out"
API_KEY = "sk-e2e-50-secret"
NOTES = "重点考察边界条件，增加一道综合计算题，答案单独显示。\n第二章链表部分也要覆盖，出一道头插法相关的题。"
GOLD = "sha256:558ccbada7569a59bcd34659913c800ceb5c695af4b81d91f78ffb892cf2d811"

failures: list[str] = []
CAPTURED: list[dict] = []


def check(name: str, cond: bool, detail: str = ""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ───────── mock 模型（本地 HTTP；按 D 提示词解析配额出题） ─────────

STEM_TAILS = [
    "试比较两种实现的适用场景并说明理由", "判断给定序列是否满足定义并说明依据",
    "归纳核心操作的执行步骤顺序", "分析平均与最坏情况的复杂度差异",
    "指出实现时最常见的边界错误", "说明结论成立所需的推导前提",
    "结合实例解释前驱与后继关系的处理", "给出反例说明常见误解",
    "列出关键不变式并解释作用", "总结工程实践中的取舍依据",
]


def build_output(user: str) -> dict:
    kps = [(m.group(1), m.group(2).strip()) for m in re.finditer(r"^- (kp:[0-9a-f]+) (.+?)｜章节：", user, re.M)]
    quotas = [(m.group(1), int(m.group(2))) for m in re.finditer(r"^- (\w+) × (\d+)，每题", user, re.M)]
    chunks = list(dict.fromkeys(re.findall(r"\[(chk_[A-Za-z0-9_]+) \|", user)))
    plan: list[tuple[str, str, str]] = []
    i = 0
    for qtype, n in quotas:
        for _ in range(n):
            kp_id, kp_name = kps[i % len(kps)]
            plan.append((qtype, kp_id, kp_name))
            i += 1
    questions = []
    for idx, (qtype, _kp_id, kp_name) in enumerate(plan, start=1):
        tail = STEM_TAILS[(idx - 1) % len(STEM_TAILS)]
        if qtype == "choice":
            options = [{"key": k, "text": f"{kp_name}子项 {k}（第{idx}题）"} for k in "ABCD"]
            questions.append({
                "qtype": "choice", "stem": f"第{idx}题（{kp_name}）：{tail}。", "options": options,
                "answer": "A", "explanation": f"解析：依据材料关于{kp_name}的表述（第{idx}题）。", "scoring": [],
                "kp_ids": [_kp_id], "difficulty": "easy" if idx % 2 else "medium",
                "source_refs": [{"chunk_id": chunks[(idx - 1) % len(chunks)]}] if chunks else [{"notes_line": 1}],
            })
        else:
            questions.append({
                "qtype": qtype, "stem": f"第{idx}题（{kp_name}）：{tail}。", "options": [],
                "answer": f"参考解答：依据{kp_name}的定义与步骤推导（第{idx}题）。",
                "explanation": f"解析：依据材料关于{kp_name}的表述（第{idx}题）。",
                "scoring": ["关键定义", "推导完整", "结论正确"],
                "kp_ids": [_kp_id], "difficulty": "hard",
                "source_refs": [{"notes_line": 1 if idx % 2 else 2}],
            })
    return {"title": "数据结构基础 复习卷（e2e）", "questions": questions}


def build_repair_output(user: str) -> dict:
    prev = user.split("上一轮输出（JSON）：\n", 1)[1].split("\n\n校验问题清单：", 1)[0]
    data = json.loads(prev)
    questions = data.get("questions") or []
    if len(questions) >= 3:
        questions[2]["explanation"] = "解析（修复轮补充）：已核对材料并给出解析。"
    return data


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        CAPTURED.append({"path": self.path, "payload": payload, "headers": dict(self.headers)})
        messages = payload.get("messages") or []
        system = messages[0]["content"] if messages else ""
        user = messages[1]["content"] if len(messages) > 1 else ""
        if system.startswith("你是复习卷修复引擎"):
            content = json.dumps(build_repair_output(user), ensure_ascii=False)
        else:
            content = json.dumps(build_output(user), ensure_ascii=False)
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_health(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"http://127.0.0.1:{PORT}/api/v1/health", timeout=1).status_code == 200:
                return True
        except requests.RequestException:
            time.sleep(0.3)
    return False


def poll_job(job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = requests.get(f"{BASE}/jobs/{job_id}", timeout=5).json()
        if body["status"] in ("succeeded", "partial_failed", "failed", "cancelled"):
            return body
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} 未在 {timeout}s 内终态")


def prepare_isolated_env(env: dict) -> bool:
    """重建脚本专属隔离库（alembic + seed）与密钥目录；不触碰任何既有库。"""
    for path in (DB_FILE, DB_FILE.with_name(DB_FILE.name + "-shm"), DB_FILE.with_name(DB_FILE.name + "-wal")):
        path.unlink(missing_ok=True)
    shutil.rmtree(SECRETS_DIR, ignore_errors=True)
    print(f"重建隔离库 var/{DB_FILE.name}（alembic upgrade head + app.seed）")
    for args in (["-m", "alembic", "upgrade", "head"], ["-m", "app.seed"]):
        done = subprocess.run([sys.executable, *args], cwd=API_DIR, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            print(done.stdout[-1500:])
            print(done.stderr[-1500:])
            return False
    return True


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    env = {
        **os.environ,
        "STUDY_DATABASE_URL": DB_URL,
        "STUDY_SECRETS_DIR": SECRETS_DIR.as_posix(),
        "STUDY_WORKER_INPROC": "1",
        "STUDY_WORKER_POLL_SECONDS": "0.3",
        "PYTHONIOENCODING": "utf-8",
    }
    if not prepare_isolated_env(env):
        check("隔离库初始化（alembic+seed）", False)
        mock.shutdown()
        return 1
    check("隔离库初始化（alembic+seed）", True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=API_DIR, env=env,
    )
    try:
        check("uvicorn 健康检查", wait_health(), f"port {PORT}")
        if failures:
            return 1

        # 1) provider 配置 + 密钥 + 连接测试（真实 HTTP → mock 模型）
        r = requests.post(f"{BASE}/ai/providers", json={
            "name": "e2e 本地网关", "base_url": f"http://127.0.0.1:{mock_port}",
            "model": "mock-chat-1", "timeout_seconds": 30,
        }, timeout=10)
        check("创建 provider 201", r.status_code == 201, r.text[:200])
        pid = r.json()["id"]
        r = requests.patch(f"{BASE}/ai/providers/{pid}", json={"api_key": API_KEY}, timeout=10)
        check("保存密钥（响应不含密钥）", r.status_code == 200 and API_KEY not in r.text, r.text[:200])
        r = requests.post(f"{BASE}/ai/providers/{pid}/test", timeout=15)
        body = r.json()
        check("连接测试 ok=true 且无密钥", r.status_code == 200 and body["result"]["ok"] is True
              and API_KEY not in r.text, r.text[:300])
        CAPTURED.clear()  # 连接测试为单条消息请求，后续计数只统计出卷调用

        # 2) 材料清单 + 预检（无副作用预览）
        r = requests.get(f"{BASE}/courses/crs_3f8a21/review-materials", params={"limit": 100}, timeout=10)
        items = r.json()["items"]
        by_id = {it["id"]: it for it in items}
        check("材料清单含课件与作业", r.status_code == 200 and by_id["res_01a7"]["usable"] is True
              and by_id["asg_a102"]["kind"] == "assignment", r.text[:200])

        request_body = {
            "course_id": "crs_3f8a21", "mode": "ai", "provider_config_id": pid, "title": None,
            "materials": {
                "resources": [{"resource_id": "res_01a7", "version": None, "chunk_ids": None},
                              {"resource_id": "res_01d4"}],
                "assignments": [{"assignment_id": "asg_a102", "include_attachments": True}],
            },
            "notes": NOTES,
            "constraints": {
                "question_types": [
                    {"type": "single_choice", "count": 4, "points_each": 5},
                    {"type": "calculation", "count": 2, "points_each": 20},
                    {"type": "short_answer", "count": 2, "points_each": 15}],
                "difficulty": "mixed", "total_points": 90, "duration_minutes": 90, "answer_display": "separate",
            },
        }
        r = requests.post(f"{BASE}/review-papers/preflight", json=request_body, timeout=15)
        pre = r.json()
        check("预检指纹=GOLD（558c…）", r.status_code == 200 and pre["fingerprint"] == GOLD, r.text[:300])
        joined = "\n".join(it["text"] for it in pre["materials"]["items"])
        check("预检含将发送正文原文（课件+作业附件）",
              "线性表是由n个数据元素构成的有限序列" in joined and "头插法建表时间复杂度O(n)" in joined)
        check("预检备注原文逐字", pre["materials"]["notes"]["text"] == NOTES)
        check("预检 provider 有密钥且不回传密钥",
              pre["provider"]["has_api_key"] is True and API_KEY not in r.text)

        # 3) 生成（幂等）→ 真实 worker 处理
        r = requests.post(f"{BASE}/review-papers/generate", json=request_body,
                          headers={"Idempotency-Key": "e2e-50-generate"}, timeout=15)
        check("generate 202", r.status_code == 202, r.text[:200])
        job_id = r.json()["job"]["id"]
        r2 = requests.post(f"{BASE}/review-papers/generate", json=request_body,
                           headers={"Idempotency-Key": "e2e-50-generate"}, timeout=15)
        check("Idempotency-Key 重放同一作业", r2.json()["job"]["id"] == job_id)
        job = poll_job(job_id)
        check("job succeeded", job["status"] == "succeeded", json.dumps(job.get("errors"), ensure_ascii=False))
        check("模型恰 1 次调用（无修复轮）", len(CAPTURED) == 1, str(len(CAPTURED)))
        cap = CAPTURED[0]
        user_text = cap["payload"]["messages"][1]["content"]
        check("模型请求含备注原文与材料正文逐字",
              NOTES in user_text and "线性表是由n个数据元素构成的有限序列" in user_text
              and "头插法建表时间复杂度O(n)" in user_text)
        auth = cap["headers"].get("Authorization") or cap["headers"].get("authorization")
        check("密钥仅经 Authorization 头且不进请求体",
              auth == f"Bearer {API_KEY}" and API_KEY not in json.dumps(cap["payload"]), str(auth))
        paper_id = None
        with_errors = json.dumps(job, ensure_ascii=False)
        check("job 响应无密钥", API_KEY not in with_errors)

        # 4) 试卷详情 / 列表 / 版本
        r = requests.get(f"{BASE}/review-papers", params={"course_id": "crs_3f8a21"}, timeout=10)
        items = r.json()["items"]
        check("列表含新卷", r.status_code == 200 and len(items) == 1, r.text[:200])
        paper_id = items[0]["id"]
        d = requests.get(f"{BASE}/review-papers/{paper_id}", timeout=10)
        paper = d.json()
        check("详情=最新 v1 / GOLD 指纹 / 8 题",
              paper["revision"] == 1 and paper["snapshot"]["fingerprint"] == GOLD and len(paper["questions"]) == 8,
              d.text[:300])
        check("详情无密钥", API_KEY not in d.text)
        types = [q["question_type"] for q in paper["questions"]]
        check("配额 4/2/2", [types.count(t) for t in ("single_choice", "calculation", "short_answer")] == [4, 2, 2],
              str(types))

        # 5) 修订（同参数重做 → regenerate，指纹不漂移）
        r = requests.post(f"{BASE}/review-papers/{paper_id}/revisions", json={},
                          headers={"Idempotency-Key": "e2e-50-revise"}, timeout=15)
        check("修订 202", r.status_code == 202, r.text[:200])
        job2 = poll_job(r.json()["job"]["id"])
        check("修订 succeeded", job2["status"] == "succeeded", json.dumps(job2.get("errors"), ensure_ascii=False))
        revs = requests.get(f"{BASE}/review-papers/{paper_id}/revisions", params={"limit": 100}, timeout=10).json()
        check("版本倒序 [2,1] 且 v2=regenerate",
              [it["revision"] for it in revs["items"]] == [2, 1]
              and revs["items"][0]["revision_reason"] == "regenerate", json.dumps(revs)[:200])
        check("v2 指纹不漂移（GOLD）", revs["items"][0]["fingerprint"] == GOLD)

        # 6) 导出三格式（真实 Content-Disposition + 文件落盘）
        ex = requests.get(f"{BASE}/review-papers/{paper_id}/export", timeout=10)
        cd = ex.headers.get("content-disposition", "")
        (OUT_DIR / "完整卷.md").write_text(ex.text, encoding="utf-8")
        check("markdown 导出 + 中文文件名",
              ex.status_code == 200 and ex.headers["content-type"].startswith("text/markdown")
              and "filename*=UTF-8''" in cd and urllib.parse.quote("完整卷.md") in cd, cd)
        check("markdown 含题面/答案/解析/出处且无密钥/路径",
              all(k in ex.text for k in ("答案", "解析", "出处", "头插法"))
              and API_KEY not in ex.text and "\\" not in ex.text)
        st = requests.get(f"{BASE}/review-papers/{paper_id}/export", params={"format": "student"}, timeout=10)
        (OUT_DIR / "学生版.html").write_text(st.text, encoding="utf-8")
        check("学生版=HTML 且不含答案/解析/评分点",
              st.status_code == 200 and st.headers["content-type"].startswith("text/html")
              and "答案：" not in st.text and "解析：" not in st.text and "评分点：" not in st.text)
        an = requests.get(f"{BASE}/review-papers/{paper_id}/export", params={"format": "answers"}, timeout=10)
        (OUT_DIR / "答案版.html").write_text(an.text, encoding="utf-8")
        check("答案版含答案/解析/评分点", an.status_code == 200 and all(k in an.text for k in ("答案：", "解析：", "评分点：")))

        # 7) 题库能力透传（未配置真实来源时如实）
        r = requests.get(f"{BASE}/questionbanks/capabilities", timeout=10)
        check("题库能力端点 200", r.status_code == 200, r.text[:200])

        print(f"\n产物：{OUT_DIR}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()

    print()
    if failures:
        print(f"FAILED {len(failures)}: {failures}")
        return 1
    print("ALL PASS（真实本地 API 端到端）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
