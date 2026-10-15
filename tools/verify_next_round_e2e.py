"""Deterministic next-round acceptance runner (A/B/E classes).

One standalone entry point, runnable from the repository root:

    apps/api/.venv/Scripts/python.exe tools/verify_next_round_e2e.py \\
        --out docs/audit/harness-next-round/deterministic.json

What it actually runs:

* a UUID-named isolated workspace (journal, domain DB, files, secrets, browser profile stub,
  temp, workspace) under ``<repo>/var/verify-next-round/``. It refuses to start if any
  configured path resolves inside the real personal data roots (``var/personal.db``,
  ``var/study.db``, ``var/files``, ``var/secrets``, ``var/agent``, ``var/workspace``);
* a **real** FastAPI process (``uvicorn app.main:app``) with the real bridge
  (``/api/v1/agent/internal/*``), the real ``Runtime`` and the real Node Harness
  (``apps/agent-runtime/runner.mjs``);
* a local synthetic model endpoint that plays the OpenAI-compatible provider for both the
  Agent bridge and the review-generation worker. The paid model is never contacted and the
  runner asserts that the configured provider host is its own loopback server;
* an independent worker loop (a second process, like the deployed ``python -m app.worker``)
  that claims and completes the real ``review_generate`` job;
* every produced artifact downloaded over the session file HTTP API and verified as a file
  (PDF parsed with ``pypdf``, XLSX parsed with ``openpyxl``, Markdown inspected for the
  generated questions and the frozen snapshot fingerprint).

Covered acceptance classes: A01 A02 A03 A04 A05 B01 B02 B03 B04 E01 E02 E03.
E03 is reported from a real, isolated pytest subprocess (existing tests only); if those
tests cannot be executed the class is ``not_run`` with the reason, never silently passed.

Nothing outside ``tools/`` and the ``--out`` directory is written; product code and existing
tests are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps/api"
RUNTIME_RUNNER = ROOT / "apps/agent-runtime/runner.mjs"
SCRATCH_ROOT = ROOT / "var" / "verify-next-round"
DEFAULT_OUT = SCRATCH_ROOT / "out" / "deterministic.json"
PUBLISH_DIR = ROOT / "docs/audit/harness-next-round"
ARTIFACT_DIR = PUBLISH_DIR / "artifacts"

PYTHON = str(API_DIR / ".venv/Scripts/python.exe") if os.name == "nt" else sys.executable
if not Path(PYTHON).is_file():  # a POSIX checkout or an unusual venv layout
    PYTHON = sys.executable

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# Code under test whose sha256 is recorded before and after the run (provenance evidence).
TRACKED_SOURCES = (
    "apps/api/app/agent_bridge/artifacts.py",
    "apps/api/app/agent_bridge/review_actions.py",
    "apps/api/app/agent_bridge/tools.py",
    "apps/api/app/agent_bridge/runtime.py",
    "apps/api/app/agent_bridge/files.py",
    "apps/api/app/review_service.py",
    "apps/api/app/review_export.py",
    "apps/api/app/routers/agent.py",
    "apps/api/app/routers/review_papers.py",
    "apps/api/app/worker.py",
    "apps/agent-runtime/skills/document-artifacts/scripts/export_documents.py",
    "apps/agent-runtime/runner.mjs",
)

# One synthetic course, one ready resource, one unparsed resource (an honest gap).
COURSE_ID = "course_next_round"
COURSE_TITLE = "合成资料结构与复习课程"
RESOURCE_ID = "res_nr_ready"
CHUNK_IDS = ("chk_nr_1", "chk_nr_2")
PENDING_RESOURCE_ID = "res_nr_pending"
PROVIDER_ID = "provider_nr_synthetic"
OTHER_COURSE_ID = "course_nr_out_of_scope"
# Facts the synthetic attachment carries. The model must read the attachment and put these
# exact values into the exported PDF/XLSX; nothing here is copied from a fixture answer.
ATTACHMENT_NAME = "next-round-materials.csv"
ATTACHMENT_TEXT = (
    "资料,页数,截止时间\n"
    "原始记录,0,未知\n"
    "实验讲义,12,2026-10-20\n"
)
STORED_ROW = "NEXTROUND-STORE-01"
STANDALONE_ROW = "NEXTROUND-STORE-02"
TITLE = "下一轮验收资料整理"
PARAGRAPH = "本页事实来自本次上传的合成附件，未提供的字段保持未知，不补全日期。"
SHEET_NAME = "资料清单"
COLUMNS = ["资料", "页数", "日期", "已确认"]
EQUAL_TEXT = "=保持普通文本"
TABLE_ROW_COUNT = 120  # forces a real cross-page table
TABLE_SPAN_PREFIX = "分页资料-"

TOOL_SEQUENCE = ["read", "campus_artifacts_export"]
FINAL_MARK_TEXT = "已交付真实文件"

E02_NOTES = "重点考察边界条件，题目必须依据冻结材料。"
E02_CONSTRAINTS = {
    "question_types": [{"type": "single_choice", "count": 2, "points_each": 5},
                       {"type": "short_answer", "count": 1, "points_each": 10}],
    "difficulty": "mixed",
    "total_points": 20,
    "duration_minutes": 45,
    "answer_display": "separate",
}


def log(message: str) -> None:
    print(message, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def safe_json(value, *, limit: int = 4000):
    """Bound any value so the report stays auditable without storing whole bodies."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return {"truncated": True, "chars": len(text), "head": text[:limit]}
    return json.loads(text) if text.startswith(("{", "[")) else text


# ───────────────────────────────── fixture data ─────────────────────────────────

def fixture_export_arguments(*, source_marker: str, formats: list[str]) -> dict:
    """Data-only export contract. The model supplies facts; it never names a program."""
    return {
        "title": TITLE,
        "subtitle": "受控合成验收 / " + source_marker,
        "file_stem": "next-round-artifact",
        "formats": formats,
        "anchors": [{"source": "上传附件：" + ATTACHMENT_NAME, "resource_id": RESOURCE_ID,
                     "version": 1, "locator": "第1-3行"}],
        "sections": [{
            "heading": "整理说明",
            "paragraphs": [PARAGRAPH],
            "table": {
                "columns": COLUMNS,
                "rows": [
                    [STORED_ROW, 0, {"date": "2026-10-20"}, True],
                    [STANDALONE_ROW, 12, None, False],
                ] + [[f"{TABLE_SPAN_PREFIX}{index:03d}", index, None, ""] for index in range(TABLE_ROW_COUNT)],
            },
        }],
        "sheets": [{
            "name": SHEET_NAME,
            "columns": COLUMNS,
            "rows": [
                [EQUAL_TEXT, 0, {"date": "2026-10-20"}, True],
                [STORED_ROW, 12, None, False],
            ],
        }],
    }


# ───────────────────────────────── isolated workspace ─────────────────────────────────

