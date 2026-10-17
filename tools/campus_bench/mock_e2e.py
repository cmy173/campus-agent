"""Run one isolated scripted-model E2E through the real local Agent API and Harness.

This is deliberately not a model-quality or campus-connectivity benchmark. The
OpenAI-compatible provider is scripted and loopback-only; the API database and
all Agent state live in a disposable temporary directory containing synthetic
course data. Only the ordinary public conversation/turn endpoints are used to
drive the run. No existing personal workspace, browser profile, or campus
credential is read.

Run from the repository root with the same Python environment used by the API::

    python tools/campus_bench/mock_e2e.py

The command emits one redacted JSON object to stdout and returns nonzero unless
the Harness selects the expected tool, the real executor returns seeded
synthetic data, and that result reaches the scripted provider's final turn.
"""
from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import http.server
import json
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.campus_bench.runner import agent_api_source_sha256, api_agent_source_fingerprint_status
API_DIR = ROOT / "apps" / "api"
API_PYTHON = API_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
COURSE_MARKER = "BENCH-ONLY-COURSE"
COURSE_ID = "bench_synthetic_course"
COURSE_TITLE = "BENCH-ONLY-COURSE - 只读 E2E 合成课程"
JOB_MARKER = "BENCH-JOB-READ"
JOB_ID = "bench_synthetic_sync_job"
JOB_RECEIPT_ID = "bench_synthetic_sync_receipt"
MATERIAL_MARKER = "BENCH-MATERIAL-READ"
MATERIAL_ID = "bench_synthetic_material"
MATERIAL_CHUNK_ID = "bench_synthetic_material_chunk"
MATERIAL_TEXT = "课程目标：掌握线性代数基本概念与证明方法。"
ASSIGNMENT_ID = "bench_synthetic_assignment"
ASSIGNMENT_TITLE = "BENCH-ONLY-数据结构课程作业"
LEARNING_TASK_ID = "bench_synthetic_learning_task"
PLAN_FIXTURE_MARKER = "BENCH-PLAN-PREVIEW"
REVIEW_FIXTURE_MARKER = "BENCH-REVIEW-PREFLIGHT"
EMPTY_SEARCH_MARKER = "BENCH-EMPTY-MATERIALS-SEARCH"
PARAM_ERROR_MARKER = "BENCH-INVALID-MATERIAL-READ-PARAM"
EXPIRED_SESSION_MARKER = "BENCH_EXPIRED_SESSION"
EMPTY_ASSIGNMENT_MARKER = "BENCH-EMPTY-ASSIGNMENT-CACHE"
ASSIGNMENT_PAGE_MARKER = "BENCH-ASSIGNMENT-PAGINATION"
ASSIGNMENT_PAGE_LAST_ID = "bench_synthetic_assignment_page3"
MODEL_ID = "campus-bench-scripted-e2e"
LOCAL_API_KEY = "benchmark-scripted-key"
TURN_TIMEOUT_SECONDS = 120
POLL_SECONDS = 0.25
MAX_MODEL_CALLS = 6
_API_OPENER: urllib.request.OpenerDirector | None = None


class SafeFailure(RuntimeError):
    def __init__(self, stage: str, code: str):
        super().__init__(code)
        self.stage = stage
        self.code = code


class ScriptState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.error_code: str | None = None

    def add(self, item: dict) -> int:
        with self.lock:
            self.requests.append(item)
            return len(self.requests)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.requests)


