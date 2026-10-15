"""Offline reproducible *scripted* domain/bridge scenarios, NOT an LLM benchmark.

Run: .venv/Scripts/python.exe tools/audit_agent_eval.py --output <json>
No school service, provider credential, persistent DB or user data is accessed.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import subprocess
import hashlib
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import models as m
from app.agent_bridge.tools import ToolContext, execute
from app.agent_bridge.model import messages_for_provider
from app.db import Base
from app.errors import ApiError


class Scenario:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([m.Course(id="a", title="微积分", term="2026秋"),
                         m.Course(id="b", title="物理", term="2026秋"),
                         m.Course(id="private", title="另一账号", term="2026秋")])
        self.db.flush()
        self.db.add_all([m.Resource(id="ra", course_id="a", title="实验说明", kind="document", version=2, parse_status="ready"),
                         m.Resource(id="rb", course_id="b", title="大文件", kind="document", version=1, parse_status="ready"),
                         m.Resource(id="rp", course_id="private", title="隐私", kind="document", version=1, parse_status="ready"),
                         m.Assignment(id="aa", course_id="a", title="实验报告", due_precision="unknown", revision=1)])
        self.db.flush()
        self.db.add(m.ContentChunk(id="ca", resource_id="ra", resource_version=2, ordinal=0,
                                   text="实验报告需包含方法和数据附件。", locator={"page": 1}, provenance={"kind": "synthetic"}))
        self.db.add(m.ContentChunk(id="cp", resource_id="rp", resource_version=1, ordinal=0,
                                   text="实验报告私有内容", locator={}, provenance={"kind": "synthetic"}))
        for i in range(8):
            self.db.add(m.ContentChunk(id=f"cb{i}", resource_id="rb", resource_version=1, ordinal=i,
                                       text=f"章节{i}" + "文" * 5997, locator={"page": i+1}, provenance={"kind": "synthetic"}))
        self.db.commit()
        self.ctx = ToolContext("local", "audit", frozenset({"a", "b"}), allow_uncoursed_tasks=True,
                               request_key="audit-fixture")
        self.trace = []

    def call(self, name, args, ctx=None):
        entry = {"name": name, "arguments": args}
        self.trace.append(entry)
        try:
            result = execute(name, args, self.db, ctx or self.ctx)
            entry.update(status=result["status"], result_bytes=len(json.dumps(result, ensure_ascii=False).encode()))
            return result
        except ApiError as exc:
            entry.update(error=exc.code, details=exc.details)
            raise

    def denied(self, name, args, code):
        try:
            self.call(name, args)
        except ApiError as exc:
            assert exc.code == code, (exc.code, code)
            return exc
        raise AssertionError("Expected denial")

    def close(self):
        self.db.close()
        self.engine.dispose()


def course_fact(s):
    r = s.call("courses.list", {"query": "微积分"})
    assert [i["id"] for i in r["data"]["items"]] == ["a"]
    assert r["status"] == "partial"  # local cache is not all school data


def ambiguous(s):
    s.db.add(m.Course(id="repeat", title="微积分", term="2025秋")); s.db.commit()
    r = s.call("courses.list", {"query": "微积分"}, replace(s.ctx, course_ids=frozenset({"a", "repeat"})))
    assert r["status"] == "needs_user" and len(r["data"]["items"]) == 2


def search_read(s):
    hit = s.call("materials.search", {"course_id": "a", "query": "实验报告"})["data"]["items"][0]
    r = s.call("materials.read", {"resource_id": hit["resource_id"], "expected_version": hit["resource_version"], "chunk_ids": [hit["chunk_id"]]})
    assert "数据附件" in r["data"]["items"][0]["text"] and r["evidence"][0]["source_id"] == "ca"


def scoped_search(s):
    r = s.call("materials.search", {"query": "实验报告"})
    assert {i["course_id"] for i in r["data"]["items"]} == {"a"}
    assert r["coverage"]["scope"] == "authorized_courses"


def empty_scope(s):
    r = s.call("materials.search", {"query": "实验报告"}, replace(s.ctx, course_ids=frozenset()))
    assert not r["data"]["items"]


def invalid_feedback(s):
    exc = s.denied("courses.list", {"limit": "20"}, "INVALID_INPUT")
    assert {"field": "limit", "type": "int_type"} in exc.details["validation_errors"]
    assert "input" not in json.dumps(exc.details)
    assert s.call("courses.list", {"limit": 20})["data"]["items"]


def invalid_nested_safe(s):
    exc = s.denied("plans.preview", {"blocked": [{"starts_at": 123, "ends_at": "private-value"}]}, "INVALID_INPUT")
    assert {"field": "blocked.0.starts_at", "type": "string_type"} in exc.details["validation_errors"]
    assert "private-value" not in json.dumps(exc.details)


def budget_page(s):
    r = s.call("materials.read", {"resource_id": "rb", "expected_version": 1})
    assert len(r["data"]["items"]) == 4
    assert sum(len(i["text"]) for i in r["data"]["items"]) <= 24000
    assert r["next_cursor"] and not r["coverage"]["complete"]


def budget_continue(s):
    ids, cursor = [], None
    for _ in range(10):
        args = {"resource_id": "rb", "expected_version": 1}
        if cursor is not None: args["cursor"] = cursor
        r = s.call("materials.read", args)
        ids += [i["id"] for i in r["data"]["items"]]
        cursor = r["next_cursor"]
        if not cursor: break
    assert ids == [f"cb{i}" for i in range(8)]
    assert len(s.trace) == 2 and len(ids) == len(set(ids))
    assert not r["coverage"]["complete"]  # last page isn't full-file coverage


def explicit_budget(s):
    s.denied("materials.read", {"resource_id": "rb", "expected_version": 1, "chunk_ids": [f"cb{i}" for i in range(8)]}, "BUDGET_EXCEEDED")


def stale_recovery(s):
    s.denied("materials.read", {"resource_id": "ra", "expected_version": 1}, "STALE_VERSION")
    hit = s.call("materials.search", {"course_id": "a", "query": "实验报告"})["data"]["items"][0]
    assert s.call("materials.read", {"resource_id": "ra", "expected_version": hit["resource_version"]})["data"]["items"]


def unauthorized(s):
    s.denied("materials.read", {"resource_id": "rp", "expected_version": 1}, "SCOPE_DENIED")
    s.denied("materials.search", {"query": "实验报告", "course_id": "private"}, "SCOPE_DENIED")


def unknown_dates(s):
    r = s.call("assignments.list", {"due_before": "2026-10-16T00:00:00+08:00"})
    assert r["data"]["items"][0]["due_at"] is None and r["warnings"]
    s.denied("assignments.list", {"due_before": "2026-10-16T00:00:00"}, "INVALID_INPUT")


def queued_not_complete(s):
    r = s.call("tasks.build", {"course_id": "a"})
    assert r["status"] == "queued" and r["job_id"]
    j = s.call("jobs.read", {"job_id": r["job_id"]})
    assert j["status"] == "queued" and j["data"]["status"] == "queued"


def unsupported(s):
    s.denied("campus.query", {"capability": "study_room_availability"}, "INVALID_INPUT")
    s.denied("route.navigate", {}, "UNSUPPORTED")


def no_evidence(s):
    r = s.call("materials.search", {"course_id": "a", "query": "完全未出现"})
    assert not r["data"]["items"] and r["warnings"]


def search_output_budget(s):
    r = s.call("materials.search", {"course_id": "b", "query": "文", "limit": 50})
    assert len(json.dumps(r)) < 100000  # backend's default escaped-wire representation
    assert all(len(i["text"]) <= 1200 for i in r["data"]["items"])
    assert all(i["text_is_excerpt"] for i in r["data"]["items"])
    assert r["status"] == "partial" and not r["coverage"]["complete"]
    hit = r["data"]["items"][0]
    full = s.call("materials.read", {"resource_id": hit["resource_id"], "expected_version": hit["resource_version"], "chunk_ids": [hit["chunk_id"]]})
    assert len(full["data"]["items"][0]["text"]) == 6000


def ordinary_unknown_property_privacy(s):
    exc = s.denied("courses.list", {"secret-sensitive-property": "private-value"}, "INVALID_INPUT")
    assert exc.details["validation_errors"] == [{"field": "<extra>", "type": "extra_forbidden"}]
    assert "secret-sensitive-property" not in json.dumps(exc.details)


def conversation_contract(s):
    messages = [{"role": "user", "content": [{"type": "text", "text": "本周二晚上不要安排"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "收到"}]},
                {"role": "user", "content": [{"type": "text", "text": "再看看明天"}]}]
    r = messages_for_provider(messages)
    assert r[0]["content"] == "本周二晚上不要安排" and len(r) == 3
    assert not s.trace


def ordinary_contract(s):
    r = messages_for_provider([{"role": "user", "content": [{"type": "text", "text": "谢谢"}]}])
    assert r == [{"role": "user", "content": "谢谢"}] and not s.trace


CASES = [("course_fact", course_fact), ("ambiguous_course", ambiguous), ("search_then_read", search_read),
         ("cross_authorized_course_search", scoped_search), ("empty_authorized_scope", empty_scope),
         ("invalid_parameter_recovery", invalid_feedback), ("nested_validation_privacy", invalid_nested_safe),
         ("read_automatic_budget_page", budget_page), ("read_budget_continuation", budget_continue),
         ("explicit_chunks_no_silent_drop", explicit_budget), ("stale_version_recovery", stale_recovery),
         ("unauthorized_denied", unauthorized), ("unknown_deadline_timezone", unknown_dates),
         ("build_and_poll_not_complete", queued_not_complete), ("unsupported_capability", unsupported),
         ("empty_search_not_absence", no_evidence), ("multi_turn_transport_only", conversation_contract),
         ("ordinary_chat_transport_only", ordinary_contract), ("search_output_budget", search_output_budget),
         ("unknown_property_privacy", ordinary_unknown_property_privacy)]


def evaluate():
    results = []
    for name, case in CASES:
        s = Scenario(); started = time.perf_counter()
        error = None
        try: case(s)
        except Exception as exc: error = f"{type(exc).__name__}: {exc}"
        finally: s.close()
        results.append({"name": name, "pass": error is None, "error": error,
                        "elapsed_ms": round((time.perf_counter() - started)*1000, 3), "trace": s.trace,
                        "tool_calls": len(s.trace), "response_bytes": sum(t.get("result_bytes", 0) for t in s.trace)})
    return {"kind": "scripted_synthetic_domain_and_transport_NOT_LLM", "fixture": "audit-v1",
            "model": None, "tokens": None, "timezone": "Asia/Shanghai", "python": sys.version.split()[0],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "passed": sum(r["pass"] for r in results), "total": len(results), "results": results,
            "limitations": ["Tool sequences and parameters are authored, not selected by a model.",
                            "No NLU, live campus services, real provider, or whole-agent task success is measured.",
                            "Latency is local domain execution only; response bytes are not token counts."]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline-ref", help="Read the two original domain modules from a local git ref without checkout or file modification.")
    args = p.parse_args()
    sources = {}
    modules = [("app.routers.search", "apps/api/app/routers/search.py"),
               ("app.agent_bridge.tools", "apps/api/app/agent_bridge/tools.py")]
    for module_name, path in modules:
        if args.baseline_ref:
            source = subprocess.check_output(["git", "show", args.baseline_ref + ":" + path], cwd=ROOT).decode("utf-8")
            exec(compile(source, str(ROOT / path), "exec"), sys.modules[module_name].__dict__)
        else:
            source = (ROOT / path).read_text(encoding="utf-8")
        sources[path] = hashlib.sha256(source.encode()).hexdigest()
    # Rebind the entrypoint too: executing an old module body replaces its
    # functions; an earlier imported function reference would still run new code.
    execute = sys.modules["app.agent_bridge.tools"].execute
    result = evaluate()
    result["source_sha256"] = sources
    result["baseline_ref"] = args.baseline_ref
    result["eval_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "total": result["total"], "output": str(args.output)}))
    sys.exit(0 if result["passed"] == result["total"] else 1)