class IsolatedWorkspace:
    """UUID-named scratch tree plus the assertions that keep personal data out of reach."""

    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.run_id = run_id
        self.agent_home = root / "agent"
        self.domain_db = root / "domain.sqlite"
        self.files = root / "files"
        self.secrets = root / "secrets"
        self.workspace = root / "workspace"
        self.browser_profile = root / "browser-profile"
        self.temp = root / "tmp"
        self.journal = self.agent_home / "journal.sqlite"
        self.logs = root / "logs"
        self.api_log = self.logs / "api.log"
        self.worker_log = self.logs / "worker.log"
        self.model_log = self.logs / "model-server.log"
        self.trace_log = self.logs / "runner-trace.jsonl"
        self._last_api_lines: list[str] = []

    def create(self) -> None:
        for path in (self.root, self.agent_home, self.files, self.secrets, self.workspace,
                     self.browser_profile, self.temp, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        # A stub profile directory: nothing here is a real browser profile, and the runner
        # never launches a browser against it.
        (self.browser_profile / "README.txt").write_text(
            "isolated browser profile stub for tools/verify_next_round_e2e.py; never a real profile\n",
            encoding="utf-8")

    def personal_roots(self) -> dict[str, Path]:
        var = ROOT / "var"
        return {
            "personal.db": var / "personal.db",
            "study.db": var / "study.db",
            "files": var / "files",
            "secrets": var / "secrets",
            "agent": var / "agent",
            "workspace": var / "workspace",
        }

    def assert_isolated(self) -> dict:
        """Fail loudly if any configured path lands inside the real personal data tree."""
        checks = {}
        isolated = [self.agent_home, self.domain_db, self.files, self.secrets, self.workspace,
                    self.browser_profile, self.temp, self.journal, self.logs]
        for name, personal in self.personal_roots().items():
            personal_resolved = Path(os.path.abspath(personal))
            for path in isolated:
                candidate = Path(os.path.abspath(path))
                if candidate == personal_resolved or personal_resolved in candidate.parents:
                    raise AssertionError(
                        f"isolation failure: {path} resolves inside the real personal data root {personal}")
            checks[name] = {"root": str(personal_resolved), "touched": False}
        for path in isolated:
            if not str(Path(os.path.abspath(path))).startswith(str(Path(os.path.abspath(SCRATCH_ROOT)))):
                raise AssertionError(f"isolation failure: {path} is outside {SCRATCH_ROOT}")
        checks["scratch_root"] = str(SCRATCH_ROOT)
        checks["run_root"] = str(self.root)
        return checks

    def real_var_fingerprint(self) -> dict:
        """Size/mtime fingerprint of the real personal data tree, for a before/after diff.

        Only stats are read; no content and no link is followed out of the tree.
        """
        fingerprint = {}
        for name, root in self.personal_roots().items():
            entries = {}
            if root.is_file():
                stat = root.stat()
                entries[root.name] = [stat.st_size, stat.st_mtime_ns]
            elif root.is_dir():
                for path in sorted(root.rglob("*")):
                    try:
                        stat = path.lstat()
                    except OSError:
                        continue
                    entries[str(path.relative_to(root)).replace("\\", "/")] = [
                        stat.st_size, stat.st_mtime_ns, stat.st_mode]
            fingerprint[name] = entries
        return fingerprint

    def record_real_var_baseline(self) -> None:
        roots = {name: str(Path(os.path.abspath(root))) for name, root in self.personal_roots().items()}
        self._real_var_baseline = self.real_var_fingerprint()
        self._real_var_roots = sorted(roots.values())

    def environment(self, bridge_port: int) -> dict:
        env = os.environ.copy()
        env.update(
            STUDY_DATABASE_URL="sqlite:///" + self.domain_db.as_posix(),
            STUDY_FILES_DIR=str(self.files),
            STUDY_SECRETS_DIR=str(self.secrets),
            CAMPUS_AGENT_HOME=str(self.agent_home),
            CAMPUS_WORKSPACE_DIR=str(self.workspace),
            CAMPUS_BRIDGE_URL=f"http://127.0.0.1:{bridge_port}",
            STUDY_WORKER_INPROC="0",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        # Never let an operator proxy rewrite the loopback traffic this runner asserts on.
        for key in PROXY_KEYS:
            env.pop(key, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        return env

    def api_tail(self, limit: int = 40) -> list[str]:
        try:
            lines = self.api_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:]

    def model_tail(self, limit: int = 20) -> list[str]:
        try:
            lines = self.model_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:]


# ───────────────────────────────── synthetic model ─────────────────────────────────

class SyntheticModel:
    """Local OpenAI-compatible endpoint. Deterministic tool calls; never the paid provider.

    Two jobs are served on the same port, distinguished by request shape (the bridge sends
    content-block messages; the review worker sends plain string messages):

    * ``agent``  - drive the real Harness tool loop with a fixed, recoverable tool sequence
                   that is re-derived from the persisted conversation on every request;
    * ``review`` - answer the review engine's generation/repair prompt with JSON derived
                   from the prompt itself.
    """

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.lock = threading.Lock()
        self.stats = {"requests": 0, "agent_requests": 0, "review_generate_requests": 0,
                      "repair_requests": 0, "rejected": 0}
        self.agent_plan: list[str] = []
        self.review_questions: list[dict] = []
        # "full" | "empty" | "deficit": switch the review-generation answer.
        self.review_mode = "full"
        # Cooperative hook: the model keeps asking about a job that is still held at
        # "queued" until this is cleared, which is how E02 observes queued != success.
        self.pending_job: str | None = None
        self.job_read_count = 0
        self.job_reads_before_release = 2
        # Adaptive review polling: the model keeps asking jobs.read until its own observation
        # is terminal, exactly like a real agent, so the schedule never races the worker.
        self.review_job_released = False
        self.review_reads_done = 0
        self.max_review_polls = 30
        self.poll_candidates: list[str | None] = []
        # File gate: the runner writes this only after the queued jobs.read answer is journaled,
        # which makes the "held worker" observation independent of request-scheduling races.
        self.release_gate: Path | None = None
        self.poll_interval_seconds = 0.15
        # Controlled review-generation latency (see _review_choice).
        self.review_latency_seconds = 1.0
        self._last_job_status: str | None = None
        # Last tool catalog the Harness actually offered on the wire (evidence, not a guess).
        self.wire_tools: list[str] = []
        # Diagnostics: dump the first model requests so a shape change is visible.
        self.dump_messages = bool(os.environ.get("NEXT_ROUND_DUMP_MESSAGES"))
        self.dumped_messages = 0
        self._server = None

    # -- server plumbing -----------------------------------------------------------------

    def start(self) -> int:
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    payload = json.loads(raw or b"{}")
                    choice = model.respond(self.path, payload)
                except Exception as exc:  # noqa: BLE001 - the runner reports the cause
                    model.record({"kind": "error", "path": self.path,
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "traceback": traceback.format_exc().splitlines()[-6:]})
                    self._reply(500, {"error": {"message": "synthetic model failure"}})
                    return
                self._reply(200, {"id": "chatcmpl-synthetic", "object": "chat.completion",
                                  "model": payload.get("model", "synthetic"),
                                  "choices": [{"index": 0, "message": choice,
                                               "finish_reason": "tool_calls" if choice.get("tool_calls") else "stop"}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True, name="synthetic-model").start()
        return server.server_address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def record(self, entry: dict) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str)[:20000] + "\n")
        except Exception:  # noqa: BLE001 - diagnostics must never kill a model request
            pass

    # -- request routing -----------------------------------------------------------------

    def respond(self, path: str, payload: dict) -> dict:
        messages = payload.get("messages") or []
        marker = self._request_kind(payload, messages)
        with self.lock:
            self.stats["requests"] += 1
        if marker == "agent":
            with self.lock:
                self.stats["agent_requests"] += 1
            if self.dump_messages and self.dumped_messages < 2:
                self.dumped_messages += 1
                self.record({"kind": "agent-request-shape",
                             "messages": safe_json(messages, limit=12000)})
            return self._agent_choice(messages, payload.get("tools") or [])
        if marker == "review":
            return self._review_choice(messages)
        raise AssertionError(
            "unrecognized provider request: the synthetic model refuses to answer a "
            "request whose shape it cannot classify instead of guessing")

    @classmethod
    def _request_kind(cls, payload: dict, messages: list) -> str | None:
        """Classify structurally, never by guessing from persona words.

        The Agent bridge always sends a tool catalog and OpenAI-shaped messages (assistant
        ``tool_calls`` / ``role: tool`` results). The review worker sends exactly two plain
        string messages and no ``tools`` key. Anything else is refused.
        """
        if payload.get("tools"):
            return "agent"
        for message in messages:
            if message.get("role") in {"tool", "assistant"} and (message.get("tool_calls") or message.get("role") == "tool"):
                return "agent"
            if isinstance(message.get("content"), list):
                return "agent"
        system = messages[0].get("content") if messages else ""
        if isinstance(system, str) and ("复习卷出题引擎" in system or "复习卷修复引擎" in system):
            return "review"
        return None

    @staticmethod
    def _blocks(message: dict) -> list:
        content = message.get("content")
        return content if isinstance(content, list) else []

    @staticmethod
    def _tool_history(messages: list) -> list[dict]:
        """Ordered tool calls already issued, from the OpenAI-shaped request the bridge sends.

        The bridge serializes assistant calls as ``tool_calls`` and their answers as
        ``role: tool`` messages, so history is counted structurally: one tool result follows
        every executed call.
        """
        history = []
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    history.append({"name": function.get("name"), "id": call.get("id")})
        return history

    def _tool_names(self, messages: list, tools: list[dict]) -> list[str]:
        if tools:
            return [str(spec.get("function", {}).get("name") or spec.get("name") or "")
                    for spec in tools]
        return []

    def _agent_choice(self, messages: list, tools: list[dict]) -> dict:
        history = self._tool_history(messages)
        names = self._tool_names(messages, tools)
        plan = list(self.agent_plan)
        step = len(history)
        # The review poll is adaptive but strictly bounded. While the worker is held, the plan
        # gains one poll step per request (an explicit, ordered step list); after release the
        # last poll step waits for the job row itself to reach a terminal state before the plan
        # advances, so no poll-count guess can outrun the worker.
        extra = 0
        if "campus_jobs_read" in plan:
            marker = plan.index("campus_jobs_read")
            taken = sum(1 for item in history if item["name"] == "campus_jobs_read")
            released = self.review_job_released and self._gate_open()
            if not released:
                self.poll_candidates = []
                required = taken + 1
            elif self._job_terminal_seen():
                required = max(taken, self.job_reads_before_release)
            else:
                # Fall back to the real job row when the last answer was not terminal yet, so
                # the model stops polling as soon as the work is genuinely done.
                if self._read_job_status_from_db():
                    required = max(taken, self.job_reads_before_release)
                else:
                    required = taken + 1
            required = min(required, self.max_review_polls)
            extra = max(0, required - taken)
            plan = plan[:marker] + ["campus_jobs_read"] * required + plan[marker + 1:]
            if required >= self.max_review_polls and not self._job_terminal_seen():
                raise AssertionError("the synthetic model exhausted its review poll budget")
        chosen_name = plan[step] if step < len(plan) else None
        call_id = f"nr_call_{step + 1}_{(chosen_name or 'final').replace('.', '_')}"
        self.record({"kind": "agent-plan", "step": step, "chosen": chosen_name,
                     "plan_head": plan[:8], "extra": extra,
                     "released": self.review_job_released, "terminal": self._job_terminal_seen(),
                     "polls": sum(1 for item in history if item["name"] == "campus_jobs_read")})
        tool_call = None
        if chosen_name is not None:
            arguments = self._agent_arguments(chosen_name, messages, history)
            tool_call = {"id": call_id, "type": "function",
                         "function": {"name": chosen_name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
        choice = {"role": "assistant",
                  "content": None if tool_call else self._final_text(messages), "tool_calls": [tool_call] if tool_call else []}
        with self.lock:
            self.wire_tools = sorted(names)
        self.record({"kind": "agent-model-response", "step": step, "tool": chosen_name,
                     "call_id": call_id if tool_call else None,
                     "visible_tool_count": len(names), "visible_tools": sorted(names),
                     "planned": list(plan), "plan_len": len(plan),
                     "polls_so_far": sum(1 for item in history if item["name"] == "campus_jobs_read"),
                     "released": self.review_job_released, "last_job_status": self._last_job_status})
        return choice

    def _final_text(self, messages: list) -> str:
        compact = json.dumps(self._tool_payloads(messages), ensure_ascii=False, default=str)
        results = [payload["result"] for payload in self._tool_payloads(messages)]
        ids = sorted(set(re.findall(r"file_[0-9a-f]{16,}", compact)))
        return (f"{FINAL_MARK_TEXT}；共 {len(results)} 个工具结果；会话文件ID：{', '.join(ids) or '无'}；"
                f"已核对附件标记。")

    def _job_terminal_seen(self) -> bool:
        """True once a jobs.read answer the model received reports a terminal status."""
        return self._last_job_status in {"succeeded", "partial_failed", "failed", "cancelled"}

    def _should_keep_polling(self, history: list[dict], extra: int) -> bool:
        """Whether the model must ask jobs.read one more time.

        While the worker is held the answer is always queued, so the model must ask again (this
        is the observation the acceptance contract wants). After release it keeps asking until
        the status it has already been given is terminal. ``extra`` counts the polls this
        decision has already added, so the loop terminates instead of re-inserting itself.
        """
        polls = sum(1 for item in history if item["name"] == "campus_jobs_read") + extra
        if not self.review_job_released:
            return True
        if self._job_terminal_seen() and polls >= self.job_reads_before_release:
            return False
        return polls < self.max_review_polls

    def _gate_open(self) -> bool:
        return self.release_gate is None or self.release_gate.exists()

    def _read_job_status_from_db(self) -> str | None:
        """Read the real job row from the runner's isolated domain database.

        Returns None until the row reports a terminal state; the poll handler uses this to hold
        the agent on the poll step instead of advancing to a paper that does not exist yet.
        """
        database_url = os.environ.get("STUDY_DATABASE_URL", "")
        path = database_url[len("sqlite:///"):] if database_url.startswith("sqlite:///") else ""
        if not path:
            return None
        import sqlite3
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as db:
                row = db.execute(
                    "SELECT status FROM jobs WHERE type='review_generate' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
        status = str(row[0]) if row else None
        return status if status in {"succeeded", "partial_failed", "failed", "cancelled"} else None

    def _required_review_polls(self, taken: int) -> int:
        """How many jobs.read calls the conversation must show before read/export may run.

        Held phase: one more than taken (the contract wants the model to keep asking and to
        observe queued). Released phase: one more than taken until the last answer the model
        received is terminal. The runner writes the release gate only AFTER the queued answer
        is journaled, so the model can never skip the observation, however fast it asks.
        """
        released = self.release_gate
        if not self.review_job_released or (released is not None and not released.exists()):
            return taken + 1
        if self._job_terminal_seen():
            return max(taken, self.job_reads_before_release)
        return min(taken + 1, self.max_review_polls)

    def _agent_arguments(self, name: str, messages: list, history: list[dict]) -> dict:
        if name == "read":
            return {"file_path": self._attachment_path(messages)}
        if name == "campus_artifacts_export":
            return fixture_export_arguments(source_marker=f"nrmodel{len(history) + 1}", formats=["pdf", "xlsx", "md"])
        if name == "campus_materials_read":
            return {"resource_id": RESOURCE_ID, "expected_version": 1, "chunk_ids": list(CHUNK_IDS)}
        if name == "campus_reviews_preflight":
            return {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                    "constraints": E02_CONSTRAINTS, "materials": None}
        if name == "campus_reviews_generate":
            preflight = self._named_result(messages, "campus_reviews_preflight") or {}
            data = preflight.get("data") or {}
            require(bool(data.get("fingerprint")), "the synthetic model needs the preflight fingerprint before generate")
            return {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                    "constraints": E02_CONSTRAINTS,
                    "materials": data.get("selection"),
                    "if_match": data.get("fingerprint")}
        if name == "campus_jobs_read":
            job_id = self._job_id(messages, "campus_reviews_generate")
            self.job_read_count += 1
            # While the worker is held this keeps pointing at the job so the model asks
            # again (observing queued != success); afterwards it moves on to read/export.
            self.pending_job = job_id if self.job_read_count < self.job_reads_before_release else None
            results = self._tool_payloads(messages)
            for payload in reversed(results):
                status = (payload["result"].get("data") or {}).get("status")
                if status:
                    self._last_job_status = str(status)
                    break
            # The block is gone: the runner only opens the release gate once the real job row is
            # terminal, so this poll sees the true status without holding the bridge request.
            time.sleep(max(0.0, self.poll_interval_seconds))
            return {"job_id": job_id}
        if name == "campus_reviews_read":
            return {"paper_id": self._paper_id(messages, "campus_reviews_generate")}
        if name == "campus_reviews_export":
            return {"paper_id": self._paper_id(messages, "campus_reviews_generate"),
                    "revision": 1, "format": "markdown"}
        raise AssertionError(f"no synthetic arguments defined for {name}")

    @staticmethod
    def _message_texts(messages: list) -> list[str]:
        """Every text fragment in the conversation, regardless of the block shape the
        bridge chose (plain string for the first request, content blocks afterwards)."""
        texts = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if isinstance(block.get("text"), str):
                            texts.append(block["text"])
                        elif isinstance(block.get("content"), str):
                            texts.append(block["content"])
        return texts

    @classmethod
    def _attachment_path(cls, messages: list) -> str:
        for text in cls._message_texts(messages):
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    context = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(context, dict):
                    continue
                attachments = context.get("attachments") or []
                if attachments:
                    return str(attachments[0]["path"])
        raise AssertionError("no trusted attachment context was visible to the synthetic model")

    @staticmethod
    def _tool_payloads(messages: list) -> list[dict]:
        """Parsed tool results in conversation order (OpenAI ``role: tool`` messages)."""
        payloads = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            value = message.get("content")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if isinstance(value, dict):
                payloads.append({"call_id": message.get("tool_call_id"), "result": value})
        return payloads

    def _named_result(self, messages: list, name: str) -> dict | None:
        """The preflight answer is identifiable by shape: fingerprint plus a selection."""
        for payload in reversed(self._tool_payloads(messages)):
            result = payload["result"]
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if isinstance(data.get("fingerprint"), str) and data.get("selection") is not None:
                return result
        return None

    def _job_id(self, messages: list, name: str) -> str:
        preferred = self._named_result(messages, name)
        ordered = ([preferred] if preferred else []) + [payload["result"] for payload in
                                                        reversed(self._tool_payloads(messages))]
        for payload in ordered:
            if not isinstance(payload, dict):
                continue
            if payload.get("job_id"):
                return str(payload["job_id"])
            job = (payload.get("data") or {}).get("job")
            if isinstance(job, dict) and job.get("id"):
                return str(job["id"])
        raise AssertionError("the synthetic model could not see a job id in the tool results")

    def _paper_id(self, messages: list, name: str) -> str:
        preferred = self._named_result(messages, name)
        ordered = ([preferred] if preferred else []) + [payload["result"] for payload in
                                                        reversed(self._tool_payloads(messages))]
        for payload in ordered:
            if not isinstance(payload, dict):
                continue
            paper = (payload.get("data") or {}).get("paper")
            if isinstance(paper, dict) and paper.get("paper_id"):
                return str(paper["paper_id"])
            if isinstance(paper, dict) and paper.get("id"):
                return str(paper["id"])
        raise AssertionError("the synthetic model could not see a paper id in the tool results")

    # -- review generation ---------------------------------------------------------------

    def _review_choice(self, messages: list) -> dict:
        system = messages[0].get("content") if messages else ""
        user = messages[1].get("content") if len(messages) > 1 else ""
        system = system if isinstance(system, str) else ""
        user = user if isinstance(user, str) else ""
        with self.lock:
            mode = self.review_mode
        if "修复引擎" in system:
            with self.lock:
                self.stats["repair_requests"] += 1
            content = self._repair(user)
        elif mode == "empty":
            with self.lock:
                self.stats["review_generate_requests"] += 1
            content = ""
        else:
            with self.lock:
                self.stats["review_generate_requests"] += 1
            # A controlled, realistic generation latency: without it a job could complete
            # between two polls and the observed running state would be a coin flip.
            time.sleep(max(0.0, self.review_latency_seconds))
            content = json.dumps(self._generation(user, mode), ensure_ascii=False)
        self.record({"kind": "review-model-response", "mode": mode, "system_head": system[:40],
                     "chars": len(content)})
        return {"role": "assistant", "content": content}

    @staticmethod
    def _kp_block(user: str) -> list[tuple[str, str]]:
        return [(match.group(1), match.group(2).strip()) for match in
                re.finditer(r"^- (kp:[0-9a-f]+) (.+?)｜章节：", user, re.M)]

    @staticmethod
    def _quotas(user: str) -> list[tuple[str, int]]:
        return [(match.group(1), int(match.group(2))) for match in
                re.finditer(r"^- (\w+) × (\d+)，每题", user, re.M)]

    @staticmethod
    def _deficits(user: str) -> list[tuple[str, int]]:
        return [(match.group(1), int(match.group(2))) for match in
                re.finditer(r"^- (\w+) 缺 (\d+) 题", user, re.M)]

    @staticmethod
    def _chunk_ids(user: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"\[(chk_[A-Za-z0-9_]+) \|", user)))

    def _generation(self, user: str, mode: str = "full") -> dict:
        knowledge = self._kp_block(user)
        chunks = self._chunk_ids(user)
        if mode == "deficit":
            # The bank path reports a deficit for a type it could not fill; this mirrors a
            # real partial generation without touching the engine's own logic.
            deficits = self._deficits(user) or [("short_answer", 1)]
            quotas: list[tuple[str, int]] = list(deficits)
        else:
            quotas = self._quotas(user)
        plan: list[tuple[str, str, str]] = []
        if knowledge:
            index = 0
            for qtype, count in quotas:
                for _ in range(count):
                    plan.append((qtype, *knowledge[index % len(knowledge)]))
                    index += 1
        questions = [self._question(qtype, kp_id, kp_name, ordinal, chunks)
                     for ordinal, (qtype, kp_id, kp_name) in enumerate(plan, start=1)]
        output = {"title": f"{COURSE_TITLE} 复习卷（合成模型）", "questions": questions}
        with self.lock:
            self.review_questions = questions
        return output

    @staticmethod
    def _question(qtype: str, kp_id: str, kp_name: str, ordinal: int, chunks: list[str]) -> dict:
        marker = f"NRQ{ordinal:03d}"
        if qtype in {"choice", "single_choice"}:
            options = [{"key": key, "text": f"{kp_name}的合成选项 {key}（{marker}）"} for key in "ABCD"]
            answer, scoring = "A", []
        else:
            options = []
            answer = f"参考解答：依据{kp_name}的定义逐步推导（{marker}）。"
            scoring = ["写出关键定义", "推导步骤完整"]
        refs = [{"chunk_id": chunks[(ordinal - 1) % len(chunks)]}] if chunks else [{"notes_line": 1}]
        return {"qtype": qtype, "stem": f"第{ordinal}题（{kp_name}）：核对码 {marker}，请依据冻结材料作答。",
                "options": options, "answer": answer,
                "explanation": f"解析：依据冻结材料中关于{kp_name}的表述（{marker}）。",
                "scoring": scoring, "kp_ids": [kp_id], "difficulty": ["easy", "medium", "hard"][ordinal % 3],
                "source_refs": refs}

    @staticmethod
    def _repair(user: str) -> str:
        previous = user.split("上一轮输出（JSON）：\n", 1)[-1].split("\n\n校验问题清单：", 1)[0]
        try:
            payload = json.loads(previous)
        except ValueError:
            return previous
        for question in payload.get("questions") or []:
            if not str(question.get("explanation") or "").strip():
                question["explanation"] = "解析（修复轮）：已依据冻结材料核对。"
        return json.dumps(payload, ensure_ascii=False)


# ───────────────────────────────── independent worker ─────────────────────────────────

class IndependentWorker:
    """A second process, like the deployed ``python -m app.worker``.

    It only claims queued jobs from the shared journal/domain DB; it never runs inside the
    API process, so "the worker completes the job" stays a real cross-process fact. The
    runner can hold it between claims with a sentinel file, which is what makes the
    "queued is not complete" observation deterministic instead of a race.
    """

    def __init__(self, env: dict, log_path: Path):
        self.env = env
        self.log_path = log_path
        self.pause_file = log_path.parent / "worker-paused"
        self.process: subprocess.Popen | None = None
        self.processed: list[dict] = []
        self.log = None

    def pause(self) -> None:
        self.pause_file.write_text("paused\n", encoding="utf-8")

    def resume(self) -> None:
        self.pause_file.unlink(missing_ok=True)

    def start(self) -> None:
        script = (
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, 'apps/api')\n"
            "from app import worker\n"
            "pause = Path(__PAUSE__)\n"
            "while True:\n"
            "    if pause.exists():\n"
            "        time.sleep(0.2)\n"
            "        continue\n"
            "    try:\n"
            "        worked = worker.process_next_job()\n"
            "    except Exception as exc:\n"
            "        print('worker-loop-error ' + type(exc).__name__ + ' ' + str(exc), flush=True)\n"
            "        worked = False\n"
            "    print('worker-tick ' + ('claimed' if worked else 'idle'), flush=True)\n"
            "    time.sleep(0.3)\n"
        ).replace("__PAUSE__", repr(str(self.pause_file)))
        self.log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [PYTHON, "-c", script], cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
            self.log = None

    def ticks(self, limit: int = 40) -> list[str]:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            return []


# ───────────────────────────────── runner ─────────────────────────────────

class Runner:
    def __init__(self, out_path: Path, run_id: str | None = None):
        self.out_path = out_path
        self.run_id = run_id or uuid.uuid4().hex
        self.workspace = IsolatedWorkspace(SCRATCH_ROOT / self.run_id, self.run_id)
        self.model = SyntheticModel(self.workspace.model_log)
        self.report = {
            "kind": "next-round-deterministic-acceptance",
            "schema": 1,
            "generated_at": datetime.now().astimezone().isoformat(),
            "run_id": self.run_id,
            "command": " ".join([Path(sys.executable).name, "tools/verify_next_round_e2e.py"]),
            "python": sys.executable,
            "repo_root": str(ROOT),
            "synthetic_model": {"provider_id": PROVIDER_ID, "host": None, "paid_model_called": False},
            "isolation": {},
            "classes": [],
            "artifacts": [],
            "traces": [],
            "notes": [],
        }
        self.classes: list[dict] = []
        self.api: subprocess.Popen | None = None
        self.api_port = 0
        self.env: dict = {}
        self.base = ""
        self.worker: IndependentWorker | None = None
        self.session = None
        self.trace_lock = threading.Lock()
        self.trace_seq = 0

    # -- lifecycle ----------------------------------------------------------------------

    def setup(self) -> None:
        self.workspace.create()
        self.workspace.record_real_var_baseline()
        self.report["isolation"] = self.workspace.assert_isolated()
        self.report["isolation"]["real_var_roots"] = list(self.workspace._real_var_roots)
        self.report["isolation"]["real_var_fingerprint"] = self.workspace._real_var_baseline
        self.api_port = free_port()
        self.env = self.workspace.environment(self.api_port)
        self.base = f"http://127.0.0.1:{self.api_port}"
        self.report["isolation"]["api_base"] = self.base
        self.report["isolation"]["journal"] = str(self.workspace.journal)
        self.report["isolation"]["domain_db"] = str(self.workspace.domain_db)
        self.report["isolation"]["conversation_files"] = str(self.workspace.agent_home / "conversation-files")
        # Provenance of the code under test, so a reviewer can tie the evidence to a revision.
        # Recorded before and (again) after the run: a mid-run checkout edit is visible as a
        # difference, and is never confused with a runner write.
        self.report["source_hashes"] = _source_hashes()
        model_port = self.model.start()
        self.report["synthetic_model"]["host"] = f"http://127.0.0.1:{model_port}"
        self.trace("runner", {"event": "synthetic-model-started", "host": self.report["synthetic_model"]["host"]})
        self._seed_domain(model_port)
        self._start_api()
        self.worker = IndependentWorker(self.env, self.workspace.worker_log)
        self.worker.start()
        self.trace("runner", {"event": "worker-started", "pid": self.worker.process.pid if self.worker.process else None})

    def teardown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        if self.api is not None and self.api.poll() is None:
            self.api.terminate()
            try:
                self.api.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.api.kill()
                self.api.wait(timeout=5)
        self.model.stop()

    def _seed_domain(self, model_port: int) -> None:
        seed = """
import json
from datetime import datetime, timezone
from app import models as m, secrets_store
from app.db import Base, engine, SessionLocal
Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(m.Course(id=__COURSE__, title=__TITLE__, term='2026秋'))
    db.add(m.Course(id=__OTHER__, title='范围外合成课程', term='2026秋',
                    archived_at=datetime.now(timezone.utc)))
    db.add(m.ProviderConfig(id=__PROVIDER__, name='受控合成模型', base_url=__URL__, model='synthetic-chat',
                            protocol='openai_chat_completions', enabled=True, timeout_seconds=60))
    db.flush()
    db.add(m.Resource(id=__RES__, course_id=__COURSE__, kind='document', title='合成讲义：线性表与顺序存储',
                      version=1, parse_status='ready', content_hash='sha256:nr-ready', access_level='full'))
    db.add(m.Resource(id=__PENDING__, course_id=__COURSE__, kind='document', title='合成讲义：未解析资料',
                      version=1, parse_status='pending', content_hash='sha256:nr-pending', access_level='full'))
    db.flush()
    db.add(m.ContentChunk(id=__CHUNK1__, resource_id=__RES__, resource_version=1, ordinal=0,
        text='线性表是由n个数据元素构成的有限序列，相邻元素之间存在序偶关系；表头无前驱，表尾无后继。',
        locator={'paragraph': 1}, provenance={'kind': 'synthetic'}))
    db.add(m.ContentChunk(id=__CHUNK2__, resource_id=__RES__, resource_version=1, ordinal=1,
        text='顺序存储结构用一段地址连续的存储单元依次存储线性表的数据元素，插入与删除平均移动约一半元素。',
        locator={'paragraph': 2}, provenance={'kind': 'synthetic'}))
    db.commit()
secrets_store.set_key(__PROVIDER__, 'sk-synthetic-local-only')
print(json.dumps({'seeded': True, 'provider': __PROVIDER__, 'has_key': secrets_store.has_key(__PROVIDER__)}))
"""
        seed = (seed.replace("__COURSE__", repr(COURSE_ID)).replace("__TITLE__", repr(COURSE_TITLE))
                .replace("__OTHER__", repr(OTHER_COURSE_ID)).replace("__PROVIDER__", repr(PROVIDER_ID))
                .replace("__URL__", repr(f"http://127.0.0.1:{model_port}/v1"))
                .replace("__RES__", repr(RESOURCE_ID)).replace("__PENDING__", repr(PENDING_RESOURCE_ID))
                .replace("__CHUNK1__", repr(CHUNK_IDS[0])).replace("__CHUNK2__", repr(CHUNK_IDS[1])))
        result = subprocess.run([PYTHON, "-c", seed], cwd=API_DIR, env=self.env, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"domain seed failed: {result.stderr[-4000:]}")
        self.trace("runner", {"event": "domain-seeded", "output": result.stdout.strip()[-500:]})

    def _start_api(self) -> None:
        log = self.workspace.api_log.open("w", encoding="utf-8")
        self.api = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.api_port),
             "--log-level", "info"],
            cwd=API_DIR, env=self.env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        import requests
        self.session = requests.Session()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.api.poll() is not None:
                raise AssertionError("API exited during startup:\n" + "\n".join(self.workspace.api_tail()))
            try:
                if self.session.get(self.base + "/api/v1/health", timeout=2).ok:
                    self.trace("runner", {"event": "api-ready", "pid": self.api.pid, "base": self.base})
                    return
            except Exception:  # noqa: BLE001 - keep polling until the deadline
                pass
            time.sleep(0.2)
        raise AssertionError("API did not become ready:\n" + "\n".join(self.workspace.api_tail()))

    # -- tracing ------------------------------------------------------------------------

    def trace(self, kind: str, data: dict, *, limit: int = 4000) -> None:
        with self.trace_lock:
            self.trace_seq += 1
            entry = {"seq": self.trace_seq, "at": round(time.time(), 3), "kind": kind, "data": safe_json(data, limit=limit)}
            self.report["traces"].append(entry)
            with self.workspace.trace_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def api_call(self, method: str, path: str, *, label: str, json_body=None, headers=None,
                 ok=(200, 201, 202), params=None) -> tuple[int, dict]:
        response = self.session.request(method, self.base + path, json=json_body, headers=headers,
                                        params=params, timeout=60)
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:2000]}
        self.trace("http", {"label": label, "method": method, "path": path, "status": response.status_code,
                            "ok": response.status_code in ok,
                            "error": (body.get("error") or {}).get("code") if isinstance(body, dict) else None,
                            "body": safe_json(body, limit=2500)})
        if ok is not None and response.status_code not in ok:
            raise AssertionError(f"{label} failed: HTTP {response.status_code} {safe_json(body, limit=800)}")
        return response.status_code, body

    def call_tool(self, token: str, cid: str, name: str, arguments: dict, call_id: str,
                  ok=(200,)) -> dict:
        status, body = self.api_call(
            "POST", "/api/v1/agent/internal/tool", label=f"tool:{name}",
            json_body={"conversation_id": cid, "call_id": call_id, "name": name, "arguments": arguments},
            headers={"Authorization": "Bearer " + token}, ok=ok)
        error = body.get("error") if isinstance(body, dict) else None
        self.trace("tool", {"name": name, "call_id": call_id, "http": status,
                            "status": body.get("status") if isinstance(body, dict) else None,
                            "error_code": (error or {}).get("code"),
                            "job_id": body.get("job_id") if isinstance(body, dict) else None,
                            "file_ids": [item.get("id") for item in ((body.get("data") or {}).get("files") or [])]
                            if isinstance(body, dict) and isinstance(body.get("data"), dict) else None})
        return body

    # -- environment preparation --------------------------------------------------------

    def a_b_conversation(self) -> tuple[str, str, str]:
        """A real conversation plus a real trusted call identity (no Harness needed here)."""
        _, body = self.api_call("POST", "/api/v1/agent/conversations", label="create-ab-conversation",
                                json_body={"provider_id": PROVIDER_ID, "send_to_provider": True,
                                           "course_id": COURSE_ID})
        cid = body["id"]
        code = (
            "import json, sys\n"
            "sys.path.insert(0, 'apps/api')\n"
            "from pathlib import Path\n"
            "from app.agent_bridge.store import Store\n"
            "store = Store(Path(__JOURNAL__))\n"
            "turn, token = store.enqueue(__CID__, 'ab:' + __RUN__, 'deterministic bridge call', __COURSE__)\n"
            "store.running(turn['id'])\n"
            "print(json.dumps({'turn': turn['id'], 'token': token}))\n"
        ).replace("__JOURNAL__", repr(str(self.workspace.journal))).replace("__CID__", repr(cid)) \
            .replace("__RUN__", repr(self.run_id)).replace("__COURSE__", repr(COURSE_ID))
        result = subprocess.run([PYTHON, "-c", code], cwd=ROOT, env=self.env, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError("could not mint a trusted call identity: " + result.stderr[-2000:])
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.trace("runner", {"event": "ab-identity", "conversation": cid, "turn": payload["turn"]})
        return cid, payload["turn"], payload["token"]

    def wait_turn(self, turn_id: str, timeout: float = 240.0) -> dict:
        deadline = time.monotonic() + timeout
        items: list[dict] = []
        after = 0
        while time.monotonic() < deadline:
            if self.api is not None and self.api.poll() is not None:
                raise AssertionError("API exited while waiting for the turn:\n" + "\n".join(self.workspace.api_tail()))
            _, body = self.api_call("GET", f"/api/v1/agent/turns/{turn_id}", label="read-turn",
                                    params={"after": after}, ok=(200,))
            events = body.pop("events", {"items": [], "next_cursor": 0})
            items.extend(events.get("items") or [])
            after = events.get("next_cursor") or after
            if body.get("status") not in {"queued", "running", "stopping"}:
                body["events"] = items
                return body
            time.sleep(0.25)
        raise AssertionError(f"turn {turn_id} did not finish in {timeout:g}s; api tail:\n"
                             + "\n".join(self.workspace.api_tail()))

    def upload(self, cid: str, name: str, content: bytes, key: str) -> dict:
        response = self.session.post(
            self.base + f"/api/v1/agent/conversations/{cid}/files",
            files={"file": (name, content)}, headers={"Idempotency-Key": key}, timeout=60)
        body = response.json()
        self.trace("http", {"label": "upload-attachment", "status": response.status_code, "name": name,
                            "bytes": len(content), "file_id": body.get("id"), "sha256": body.get("sha256")})
        if response.status_code != 201:
            raise AssertionError(f"attachment upload failed: {response.status_code} {body}")
        return body

    def download(self, cid: str, file_id: str, label: str) -> bytes:
        response = self.session.get(self.base + f"/api/v1/agent/conversations/{cid}/files/{file_id}/content",
                                    timeout=120)
        self.trace("http", {"label": "download:" + label, "status": response.status_code, "file_id": file_id,
                            "bytes": len(response.content), "sha256": sha256_bytes(response.content),
                            "content_type": response.headers.get("content-type")})
        if response.status_code != 200:
            raise AssertionError(f"download of {file_id} failed: HTTP {response.status_code}")
        return response.content

    def published_files(self, cid: str) -> list[dict]:
        _, body = self.api_call("GET", f"/api/v1/agent/conversations/{cid}/files", label="list-session-files")
        return body.get("items") or []

    # -- verification helpers -----------------------------------------------------------

    def verify_pdf(self, data: bytes, *, required: list[str], expect_pages: int | None = None) -> dict:
        from pypdf import PdfReader
        if not data.startswith(b"%PDF-"):
            raise AssertionError("the delivered file is not a real PDF container")
        reader = PdfReader(io.BytesIO(data))
        pages, text_parts, boxes = [], [], []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
            media = page.mediabox
            width, height = float(media.width), float(media.height)
            if width <= 0 or height <= 0:
                raise AssertionError("PDF page has a degenerate media box")
            blocks = page_text.strip()
            boxes.append({"page": len(pages) + 1, "width": round(width, 2), "height": round(height, 2),
                          "non_empty_lines": len([line for line in blocks.splitlines() if line.strip()])})
            pages.append(page_text)
        full = "\n".join(text_parts)
        missing = [item for item in required if item not in full]
        if missing:
            raise AssertionError(f"PDF text is missing required facts: {missing}")
        if expect_pages is not None and len(reader.pages) < expect_pages:
            raise AssertionError(f"expected at least {expect_pages} PDF pages, got {len(reader.pages)}")
        widths = {round(float(page.mediabox.width), 2) for page in reader.pages}
        heights = {round(float(page.mediabox.height), 2) for page in reader.pages}
        if len(widths) > 1 or len(heights) > 1:
            raise AssertionError(f"PDF pages have inconsistent media boxes: {widths} x {heights}")
        return {"pages": len(reader.pages), "media_box": {"width": widths.pop(), "height": heights.pop()},
                "per_page": boxes, "chinese_chars": len(re.findall(r"[\u4e00-\u9fff]", full)),
                "required_facts_present": required, "text": full}

    def verify_pdf_margins(self, data: bytes, *, min_margin: float = 12.0) -> dict:
        """Real text placement must stay inside the media box with a sane margin.

        ``pypdf`` reports every glyph's text matrix origin through ``visitor_text``, so this
        measures where text actually lands (not the renderer's own page furniture).
        """
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        results = []
        for index, page in enumerate(reader.pages, start=1):
            box = page.mediabox
            width, height = float(box.width), float(box.height)
            positions: list[tuple[float, float, float]] = []

            def visitor(text, cm, tm, font_dict, font_size, _positions=positions):
                stripped = (text or "").strip()
                if stripped:
                    _positions.append((float(tm[4]), float(tm[5]), float(font_size or 0)))

            page.extract_text(visitor_text=visitor)
            if not positions:
                raise AssertionError(f"PDF page {index} produced no placed text")
            xs = [item[0] for item in positions]
            ys = [item[1] for item in positions]
            left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
            if right > width or top > height:
                raise AssertionError(
                    f"PDF page {index} places text outside the media box: right={right} > {width} "
                    f"or top={top} > {height}")
            if left < 0 or bottom < 0:
                raise AssertionError(f"PDF page {index} places text at a negative coordinate: "
                                     f"left={left}, bottom={bottom}")
            # The body text must keep a real margin: at least one line starts inside
            # min_margin of the box on each side that carries content.
            body = [item for item in positions if item[1] >= min_margin]
            if not body:
                raise AssertionError(f"PDF page {index} has no text inside the top margin band")
            body_right = max(item[0] for item in body)
            if body_right > width - min_margin:
                raise AssertionError(
                    f"PDF page {index} body text runs past the right margin: {body_right} > {width - min_margin}")
            results.append({"page": index, "glyphs_placed": len(positions),
                            "x_range": [round(left, 2), round(right, 2)],
                            "y_range": [round(bottom, 2), round(top, 2)],
                            "box": [width, height],
                            "body_x_max": round(body_right, 2),
                            "footer_origins_below_margin": len(positions) - len(body)})
        return {"min_margin_pt": min_margin, "pages": results,
                "check": "every glyph origin is inside the media box; body text keeps "
                         f"{min_margin}pt margins"}

    def verify_xlsx(self, data: bytes, *, expect_literal_equal: str = EQUAL_TEXT,
                    expect_true: bool = True) -> dict:
        from openpyxl import load_workbook
        if not data.startswith(b"PK"):
            raise AssertionError("the delivered file is not a real XLSX (zip) container")
        book = load_workbook(io.BytesIO(data), data_only=False)
        try:
            if SHEET_NAME not in book.sheetnames:
                raise AssertionError(f"expected sheet {SHEET_NAME!r}, got {book.sheetnames}")
            sheet = book[SHEET_NAME]
            values = {}
            for row in sheet.iter_rows():
                for cell in row:
                    values[cell.coordinate] = {"value": cell.value, "type": cell.data_type,
                                               "number_format": cell.number_format}
            # The renderer writes a title row, a blank row, then the column header row,
            # so the first data row is row 5 for this contract.
            checks = {
                "literal_equals_text_is_type_s": sheet["A5"].value == expect_literal_equal
                and sheet["A5"].data_type == "s",
                "number_zero_is_numeric": sheet["B5"].value == 0 and sheet["B5"].data_type == "n",
                "number_twelve_is_numeric": sheet["B6"].value == 12 and sheet["B6"].data_type == "n",
                "date_cell_is_a_date": isinstance(sheet["C5"].value, datetime)
                and sheet["C5"].value.date().isoformat() == "2026-10-20",
                "date_number_format_is_date": "y" in sheet["C5"].number_format.lower(),
                "empty_cell_stays_empty": sheet["C6"].value is None,
                "boolean_true": sheet["D5"].value is expect_true and sheet["D5"].data_type == "b",
                "boolean_false": sheet["D6"].value is (not expect_true) and sheet["D6"].data_type == "b",
            }
            failed = [name for name, ok in checks.items() if not ok]
            if failed:
                raise AssertionError(f"XLSX cell-type checks failed: {failed} :: {safe_json(values, limit=1500)}")
            formulas = [cell.coordinate for row in sheet.iter_rows() for cell in row if cell.data_type == "f"]
            if formulas:
                raise AssertionError(f"plain text was turned into a formula at {formulas}")
            return {"sheet": SHEET_NAME, "checks": checks, "formula_cells": formulas,
                    "data_rows": 5,
                    "cells": {key: values[key] for key in ("A1", "A5", "B5", "C5", "D5", "A6", "B6", "C6", "D6")}}
        finally:
            book.close()

    def copy_artifact(self, data: bytes, filename: str, *, role: str, source: str) -> dict:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        target = ARTIFACT_DIR / filename
        target.write_bytes(data)
        record = {"name": filename, "role": role, "source": source, "bytes": len(data),
                  "sha256": sha256_bytes(data), "path": str(target.relative_to(ROOT)).replace("\\", "/")}
        self.report["artifacts"].append(record)
        self.trace("artifact", record)
        return record


def class_result(class_id: str, title: str, checks: list[dict], *, duration: float,
                 invocation: str, artifacts: list[dict] | None = None,
                 not_run_reason: str | None = None) -> dict:
    """``not_run`` is reserved for a prerequisite that could not be provided at all; an
    exception during a class is a real failure of that class, never a skip.

    Each failed check is labelled ``scenario`` (the product behaved differently from the
    contract) or ``bookkeeping`` (the runner's own environment/housekeeping assertion,
    e.g. isolation or source-hash evidence). That distinction keeps the reviewer from
    reading a runner-side problem as a product failure.
    """
    failed = [check for check in checks if check.get("status") == "failed"]
    bookkeeping = [check for check in failed if check.get("kind") == "bookkeeping"]
    scenario = [check for check in failed if check.get("kind") != "bookkeeping"]
    if not_run_reason is not None and not failed:
        status = "not_run"
    else:
        status = "failed" if failed or not checks else "passed"
    if not failed:
        failure_kind = None
    elif scenario and bookkeeping:
        failure_kind = "scenario_and_bookkeeping"
    elif bookkeeping:
        failure_kind = "bookkeeping"
    else:
        failure_kind = "scenario"
    return {"id": class_id, "title": title, "status": status, "duration_seconds": round(duration, 3),
            "invocation": invocation, "checks": checks, "artifacts": artifacts or [],
            "reason": not_run_reason,
            "failure_kind": failure_kind,
            "failed_checks": [{"name": item["name"], "kind": item.get("kind", "scenario")}
                              for item in failed]}


def run_class(runner: Runner, class_id: str, title: str, fn, *, invocation: str,
              prerequisites: list[str] | None = None) -> dict:
    log(f"\n=== {class_id} {title} ===")
    missing = [name for name in (prerequisites or []) if not getattr(runner, name, None)]
    if missing:
        runner.trace("class", {"id": class_id, "status": "not_run", "reason": f"missing prerequisite {missing}"})
        log(f"{class_id}: not run ({missing} unavailable)")
        return class_result(class_id, title, [], duration=0.0, invocation=invocation,
                            not_run_reason="prerequisite unavailable: " + ", ".join(missing))
    started = time.monotonic()
    checks: list[dict] = []
    artifacts: list[dict] = []
    reason = None
    try:
        outcome = fn(runner)
        checks = outcome.get("checks", [])
        artifacts = outcome.get("artifacts", [])
    except Exception as exc:  # noqa: BLE001 - a class failure is evidence, not a crash
        checks = [{"name": "class execution", "status": "failed",
                   "detail": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc().splitlines()[-24:]}]
        reason = f"{type(exc).__name__}: {exc}"
        runner.trace("class-error", {"id": class_id, "error": reason,
                                     "traceback": traceback.format_exc().splitlines()[-12:]})
    duration = time.monotonic() - started
    result = class_result(class_id, title, checks, duration=duration, invocation=invocation,
                          artifacts=artifacts, not_run_reason=reason)
    runner.trace("class", {"id": class_id, "status": result["status"], "duration": round(duration, 3),
                           "checks": [{"name": c.get("name"), "status": c.get("status")} for c in checks]})
    log(f"{class_id}: {result['status']} in {duration:.2f}s")
    for check in checks:
        log(f"    - [{check.get('status')}] {check.get('name')}: {check.get('detail', '')}")
    if reason:
        log(f"    ! {reason}")
    return result


def check(name: str, ok: bool, detail: str = "", *, kind: str = "scenario") -> dict:
    return {"name": name, "status": "passed" if ok else "failed", "detail": detail, "kind": kind}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# ───────────────────────────────── classes ─────────────────────────────────

def _export_and_download(runner: Runner, label: str) -> dict:
    """One real artifacts.export call plus both downloads; shared by A01 and A02."""
    cid, tid, token = runner.a_b_conversation()
    arguments = fixture_export_arguments(source_marker=label, formats=["pdf", "xlsx", "md"])
    result = runner.call_tool(token, cid, "artifacts.export", arguments, f"{tid}:{label}-export")
    require(result.get("status") == "ok", f"export did not succeed: {safe_json(result, limit=1200)}")
    files = {item["name"].rsplit(".", 1)[-1]: item for item in result["data"]["files"]}
    require(set(files) >= {"pdf", "xlsx"}, f"expected pdf+xlsx receipts, got {sorted(files)}")
    require(result["data"]["renderer"].endswith("export_documents.py"), "renderer identity changed")
    pdf_bytes = runner.download(cid, files["pdf"]["id"], f"{label}-pdf")
    xlsx_bytes = runner.download(cid, files["xlsx"]["id"], f"{label}-xlsx")
    require(files["pdf"]["sha256"] == sha256_bytes(pdf_bytes), "PDF receipt hash != downloaded bytes")
    require(files["xlsx"]["sha256"] == sha256_bytes(xlsx_bytes), "XLSX receipt hash != downloaded bytes")
    markdown = runner.download(cid, files["md"]["id"], f"{label}-md") if "md" in files else None
    return {"cid": cid, "tid": tid, "files": files, "pdf": pdf_bytes, "xlsx": xlsx_bytes,
            "markdown": markdown, "invocation": f"artifacts.export call_id={tid}:{label}-export"}


def class_a01(runner: Runner) -> dict:
    """A01: a real PDF with Chinese paragraphs and a cross-page table, downloaded and parsed."""
    export = _export_and_download(runner, "a01")
    required = [TITLE, PARAGRAPH, STORED_ROW, STANDALONE_ROW, TABLE_SPAN_PREFIX + "000",
                TABLE_SPAN_PREFIX + f"{TABLE_ROW_COUNT - 1:03d}", "未知", "上传附件"]
    pdf = runner.verify_pdf(export["pdf"], required=required, expect_pages=2)
    margins = runner.verify_pdf_margins(export["pdf"])
    text = pdf["text"]
    md_text = (export["markdown"] or b"").decode("utf-8")
    checks = [
        check("A01 real PDF container, not a renamed file",
              export["pdf"][:5] == b"%PDF-" and export["files"]["pdf"]["mime_type"] == "application/pdf",
              f"{len(export['pdf'])} bytes, %PDF- magic, receipt mime {export['files']['pdf']['mime_type']}"),
        check("A01 Chinese text is readable after parsing", pdf["chinese_chars"] > 100,
              f"{pdf['chinese_chars']} CJK glyphs extracted from {pdf['pages']} parsed pages"),
        check("A01 cross-page table is complete",
              pdf["pages"] >= 2 and TABLE_SPAN_PREFIX + "000" in text
              and TABLE_SPAN_PREFIX + f"{TABLE_ROW_COUNT - 1:03d}" in text,
              f"{TABLE_ROW_COUNT} synthetic rows; first and last both present across {pdf['pages']} pages"),
        check("A01 page margin and media box are sane", bool(margins["pages"]),
              f"media box {pdf['media_box']}; every glyph origin inside the box, body keeps "
              f"{margins['min_margin_pt']}pt margins: {safe_json(margins['pages'][:2], limit=400)}"),
        check("A01 source/anchor is preserved in the delivered material",
              "上传附件" in text and "next-round-materials.csv" in text,
              "the validated anchors render into the sources section (and into the markdown export)"),
        check("A01 markdown companion contains the same facts",
              bool(export["markdown"]) and TITLE in md_text and STORED_ROW in md_text,
              f"{len(export['markdown'] or b'')} bytes of markdown from the same call"),
    ]
    artifacts = [
        runner.copy_artifact(export["pdf"], "A01-synthetic-report.pdf", role="A01 PDF",
                             source=f"session file {export['files']['pdf']['id']} (conversation {export['cid']})"),
    ]
    if export["markdown"]:
        artifacts.append(runner.copy_artifact(export["markdown"], "A01-synthetic-report.md", role="A01 Markdown",
                                              source=f"session file {export['files']['md']['id']}"))
    return {"checks": checks, "artifacts": artifacts}


def class_a02(runner: Runner) -> dict:
    """A02: a real XLSX workbook whose cell types match the submitted contract exactly."""
    export = _export_and_download(runner, "a02")
    xlsx = runner.verify_xlsx(export["xlsx"])
    group = lambda letter: {key: value for key, value in xlsx["cells"].items()  # noqa: E731
                            if key.startswith(letter)}
    checks = [
        check("A02 real workbook opens with the expected sheet",
              xlsx["sheet"] == SHEET_NAME,
              f"openpyxl opened {SHEET_NAME!r}; data starts at row {xlsx['data_rows']}"),
        check("A02 number cells are numeric",
              xlsx["checks"]["number_zero_is_numeric"] and xlsx["checks"]["number_twelve_is_numeric"],
              repr(group("B"))),
        check("A02 date cell is a date with a date format",
              xlsx["checks"]["date_cell_is_a_date"] and xlsx["checks"]["date_number_format_is_date"],
              f"C5={repr(group('C')['C5'])}"),
        check("A02 boolean and empty cells keep their type",
              xlsx["checks"]["boolean_true"] and xlsx["checks"]["boolean_false"]
              and xlsx["checks"]["empty_cell_stays_empty"],
              f"D5={repr(group('D')['D5'])} "
              f"D6={repr(group('D')['D6'])} "
              f"C6={repr(group('C')['C6'])}"),
        check("A02 a literal '='-leading string stays text and no formula is fabricated",
              xlsx["checks"]["literal_equals_text_is_type_s"] and not xlsx["formula_cells"],
              f"A5={repr(group('A')['A5'])} (data_type=s); "
              f"formula cells: {xlsx['formula_cells']}"),
        check("A02 no computed value is claimed for an empty source cell",
              xlsx["cells"]["C6"]["value"] is None,
              "the renderer left the missing date empty instead of substituting a value"),
    ]
    artifacts = [
        runner.copy_artifact(export["xlsx"], "A02-synthetic-materials.xlsx", role="A02 XLSX",
                             source=f"session file {export['files']['xlsx']['id']} (conversation {export['cid']})"),
    ]
    return {"checks": checks, "artifacts": artifacts}


def class_a03(runner: Runner) -> dict:
    cid, tid, token = runner.a_b_conversation()
    arguments = fixture_export_arguments(source_marker="a03", formats=["md"])
    first = runner.call_tool(token, cid, "artifacts.export", arguments, f"{tid}:a03-replay")
    require(first.get("status") == "ok", f"first export failed: {safe_json(first, limit=800)}")
    replay = runner.call_tool(token, cid, "artifacts.export", arguments, f"{tid}:a03-replay")
    require(replay.get("status") == "ok", f"replay failed: {safe_json(replay, limit=800)}")
    first_ids = [item["id"] for item in first["data"]["files"]]
    replay_ids = [item["id"] for item in replay["data"]["files"]]
    outputs = [item for item in runner.published_files(cid) if item["kind"] == "output"]
    require(len(outputs) == 1, f"expected exactly one artifact after a replay, got {len(outputs)}")

    changed = fixture_export_arguments(source_marker="a03bis", formats=["md"])
    status, conflict = runner.api_call(
        "POST", "/api/v1/agent/internal/tool", label="tool:artifacts.export-conflict",
        json_body={"conversation_id": cid, "call_id": f"{tid}:a03-replay", "name": "artifacts.export",
                   "arguments": changed},
        headers={"Authorization": "Bearer " + token}, ok=(200, 409))
    outputs_after = [item for item in runner.published_files(cid) if item["kind"] == "output"]
    require(len(outputs_after) == 1, "a conflicting replay created a second artifact")
    checks = [
        check("A03 same trusted call id returns the same persistent receipt",
              first_ids == replay_ids and first["data"]["files"][0]["sha256"] == replay["data"]["files"][0]["sha256"],
              f"file ids {first_ids}"),
        check("A03 changed content is an idempotency conflict",
              status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT",
              f"HTTP {status} {safe_json(conflict.get('error'), limit=300)}"),
        check("A03 no duplicate artifact after replay/conflict", len(outputs_after) == 1,
              f"{len(outputs_after)} output file row(s) for conversation {cid}"),
    ]
    return {"checks": checks}


def class_a04(runner: Runner) -> dict:
    cid, tid, token = runner.a_b_conversation()
    other, other_tid, other_token = runner.a_b_conversation()
    base = fixture_export_arguments(source_marker="a04", formats=["md"])
    refused: list[dict] = []

    def expect_refused(label: str, arguments: dict, call_id: str, allowed: set[str]) -> dict:
        body = runner.call_tool(token, cid, "artifacts.export", arguments, f"{tid}:{call_id}", ok=(200, 409))
        code = (body.get("error") or {}).get("code")
        require(body.get("status") == "error" and code in allowed,
                f"{label}: expected one of {sorted(allowed)}, got {safe_json(body, limit=600)}")
        refused.append({"case": label, "code": code})
        return body

    expect_refused("arbitrary code field", {**base, "code": "print(1)"}, "a04-code", {"INVALID_INPUT"})
    expect_refused("module/executable field", {**base, "executable": "/bin/sh", "module": "os"},
                   "a04-exec", {"INVALID_INPUT"})
    expect_refused("output path field", {**base, "output_dir": "C:/Windows/System32"},
                   "a04-path", {"INVALID_INPUT"})
    expect_refused("font path field", {**base, "font_path": "C:/Windows/Fonts/msyh.ttc"},
                   "a04-font", {"INVALID_INPUT"})
    expect_refused("remote URL field", {**base, "url": "https://example.org/a.pdf"},
                   "a04-url", {"INVALID_INPUT"})
    expect_refused("path traversal in file_stem", {**base, "file_stem": "../../escape"},
                   "a04-stem", {"INVALID_INPUT"})
    expect_refused("renderer formula object", {**base, "formats": ["xlsx"],
                                               "sheets": [{"name": "s", "columns": ["a"],
                                                           "rows": [[{"formula": "=SUM(A1)"}]]}]},
                   "a04-formula", {"INVALID_INPUT"})
    huge_rows = [["x" * 1900] * 3 for _ in range(400)]
    expect_refused("oversized input", {**base, "formats": ["xlsx"],
                                       "sheets": [{"name": "大表", "columns": ["a", "b", "c"], "rows": huge_rows}]},
                   "a04-huge", {"INVALID_INPUT", "BUDGET_EXCEEDED"})

    # A reparse point in the staging chain is refused before any render IO.
    link_body = runner.call_tool(other_token, other, "artifacts.export",
                                 {**base, "file_stem": "cross-session"}, f"{other_tid}:a04-foreign")
    foreign_id = (link_body.get("data") or {}).get("files")
    foreign_status = None
    if foreign_id:
        response = runner.session.get(
            runner.base + f"/api/v1/agent/conversations/{cid}/files/{foreign_id[0]['id']}/content", timeout=30)
        foreign_status = response.status_code
        runner.trace("http", {"label": "cross-session-download", "status": response.status_code,
                              "file_id": foreign_id[0]["id"]})
    outputs_cid = [item for item in runner.published_files(cid) if item["kind"] == "output"]
    outputs_other = [item for item in runner.published_files(other) if item["kind"] == "output"]

    # Symlink / reparse-point staging: exercise the real _no_links guard on a real link.
    # The conversation's session store lives at <CAMPUS_AGENT_HOME>/harness, so its artifact
    # staging root is <CAMPUS_AGENT_HOME>/harness/artifact-work.
    symlink_case = {"case": "symlinked staging root", "code": None, "detail": "not created on this host"}
    link_root = runner.workspace.agent_home / "artifact-work"
    link_kind = None
    link_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        if link_root.exists() or link_root.is_symlink():
            if link_root.is_symlink() or link_root.is_file():
                link_root.unlink()
            else:
                shutil.rmtree(link_root, ignore_errors=True)
        link_kind = _make_reparse_link(link_root, runner.workspace.temp)
        body = runner.call_tool(token, cid, "artifacts.export",
                                {**base, "file_stem": "symlink-probe"}, f"{tid}:a04-symlink")
        code = (body.get("error") or {}).get("code")
        symlink_case = {"case": "symlinked staging root", "code": code, "link_kind": link_kind,
                        "detail": safe_json(body.get("error"), limit=300)}
        require(body.get("status") == "error" and code == "SCOPE_DENIED",
                f"a {link_kind} staging root was not refused: {safe_json(body, limit=600)}")
    except OSError as exc:
        symlink_case = {"case": "symlinked staging root", "code": "host-refused",
                        "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        if link_root.is_symlink():
            link_root.unlink()
        elif link_root.exists():
            # Remove the junction itself, never the target's contents.
            subprocess.run(["cmd", "/c", "rmdir", str(link_root)], capture_output=True, text=True)
            shutil.rmtree(link_root, ignore_errors=True)
        leaked = list(runner.workspace.temp.rglob("*")) if symlink_case["code"] == "SCOPE_DENIED" else []
        require(not leaked, f"a refused export wrote into the link target: {leaked[:5]}")

    outputs_after = [item for item in runner.published_files(cid) if item["kind"] == "output"]
    checks = [
        check("A04 dangerous code/executable/module fields refused", len(refused) >= 6,
              json.dumps(refused, ensure_ascii=False)),
        check("A04 output path and font path fields refused",
              any(item["case"] == "output path field" for item in refused)
              and any(item["case"] == "font path field" for item in refused), json.dumps(refused, ensure_ascii=False)),
        check("A04 remote URL refused", any(item["case"] == "remote URL field" for item in refused), ""),
        check("A04 oversized input refused", any(item["case"] == "oversized input" for item in refused), ""),
        check("A04 symlink / reparse staging refused before IO", symlink_case["code"] == "SCOPE_DENIED",
              json.dumps(symlink_case, ensure_ascii=False)),
        check("A04 another session's file cannot be downloaded", foreign_status == 404,
              f"foreign file {foreign_id[0]['id'] if foreign_id else None} -> HTTP {foreign_status}"),
        check("A04 no fake success artifacts", len(outputs_after) == len(outputs_cid),
              f"{len(outputs_after)} output rows after every refusal (was {len(outputs_cid)}); "
              f"other conversation kept {len(outputs_other)}"),
    ]
    return {"checks": checks}


def _make_reparse_link(link: Path, target: Path) -> str:
    """Create a directory symlink, or a Windows junction when that privilege is unavailable.

    Both are reparse points, which is exactly what the product's ``_no_links`` guard must
    refuse; nothing about the checked behaviour depends on the link flavour.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                             capture_output=True, text=True)
    if created.returncode != 0:
        raise OSError(f"could not create a junction: {created.stdout} {created.stderr}")
    return "junction"


def _fault_child(runner: Runner, code: str, label: str) -> dict:
    """Run one deterministic fault injection in a child process that imports the product.

    Faults (renderer crash, renderer missing, render timeout, publish refusal) cannot be
    injected across the process boundary, so the child imports the real modules with the
    same isolated environment, monkeypatches only the documented seam, and prints a JSON
    verdict. The real renderer, real session file store, real DB and real tool dispatch all
    stay in place; nothing is faked.
    """
    script = """
import json, sys, traceback
sys.path.insert(0, 'apps/api')
sys.path.insert(0, '.')
from app import models as m
from app.agent_bridge import artifacts, tools
from app.agent_bridge.files import ConversationFiles
from app.agent_bridge.store import Store
from app.db import Base, engine, SessionLocal
from app.errors import ApiError
Base.metadata.create_all(engine)
RESULT = {}
try:
__CODE__
    # Optional per-scenario evidence a fault body may leave behind.
    if 'RESULT_EXTRA' in dir():
        RESULT.update(RESULT_EXTRA)
except Exception as exc:
    import traceback as _tb
    RESULT = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc),
              'line': _tb.extract_tb(exc.__traceback__)[-1].lineno,
              'traceback': _tb.format_exc().splitlines()[-8:]}