def _find_text(value) -> str:
    """Collect text only for local fixture checks; never return it in evidence."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_find_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_find_text(item) for item in value)
    return ""


def _tool_names(payload: dict) -> list[str]:
    names = []
    for item in payload.get("tools", []):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            names.append(name)
    return names


def _loader_supports_review_group(payload: dict) -> bool:
    for item in payload.get("tools", []):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        if not isinstance(function, dict) or function.get("name") != "load_tools":
            continue
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") if isinstance(parameters, dict) else {}
        group = properties.get("group") if isinstance(properties, dict) else {}
        if isinstance(group, dict) and "campus_reviews" in (group.get("enum") or []):
            return True
    return False


def _has_synthetic_tool_result(messages: list, *, job: bool = False) -> bool:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            text = _find_text(message.get("content"))
            if (JOB_ID in text and '"status": "succeeded"' in text) if job else COURSE_MARKER in text:
                return True
    return False


def _has_material_result(messages: list, *, read: bool = False) -> bool:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            text = _find_text(message.get("content"))
            if MATERIAL_CHUNK_ID in text and (MATERIAL_TEXT in text if read else MATERIAL_ID in text):
                return True
    return False


def _has_assignment_result(messages: list) -> bool:
    return any(isinstance(message, dict) and message.get("role") == "tool"
               and ASSIGNMENT_ID in _find_text(message.get("content"))
               and ASSIGNMENT_TITLE in _find_text(message.get("content")) for message in messages)


def _assignment_cursor_in_messages(messages: list) -> str | None:
    """Read a cursor from an actual tool result in provider context; never persist it."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        match = re.search(r'"next_cursor"\s*:\s*"([^"\\]+)"', _find_text(message.get("content")))
        if match:
            return match.group(1)
    return None


def _has_pending_plan_preview(messages: list) -> bool:
    return any(isinstance(message, dict) and message.get("role") == "tool"
               and '"kind": "apply"' in _find_text(message.get("content"))
               and '"status": "pending"' in _find_text(message.get("content"))
               and '"status": "candidate"' in _find_text(message.get("content")) for message in messages)


def _has_tool_error(messages: list, code: str) -> bool:
    return any(isinstance(message, dict) and message.get("role") == "tool"
               and f'"code": "{code}"' in _find_text(message.get("content")) for message in messages)


def _has_review_preflight_result(messages: list) -> bool:
    return any(isinstance(message, dict) and message.get("role") == "tool"
               and '"can_generate"' in _find_text(message.get("content"))
               and '"fingerprint"' in _find_text(message.get("content")) for message in messages)


def _has_loaded_review_group_receipt(messages: list) -> bool:
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        text = _find_text(message.get("content"))
        try:
            result = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict) and "campus_reviews" in (result.get("loaded_groups") or []):
            return True
    return False


def _completion(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _scripted_handler(state: ScriptState, fixture: str, defer_tool_group: str | None = None):
    job_fixture = fixture == "JOB-01"
    material_fixture = fixture == "BUS-03"
    plan_fixture = fixture == "MULTI-02"
    empty_search_fixture = fixture == "ERR-02"
    empty_assignment_fixture = fixture == "ERR-11"
    assignment_page_fixture = fixture == "BUS-12"
    parameter_error_fixture = fixture == "ERR-09"
    expired_session_fixture = fixture == "ERR-10"
    review_fixture = fixture == "TOOL-REV-01"
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *args):
            # Request text and provider headers stay out of server logs.
            return

        def _reply(self, status: int, body: dict) -> None:
            encoded = _completion(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1_000_000:
                    state.error_code = "invalid_request_size"
                    return self._reply(413, {"error": {"message": "invalid fixture request"}})
                payload = json.loads(self.rfile.read(length))
                messages = payload.get("messages") if isinstance(payload, dict) else None
                if self.path != "/v1/chat/completions" or not isinstance(messages, list):
                    state.error_code = "unexpected_provider_route"
                    return self._reply(400, {"error": {"message": "unexpected fixture request"}})
                call_number = len(state.snapshot()) + 1
                tools = _tool_names(payload)
                record = {
                    "sequence": call_number,
                    "route_ok": self.path == "/v1/chat/completions",
                    "model_ok": payload.get("model") == MODEL_ID,
                    "api_key_ok": self.headers.get("Authorization") == f"Bearer {LOCAL_API_KEY}",
                    "message_count": len(messages),
                    "available_course_tool": "campus_courses_list" in tools,
                    "available_job_tool": "campus_jobs_read" in tools,
                    "available_material_search_tool": "campus_materials_search" in tools,
                    "available_material_read_tool": "campus_materials_read" in tools,
                    "available_assignment_tool": "campus_assignments_list" in tools,
                    "available_availability_tool": "campus_availability_read" in tools,
                    "available_plan_preview_tool": "campus_plans_preview" in tools,
                    "available_load_tools": "load_tools" in tools,
                    "load_tools_supports_review_group": _loader_supports_review_group(payload),
                    "available_review_preflight_tool": "campus_reviews_preflight" in tools,
                    "review_user_prompt_seen": REVIEW_FIXTURE_MARKER in _find_text(messages),
                    "review_preflight_result_seen": _has_review_preflight_result(messages),
                    "loaded_review_group_receipt_seen": _has_loaded_review_group_receipt(messages),
                    "review_tool_load_result_seen": any(isinstance(message, dict) and message.get("role") == "tool"
                        and "campus_reviews_preflight" in _find_text(message.get("content")) for message in messages),
                    "synthetic_user_prompt_seen": COURSE_MARKER in _find_text(messages),
                    "synthetic_tool_result_seen": _has_synthetic_tool_result(messages),
                    "job_user_prompt_seen": JOB_MARKER in _find_text(messages),
                    "job_tool_result_seen": _has_synthetic_tool_result(messages, job=True),
                    "material_search_result_seen": _has_material_result(messages),
                    "material_read_result_seen": _has_material_result(messages, read=True),
                    "assignment_result_seen": _has_assignment_result(messages),
                    "availability_result_seen": any(isinstance(message, dict) and message.get("role") == "tool"
                        and '"configured": true' in _find_text(message.get("content")) for message in messages),
                    "plan_preview_result_seen": _has_pending_plan_preview(messages),
                    "empty_search_user_prompt_seen": EMPTY_SEARCH_MARKER in _find_text(messages),
                    "empty_assignment_user_prompt_seen": EMPTY_ASSIGNMENT_MARKER in _find_text(messages),
                    "empty_assignment_result_seen": (any(isinstance(message, dict) and message.get("role") == "tool"
                        and '"items": []' in _find_text(message.get("content")) for message in messages)),
                    "assignment_page1_seen": ASSIGNMENT_ID in _find_text(messages),
                    "assignment_page2_seen": ASSIGNMENT_PAGE_LAST_ID in _find_text(messages),
                    "assignment_cursor_seen": bool(_assignment_cursor_in_messages(messages)),
                    "assignment_page_user_prompt_seen": ASSIGNMENT_PAGE_MARKER in _find_text(messages),
                    "empty_material_search_result_seen": any(isinstance(message, dict) and message.get("role") == "tool"
                        and '"items": []' in _find_text(message.get("content")) for message in messages),
                    "invalid_parameter_error_seen": _has_tool_error(messages, "INVALID_INPUT"),
                    "parameter_error_user_prompt_seen": PARAM_ERROR_MARKER in _find_text(messages),
                    "available_connection_sync_tool": "campus_connections_sync" in tools,
                    "expired_session_user_prompt_seen": "BENCH_EXPIRED_SESSION" in _find_text(messages),
                    "auth_required_error_seen": _has_tool_error(messages, "AUTH_REQUIRED"),
                }
                actual_number = state.add(record)
                if actual_number != call_number:
                    state.error_code = "concurrent_model_call"
                    return self._reply(409, {"error": {"message": "concurrent fixture request"}})
                if actual_number == 1:
                    if not record["model_ok"] or not record["api_key_ok"]:
                        state.error_code = "provider_identity_mismatch"
                        return self._reply(403, {"error": {"message": "fixture provider mismatch"}})
                    fixture_available = ((record["available_job_tool"] and record["job_user_prompt_seen"])
                                         if job_fixture else
                                         (record["review_user_prompt_seen"] and record["available_load_tools"]
                                          and (record["available_review_preflight_tool"] if not defer_tool_group
                                               else not record["available_review_preflight_tool"])) if review_fixture else
                                         (record["available_material_search_tool"] and record["available_material_read_tool"]
                                          and record["available_assignment_tool"] and record["available_availability_tool"]
                                          and record["available_plan_preview_tool"] and PLAN_FIXTURE_MARKER in _find_text(messages)) if plan_fixture else
                                         (record["available_material_search_tool"] and record["empty_search_user_prompt_seen"]) if empty_search_fixture else
                                         (record["available_assignment_tool"] and record["empty_assignment_user_prompt_seen"]) if empty_assignment_fixture else
                                         (record["available_assignment_tool"] and ASSIGNMENT_PAGE_MARKER in _find_text(messages)) if assignment_page_fixture else
                                         (record["available_material_read_tool"] and record["parameter_error_user_prompt_seen"]) if parameter_error_fixture else
                                         (record["available_connection_sync_tool"] and record["expired_session_user_prompt_seen"]) if expired_session_fixture else
                                         (record["available_material_search_tool"] and record["available_material_read_tool"]
                                          and MATERIAL_MARKER in _find_text(messages)) if material_fixture else
                                         (record["available_course_tool"] and record["synthetic_user_prompt_seen"]))
                    if not fixture_available:
                        state.error_code = "expected_tool_or_prompt_missing"
                        return self._reply(500, {"error": {"message": "expected fixture input missing"}})
                    if review_fixture:
                        if defer_tool_group:
                            return self._reply(200, {"id": "mock-review-load-tools-1", "object": "chat.completion",
                                "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                    "message": {"role": "assistant", "content": None, "tool_calls": [{
                                        "id": "bench-review-load-call", "type": "function", "function": {
                                            "name": "load_tools",
                                            "arguments": json.dumps({"group": "campus_reviews"}),
                                        }}]}, "finish_reason": "tool_calls"}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                        return self._reply(200, {"id": "mock-review-preflight-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-review-preflight-call", "type": "function", "function": {
                                        "name": "campus_reviews_preflight",
                                        "arguments": json.dumps({"course_id": COURSE_ID, "mode": "bank",
                                            "constraints": {"question_types": [{"type": "short_answer", "count": 2}]}}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if job_fixture:
                        return self._reply(200, {"id": "mock-e2e-job-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-job-call-1", "type": "function", "function": {
                                        "name": "campus_jobs_read",
                                        "arguments": json.dumps({"job_id": JOB_ID}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if material_fixture:
                        return self._reply(200, {"id": "mock-e2e-material-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-material-search-call", "type": "function", "function": {
                                        "name": "campus_materials_search",
                                        "arguments": json.dumps({"course_id": COURSE_ID, "query": "课程目标"}, ensure_ascii=False),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if plan_fixture:
                        return self._reply(200, {"id": "mock-e2e-plan-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-plan-material-search", "type": "function", "function": {
                                        "name": "campus_materials_search",
                                        "arguments": json.dumps({"course_id": COURSE_ID, "query": "课程目标"}, ensure_ascii=False),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if empty_search_fixture:
                        return self._reply(200, {"id": "mock-e2e-empty-search-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-empty-material-search", "type": "function", "function": {
                                        "name": "campus_materials_search",
                                        "arguments": json.dumps({"course_id": COURSE_ID, "query": "合成空结果关键词"}, ensure_ascii=False),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if empty_assignment_fixture:
                        return self._reply(200, {"id": "mock-e2e-empty-assignment-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-empty-assignment-list", "type": "function", "function": {
                                        "name": "campus_assignments_list",
                                        "arguments": json.dumps({"order_by": "due_soon", "limit": 10}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if assignment_page_fixture:
                        return self._reply(200, {"id": "mock-assignment-page-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-assignment-page-call-1", "type": "function", "function": {
                                        "name": "campus_assignments_list",
                                        "arguments": json.dumps({"order_by": "due_soon", "limit": 2}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if parameter_error_fixture:
                        return self._reply(200, {"id": "mock-e2e-param-error-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-invalid-material-read", "type": "function", "function": {
                                        "name": "campus_materials_read",
                                        "arguments": json.dumps({"resource_id": MATERIAL_ID, "expected_version": 0,
                                                                 "chunk_ids": [MATERIAL_CHUNK_ID]}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if expired_session_fixture:
                        return self._reply(200, {"id": "mock-e2e-expired-session-1", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-expired-session-sync", "type": "function", "function": {
                                        "name": "campus_connections_sync",
                                        "arguments": json.dumps({"platform": "xzzd", "kinds": ["assignments"]}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    return self._reply(200, {"id": "mock-e2e-1", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "bench-course-call-1", "type": "function", "function": {
                                    "name": "campus_courses_list",
                                    "arguments": json.dumps({"query": COURSE_MARKER}, ensure_ascii=False),
                                }}]}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if actual_number == 2:
                    if assignment_page_fixture:
                        cursor = _assignment_cursor_in_messages(messages)
                        if not cursor:
                            state.error_code = "assignment_page_cursor_not_returned"
                            return self._reply(500, {"error": {"message": "first page cursor missing"}})
                        return self._reply(200, {"id": "mock-assignment-page-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-assignment-page-call-2", "type": "function", "function": {
                                        "name": "campus_assignments_list",
                                        "arguments": json.dumps({"order_by": "due_soon", "limit": 2, "cursor": cursor}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if review_fixture:
                        if defer_tool_group:
                            if not record["available_review_preflight_tool"] or not record["review_tool_load_result_seen"]:
                                state.error_code = "review_tools_not_loaded"
                                return self._reply(500, {"error": {"message": "review tool schema missing after load"}})
                            return self._reply(200, {"id": "mock-review-preflight-2", "object": "chat.completion",
                                "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                    "message": {"role": "assistant", "content": None, "tool_calls": [{
                                        "id": "bench-review-preflight-call", "type": "function", "function": {
                                            "name": "campus_reviews_preflight",
                                            "arguments": json.dumps({"course_id": COURSE_ID, "mode": "bank",
                                                "constraints": {"question_types": [{"type": "short_answer", "count": 2}]}}),
                                        }}]}, "finish_reason": "tool_calls"}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                        if not record["review_preflight_result_seen"]:
                            state.error_code = "review_preflight_result_not_returned"
                            return self._reply(500, {"error": {"message": "review preflight result missing"}})
                        return self._reply(200, {"id": "mock-review-final-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content":
                                    "本地复习卷预检已完成，结果中包含材料缺口与能否生成的状态。没有生成或导出复习卷。"},
                                "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if expired_session_fixture:
                        if not record["auth_required_error_seen"]:
                            state.error_code = "expired_session_error_not_returned"
                            return self._reply(500, {"error": {"message": "AUTH_REQUIRED missing from tool result"}})
                        return self._reply(200, {"id": "mock-e2e-expired-session-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content":
                                    "学在浙大会话已失效，课程作业查询已暂停且没有重复发起同步。请在受控校园窗口完成登录后，再告诉我继续查询。"},
                                "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if empty_assignment_fixture:
                        if not record["empty_assignment_result_seen"]:
                            state.error_code = "empty_assignment_result_not_returned"
                            return self._reply(500, {"error": {"message": "empty assignment result missing"}})
                        return self._reply(200, {"id": "mock-e2e-empty-assignment-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content":
                                    "本机缓存中没有可核验的作业；缓存完整性未确认且数据可能过期，这不代表学校端没有作业。我没有刷新校园数据。"},
                                "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if parameter_error_fixture:
                        if not record["invalid_parameter_error_seen"]:
                            state.error_code = "invalid_parameter_error_not_returned"
                            return self._reply(500, {"error": {"message": "validation error missing from tool result"}})
                        return self._reply(200, {"id": "mock-e2e-param-error-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-corrected-material-read", "type": "function", "function": {
                                        "name": "campus_materials_read",
                                        "arguments": json.dumps({"resource_id": MATERIAL_ID, "expected_version": 2,
                                                                 "chunk_ids": [MATERIAL_CHUNK_ID]}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if empty_search_fixture:
                        if not record["empty_material_search_result_seen"]:
                            state.error_code = "empty_material_search_result_not_returned"
                            return self._reply(500, {"error": {"message": "empty synthetic result missing"}})
                        return self._reply(200, {"id": "mock-e2e-empty-search-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content":
                                    "本地缓存中没有找到匹配的课程资料；这不代表课程要求不存在，当前也没有刷新校园数据。"},
                                "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if plan_fixture:
                        if not record["material_search_result_seen"]:
                            state.error_code = "material_search_result_not_returned"
                            return self._reply(500, {"error": {"message": "synthetic search result missing"}})
                        return self._reply(200, {"id": "mock-e2e-plan-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-plan-material-read", "type": "function", "function": {
                                        "name": "campus_materials_read",
                                        "arguments": json.dumps({"resource_id": MATERIAL_ID, "expected_version": 2,
                                                                 "chunk_ids": [MATERIAL_CHUNK_ID]}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    if material_fixture:
                        if not record["material_search_result_seen"]:
                            state.error_code = "material_search_result_not_returned"
                            return self._reply(500, {"error": {"message": "synthetic search result missing"}})
                        return self._reply(200, {"id": "mock-e2e-material-2", "object": "chat.completion",
                            "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                                "message": {"role": "assistant", "content": None, "tool_calls": [{
                                    "id": "bench-material-read-call", "type": "function", "function": {
                                        "name": "campus_materials_read",
                                        "arguments": json.dumps({"resource_id": MATERIAL_ID, "expected_version": 2,
                                                                 "chunk_ids": [MATERIAL_CHUNK_ID]}),
                                    }}]}, "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    result_seen = record["job_tool_result_seen"] if job_fixture else record["synthetic_tool_result_seen"]
                    if not result_seen:
                        state.error_code = "synthetic_tool_result_not_returned"
                    final = (("合成后台任务状态：succeeded。" if result_seen else "未收到合成后台任务终态。")
                             if job_fixture else
                             (f"本地合成课程查询结果：{COURSE_TITLE}。" if result_seen else
                              "工具结果未包含预期的合成课程标记。"))
                    return self._reply(200, {"id": "mock-e2e-2", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": final}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if actual_number == 3 and assignment_page_fixture:
                    if not record["assignment_page1_seen"] or not record["assignment_page2_seen"]:
                        state.error_code = "assignment_final_page_not_returned"
                        return self._reply(500, {"error": {"message": "page results missing before final"}})
                    return self._reply(200, {"id": "mock-assignment-page-final", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content":
                                "按截止时间已读取本机缓存中的3项合成作业，第一页和续页记录无重复，当前结果仅覆盖本机缓存。"},
                            "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if actual_number == 3 and material_fixture:
                    if not record["material_read_result_seen"]:
                        state.error_code = "material_read_result_not_returned"
                    return self._reply(200, {"id": "mock-e2e-material-3", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content":
                                f"课程大纲原文：{MATERIAL_TEXT}", "tool_calls": []}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if actual_number == 3 and review_fixture and defer_tool_group:
                    if not record["review_preflight_result_seen"]:
                        state.error_code = "review_preflight_result_not_returned"
                        return self._reply(500, {"error": {"message": "review preflight result missing after discovery"}})
                    return self._reply(200, {"id": "mock-review-final-3", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content":
                                "本地复习卷预检已完成，结果中包含材料缺口与能否生成的状态。没有生成或导出复习卷。"},
                            "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if plan_fixture and actual_number == 3:
                    if not record["material_read_result_seen"]:
                        state.error_code = "material_read_result_not_returned"
                        return self._reply(500, {"error": {"message": "synthetic material result missing"}})
                    return self._reply(200, {"id": "mock-e2e-plan-3", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "bench-plan-assignments", "type": "function", "function": {
                                    "name": "campus_assignments_list",
                                    "arguments": json.dumps({"course_id": COURSE_ID, "limit": 10,
                                                             "include_instructions": True, "order_by": "due_soon"}),
                                }}]}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if parameter_error_fixture and actual_number == 3:
                    if not record["material_read_result_seen"]:
                        state.error_code = "corrected_material_result_not_returned"
                        return self._reply(500, {"error": {"message": "corrected material result missing"}})
                    return self._reply(200, {"id": "mock-e2e-param-error-3", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": f"课程资料原文：{MATERIAL_TEXT}。"},
                            "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if plan_fixture and actual_number == 4:
                    if not record["assignment_result_seen"]:
                        state.error_code = "assignment_result_not_returned"
                        return self._reply(500, {"error": {"message": "synthetic assignment result missing"}})
                    return self._reply(200, {"id": "mock-e2e-plan-4", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "bench-plan-availability", "type": "function", "function": {
                                    "name": "campus_availability_read", "arguments": "{}",
                                }}]}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if plan_fixture and actual_number == 5:
                    if not record["availability_result_seen"]:
                        state.error_code = "availability_result_not_returned"
                        return self._reply(500, {"error": {"message": "synthetic availability result missing"}})
                    from datetime import date, timedelta
                    start = date.today().isoformat()
                    end = (date.today() + timedelta(days=6)).isoformat()
                    return self._reply(200, {"id": "mock-e2e-plan-5", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "bench-plan-preview", "type": "function", "function": {
                                    "name": "campus_plans_preview",
                                    "arguments": json.dumps({"period": "dates", "start_date": start, "end_date": end}),
                                }}]}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                if plan_fixture and actual_number == 6:
                    if not record["plan_preview_result_seen"]:
                        state.error_code = "pending_plan_preview_not_returned"
                        return self._reply(500, {"error": {"message": "pending plan preview missing"}})
                    return self._reply(200, {"id": "mock-e2e-plan-6", "object": "chat.completion",
                        "created": 0, "model": MODEL_ID, "choices": [{"index": 0,
                            "message": {"role": "assistant", "content":
                                f"依据课程要求‘{MATERIAL_TEXT}’，已为‘{ASSIGNMENT_TITLE}’结合可用时间生成一周计划预览；计划仍待你确认，尚未应用。"},
                                "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                state.error_code = "model_call_limit_exceeded"
                return self._reply(429, {"error": {"message": "scripted model call limit exceeded"}})
            except (ValueError, TypeError, json.JSONDecodeError):
                state.error_code = "malformed_provider_request"
                return self._reply(400, {"error": {"message": "malformed fixture request"}})

    return Handler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, *, method: str = "GET", payload: dict | None = None,
               headers: dict | None = None, timeout: float = 5) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json"}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        opener = _API_OPENER or urllib.request.build_opener()
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise SafeFailure("api_response", "response_too_large")
            return response.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(2_000_001)
            body = json.loads(raw or b"{}")
        except (ValueError, OSError):
            body = {}
        return exc.code, body


def _check(status: int, expected: int, stage: str) -> None:
    if status != expected:
        raise SafeFailure(stage, f"http_{status}_expected_{expected}")


def _terminate_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _set_isolated_environment(tmp: pathlib.Path, api_port: int) -> dict[str, str]:
    env = os.environ.copy()
    # Do not let this run inherit any personal data locations or campus adapters.
    for key in list(env):
        if key.startswith(("XZZD_", "ZYKT_", "CAMPUS_")) or key in {
            "STUDY_DATABASE_URL", "STUDY_FILES_DIR", "STUDY_SECRETS_DIR",
            "STUDY_CONFIG_DEFAULTS", "STUDY_EXAMPLES_DIR", "STUDY_SYNC_ADAPTERS",
            "DEEPSEEK_API_KEY", "DEEPSEEK_SEARCH_API_KEY", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        }:
            env.pop(key, None)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        "STUDY_DATABASE_URL": "sqlite:///" + (tmp / "study.sqlite").as_posix(),
        "STUDY_FILES_DIR": str(tmp / "files"),
        "STUDY_SECRETS_DIR": str(tmp / "secrets"),
        "CAMPUS_AGENT_HOME": str(tmp / "agent-home"),
        "CAMPUS_BRIDGE_URL": f"http://127.0.0.1:{api_port}",
        "CAMPUS_WEB_ORIGINS": f"http://127.0.0.1:{api_port}",
        "CAMPUS_MAX_MODEL_CALLS": str(MAX_MODEL_CALLS),
        "CAMPUS_MAX_TOOL_CALLS": str(MAX_MODEL_CALLS),
        "CAMPUS_TURN_TIMEOUT_SECONDS": str(TURN_TIMEOUT_SECONDS),
        "CAMPUS_MAX_OUTPUT_TOKENS": "512",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    })
    return env


def _prepare_database(env: dict[str, str], fixture: str) -> None:
    if not API_PYTHON.is_file():
        raise SafeFailure("isolated_database_setup", "api_virtualenv_missing")
    for args, stage in (([str(API_PYTHON), "-m", "alembic", "upgrade", "head"], "migration"),
                        ([str(API_PYTHON), "-m", "app.bootstrap"], "bootstrap")):
        result = subprocess.run(args, cwd=API_DIR, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=90, check=False)
        if result.returncode:
            raise SafeFailure("isolated_database_" + stage, "command_failed")
    if fixture == "JOB-01":
        seed = ("from app.db import SessionLocal; from app.models import Course, Job; "
                f"db=SessionLocal(); db.add(Course(id={COURSE_ID!r}, title={COURSE_TITLE!r}, term='BENCH-SYNTHETIC')); "
                f"db.add(Job(id={JOB_ID!r}, type='sync', status='running', payload=None)); "
                "db.commit(); db.close()")
    elif fixture in {"BUS-03", "ERR-09", "TOOL-REV-01"}:
        seed = (
            "from app.db import SessionLocal; from app.models import Course, Resource, ContentChunk; "
            f"db=SessionLocal(); db.add(Course(id={COURSE_ID!r}, title={COURSE_TITLE!r}, term='BENCH-SYNTHETIC')); "
            f"db.add(Resource(id={MATERIAL_ID!r}, course_id={COURSE_ID!r}, kind='syllabus', title='BENCH-ONLY-SYLLABUS', "
            "version=2, parse_status='ready', access_level='full')); db.flush(); "
            f"db.add(ContentChunk(id={MATERIAL_CHUNK_ID!r}, resource_id={MATERIAL_ID!r}, resource_version=2, "
            f"ordinal=0, text={MATERIAL_TEXT!r}, locator={{'section':'课程目标'}}, provenance={{'kind':'synthetic'}})); "
            "db.commit(); db.close()"
        )
    elif fixture == "ERR-11":
        seed = (
            "from app.db import SessionLocal; from app.models import Course; "
            f"db=SessionLocal(); db.add(Course(id={COURSE_ID!r}, title={COURSE_TITLE!r}, term='BENCH-SYNTHETIC')); "
            "db.commit(); db.close()"
        )
    elif fixture == "BUS-12":
        seed = (
            "from datetime import datetime, timedelta, timezone; "
            "from app.db import SessionLocal; from app.models import Course, Assignment; "
            "db=SessionLocal(); now=datetime.now(timezone.utc); "
            f"db.add(Course(id={COURSE_ID!r}, title={COURSE_TITLE!r}, term='BENCH-SYNTHETIC')); db.flush(); "
            f"db.add_all([Assignment(id={ASSIGNMENT_ID!r}, course_id={COURSE_ID!r}, title={ASSIGNMENT_TITLE!r}, "
            "due_at=now+timedelta(days=1), due_precision='exact', observed_at=now), "
            f"Assignment(id='bench_synthetic_assignment_page2', course_id={COURSE_ID!r}, title='BENCH-ONLY-第二页作业', "
            "due_at=now+timedelta(days=2), due_precision='exact', observed_at=now), "
            f"Assignment(id={ASSIGNMENT_PAGE_LAST_ID!r}, course_id={COURSE_ID!r}, title='BENCH-ONLY-第三页作业', "
            "due_at=now+timedelta(days=3), due_precision='exact', observed_at=now)]); "
            "db.commit(); db.close()"
        )
    elif fixture == "ERR-10":
        seed = (
            "from app.db import SessionLocal; from app.models import Connection; "
            "db=SessionLocal(); "
            "db.query(Connection).filter(Connection.platform == 'xzzd').update({"
            "'status':'auth_required','last_error':{'code':'AUTH_REQUIRED','message':'synthetic expired session'},"
            "'recovery_action':{'kind':'manual_login'}}); db.commit(); db.close()"
        )
    elif fixture == "MULTI-02":
        seed = (
            "from datetime import datetime, timedelta, timezone; "
            "from app.db import SessionLocal; from app.models import Course, Resource, ContentChunk, Assignment, LearningTask, Availability; "
            "db=SessionLocal(); now=datetime.now(timezone.utc); due=now+timedelta(days=4); "
            f"db.add(Course(id={COURSE_ID!r}, title='数据结构（合成测试）', term='BENCH-SYNTHETIC')); db.flush(); "
            f"db.add(Resource(id={MATERIAL_ID!r}, course_id={COURSE_ID!r}, kind='syllabus', title='BENCH-ONLY-SYLLABUS', "
            "version=2, parse_status='ready', access_level='full')); db.flush(); "
            f"db.add(ContentChunk(id={MATERIAL_CHUNK_ID!r}, resource_id={MATERIAL_ID!r}, resource_version=2, "
            f"ordinal=0, text={MATERIAL_TEXT!r}, locator={{'section':'课程目标'}}, provenance={{'kind':'synthetic'}})); "
            f"db.add(Assignment(id={ASSIGNMENT_ID!r}, course_id={COURSE_ID!r}, title={ASSIGNMENT_TITLE!r}, "
            "instructions='完成课程要求中的基础练习。', due_at=due, due_precision='exact', submission_status='not_submitted', "
            "fact_source='platform', confidence=1.0, observed_at=now)); "
            f"db.add(LearningTask(id={LEARNING_TASK_ID!r}, course_id={COURSE_ID!r}, type='assignment', "
            f"title={ASSIGNMENT_TITLE!r}, estimated_minutes=60, due_at=due, due_precision='exact', "
            "prerequisite_ids=[], status='pending', fact_source='platform')); "
            "db.add(Availability(id=1, timezone='Asia/Shanghai', weekly_windows=["
            "{'weekday':1,'start':'18:00','end':'22:00'},{'weekday':2,'start':'18:00','end':'22:00'},"
            "{'weekday':3,'start':'18:00','end':'22:00'},{'weekday':4,'start':'18:00','end':'22:00'},"
            "{'weekday':5,'start':'18:00','end':'22:00'},{'weekday':6,'start':'18:00','end':'22:00'},"
            "{'weekday':7,'start':'18:00','end':'22:00'}], date_exceptions=[], daily_limit_minutes=120)); "
            "db.commit(); db.close()"
        )
    else:
        seed = (
            "from app.db import SessionLocal; from app.models import Course; "
            f"db=SessionLocal(); db.add(Course(id={COURSE_ID!r}, title={COURSE_TITLE!r}, term='BENCH-SYNTHETIC')); "
            "db.commit(); db.close()"
        )
    result = subprocess.run([str(API_PYTHON), "-c", seed], cwd=API_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=30, check=False)
    if result.returncode:
        raise SafeFailure("synthetic_data_setup", "synthetic_fixture_seed_failed")


def _spawn_api(env: dict[str, str], port: int, log_path: pathlib.Path) -> subprocess.Popen:
    log = log_path.open("wb")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen([str(API_PYTHON), "-m", "uvicorn", "app.main:app",
                                    "--host", "127.0.0.1", "--port", str(port),
                                    "--no-access-log"], cwd=API_DIR, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
    finally:
        log.close()
    return process


def _seed_job_receipt(env: dict[str, str], conversation_id: str) -> None:
    script = (
        "from app.db import SessionLocal; from app.models import AgentJobRequest; "
        f"db=SessionLocal(); db.add(AgentJobRequest(id={JOB_RECEIPT_ID!r}, "
        f"conversation_id={conversation_id!r}, request_hash={'a' * 64!r}, course_id={COURSE_ID!r}, job_id={JOB_ID!r})); "
        "db.commit(); db.close()"
    )
    result = subprocess.run([str(API_PYTHON), "-c", script], cwd=API_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=30, check=False)
    if result.returncode:
        raise SafeFailure("synthetic_job_receipt", "job_ownership_fixture_failed")


def _complete_job_later(database_path: pathlib.Path) -> None:
    time.sleep(0.6)
    with sqlite3.connect(database_path, timeout=5) as db:
        db.execute("UPDATE jobs SET status='succeeded', progress=1, "
                   "finished_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=? AND status='running'", (JOB_ID,))


def _main(timeout: int, fixture: str = "course-list-smoke", defer_tool_group: str | None = None) -> dict:
    global _API_OPENER
    if defer_tool_group and fixture not in {"TOOL-REV-01", "MULTI-02"}:
        raise ValueError("tool exposure treatment is only valid for TOOL-REV-01 and MULTI-02")
    wall_started = time.perf_counter()
    # A fresh per-run cookie jar reproduces the browser-owned local API session;
    # it cannot read or reuse any existing browser cookie.
    cookie_jar = http.cookiejar.CookieJar()
    _API_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    result = {
        "schema_version": 1,
        "experiment": "campus-agent-scripted-mock-e2e",
        "scenario_id": fixture if fixture in {"JOB-01", "BUS-03", "BUS-12", "MULTI-02", "ERR-02", "ERR-09", "ERR-10", "ERR-11", "TOOL-REV-01"} else None,
        "benchmark_tool_groups": [defer_tool_group] if defer_tool_group else [],
        "classification": {
            "model": "scripted mock model",
            "harness": "real Agent API and real Harness Node subprocess",
            "tools": ("real local reviews.preflight read-only executor and load_tools exposure treatment" if fixture == "TOOL-REV-01" else
                      "real local connections.sync expired-session rejection path; no external connector call" if fixture == "ERR-10" else
                      "real local materials.read parameter-validation and recovery path" if fixture == "ERR-09" else
                      "real local empty-result materials.search executor" if fixture == "ERR-02" else
                      "real local assignments.list executor over an intentionally empty synthetic cache" if fixture == "ERR-11" else
                      "real local assignments.list executor with cursor-dependent synthetic pages" if fixture == "BUS-12" else
                      "real local materials, assignments and read-only preview executors" if fixture == "MULTI-02" else
                      "real local read-only jobs.read executor" if fixture == "JOB-01" else
                      "real local read-only materials.search and materials.read executors" if fixture == "BUS-03" else
                      "real local read-only courses.list executor"),
            "data": "synthetic SQLite fixture only",
            "campus_services": "not contacted",
        },
        "status": "failed",
        "verified_success": False,
    }
    stage = "temporary_workspace"
    api_process = None
    api_source_check = None
    api_port = _free_port()
    state = ScriptState()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _scripted_handler(state, fixture, defer_tool_group))
    server.daemon_threads = True
    provider_port = int(server.server_address[1])
    provider_thread = threading.Thread(target=server.serve_forever, name="bench-scripted-provider", daemon=True)
    provider_thread.start()
    try:
        temp_root = ROOT / "var" / "campus-bench"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mock-e2e-", dir=temp_root) as temp, contextlib.ExitStack() as process_cleanup:
            tmp = pathlib.Path(temp).resolve()
            env = _set_isolated_environment(tmp, api_port)
            if defer_tool_group:
                env["CAMPUS_BENCH_ALLOW_TOOL_GROUPS"] = "1"
            if fixture == "ERR-10":
                env["CAMPUS_MAX_MODEL_CALLS"] = "2"
                env["CAMPUS_MAX_TOOL_CALLS"] = "1"
            _prepare_database(env, fixture)
            stage = "api_start"
            api_process = _spawn_api(env, api_port, tmp / "api.log")
            process_cleanup.callback(_terminate_tree, api_process)
            base = f"http://127.0.0.1:{api_port}"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if api_process.poll() is not None:
                    raise SafeFailure(stage, "api_process_exited")
                try:
                    status, health_body = _http_json(base + "/api/v1/health", timeout=1)
                    if status == 200:
                        api_source_check = api_agent_source_fingerprint_status(
                            health_body, agent_api_source_sha256())
                        if api_source_check["status"] != "matched":
                            raise SafeFailure("api_source_fingerprint",
                                "api_agent_source_" + api_source_check["status"])
                        break
                except (OSError, TimeoutError, urllib.error.URLError):
                    pass
                time.sleep(0.2)
            else:
                raise SafeFailure(stage, "api_start_timeout")

            stage = "provider_config"
            status, provider = _http_json(base + "/api/v1/ai/providers", method="POST", payload={
                "name": "Campus benchmark local scripted fixture",
                "protocol": "openai_chat_completions",
                "base_url": f"http://127.0.0.1:{provider_port}/v1",
                "model": MODEL_ID,
                "timeout_seconds": 5,
                "enabled": True,
                "api_key": LOCAL_API_KEY,
            })
            _check(status, 201, stage)
            provider_id = provider.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                raise SafeFailure(stage, "provider_id_missing")

            stage = "conversation_create"
            conversation_payload = {"provider_id": provider_id, "send_to_provider": True}
            if defer_tool_group:
                conversation_payload["benchmark_tool_groups"] = [defer_tool_group]
            status, conversation = _http_json(base + "/api/v1/agent/conversations", method="POST",
                payload=conversation_payload)
            _check(status, 201, stage)
            conversation_id = conversation.get("id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise SafeFailure(stage, "conversation_id_missing")

            if fixture == "JOB-01":
                stage = "job_ownership_fixture"
                _seed_job_receipt(env, conversation_id)

            stage = "turn_submit"
            prompt = (f"请检查当前会话刚才的后台同步任务状态，测试标记 {JOB_MARKER}。"
                      "若仍运行中不能说已完成；不要创建或再次触发同步。"
                      if fixture == "JOB-01" else
                      f"请读取本地合成课程 {COURSE_ID} 的课程要求与作业截止时间，结合已配置的可用时间生成一周计划预览；只预览，绝不应用。测试标记 {PLAN_FIXTURE_MARKER}。" if fixture == "MULTI-02" else
                      f"请查找本地课程 {COURSE_ID} 中关键词‘合成空结果关键词’对应的资料；如果本地没有匹配项，请说明缓存范围，不要声称课程没有资料，也不要刷新或同步。测试标记 {EMPTY_SEARCH_MARKER}。" if fixture == "ERR-02" else
                      f"请只读查询本机作业缓存并说明是否有可核验作业，测试标记 {EMPTY_ASSIGNMENT_MARKER}。若缓存为空或过期，要说明它不能证明学校端没有作业；不要刷新或同步。" if fixture == "ERR-11" else
                      f"请读取本机缓存中按截止时间排序的完整合成作业清单，测试标记 {ASSIGNMENT_PAGE_MARKER}；如有下一页，必须使用工具返回的游标续读，避免重复或漏项；不刷新或同步。" if fixture == "BUS-12" else
                      f"请读取学在浙大的最新课程作业，测试标记 {EXPIRED_SESSION_MARKER}。若会话失效，请停止自动重试，说明需要我登录后才能继续，不要重复同步。" if fixture == "ERR-10" else
                      f"请读取本地合成课程 {COURSE_ID} 的课程资料原文，测试标记 {PARAM_ERROR_MARKER}。遇到工具参数错误后，检查错误并修正参数重试一次，不要重复使用错误参数。" if fixture == "ERR-09" else
                      f"检查本地合成课程 {COURSE_ID} 的复习卷生成条件，只做无副作用预检并说明缺口；不要生成/导出，不要联网。测试标记 {REVIEW_FIXTURE_MARKER}。" if fixture == "TOOL-REV-01" else
                      f"请在本地合成课程 {COURSE_ID} 的资料中查找课程目标并读取命中的原文片段，标记 {MATERIAL_MARKER}。" if fixture == "BUS-03" else
                      f"请只读本地课程库中标记为 {COURSE_MARKER} 的课程，并告诉我课程名称；不要联网。")
            status, submitted = _http_json(base + f"/api/v1/agent/conversations/{conversation_id}/turns",
                method="POST", payload={"text": prompt},
            headers={"Idempotency-Key": ("mock-e2e-assignment-pages-1" if fixture == "BUS-12" else
                                          "mock-e2e-empty-assignment-1" if fixture == "ERR-11" else
                                          "mock-e2e-expired-session-1" if fixture == "ERR-10" else
                                              "mock-e2e-param-recovery-1" if fixture == "ERR-09" else
                                              "mock-e2e-empty-search-1" if fixture == "ERR-02" else
                                              "mock-e2e-plan-preview-1" if fixture == "MULTI-02" else
                                              "mock-e2e-job-read-1" if fixture == "JOB-01" else
                                              "mock-e2e-materials-read-1" if fixture == "BUS-03" else
                                              "mock-e2e-course-list-1")})
            _check(status, 202, stage)
            turn_id = submitted.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise SafeFailure(stage, "turn_id_missing")

            job_worker = None
            if fixture == "JOB-01":
                job_worker = threading.Thread(target=_complete_job_later,
                    args=(pathlib.Path(env["STUDY_DATABASE_URL"].removeprefix("sqlite:///")),),
                    name="bench-synthetic-job-worker", daemon=True)
                job_worker.start()

            stage = "turn_wait"
            deadline = time.monotonic() + timeout
            turn = submitted
            while time.monotonic() < deadline:
                status, turn = _http_json(base + f"/api/v1/agent/turns/{turn_id}", timeout=5)
                _check(status, 200, "turn_poll")
                if turn.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(POLL_SECONDS)
            else:
                with contextlib.suppress(Exception):
                    _http_json(base + f"/api/v1/agent/turns/{turn_id}/cancel", method="POST", payload={}, timeout=2)
                raise SafeFailure(stage, "turn_timeout")

            events = (turn.get("events") or {}).get("items", [])
            model_events = [item for item in events if item.get("kind") == "model_call"]
            tool_events = [item for item in events if item.get("kind") == "tool_result"]
            execution_trace = []
            for item in events:
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                nested = data.get("event") if isinstance(data.get("event"), dict) else {}
                nested_data = nested.get("data") if isinstance(nested.get("data"), dict) else {}
                result = data.get("result") if isinstance(data.get("result"), dict) else {}
                if not result and isinstance(nested_data.get("result"), dict):
                    result = nested_data["result"]
                error = result.get("error") if isinstance(result.get("error"), dict) else {}
                execution_trace.append({
                    "kind": item.get("kind"), "harness_event": nested.get("type"),
                    "tool": data.get("name") or nested_data.get("name") or nested_data.get("tool"),
                    "status": result.get("status") or nested_data.get("status"),
                    "error_code": error.get("code") or nested_data.get("error_code") or data.get("code"),
                })
            selected = []
            harness_tool_calls = []
            model_trace = []
            for event in model_events:
                data = event.get("data") or {}
                selected.extend(name for name in data.get("selected_tools", []) if isinstance(name, str))
                model_trace.append({"duration_ms": data.get("duration_ms"),
                                    "available_tool_count": len(data.get("available_tools", [])),
                                    "selected_tools": [name for name in data.get("selected_tools", []) if isinstance(name, str)]})
            for event in events:
                harness_event = ((event.get("data") or {}).get("event") or {})
                payload = harness_event.get("data") or {}
                if harness_event.get("type") == "tool/call" and isinstance(payload.get("name"), str):
                    harness_tool_calls.append(payload["name"])
            if job_worker:
                job_worker.join(timeout=5)
            verified_tool = False
            result_status = None
            material_results = {}
            plan_results = {}
            assignment_results = []
            for event in tool_events:
                data = event.get("data") or {}
                returned = (data.get("result") or {}).get("data") or {}
                if fixture == "JOB-01" and data.get("name") == "jobs.read":
                    result_status = returned.get("status")
                    verified_tool = data.get("result", {}).get("status") in {"ok", "partial"} and result_status == "succeeded"
                elif fixture in {"BUS-03", "ERR-09"} and data.get("name") in {"materials.search", "materials.read"}:
                    material_results[data["name"]] = data.get("result") or {}
                elif fixture == "MULTI-02" and data.get("name") in {
                    "materials.search", "materials.read", "assignments.list", "availability.read", "plans.preview"
                }:
                    plan_results[data["name"]] = data.get("result") or {}
                elif fixture == "BUS-12" and data.get("name") == "assignments.list":
                    assignment_results.append(data.get("result") or {})
                elif fixture != "JOB-01" and data.get("name") == "courses.list":
                    verified_tool = any(item.get("id") == COURSE_ID and item.get("title") == COURSE_TITLE
                                         for item in returned.get("items", []) if isinstance(item, dict))
            provider_requests = state.snapshot()
            final_text = turn.get("response") or ""
            status_name = turn.get("status")
            job_call_args = []
            material_calls = []
            plan_calls = []
            step_trace = []
            for event in tool_events:
                data = event.get("data") or {}
                tool_result = data.get("result") or {}
                result_data = tool_result.get("data") or {}
                items = result_data.get("items") if isinstance(result_data, dict) else None
                arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
                error = tool_result.get("error") if isinstance(tool_result.get("error"), dict) else None
                missing = tool_result.get("missing") if isinstance(tool_result.get("missing"), list) else []
                warnings = tool_result.get("warnings") if isinstance(tool_result.get("warnings"), list) else []
                coverage = tool_result.get("coverage") if isinstance(tool_result.get("coverage"), dict) else {}
                coverage_missing = coverage.get("missing") if isinstance(coverage.get("missing"), list) else []
                step_trace.append({"tool": data.get("name"), "duration_ms": data.get("duration_ms"),
                                   "status": tool_result.get("status"), "argument_fields": sorted(arguments)[:40],
                                   "result_item_count": len(items) if isinstance(items, list) else None,
                                   "missing_count": len(missing), "warning_count": len(warnings),
                                   "coverage_complete": coverage.get("complete"),
                                   "coverage_missing_count": len(coverage_missing),
                                   "error_code": error.get("code") if error else None})
            if fixture == "TOOL-REV-01":
                preflight_event = next((event for event in tool_events
                    if (event.get("data") or {}).get("name") == "reviews.preflight"), None)
                preflight_result = ((preflight_event or {}).get("data") or {}).get("result") or {}
                preflight_data = preflight_result.get("data") if isinstance(preflight_result.get("data"), dict) else {}
                verified_tool = (preflight_result.get("status") in {"ok", "partial"}
                                 and isinstance(preflight_data.get("can_generate"), bool))
                expected_tools = (["load_tools", "campus_reviews_preflight"] if defer_tool_group
                                  else ["campus_reviews_preflight"])
                expected_harness_calls = (["load_tools", "campus_reviews_preflight"] if defer_tool_group
                                          else ["campus_reviews_preflight"])
                expected_requests = 3 if defer_tool_group else 2
                load_step = provider_requests[1] if defer_tool_group and len(provider_requests) > 1 else {}
                result_step = provider_requests[-1] if provider_requests else {}
                success = (
                    status_name == "succeeded" and verified_tool and selected == expected_tools
                    and harness_tool_calls == expected_harness_calls
                    and len(tool_events) == 1
                    and len(model_events) == expected_requests and len(provider_requests) == expected_requests
                    and (provider_requests[0].get("available_review_preflight_tool") is (not bool(defer_tool_group)))
                    and (not defer_tool_group or (not provider_requests[0].get("available_review_preflight_tool")
                         and load_step.get("available_review_preflight_tool")
                         and load_step.get("review_tool_load_result_seen")))
                    and result_step.get("review_preflight_result_seen")
                    and "预检已完成" in final_text and "没有生成或导出" in final_text
                    and "reviews.generate" not in selected and "reviews.export" not in selected
                )
            elif fixture == "JOB-01":
                for event in events:
                    payload = ((event.get("data") or {}).get("event") or {}).get("data") or {}
                    if ((event.get("data") or {}).get("event") or {}).get("type") == "tool/call" and payload.get("name") == "campus_jobs_read":
                        try:
                            job_call_args.append(json.loads(payload.get("arguments") or "{}"))
                        except (TypeError, ValueError):
                            job_call_args.append({})
                success = (status_name == "succeeded" and verified_tool and result_status == "succeeded"
                           and "campus_jobs_read" in selected and len(model_events) == 2
                           and len(provider_requests) == 2 and len(job_call_args) == 1
                           and set(job_call_args[0]) == {"job_id"}
                           and provider_requests[0].get("available_job_tool")
                           and provider_requests[0].get("job_user_prompt_seen")
                           and provider_requests[1].get("job_tool_result_seen")
                           and not any(name == "campus_connections_sync" for name in selected))
            elif fixture == "BUS-03":
                for event in events:
                    harness_event = ((event.get("data") or {}).get("event") or {})
                    payload = harness_event.get("data") or {}
                    if harness_event.get("type") != "tool/call":
                        continue
                    try:
                        arguments = payload.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                    material_calls.append({"name": payload.get("name"), "arguments": arguments})
                search_data = material_results.get("materials.search", {}).get("data") or {}
                read_data = material_results.get("materials.read", {}).get("data") or {}
                hits = search_data.get("items") or []
                read_items = read_data.get("items") or []
                material_calls_valid = (
                    len(material_calls) == 2
                    and material_calls[0].get("name") == "campus_materials_search"
                    and material_calls[0].get("arguments", {}).get("course_id") == COURSE_ID
                    and material_calls[0].get("arguments", {}).get("query") == "课程目标"
                    and material_calls[1].get("name") == "campus_materials_read"
                    and material_calls[1].get("arguments") == {"resource_id": MATERIAL_ID,
                        "expected_version": 2, "chunk_ids": [MATERIAL_CHUNK_ID]})
                verified_tool = (len(hits) == 1 and hits[0].get("resource_id") == MATERIAL_ID
                                 and len(read_items) == 1 and read_items[0].get("id") == MATERIAL_CHUNK_ID
                                 and read_items[0].get("text") == MATERIAL_TEXT
                                 and len(material_results) == 2
                                 and all(value.get("status") in {"ok", "partial"}
                                         and value.get("error") is None for value in material_results.values()))
                success = (status_name == "succeeded" and verified_tool and material_calls_valid
                           and "campus_materials_search" in selected and "campus_materials_read" in selected
                           and len(model_events) == 3 and len(provider_requests) == 3
                           and provider_requests[0].get("available_material_search_tool")
                           and provider_requests[0].get("available_material_read_tool")
                           and provider_requests[1].get("material_search_result_seen")
                           and provider_requests[2].get("material_read_result_seen")
                           and MATERIAL_TEXT in final_text)
            elif fixture == "MULTI-02":
                for event in events:
                    harness_event = ((event.get("data") or {}).get("event") or {})
                    payload = harness_event.get("data") or {}
                    if harness_event.get("type") != "tool/call":
                        continue
                    try:
                        arguments = payload.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                    plan_calls.append({"name": payload.get("name"), "arguments": arguments})
                search_items = (plan_results.get("materials.search", {}).get("data") or {}).get("items") or []
                read_items = (plan_results.get("materials.read", {}).get("data") or {}).get("items") or []
                assignment_items = (plan_results.get("assignments.list", {}).get("data") or {}).get("items") or []
                preview_action = ((plan_results.get("plans.preview", {}).get("data") or {}).get("action") or {})
                preview_plan = ((preview_action.get("preview") or {}).get("plan") or {})
                verified_tool = (
                    len(search_items) == 1 and search_items[0].get("resource_id") == MATERIAL_ID
                    and len(read_items) == 1 and read_items[0].get("id") == MATERIAL_CHUNK_ID
                    and read_items[0].get("text") == MATERIAL_TEXT
                    and any(item.get("id") == ASSIGNMENT_ID and item.get("title") == ASSIGNMENT_TITLE
                            for item in assignment_items)
                    and (plan_results.get("availability.read", {}).get("data") or {}).get("configured") is True
                    and preview_action.get("kind") == "apply" and preview_action.get("status") == "pending"
                    and preview_plan.get("status") == "candidate"
                    and all((plan_results.get(name) or {}).get("status") in {"ok", "partial"}
                            and (plan_results.get(name) or {}).get("error") is None
                            for name in ("materials.search", "materials.read", "assignments.list", "availability.read", "plans.preview"))
                )
                expected_calls = ["campus_materials_search", "campus_materials_read", "campus_assignments_list",
                                  "campus_availability_read", "campus_plans_preview"]
                success = (
                    status_name == "succeeded" and verified_tool and [call.get("name") for call in plan_calls] == expected_calls
                    and not any(call.get("name") == "campus_plans_apply" for call in plan_calls)
                    and len(model_events) == 6 and len(provider_requests) == 6
                    and (not defer_tool_group or (
                        all(not request.get("available_review_preflight_tool") for request in provider_requests)
                        and all(request.get("load_tools_supports_review_group") for request in provider_requests)
                        and "load_tools" not in harness_tool_calls
                    ))
                    and all(provider_requests[index].get(key) for index, key in (
                        (0, "available_material_search_tool"), (0, "available_material_read_tool"),
                        (0, "available_assignment_tool"), (0, "available_availability_tool"),
                        (0, "available_plan_preview_tool"), (1, "material_search_result_seen"),
                        (2, "material_read_result_seen"), (3, "assignment_result_seen"),
                        (4, "availability_result_seen"), (5, "plan_preview_result_seen")))
                    and MATERIAL_TEXT in final_text and "尚未应用" in final_text
                )
            elif fixture == "ERR-10":
                sync_event = next((event for event in tool_events
                    if (event.get("data") or {}).get("name") == "connections.sync"), None)
                sync_result = ((sync_event or {}).get("data") or {}).get("result") or {}
                sync_error = sync_result.get("error") or {}
                error_code = sync_error.get("code") or sync_result.get("error_code")
                verified_tool = sync_result.get("status") == "error" and error_code == "AUTH_REQUIRED"
                success = (
                    status_name == "succeeded" and verified_tool
                    and selected == ["campus_connections_sync"]
                    and len(tool_events) == 1 and len(model_events) == 2 and len(provider_requests) == 2
                    and provider_requests[0].get("available_connection_sync_tool")
                    and provider_requests[0].get("expired_session_user_prompt_seen")
                    and provider_requests[1].get("auth_required_error_seen")
                    and "查询已暂停" in final_text and "登录后" in final_text
                    and "没有重复发起同步" in final_text
                    and "campus_jobs_read" not in selected
                )
            elif fixture == "ERR-02":
                for event in events:
                    harness_event = ((event.get("data") or {}).get("event") or {})
                    payload = harness_event.get("data") or {}
                    if harness_event.get("type") != "tool/call":
                        continue
                    try:
                        arguments = payload.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                    material_calls.append({"name": payload.get("name"), "arguments": arguments})
                search_result = next((event.get("data", {}).get("result", {}) for event in tool_events
                                      if (event.get("data") or {}).get("name") == "materials.search"), {})
                coverage = search_result.get("coverage") or {}
                search_warnings = search_result.get("warnings") or []
                verified_tool = (
                    len(material_calls) == 1
                    and material_calls[0].get("name") == "campus_materials_search"
                    and material_calls[0].get("arguments") == {"course_id": COURSE_ID, "query": "合成空结果关键词"}
                    and search_result.get("status") == "ok"
                    and not ((search_result.get("data") or {}).get("items") or [])
                    and coverage.get("complete") is True
                    and any("不能据此推断要求不存在" in warning for warning in search_warnings)
                )
                success = (
                    status_name == "succeeded" and verified_tool
                    and "campus_materials_search" in selected
                    and len(model_events) == 2 and len(provider_requests) == 2
                    and provider_requests[0].get("available_material_search_tool")
                    and provider_requests[0].get("empty_search_user_prompt_seen")
                    and provider_requests[1].get("empty_material_search_result_seen")
                    and "本地缓存中没有找到" in final_text
                    and "不代表课程要求不存在" in final_text
                    and "没有刷新校园数据" in final_text
                    and not any(name == "campus_connections_sync" for name in selected)
                )
            elif fixture == "ERR-11":
                assignment_event = next((event for event in tool_events
                    if (event.get("data") or {}).get("name") == "assignments.list"), None)
                assignment_result = ((assignment_event or {}).get("data") or {}).get("result") or {}
                assignment_data = assignment_result.get("data") or {}
                coverage = assignment_result.get("coverage") or {}
                coverage_missing = coverage.get("missing") or []
                call_arguments = next(((((event.get("data") or {}).get("event") or {}).get("data") or {}).get("arguments") for event in events
                    if (((event.get("data") or {}).get("event") or {}).get("type") == "tool/call"
                        and (((event.get("data") or {}).get("event") or {}).get("data") or {}).get("name")
                        in {"assignments.list", "campus_assignments_list"})), {})
                if isinstance(call_arguments, str):
                    try: call_arguments = json.loads(call_arguments)
                    except (TypeError, ValueError): call_arguments = {}
                verified_tool = (assignment_result.get("status") == "partial"
                    and assignment_data.get("items") == [] and assignment_data.get("next_cursor") is None
                    and assignment_result.get("freshness", {}).get("stale") is True
                    and coverage.get("complete") is False and len(coverage_missing) > 0
                    and isinstance(call_arguments, dict)
                    and call_arguments.get("order_by") == "due_soon" and call_arguments.get("limit") == 10
                    and not call_arguments.get("cursor"))
                success = (status_name == "succeeded" and verified_tool
                    and selected == ["campus_assignments_list"] and len(tool_events) == 1
                    and len(model_events) == 2 and len(provider_requests) == 2
                    and provider_requests[0].get("available_assignment_tool")
                    and provider_requests[0].get("empty_assignment_user_prompt_seen")
                    and provider_requests[1].get("empty_assignment_result_seen")
                    and "不代表学校端没有作业" in final_text and "没有刷新校园数据" in final_text
                    and "campus_connections_sync" not in selected)
            elif fixture == "BUS-12":
                page_calls = []
                for event in events:
                    harness_event = ((event.get("data") or {}).get("event") or {})
                    payload = harness_event.get("data") or {}
                    if harness_event.get("type") != "tool/call" or payload.get("name") not in {
                        "assignments.list", "campus_assignments_list"
                    }:
                        continue
                    arguments = payload.get("arguments") or {}
                    if isinstance(arguments, str):
                        try: arguments = json.loads(arguments)
                        except (TypeError, ValueError): arguments = {}
                    page_calls.append(arguments)
                first_data = (assignment_results[0].get("data") or {}) if len(assignment_results) > 0 else {}
                second_data = (assignment_results[1].get("data") or {}) if len(assignment_results) > 1 else {}
                first_items = first_data.get("items") or []
                second_items = second_data.get("items") or []
                first_cursor = first_data.get("next_cursor")
                item_ids = [item.get("id") for item in first_items + second_items if isinstance(item, dict)]
                cursor_forwarded = bool(first_cursor and len(page_calls) == 2
                    and page_calls[1].get("cursor") == first_cursor)
                verified_tool = (len(assignment_results) == 2 and len(page_calls) == 2
                    and page_calls[0].get("order_by") == "due_soon" and page_calls[0].get("limit") == 2
                    and not page_calls[0].get("cursor") and cursor_forwarded
                    and len(first_items) == 2 and len(second_items) == 1
                    and len(item_ids) == 3 and len(set(item_ids)) == 3
                    and first_cursor and second_data.get("next_cursor") is None)
                success = (status_name == "succeeded" and verified_tool
                    and selected == ["campus_assignments_list", "campus_assignments_list"]
                    and len(model_events) == 3 and len(provider_requests) == 3
                    and provider_requests[0].get("available_assignment_tool")
                    and provider_requests[0].get("assignment_page_user_prompt_seen")
                    and provider_requests[1].get("assignment_cursor_seen")
                    and provider_requests[1].get("assignment_page1_seen")
                    and provider_requests[2].get("assignment_page1_seen")
                    and provider_requests[2].get("assignment_page2_seen")
                    and "3项" in final_text and "本机缓存" in final_text
                    and "campus_connections_sync" not in selected)
            elif fixture == "ERR-09":
                for event in events:
                    harness_event = ((event.get("data") or {}).get("event") or {})
                    payload = harness_event.get("data") or {}
                    if harness_event.get("type") != "tool/call" or payload.get("name") != "campus_materials_read":
                        continue
                    try:
                        arguments = payload.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                    material_calls.append(arguments)
                read_result = material_results.get("materials.read", {})
                read_items = (read_result.get("data") or {}).get("items") or []
                invalid_result = next((event.get("data", {}).get("result", {}) for event in tool_events
                                       if (event.get("data") or {}).get("name") == "materials.read"
                                       and (event.get("data", {}).get("result") or {}).get("status") == "error"), {})
                error_code = ((invalid_result.get("error") or {}).get("code"))
                verified_tool = (
                    len(material_calls) == 2
                    and material_calls[0] == {"resource_id": MATERIAL_ID, "expected_version": 0,
                                              "chunk_ids": [MATERIAL_CHUNK_ID]}
                    and material_calls[1] == {"resource_id": MATERIAL_ID, "expected_version": 2,
                                              "chunk_ids": [MATERIAL_CHUNK_ID]}
                    and error_code == "INVALID_INPUT"
                    and read_result.get("status") == "ok"
                    and len(read_items) == 1 and read_items[0].get("id") == MATERIAL_CHUNK_ID
                    and read_items[0].get("text") == MATERIAL_TEXT
                )
                success = (
                    status_name == "succeeded" and verified_tool
                    and selected.count("campus_materials_read") == 2
                    and len(model_events) == 3 and len(provider_requests) == 3
                    and provider_requests[0].get("available_material_read_tool")
                    and provider_requests[0].get("parameter_error_user_prompt_seen")
                    and provider_requests[1].get("invalid_parameter_error_seen")
                    and provider_requests[2].get("material_read_result_seen")
                    and MATERIAL_TEXT in final_text
                )
            else:
                success = (status_name == "succeeded" and COURSE_TITLE in final_text and verified_tool
                           and "campus_courses_list" in selected and len(model_events) == 2
                           and len(provider_requests) == 2
                           and provider_requests[0].get("available_course_tool")
                           and provider_requests[0].get("synthetic_user_prompt_seen")
                           and provider_requests[1].get("synthetic_tool_result_seen"))
            invalid_parameter_code = None
            if fixture == "ERR-09":
                invalid_parameter_event = next((event for event in tool_events
                    if (event.get("data") or {}).get("name") == "materials.read"
                    and ((event.get("data") or {}).get("result") or {}).get("status") == "error"), None)
                invalid_parameter_result = ((invalid_parameter_event or {}).get("data") or {}).get("result") or {}
                invalid_parameter_code = (invalid_parameter_result.get("error") or {}).get("code")
            result.update({
                "status": "passed" if success else "failed",
                "verified_success": bool(success),
                "request_path": "public POST conversation -> public POST turn -> public GET turn",
                "runtime": {"api_host": "127.0.0.1", "scripted_provider_host": "127.0.0.1",
                            "isolated_temp_directory": True, "turn_timeout_seconds": timeout,
                            "api_agent_source": api_source_check,
                            "max_model_calls": int(env.get("CAMPUS_MAX_MODEL_CALLS", MAX_MODEL_CALLS)), "observed_model_calls": len(model_events),
                            "observed_provider_requests": len(provider_requests)},
                "evidence": {
                    "turn_status": status_name,
                    "selected_model_tools": sorted(set(selected)),
                    "harness_tool_calls": harness_tool_calls,
                    "real_domain_tool_result_verified": verified_tool,
                    "synthetic_course_in_final_answer": COURSE_TITLE in final_text if fixture != "JOB-01" else None,
                    "synthetic_job_result_status": result_status,
                    "assignment_empty_cache_contract": ({
                        "status": assignment_result.get("status"),
                        "item_count": len(assignment_data.get("items") or []),
                        "has_next_cursor": bool(assignment_data.get("next_cursor")),
                        "stale": (assignment_result.get("freshness") or {}).get("stale"),
                        "coverage_complete": (assignment_result.get("coverage") or {}).get("complete"),
                        "coverage_missing_count": len((assignment_result.get("coverage") or {}).get("missing") or []),
                        "arguments_valid": isinstance(call_arguments, dict)
                            and call_arguments.get("order_by") == "due_soon" and call_arguments.get("limit") == 10
                            and not call_arguments.get("cursor"),
                    } if fixture == "ERR-11" else None),
                    "assignment_pagination_contract": ({
                        "page_count": len(assignment_results),
                        "page_item_counts": [len((item.get("data") or {}).get("items") or []) for item in assignment_results],
                        "unique_item_count": len(set(item_ids)) if fixture == "BUS-12" else None,
                        "cursor_forwarded": cursor_forwarded if fixture == "BUS-12" else None,
                        "final_cursor_absent": bool(assignment_results and
                            not ((assignment_results[-1].get("data") or {}).get("next_cursor"))),
                    } if fixture == "BUS-12" else None),
                    "material_result_names": sorted(material_results),
                    "material_search_hit_count": len((material_results.get("materials.search", {}).get("data") or {}).get("items") or []),
                    "material_read_chunk_count": len((material_results.get("materials.read", {}).get("data") or {}).get("items") or []),
                    "material_read_call_contract_valid": material_calls_valid if fixture == "BUS-03" else None,
                    "material_source_text_verified": (MATERIAL_TEXT in final_text) if fixture == "BUS-03" else None,
                    "invalid_parameter_error_code": invalid_parameter_code,
                    "expired_session_error_code": error_code if fixture == "ERR-10" else None,
                    "expired_session_stopped_without_retry": bool(fixture == "ERR-10" and verified_tool
                        and len(tool_events) == 1 and len(model_events) == 2),
                    "parameter_error_recovered": bool(fixture == "ERR-09" and verified_tool
                        and provider_requests and provider_requests[-1].get("material_read_result_seen")),
                    "plan_preview_action_status": preview_action.get("status") if fixture == "MULTI-02" else None,
                    "plan_preview_candidate_status": preview_plan.get("status") if fixture == "MULTI-02" else None,
                    "plan_preview_not_applied": (not any(call.get("name") == "campus_plans_apply" for call in plan_calls)
                                                 and preview_action.get("status") == "pending") if fixture == "MULTI-02" else None,
                    "review_preflight_can_generate": preflight_data.get("can_generate") if fixture == "TOOL-REV-01" else None,
                    "review_tool_group_loaded": bool(defer_tool_group and any(item.get("review_tool_load_result_seen")
                        for item in provider_requests)) if fixture == "TOOL-REV-01" else None,
                    "review_tool_schema_hidden_initially": (not provider_requests[0].get("available_review_preflight_tool")
                        if defer_tool_group and provider_requests else None),
                    "plan_chain_tool_names": [call.get("name") for call in plan_calls] if fixture == "MULTI-02" else None,
                    "job_read_wait_argument_omitted": bool(job_call_args and set(job_call_args[0]) == {"job_id"}) if fixture == "JOB-01" else None,
                    "job_result_returned_before_final": bool(provider_requests and provider_requests[-1].get("job_tool_result_seen")) if fixture == "JOB-01" else None,
                    "provider_received_tool_result_before_final": bool(
                        len(provider_requests) >= 2 and provider_requests[-1].get(
                            "job_tool_result_seen" if fixture == "JOB-01" else
                            "plan_preview_result_seen" if fixture == "MULTI-02" else
                            "material_read_result_seen" if fixture == "ERR-09" else
                            "auth_required_error_seen" if fixture == "ERR-10" else
                            "empty_assignment_result_seen" if fixture == "ERR-11" else
                            "assignment_page2_seen" if fixture == "BUS-12" else
                            "empty_material_search_result_seen" if fixture == "ERR-02" else
                            "material_read_result_seen" if fixture == "BUS-03" else
                            "review_preflight_result_seen" if fixture == "TOOL-REV-01" else
                            "synthetic_tool_result_seen")),
                    "model_round_trips": [{key: request.get(key) for key in (
                        "sequence", "route_ok", "model_ok", "api_key_ok", "message_count",
                        "available_course_tool", "synthetic_user_prompt_seen", "synthetic_tool_result_seen",
                        "available_job_tool", "job_user_prompt_seen", "job_tool_result_seen",
                        "available_material_search_tool", "available_material_read_tool",
                        "material_search_result_seen", "material_read_result_seen",
                        "available_assignment_tool", "available_availability_tool", "available_plan_preview_tool",
                        "assignment_result_seen", "availability_result_seen", "plan_preview_result_seen",
                        "empty_search_user_prompt_seen", "empty_material_search_result_seen",
                        "assignment_page1_seen", "assignment_page2_seen", "assignment_cursor_seen",
                        "assignment_page_user_prompt_seen",
                        "invalid_parameter_error_seen", "parameter_error_user_prompt_seen",
                        "available_connection_sync_tool", "expired_session_user_prompt_seen",
                        "auth_required_error_seen", "available_load_tools", "load_tools_supports_review_group",
                        "available_review_preflight_tool",
                        "review_user_prompt_seen", "review_preflight_result_seen", "review_tool_load_result_seen",
                        "loaded_review_group_receipt_seen")}
                        for request in provider_requests],
                    "tool_result_count": len(tool_events),
                    "execution_trace": execution_trace,
                    "model_call_count": len(model_events),
                    "tool_steps": step_trace,
                    "model_steps": model_trace,
                },
                "final_output": final_text,
                "failure_code": None if success else (state.error_code or "e2e_assertion_failed"),
            })
            stage = "complete"
    except SafeFailure as exc:
        result.update({"status": "failed", "verified_success": False,
                       "failure_stage": exc.stage, "failure_code": exc.code})
    except Exception as exc:
        # Keep diagnostics machine-readable and avoid echoing exception/request data.
        frames = traceback.extract_tb(exc.__traceback__)
        frame = frames[-1] if frames else None
        filename = pathlib.Path(frame.filename).resolve() if frame else None
        try:
            diagnostic_file = filename.relative_to(ROOT).as_posix() if filename else None
        except ValueError:
            diagnostic_file = filename.name if filename else None
        result.update({"status": "failed", "verified_success": False,
                       "failure_stage": stage, "failure_code": type(exc).__name__,
                       "failure_location": {"file": diagnostic_file, "function": frame.name, "line": frame.lineno}
                           if frame else None})
    finally:
        if api_process is not None:
            _terminate_tree(api_process)
        server.shutdown()
        server.server_close()
        provider_thread.join(timeout=2)
        cookie_jar.clear()
        _API_OPENER = None
        result["cleanup"] = {"api_process_tree_stopped": api_process is None or api_process.poll() is not None,
                             "scripted_provider_closed": not provider_thread.is_alive(),
                             "temporary_state_discarded": True}
        result["elapsed_ms"] = round((time.perf_counter() - wall_started) * 1000, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=TURN_TIMEOUT_SECONDS,
                        help="maximum seconds to wait for this single Agent turn (default: 120)")
    parser.add_argument("--scenario", choices=("course-list-smoke", "JOB-01", "BUS-03", "BUS-12", "MULTI-02", "ERR-02", "ERR-09", "ERR-10", "ERR-11", "TOOL-REV-01"), default="course-list-smoke",
                        help="named isolated fixture; ERR-10 verifies AUTH_REQUIRED stop and ERR-11 verifies empty assignment cache handling")
    parser.add_argument("--defer-tool-group", choices=("campus_reviews",),
                        help="benchmark treatment; only valid with TOOL-REV-01 or MULTI-02")
    args = parser.parse_args()
    if args.timeout < 10 or args.timeout > 300:
        parser.error("--timeout must be between 10 and 300 seconds")
    if args.defer_tool_group and args.scenario not in {"TOOL-REV-01", "MULTI-02"}:
        parser.error("--defer-tool-group is only supported for TOOL-REV-01 and MULTI-02")
    evidence = _main(args.timeout, args.scenario, args.defer_tool_group)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0 if evidence.get("verified_success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
