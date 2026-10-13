"""Real Harness -> actual FastAPI bridge -> real synthetic domain DB integration.

Only the model is controlled. Provider responses derive domain facts exclusively
from the actual tool messages. Saves evidence; never uses campus credentials.
Run with apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py.
"""
from __future__ import annotations

import json
import argparse
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps/api"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def verify(*, serve=False, web_port=15174, stop_file=None):
    (ROOT / "var").mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="agent-integration-", dir=ROOT / "var"))
    receipts = []
    slow_started = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            receipts.append(body)
            messages = body["messages"]
            last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
            if "取消演练" in messages[last_user]["content"]:
                slow_started.set()
                time.sleep(2)
            results = [json.loads(m["content"]) for m in messages[last_user + 1:] if m["role"] == "tool"]
            calls = []
            content = None
            context = json.loads(messages[last_user]["content"].splitlines()[1])
            user_text = messages[last_user]["content"].split("用户消息：\n", 1)[1]
            def call(name, arguments):
                return [{"id": "plan_call_" + str(len(receipts)), "type": "function", "function": {
                    "name": "campus_" + name.replace(".", "_"), "arguments": json.dumps(arguments, ensure_ascii=False)}}]
            if "我完成了准备实验报告材料" in user_text:
                if not results:
                    calls = call("tasks.list", {"course_id": "course_synthetic"})
                elif len(results) == 1:
                    task = next(t for t in results[0]["data"]["items"] if t["title"] == "准备实验报告材料")
                    calls = call("tasks.feedback", {"task_id": task["id"], "expected_revision": task["revision"],
                                                  "outcome": "completed", "actual_minutes": 30})
                else:
                    result = results[-1]
                    content = "本地学习结果：" + str(result["data"].get("task", {}).get("status", result.get("error")))
            elif "生成学习任务" in user_text:
                if not results:
                    calls = call("tasks.build", {"course_id": "course_synthetic"})
                elif len(results) == 1 and results[0]["data"].get("job"):
                    jid = results[0]["data"]["job"]["id"]
                    calls = call("jobs.cancel" if "并停止" in user_text else "jobs.read", {"job_id": jid})
                else:
                    result = results[-1]
                    job = result["data"].get("job", result["data"])
                    content = "后台任务实际状态：" + str(job.get("status", result.get("error")))
            elif context.get("accepted_action"):
                accepted = context["accepted_action"]
                if not results:
                    calls = call(accepted["tool"], {"action_id": accepted["action_id"]})
                else:
                    result = results[-1]
                    content = ("操作回执：" + result["data"]["receipt"]["plan"]["status"] if result.get("status") == "ok"
                               else "操作未完成：" + result["error"]["message"])
            elif "安排" in user_text and "取消演练" not in user_text:
                if not results:
                    calls = call("availability.read", {})
                elif not any("action" in r["data"] for r in results) and len(results) == 1 and results[0]["status"] == "ok":
                    args = {"period": "tomorrow" if "明天" in user_text else "this_week"}
                    weekdays = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}
                    for label, number in weekdays.items():
                        if f"本周{label}晚上没空" in user_text:
                            args["unavailable_evenings"] = [number]
                    if "19:00–21:00" in user_text:
                        day = (datetime.fromisoformat(context["now"]) + timedelta(days=1)).date()
                        args["blocked"] = [{"starts_at": f"{day}T19:00:00+08:00", "ends_at": f"{day}T21:00:00+08:00"}]
                    calls = call("plans.preview", args)
                else:
                    result = results[-1]
                    if result["status"] == "ok" and "action" in result["data"]:
                        plan = result["data"]["action"]["preview"]["plan"]
                        content = f"已生成预览：{len(plan['blocks'])} 段安排，{len(plan['unassigned'])} 项未分配。请查看方案差异后接受。"
                    else:
                        content = "预览未完成：" + str(result.get("error"))
            elif not results:
                calls = [{"id": "assignment_call_" + str(len(receipts)), "type": "function", "function": {
                            "name": "campus_assignments_list", "arguments": json.dumps({"course_id": "course_synthetic"})}},
                         {"id": "search_call_" + str(len(receipts)), "type": "function", "function": {
                            "name": "campus_materials_search", "arguments": json.dumps({"course_id": "course_synthetic", "query": "实验报告"}, ensure_ascii=False)}}]
            else:
                found = next((r for r in results if r["data"].get("items") and "chunk_id" in r["data"]["items"][0]), None)
                read = next((r for r in results if "resource" in r["data"]), None)
                if found and not read:
                    hit = found["data"]["items"][0]
                    calls = [{"id": "read_call_" + str(len(receipts)), "type": "function", "function": {
                        "name": "campus_materials_read", "arguments": json.dumps({"resource_id": hit["resource_id"], "expected_version": hit["resource_version"], "chunk_ids": [hit["chunk_id"]]})}}]
                elif read:
                    assignments = next(r for r in results if r["data"].get("items") and "due_precision" in r["data"]["items"][0])
                    row = assignments["data"]["items"][0]
                    chunk = read["data"]["items"][0]
                    content = row["title"] + "；截止精度：" + row["due_precision"] + "；原文：" + chunk["text"] + "；来源：" + read["evidence"][0]["source_id"]
                else:
                    content = "没有可读取的正文依据。"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": content,
                    "tool_calls": calls}, "finish_reason": "tool_calls" if calls else "stop"}]}, ensure_ascii=False).encode())

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    port = free_port()
    env = os.environ.copy()
    env.update(STUDY_DATABASE_URL="sqlite:///" + (run_dir / "domain.sqlite").as_posix(),
               STUDY_FILES_DIR=str(run_dir / "files"), STUDY_SECRETS_DIR=str(run_dir / "secrets"), CAMPUS_AGENT_HOME=str(run_dir / "agent"),
               CAMPUS_BRIDGE_URL=f"http://127.0.0.1:{port}", PYTHONUTF8="1", STUDY_WORKER_INPROC="0")
    env["CAMPUS_WEB_ORIGINS"] = f"http://127.0.0.1:{web_port},http://localhost:{web_port}"
    # Seed in a separate process: config.py reads paths only once, before import.
    seed = """
from app import models as m
from app.db import Base, engine, SessionLocal
Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(m.Course(id='course_synthetic', title='合成实验课程', term='2026秋'))
    db.add(m.ProviderConfig(id='provider_synthetic', name='受控测试模型', base_url=__PROVIDER_URL__, model='controlled-test'))
    db.flush()
    db.add(m.Assignment(id='assignment_synthetic', course_id='course_synthetic', title='合成实验报告', due_precision='unknown', revision=1))
    db.add(m.LearningTask(id='task_synthetic', course_id='course_synthetic', title='准备实验报告材料', type='reading', estimated_minutes=120, due_precision='unknown', status='pending'))
    db.add(m.Availability(id=1, timezone='Asia/Shanghai', weekly_windows=[{'weekday':d, 'start':'18:00', 'end':'22:00'} for d in range(1,8)], date_exceptions=[], daily_limit_minutes=120))
    db.add(m.Resource(id='resource_synthetic', course_id='course_synthetic', kind='document', title='合成实验要求', version=2, parse_status='ready'))
    db.flush()
    db.add(m.ContentChunk(id='chunk_synthetic', resource_id='resource_synthetic', resource_version=2, ordinal=0, text='实验报告需要方法说明与原始数据附件。', locator={'paragraph':1}, provenance={'kind':'synthetic'}))
    db.commit()
""".replace("__PROVIDER_URL__", repr(f"http://127.0.0.1:{provider.server_port}/v1"))
    subprocess.run([sys.executable, "-c", seed], cwd=API, env=env, check=True)
    log = (run_dir / "api.log").open("w", encoding="utf-8")
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                           cwd=API, env=env, stdout=log, stderr=log,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    base = f"http://127.0.0.1:{port}"
    client = requests.Session()
    evidence = {"kind": "synthetic-model-real-harness-real-python", "runtime_version": "0.2.0-rc.2", "turns": []}
    web = None

    def post(path, payload, headers=None):
        response = client.post(base + "/api/v1/agent" + path, json=payload, headers=headers, timeout=15)
        assert response.ok, (response.status_code, response.text)
        return response.json()

    def wait_turn(tid):
        deadline, items, after = time.monotonic() + 100, [], 0
        while time.monotonic() < deadline:
            response = client.get(base + "/api/v1/agent/turns/" + tid, params={"after": after}, timeout=10)
            assert response.ok, response.text
            body = response.json()
            events = body.pop("events")
            items.extend(events["items"])
            after = events["next_cursor"]
            if body["status"] not in {"queued", "running"}:
                body["events"] = items
                return body
            time.sleep(0.2)
        raise AssertionError("integration turn timed out")

    try:
        for _ in range(100):
            if api.poll() is not None:
                raise AssertionError("API exited before ready")
            try:
                if client.get(base + "/api/v1/health", timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        if serve:
            env.update(VITE_API_MODE="real", VITE_PROXY_TARGET=base, VITE_API_BASE_URL="", VITE_AGENT_TEST_DATA="1")
            web = subprocess.Popen([shutil.which("node"), str(ROOT / "apps/web/node_modules/vite/bin/vite.js"),
                                    "--port", str(web_port), "--strictPort"], cwd=ROOT / "apps/web", env=env,
                                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            metadata = {"url": f"http://127.0.0.1:{web_port}", "api_url": base, "run_dir": str(run_dir),
                        "api_pid": api.pid, "web_pid": web.pid, "synthetic": True}
            (run_dir / "demo.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            print(json.dumps(metadata), flush=True)
            while not (stop_file and stop_file.exists()):
                if api.poll() is not None or web.poll() is not None:
                    raise AssertionError("owned demo service exited")
                time.sleep(0.25)
            return
        cid = post("/conversations", {"provider_id": "provider_synthetic", "send_to_provider": True, "course_id": "course_synthetic"})["id"]
        for ordinal, text in enumerate(["查实验作业截止以及报告所需材料", "继续上一条，再核对原文依据"]):
            result = post(f"/conversations/{cid}/turns", {"text": text}, {"Idempotency-Key": f"integration-{ordinal}"})
            duplicate = post(f"/conversations/{cid}/turns", {"text": text}, {"Idempotency-Key": f"integration-{ordinal}"})
            assert duplicate["id"] == result["id"]
            completed = wait_turn(result["id"])
            evidence["turns"].append(completed)
            assert completed["status"] == "succeeded", completed
            assert "原始数据附件" in completed["response"] and "unknown" in completed["response"]
            assert "chunk_synthetic" in completed["response"]
            tool_events = [e for e in completed["events"] if e["kind"] == "tool_result"]
            assert {e["data"]["name"] for e in tool_events} == {"assignments.list", "materials.search", "materials.read"}
            assert len(tool_events) == 3
        history = client.get(base + "/api/v1/agent/conversations/" + cid, timeout=10).json()
        assert len(history["turns"]) == 2 and history["current_course_id"] == "course_synthetic"
        assert sum(m["role"] == "user" for m in receipts[-1]["messages"]) >= 2, "official persisted session did not resume"
        expected_tools = {"campus_connections_status", "campus_courses_list", "campus_materials_list", "campus_materials_search", "campus_materials_read", "campus_assignments_list", "campus_tasks_list", "campus_plans_read", "campus_jobs_read", "campus_availability_read", "campus_plans_preview", "campus_plans_apply", "campus_plans_revoke", "campus_tasks_build", "campus_jobs_cancel", "campus_tasks_feedback"}
        assert {s["function"]["name"] for s in receipts[-1]["tools"]} == expected_tools
        cancel_turn = post(f"/conversations/{cid}/turns", {"text": "取消演练"}, {"Idempotency-Key": "integration-cancel"})
        assert slow_started.wait(timeout=15), "controlled provider was not reached before cancellation"
        cancelled = post("/turns/" + cancel_turn["id"] + "/cancel", {})
        assert cancelled["status"] == "cancelled"
        # Send immediately: no artificial shutdown delay hides SDK lease races.
        resumed = post(f"/conversations/{cid}/turns", {"text": "取消后继续核对原文"}, {"Idempotency-Key": "integration-after-cancel"})
        completed = wait_turn(resumed["id"])
        evidence["after_cancel_turn"] = completed
        assert completed["status"] == "succeeded" and "原始数据附件" in completed["response"], completed
        cancelled = wait_turn(cancel_turn["id"])
        assert cancelled["status"] == "cancelled"
        assert not [e for e in cancelled["events"] if e["kind"] == "tool_result"], "cancelled model response caused new tool execution"
        evidence["cancelled_turn"] = cancelled
        # Restart the actual API, keep the client cookie and both journals, then
        # resume again. No progress file or simulated memory substitutes for this.
        api.terminate()
        api.wait(timeout=20)
        api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                               cwd=API, env=env, stdout=log, stderr=log,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(100):
            try:
                if client.get(base + "/api/v1/health", timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        recovered = client.get(base + "/api/v1/agent/conversations/" + cid, timeout=10).json()
        assert len(recovered["turns"]) == 4 and recovered["current_course_id"] == "course_synthetic"
        restarted_turn = post(f"/conversations/{cid}/turns", {"text": "重启后继续核对材料"}, {"Idempotency-Key": "integration-after-restart"})
        completed = wait_turn(restarted_turn["id"])
        evidence["after_api_restart_turn"] = completed
        assert completed["status"] == "succeeded" and "原始数据附件" in completed["response"], completed
        assert sum(m["role"] == "user" for m in receipts[-1]["messages"]) >= 5
        trusted_context = json.loads(next(m["content"] for m in receipts[0]["messages"] if m["role"] == "user").splitlines()[1])
        assert trusted_context["timezone"] == "Asia/Shanghai" and trusted_context["now"].endswith("+08:00")
        # M2 uses the same conversation and the real Harness for every write.
        def plan_turn(text, key, action_id=None):
            payload = {"text": text}
            if action_id:
                payload["action_id"] = action_id
            queued = post(f"/conversations/{cid}/turns", payload, {"Idempotency-Key": key})
            assert post(f"/conversations/{cid}/turns", payload, {"Idempotency-Key": key})["id"] == queued["id"]
            done = wait_turn(queued["id"])
            evidence.setdefault("plan_turns", []).append(done)
            assert done["status"] == "succeeded", done
            results = [e["data"] for e in done["events"] if e["kind"] == "tool_result"]
            assert results and all(e["result"]["status"] == "ok" for e in results), results
            return results

        original_availability = client.get(base + "/api/v1/preferences/availability", timeout=10).json()
        def preview_and_apply(text, key):
            results = plan_turn(text, key)
            assert [e["name"] for e in results] == ["availability.read", "plans.preview"]
            action = results[-1]["result"]["data"]["action"]
            assert action["preview"]["plan"]["blocks"]
            assert action["preview"]["task_titles"]["task_synthetic"] == "准备实验报告材料"
            accepted = post(f"/conversations/{cid}/actions/{action['id']}/accept", {"operation": "apply"}, {"Idempotency-Key": key + "-accept"})
            applied = plan_turn("按这个来", key + "-apply", accepted["id"])
            assert [e["name"] for e in applied] == ["plans.apply"]
            assert applied[0]["result"]["data"]["receipt"]["plan"]["status"] == "active"
            current = client.get(base + "/api/v1/plans/current", timeout=10).json()["plan"]
            assert current["id"] == action["plan_id"]
            return action

        # Relative weekday comes from a semantic tool argument; Python resolves
        # the actual date and exclusion interval, which remains in the preview.
        current_weekday = datetime.fromisoformat(trusted_context["now"]).isoweekday()
        evening_label = {1:"一", 2:"二", 3:"三", 4:"四", 5:"五", 6:"六", 7:"日"}[min(current_weekday + 1, 7)]
        weekly = preview_and_apply(f"安排这周，本周{evening_label}晚上没空", "plan-weekly")
        assert weekly["preview"]["constraints"]["blocked"]
        first = preview_and_apply("把剩余任务安排到明天", "plan-first")
        second = preview_and_apply("把剩余任务安排到明天，19:00–21:00没空", "plan-second")
        for block in second["preview"]["plan"]["blocks"]:
            start = datetime.fromisoformat(block["starts_at"].replace("Z", "+00:00")).astimezone(datetime.fromisoformat(trusted_context["now"]).tzinfo)
            end = datetime.fromisoformat(block["ends_at"].replace("Z", "+00:00")).astimezone(start.tzinfo)
            assert end.hour <= 19 or start.hour >= 21
        assert client.get(base + "/api/v1/preferences/availability", timeout=10).json() == original_availability
        undo = post(f"/conversations/{cid}/actions/{second['id']}/accept", {"operation": "revoke"}, {"Idempotency-Key": "undo-second"})
        undone = plan_turn("撤销刚才安排", "plan-undo", undo["id"])
        assert [e["name"] for e in undone] == ["plans.revoke"]
        assert undone[0]["result"]["data"]["receipt"]["restored_plan_id"] == first["plan_id"]
        assert client.get(base + "/api/v1/plans/current", timeout=10).json()["plan"]["id"] == first["plan_id"]
        replay = plan_turn("重复请求只读取刚才的回执", "plan-undo-replay", undo["id"])
        assert replay[0]["result"]["data"]["receipt"] == undone[0]["result"]["data"]["receipt"]
        evidence["plan_checks"] = {"actual_harness_apply_revoke": True, "same_conversation": True,
            "temporary_constraints_preserve_preferences": True, "previous_plan_restored": True, "durable_replay": True}
        def job_turn(text, key):
            queued = post(f"/conversations/{cid}/turns", {"text": text}, {"Idempotency-Key": key})
            done = wait_turn(queued["id"])
            evidence.setdefault("job_turns", []).append(done)
            assert done["status"] == "succeeded", done
            results = [e["data"] for e in done["events"] if e["kind"] == "tool_result"]
            assert all(e["result"]["error"] is None for e in results), results
            return results
        results = job_turn("为合成实验课程生成学习任务", "job-build")
        assert [e["name"] for e in results] == ["tasks.build", "jobs.read"]
        jid = results[0]["result"]["job_id"]
        assert results[-1]["result"]["data"]["status"] == "queued"
        # Execute the actual registered worker once in the isolated business DB.
        subprocess.run([sys.executable, "-c", "from app.worker import process_next_job; assert process_next_job()"],
                       cwd=API, env=env, check=True, timeout=30, stdout=log, stderr=log)
        completed_job = client.get(base + "/api/v1/jobs/" + jid, timeout=10).json()
        assert completed_job["status"] == "succeeded" and completed_job["result_refs"], completed_job
        results = job_turn("为合成实验课程生成学习任务并停止", "job-cancel")
        assert [e["name"] for e in results] == ["tasks.build", "jobs.cancel"]
        assert results[-1]["result"]["data"]["job"]["status"] == "cancelled"
        evidence["job_checks"] = {"real_harness_build_read_cancel": True, "actual_worker_completed": True,
                                  "queued_not_claimed_complete": True}
        before_assignment = client.get(base + "/api/v1/assignments/assignment_synthetic", timeout=10).json()
        results = job_turn("我完成了准备实验报告材料，用了30分钟", "feedback-completed")
        assert [e["name"] for e in results] == ["tasks.list", "tasks.feedback"]
        assert results[-1]["result"]["data"]["task"]["status"] == "completed"
        assert results[-1]["result"]["data"]["feedback"]["actual_minutes"] == 30
        after_assignment = client.get(base + "/api/v1/assignments/assignment_synthetic", timeout=10).json()
        assert before_assignment["submission_status"] == after_assignment["submission_status"]
        evidence["feedback_checks"] = {"real_harness_read_then_feedback": True, "school_submission_unchanged": True}
        evidence.update(model_call_count=len(receipts), model_visible_tools=sorted(expected_tools), second_turn_preserved_history=True)
        print(json.dumps({"ok": True, "evidence": str(run_dir / "evidence.json"), "model_calls": len(receipts)}, ensure_ascii=False))
    finally:
        (run_dir / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "model-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
        client.close()
        if web:
            web.terminate()
            web.wait(timeout=10)
        api.terminate()
        try:
            api.wait(timeout=20)
        except subprocess.TimeoutExpired:
            api.kill()
            api.wait(timeout=5)
        log.close()
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Keep synthetic API/model/web services for browser QA")
    parser.add_argument("--web-port", type=int, default=15174)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    verify(serve=args.serve, web_port=args.web_port, stop_file=args.stop_file)