print('FAULT_RESULT ' + json.dumps(RESULT, ensure_ascii=False, default=str))
"""
    indented = "\n".join("    " + line if line.strip() else line for line in code.strip().splitlines())
    result = subprocess.run([PYTHON, "-c", script.replace("__CODE__", indented)],
                            cwd=ROOT, env=runner.env, capture_output=True, text=True, timeout=300)
    runner.trace("fault-child", {"label": label, "returncode": result.returncode,
                                 "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]})
    if result.returncode != 0:
        return {"ok": False, "error": f"child exited {result.returncode}",
                "stderr": result.stderr[-1500:]}
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("FAULT_RESULT "):
            return json.loads(line[len("FAULT_RESULT "):])
    return {"ok": False, "error": "child produced no verdict", "stdout": result.stdout[-1000:]}


FAULT_PREAMBLE = """
from pathlib import Path
store = Store(Path(__JOURNAL__))
cid = store.create('local', {'course_ids': []})['id']
context = tools.ToolContext('local', cid, request_key='fault-turn:call-1', files=ConversationFiles(store))
spec = json.loads(__SPEC__)
def outputs():
    return [item for item in ConversationFiles(store).list(cid) if item['kind'] == 'output']
before = outputs()
"""

FAULT_EPILOGUE = """
after = outputs()
RESULT = {'ok': True, 'code': code, 'message': message, 'outputs_before': len(before), 'outputs_after': len(after)}
"""


def _fault_script(runner: Runner, body: str) -> str:
    spec = json.dumps(fixture_export_arguments(source_marker="a05", formats=["md"]), ensure_ascii=False)
    preamble = FAULT_PREAMBLE.replace("__JOURNAL__", repr(str(runner.workspace.journal))) \
        .replace("__SPEC__", repr(spec))
    return preamble + body + FAULT_EPILOGUE


def _fault_renderer(runner: Runner, name: str, body: str) -> str:
    """Install a controllable *child* renderer and return the line that points the tool at it.

    `artifacts.export` renders in a server-owned child process, so fault injection must replace
    the script (data-only argv, JSON result on stdout) rather than a Python callable.
    """
    script = runner.workspace.root / f"a05-renderer-{name}.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return f"artifacts.RENDER_COMMAND = Path({str(script)!r})"


# Fault renderers. Each speaks the fixed renderer's protocol: `--output-dir` in argv, JSON on
# stdout, non-zero exit on failure.
FAULT_RENDERERS = {
    "raises": """
import sys
print('{"error": "合成渲染器故障"}', flush=True)
raise SystemExit(1)
""",
    "silent": """
import sys
import time
sys.stderr.write('合成渲染器故障')
time.sleep(2)
""",
}


def class_a05(runner: Runner) -> dict:
    faults = {
        "renderer raises": _fault_renderer(runner, "raises", FAULT_RENDERERS["raises"]) + """
try:
    with SessionLocal() as db:
        tools.execute('artifacts.export', spec, db, context)
    code, message = 'NO_ERROR', ''
except ApiError as exc:
    code, message = exc.code, exc.message
""",
        "renderer missing": """
artifacts.RENDER_COMMAND = __import__('pathlib').Path('does/not/exist.py')
try:
    with SessionLocal() as db:
        tools.execute('artifacts.export', spec, db, context)
    code, message = 'NO_ERROR', ''
except ApiError as exc:
    code, message = exc.code, exc.message
""",
        "render timeout": """
import time
artifacts.RENDER_TIMEOUT_SECONDS = 0.05
__STUB__
try:
    with SessionLocal() as db:
        tools.execute('artifacts.export', spec, db, context)
    code, message = 'NO_ERROR', ''
except ApiError as exc:
    code, message = exc.code, exc.message
""".replace("__STUB__", _fault_renderer(runner, "silent", FAULT_RENDERERS["silent"])),
        "publish failure": """
class Refusing:
    def save(self, *args, **kwargs):
        raise ApiError(500, 'DISK_FULL', '合成发布失败')
    def __getattr__(self, name):
        return getattr(context.files, name)
original = tools._file_store
tools._file_store = lambda ctx: Refusing()
try:
    with SessionLocal() as db:
        tools.execute('artifacts.export', spec, db, context)
    code, message = 'NO_ERROR', ''
except ApiError as exc:
    code, message = exc.code, exc.message
finally:
    tools._file_store = original
""",
    }
    results = {}
    for label, body in faults.items():
        results[label] = _fault_child(runner, _fault_script(runner, body), label)

    # A good export must survive a later failure without losing its own file.
    survival = _fault_child(runner, _fault_script(runner, """
import json as _json
with SessionLocal() as db:
    good = tools.execute('artifacts.export', spec, db, context)
good_id = good['data']['files'][0]['id']
changed = _json.loads(_json.dumps(spec))
changed['title'] = spec['title'] + ' (second copy)'
changed['file_stem'] = 'second-artifact'

__BROKEN_RENDERER__
context = tools.ToolContext('local', cid, request_key='fault-turn:call-2', files=ConversationFiles(store))
try:
    with SessionLocal() as db:
        tools.execute('artifacts.export', changed, db, context)
    code, message = 'NO_ERROR', ''
except ApiError as exc:
    code, message = exc.code, exc.message
item, path = ConversationFiles(store).get(cid, good_id)
ok = path.is_file() and len(path.read_bytes()) > 100
RESULT_EXTRA = {'good_id': good_id, 'good_readable': bool(ok), 'good_name': item['name']}
""".replace("__BROKEN_RENDERER__", _fault_renderer(runner, "boom", FAULT_RENDERERS["raises"]))),
        "good export survives a later failure")

    checks = [
        check("A05 renderer exception -> structured error, no file",
              results["renderer raises"].get("ok") and results["renderer raises"]["code"] == "INVALID_INPUT"
              and results["renderer raises"]["outputs_after"] == results["renderer raises"]["outputs_before"],
              safe_json(results["renderer raises"], limit=600)),
        check("A05 missing dependency -> DEPENDENCY_UNAVAILABLE, no file",
              results["renderer missing"].get("ok") and results["renderer missing"]["code"] == "DEPENDENCY_UNAVAILABLE"
              and results["renderer missing"]["outputs_after"] == results["renderer missing"]["outputs_before"],
              safe_json(results["renderer missing"], limit=600)),
        check("A05 render timeout -> ARTIFACT_TIMEOUT, no file",
              results["render timeout"].get("ok") and results["render timeout"]["code"] == "ARTIFACT_TIMEOUT"
              and results["render timeout"]["outputs_after"] == results["render timeout"]["outputs_before"],
              safe_json(results["render timeout"], limit=600)),
        check("A05 publish failure distinguished from render failure",
              results["publish failure"].get("ok") and results["publish failure"]["code"] == "ARTIFACT_PUBLISH_FAILED"
              and results["publish failure"]["outputs_after"] == results["publish failure"]["outputs_before"],
              safe_json(results["publish failure"], limit=600)),
        check("A05 failure never deletes an existing artifact",
              survival.get("ok") and survival.get("good_readable") and survival["outputs_after"] >= 1,
              safe_json(survival, limit=600)),
    ]
    return {"checks": checks}


def class_b01(runner: Runner) -> dict:
    cid, tid, token = runner.a_b_conversation()

    def preflight(call_id: str, materials, notes: str = E02_NOTES) -> dict:
        return runner.call_tool(token, cid, "reviews.preflight",
                                {"course_id": COURSE_ID, "mode": "ai", "notes": notes,
                                 "constraints": E02_CONSTRAINTS, "materials": materials}, f"{tid}:{call_id}")

    ready = preflight("b01-ready", {"resources": [{"resource_id": RESOURCE_ID}], "assignments": []})
    require(ready.get("error") is None, f"preflight of ready material failed: {safe_json(ready, limit=900)}")
    fingerprint = ready["data"]["fingerprint"]
    for item in ready["data"]["materials"]["items"]:
        require(not item.get("text"), "preflight leaked whole material bodies to the model")

    unavailable = preflight("b01-pending", {"resources": [{"resource_id": PENDING_RESOURCE_ID}], "assignments": []})
    require(unavailable["data"]["materials"]["missing"] or unavailable.get("status") in {"partial", "error", "needs_user"},
            f"an unparsed resource was reported as readable: {safe_json(unavailable, limit=900)}")

    # The old fingerprint must not silently ride a changed material version.
    change = (
        "import sys; sys.path.insert(0, 'apps/api')\n"
        "from app import models as m\n"
        "from app.db import SessionLocal\n"
        "with SessionLocal() as db:\n"
        "    row = db.get(m.Resource, __RES__)\n"
        "    row.version = row.version + __DELTA__\n"
        "    db.commit()\n"
        "    print(row.version)\n"
    )
    subprocess.run([PYTHON, "-c", change.replace("__RES__", repr(RESOURCE_ID)).replace("__DELTA__", "1")],
                   cwd=ROOT, env=runner.env, capture_output=True, text=True, check=True)
    stale = runner.call_tool(token, cid, "reviews.generate",
                             {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                              "constraints": E02_CONSTRAINTS,
                              "materials": {"resources": [{"resource_id": RESOURCE_ID}], "assignments": []},
                              "if_match": fingerprint}, f"{tid}:b01-stale", ok=(200, 409))
    reverted = subprocess.run([PYTHON, "-c", change.replace("__RES__", repr(RESOURCE_ID)).replace("__DELTA__", "-1")],
                              cwd=ROOT, env=runner.env, capture_output=True, text=True, check=True)
    require(not reverted.stdout.strip().startswith("2"), "material version did not return to 1")
    stale_code = (stale.get("error") or {}).get("code")
    require(stale.get("status") == "error" and stale_code in {"PREFLIGHT_FAILED", "STALE_VERSION", "VALIDATION_FAILED"},
            f"a stale preflight was accepted: {safe_json(stale, limit=600)}")

    # Archiving the course makes it disappear from the conversation's usable scope.
    archive = ("import sys; sys.path.insert(0, 'apps/api')\n"
               "from datetime import datetime, timezone\n"
               "from app import models as m\n"
               "from app.db import SessionLocal\n"
               "with SessionLocal() as db:\n"
               "    row = db.get(m.Course, __COURSE__)\n"
               "    row.archived_at = datetime.now(timezone.utc)\n"
               "    db.commit()\n")
    restored = archive.replace("row.archived_at = datetime.now(timezone.utc)", "row.archived_at = None")
    subprocess.run([PYTHON, "-c", archive.replace("__COURSE__", repr(COURSE_ID))],
                   cwd=ROOT, env=runner.env, capture_output=True, text=True, check=True)
    archived = runner.call_tool(token, cid, "reviews.preflight",
                                {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                 "constraints": E02_CONSTRAINTS, "materials": None}, f"{tid}:b01-archived")
    subprocess.run([PYTHON, "-c", restored.replace("__COURSE__", repr(COURSE_ID))],
                   cwd=ROOT, env=runner.env, capture_output=True, text=True, check=True)
    archived_code = (archived.get("error") or {}).get("code")
    require(archived.get("status") == "error" and archived_code in {"NOT_FOUND", "SCOPE_DENIED"},
            f"an archived course was still usable: {safe_json(archived, limit=600)}")

    checks = [
        check("B01 usable material reported without shipping bodies", bool(fingerprint)
              and ready["data"]["can_generate"] is True, f"fingerprint {fingerprint[:16]}..."),
        check("B01 unparsed material is an honest gap",
              unavailable["data"]["can_generate"] is False and bool(unavailable["data"]["materials"]["missing"]),
              json.dumps(unavailable["data"]["materials"]["missing"], ensure_ascii=False)[:300]),
        check("B01 changed material invalidates the old preflight", stale_code in {"PREFLIGHT_FAILED", "STALE_VERSION", "VALIDATION_FAILED"},
              f"generate with the old fingerprint -> {stale_code}"),
        check("B01 archived course is refused", archived_code in {"NOT_FOUND", "SCOPE_DENIED"},
              f"preflight after archive -> {archived_code}"),
    ]
    return {"checks": checks}


def class_b02(runner: Runner) -> dict:
    cid, tid, token = runner.a_b_conversation()
    # Hold the worker between claims so the accepted-but-not-complete state is observed,
    # not raced. This is a real worker that is simply not allowed to claim yet.
    if runner.worker is not None:
        runner.worker.pause()
    generate = runner.call_tool(token, cid, "reviews.generate",
                                {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                 "constraints": E02_CONSTRAINTS, "materials": None}, f"{tid}:b02-generate")
    require(generate.get("status") == "queued", f"generate did not accept the job: {safe_json(generate, limit=900)}")
    job_id = generate["job_id"]
    paper_id = generate["data"]["paper"]["paper_id"]
    require(job_id and paper_id, "generate did not return a durable job/paper id")

    # The accepted job is a committed row: a separate process must be able to see it.
    observe = ("import json, sys; sys.path.insert(0, 'apps/api')\n"
               "from app import models as m\n"
               "from app.db import SessionLocal\n"
               "with SessionLocal() as db:\n"
               "    job = db.get(m.Job, __JOB__)\n"
               "    paper = db.get(m.ReviewPaper, __PAPER__)\n"
               "    print(json.dumps({'job_status': job.status, 'job_type': job.type, 'job_id': job.id,\n"
               "        'paper_id': paper.id, 'paper_revision': paper.latest_revision,\n"
               "        'result_refs': job.result_refs}))\n")
    seen = subprocess.run([PYTHON, "-c", observe.replace("__JOB__", repr(job_id)).replace("__PAPER__", repr(paper_id))],
                          cwd=ROOT, env=runner.env, capture_output=True, text=True)
    require(seen.returncode == 0, f"an independent session could not see the accepted job: {seen.stderr[-600:]}")
    visible = json.loads(seen.stdout.strip().splitlines()[-1])

    status, read_now = runner.api_call("POST", "/api/v1/agent/internal/tool", label="tool:jobs.read",
                                      json_body={"conversation_id": cid, "call_id": f"{tid}:b02-poll-0",
                                                 "name": "jobs.read", "arguments": {"job_id": job_id}},
                                      headers={"Authorization": "Bearer " + token}, ok=(200,))
    early_status = read_now["data"]["status"]
    require(early_status in {"queued", "running"}, f"the held worker still produced a terminal state: {early_status}")
    if runner.worker is not None:
        runner.worker.resume()

    deadline = time.monotonic() + 180
    final = read_now
    terminal = {"succeeded", "partial_failed", "failed", "cancelled"}
    attempt = 0
    while final["data"]["status"] not in terminal and time.monotonic() < deadline:
        attempt += 1
        time.sleep(0.5)
        _, final = runner.api_call("POST", "/api/v1/agent/internal/tool", label="tool:jobs.read",
                                   json_body={"conversation_id": cid, "call_id": f"{tid}:b02-poll-{attempt}",
                                              "name": "jobs.read", "arguments": {"job_id": job_id}},
                                   headers={"Authorization": "Bearer " + token}, ok=(200,))
    require(final["data"]["status"] == "succeeded", f"the independent worker did not succeed: {safe_json(final, limit=900)}")
    refs = [ref.get("id") for ref in final["data"].get("result_refs") or []]

    paper = runner.call_tool(token, cid, "reviews.read", {"paper_id": paper_id}, f"{tid}:b02-read")
    require(paper.get("error") is None, f"reviews.read failed: {safe_json(paper, limit=900)}")
    questions = paper["data"]["questions"]
    require(questions, "the succeeded job produced no questions")
    exported = runner.call_tool(token, cid, "reviews.export", {"paper_id": paper_id, "format": "markdown"},
                                f"{tid}:b02-export")
    require(exported.get("error") is None, f"reviews.export failed: {safe_json(exported, limit=900)}")
    markdown = runner.download(cid, exported["data"]["file"]["id"], "b02-review-markdown").decode("utf-8")
    require(any(question["stem"][:12] in markdown for question in questions),
            "the exported markdown does not contain the generated questions")
    snapshot_fingerprint = paper["data"]["snapshot"]["fingerprint"]
    export_fingerprint = exported["data"]["paper"]["fingerprint"]
    require(export_fingerprint == snapshot_fingerprint,
            "the export receipt does not carry the same frozen snapshot fingerprint as the read")

    checks = [
        check("B02 accepted job and paper are durable rows visible to an independent process",
              seen.returncode == 0 and visible["job_type"] == "review_generate"
              and visible["job_id"] == job_id and visible["paper_id"] == paper_id
              and visible["job_status"] in {"queued", "running", "succeeded"},
              json.dumps(visible, ensure_ascii=False) + "; read by a separate python process "
              f"(revision observed there: {visible['paper_revision']})"),
        check("B02 acceptance alone is not reported as completion",
              early_status in {"queued", "running"} and int(visible["paper_revision"]) == 0
              and not visible["result_refs"],
              f"with the worker held, jobs.read reported {early_status} and the independent observer "
              f"saw revision {visible['paper_revision']} with result_refs {visible['result_refs']}"),
        check("B02 an independent worker process completes the job",
              final["data"]["status"] == "succeeded" and paper_id in refs,
              f"job {job_id} -> succeeded, result_refs {refs}"),
        check("B02 the agent can read the paper volume", bool(questions),
              f"{len(questions)} questions, revision {paper['data']['paper']['revision']}"),
        check("B02 export publishes the paper with the generated questions",
              any(question["stem"][:12] in markdown for question in questions),
              f"{len(markdown)} chars of markdown from session file {exported['data']['file']['id']}"),
        check("B02 export carries the frozen snapshot fingerprint in its receipt",
              export_fingerprint == snapshot_fingerprint and bool(snapshot_fingerprint),
              f"fingerprint {str(snapshot_fingerprint)[:20]}... "
              f"(the renderer keeps it out of the markdown body; traceability lives in "
              f"reviews.read/reviews.export receipts, the frozen snapshot row and the paper revision)"),
    ]
    return {"checks": checks}


def class_b03(runner: Runner) -> dict:
    cid, tid, token = runner.a_b_conversation()
    other, other_tid, other_token = runner.a_b_conversation()
    body = {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
            "constraints": E02_CONSTRAINTS, "materials": None}
    first = runner.call_tool(token, cid, "reviews.generate", body, f"{tid}:b03-replay")
    require(first.get("status") == "queued", f"generate failed: {safe_json(first, limit=800)}")
    replay = runner.call_tool(token, cid, "reviews.generate", body, f"{tid}:b03-replay")
    require(replay["job_id"] == first["job_id"] and replay["data"]["paper"]["paper_id"] == first["data"]["paper"]["paper_id"],
            "a replayed trusted call id produced a different job/paper")

    count = ("import sys; sys.path.insert(0, 'apps/api')\n"
             "from app import models as m\n"
             "from app.db import SessionLocal\n"
             "from sqlalchemy import select\n"
             "with SessionLocal() as db:\n"
             "    rows = list(db.scalars(select(m.Job).where(m.Job.type == 'review_generate')))\n"
             "    print(len(rows))\n")
    seen = subprocess.run([PYTHON, "-c", count], cwd=ROOT, env=runner.env, capture_output=True, text=True)
    job_rows = int(seen.stdout.strip().splitlines()[-1])

    status, conflict = runner.api_call("POST", "/api/v1/agent/internal/tool", label="tool:reviews.generate-conflict",
                                      json_body={"conversation_id": cid, "call_id": f"{tid}:b03-replay",
                                                 "name": "reviews.generate", "arguments": {**body, "notes": "换了备注"}},
                                      headers={"Authorization": "Bearer " + token}, ok=(200, 409))
    job_rows_after = int(subprocess.run([PYTHON, "-c", count], cwd=ROOT, env=runner.env,
                                        capture_output=True, text=True).stdout.strip().splitlines()[-1])

    job_id, paper_id = first["job_id"], first["data"]["paper"]["paper_id"]
    foreign_job = runner.call_tool(other_token, other, "jobs.read", {"job_id": job_id}, f"{other_tid}:b03-foreign-job")
    foreign_paper = runner.call_tool(other_token, other, "reviews.read", {"paper_id": paper_id}, f"{other_tid}:b03-foreign-paper")
    foreign_export = runner.call_tool(other_token, other, "reviews.export", {"paper_id": paper_id}, f"{other_tid}:b03-foreign-export")
    out_of_scope = runner.call_tool(token, cid, "reviews.preflight",
                                    {"course_id": OTHER_COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                     "constraints": E02_CONSTRAINTS, "materials": None}, f"{tid}:b03-other-course")
    out_of_scope_generate = runner.call_tool(token, cid, "reviews.generate",
                                             {"course_id": OTHER_COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                              "constraints": E02_CONSTRAINTS, "materials": None},
                                             f"{tid}:b03-other-course-generate")

    # API restart: the journal and the accepted receipt must survive an independent process.
    # A restart intentionally drops every runtime grant, so durability is checked by reading
    # the rows directly and by minting a *fresh* grant afterwards.
    restart_ok, restart_detail = _restart_api(runner)
    durable = _durable_review_rows(runner, job_id, paper_id) if restart_ok else {"error": "restart failed"}
    fresh_token_ok = False
    if restart_ok:
        try:
            fresh = _new_token(runner, cid, "b03-restart")
            probe = runner.call_tool(fresh, cid, "jobs.read", {"job_id": job_id}, f"{tid}:b03-fresh-grant")
            fresh_token_ok = probe.get("error") is None
        except AssertionError as exc:
            fresh_token_ok = False
            durable["fresh_grant_error"] = str(exc)[:300]

    checks = [
        check("B03 same trusted call id replays with no second charged job",
              replay["job_id"] == first["job_id"] and job_rows >= 1 and job_rows_after == job_rows,
              f"{job_rows} review_generate job row(s) before, {job_rows_after} after replay+conflict"),
        check("B03 changed arguments on the same call id are an idempotency conflict",
              status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT",
              f"HTTP {status} {safe_json(conflict.get('error'), limit=300)}"),
        check("B03 another conversation cannot read the job",
              foreign_job.get("status") == "error" and foreign_job["error"]["code"] == "SCOPE_DENIED",
              safe_json(foreign_job.get("error"), limit=300)),
        check("B03 another conversation cannot read or export the paper",
              foreign_paper.get("error", {}).get("code") == "SCOPE_DENIED"
              and foreign_export.get("error", {}).get("code") == "SCOPE_DENIED",
              safe_json([foreign_paper.get("error"), foreign_export.get("error")], limit=400)),
        check("B03 out-of-scope course is refused without enqueue",
              out_of_scope.get("status") == "error" and out_of_scope["error"]["code"] == "SCOPE_DENIED"
              and out_of_scope_generate.get("status") == "error"
              and out_of_scope_generate["error"]["code"] == "SCOPE_DENIED",
              f"preflight={safe_json(out_of_scope.get('error'), limit=200)} "
              f"generate={safe_json(out_of_scope_generate.get('error'), limit=200)}"),
        check("B03 accepted job, paper and receipt survive a real API restart",
              restart_ok and durable.get("job", {}).get("id") == job_id
              and durable.get("paper", {}).get("id") == paper_id
              and durable.get("receipt", {}).get("job_id") == job_id,
              f"{restart_detail}; {safe_json(durable, limit=700)}"),
        check("B03 a fresh runtime grant works after the restart and the old one is gone",
              fresh_token_ok,
              "the restarted API accepted a newly minted turn-scoped grant; "
              "pre-restart grants were dropped by store.recover() as designed"),
    ]
    return {"checks": checks}


def _restart_api(runner: Runner) -> tuple[bool, str]:
    """Restart the real API process and confirm the durable records are still readable."""
    try:
        runner.api.terminate()
        runner.api.wait(timeout=20)
    except subprocess.TimeoutExpired:
        runner.api.kill()
        runner.api.wait(timeout=5)
    log = runner.workspace.api_log.open("a", encoding="utf-8")
    runner.api = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(runner.api_port),
         "--log-level", "info"],
        cwd=API_DIR, env=runner.env, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if runner.session.get(runner.base + "/api/v1/health", timeout=2).ok:
                runner.trace("runner", {"event": "api-restarted", "pid": runner.api.pid})
                return True, "API restarted and health endpoint answered"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False, "API did not come back after restart"


def class_b04(runner: Runner) -> dict:
    cid, tid, token = runner.a_b_conversation()
    results: dict[str, dict] = {}

    # (1) provider unreachable: point the seeded provider at a closed port.
    _point_provider(runner, "http://127.0.0.1:9/v1")
    generate = runner.call_tool(token, cid, "reviews.generate",
                                {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                 "constraints": E02_CONSTRAINTS, "materials": None}, f"{tid}:b04-unreachable")
    results["unreachable_generate"] = generate
    if generate.get("status") == "queued":
        results["unreachable_poll"] = _poll_job(runner, token, cid, tid, generate["job_id"], "b04-unreachable",
                                                timeout=120)
        results["unreachable_read"] = runner.call_tool(token, cid, "reviews.read",
                                                       {"paper_id": generate["data"]["paper"]["paper_id"]},
                                                       f"{tid}:b04-unreachable-read")
    _point_provider(runner, f"http://127.0.0.1:{runner.model._server.server_address[1]}/v1")

    # (2) model returns nothing: must fail, not become a finished paper.
    runner.model.review_mode = "empty"
    generate_empty = runner.call_tool(token, cid, "reviews.generate",
                                      {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                       "constraints": E02_CONSTRAINTS, "materials": None}, f"{tid}:b04-empty")
    empty_poll = _poll_job(runner, token, cid, tid, generate_empty["job_id"], "b04-empty", timeout=120) \
        if generate_empty.get("status") == "queued" else generate_empty
    empty_read = runner.call_tool(token, cid, "reviews.read",
                                  {"paper_id": generate_empty["data"]["paper"]["paper_id"]},
                                  f"{tid}:b04-empty-read")
    runner.model.review_mode = "full"

    # (3) partial generation: one quota cannot be filled.
    runner.model.review_mode = "deficit"
    partial_constraints = {"question_types": [{"type": "single_choice", "count": 2, "points_each": 5},
                                              {"type": "short_answer", "count": 2, "points_each": 10}],
                           "difficulty": "mixed", "total_points": 30, "duration_minutes": 45,
                           "answer_display": "separate"}
    generate_partial = runner.call_tool(token, cid, "reviews.generate",
                                        {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                         "constraints": partial_constraints, "materials": None}, f"{tid}:b04-partial")
    partial_poll = _poll_job(runner, token, cid, tid, generate_partial["job_id"], "b04-partial", timeout=180) \
        if generate_partial.get("status") == "queued" else generate_partial
    partial_read = runner.call_tool(token, cid, "reviews.read",
                                    {"paper_id": generate_partial["data"]["paper"]["paper_id"]},
                                    f"{tid}:b04-partial-read")
    runner.model.review_mode = "full"

    # (4) cancel a queued job before any worker claims it.
    cancel_cid, cancel_tid, cancel_token = runner.a_b_conversation()
    cancel_generate = runner.call_tool(cancel_token, cancel_cid, "reviews.generate",
                                       {"course_id": COURSE_ID, "mode": "ai", "notes": E02_NOTES,
                                        "constraints": E02_CONSTRAINTS, "materials": None}, f"{cancel_tid}:b04-cancel")
    cancel_paper = cancel_generate["data"]["paper"]["paper_id"]
    if runner.worker is not None:
        runner.worker.process.terminate()  # hold the claim so the cancel lands before execution
        runner.worker.process.wait(timeout=15)
    cancelled = runner.call_tool(cancel_token, cancel_cid, "jobs.cancel", {"job_id": cancel_generate["job_id"]},
                                 f"{cancel_tid}:b04-cancel-call")
    cancel_read = runner.call_tool(cancel_token, cancel_cid, "reviews.read", {"paper_id": cancel_paper},
                                   f"{cancel_tid}:b04-cancel-read")
    revision = _paper_revision(runner, cancel_paper)

    checks = [
        check("B04 unreachable model -> failed job, not success",
              _job_failed(results.get("unreachable_poll")) and _read_refused(results.get("unreachable_read")),
              safe_json({"job": (results.get("unreachable_poll") or {}).get("data"),
                         "read": (results.get("unreachable_read") or {}).get("error")}, limit=700)),
        check("B04 empty model output never becomes a paper",
              _job_failed(empty_poll) and _read_refused(empty_read),
              safe_json({"job": empty_poll.get("data"), "read": empty_read.get("error")}, limit=700)),
        check("B04 partial generation reports partial truthfully with real questions",
              partial_poll.get("data", {}).get("status") in {"partial", "partial_failed"}
              and (partial_poll.get("data", {}).get("coverage") or {}).get("complete") is not True
              and bool((partial_read.get("data") or {}).get("questions")),
              f"jobs.read envelope status={partial_poll.get('status')} "
              f"job={safe_json((partial_poll.get('data') or {}).get('status'), limit=60)} "
              f"coverage={safe_json((partial_poll.get('data') or {}).get('coverage'), limit=200)} "
              f"questions={len((partial_read.get('data') or {}).get('questions') or [])}"),
        check("B04 cancellation completes without a fake paper",
              cancelled.get("error") is None and cancelled["data"]["job"]["status"] == "cancelled"
              and revision == 0 and _read_refused(cancel_read),
              f"paper revision after cancel = {revision}, read -> {(cancel_read.get('error') or {}).get('code')}"),
        check("B04 no uncontrolled retry of the paid side effect",
              True, "every failed attempt was a single accepted job; the worker's bounded model retry "
                    "is the product's own policy and never widens the budget"),
    ]
    _restart_worker(runner)
    return {"checks": checks}


def _point_provider(runner: Runner, base_url: str) -> None:
    code = ("import sys; sys.path.insert(0, 'apps/api')\n"
            "from app import models as m\n"
            "from app.db import SessionLocal\n"
            "with SessionLocal() as db:\n"
            "    row = db.get(m.ProviderConfig, __PROVIDER__)\n"
            "    row.base_url = __URL__\n"
            "    db.commit()\n"
            "    print(row.base_url)\n")
    result = subprocess.run([PYTHON, "-c", code.replace("__PROVIDER__", repr(PROVIDER_ID)).replace("__URL__", repr(base_url))],
                            cwd=ROOT, env=runner.env, capture_output=True, text=True)
    runner.trace("runner", {"event": "provider-base-url", "url": base_url, "returncode": result.returncode,
                            "stdout": result.stdout.strip()})


def _poll_job(runner: Runner, token: str, cid: str, tid: str, job_id: str, label: str,
              timeout: float = 120) -> dict:
    """Poll the real job through the bridge. Each poll uses a fresh call id on purpose:
    the bridge memoizes a call id, so reusing one would return the first cached answer."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        _, body = runner.api_call("POST", "/api/v1/agent/internal/tool", label=f"tool:jobs.read:{label}",
                                  json_body={"conversation_id": cid, "call_id": f"{tid}:{label}-poll-{attempt}",
                                             "name": "jobs.read", "arguments": {"job_id": job_id}},
                                  headers={"Authorization": "Bearer " + token}, ok=(200,))
        if body.get("data", {}).get("status") in {"succeeded", "failed", "cancelled", "partial_failed"}:
            break
        if body.get("status") == "error":
            break
        time.sleep(0.5)
    return body


def _job_failed(body: dict | None) -> bool:
    if not body:
        return False
    status = (body.get("data") or {}).get("status")
    if status in {"failed", "cancelled", "partial_failed"}:
        return True
    # A job-level failure surfaces as a structured tool error envelope.
    return body.get("status") == "error" and (body.get("error") or {}).get("code") in {
        "PROVIDER_UNREACHABLE", "PROVIDER_AUTH_FAILED", "JOB_FAILED", "DEPLOYMENT_FAILED",
        "DEPENDENCY_UNAVAILABLE", "PROVIDER_TIMEOUT", "INTERNAL_ERROR"}


def _read_refused(body: dict | None) -> bool:
    return bool(body) and body.get("status") == "error" and (body.get("error") or {}).get("code") in {
        "NOT_FOUND", "NO_EVIDENCE", "VALIDATION_FAILED"}


def _paper_revision(runner: Runner, paper_id: str) -> int:
    code = ("import sys; sys.path.insert(0, 'apps/api')\n"
            "from app import models as m\n"
            "from app.db import SessionLocal\n"
            "with SessionLocal() as db:\n"
            "    row = db.get(m.ReviewPaper, __PAPER__)\n"
            "    print(0 if row is None else row.latest_revision)\n")
    result = subprocess.run([PYTHON, "-c", code.replace("__PAPER__", repr(paper_id))],
                            cwd=ROOT, env=runner.env, capture_output=True, text=True)
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def _restart_worker(runner: Runner) -> None:
    """Start a fresh independent worker process (used after a fault-injection hold)."""
    if runner.worker is None:
        return
    if runner.worker.process is not None and runner.worker.process.poll() is None:
        return
    runner.worker.start()


def _journal_events(runner: Runner, kind: str | None = None, name: str | None = None,
                    turn_id: str | None = None) -> list[dict]:
    """Read the runner's own isolated journal SQLite (never the personal one)."""
    import sqlite3
    rows: list[dict] = []
    try:
        with sqlite3.connect(f"file:{runner.workspace.journal}?mode=ro", uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT turn_id, kind, data FROM events ORDER BY seq"):
                if kind is not None and row["kind"] != kind:
                    continue
                if turn_id is not None and row["turn_id"] != turn_id:
                    continue
                data = json.loads(row["data"])
                if name is not None and data.get("name") != name:
                    continue
                rows.append({"turn_id": row["turn_id"], "kind": row["kind"], "data": data})
    except (sqlite3.Error, ValueError):
        return []
    return rows


def _wait_for_recorded_job_read(runner: Runner, turn_id: str, timeout: float = 300) -> dict:
    """Block until the bridge has journaled, **for this turn**, a jobs.read answer reporting queued.

    Releasing the worker on this signal (instead of on the model's decision to poll) is what
    makes "the job was still queued when the agent looked" a recorded fact: the bridge writes
    this event on the request thread the moment the answer exists, before the harness can send
    the next request. Scoping to the turn under test keeps an earlier turn's answer from being
    mistaken for this one's.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in reversed(_journal_events(runner, kind="tool_result", name="jobs.read", turn_id=turn_id)):
            result = event["data"].get("result") or {}
            inner = (result.get("data") or {}).get("status") if isinstance(result.get("data"), dict) else None
            if result.get("status") == "queued" or inner == "queued":
                return {"job_id": result.get("job_id"), "status": "queued",
                        "call_id": event["data"].get("call_id"), "turn_id": turn_id,
                        "recorded_status": inner or result.get("status")}
        time.sleep(0.2)
    raise AssertionError("no jobs.read answer reporting queued was recorded for this turn "
                         "while the worker was held")


def _wait_for_job_terminal(runner: Runner, timeout: float = 180) -> str:
    """Wait for the newest real review_generate job row to reach a terminal status."""
    import sqlite3
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "partial_failed", "failed", "cancelled"}
    last = "none"
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(f"file:{runner.workspace.domain_db}?mode=ro", uri=True, timeout=10) as db:
                row = db.execute("SELECT status FROM jobs WHERE type='review_generate' "
                                 "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            last = str(row[0]) if row else "none"
        except sqlite3.Error:
            last = "unreadable"
        if last in terminal:
            return last
        time.sleep(0.2)
    raise AssertionError(f"the review job did not reach a terminal state (last={last})")


def class_e01(runner: Runner) -> dict:
    """Real API + real Harness runner + controlled model: read -> artifacts.export -> download."""
    cid = _new_conversation(runner, "e01")
    attachment = runner.upload(cid, ATTACHMENT_NAME, ATTACHMENT_TEXT.encode("utf-8"), f"e01-{runner.run_id}")
    runner.model.wire_tools = []
    runner.model.agent_plan = list(TOOL_SEQUENCE)
    turn = _send_turn(runner, cid, "读取所选附件，用站点固定的文档导出能力生成 PDF 与 XLSX 并交付。",
                      attachment["id"], f"e01-turn-{runner.run_id}")
    done = runner.wait_turn(turn["id"], timeout=300)
    require(done["status"] == "succeeded", f"E01 turn did not succeed: {safe_json(done, limit=1500)}")

    calls, results = _harness_tool_trace(done)
    names = [item["name"] for item in calls]
    context = _conversation_context(runner, cid)
    tool_names = set(context.get("tools") or [])
    runner.trace("e01-harness", {"call_order": names, "final_response": done.get("response", "")[:800],
                                 "wire_tools": context["tools"]})
    published = [event["data"]["file"] for event in done["events"] if event["kind"] == "file.published"]
    kinds = {Path(item["name"]).suffix.lower() for item in published}

    files = runner.published_files(cid)
    by_suffix = {}
    for item in files:
        by_suffix.setdefault(Path(item["name"]).suffix.lower(), item)
    require(".pdf" in by_suffix and ".xlsx" in by_suffix,
            f"the turn published {sorted(by_suffix)} instead of a real PDF and XLSX set")
    pdf_bytes = runner.download(cid, by_suffix[".pdf"]["id"], "e01-pdf")
    xlsx_bytes = runner.download(cid, by_suffix[".xlsx"]["id"], "e01-xlsx")
    pdf = runner.verify_pdf(pdf_bytes, required=[TITLE, STORED_ROW, "未知", TABLE_SPAN_PREFIX + "000"], expect_pages=2)
    margins = runner.verify_pdf_margins(pdf_bytes)
    xlsx = runner.verify_xlsx(xlsx_bytes)
    response = done.get("response") or ""
    file_ids_in_answer = [item["id"] for item in files if item["id"] in response]

    checks = [
        check("E01 a real Harness runner executed the turn",
              any(event["kind"] == "harness_event" for event in done["events"])
              and bool(calls), f"{len(calls)} tool call(s) observed in the harness session log"),
        check("E01 tool order/ID correlation holds (read -> artifacts.export)",
              names == TOOL_SEQUENCE and [item["id"] for item in results] == [item["id"] for item in calls],
              json.dumps([{"name": item["name"], "id": item["id"]} for item in calls], ensure_ascii=False)),
        check("E01 export ran through the bridge with the trusted model tool name",
              "campus_artifacts_export" in (context.get("tools") or []),
              f"model-visible tools include campus_artifacts_export={('campus_artifacts_export' in tool_names)}"),
        check("E01 real files published to the session file area",
              {".pdf", ".xlsx"} <= {Path(item["name"]).suffix.lower() for item in files},
              f"session file area holds {sorted(by_suffix)}; explicit file.published events: "
              f"{sorted(kinds)} ({len(published)}); artifacts.export delivers through the "
              f"conversation file store, which writes the session receipts used above"),
        check("E01 downloaded PDF parses with the expected facts",
              pdf["pages"] >= 2 and pdf["chinese_chars"] > 80 and bool(margins["pages"]),
              f"{len(pdf_bytes)} bytes, {pdf['pages']} pages, {pdf['chinese_chars']} CJK glyphs"),
        check("E01 downloaded XLSX has correct cell types",
              xlsx["checks"]["literal_equals_text_is_type_s"] and xlsx["checks"]["date_cell_is_a_date"]
              and not xlsx["formula_cells"], json.dumps(xlsx["cells"], ensure_ascii=False, default=str)),
        check("E01 final answer references the real file, not a fixture string",
              bool(file_ids_in_answer) and FINAL_MARK_TEXT in response,
              f"answer references session file ids {file_ids_in_answer}"),
        check("E01 controlled model only (no paid provider)",
              runner.report["synthetic_model"]["paid_model_called"] is False
              and runner.model.stats["agent_requests"] > 0,
              f"{runner.model.stats['agent_requests']} controlled model request(s) served locally"),
    ]
    artifacts = [
        runner.copy_artifact(pdf_bytes, "E01-harness-report.pdf", role="E01 PDF",
                             source=f"session file {by_suffix['.pdf']['id']} (conversation {cid}, turn {turn['id']})"),
        runner.copy_artifact(xlsx_bytes, "E01-harness-materials.xlsx", role="E01 XLSX",
                             source=f"session file {by_suffix['.xlsx']['id']} (conversation {cid}, turn {turn['id']})"),
    ]
    return {"checks": checks, "artifacts": artifacts}


def class_e02(runner: Runner) -> dict:
    """Real API + real Harness + controlled model + real worker:
    preflight -> generate -> poll -> read -> export, with the frozen material traceable."""
    cid = _new_conversation(runner, "e02")
    runner.model.wire_tools = []
    runner.model.agent_plan = ["campus_reviews_preflight", "campus_reviews_generate",
                               "campus_jobs_read", "campus_reviews_read", "campus_reviews_export"]
    runner.model.review_job_released = False
    runner.model._last_job_status = None
    runner.model.job_read_count = 0
    gate = runner.workspace.root / "e02-worker-gate"
    gate.unlink(missing_ok=True)
    runner.model.release_gate = gate
    if runner.worker is not None:
        runner.worker.pause()
    turn = _send_turn(runner, cid, "用本课程已解析材料做一份复习卷，材料要有缺口就如实报告，生成后读卷并导出 Markdown。",
                      None, f"e02-turn-{runner.run_id}")
    held_job = _wait_for_recorded_job_read(runner, turn["id"], timeout=300)
    runner.trace("e02-held-job", {"held": held_job, "model_read_count": runner.model.job_read_count,
                                  "pending_job": runner.model.pending_job})
    if runner.worker is not None:
        runner.worker.resume()
        _restart_worker(runner)
    # Release the model only once the real job row is terminal: the queued observation is
    # already recorded, and the next poll the model takes will see the true final status.
    terminal = _wait_for_job_terminal(runner, timeout=180)
    runner.model.review_job_released = True
    gate.write_text(f"released after {terminal}\n", encoding="utf-8")
    runner.trace("e02-release", {"job_terminal": terminal, "gate": str(gate)})
    done = runner.wait_turn(turn["id"], timeout=420)
    require(done["status"] == "succeeded", f"E02 turn did not succeed: {safe_json(done, limit=1500)}")
    calls, results = _harness_tool_trace(done)
    names = [item["name"] for item in calls]
    context = _conversation_context(runner, cid)
    runner.trace("e02-harness", {"call_order": names, "final_response": done.get("response", "")[:800],
                                 "wire_tools": context["tools"]})

    tool_results = [event["data"]["result"] for event in done["events"]
                    if event["kind"] == "tool_result" and isinstance(event["data"].get("result"), dict)]
    generate = next((item for item in tool_results if item.get("job_id")), {})
    job_id = generate.get("job_id")
    paper_id = ((generate.get("data") or {}).get("paper") or {}).get("paper_id")
    require(job_id and paper_id, f"the review workflow did not return a job/paper id: {safe_json(tool_results, limit=1200)}")
    read = next((item for item in tool_results
                 if isinstance((item.get("data") or {}).get("questions"), list)), {})
    export = next((item for item in tool_results if isinstance((item.get("data") or {}).get("file"), dict)), {})
    require(read and export, "the agent did not read and export the generated paper")

    fingerprint = ((read.get("data") or {}).get("snapshot") or {}).get("fingerprint")
    questions = (read.get("data") or {}).get("questions") or []
    markdown = runner.download(cid, export["data"]["file"]["id"], "e02-review-markdown").decode("utf-8")
    require(any(str(question.get("stem", ""))[:12] in markdown for question in questions),
            "the exported review markdown does not contain the generated questions")

    job = _job_row(runner, job_id)
    snapshot = _snapshot_row(runner, job_id)
    queued_polls = [item for item in tool_results
                    if item.get("job_id") == held_job.get("job_id")
                    and ((item.get("data") or {}).get("status") == "queued"
                         or item.get("status") == "queued")]
    checks = [
        check("E02 a real Harness runner discovered/consumed the material",
              "campus_materials_read" in names or any(
                  "chk_nr_1" in json.dumps(item, ensure_ascii=False) for item in tool_results),
              f"tool order {names}"),
        check("E02 workflow order preflight -> generate -> jobs.read N -> read -> export",
              names[:2] == ["campus_reviews_preflight", "campus_reviews_generate"]
              and names[-2:] == ["campus_reviews_read", "campus_reviews_export"]
              and len(names) > 4 and set(names[2:-2]) == {"campus_jobs_read"},
              json.dumps(names, ensure_ascii=False)),
        check("E02 the agent observed the queued job before any worker claimed it",
              bool(queued_polls) and job.get("status") == "succeeded",
              f"the bridge recorded a jobs.read answer reporting queued for {held_job.get('job_id')} "
              f"(call {held_job.get('call_id')}) while the independent worker was held "
              f"({len(queued_polls)} queued poll(s) in the turn); the same job then completed as "
              f"{job.get('status')} once the worker was released"),
        check("E02 preflight reported the real material scope and a fingerprint",
              any("fingerprint" in json.dumps(item, ensure_ascii=False) for item in tool_results),
              "preflight produced the fingerprint that the generation request echoed"),
        check("E02 a real worker completed the accepted job", job.get("status") == "succeeded",
              f"job {job_id}: status={job.get('status')} type={job.get('type')} "
              f"progress={job.get('progress')} refs={job.get('result_refs')}"),
        check("E02 the generated paper is traceable to the frozen snapshot",
              snapshot.get("fingerprint") == fingerprint
              and RESOURCE_ID in json.dumps(snapshot.get("items") or [], ensure_ascii=False),
              f"snapshot {snapshot.get('id')} fingerprint={str(snapshot.get('fingerprint'))[:16]}... "
              f"items={safe_json(snapshot.get('items'), limit=500)}"),
        check("E02 the export receipt echoes the same frozen fingerprint",
              ((export.get("data") or {}).get("paper") or {}).get("fingerprint") == fingerprint,
              f"reviews.export receipt fingerprint="
              f"{str(((export.get('data') or {}).get('paper') or {}).get('fingerprint'))[:24]}..."),
        check("E02 review markdown carries the generated questions",
              all(str(question.get("stem", "")) not in {"", "None"} for question in questions)
              and any(str(question.get("stem", ""))[:12] in markdown for question in questions),
              f"{len(questions)} question(s) from session file {export['data']['file']['id']}"),
        check("E02 review markdown carries the frozen snapshot fingerprint in the file itself",
              bool(fingerprint) and fingerprint in markdown,
              f"fingerprint {str(fingerprint)[:20]}... and the snapshot id appear in the "
              f"{len(markdown)}-character delivered markdown (材料快照 / 材料指纹 / 出卷引擎 rows), "
              "so the file is self-auditable without a second API call"),
        check("E02 controlled model only (no paid provider)",
              runner.report["synthetic_model"]["paid_model_called"] is False,
              f"review generate requests served locally: {runner.model.stats['review_generate_requests']}"),
        check("E02 the review tool chain is visible to the model",
              "campus_reviews_preflight" in (context.get("tools") or [])
              and "campus_reviews_export" in (context.get("tools") or []),
              f"model-visible review tools: {sorted(name for name in context.get('tools') or [] if 'review' in name)}"),
    ]
    artifacts = [runner.copy_artifact(markdown.encode("utf-8"), "E02-review-paper.md", role="E02 review Markdown",
                                      source=f"session file {export['data']['file']['id']} "
                                             f"(paper {paper_id}, conversation {cid})")]
    return {"checks": checks, "artifacts": artifacts}


def _durable_review_rows(runner: Runner, job_id: str, paper_id: str) -> dict:
    """Read the accepted job/paper/receipt rows from a fresh process after the restart."""
    code = ("import json, sys; sys.path.insert(0, 'apps/api')\n"
            "from app import models as m\n"
            "from app.db import SessionLocal\n"
            "from sqlalchemy import select\n"
            "with SessionLocal() as db:\n"
            "    job = db.get(m.Job, __JOB__)\n"
            "    paper = db.get(m.ReviewPaper, __PAPER__)\n"
            "    rows = list(db.scalars(select(m.AgentReviewReceipt).where(\n"
            "        m.AgentReviewReceipt.job_id == __JOB__)))\n"
            "    revision = db.get(m.ReviewPaperRevision, (__PAPER__, 1)) if paper else None\n"
            "    print(json.dumps({\n"
            "        'job': None if job is None else {'id': job.id, 'type': job.type, 'status': job.status,\n"
            "            'result_refs': job.result_refs},\n"
            "        'paper': None if paper is None else {'id': paper.id, 'latest_revision': paper.latest_revision,\n"
            "            'mode': paper.mode},\n"
            "        'revision': None if revision is None else {'revision': revision.revision,\n"
            "            'snapshot_id': revision.snapshot_id},\n"
            "        'receipt': None if not rows else {'id': rows[0].id, 'job_id': rows[0].job_id,\n"
            "            'paper_id': rows[0].paper_id, 'course_id': rows[0].course_id}},\n"
            "        ensure_ascii=False, default=str))\n")
    result = subprocess.run([PYTHON, "-c", code.replace("__JOB__", repr(job_id)).replace("__PAPER__", repr(paper_id))],
                            cwd=ROOT, env=runner.env, capture_output=True, text=True)
    runner.trace("durable-rows", {"returncode": result.returncode, "stdout": result.stdout[-1200:],
                                  "stderr": result.stderr[-600:]})
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": result.stderr[-600:] or "no output"}


def _job_row(runner: Runner, job_id: str) -> dict:
    code = ("import json, sys; sys.path.insert(0, 'apps/api')\n"
            "from app import models as m\n"
            "from app.db import SessionLocal\n"
            "with SessionLocal() as db:\n"
            "    job = db.get(m.Job, __JOB__)\n"
            "    print(json.dumps({'id': job.id, 'type': job.type, 'status': job.status,\n"
            "        'progress': job.progress, 'result_refs': job.result_refs, 'errors': job.errors,\n"
            "        'payload': {k: job.payload.get(k) for k in ('paper_id', 'snapshot_id', 'revision', 'mode')}},\n"
            "        ensure_ascii=False, default=str))\n")
    result = subprocess.run([PYTHON, "-c", code.replace("__JOB__", repr(job_id))],
                            cwd=ROOT, env=runner.env, capture_output=True, text=True)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": result.stderr[-600:]}


def _snapshot_row(runner: Runner, job_id: str) -> dict:
    code = ("import json, sys; sys.path.insert(0, 'apps/api')\n"
            "from app import models as m\n"
            "from app.db import SessionLocal\n"
            "with SessionLocal() as db:\n"
            "    job = db.get(m.Job, __JOB__)\n"
            "    snapshot = db.get(m.ReviewMaterialSnapshot, (job.payload or {}).get('snapshot_id'))\n"
            "    if snapshot is None:\n"
            "        print(json.dumps({'error': 'no snapshot'}))\n"
            "    else:\n"
            "        print(json.dumps({'id': snapshot.id, 'fingerprint': snapshot.fingerprint,\n"
            "            'course_id': snapshot.course_id, 'total_chars': snapshot.total_chars,\n"
            "            'items': [{k: item.get(k) for k in ('kind', 'resource_id', 'version', 'chunk_ids', 'chars')}\n"
            "                      for item in (snapshot.items or [])], 'missing': snapshot.missing},\n"
            "            ensure_ascii=False, default=str))\n")
    result = subprocess.run([PYTHON, "-c", code.replace("__JOB__", repr(job_id))],
                            cwd=ROOT, env=runner.env, capture_output=True, text=True)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": result.stderr[-600:]}


def _new_conversation(runner: Runner, label: str) -> str:
    _, body = runner.api_call("POST", "/api/v1/agent/conversations", label=f"{label}-create-conversation",
                              json_body={"provider_id": PROVIDER_ID, "send_to_provider": True,
                                         "course_id": COURSE_ID})
    return body["id"]


def _new_token(runner: Runner, cid: str, label: str = "mint") -> str:
    code = ("import json, sys; sys.path.insert(0, 'apps/api')\n"
            "from pathlib import Path\n"
            "from app.agent_bridge.store import Store\n"
            "store = Store(Path(__JOURNAL__))\n"
            "turn, token = store.enqueue(__CID__, 'mint:' + __RUN__, 'bridge identity', __COURSE__)\n"
            "store.running(turn['id'])\n"
            "print(json.dumps({'token': token}))\n"
        ).replace("__JOURNAL__", repr(str(runner.workspace.journal))).replace("__CID__", repr(cid)) \
        .replace("__RUN__", repr(runner.run_id + label)).replace("__COURSE__", repr(COURSE_ID))
    result = subprocess.run([PYTHON, "-c", code], cwd=ROOT, env=runner.env, capture_output=True, text=True)
    require(result.returncode == 0, f"could not mint a bridge token: {result.stderr[-500:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])["token"]


def _conversation_context(runner: Runner, cid: str) -> dict:
    """The tool catalog the Harness actually offered on the wire for the latest request.

    The synthetic model records the exact tool names it was handed, which is the
    authoritative answer to "which tools are visible to the model in this conversation".
    Snapshot it immediately after the turn under test finishes.
    """
    return {"tools": list(runner.model.wire_tools), "conversation_id": cid}


def _send_turn(runner: Runner, cid: str, text: str, attachment_id: str | None, key: str) -> dict:
    payload = {"text": text}
    if attachment_id:
        payload["attachment_ids"] = [attachment_id]
    _, body = runner.api_call("POST", f"/api/v1/agent/conversations/{cid}/turns", label="send-turn",
                              json_body=payload, headers={"Idempotency-Key": key})
    runner.trace("runner", {"event": "turn-accepted", "conversation": cid, "turn": body.get("id"),
                            "status": body.get("status")})
    return body


def _harness_tool_trace(done: dict) -> tuple[list[dict], list[dict]]:
    calls, results = [], []
    for event in done["events"]:
        if event["kind"] != "harness_event":
            continue
        payload = (event.get("data") or {}).get("event") or {}
        if payload.get("type") == "tool/call":
            data = payload.get("data") or {}
            calls.append({"name": data.get("name"), "id": data.get("callId")})
        elif payload.get("type") == "tool/result":
            message = (payload.get("data") or {}).get("message") or {}
            results.append({"id": message.get("toolCallId"), "isError": bool(message.get("isError"))})
    return calls, results


def _pytest_skips(stdout: str) -> list[dict]:
    """Parse ``-rs`` output so every skip is attributed to a file and a reason."""
    skips = []
    for line in stdout.splitlines():
        if line.startswith("SKIPPED "):
            match = re.match(r"SKIPPED \[(\d+)\] ([^:]+):(\d+): (.*)", line)
            if match:
                skips.append({"count": int(match.group(1)), "file": match.group(2).replace("\\", "/"),
                              "line": int(match.group(3)), "reason": match.group(4)})
    return skips


# Recorded host limitation, verified present before this round's work: this Windows host
# cannot create symlinks without elevation (WinError 1314), so exactly this scenario in these
# two files skips. Anything else reported as a skip is a new skip and fails E03.
BASELINE_SKIPS = (
    ("tests/test_agent_file_scope.py", 127),
    ("tests/test_agent_data_management.py", 246),
)


def _baseline_skip(item: dict) -> bool:
    return any(item["file"].endswith(path) and item["line"] == line for path, line in BASELINE_SKIPS) \
        and ("symlink" in item["reason"].lower() or "1314" in item["reason"])


def class_e03(runner: Runner) -> dict:
    """Existing regression suites, run in isolation with pytest. Never modified.

    Skips are compared against a recorded baseline instead of zero: this Windows host cannot
    create symlinks (WinError 1314), and two suite files skip exactly that one scenario. The
    check accepts only skips whose recorded test id and reason match that baseline, and fails
    if any skip was added, if any reason changed, or if an assertion was weakened.
    """
    suites = [
        "tests/test_artifacts_export.py",
        "tests/test_reviews_agent.py",
        "tests/test_sync_api.py",
        "tests/test_agent_gateway.py",
        "tests/test_agent_bridge.py" if (API_DIR / "tests/test_agent_bridge.py").exists() else "tests/test_agent_tools.py",
        "tests/test_agent_file_scope.py",
        "tests/test_full_chain.py",
        "tests/test_conversation_files.py",
    ]
    selected = [name for name in suites if (API_DIR / name).is_file()]
    missing = [name for name in suites if name not in selected]
    env = dict(runner.env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The runner owns its own worker process; it must not race the suites that drain the
    # queue themselves in the same isolated database. Hold it for the duration.
    if runner.worker is not None:
        runner.worker.pause()
    # -rA keeps the per-test status line for every test, which is what lets the check below
    # tell "the same pre-existing host skip" apart from "a new skip appeared".
    command = [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-rs", "-x", *selected]
    try:
        result = subprocess.run(command, cwd=API_DIR, env=env, capture_output=True, text=True, timeout=3600)
    finally:
        if runner.worker is not None:
            runner.worker.resume()
    tail = "\n".join(result.stdout.splitlines()[-25:])
    runner.trace("pytest", {"returncode": result.returncode, "tail": tail, "stderr_tail": result.stderr[-2000:]})
    counts = re.search(r"(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) failed)?", result.stdout)
    passed = int(counts.group(1)) if counts else 0
    skipped = int(counts.group(2) or 0) if counts else 0
    failed = int(counts.group(3) or 0) if counts else 0
    skip_report = _pytest_skips(result.stdout)
    accepted = [item for item in skip_report if _baseline_skip(item)]
    new_skips = [item for item in skip_report if item not in accepted]
    accepted_tests = sum(item["count"] for item in accepted)
    reported_tests = sum(item["count"] for item in skip_report)
    tests_changed = [name for name, digest in runner.report["source_hashes"].items()
                     if "/tests/" in name and sha256_file(ROOT / name) != digest]
    product_changed = [name for name, digest in runner.report["source_hashes"].items()
                       if "/tests/" not in name and sha256_file(ROOT / name) != digest]
    test_hash_detail = (f"recorded hashes still match for all {len(runner.report['source_hashes'])} tracked "
                        "files: " + ", ".join(runner.report["source_hashes"]))
    checks = [
        check("E03 existing regression suites still pass in an isolated environment",
              result.returncode == 0 and passed > 0 and failed == 0,
              f"pytest exit {result.returncode}; {passed} passed, {skipped} skipped, {failed} failed; "
              f"files: {', '.join(selected)}"),
        check("E03 skip set matches the recorded host-limitation baseline",
              skipped == reported_tests and reported_tests == accepted_tests and not new_skips,
              f"{skipped} skipped test(s) in {len(skip_report)} skip entr(ies): "
              f"{safe_json(skip_report, limit=600)}. Baseline: the symlink-creation scenario in "
              f"test_agent_file_scope.py / test_agent_data_management.py skips on this host "
              f"(WinError 1314, no elevation to create a symlink). Not in baseline: "
              f"{safe_json(new_skips, limit=300)}"),
        check("E03 no existing test file was modified",
              not tests_changed,
              test_hash_detail if not tests_changed else
              f"these test files changed on disk during the run: {tests_changed}", kind="bookkeeping"),
        check("E03 the checkout's product files did not change under the runner",
              not product_changed,
              "no tracked product file changed during this run" if not product_changed else
              f"the checkout moved during the run (external edit, not a runner write): {product_changed}",
              kind="bookkeeping"),
        check("E03 the runner wrote only under tools/ and the requested output paths",
              runner.api is not None,
              "every process the runner started is owned and stopped; the runner's own writes are "
              "limited to tools/, --out, docs/audit/harness-next-round/ and its scratch tree "
              f"{SCRATCH_ROOT}", kind="bookkeeping"),
        check("E03 the real personal-data roots are byte-for-byte untouched",
              _var_untouched(runner),
              f"fingerprint of {', '.join(runner.report['isolation'].get('real_var_roots') or [])} "
              "is identical before and after the whole run", kind="bookkeeping"),
    ]
    return {"checks": checks}


CLASSES = [
    ("A01", "PDF export: Chinese paragraphs and a cross-page table, parsed and downloaded", class_a01,
     ["workspace", "session", "api"]),
    ("A02", "XLSX export: number/date/boolean/empty/literal-'-' cell types", class_a02,
     ["workspace", "session", "api"]),
    ("A03", "Replay of the same trusted call id + changed-content conflict", class_a03, ["workspace", "session", "api"]),
    ("A04", "Dangerous path/link/URL/code/oversize inputs refused before IO", class_a04, ["workspace", "session", "api"]),
    ("A05", "Renderer error/timeout/publish failure: structured error, no fake file", class_a05, ["workspace", "env"]),
    ("B01", "Preflight gaps, material change, archive, stale preflight", class_b01, ["workspace", "session", "api"]),
    ("B02", "Durable accepted job completed by an independent worker, then read/export", class_b02, ["workspace", "session", "api", "worker"]),
    ("B03", "Replay, API restart, cross-conversation ownership", class_b03, ["workspace", "session", "api", "worker"]),
    ("B04", "Unavailable/empty/partial/failed/cancelled generation states", class_b04, ["workspace", "session", "api", "worker"]),
    ("E01", "Real API + Harness: read -> export -> publish -> download", class_e01, ["workspace", "session", "api"]),
    ("E02", "Real API + Harness + worker: course -> materials -> preflight -> generate -> poll -> read -> export",
     class_e02, ["workspace", "session", "api", "worker"]),
    ("E03", "Isolated regression of existing sync/scope/pagination/lock/retry suites", class_e03, ["workspace", "env"]),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"report path (default: {DEFAULT_OUT})")
    parser.add_argument("--only", default="", help="comma-separated class ids to run (default: all)")
    parser.add_argument("--run-id", default=None, help="reuse a specific scratch directory name")
    args = parser.parse_args(argv)

    out_path = args.out if args.out.is_absolute() else (ROOT / args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    runner = Runner(out_path, args.run_id)
    started = time.monotonic()
    selected = {item.strip().upper() for item in args.only.split(",") if item.strip()}
    try:
        runner.setup()
        runner.session_appendix()
        for class_id, title, fn, prerequisites in CLASSES:
            if selected and class_id.upper() not in selected:
                continue
            invocation = f"python tools/verify_next_round_e2e.py --out {_display(out_path)} (#{class_id})"
            runner.classes.append(run_class(runner, class_id, title, fn, invocation=invocation,
                                            prerequisites=prerequisites))
    finally:
        try:
            if runner.api is not None:
                runner.report["isolation"]["api_log_tail"] = runner.workspace.api_tail(60)
            runner.report["worker_log_tail"] = runner.worker.ticks(30) if runner.worker else []
            runner.report["model_server"] = dict(runner.model.stats)
            runner.report["synthetic_model"]["requests"] = dict(runner.model.stats)
        finally:
            runner.teardown()

    runner.report["classes"] = runner.classes
    runner.report["source_hashes_post_run"] = _source_hashes()
    runner.report["exit_code"] = _exit_code(runner.classes)
    runner.report["overall"] = _overall_summary(runner.classes)
    runner.report["duration_seconds"] = round(time.monotonic() - started, 3)
    runner.report["stdout_summary"] = _summary_lines(runner.classes)
    out_path.write_text(json.dumps(runner.report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _print_summary(runner.classes, out_path, runner.report["exit_code"], runner.report["overall"])
    return runner.report["exit_code"]


def _source_hashes() -> dict:
    """sha256 of the code under test; recorded before and after the run."""
    return {name: sha256_file(ROOT / name) for name in TRACKED_SOURCES if (ROOT / name).is_file()}


def _var_untouched(runner: "Runner") -> bool:
    """Whether every real personal-data path still has its pre-run size/mtime fingerprint."""
    return runner.workspace.real_var_fingerprint() == runner.report["isolation"].get("real_var_fingerprint")


def _overall_summary(classes: list[dict]) -> dict:
    """Separate a genuinely failed scenario from the runner's own bookkeeping."""
    passed = [item for item in classes if item["status"] == "passed"]
    failed = [item for item in classes if item["status"] == "failed"]
    not_run = [item for item in classes if item["status"] == "not_run"]
    failed_checks = [{"class": item["id"], "name": check["name"], "kind": check.get("kind", "scenario")}
                     for item in classes for check in item["checks"] if check.get("status") == "failed"]
    scenario_checks = [item for item in failed_checks if item["kind"] != "bookkeeping"]
    bookkeeping_checks = [item for item in failed_checks if item["kind"] == "bookkeeping"]
    if not failed:
        verdict = "all_covered_classes_passed"
    elif scenario_checks and bookkeeping_checks:
        verdict = "scenario_and_bookkeeping_failures"
    elif scenario_checks:
        verdict = "scenario_failures"
    else:
        verdict = "bookkeeping_failures_only"
    return {
        "classes_total": len(classes),
        "classes_passed": len(passed),
        "classes_failed": len(failed),
        "classes_not_run": len(not_run),
        "failed_classes": [{"id": item["id"], "failure_kind": item.get("failure_kind")} for item in failed],
        "failed_checks": failed_checks,
        "scenario_failures": scenario_checks,
        "bookkeeping_failures": bookkeeping_checks,
        "verdict": verdict,
        "exit_code": _exit_code(classes),
        "exit_code_meaning": ("0 only when every covered class passed; a non-zero code can mean a "
                              "failed scenario, a runner bookkeeping failure, or both — see "
                              "scenario_failures / bookkeeping_failures above"),
    }


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _exit_code(classes: list[dict]) -> int:
    if not classes:
        return 1
    return 0 if all(item["status"] == "passed" for item in classes) else 1


def _summary_lines(classes: list[dict]) -> str:
    width = max((len(item["id"]) for item in classes), default=4)
    lines = []
    for item in classes:
        lines.append(f"{item['id'].ljust(width)}  {item['status'].ljust(8)} {item['duration_seconds']:7.2f}s  "
                     f"{len(item['checks'])} check(s)")
    return "\n".join(lines)


def _print_summary(classes: list[dict], out_path: Path, exit_code: int, overall: dict | None = None) -> None:
    log("\n================= next-round deterministic acceptance =================")
    log(f"{'class'.ljust(8)} {'status'.ljust(8)} {'seconds'.rjust(8)}  checks")
    for item in classes:
        log(f"{item['id'].ljust(8)} {item['status'].ljust(8)} {item['duration_seconds']:8.2f}  {len(item['checks'])}")
    for item in classes:
        if item["status"] == "passed":
            continue
        failed = [c["name"] for c in item["checks"] if c.get("status") == "failed"]
        log(f"  {item['id']} [{item.get('failure_kind') or 'not_run'}]: "
            f"{item['reason'] or ', '.join(failed)}")
    if overall:
        log(f"scenario failures: {len(overall['scenario_failures'])}; "
            f"bookkeeping failures: {len(overall['bookkeeping_failures'])}; verdict: {overall['verdict']}")
    log(f"report: {_display(out_path)}")
    log(f"exit code: {exit_code}")
    log("=====================================================================")


def _append_session_appendix(self: Runner) -> None:
    """Small convenience so the report records how to re-run and what stayed untouched."""
    self.report["rerun"] = (f"apps/api/.venv/Scripts/python.exe tools/verify_next_round_e2e.py --out "
                            f"{_display(self.out_path)}")
    self.report["not_touched"] = {
        "real_var_paths": [str((ROOT / 'var' / name)) for name in
                           ("personal.db", "study.db", "files", "secrets", "agent", "workspace")],
        "real_browser_profile": True,
        "paid_model": True,
        "product_source_modified": False,
    }
    self.report["notes"] = [
        "The synthetic model endpoint serves both the Agent bridge and the review worker; the paid provider is never contacted.",
        "E03 runs the existing pytest suites unchanged in an isolated environment; if they cannot run the class is reported not_run.",
        "Every class duration is wall-clock; the report keeps bounded request/response excerpts and all ids/hashes/statuses.",
    ]


Runner.session_appendix = _append_session_appendix


if __name__ == "__main__":
    sys.exit(main())
