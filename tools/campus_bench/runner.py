from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import http.cookiejar
import html
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    # Keep the documented ``python tools/campus_bench/runner.py ...`` entrypoint
    # able to import project modules used by live probes (for example
    # connectors.campus.academic_defaults). Pytest imports happen to add the
    # repository root already, which previously hid this CLI-only failure.
    sys.path.insert(0, str(ROOT))
CATALOG = Path(__file__).with_name("scenarios.json")
OUT_ROOT = ROOT / "var" / "campus-bench"
SECRET_KEYS = re.compile(r"(?i)(password|passwd|cookie|session[_-]?token|access[_-]?token|authorization|api[_-]?key|secret|username|student[_-]?(?:id|no)|用户名|学号|验证码|密码|凭据)")
PUBLIC_NOTICE_URL = re.compile(
    r"https://zdbk\.zju\.edu\.cn/jwglxt/xtgl/xwck_ckLoginNews\.html\?"
    r"xwbh=[0-9A-Fa-f]{32}&doType=save\Z")
SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(session(?:id|token)?\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)((?:password|passwd|secret|token|验证码|密码|密钥)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+"),
    re.compile(r"\b(?:sk|ghp|gho|github_pat|ds)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,6}\*{2,}[A-Za-z0-9]{1,6}|[0-9]{1,8}\*{2,}[0-9]{1,8})(?![A-Za-z0-9])"),
]
ACCOUNT_DATA_TOOLS = {
    "campus.status", "campus.academics", "campus.notice.read", "campus.notice.import",
    "courses.list", "assignments.list", "assignments.next.by.course", "materials.list", "materials.search",
    "materials.read", "tasks.list", "grades.list", "profile.read",
}


def is_account_data_tool(name, arguments=None):
    if not isinstance(name, str):
        return False
    normalized = name.lower()
    if normalized.startswith("campus_"):
        normalized = normalized[len("campus_"):]
    normalized = normalized.replace("_", ".")
    if normalized in {"campus.query", "campus.sync"}:
        args = arguments if isinstance(arguments, dict) else {}
        if isinstance(arguments, str):
            try: args = json.loads(arguments)
            except ValueError: args = {}
        # Campus notices from public sources are not account data; course,
        # grade, profile and account-selected college records are.
        return args.get("capability") != "notices"
    return normalized in ACCOUNT_DATA_TOOLS


def private_calls_may_have_returned_data(events, private_calls):
    """Whether an account-data call may have exposed private data to the model.

    Tool-call intent alone is not evidence that private data was returned. Keep
    answer redaction conservative for missing/ambiguous results, but don't erase
    an unrelated answer when every private call is explicitly an error with no
    payload (for example, an unsupported notice ID).
    """
    if not private_calls:
        return False
    by_call_id = {}
    by_step = {}

    def add_result(call_id, turn, step, value):
        if call_id:
            by_call_id.setdefault(str(call_id), []).append(value)
        elif turn is not None and step is not None:
            by_step.setdefault((turn, step), []).append(value)

    for event in events:
        if not isinstance(event, dict):
            continue
        kind, data = event.get("kind"), event.get("data")
        if not isinstance(data, dict):
            continue
        if kind == "tool_result":
            add_result(data.get("call_id") or data.get("callId"), data.get("turn"), data.get("step"),
                       data.get("result"))
        elif kind == "harness_event":
            inner = data.get("event") if isinstance(data.get("event"), dict) else {}
            if inner.get("type") != "tool/result":
                continue
            payload = inner.get("data") if isinstance(inner.get("data"), dict) else {}
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            parsed = None
            for item in content:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                try:
                    parsed = json.loads(item["text"])
                except (ValueError, TypeError):
                    parsed = None
                if parsed is not None:
                    break
            add_result(payload.get("callId") or payload.get("call_id"), payload.get("turn"),
                       payload.get("step"), {"isError": payload.get("isError"), "result": parsed,
                                               "message": message})

    def error_without_payload(value):
        if not isinstance(value, dict):
            return False
        nested = value.get("result") if isinstance(value.get("result"), dict) else value
        data = nested.get("data")
        has_payload = bool(data) if isinstance(data, (dict, list, str)) else data is not None
        if value.get("isError") is True and not has_payload:
            return True
        status = str(nested.get("status", "")).lower()
        failed = status in {"error", "failed", "unsupported", "blocked", "rejected"}
        has_error = bool(nested.get("error"))
        return (failed or has_error) and not has_payload

    for call in private_calls:
        call_id = call.get("callId") or call.get("call_id")
        results = by_call_id.get(str(call_id), []) if call_id else []
        if not results and call.get("turn") is not None and call.get("step") is not None:
            results = by_step.get((call.get("turn"), call.get("step")), [])
        if not results or any(not error_without_payload(result) for result in results):
            return True
    return False


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def event_latency_ms(events, origin_epoch_ms, kind=None):
    """Return latency to the first timestamped event of a kind, without reading its payload."""
    if origin_epoch_ms is None:
        return None
    stamps = []
    for event in events:
        if kind is not None and event.get("kind") != kind:
            continue
        created_at = event.get("created_at")
        if not isinstance(created_at, str):
            continue
        try:
            stamps.append(dt.datetime.fromisoformat(created_at).timestamp() * 1000)
        except ValueError:
            continue
    return round(max(0, min(stamps) - origin_epoch_ms), 2) if stamps else None


def safe(value, key="", depth=0):
    if SECRET_KEYS.search(key):
        return "[REDACTED]"
    if depth > 14:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        return {str(k)[:160]: safe(v, str(k), depth + 1) for k, v in list(value.items())[:1000]}
    if isinstance(value, list):
        return [safe(v, key, depth + 1) for v in value[:1000]]
    if isinstance(value, str):
        text = value[:30000]
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (dict, list)):
                    text = json.dumps(safe(parsed, key, depth + 1), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
        # Query values can contain campus tickets. Keep them redacted except a
        # precise, registered public-notice URL whose opaque ID is public source
        # identity, not a credential. Preserve Markdown/JSON delimiters so the
        # sanitized report remains parseable by deterministic source oracles.
        def redact_url_query(match):
            base, query = match.group(1), match.group(2)
            trailing = re.search(r"[),.;]+$", query)
            suffix = trailing.group(0) if trailing else ""
            candidate_query = query[:-len(suffix)] if suffix else query
            candidate = base + "?" + candidate_query
            if PUBLIC_NOTICE_URL.fullmatch(candidate):
                return candidate + suffix
            return base + "?<redacted>" + suffix
        text = re.sub(r"(https?://[^\s?\"'<>]+)\?([^\s#?\"'<>]+)", redact_url_query, text)
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _result_digest(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raw = str(value).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _private_result_summary(value):
    data = value if isinstance(value, dict) else {}
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    if not error and isinstance(payload.get("error"), dict):
        error = payload["error"]
    count = next((len(payload[k]) for k in ("items", "records", "events", "courses")
                  if isinstance(payload.get(k), list)), None)
    return {"redacted": True, "status": data.get("status"), "error_code": error.get("code"),
            "record_count": count, "sha256": _result_digest(value)}


def _private_arguments_summary(value):
    """Retain correlation/debug metadata without persisting account-query values."""
    if (isinstance(value, dict) and value.get("redacted") is True and
            isinstance(value.get("argument_keys"), list) and isinstance(value.get("sha256"), str)):
        # Report rebuilding may apply privacy_event to an already sanitized trace.
        # Preserve its safe, allowlisted operational options instead of hashing
        # the summary a second time and silently dropping them.
        return safe(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {"raw": value}
    keys = sorted(str(key)[:80] for key in value) if isinstance(value, dict) else []
    options = {}
    if isinstance(value, dict):
        limit = value.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 50:
            options["limit"] = limit
        wait_seconds = value.get("wait_seconds")
        if isinstance(wait_seconds, int) and not isinstance(wait_seconds, bool) and 0 <= wait_seconds <= 15:
            options["wait_seconds"] = wait_seconds
        order = value.get("order_by")
        if isinstance(order, str) and order in {"id", "due_soon"}:
            options["order_by"] = order
        include = value.get("include_instructions")
        if isinstance(include, bool):
            options["include_instructions"] = include
        if "cursor" in value:
            options["cursor_present"] = bool(value.get("cursor"))
    return {"redacted": True, "argument_keys": keys, "safe_options": options,
            "sha256": _result_digest(value)}


def privacy_event(event, private_call_ids=frozenset(), private_steps=frozenset()):
    """Keep execution evidence while dropping duplicate assistant prose/reasoning."""
    cleaned = safe(event)
    if not isinstance(cleaned, dict):
        return cleaned
    kind, data = cleaned.get("kind"), cleaned.get("data")
    raw_data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if kind in {"tool_started", "tool_result"} and is_account_data_tool(
            raw_data.get("name"), raw_data.get("arguments")):
        # tool_started normally stores only argument keys; keep this defensive for
        # future event producers that may attach the complete argument object.
        if "arguments" in data:
            data["arguments"] = _private_arguments_summary(raw_data.get("arguments"))
    if kind == "tool_result" and is_account_data_tool(raw_data.get("name"), raw_data.get("arguments")) and not (data.get("result") or {}).get("redacted"):
        data["result"] = _private_result_summary(raw_data.get("result"))
    if kind == "harness_event" and isinstance(data, dict):
        inner = data.get("event")
        if isinstance(inner, dict) and inner.get("type") == "tool/call":
            payload = inner.get("data") if isinstance(inner.get("data"), dict) else {}
            if is_account_data_tool(payload.get("name"), payload.get("arguments")):
                payload["arguments"] = _private_arguments_summary(payload.get("arguments"))
        elif isinstance(inner, dict) and inner.get("type") == "assistant/message":
            payload = inner.get("data") if isinstance(inner.get("data"), dict) else {}
            inner["data"] = {k: payload[k] for k in ("turn", "step", "usage") if k in payload}
        elif isinstance(inner, dict) and inner.get("type") == "tool/result":
            payload = inner.get("data") if isinstance(inner.get("data"), dict) else {}
            call_id = payload.get("callId") or payload.get("call_id")
            if call_id in private_call_ids or (payload.get("turn"), payload.get("step")) in private_steps:
                payload["message"] = {"content": [{"text": "[REDACTED account-derived tool result]"}]}
            else:
                # Browser review results may contain action identifiers and page context.
                # Preserve only the actionable state needed by benchmark analysis.
                pending = needs_user_result(payload)
                if pending:
                    payload["message"] = {"content": [{"text": json.dumps(
                        {"status": "needs_user", "message": "[REDACTED pending user action details]"})}]}
    if kind == "result" and isinstance(data, dict) and isinstance(data.get("response"), str):
        data["response_chars"] = len(data.pop("response"))
    return cleaned


def needs_user_result(payload):
    """Recognize a structured human-action handoff without retaining its payload."""
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return False
    for block in message.get("content", []):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        try:
            value = json.loads(block["text"])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get("status") == "needs_user":
            return True
    structured = message.get("structuredContent")
    return isinstance(structured, dict) and structured.get("status") == "needs_user"


def pending_user_actions(events):
    return sum(1 for row in events if row.get("kind") == "harness_event"
               and ((event_data(row).get("event") or {}).get("type") == "tool/result")
               and needs_user_result(((event_data(row).get("event") or {}).get("data") or {})))


class Api:
    def __init__(self, base, timeout):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def call(self, method, path, payload=None, headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=raw, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(5_000_000)
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read(1_000_000)
            try:
                data = json.loads(body)
            except ValueError:
                data = {"message": body.decode("utf-8", "replace")[:1000]}
            return exc.code, data


def api_preflight(api):
    """Verify the local Agent API is alive before creating per-scenario records."""
    started = time.perf_counter()
    try:
        code, body = api.call("GET", "/api/v1/health")
    except Exception as exc:
        return {"status": "failed", "http_status": None,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    healthy = code == 200 and isinstance(body, dict) and body.get("status") == "ok"
    expected_source = agent_api_source_sha256()
    source_check = api_agent_source_fingerprint_status(body, expected_source)
    return {"status": "passed" if healthy else "failed", "http_status": code,
            "api_status": body.get("status") if isinstance(body, dict) else None,
            "agent_source_status": source_check["status"],
            "agent_source_sha256_loaded": source_check["loaded_sha256"],
            "agent_source_sha256_expected": expected_source,
            "error": None if healthy else "health endpoint did not return HTTP 200 and status=ok",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}


def agent_api_source_sha256(root=ROOT):
    """Match the API health fingerprint over campus Agent executor Python sources."""
    root = Path(root)
    source_roots = (root / "apps" / "api" / "app",
                    root / "connectors" / "campus",
                    root / "packages" / "learning" / "src" / "learning")
    files = sorted(path for base in source_roots if base.is_dir()
                   for path in base.rglob("*.py") if "__pycache__" not in path.parts)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def api_agent_source_fingerprint_status(health, expected_sha256=None):
    loaded = health.get("agent_source_sha256") if isinstance(health, dict) else None
    expected = expected_sha256 or agent_api_source_sha256()
    if loaded is None:
        status = "not_reported"
    elif not isinstance(loaded, str) or not re.fullmatch(r"[0-9a-f]{64}", loaded):
        status = "invalid"
    else:
        status = "matched" if loaded == expected else "mismatch"
    return {"status": status, "loaded_sha256": loaded if isinstance(loaded, str) else None,
            "expected_sha256": expected}


def api_source_gate(api_check, allow_unmatched=False):
    """Require a source-aligned Agent API for ordinary benchmark runs.

    Unmatched services remain measurable in an explicit diagnostic mode, but
    must not silently produce results presented as current-checkout behavior.
    """
    status = (api_check or {}).get("agent_source_status", "not_reported")
    if status == "matched":
        return {"status": "passed", "agent_source_status": status, "override": False, "error": None}
    if allow_unmatched:
        return {"status": "overridden", "agent_source_status": status, "override": True,
                "error": "结果仅代表未匹配/未报告源码指纹的运行服务，不可归因到当前工作树"}
    return {"status": "blocked", "agent_source_status": status, "override": False,
            "error": "运行 API 未证明加载当前工作树源码；请对齐服务版本后重试，诊断旧服务时显式传 --allow-unmatched-api-source"}


def provider_preflight(api, provider_id):
    """Verify a selected provider exists and is enabled without exposing its secrets."""
    started = time.perf_counter()
    path = "/api/v1/ai/providers/" + urllib.parse.quote(str(provider_id), safe="")
    try:
        code, body = api.call("GET", path)
    except Exception as exc:
        return {"status": "failed", "http_status": None, "provider_matches": False,
                "enabled": None, "key_configured": None,
                "error": f"provider preflight transport error: {type(exc).__name__}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    matches = code == 200 and isinstance(body, dict) and body.get("id") == provider_id
    enabled = body.get("enabled") if matches else None
    key_configured = body.get("has_api_key") if matches else None
    runtime_provider = None
    if matches and body.get("protocol") == "openai_chat_completions":
        try:
            endpoint = urllib.parse.urlsplit(str(body.get("base_url") or ""))
            official = (endpoint.scheme == "https" and endpoint.hostname == "api.deepseek.com"
                        and endpoint.port in (None, 443) and endpoint.path.rstrip("/") in ("", "/v1"))
            runtime_provider = "deepseek-official" if official else "campus-model"
        except ValueError:
            runtime_provider = None
    passed = matches and enabled is True and key_configured is True
    if code != 200:
        error = "provider configuration endpoint unavailable or provider not found"
    elif not matches:
        error = "provider response did not match the requested provider"
    elif enabled is not True:
        error = "provider is disabled"
    elif key_configured is not True:
        error = "provider key is not configured"
    else:
        error = None
    return {"status": "passed" if passed else "failed", "http_status": code,
            "provider_matches": bool(matches), "enabled": enabled,
            "key_configured": key_configured, "runtime_provider": runtime_provider, "error": error,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}


def tool_exposure_preflight(api, groups, runtime_provider=None):
    """Verify the API has explicitly enabled each requested schema treatment."""
    if not groups:
        return {"status": "passed", "enabled_groups": [], "requested_groups": []}
    if runtime_provider != "campus-model":
        return {"status": "failed", "enabled_groups": [], "requested_groups": list(groups),
                "error": "requested tool-exposure treatment requires a campus-model gateway provider; no conversation was created"}
    try:
        code, body = api.call("GET", "/api/v1/agent/benchmark/tool-exposure")
    except Exception as exc:
        return {"status": "failed", "enabled_groups": [], "requested_groups": list(groups),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    available = body.get("groups", []) if code == 200 and isinstance(body, dict) else []
    passed = code == 200 and body.get("enabled") is True and set(groups) <= set(available)
    return {"status": "passed" if passed else "failed", "enabled_groups": available,
            "requested_groups": list(groups), "http_status": code,
            "error": None if passed else "API did not explicitly enable all requested tool exposure groups"}


class CallBudget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.lock = threading.Lock()
        self.total = 0
        self.per_run = {}
        self.exhausted = threading.Event()

    def observe(self, run_id, events):
        count = len(harness_model_steps(events))
        with self.lock:
            previous = self.per_run.get(run_id, 0)
            self.total += max(0, count - previous)
            self.per_run[run_id] = max(previous, count)
            if self.total >= self.maximum:
                self.exhausted.set()
            return self.exhausted.is_set(), self.total


def loopback_only(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Benchmark API 必须是本机 HTTP loopback 地址（项目 Agent API 为 local-only）")


def load_catalog():
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    if len({x["id"] for x in data["scenarios"]}) != len(data["scenarios"]):
        raise ValueError("场景 ID 必须唯一")
    required = {"id", "input", "environment", "goal", "success_conditions", "required_tools",
                "allowed_alternatives", "intervention", "category", "complexity", "modes",
                "capability_state", "mock_fixture", "mock_status", "eval", "initial_state", "preconditions", "run_status", "run_block_reason"}
    for scenario in data["scenarios"]:
        missing = required - set(scenario)
        if missing:
            raise ValueError(f"{scenario.get('id', '<unknown>')} 缺少场景字段：{sorted(missing)}")
        if not scenario["input"].strip() or not scenario["goal"].strip() or not scenario["success_conditions"]:
            raise ValueError(f"{scenario['id']} 必须提供用户输入、目标和可验证成功条件")
        if not scenario["modes"] or set(scenario["modes"]) - {"real", "mock"}:
            raise ValueError(f"{scenario['id']} 的 modes 只允许 real/mock")
        if "mock" in scenario["modes"] and not scenario["mock_fixture"]:
            raise ValueError(f"{scenario['id']} 声明支持 mock，但没有逐场景 mock_fixture")
        if scenario["capability_state"] not in {"implemented", "expected_missing", "unverified"}:
            raise ValueError(f"{scenario['id']} 的 capability_state 不合法")
        if scenario["run_status"] not in {"runnable", "blocked_precondition"}:
            raise ValueError(f"{scenario['id']} 的 run_status 不合法")
        if scenario["run_status"] == "blocked_precondition" and not scenario["run_block_reason"]:
            raise ValueError(f"{scenario['id']} 被阻塞时必须说明原因")
        if not isinstance(scenario["initial_state"], str) or not isinstance(scenario["preconditions"], list):
            raise ValueError(f"{scenario['id']} 必须说明初始状态和前置条件")
        if "messages" in scenario and (not isinstance(scenario["messages"], list) or not scenario["messages"] or
                                        any(not isinstance(x, str) or not x.strip() for x in scenario["messages"])):
            raise ValueError(f"{scenario['id']} 的 messages 必须是非空文本数组")
        if not isinstance(scenario["eval"], dict) or not scenario["eval"].get("kind"):
            raise ValueError(f"{scenario['id']} 缺少评测定义")
    return data


def event_data(event):
    return event.get("data") if isinstance(event.get("data"), dict) else {}


def event_tools(events):
    names = []
    for e in events:
        d = event_data(e)
        if isinstance(d.get("name"), str):
            names.append(d["name"].lower())
        def walk(x):
            if isinstance(x, dict):
                typ = x.get("type", "")
                if isinstance(typ, str) and typ.startswith("tool/"):
                    nm = x.get("name") or x.get("toolName")
                    if isinstance(nm, str):
                        names.append(nm.lower())
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        event = d.get("event", {})
        walk(event)
        if isinstance(event, dict) and isinstance(event.get("data"), dict):
            name = event["data"].get("name")
            if isinstance(name, str) and event.get("type") == "tool/call":
                names.append(name.lower())
    return list(dict.fromkeys(names))


def harness_model_steps(events):
    return {(d.get("turn"), d.get("step")) for e in events if e.get("kind") == "harness_event"
            for event in [event_data(e).get("event") or {}] if event.get("type") == "assistant/message"
            for d in [event.get("data") or {}]}


def tool_error_evidence(events):
    """Normalize failed domain and native Harness tool results for scoring/diagnosis."""
    failures = []
    domain_call_ids = set()
    native_call_ids_by_seq = {}
    for event in events:
        if event.get("kind") != "harness_event":
            continue
        inner = event_data(event).get("event") or {}
        data = inner.get("data") or {}
        if inner.get("type") == "tool/call":
            call_id = data.get("callId") or data.get("call_id")
            if call_id and isinstance(inner.get("seq"), int):
                native_call_ids_by_seq[inner["seq"]] = call_id
    for event in events:
        if event.get("kind") == "tool_result":
            data = event_data(event)
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            code = error.get("code") or result.get("error_code")
            if result.get("status") in {"error", "failed"} or code:
                if data.get("call_id"):
                    domain_call_ids.add(str(data["call_id"]))
                failures.append({"name": data.get("name"), "error_code": code,
                                 "message": error.get("message") or result.get("message"), "channel": "domain",
                                 "call_id": data.get("call_id")})
        if event.get("kind") != "harness_event":
            continue
        inner = event_data(event).get("event") or {}
        if inner.get("type") != "tool/result":
            continue
        data = inner.get("data") or {}
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        if not message.get("isError"):
            continue
        texts = [item.get("text", "") for item in message.get("content", []) if isinstance(item, dict)]
        message_text = "\n".join(x for x in texts if isinstance(x, str))[:1000]
        call_id = native_tool_result_call_id(data)
        if not call_id:
            call_id = next((native_call_ids_by_seq.get(seq) for seq in inner.get("sourceEventSeqs", [])
                            if native_call_ids_by_seq.get(seq)), None)
        # API domain journal and provider-facing Harness result are two views of
        # the same campus executor call. Correlate by ID/lineage and retain one
        # failure so metrics and root-cause evidence count executions, not events.
        if call_id and str(call_id) in domain_call_ids:
            continue
        tool_name = None
        for candidate in events:
            if candidate.get("kind") != "harness_event":
                continue
            call_event = event_data(candidate).get("event") or {}
            call_data = call_event.get("data") or {}
            if call_event.get("type") == "tool/call" and call_data.get("callId") == call_id:
                tool_name = call_data.get("name")
                break
        match = re.search(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", message_text)
        failures.append({"name": tool_name, "error_code": match.group(0) if match else None,
                         "message": message_text, "channel": "native", "call_id": call_id})
    return failures


def _normalized_source_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.hostname.lower(), parsed.path.rstrip("/")
    except (TypeError, ValueError):
        return None


def native_tool_result_call_id(data):
    """Read call IDs from both top-level and SDK-native result envelopes."""
    message = data.get("message") if isinstance(data, dict) else None
    message = message if isinstance(message, dict) else {}
    source = message.get("source") if isinstance(message.get("source"), dict) else {}
    return (data.get("callId") or data.get("call_id") or message.get("toolCallId") or
            source.get("callId") or source.get("toolCallId"))


def source_grounding(response, events, minimum_citations=1, require_dates=True, allowed_domains=None):
    """Deterministically align answer citations/dates with observed tool results."""
    sources, observed_text, campus_dates = [], [], set()
    browser_calls = {}
    for event in events:
        if event.get("kind") != "harness_event":
            continue
        inner = event_data(event).get("event") or {}
        data = inner.get("data") or {}
        if inner.get("type") == "tool/call" and data.get("name") in {
                "mcp__campus_browser__browser_navigate", "browser_navigate"}:
            call_id = data.get("callId") or data.get("call_id")
            arguments = data.get("arguments", {})
            if isinstance(arguments, str):
                try: arguments = json.loads(arguments)
                except ValueError: arguments = {}
            if call_id and isinstance(arguments, dict) and arguments.get("url"):
                browser_calls[call_id] = arguments["url"]
    for event in events:
        data = event_data(event)
        if event.get("kind") == "tool_result":
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            payload = result.get("data") if isinstance(result.get("data"), dict) else {}
            for record in payload.get("records", []) if isinstance(payload.get("records"), list) else []:
                if isinstance(record, dict):
                    if record.get("published_at"):
                        campus_dates.add(str(record["published_at"])[:10])
                    if record.get("title"):
                        observed_text.append(str(record["title"]))
                    if record.get("source"):
                        sources.append({"url": record["source"], "title": record.get("title", "")})
        if event.get("kind") != "harness_event":
            continue
        inner = data.get("event") or {}
        if inner.get("type") != "tool/result":
            continue
        result_data = inner.get("data") or {}
        meta = result_data.get("meta") if isinstance(result_data.get("meta"), dict) else {}
        message = result_data.get("message") if isinstance(result_data.get("message"), dict) else {}
        texts = [x.get("text", "") for x in message.get("content", []) if isinstance(x, dict)]
        observed_text.extend(x for x in texts if isinstance(x, str))
        result_source = message.get("source") if isinstance(message.get("source"), dict) else {}
        call_id = result_source.get("callId") or result_data.get("toolCallId")
        if call_id in browser_calls and not message.get("isError"):
            page_text = "\n".join(x for x in texts if isinstance(x, str))
            page_url_match = re.search(r"Page URL:\s*(https?://\S+)", page_text)
            page_url = page_url_match.group(1).rstrip(".,)") if page_url_match else browser_calls[call_id]
            page_title_match = re.search(r"Page Title:\s*([^\r\n]+)", page_text)
            page_title = page_title_match.group(1).strip() if page_title_match else ""
            # Browser snapshots expose link labels and relative hrefs as adjacent
            # lines. Resolve only URLs actually present in the returned page.
            for match in re.finditer(r'link\s+"([^"\r\n]+)"[^\r\n]*\r?\n\s*-\s*/url:\s*("[^"]+"|\S+)', page_text):
                label, raw_url = match.group(1), match.group(2).strip('"')
                resolved = urllib.parse.urljoin(page_url, raw_url)
                if _normalized_source_url(resolved):
                    sources.append({"url": resolved, "title": label, "text": page_text})
            # Capture source dates in both ISO and Chinese date formats.
            for year, month, day in re.findall(r"\b(20\d{2})年0?(\d{1,2})月0?(\d{1,2})日", page_text):
                campus_dates.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
            if page_url:
                sources.append({"url": page_url, "title": page_title, "text": page_text})
        if meta.get("url"):
            sources.append({"url": meta["url"], "title": "", "text": "\n".join(texts)})
        for item in meta.get("sources", []) if isinstance(meta.get("sources"), list) else []:
            if isinstance(item, dict) and item.get("url"):
                sources.append({"url": item["url"], "title": item.get("title", ""), "text": "\n".join(texts)})
    # Exclude URLs so date-like path fragments are not treated as factual claims.
    answer_without_urls = re.sub(r"https?://[^\s)\]>]+", "", response or "")
    dates = set(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", answer_without_urls))
    for year, month, day in re.findall(r"\b(20\d{2})年0?(\d{1,2})月0?(\d{1,2})日", answer_without_urls):
        dates.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
    source_corpus = "\n".join(observed_text)
    supported_dates = {date for date in dates if date in campus_dates or date in source_corpus}
    links = re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", response or "")
    citation_checks = []
    allowed = {str(x).lower().lstrip(".") for x in (allowed_domains or []) if isinstance(x, str)}
    for label, url in links:
        normalized = _normalized_source_url(url)
        matches = [item for item in sources if _normalized_source_url(item.get("url")) == normalized]
        label_clean = re.sub(r"\s+", " ", label).strip().lower()
        title_seen = any(label_clean and label_clean in
                         (str(item.get("title", "")) + "\n" + str(item.get("text", ""))).lower()
                         for item in matches)
        if label_clean in {"原链接", "来源", "链接", "网页", "页面"} and matches:
            title_seen = True
        host = normalized[0] if normalized else ""
        domain_allowed = not allowed or any(host == domain or host.endswith("." + domain) for domain in allowed)
        citation_checks.append({"url": url, "matched_tool_source": bool(matches),
                                "label_matches_source": title_seen, "allowed_domain": domain_allowed})
    date_aligned = (not require_dates) or (bool(dates) and dates <= supported_dates)
    complete = (len(links) >= minimum_citations and date_aligned and
                all(x["matched_tool_source"] and x["label_matches_source"] and x["allowed_domain"]
                    for x in citation_checks))
    return {"status": "aligned" if complete else "incomplete_or_unverified",
            "citation_count": len(links), "minimum_citations": minimum_citations,
            "citations": citation_checks, "date_claims": sorted(dates),
            "dates_supported_by_tool_results": sorted(supported_dates),
            "date_check": "verified" if require_dates and date_aligned else "not_required" if not require_dates else "unverified",
            "allowed_domains": sorted(allowed),
            "campus_latest_notice_date": max(campus_dates) if campus_dates else None,
            "method": "exact URL host/path, optional official-host allowlist, citation-label and configured date alignment against observed tool results; does not prove factual claim correctness or search completeness"}


_CONTENT_STOPWORDS = {"about", "after", "also", "from", "have", "into", "more", "most", "official",
                     "page", "this", "that", "their", "there", "these", "they", "with", "your",
                     "deepseek", "deepseekapi", "api", "docs", "document", "documentation"}


def _content_terms(value):
    """Extract modest language-independent overlap evidence for page extraction."""
    text = re.sub(r"https?://\S+", " ", value or "")
    text = re.sub(r"!?(?:\[[^\]]*\])\([^)]*\)", " ", text)
    terms = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
             if word.lower() not in _CONTENT_STOPWORDS}
    for run in re.findall(r"[\u3400-\u9fff]{2,}", text):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def target_page_extraction(response, events, target_url, minimum_shared_terms=3,
                           minimum_answer_coverage=0.10):
    """Verify the requested page was fetched and the answer reuses page evidence.

    This does not judge whether the selected facts are the most useful summary;
    it prevents tool success or a generic mention of the site from counting alone.
    """
    target = _normalized_source_url(target_url)
    calls, results, call_ids_by_event_seq = {}, {}, {}
    for event in events:
        if event.get("kind") != "harness_event":
            continue
        inner = event_data(event).get("event") or {}
        data = inner.get("data") or {}
        if inner.get("type") == "tool/call" and (data.get("callId") or data.get("call_id")):
            native_id = data.get("callId") or data.get("call_id")
            call_id = ("id", native_id)
            if isinstance(inner.get("seq"), int):
                call_ids_by_event_seq[inner["seq"]] = call_id
        elif inner.get("type") == "tool/result" and native_tool_result_call_id(data):
            call_id = ("id", native_tool_result_call_id(data))
        elif inner.get("type") == "tool/result" and any(
                seq in call_ids_by_event_seq for seq in (inner.get("sourceEventSeqs") or [])):
            # MCP browser results omit a toolCallId and identify their originating
            # tool/call through Harness event lineage instead.
            source_call = next(call_ids_by_event_seq[seq] for seq in inner.get("sourceEventSeqs", [])
                               if seq in call_ids_by_event_seq)
            call_id = source_call
        elif data.get("turn") is not None or data.get("step") is not None:
            call_id = ("turn-step", data.get("turn"), data.get("step"))
        else:
            continue
        if inner.get("type") == "tool/call":
            arguments = data.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            calls[call_id] = {"name": data.get("name"), "arguments": arguments}
        elif inner.get("type") == "tool/result":
            results[call_id] = data
    evidence = []
    for call_id, call in calls.items():
        tool_name = str(call.get("name") or "")
        is_browser = tool_name in {"browser_navigate", "browser_open"} or tool_name.endswith(
            ("__browser_navigate", "__browser_open"))
        if tool_name != "web_fetch" and not is_browser:
            continue
        requested = _normalized_source_url((call.get("arguments") or {}).get("url"))
        if requested != target or call_id not in results:
            continue
        result = results[call_id]
        message = result.get("message") if isinstance(result.get("message"), dict) else {}
        texts = [item.get("text", "") for item in message.get("content", []) if isinstance(item, dict)]
        content = "\n".join(text for text in texts if isinstance(text, str))
        is_error = bool(message.get("isError"))
        # Native web_fetch emits its transport status in the result text. A 2xx
        # response is necessary but the separate content-alignment check is also
        # mandatory before the rubric can award success.
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        status_code = meta.get("statusCode")
        if is_browser:
            observed_urls = re.findall(r"(?m)^-\s*Page URL:\s*(\S+)", content)
            fetched_ok = not is_error and any(_normalized_source_url(url) == target for url in observed_urls)
        else:
            fetched_ok = not is_error and ((isinstance(status_code, int) and 200 <= status_code < 300) or
                                           bool(re.search(r"\bHTTP\s+2\d\d\b", content)))
        source_terms = _content_terms(content)
        answer_terms = _content_terms(response)
        shared = source_terms & answer_terms
        coverage = len(shared) / max(1, len(answer_terms))
        evidence.append({"tool": call.get("name"), "requested_url_matches": True,
                         "result_present": True, "fetch_success": fetched_ok,
                         "shared_term_count": len(shared), "answer_term_coverage": round(coverage, 3),
                         "result_chars": len(content)})
        if fetched_ok and len(shared) >= minimum_shared_terms and coverage >= minimum_answer_coverage:
            return {"status": "aligned", "target_url": target_url, "evidence": evidence,
                    "method": "correlated requested URL and non-error page result; deterministic lexical overlap against the answer; does not judge summary relevance/completeness"}
    return {"status": "incomplete_or_unverified", "target_url": target_url, "evidence": evidence,
            "minimum_shared_terms": minimum_shared_terms,
            "minimum_answer_coverage": minimum_answer_coverage,
            "method": "correlated requested URL and non-error page result; deterministic lexical overlap against the answer; does not judge summary relevance/completeness"}


def campus_entry_grounding(response, events, reference_sources=None):
    """Verify the core campus portal against either observed status or independent references.

    A generic public-entry question must not require reading personal account state.
    The independent source is maintained in the scenario and was checked outside
    the Agent trace. Extra claims/citations are not audited, so the task stays partial.
    """
    calls, results = [], []
    for event in events:
        if event.get("kind") != "harness_event":
            continue
        inner = event_data(event).get("event") or {}
        payload = inner.get("data") or {}
        if inner.get("type") == "tool/call":
            calls.append(payload)
        elif inner.get("type") == "tool/result":
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            contents = message.get("content") if isinstance(message.get("content"), list) else []
            text = "\n".join(str(item.get("text", "")) for item in contents if isinstance(item, dict))
            results.append((payload, text, bool(message.get("isError"))))

    status_text = ""
    status = None
    # The backend journal carries a structured result; prefer it over the SDK's
    # mirrored text event, whose result payload may omit the tool name/call ID.
    for event in events:
        if event.get("kind") != "tool_result":
            continue
        payload = event_data(event)
        if str(payload.get("name", "")).lower() not in {"campus.status", "campus_campus_status"}:
            continue
        value = payload.get("result")
        candidate = value.get("data") if isinstance(value, dict) and isinstance(value.get("data"), dict) else None
        connector_rows = candidate.get("connectors", []) if isinstance(candidate, dict) else []
        if any(isinstance(item, dict) and str(item.get("platform_id", "")).lower() == "zdbk"
               for item in connector_rows):
            status, status_text = candidate, json.dumps(value, ensure_ascii=False)
            break

    status_call_turns = {call.get("turn") for call in calls
                         if "campus_status" in str(call.get("name", "")).lower()}
    for payload, text, is_error in results:
        if status is not None:
            break
        if is_error or payload.get("turn") not in status_call_turns:
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        candidate = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        connector_rows = candidate.get("connectors", []) if isinstance(candidate, dict) else []
        if any(isinstance(item, dict) and str(item.get("platform_id", "")).lower() == "zdbk"
               for item in connector_rows):
            status, status_text = candidate, text
            break
    connectors = status.get("connectors", []) if isinstance(status, dict) else []
    registered_zdbk = any(isinstance(item, dict) and str(item.get("platform_id", "")).lower() == "zdbk"
                          for item in connectors)
    response_text = str(response or "")
    references = [item for item in (reference_sources or []) if isinstance(item, dict)
                  and item.get("domain") == "zdbk.zju.edu.cn" and isinstance(item.get("url"), str)]
    reference_has_zdbk = bool(references)
    source_has_zdbk = registered_zdbk or "zdbk.zju.edu.cn" in status_text.lower() or reference_has_zdbk
    answer_has_zdbk = "zdbk.zju.edu.cn" in response_text.lower()
    disconnected = isinstance(status, dict) and status.get("connected") is False
    compact_response = re.sub(r"\s+", "", response_text.lower())
    boundary_terms = ("connected:false", "connected=false", "未连接", "未登录", "无法读取", "不能读取",
                      "未实际查看", "未同步", "缓存过期", "未能读取")
    boundary_disclosed = any(term in compact_response for term in boundary_terms)
    result = {"status_result_observed": bool(status_text), "registered_zdbk_connector": registered_zdbk,
              "zdbk_supported_by_status_result": source_has_zdbk, "zdbk_in_answer": answer_has_zdbk,
              "connected": status.get("connected") if isinstance(status, dict) else None,
              "offline_boundary_disclosed": boundary_disclosed,
              "independent_reference_sources": references,
              "scope_note": "Core official portal is checked against a registry result or independent benchmark reference; personal connection state is not required for a general-entry task; extra claims/citations need review."}
    if source_has_zdbk and answer_has_zdbk and (not disconnected or boundary_disclosed):
        result["status"] = "core_claim_aligned"
    elif (status_text and source_has_zdbk) or reference_has_zdbk:
        result["status"] = "core_claim_mismatch"
    else:
        result["status"] = "insufficient_observed_evidence"
    return result


def campus_credentials_grounding(response, events):
    """Check the dedicated credential metadata call without storing any credential values."""
    result_data = None
    calls = []
    for event in events:
        if event.get("kind") == "harness_event":
            inner = event_data(event).get("event") or {}
            if inner.get("type") == "tool/call":
                calls.append((inner.get("data") or {}).get("name"))
        if event.get("kind") == "tool_result":
            payload = event_data(event)
            if str(payload.get("name", "")).lower() in {
                    "campus.credentials.status", "campus_campus_credentials_status"}:
                envelope = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
                if isinstance(data.get("credentials"), list):
                    result_data = data
    expected_model_name = "campus_campus_credentials_status"
    answer = str(response or "")
    hint_pattern = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,6}\*{2,}[A-Za-z0-9]{1,6}|[0-9]{1,8}\*{2,}[0-9]{1,8})(?![A-Za-z0-9])")
    safe_result = bool(result_data) and all(
        isinstance(item, dict) and set(item) <= {"scope", "configured", "auto_relogin"}
        and isinstance(item.get("configured"), bool) and isinstance(item.get("auto_relogin"), bool)
        for item in result_data["credentials"])
    safe_result = safe_result and isinstance(result_data.get("browser_session_authorized"), bool) \
        and isinstance(result_data.get("campus_workspace_connected"), bool)
    concepts = all(term in answer.lower() for term in ("凭据", "自动", "浏览器", "连接"))
    limit_disclosed = any(term in answer for term in ("不能验证", "无法验证", "不能证明", "无法证明", "不能单独判断", "无法单独判断"))
    return {"dedicated_tool_call_observed": expected_model_name in calls,
            "safe_structured_status_observed": safe_result,
            "distinct_auth_states_explained": concepts,
            "remote_authentication_limit_disclosed": limit_disclosed,
            "username_hint_in_answer": bool(hint_pattern.search(answer)),
            "credential_secret_fields_returned": bool(result_data) and not safe_result,
            "status": "aligned" if expected_model_name in calls and safe_result and concepts and limit_disclosed and not hint_pattern.search(answer)
                else "incomplete_or_unverified"}


def assignment_date_grounding(response, events):
    """Compare dates against live assignment results in memory; persist counts only.

    Assignment snapshots are account data. The report must not retain their titles,
    course ids, instructions, or dates, so this oracle returns only aggregate counts.
    """
    source_dates, sources, source_errors, incomplete = set(), 0, 0, False

    def visit(value):
        nonlocal sources
        if isinstance(value, dict):
            for due_key in ("due_at", "due_at_local"):
                due = value.get(due_key)
                if isinstance(due, str) and re.match(r"^\d{4}-\d{2}-\d{2}", due):
                    try:
                        instant = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
                        source_dates.add(instant.date().isoformat())
                        if instant.tzinfo is not None:
                            source_dates.add(instant.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
                    except ValueError:
                        pass
            for key, item in value.items():
                if key not in {"instructions", "title", "course_id", "id"}:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for event in events:
        if event.get("kind") != "tool_result":
            continue
        payload = event_data(event)
        if str(payload.get("name", "")).lower() not in {
                "assignments.list", "campus_assignments_list",
                "assignments.next_by_course", "campus_assignments_next_by_course"}:
            continue
        sources += 1
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if result.get("redacted"):
            incomplete = True
            continue
        if result.get("status") not in {"ok", "partial"} or result.get("error"):
            source_errors += 1
            incomplete = True
        coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
        if coverage.get("complete") is not True:
            incomplete = True
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        visit(data)

    # Only calendar dates are checked; time-of-day and assignment identity remain
    # outside this oracle. Unsupported dates are evidence of a factual mismatch.
    answer_text = str(response or "")
    date_matches = list(re.finditer(r"(?<!\d)(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?", answer_text))
    occurrence_dates = []
    for match in date_matches:
        year, month, day = map(int, match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            occurrence_dates.append((f"{year:04d}-{month:02d}-{day:02d}", match.start(), match.end()))
    normalized = {item[0] for item in occurrence_dates}
    unsupported = normalized - source_dates
    matched = normalized & source_dates
    unsupported_context = {"deadline": 0, "cache": 0, "generic": 0}
    for value, start, end in occurrence_dates:
        if value not in unsupported:
            continue
        left_bound = max(0, start - 48)
        left_marks = [answer_text.rfind(mark, left_bound, start) for mark in "。；;\n"]
        left = max([left_bound, *(position + 1 for position in left_marks if position >= 0)])
        right_bound = min(len(answer_text), end + 48)
        right_marks = [position for mark in "。；;\n"
                       if (position := answer_text.find(mark, end, right_bound)) >= 0]
        right = min(right_marks) if right_marks else right_bound
        nearby = answer_text[left:right]
        if re.search(r"截止|到期|作业|deadline|due", nearby, re.I):
            unsupported_context["deadline"] += 1
        elif re.search(r"缓存|同步|更新|截至|数据|覆盖|快照", nearby):
            unsupported_context["cache"] += 1
        else:
            unsupported_context["generic"] += 1
    if not sources or not source_dates:
        status = "unverified"
    elif unsupported:
        status = "mismatch"
    elif matched and not incomplete and not source_errors:
        status = "dates_aligned_complete_source"
    elif matched:
        status = "dates_aligned_partial_source"
    else:
        status = "no_date_claim_to_verify"
    return {"status": status, "assignment_tool_results": sources,
            "source_due_date_count": len(source_dates), "answer_date_count": len(normalized),
            "matched_date_count": len(matched), "unsupported_date_count": len(unsupported),
            "unsupported_date_context_counts": unsupported_context,
            "source_coverage_complete": bool(sources and not incomplete and not source_errors),
            "date_label_timezones": ["UTC", "Asia/Shanghai"],
            "privacy_note": "仅保留聚合计数；未持久化作业标题、课程、正文或日期"}


def course_directory_grounding(response, events):
    """Check a private course-list answer against tool rows, retaining counts only."""
    items, calls = [], 0
    for row in events:
        if row.get("kind") != "tool_result":
            continue
        payload = event_data(row)
        name = str(payload.get("name") or "").lower().replace("_", ".")
        if name.startswith("campus."):
            name = name[len("campus."):]
        if name != "courses.list":
            continue
        calls += 1
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        result_items = data.get("items") if isinstance(data.get("items"), list) else []
        items.extend(item for item in result_items if isinstance(item, dict))

    answer = re.sub(r"[\W_]+", "", str(response or "").lower(), flags=re.UNICODE)
    title_matches = term_matches = terms_expected = 0
    for item in items:
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = re.sub(r"[\W_]+", "", title.lower(), flags=re.UNICODE)
            if normalized_title and normalized_title in answer:
                title_matches += 1
        term = item.get("term")
        if isinstance(term, str) and term.strip():
            terms_expected += 1
            normalized_term = re.sub(r"[\W_]+", "", term.lower(), flags=re.UNICODE)
            if normalized_term and normalized_term in answer:
                term_matches += 1
    compact = re.sub(r"\s+", "", str(response or "").lower())
    boundary = any(mark in compact for mark in ("本机缓存", "本地缓存", "本机快照", "本地快照"))
    titled = sum(1 for item in items if isinstance(item.get("title"), str) and item["title"].strip())
    if not calls:
        status = "required_tool_missing"
    elif not items:
        status = "empty_cache_unverified"
    elif title_matches != titled or term_matches != terms_expected:
        status = "content_mismatch"
    elif not boundary:
        status = "aligned_boundary_omitted"
    else:
        status = "aligned"
    return {"status": status, "tool_result_count": calls,
            "returned_course_count": len(items), "returned_title_count": titled,
            "matched_title_count": title_matches, "returned_term_count": terms_expected,
            "matched_term_count": term_matches, "cache_boundary_disclosed": boundary,
            "privacy_note": "仅保存聚合匹配数量和状态；课程名称、学期、ID及回答正文只在内存中比对"}


def assignment_next_by_course_grounding(response, events):
    """Verify the dedicated per-course deadline tool and its read-only boundary."""
    tool_used = False
    sync_used = False
    record_count = 0
    course_ids = set()
    seen_course_ids = set()
    duplicate_courses = 0
    status_counts = {}
    status_disclosures = {
        "unknown_deadline_only": "截止时间未知",
        "no_known_deadline": "没有已知截止日期",
        "no_matching_deadline_in_range": "没有匹配所给日期范围",
        "no_cached_assignments": "本机缓存没有作业记录",
    }
    compact = re.sub(r"\s+", "", str(response or "").lower())
    statuses_disclosed = True
    for event in events:
        if event.get("kind") != "tool_result":
            continue
        payload = event_data(event)
        name = str(payload.get("name", "")).lower()
        if name in {"connections.sync", "campus_connections_sync", "campus.sync", "campus_campus_sync"}:
            sync_used = True
        if name not in {"assignments.next_by_course", "campus_assignments_next_by_course"}:
            continue
        tool_used = True
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        record_count += len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            course_id = item.get("course_id")
            course_title = item.get("course_title")
            if not isinstance(course_id, str) or not isinstance(course_title, str):
                continue
            if course_id in seen_course_ids:
                duplicate_courses += 1
            seen_course_ids.add(course_id)
            course_ids.add(course_id)
            course_status = item.get("course_status")
            if isinstance(course_status, str):
                status_counts[course_status] = status_counts.get(course_status, 0) + 1
                phrase = status_disclosures.get(course_status)
                normalized_title = re.sub(r"\s+", "", course_title).lower()
                if normalized_title not in compact or (phrase and phrase not in compact):
                    statuses_disclosed = False
    date_evidence = assignment_date_grounding(response, events)
    disclosed = any(term in compact for term in ("本机缓存", "本地缓存", "已导入课程", "数据覆盖", "未证明全量", "不是全量"))
    if not tool_used:
        status = "required_tool_missing"
    elif sync_used:
        status = "forbidden_sync_observed"
    elif not record_count:
        status = "no_cached_deadline_rows"
    elif date_evidence["status"] == "mismatch":
        status = "date_mismatch"
    elif not status_counts:
        status = "course_status_unverified"
    elif not statuses_disclosed:
        status = "course_status_omitted"
    elif (date_evidence["status"] in {"unverified", "no_date_claim_to_verify"}
          and status_counts and statuses_disclosed and date_evidence["answer_date_count"] == 0):
        status = "aligned_no_deadlines"
    elif date_evidence["status"] not in {"dates_aligned_partial_source", "dates_aligned_complete_source"}:
        status = "dates_unverifiable"
    elif duplicate_courses:
        status = "duplicate_course_rows"
    elif not disclosed:
        status = "cache_boundary_omitted"
    else:
        status = "aligned"
    return {"status": status, "dedicated_tool_observed": tool_used, "sync_observed": sync_used,
            "returned_rows": record_count, "courses_returned": len(course_ids),
            "duplicate_course_rows": duplicate_courses, "cache_boundary_disclosed": disclosed,
            "course_status_counts": status_counts, "course_statuses_disclosed": statuses_disclosed,
            "date_evidence_status": date_evidence["status"],
            "unsupported_date_count": date_evidence["unsupported_date_count"],
            "unsupported_date_context_counts": date_evidence["unsupported_date_context_counts"],
            "privacy_note": "只持久化工具边界与聚合数量；不保存课程/作业名称、ID或日期"}


def tool_selection_evidence(ev, events):
    """Check exact execution/forbidden-tool constraints while retaining selection separately."""
    def canonical(name):
        value = str(name or "").lower()
        if value.startswith("campus_"):
            value = value[len("campus_"):]
        return value.replace("_", ".")

    selected, executed = set(), set()
    native_calls, native_call_sequences, native_results = {}, {}, set()
    for row in events:
        if row.get("kind") == "tool_started":
            name = event_data(row).get("name")
            if name:
                executed.add(canonical(name))
        elif row.get("kind") == "tool_result":
            name = event_data(row).get("name")
            if name:
                executed.add(canonical(name))
        elif row.get("kind") == "harness_event":
            inner = event_data(row).get("event") or {}
            payload = inner.get("data") or {}
            if inner.get("type") == "tool/call":
                name = payload.get("name")
                if name:
                    selected.add(canonical(name))
                    call_id = payload.get("callId") or payload.get("call_id")
                    if call_id:
                        call_id = str(call_id)
                        native_calls[call_id] = canonical(name)
                        if isinstance(inner.get("seq"), int):
                            native_call_sequences[inner["seq"]] = call_id
            elif inner.get("type") == "tool/result":
                call_id = native_tool_result_call_id(payload)
                if call_id:
                    native_results.add(str(call_id))
                # The Agent journal may omit callId on the provider-facing result
                # event but retain sourceEventSeqs pointing at the tool/call event.
                for source_seq in inner.get("sourceEventSeqs", []):
                    source_call_id = native_call_sequences.get(source_seq)
                    if source_call_id:
                        native_results.add(source_call_id)
    for call_id in native_results & native_calls.keys():
        executed.add(native_calls[call_id])
    required = {canonical(name) for name in ev.get("exact_tools", [])}
    forbidden = {canonical(name) for name in ev.get("forbidden_tools", [])}
    redundant = {canonical(name) for name in ev.get("redundant_tools", [])}
    forbidden_values = {canonical(name): values for name, values in
                        (ev.get("forbidden_tool_argument_values") or {}).items() if isinstance(values, dict)}
    forbidden_argument_calls = set()
    for row in events:
        if row.get("kind") != "harness_event":
            continue
        inner = event_data(row).get("event") or {}
        if inner.get("type") != "tool/call":
            continue
        payload = inner.get("data") or {}
        name = canonical(payload.get("name"))
        constraints = forbidden_values.get(name)
        if not constraints:
            continue
        arguments = payload.get("arguments", {})
        if isinstance(arguments, str):
            try: arguments = json.loads(arguments)
            except ValueError: arguments = {}
        if isinstance(arguments, dict) and all(arguments.get(key) in (values if isinstance(values, list) else [values])
                                               for key, values in constraints.items()):
            # Retain the tool name only, never the matched private argument values.
            forbidden_argument_calls.add(name)
    expected_args = {canonical(name): value for name, value in
                     (ev.get("required_tool_arguments") or {}).items() if isinstance(value, dict)}
    observed_args, mismatches, unverified = {}, [], []
    for row in events:
        if row.get("kind") != "tool_result":
            continue
        payload = event_data(row)
        name = canonical(payload.get("name"))
        if name not in expected_args:
            continue
        arguments = payload.get("arguments")
        if isinstance(arguments, dict) and arguments.get("redacted") is True:
            arguments = arguments.get("safe_options") if isinstance(arguments.get("safe_options"), dict) else None
        if not isinstance(arguments, dict):
            unverified.append(name)
            continue
        observed_args[name] = {key: arguments.get(key) for key in expected_args[name] if key in arguments}
        if any(arguments.get(key) != value for key, value in expected_args[name].items()):
            mismatches.append(name)
        elif any(key not in arguments for key in expected_args[name]):
            unverified.append(name)
    return {
        "selected_tools": sorted(selected),
        "executed_tools": sorted(executed),
        "native_calls_without_result": sorted(name for call_id, name in native_calls.items()
                                               if call_id not in native_results),
        "required_tools": sorted(required),
        "missing_required_tools": sorted(required - executed),
        "forbidden_tools": sorted(forbidden),
        "forbidden_tools_executed": sorted(forbidden & executed),
        "forbidden_argument_tools": sorted(forbidden_values),
        "forbidden_argument_calls": sorted(forbidden_argument_calls),
        "redundant_tools": sorted(redundant),
        "redundant_tools_executed": sorted(redundant & executed),
        "expected_arguments": expected_args,
        "observed_safe_arguments": observed_args,
        "argument_mismatches": sorted(set(mismatches)),
        "arguments_unverified": sorted(set(unverified)),
        "status": "constraint_violation" if (required - executed) or (forbidden & executed) or forbidden_argument_calls or mismatches else "satisfied",
        "note": "Model tool selection and backend execution are derived from separate trace events; tool names only, no arguments or private result content retained.",
    }


def evaluate(scenario, turn, events, budget_stop=None):
    response = turn.get("response", "") or ""
    lower = response.lower()
    ev = scenario.get("eval", {})
    observed = event_tools(events)
    status = turn.get("status")
    success = "失败"
    correctness = "不可验证"
    reason = "终态未达到成功"
    if budget_stop:
        success, correctness = "不可验证", "不可验证"
        reason = f"运行器达到配置的 {budget_stop.get('per_run_limit')} 轮模型调用上限并取消；任务结果样本被截尾"
    elif status == "needs_user" or pending_user_actions(events):
        success, correctness = "部分成功", "人工步骤明确；业务任务尚未完成"
        reason = "工具显式返回 needs_user；基准记录人工步骤并停止等待，不自动确认该操作"
    elif status != "succeeded":
        err = turn.get("error") or {}
        reason = (err.get("code") or status or "turn_failed")
    elif ev.get("kind") == "nonempty":
        success, correctness, reason = ("不可验证", "不可验证", "仅观察到非空回复；没有核对具体主张") if response.strip() else ("失败", "失败", "最终回答为空")
    elif ev.get("kind") == "clarification":
        terms = ev.get("terms_any", ["什么", "哪一个", "哪里", "指的是", "具体"])
        ok = any(x.lower() in lower for x in terms)
        success, correctness, reason = ("部分成功", "需人工核验", "观察到澄清线索；问题是否足够具体需人工复核") if ok else ("失败", "失败", "未观察到必要澄清")
    elif ev.get("kind") == "question_or_preview":
        ok = any(x in lower for x in ("请问", "需要你", "哪门", "具体", "课程", "时间"))
        success, correctness, reason = ("部分成功", "启发式可验证", "观察到澄清或建议线索") if ok else ("不可验证", "不可验证", "开放任务需人工抽查")
    elif ev.get("kind") == "honest_limit":
        ok = any(x in lower for x in ev.get("terms", []))
        success, correctness, reason = ("部分成功", "需人工核验", "观察到能力边界提示；仍需核验是否编造或遗漏") if ok else ("失败", "失败", "未明确说明数据不足")
    elif ev.get("kind") == "source_grounded":
        aliases = [x.lower() for x in ev.get("tools_any", [])]
        has_tool = any(any(alias in name for alias in aliases) for name in observed)
        grounding = source_grounding(response, events, int(ev.get("minimum_citations", 1)),
                                     require_dates=bool(ev.get("require_dates", True)),
                                     allowed_domains=ev.get("allowed_domains"))
        result = ("部分成功", "引用链接与检索工具结果对齐；摘要事实和完整性仍需人工核验",
                  "观察到来源工具调用，引用链接与要求的官方来源确定性对齐") if has_tool and grounding["status"] == "aligned" else (
                  "不可验证", "不可验证", "引用链接/官方来源/日期不能与本轮工具结果完整对齐")
        success, correctness, reason = result
    elif ev.get("kind") == "target_page_extraction":
        evidence = target_page_extraction(response, events, ev["target_url"],
            int(ev.get("minimum_shared_terms", 3)), float(ev.get("minimum_answer_coverage", 0.20)))
        if evidence["status"] == "aligned":
            success, correctness, reason = "成功", "指定网页工具结果与回答内容确定性对齐", "目标 URL 有关联的成功读取结果且回答复用了来源内容"
        elif evidence["evidence"]:
            success, correctness, reason = "部分成功", "目标页面已读取，但回答内容对齐不足", "页面工具结果存在，未达到内容对齐阈值"
        else:
            success, correctness, reason = "失败", "失败", "未观察到目标 URL 的成功读取及关联结果"
    elif ev.get("kind") == "campus_entry_grounded":
        entry_evidence = campus_entry_grounding(response, events, ev.get("reference_sources"))
        if entry_evidence["status"] == "core_claim_aligned":
            success, correctness, reason = "部分成功", "本机登记教务入口和未连接边界已与轨迹对齐；附加主张需人工抽查", "核心教务入口与校园状态工具结果一致"
        elif entry_evidence["status"] == "core_claim_mismatch":
            success, correctness, reason = "失败", "失败", "状态工具已有入口证据，但回答遗漏入口或当前连接边界"
        else:
            success, correctness, reason = "不可验证", "不可验证", "未观察到可解析的校园入口状态证据"
    elif ev.get("kind") == "campus_credentials_status":
        evidence = campus_credentials_grounding(response, events)
        if evidence["status"] == "aligned":
            success, correctness, reason = "部分成功", "专用凭据状态工具返回安全的结构化状态，回答覆盖凭据/自动重登/浏览器授权/当前连接；额外解释需抽查", "凭据状态和回答已对齐；不把配置当作当前登录"
        elif evidence["dedicated_tool_call_observed"]:
            success, correctness, reason = "部分成功", "专用状态工具已调用，但结果或回答未覆盖全部必要状态或未说明状态读取不能验证远端凭据", "状态结果不完整或解释未对齐"
        else:
            success, correctness, reason = "失败", "失败", "未调用专用的只读校园凭据状态工具"
    elif ev.get("kind") == "course_directory_grounded":
        evidence = course_directory_grounding(response, events)
        if evidence["status"] == "aligned":
            success, correctness, reason = "成功", "课程名称和学期与工具返回行逐项对齐，并说明缓存边界", "本机课程目录内容与只读工具结果确定性对齐"
        elif evidence["status"] == "aligned_boundary_omitted":
            success, correctness, reason = "部分成功", "课程行与工具结果对齐，但没有说明本机缓存范围", "事实对齐；缓存覆盖边界未披露"
        elif evidence["status"] == "content_mismatch":
            success, correctness, reason = "失败", "回答漏列或改写了工具返回的课程名/学期", "课程目录事实未与工具结果对齐"
        else:
            success, correctness, reason = "不可验证", "课程目录来源结果不足，不能核验回答", evidence["status"]
    elif ev.get("kind") == "assignment_next_by_course":
        evidence = assignment_next_by_course_grounding(response, events)
        if evidence["status"] == "aligned":
            success, correctness, reason = "成功", "逐课程日期与工具事实一致且标明本地缓存边界", "专用逐课程工具、日期事实与只读边界均通过确定性核验"
        elif evidence["status"] == "aligned_no_deadlines":
            success, correctness, reason = "部分成功", "逐课程无截止日期状态与工具结果对齐；本机缓存不证明学校端完整", "未输出无来源日期；缓存中的未知/缺失状态已披露"
        elif evidence["status"] in {"course_status_unverified", "course_status_omitted"}:
            success, correctness, reason = "部分成功", "日期事实可核验，但逐课程无作业/未知截止状态未完整核验", "旧版或不完整工具结果不能证明覆盖了当前授权课程"
        elif evidence["status"] == "date_mismatch":
            success, correctness, reason = "失败", "回答含来源不支持的截止日期", "日期事实核验失败"
        elif evidence["status"] == "forbidden_sync_observed":
            success, correctness, reason = "失败", "任务要求只读缓存但实际触发同步", "违反场景只读缓存约束"
        elif evidence["status"] == "required_tool_missing":
            success, correctness, reason = "失败", "没有调用逐课程最近作业专用工具", "工具选择未匹配用户任务"
        else:
            success, correctness, reason = "不可验证", "未取得足够的逐课程作业来源证据", evidence["status"]
    elif ev.get("kind") == "assignment_dates_grounded":
        evidence = assignment_date_grounding(response, events)
        if evidence["status"] == "dates_aligned_complete_source":
            success, correctness, reason = "成功", "回答日期与完整来源集合确定性对齐", "作业日期已与工具返回值对齐且来源覆盖完整"
        elif evidence["status"] == "dates_aligned_partial_source":
            success, correctness, reason = "部分成功", "回答日期与可用来源对齐；本机缓存未证明学校数据全量覆盖", "只确认已返回作业的日期，完整性边界仍然存在"
        elif evidence["status"] == "mismatch":
            success, correctness, reason = "失败", "回答包含工具来源不支持的日期", "确定性日期核验发现无来源支持的主张"
        else:
            success, correctness, reason = "不可验证", "未获得足够的非脱敏作业来源证据", "作业日期或来源完整性无法核验"
    elif ev.get("kind") in ("tool_and_output", "tool_error", "tool_or_honest_limit"):
        aliases = [x.lower() for x in ev.get("tools_any", [])]
        has_tool = not aliases or any(any(a in n for a in aliases) for n in observed)
        kw = ev.get("keywords_any", ev.get("terms", []))
        has_text = not kw or any(k.lower() in lower for k in kw)
        tool_errors = tool_error_evidence(events)
        if ev.get("kind") == "tool_error":
            matched_error = any(not aliases or any(a in str(x.get("name", "")).lower() for a in aliases)
                                for x in tool_errors)
            if matched_error and has_text:
                success, correctness, reason = "部分成功", "只验证到工具报错和解释关键词", "工具确实返回错误；解释内容仍需人工核验"
            elif matched_error:
                success, correctness, reason = "部分成功", "工具错误可验证，回答需人工核验", "观察到工具错误但回答条件未匹配"
            else:
                success, correctness, reason = "失败", "失败", "没有匹配工具的实际错误结果"
        elif ev.get("kind") == "tool_or_honest_limit" and not has_tool:
            has_text = any(k.lower() in lower for k in ev.get("terms", []))
            success = "部分成功" if has_text else "不可验证"
            correctness = "启发式可验证" if has_text else "不可验证"
            reason = "如实说明工具/实时数据缺失" if has_text else "无工具证据，且未明确说明限制"
        elif has_tool and has_text:
            success, correctness, reason = "部分成功", "需按真实数据抽查", "工具轨迹与输出启发式匹配；实时事实需人工核验"
        elif not has_tool:
            success, correctness, reason = "失败", "失败", "未观察到约束要求的工具调用"
        else:
            success, correctness, reason = "部分成功", "需人工核验", "调用工具但输出条件未匹配"
    else:
        success, correctness, reason = "不可验证", "不可验证", "没有确定性判定器"
    selection = tool_selection_evidence(ev, events)
    if selection["missing_required_tools"]:
        success, correctness = "失败", "失败"
        reason = "未执行场景要求的精确工具：" + ", ".join(selection["missing_required_tools"])
    elif selection["forbidden_tools_executed"]:
        forbidden = selection["forbidden_tools_executed"]
        if success == "成功":
            success = "部分成功"
        reason += "；执行了场景禁止的工具：" + ", ".join(forbidden)
    if selection.get("forbidden_argument_calls"):
        if success == "成功":
            success = "部分成功"
        reason += "；检测到超出任务范围的数据能力调用：" + ", ".join(selection["forbidden_argument_calls"])
    if selection["redundant_tools_executed"]:
        redundant = selection["redundant_tools_executed"]
        if success == "成功":
            success = "部分成功"
        reason += "；观察到场景标记的冗余候选调用：" + ", ".join(redundant)
    if selection["argument_mismatches"]:
        success, correctness = "失败", "失败"
        reason = "精确工具参数与场景约束不符：" + ", ".join(selection["argument_mismatches"])
    attribution = root_cause_attribution(scenario, turn, events, {"success": success, "reason": reason}, budget_stop)
    primary_cause = attribution["primary"]
    failure_category = (f"{primary_cause['code']}_{primary_cause['category']}"
                        if primary_cause.get("code") else None)
    if ev.get("kind") == "assignment_dates_grounded" and assignment_date_grounding(response, events)["status"] == "mismatch":
        failure_category = "F_模型未正确利用工具结果"
    elif ev.get("kind") == "assignment_next_by_course" and assignment_next_by_course_grounding(response, events)["status"] == "date_mismatch":
        failure_category = "F_模型未正确利用工具结果"
    elif ev.get("kind") == "course_directory_grounded" and course_directory_grounding(response, events)["status"] == "content_mismatch":
        failure_category = "F_模型未正确利用工具结果"
    elif selection["redundant_tools_executed"]:
        failure_category = "I_任务完成但存在工具冗余候选"
    date_mismatch = (ev.get("kind") == "assignment_dates_grounded" and
                     assignment_date_grounding(response, events)["status"] == "mismatch")
    if selection["forbidden_tools_executed"] and not date_mismatch:
        failure_category = ("I_任务成功但执行冗余或违反工具范围"
                            if all(name == "courses.list" for name in selection["forbidden_tools_executed"])
                            else "C_模型选择了场景禁止的工具")
    elif selection["missing_required_tools"]:
        failure_category = "C_工具本身可用但模型未选择要求的工具"
    elif selection["argument_mismatches"]:
        failure_category = "C_模型工具参数不符合任务范围"
    elif selection.get("forbidden_argument_calls"):
        failure_category = "C_模型选择了超出任务范围的数据能力"
    elif selection["argument_mismatches"]:
        failure_category = "C_模型工具选择参数不符合任务范围"
    if status == "needs_user" or pending_user_actions(events):
        failure_category = "K_安全边界要求人工确认"
    grounding = source_grounding(response, events, int(ev.get("minimum_citations", 1)),
                                 require_dates=bool(ev.get("require_dates", True)),
                                 allowed_domains=ev.get("allowed_domains")) if ev.get("kind") == "source_grounded" else None
    page_evidence = target_page_extraction(response, events, ev["target_url"],
        int(ev.get("minimum_shared_terms", 3)), float(ev.get("minimum_answer_coverage", 0.20))) if ev.get("kind") == "target_page_extraction" else None
    entry_evidence = campus_entry_grounding(response, events, ev.get("reference_sources")) if ev.get("kind") == "campus_entry_grounded" else None
    credentials_evidence = campus_credentials_grounding(response, events) if ev.get("kind") == "campus_credentials_status" else None
    assignment_evidence = (assignment_next_by_course_grounding(response, events)
                           if ev.get("kind") == "assignment_next_by_course" else
                           assignment_date_grounding(response, events)
                           if ev.get("kind") == "assignment_dates_grounded" else None)
    course_directory_evidence = (course_directory_grounding(response, events)
                                 if ev.get("kind") == "course_directory_grounded" else None)
    return {"success": success, "correctness": correctness, "reason": reason,
            "tool_quality": "需人工抽查" if observed else "无工具调用",
            "efficiency": "记录耗时，阈值待基线样本积累" if status == "succeeded" else "失败",
            "autonomy": "需人工抽查", "failure_category": failure_category,
            "observed_tools": observed, "source_grounding": grounding,
            "tool_selection": selection,
            "target_page_evidence": page_evidence,
            "campus_entry_evidence": entry_evidence,
            "campus_credentials_evidence": credentials_evidence,
            "assignment_date_evidence": assignment_evidence,
            "course_directory_evidence": course_directory_evidence}


def apply_redacted_assignment_oracle(evaluation, prior_oracle):
    """Restore only the safe aggregate score after private answer text is redacted."""
    if not prior_oracle:
        return evaluation
    evaluation["assignment_date_evidence"] = prior_oracle
    # `status` describes whether the answer aligned with returned rows; the
    # nested field describes source completeness. Do not confuse the two.
    state = prior_oracle.get("date_evidence_status", prior_oracle.get("status"))
    if int(prior_oracle.get("unsupported_date_count", 0) or 0) > 0:
        state = "mismatch"
        evaluation["assignment_date_evidence"]["date_evidence_status"] = state
    if state == "dates_aligned_partial_source":
        evaluation.update(success="部分成功",
            correctness="回答日期与本机来源对齐；本机缓存不证明学校数据全量覆盖",
            reason="作业日期经过脱敏前的确定性比对，报告仅保留计数；数据覆盖仍不完整",
            failure_category="J_作业来源完整性不可验证",
            hallucination="日期与本机来源对齐；作业身份和学校端完整性需人工抽查")
    elif state == "mismatch":
        evaluation.update(success="失败", correctness="回答日期包含来源不支持项",
            reason="脱敏前确定性日期比对发现无来源支持的日期",
            failure_category="F_工具结果利用或事实不匹配",
            hallucination="存在来源不支持的日期")
    elif state == "dates_aligned_complete_source":
        evaluation.update(success="成功", correctness="回答日期与完整来源集合对齐",
            reason="脱敏前确定性日期比对通过；私有内容仍不写入报告",
            hallucination="日期与完整来源集合对齐")
    return evaluation


def apply_redacted_course_directory_oracle(evaluation, prior_oracle):
    """Restore the allowlisted aggregate course-list score after answer redaction."""
    if not isinstance(prior_oracle, dict):
        return evaluation
    evaluation["course_directory_evidence"] = prior_oracle
    state = prior_oracle.get("status")
    if state == "aligned":
        evaluation.update(success="成功",
            correctness="脱敏前逐项比对课程名和学期均与只读工具结果对齐，并说明本机缓存边界",
            reason="确定性课程目录 oracle 仅保存计数；课程内容和最终回答仍已脱敏",
            failure_category=None,
            hallucination="课程名称/学期与工具结果对齐；缓存整体范围之外仍需人工审查")
    elif state == "aligned_boundary_omitted":
        evaluation.update(success="部分成功",
            correctness="课程名和学期与工具结果对齐，但未说明缓存范围",
            reason="脱敏前确定性比对通过；缺少缓存边界说明",
            failure_category="J_未说明本机缓存边界")
    elif state == "content_mismatch":
        evaluation.update(success="失败", correctness="课程名或学期与工具返回内容不一致",
            reason="脱敏前确定性课程目录比对发现遗漏或改写",
            failure_category="F_工具结果利用或事实不匹配",
            hallucination="课程目录回答与本机工具结果不一致")
    # Preserve independent tool-selection evidence when restoring the redacted oracle.
    redundant = (evaluation.get("tool_selection") or {}).get("redundant_tools_executed", [])
    if state == "aligned" and redundant:
        evaluation.update(success="部分成功",
            reason=(evaluation.get("reason", "") + "；课程目录事实核验通过，但执行了冗余候选工具：" + ", ".join(redundant)),
            failure_category="I_任务完成但存在工具冗余候选")
    return evaluation


def public_source_oracle_summary(grounding):
    """Persist only aggregate evidence needed to rescore a redacted public-source answer."""
    if not isinstance(grounding, dict):
        return None
    citations = grounding.get("citations", [])
    return {
        "status": grounding.get("status"),
        "citation_count": int(grounding.get("citation_count", 0) or 0),
        "matched_citation_count": sum(1 for item in citations if item.get("matched_tool_source")),
        "label_aligned_citation_count": sum(1 for item in citations
            if item.get("matched_tool_source") and item.get("label_matches_source") and item.get("allowed_domain")),
        "date_claim_count": len(grounding.get("date_claims", [])),
        "date_check": grounding.get("date_check"),
        "method": "aggregate-only source oracle computed before response redaction",
    }


def apply_redacted_public_source_oracle(evaluation, prior_oracle):
    """Restore a conservative partial score without retaining public answer text or URLs."""
    if not isinstance(prior_oracle, dict):
        return evaluation
    evaluation["public_source_oracle"] = prior_oracle
    citation_count = int(prior_oracle.get("citation_count", 0) or 0)
    aligned_count = int(prior_oracle.get("label_aligned_citation_count", 0) or 0)
    dates_verified = prior_oracle.get("date_check") in {"verified", "not_required"}
    if not dates_verified or not aligned_count:
        return evaluation
    fully_aligned = prior_oracle.get("status") == "aligned" and aligned_count == citation_count
    evaluation.update(success="部分成功",
        correctness=("引用链接、标签和要求的日期在脱敏前与本轮公开来源工具结果对齐；事实完整性仍需抽查"
                     if fully_aligned else "部分引用和日期与本轮公开来源工具结果对齐；未对齐项及事实完整性仍需抽查"),
        reason=("仅恢复脱敏前计算的公开来源聚合核验；最终回答仍已脱敏" if fully_aligned else
                f"脱敏前核验到 {aligned_count}/{citation_count} 条引用标签与来源对齐且日期通过；其余引用需人工复核，最终回答已脱敏"),
        failure_category=None,
        hallucination=("公开来源链接和日期已确定性对齐；主题相关性与来源覆盖仍需人工复核" if fully_aligned else
                       "部分公开来源链接和日期对齐；未匹配引用的主张仍需人工复核"))
    redundant = (evaluation.get("tool_selection") or {}).get("redundant_tools_executed", [])
    if redundant:
        evaluation.update(success="部分成功",
            reason=evaluation["reason"] + "；另有冗余工具调用：" + ", ".join(redundant),
            failure_category="I_任务完成但存在工具冗余候选")
    return evaluation


def apply_redacted_forbidden_scope_evidence(evaluation, tool_names):
    """Restore only the tool-name signal for a private-scope policy violation."""
    names = sorted({str(name) for name in tool_names if isinstance(name, str)})
    if not names:
        return evaluation
    selection = evaluation.setdefault("tool_selection", {})
    selection["forbidden_argument_calls"] = names
    evaluation.update(success="部分成功" if evaluation.get("success") != "失败" else "失败",
        correctness="检测到超出任务范围的数据能力调用；该工具的参数与返回值已脱敏",
        reason=(evaluation.get("reason", "") + "；检测到超出任务范围的数据能力调用：" + ", ".join(names)),
        failure_category="C_模型选择了超出任务范围的数据能力")
    return evaluation


def classify(turn, events):
    error = turn.get("error") or {}
    code = str(error.get("code", "")).upper()
    kinds = [x.get("kind", "") for x in events]
    unsupported_campus = [event_data(x) for x in events if x.get("kind") == "tool_result" and
                          event_data(x).get("name") in {"campus.query", "campus.sync"} and
                          (((event_data(x).get("result") or {}).get("error") or {}).get("code") or
                           (event_data(x).get("result") or {}).get("error_code")) == "UNSUPPORTED"]
    if code in {"PROVIDER_AUTH_FAILED", "PROVIDER_DISABLED", "CONSENT_REQUIRED", "PROVIDER_UNREACHABLE", "PROVIDER_TIMEOUT"}:
        return "J_评测环境或模型服务"
    if any(x in code for x in ("AUTH", "SESSION", "WEBVPN")):
        return "H_认证或外部依赖"
    if unsupported_campus:
        return "C_校园查询参数或工具选择待查"
    tool_failures = tool_error_evidence(events)
    failure_codes = {str(x.get("error_code") or "").upper() for x in tool_failures}
    if any(any(tag in c for tag in ("WEB_BLOCKED", "TIMEOUT", "NETWORK", "CONNECTION", "DNS")) for c in failure_codes):
        return "H_认证或外部依赖"
    if tool_failures:
        return "B_工具执行或外部依赖"
    if "model_error" in kinds:
        return "B_模型服务或网络"
    has_tool_result = any(k in kinds for k in ("tool_result", "tool_started"))
    has_native_tool_result = any(
        x.get("kind") == "harness_event" and
        ((event_data(x).get("event") or {}).get("type") == "tool/result")
        for x in events)
    if has_tool_result or has_native_tool_result:
        return "F_工具结果利用或任务组合待查"
    if code or turn.get("status") != "succeeded":
        return "D_Harness或执行失败"
    return "C_工具选择或完成条件未满足"


def rank(result):
    return {"失败": 0, "部分成功": 1, "成功": 2}.get(result)


def execute_agent_turn(api, conversation_id, message, deadline, budget, run_key):
    sent_at = time.perf_counter()
    sent_at_epoch_ms = int(time.time() * 1000)
    code, turn = api.call("POST", f"/api/v1/agent/conversations/{conversation_id}/turns",
                          {"text": message}, {"Idempotency-Key": f"bench-{uuid.uuid4().hex}"})
    if code != 202:
        raise RuntimeError(f"turn enqueue HTTP {code}: {settings_safe(turn)}")
    events, cursor = [], 0
    budget_stop = None
    user_action_required = False
    timed_out = False
    while time.monotonic() < deadline:
        code, current = api.call("GET", f"/api/v1/agent/turns/{turn['id']}?after={cursor}")
        if code != 200:
            raise RuntimeError(f"turn poll HTTP {code}: {settings_safe(current)}")
        page = current.get("events", {})
        batch = page.get("items", [])
        events.extend(batch)
        cursor = page.get("next_cursor", cursor)
        if pending_user_actions(events):
            user_action_required = True
            if current["status"] in {"queued", "running", "stopping"}:
                api.call("POST", f"/api/v1/agent/turns/{turn['id']}/cancel", {})
                code, tail = api.call("GET", f"/api/v1/agent/turns/{turn['id']}?after={cursor}")
                if code == 200:
                    turn = tail
                    page = tail.get("events", {})
                    batch = page.get("items", [])
                    events.extend(batch)
                    cursor = page.get("next_cursor", cursor)
                    while len(batch) == 100 and time.monotonic() < deadline:
                        code, tail = api.call("GET", f"/api/v1/agent/turns/{turn['id']}?after={cursor}")
                        if code != 200:
                            break
                        turn = tail
                        page = tail.get("events", {})
                        batch = page.get("items", [])
                        if not batch:
                            break
                        events.extend(batch)
                        cursor = page.get("next_cursor", cursor)
            turn = {**turn, "status": "needs_user"}
            break
        steps = len(harness_model_steps(events))
        exhausted, total_calls = budget.observe(run_key, events)
        if current["status"] in {"queued", "running", "stopping"} and (steps >= MAX_MODEL_CALLS_PER_RUN or exhausted):
            api.call("POST", f"/api/v1/agent/turns/{turn['id']}/cancel", {})
            budget_stop = {"kind": "model_call_budget", "per_run_limit": MAX_MODEL_CALLS_PER_RUN,
                           "total_limit": budget.maximum, "observed_total": total_calls}
        if current["status"] not in {"queued", "running", "stopping"}:
            turn = current
            while len(batch) == 100 and time.monotonic() < deadline:
                code, tail = api.call("GET", f"/api/v1/agent/turns/{turn['id']}?after={cursor}")
                if code != 200: break
                page = tail.get("events", {})
                batch = page.get("items", [])
                if not batch: break
                events.extend(batch)
                cursor = page.get("next_cursor", cursor)
            break
        time.sleep(0.4)
    else:
        timed_out = True
        api.call("POST", f"/api/v1/agent/turns/{turn['id']}/cancel", {})
        code, tail = api.call("GET", f"/api/v1/agent/turns/{turn['id']}?after={cursor}")
        if code == 200:
            turn = tail
            events.extend(tail.get("events", {}).get("items", []))
    return {"turn": turn, "events": events, "sent_at": sent_at, "sent_at_epoch_ms": sent_at_epoch_ms,
            "finished_at": time.perf_counter(), "timed_out": timed_out, "budget_stop": budget_stop,
            "user_action_required": user_action_required}


def summarize_run(scenario, api, provider, timeout, run_no, seed, budget, run_key, benchmark_tool_groups=(),
                  prewarm_lead_seconds=0):
    record = {"scenario_id": scenario["id"], "run_index": run_no, "seed": seed,
              "started_at": now(), "environment": "real_api", "scenario": scenario,
              "benchmark_tool_groups": list(benchmark_tool_groups),
              "trace": [], "status": "blocked", "timings": {}}
    started = time.perf_counter()
    try:
        code, settings = api.call("GET", "/api/v1/agent/settings")
        if code != 200:
            raise RuntimeError(f"settings endpoint HTTP {code}: {settings}")
        conversation_payload = {"provider_id": provider}
        if benchmark_tool_groups:
            conversation_payload["benchmark_tool_groups"] = list(benchmark_tool_groups)
        code, conversation = api.call("POST", "/api/v1/agent/conversations", conversation_payload)
        if code != 201:
            raise RuntimeError(f"conversation create HTTP {code}: {settings_safe(conversation)}")
        record["conversation_id"] = conversation["id"]
        # Conversation creation starts the API's best-effort runner prewarm in the
        # background. A bounded lead interval models time spent composing a first
        # message and enables a cold-vs-prewarmed comparison without changing the
        # Agent or its permissions.
        record["prewarm_lead_seconds"] = prewarm_lead_seconds
        if prewarm_lead_seconds:
            time.sleep(prewarm_lead_seconds)
            # Exclude simulated user composition time from task latency. The
            # manifest and run record preserve the lead interval separately.
            started = time.perf_counter()
            record["prewarm_lead_finished_at"] = now()
        messages = scenario.get("messages") or [scenario["input"]]
        if not isinstance(messages, list) or not messages or any(not isinstance(x, str) or not x.strip() for x in messages):
            raise ValueError(f"{scenario['id']} messages 必须是非空文本数组")
        run_deadline = time.monotonic() + timeout
        turn_runs, events = [], []
        first_sent_at = None
        turn = None
        for message in messages:
            trace_start = len(events)
            run = execute_agent_turn(api, conversation["id"], message, run_deadline, budget, run_key)
            first_sent_at = first_sent_at or run["sent_at"]
            turn = run["turn"]
            events.extend(run["events"])
            run["trace_start"], run["trace_end"] = trace_start, len(events)
            turn_runs.append(run)
            if run["budget_stop"]:
                record["budget_stop"] = run["budget_stop"]
            if run["timed_out"] or turn.get("status") != "succeeded" or run["budget_stop"] or run["user_action_required"]:
                break
        record["turn_ids"] = [x["turn"]["id"] for x in turn_runs]
        record["turn_id"] = record["turn_ids"][-1]
        record["budget_observed_model_calls"] = budget.per_run.get(run_key, 0)
        completed_at = time.perf_counter()
        first_submit_epoch_ms = turn_runs[0]["sent_at_epoch_ms"] if turn_runs else None
        assistant_times = [event_data(event).get("event", {}).get("time")
                           for event in events if event.get("kind") == "harness_event" and
                           (event_data(event).get("event") or {}).get("type") == "assistant/message" and
                           isinstance((event_data(event).get("event") or {}).get("time"), (int, float))]
        first_assistant_message_ms = (round(max(0, min(assistant_times) - first_submit_epoch_ms), 2)
                                      if assistant_times and first_submit_epoch_ms is not None else None)
        record["first_submit_at"] = (dt.datetime.fromtimestamp(first_submit_epoch_ms / 1000, dt.timezone.utc).isoformat()
                                     if first_submit_epoch_ms is not None else None)
        record["timings"] = {
            "setup_before_first_submit_ms": round((first_sent_at - started) * 1000, 2),
            "submission_to_completion_ms": round((completed_at - first_sent_at) * 1000, 2),
            "first_journal_event_ms": event_latency_ms(events, first_submit_epoch_ms),
            "first_runtime_event_ms": event_latency_ms(events, first_submit_epoch_ms, "runtime"),
            "first_assistant_message_ms": first_assistant_message_ms,
            "first_token_ms": None,
            "end_to_end_ms": round((completed_at - started) * 1000, 2),
        }
        record["status"] = ("needs_user" if any(x.get("user_action_required") for x in turn_runs)
                             else "timeout" if any(x["timed_out"] for x in turn_runs) else turn.get("status"))
        response = turn.get("response", "") or ""
        private_calls = [((event_data(e).get("event") or {}).get("data") or {})
                         for e in events if e.get("kind") == "harness_event" and
                         ((event_data(e).get("event") or {}).get("type") == "tool/call") and
                         is_account_data_tool(((event_data(e).get("event") or {}).get("data") or {}).get("name"),
                                              ((event_data(e).get("event") or {}).get("data") or {}).get("arguments"))]
        private_call_ids = {x.get("callId") for x in private_calls if x.get("callId")}
        private_steps = {(x.get("turn"), x.get("step")) for x in private_calls}
        redact_final_output = private_calls_may_have_returned_data(events, private_calls)
        record["turn_results"] = [{"turn_id": item["turn"]["id"], "user_input": safe(message),
                                   "trace_start": item["trace_start"], "trace_end": item["trace_end"],
                                   "status": "timeout" if item["timed_out"] else item["turn"].get("status"),
                                   "assistant_response": ("[REDACTED: answer contains account-derived campus data]"
                                       if private_calls_may_have_returned_data(
                                           item["events"], [((event_data(e).get("event") or {}).get("data") or {})
                                               for e in item["events"] if e.get("kind") == "harness_event" and
                                               ((event_data(e).get("event") or {}).get("type") == "tool/call") and
                                               is_account_data_tool(((event_data(e).get("event") or {}).get("data") or {}).get("name"),
                                                                    ((event_data(e).get("event") or {}).get("data") or {}).get("arguments"))])
                                       else safe(item["turn"].get("response", "")))}
                                  for message, item in zip(messages, turn_runs)]
        record["final_output"] = safe(response)
        if redact_final_output:
            record["final_output_sha256"] = _result_digest(response)
            record["final_output"] = "[REDACTED: answer contains account-derived campus data]"
            record["final_output_redacted"] = True
        record["error"] = safe(turn.get("error"))
        cleaned = [privacy_event(e, private_call_ids, private_steps) for e in events]
        record["trace"] = cleaned
        record["tool_availability_checks"] = tool_availability_checks(scenario, events)
        record["current_tool_schema_comparison"] = compare_model_schemas_to_local(events)
        record["metrics"] = metrics(cleaned)
        record["metrics"]["user_action_requests"] = pending_user_actions(events)
        record["metrics"]["human_intervention_events"] = (record["metrics"]["human_confirmations"] +
                                                             record["metrics"]["user_action_requests"])
        record["metrics"]["first_token_ms"] = None
        record["metrics"]["first_token_note"] = "当前 SDK 事件流未提供用户可见首 token 时间戳"
        scoring_events = turn_runs[-1]["events"] if turn_runs else events
        record["evaluation"] = evaluate(scenario, turn, scoring_events, record.get("budget_stop"))
        if redact_final_output and record["evaluation"].get("source_grounding"):
            record["evaluation"]["public_source_oracle"] = public_source_oracle_summary(
                record["evaluation"].pop("source_grounding"))
        tool_quality, efficiency, autonomy = execution_dimensions(
            scenario, cleaned, record["metrics"], record["timings"], len(messages))
        record["evaluation"].update(tool_quality=tool_quality, efficiency=efficiency, autonomy=autonomy)
        record["evaluation"]["hallucination"] = "not_automatically_verifiable; human source audit required for factual claims"
        if record.get("budget_stop"):
            record["evaluation"]["failure_category"] = "I_触发基准调用预算"
        record["diagnosis"] = diagnose(scenario, turn, cleaned, record["evaluation"], record.get("budget_stop"))
        record["layers"] = {
            "tool_foundation": {"status": "not_run", "reason": "本轮仅走正常 Agent API；使用 --direct-tools 将作为后续独立实验层"},
            "harness_dispatch": {"status": "observed" if any(e.get("kind") in {"tool_started", "tool_result", "harness_event"} for e in cleaned) else "no_tool_event",
                                 "tool_calls": record["metrics"]["tool_calls"]},
            "agent_task": record["evaluation"],
            "user_experience": {"status": "action_required" if record["metrics"]["user_action_requests"] else "needs_human_review",
                                "confirmations": record["metrics"]["human_confirmations"],
                                "action_requests": record["metrics"]["user_action_requests"],
                                "clarification_turns": sum(1 for e in cleaned if e.get("kind") == "harness_event" and "question" in str(e).lower())}
        }
        record["finished_at"] = now()
        return record
    except Exception as exc:
        record["error"] = {"stage": "setup_or_api", "type": type(exc).__name__, "message": safe(str(exc))}
        record["diagnosis"] = {"confirmed": [], "hypotheses": ["API 未运行、Provider 未配置或请求入口不可达"],
                               "evidence": [safe(str(exc))], "next_experiment": "检查本地 API 健康状态与已启用模型配置；不要在轨迹中粘贴密钥"}
        record["finished_at"] = now()
        return record


def settings_safe(obj):
    return json.dumps(safe(obj), ensure_ascii=False)[:1200]


def metrics(events):
    models = [event_data(e) for e in events if e.get("kind") == "model_call"]
    tools = [event_data(e) for e in events if e.get("kind") == "tool_result"]
    starts = [event_data(e) for e in events if e.get("kind") == "tool_started"]
    native_calls = []
    native_results = []
    model_steps = set()
    observed_usage = []
    model_elapsed = []
    tool_step_spans = []
    web_blocked_provider_results = 0
    web_blocked_repeat_shortcuts = 0
    domain_web_blocked_provider_results = 0
    domain_web_blocked_repeat_shortcuts = 0
    calls_by_step, results_by_step = {}, {}
    previous_time = None
    for row in events:
        if row.get("kind") != "harness_event":
            continue
        event = (event_data(row).get("event") or {})
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "tool/call":
            native_calls.append(data)
            stamp = event.get("time")
            if isinstance(stamp, (int, float)):
                key = (data.get("turn"), data.get("step"))
                calls_by_step.setdefault(key, []).append(stamp)
        elif kind == "tool/result":
            native_results.append(data)
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            content = " ".join(block.get("text", "") for block in message.get("content", [])
                               if isinstance(block, dict) and isinstance(block.get("text"), str))
            if "no network request was repeated" in content:
                web_blocked_repeat_shortcuts += 1
            elif "WEB_BLOCKED_URL" in content and message.get("isError"):
                web_blocked_provider_results += 1
            stamp = event.get("time")
            if isinstance(stamp, (int, float)):
                key = (data.get("turn"), data.get("step"))
                results_by_step.setdefault(key, []).append(stamp)
        elif kind == "assistant/message":
            model_steps.add((data.get("turn"), data.get("step")))
            if isinstance(data.get("usage"), dict): observed_usage.append(data["usage"])
            stamp = event.get("time")
            if isinstance(stamp, (int, float)) and previous_time is not None and stamp >= previous_time:
                model_elapsed.append(stamp - previous_time)
        stamp = event.get("time")
        if isinstance(stamp, (int, float)): previous_time = stamp
    for key, begins in calls_by_step.items():
        ends = results_by_step.get(key, [])
        if ends:
            tool_step_spans.append(max(ends) - min(begins))
    model_calls = len(models) or len(model_steps)
    # The native SDK event stream and the backend tool_started/tool_result
    # instrumentation describe the same invocation. Prefer the SDK's call
    # events when present; otherwise fall back to backend starts/results.
    # Counting both streams doubled observed calls on the native provider path.
    tool_count = len(native_calls) if native_calls else (len(starts) if starts else len(tools))
    for row in tools:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = str(error.get("code") or result.get("error_code") or "").upper()
        text = json.dumps(result, ensure_ascii=False)
        if code == "WEB_BLOCKED_URL" or "WEB_BLOCKED_URL" in text:
            if "no network request was repeated" in text:
                domain_web_blocked_repeat_shortcuts += 1
            else:
                domain_web_blocked_provider_results += 1
    usage = [x.get("usage") for x in models if x.get("usage")] or observed_usage
    model_selected = [n for x in models for n in x.get("selected_tools", [])]
    if not model_selected:
        model_selected = [x.get("name") for x in native_calls if x.get("name")]
    has_schema_snapshot = any(isinstance(x.get("tool_schema_snapshot"), dict) for x in models)
    schema_truncated = any((x.get("tool_schema_snapshot") or {}).get("truncated") is True for x in models)
    if has_schema_snapshot:
        schema_note = "本轮模型可见 schema 已记录；个别超限快照仅有摘要哈希和数量" if schema_truncated else "本轮模型可见 schema 已记录完整快照与哈希"
    else:
        schema_note = "旧轨迹没有本轮 schema 快照；available_tools 仅是名称观察，不足以证明完整 schema"
    if any(x.get("source") == "native_provider_llm_stream" for x in models):
        model_time_source = "Harness llm/stream wall time; includes SDK stream handling"
    elif models:
        model_time_source = "internal_model wall clock"
    elif native_calls:
        model_time_source = "native assistant event-gap estimate"
    else:
        model_time_source = "no model timing event observed"
    return {"model_calls": model_calls,
            "model_time_ms": round(sum(x.get("duration_ms", 0) for x in models), 2),
            "model_time_estimated_ms": round(sum(model_elapsed), 2) if model_elapsed else None,
            "model_time_source": model_time_source,
            "tool_calls": tool_count, "tool_time_ms": round(sum(x.get("duration_ms", 0) for x in tools), 2),
            "tool_time_estimated_ms": round(sum(tool_step_spans), 2) if tool_step_spans else None,
            "tool_time_source": "domain tool wall clock; native tools measured as step batch spans (parallel calls overlap)" if native_calls else "domain tool wall clock",
            "tool_errors": len(tool_error_evidence(events)),
            "web_blocked_provider_results": (web_blocked_provider_results if native_calls else domain_web_blocked_provider_results),
            "web_blocked_repeat_shortcuts": (web_blocked_repeat_shortcuts if native_calls else domain_web_blocked_repeat_shortcuts),
            "retry_signals": sum(1 for e in events if "retry" in str(e.get("kind", "")).lower()),
            "human_confirmations": sum(1 for e in events if "confirm" in str(e.get("kind", "")).lower() or "approval" in str(e.get("kind", "")).lower()),
            "model_tools_seen": list(dict.fromkeys(n for x in models for n in x.get("available_tools", []))),
            "model_tools_seen_note": schema_note,
            "model_tools_selected": model_selected,
            "tool_started_events": len(native_calls) if native_calls else len(starts), "token_usage": usage,
            "tokens_input_observed": sum(int(x.get("inputTokens", 0)) for x in usage),
            "tokens_output_observed": sum(int(x.get("outputTokens", 0)) for x in usage),
            "tokens_cache_read_observed": sum(int(x.get("cacheReadTokens", 0)) for x in usage),
            "tokens_cache_write_observed": sum(int(x.get("cacheWriteTokens", 0)) for x in usage),
            "tokens_total_observed": sum(int(x.get("totalTokens", 0)) for x in usage), "estimated_cost": None}


def schema_snapshot_summary(batch_dir):
    """Summarize model-visible tool schema fingerprints without exporting schemas."""
    fingerprints = {}
    per_tool = {}
    calls = 0
    truncated = 0
    for path in sorted((Path(batch_dir) / "runs").glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        trace = row.get("trace") if isinstance(row.get("trace"), list) else []
        for event in trace:
            if not isinstance(event, dict) or event.get("kind") != "model_call":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            snapshot = data.get("tool_schema_snapshot")
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sha256"), str):
                continue
            calls += 1
            truncated += int(snapshot.get("truncated") is True)
            fingerprints[snapshot["sha256"]] = fingerprints.get(snapshot["sha256"], 0) + 1
            for schema in snapshot.get("schemas", []) if isinstance(snapshot.get("schemas"), list) else []:
                if not isinstance(schema, dict) or not isinstance(schema.get("name"), str):
                    continue
                name = schema["name"]
                signature = hashlib.sha256(json.dumps(schema, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                entry = per_tool.setdefault(name, {"count": 0, "fingerprints": {}})
                entry["count"] += 1
                entry["fingerprints"][signature] = entry["fingerprints"].get(signature, 0) + 1
    return {"model_calls_with_schema_snapshot": calls,
            "truncated_snapshots": truncated,
            "distinct_sha256": sorted(fingerprints),
            "fingerprint_call_counts": fingerprints,
            "per_tool": per_tool}


def canonical_tool_name(name):
    value = str(name or "").lower()
    if value.startswith("campus_"):
        value = value[len("campus_"):]
    return value.replace("_", ".")


_LOCAL_DOMAIN_SCHEMAS = None
_LOCAL_DOMAIN_SCHEMA_ERROR = None


def local_domain_tool_schemas():
    """Load the current checkout's domain schemas for drift analysis, not execution."""
    global _LOCAL_DOMAIN_SCHEMAS, _LOCAL_DOMAIN_SCHEMA_ERROR
    if _LOCAL_DOMAIN_SCHEMAS is not None:
        return _LOCAL_DOMAIN_SCHEMAS
    if _LOCAL_DOMAIN_SCHEMA_ERROR is not None:
        return None
    old_path = list(sys.path)
    try:
        for path in (ROOT / "apps" / "api", ROOT / "packages" / "learning" / "src", ROOT / "connectors"):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        from app.agent_bridge.tools import schemas
        _LOCAL_DOMAIN_SCHEMAS = schemas()
        return _LOCAL_DOMAIN_SCHEMAS
    except Exception as exc:
        _LOCAL_DOMAIN_SCHEMA_ERROR = type(exc).__name__
        return None
    finally:
        sys.path[:] = old_path


def compare_model_schemas_to_local(events, local_schemas=None):
    """Compare captured campus schemas against this checkout without persisting schemas."""
    expected_rows = local_schemas if local_schemas is not None else local_domain_tool_schemas()
    if not isinstance(expected_rows, list):
        return {"status": "local_schema_unavailable", "reason": _LOCAL_DOMAIN_SCHEMA_ERROR,
                "per_model_call": [], "per_tool": {}}
    expected = {}
    for row in expected_rows:
        if not isinstance(row, dict):
            continue
        name = row.get("model_name") or row.get("name")
        if not isinstance(name, str):
            continue
        parameters = row.get("parameters") if isinstance(row.get("parameters"), dict) else row.get("input_schema")
        expected[canonical_tool_name(name)] = {"description": row.get("description"),
                                                "parameters": parameters if isinstance(parameters, dict) else {}}
    per_tool, per_call = {}, []
    for event in events:
        if event.get("kind") != "model_call":
            continue
        data = event_data(event)
        snapshot = data.get("tool_schema_snapshot")
        schemas = snapshot.get("schemas") if isinstance(snapshot, dict) else None
        seen, mismatches = [], []
        for schema in schemas if isinstance(schemas, list) else []:
            if not isinstance(schema, dict):
                continue
            name = canonical_tool_name(schema.get("name"))
            if name not in expected:
                continue
            observed = {"description": schema.get("description"),
                        "parameters": schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}}
            wanted = expected[name]
            desc_diff = observed["description"] != wanted["description"]
            params_diff = observed["parameters"] != wanted["parameters"]
            observed_hash = _result_digest(observed)
            expected_hash = _result_digest(wanted)
            item = per_tool.setdefault(name, {"observations": 0, "description_mismatches": 0,
                                               "parameter_mismatches": 0, "observed_sha256": {},
                                               "expected_sha256": expected_hash})
            item["observations"] += 1
            item["description_mismatches"] += int(desc_diff)
            item["parameter_mismatches"] += int(params_diff)
            item["observed_sha256"][observed_hash] = item["observed_sha256"].get(observed_hash, 0) + 1
            if desc_diff or params_diff:
                mismatches.append(name)
            seen.append(name)
        per_call.append({"seq": event.get("seq"), "observed_domain_tools": len(seen),
                         "mismatched_tools": sorted(set(mismatches)),
                         "snapshot_complete": isinstance(snapshot, dict) and snapshot.get("truncated") is False})
    if not per_call or not per_tool:
        status = "not_observed"
    else:
        status = "drifted" if any(x["description_mismatches"] or x["parameter_mismatches"]
                                   for x in per_tool.values()) else "matched"
    return {"status": status, "compared_tools": sorted(per_tool),
            "mismatched_tools": sorted(name for name, value in per_tool.items()
                                       if value["description_mismatches"] or value["parameter_mismatches"]),
            "per_tool": per_tool, "per_model_call": per_call}


def tool_availability_checks(scenario, events):
    """Compare exact required tools with each complete model-visible schema."""
    alias_candidates = {
        "search": ["campus.query", "campus.sync", "web_search", "web_fetch"],
        "notice": ["campus.query", "campus.sync", "campus.notice.read"],
        "college": ["campus.query", "campus.sync", "campus.notice.read"],
        "academic": ["campus.academics", "campus.query", "campus.sync"],
        "schedule": ["campus.academics", "campus.query", "campus.sync"],
        "campus": ["campus.status", "campus.query", "campus.sync"],
    }
    required = scenario.get("required_tools") or []
    exact = sorted({canonical_tool_name(name) for name in required
                    if isinstance(name, str) and ("." in name or "_" in name)})
    groups = [{"alias": name, "candidates": [canonical_tool_name(x) for x in alias_candidates[name]]}
              for name in dict.fromkeys(required) if isinstance(name, str) and name in alias_candidates]
    observable_names = set(exact) | {candidate for group in groups for candidate in group["candidates"]}
    calls = []
    for row in events:
        if row.get("kind") != "model_call":
            continue
        data = event_data(row)
        snapshot = data.get("tool_schema_snapshot")
        schemas = snapshot.get("schemas") if isinstance(snapshot, dict) else None
        names = sorted({canonical_tool_name(item.get("name")) for item in schemas
                        if isinstance(item, dict) and isinstance(item.get("name"), str)}) if isinstance(schemas, list) else []
        calls.append({"seq": row.get("seq"),
                      "complete": isinstance(snapshot, dict) and snapshot.get("truncated") is False and isinstance(schemas, list),
                      "visible_count": len(names), "visible_required": sorted(observable_names & set(names)),
                      "schema_sha256": snapshot.get("sha256") if isinstance(snapshot, dict) else None})
    status = {}
    for tool in exact:
        visible_at = [index for index, call in enumerate(calls) if tool in call["visible_required"]]
        if visible_at:
            status[tool] = "available_first_call" if visible_at[0] == 0 else "loaded_later"
        elif calls and all(call["complete"] for call in calls):
            status[tool] = "unavailable"
        else:
            status[tool] = "not_observed"
    group_status = []
    for group in groups:
        visible_at = [index for index, call in enumerate(calls)
                      if set(group["candidates"]) & set(call["visible_required"])]
        if visible_at:
            group_state = "available_first_call" if visible_at[0] == 0 else "loaded_later"
        elif calls and all(call["complete"] for call in calls):
            group_state = "unavailable"
        else:
            group_state = "not_observed"
        group_status.append({**group, "status": group_state,
                             "visible_candidates_by_call": [sorted(set(group["candidates"]) &
                                 set(call["visible_required"])) for call in calls]})
    unavailable_count = sum(value == "unavailable" for value in status.values()) + sum(
        group["status"] == "unavailable" for group in group_status)
    not_observed_count = sum(value == "not_observed" for value in status.values()) + sum(
        group["status"] == "not_observed" for group in group_status)
    return {"required_exact_tools": exact, "status": status, "model_calls": calls,
            "required_alias_groups": group_status,
            "unavailable_count": unavailable_count,
            "not_observed_count": not_observed_count,
            "note": "unavailable 仅表示该工具在本轮所有完整模型可见 schema 中缺失；可见但未选用属于工具选择问题"}


def schema_factor_comparison(before_schema, after_schema, target_tools):
    """Check a declared one-factor tool-schema change without dumping schemas."""
    left_tools = {canonical_tool_name(name): data.get("fingerprints", {})
                  for name, data in before_schema.get("per_tool", {}).items()}
    right_tools = {canonical_tool_name(name): data.get("fingerprints", {})
                   for name, data in after_schema.get("per_tool", {}).items()}
    targets = [canonical_tool_name(tool) for tool in target_tools]
    details = {}
    for target in targets:
        left_target, right_target = left_tools.get(target, {}), right_tools.get(target, {})
        details[target] = {"before_fingerprints": left_target, "after_fingerprints": right_target,
            "stable_within_before": len(left_target) == 1, "stable_within_after": len(right_target) == 1,
            "changed_between_conditions": bool(left_target and right_target and set(left_target) != set(right_target))}
    non_target = sorted(name for name in left_tools.keys() & right_tools.keys()
        if name not in set(targets) and set(left_tools[name]) != set(right_tools[name]))
    verified = bool(targets) and all(item["stable_within_before"] and item["stable_within_after"] and
                                     item["changed_between_conditions"] for item in details.values()) and not non_target
    return {"target_tools": targets, "target_tool_schema_details": details,
            "non_target_tool_schema_differences": non_target,
            "expected_one_factor_schema_change_verified": verified}


def execution_dimensions(scenario, events, run_metrics, timings, user_turn_count=1):
    """Attach observed execution evidence without converting absent signals to scores."""
    calls, results = [], []
    call_keys, result_keys = set(), set()
    for item in events:
        kind = item.get("kind")
        data = event_data(item)
        if kind == "harness_event":
            inner = data.get("event") or {}
            payload = inner.get("data") or {}
            if inner.get("type") == "tool/call":
                key = payload.get("callId") or (payload.get("turn"), payload.get("step"), payload.get("name"))
                if key not in call_keys:
                    call_keys.add(key)
                    calls.append({"key": key, "name": payload.get("name")})
            elif inner.get("type") == "tool/result":
                key = native_tool_result_call_id(payload) or (payload.get("turn"), payload.get("step"))
                if key not in result_keys:
                    result_keys.add(key)
                    results.append(key)
        elif kind == "tool_started":
            key = data.get("call_id") or (data.get("name"), len(calls))
            if key not in call_keys:
                call_keys.add(key)
                calls.append({"key": key, "name": data.get("name")})
        elif kind == "tool_result":
            key = data.get("call_id") or (data.get("name"), len(results))
            if key not in result_keys:
                result_keys.add(key)
                results.append(key)
    # SDK and backend events mirror the same call. Prefer native call/result
    # pairs whenever they are present, matching metrics()' counting policy.
    native_calls = [x for x in events if x.get("kind") == "harness_event" and
                    (event_data(x).get("event") or {}).get("type") == "tool/call"]
    if native_calls:
        native_call_rows, native_result_links = [], []
        call_event_seq_to_index = {}
        for item in events:
            if item.get("kind") != "harness_event":
                continue
            inner = event_data(item).get("event") or {}
            payload = inner.get("data") or {}
            if inner.get("type") == "tool/call":
                call_event_seq_to_index[inner.get("seq")] = len(native_call_rows)
                native_call_rows.append(payload)
            elif inner.get("type") == "tool/result":
                native_result_links.append((payload, inner.get("sourceEventSeqs") or []))
        correlated_calls = set()
        calls_by_id = {}
        for index, payload in enumerate(native_call_rows):
            call_id = payload.get("callId") or payload.get("call_id")
            if call_id:
                calls_by_id.setdefault(call_id, []).append(index)
        results_without_id = []
        for result, source_sequences in native_result_links:
            result_id = native_tool_result_call_id(result)
            matches = calls_by_id.get(result_id, []) if result_id else []
            if len(matches) == 1:
                correlated_calls.add(matches[0])
            elif not result_id and len(source_sequences) == 1:
                # Harness journal lineage is a stronger correlation key than
                # same-step proximity, especially for parallel tools.
                source_index = call_event_seq_to_index.get(source_sequences[0])
                if source_index is not None:
                    correlated_calls.add(source_index)
                else:
                    results_without_id.append(result)
            elif not result_id:
                results_without_id.append(result)
        # Some SDK journal versions omit callId from tool/result while keeping
        # turn and step. Use that fallback only when exactly one unmatched call
        # and one id-less result share the step; parallel calls stay ambiguous.
        call_steps, result_steps = {}, {}
        for index, call in enumerate(native_call_rows):
            if index in correlated_calls or call.get("turn") is None or call.get("step") is None:
                continue
            call_steps.setdefault((call.get("turn"), call.get("step")), []).append(index)
        for result in results_without_id:
            if result.get("turn") is None or result.get("step") is None:
                continue
            result_steps.setdefault((result.get("turn"), result.get("step")), []).append(result)
        for step, call_indexes in call_steps.items():
            step_results = result_steps.get(step, [])
            if len(call_indexes) == 1 and len(step_results) == 1:
                correlated_calls.add(call_indexes[0])
        missing = [payload.get("name") for index, payload in enumerate(native_call_rows)
                   if index not in correlated_calls]
        calls = [{"key": payload.get("callId") or (payload.get("turn"), payload.get("step"), payload.get("name")),
                  "name": payload.get("name")} for payload in native_call_rows]
    else:
        missing = [row["name"] for row in calls if row["key"] not in set(results)]
    aliases = (scenario.get("eval") or {}).get("tools_any") or []
    observed_names = [row["name"] for row in calls if row.get("name")]
    matched_aliases = [alias for alias in aliases if any(str(alias).lower() in str(name).lower()
                                                        for name in observed_names)]
    selection = tool_selection_evidence(scenario.get("eval") or {}, events)
    tool_quality = {"status": "no_tool_calls" if not calls else "observed",
        "tool_calls": len(calls), "tool_errors": run_metrics.get("tool_errors"),
        "calls_without_correlated_result": len(missing), "unmatched_call_tools": missing,
        "observed_tool_names": observed_names,
        "redundant_tool_candidates": selection["redundant_tools"],
        "redundant_tool_candidates_executed": selection["redundant_tools_executed"],
        "accepted_tool_aliases": aliases, "matched_tool_aliases": matched_aliases,
        "note": "调用/结果优先按 call ID 关联；若结果事件缺 ID，仅在 turn/step 内一对一时回退关联，并发歧义保留为未关联"}
    efficiency = {"status": "measured" if timings.get("end_to_end_ms") is not None else "unavailable",
        "end_to_end_ms": timings.get("end_to_end_ms"),
        "submission_to_completion_ms": timings.get("submission_to_completion_ms"),
        "first_assistant_message_ms": timings.get("first_assistant_message_ms"),
        "first_token_ms": timings.get("first_token_ms"),
        "model_calls": run_metrics.get("model_calls"), "tool_calls": run_metrics.get("tool_calls"),
        "retry_signals": run_metrics.get("retry_signals"),
        "tokens_input_observed": run_metrics.get("tokens_input_observed"),
        "tokens_output_observed": run_metrics.get("tokens_output_observed"),
        "estimated_cost": run_metrics.get("estimated_cost"),
        "threshold_status": "not_calibrated", "note": "耗时和调用量为实测值；效率优劣阈值尚未按场景标定"}
    autonomy = {"status": "event_signals_only_needs_review",
        "confirmation_events_observed": run_metrics.get("human_confirmations"),
        "scripted_user_turns": user_turn_count,
        "manual_login_events": None, "unnecessary_clarifications": None,
        "note": "当前 API 轨迹不能完整观察点击、外部登录和追问是否必要；未观测不等于零"}
    return tool_quality, efficiency, autonomy


def confirmation_summary(rows):
    """Report observed approvals and explicit user-action handoffs separately."""
    counts = [row.get("metrics", {}).get("human_confirmations") for row in rows]
    counts = [value for value in counts if isinstance(value, (int, float)) and not isinstance(value, bool)]
    actions = [row.get("metrics", {}).get("user_action_requests") for row in rows]
    actions = [value for value in actions if isinstance(value, (int, float)) and not isinstance(value, bool)]
    interventions = [row.get("metrics", {}).get("human_intervention_events") for row in rows]
    interventions = [value for value in interventions if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return {
        "mean_recorded_confirmation_events": round(sum(counts) / len(counts), 2) if counts else None,
        "confirmation_count_runs": len(counts),
        "mean_user_action_requests": round(sum(actions) / len(actions), 2) if actions else None,
        "user_action_count_runs": len(actions),
        "mean_human_interventions": round(sum(interventions) / len(interventions), 2) if interventions else None,
        "intervention_count_runs": len(interventions),
        "confirmation_observation_note": "Counts are explicit Agent journal approvals plus tool results marked needs_user; external logins and manual steps outside the journal remain unobservable.",
    }


def diagnose(scenario, turn, events, evaluation, budget_stop=None):
    confirmed = []
    user_action_count = pending_user_actions(events)
    if user_action_count:
        confirmed.append(f"工具显式返回 needs_user ×{user_action_count}；自动化基准未代替用户确认")
    if turn.get("status") != "succeeded":
        confirmed.append(f"turn terminal status={turn.get('status')}; error={(turn.get('error') or {}).get('code', 'none')}")
    if not evaluation.get("observed_tools"):
        confirmed.append("完整已收集轨迹中未观察到可识别的工具调用")
    if any(x.get("kind") == "model_error" for x in events):
        confirmed.append("存在 model_error 事件")
    if budget_stop:
        confirmed.append(f"实验运行器在 {budget_stop.get('per_run_limit')} 轮/场景调用上限触发取消")
    assignment_oracle = evaluation.get("assignment_date_evidence") or {}
    if assignment_oracle.get("status") == "mismatch":
        confirmed.append("脱敏前确定性比对确认：最终回答至少包含一个作业来源不支持的日期（仅记录聚合计数）")
    native_failures = tool_error_evidence(events)
    for failure in native_failures:
        confirmed.append(f"{failure.get('channel')} 工具 {failure.get('name') or 'unknown'} 返回错误"
                         + (f" code={failure['error_code']}" if failure.get("error_code") else ""))
    unsupported = [event_data(x) for x in events if x.get("kind") == "tool_result" and
                   event_data(x).get("name") in {"campus.query", "campus.sync"} and
                   (((event_data(x).get("result") or {}).get("error") or {}).get("code") or
                    (event_data(x).get("result") or {}).get("error_code")) == "UNSUPPORTED"]
    for item in unsupported:
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        confirmed.append(f"{item.get('name')} capability={args.get('capability')} 返回 UNSUPPORTED")
    tool_calls = []
    for event in events:
        if event.get("kind") != "harness_event":
            continue
        inner = event_data(event).get("event") or {}
        if inner.get("type") == "tool/call":
            item = inner.get("data") or {}
            if item.get("name"):
                tool_calls.append(item["name"])
    if not tool_calls:
        tool_calls = [event_data(e).get("name") for e in events if e.get("kind") == "tool_started"]
    from collections import Counter
    counts = Counter(tool_calls)
    repeated = {name: count for name, count in counts.items() if count >= 3 and
                any(tag in name.lower() for tag in ("search", "fetch", "navigate"))}
    if repeated:
        confirmed.append("观察到同类工具多次执行（是否冗余尚未证实）：" + ", ".join(f"{k} ×{v}" for k, v in sorted(repeated.items())))
    root_cause = root_cause_attribution(scenario, turn, events, evaluation, budget_stop, repeated)
    if user_action_count:
        root_cause["primary"] = {"code": "K", "category": ROOT_CAUSE_LABELS["K"], "confidence": "confirmed",
            "evidence": ["关联的 Harness tool/result 明确返回 needs_user；这是安全边界要求的人工步骤，不是工具执行故障"]}
        root_cause["failure_step"] = "浏览器操作安全门等待用户确认"
        root_cause["engineering_improvements"] = ["评测器应将需人工操作作为独立终态并停止空等",
                                                   "只读网页导航是否可安全自动化需单独审查页面语义与请求方法，不放宽提交/写入确认"]
    if assignment_oracle.get("status") == "mismatch":
        root_cause["primary"] = {"code": "F", "category": ROOT_CAUSE_LABELS["F"], "confidence": "confirmed",
            "evidence": ["最终回答日期与 assignments.list 返回日期集合不一致；报告不持久化具体日期"]}
        root_cause["failure_step"] = "最终回答构造"
        root_cause["engineering_improvements"] = ["保留日期来源引用或按结构化作业记录生成截止日期，遇到不匹配时拒绝输出未验证日期"]
    hypotheses = [item["category"] for item in root_cause["alternatives"]]
    capability_kinds = {((item.get("arguments") or {}).get("capability")) for item in unsupported}
    if any(str(x.get("error_code") or "").upper() == "WEB_BLOCKED_URL" for x in native_failures):
        next_experiment = "此 URL 被网页工具的公网地址安全策略拦截；核验官方站点时优先用已登记 campus 通知来源或允许的授权浏览器，不要通过换域名/改网络策略绕过限制。"
    elif "college_notices" in capability_kinds:
        next_experiment = "单独验证当前账号是否有可匹配学院通知源；对全校公开通知比较只用公共来源的路径，避免额外尝试学院个性化来源。"
    elif "courses" in capability_kinds:
        next_experiment = "用明确学期范围的只读参数直测教务课程查询；确认授权后再测 Agent 是否正确传递学期。"
    elif repeated:
        next_experiment = "逐次比较检索 query 与新增来源；只有证实后续调用未增加可用证据时，才设计单变量停止条件对照，不据调用次数直接修改工具策略。"
    else:
        next_experiment = "先复查脱敏事件，区分工具直测、Harness 调度和 Agent 意图后再改代码。"
    return {"confirmed": confirmed, "hypotheses": hypotheses,
            "root_cause": root_cause["primary"], "other_possible_causes": root_cause["alternatives"],
            "failure_step": root_cause["failure_step"],
            "engineering_improvements": root_cause["engineering_improvements"],
            "evidence": [{"kind":e.get("kind"),"seq":e.get("seq"),"data":safe(event_data(e))} for e in events if e.get("kind") in {"model_error","tool_started","tool_result","harness_event","runtime","error"}][:40],
            "next_experiment": ("在当前场景使用更高但仍有限的调用上限复跑一次，检查现有搜索结果能否生成带来源的摘要；同时避免反复导航同一域名。" if budget_stop else next_experiment)}


ROOT_CAUSE_LABELS = {
    "A": "工具不存在或能力缺失", "B": "工具执行失败", "C": "模型未选择合适工具",
    "D": "Harness 调度或执行失败", "E": "工具结果未回传/未观察到后续模型处理",
    "F": "模型未正确利用工具结果", "G": "多步骤任务组合失败",
    "H": "认证、网络或外部依赖失败", "I": "任务成功但存在效率/人工介入候选问题",
    "J": "评测环境或评分标准不足",
    "K": "安全边界要求人工确认（非系统故障）",
}


def root_cause_attribution(scenario, turn, events, evaluation, budget_stop=None, repeated=None):
    """Return evidence-backed A-J attribution without presenting hypotheses as facts."""
    candidates = []
    def add(code, confidence, evidence):
        candidates.append({"code": code, "category": ROOT_CAUSE_LABELS[code],
                           "confidence": confidence, "evidence": evidence})

    if budget_stop:
        add("J", "confirmed", [f"runner call budget stopped the run at {budget_stop.get('observed_total')} observed calls"])
    user_actions = pending_user_actions(events)
    if user_actions:
        add("K", "confirmed", [f"tool result explicitly returned needs_user ({user_actions} event(s)); no automatic confirmation was sent"])
    error = turn.get("error") or {}
    code = str(error.get("code", "")).upper()
    deadline_timeout = turn.get("status") == "timeout" and not code
    if deadline_timeout:
        add("J", "confirmed", ["configured per-task deadline expired before the Agent turn returned a final response"])
    failures = tool_error_evidence(events)
    failure_codes = {str(item.get("error_code") or "").upper() for item in failures}
    all_codes = failure_codes | ({code} if code else set())
    external_tags = ("AUTH", "SESSION", "WEBVPN", "TIMEOUT", "NETWORK", "CONNECTION", "DNS", "WEB_BLOCKED")
    if any(any(tag in item for tag in external_tags) for item in all_codes):
        add("H", "confirmed", [f"observed external/authorization error code={item}" for item in sorted(all_codes)
                                  if any(tag in item for tag in external_tags)])
    invalid_inputs = [item for item in failures if str(item.get("error_code") or "").upper() == "INVALID_INPUT"]
    if invalid_inputs:
        add("C", "confirmed", [f"tool {item.get('name') or 'unknown'} rejected the submitted arguments with INVALID_INPUT"
                                  for item in invalid_inputs])
    if any(any(tag in item for tag in ("TOOL_NOT_FOUND", "UNKNOWN_TOOL", "CAPABILITY_NOT_IMPLEMENTED"))
           for item in all_codes):
        add("A", "confirmed", [f"observed missing-tool/capability error code={item}" for item in sorted(all_codes) if item])
    generic_failures = [item for item in failures if str(item.get("error_code") or "").upper() != "INVALID_INPUT"]
    if generic_failures and not any(c["code"] == "H" for c in candidates):
        add("B", "confirmed", [f"{item.get('channel')} tool {item.get('name') or 'unknown'} returned an error"
                                  + (f" code={item.get('error_code')}" if item.get("error_code") else "")
                                  for item in generic_failures])

    native_calls, native_results, assistant_steps = {}, {}, set()
    for row in events:
        if row.get("kind") != "harness_event":
            continue
        event = event_data(row).get("event") or {}
        data = event.get("data") or {}
        key = (data.get("turn"), data.get("step"))
        if event.get("type") == "tool/call":
            native_calls.setdefault(key, []).append({"name": data.get("name"), "seq": event.get("seq"), "callId": data.get("callId")})
        elif event.get("type") == "tool/result":
            native_results.setdefault(key, []).append({"isError": bool((data.get("message") or {}).get("isError")), "seq": event.get("seq")})
        elif event.get("type") == "assistant/message":
            assistant_steps.add(key)
    missing_results = [key for key in native_calls if key not in native_results]
    # A hard model-call budget can cancel a turn while tool calls are still in flight.
    # In that case, missing result/follow-up events are expected truncation artifacts,
    # not evidence of a Harness dispatch or result-handoff fault.
    if missing_results and not failures and not budget_stop:
        add("D", "suspected", [f"tool/call turn={t} step={s} has no correlated result in the captured trace" for t, s in missing_results])
    no_followup = [key for key, results in native_results.items() if key in native_calls and not any(
        t == key[0] and isinstance(step, int) and isinstance(key[1], int) and step > key[1]
        for t, step in assistant_steps)]
    if no_followup and evaluation.get("success") != "成功" and not budget_stop:
        add("E", "suspected", [f"tool result turn={t} step={s} has no later assistant/message event" for t, s in no_followup])

    observed = evaluation.get("observed_tools") or event_tools(events)
    required = scenario.get("required_tools") or []
    availability = tool_availability_checks(scenario, events)
    unavailable = [name for name, state in availability.get("status", {}).items() if state == "unavailable"]
    if unavailable:
        add("A", "confirmed", ["required exact tool absent from every complete model-visible schema snapshot: " + name
                                  for name in unavailable])
    unavailable_groups = [group for group in availability.get("required_alias_groups", [])
                          if group.get("status") == "unavailable"]
    for group in unavailable_groups:
        add("A", "confirmed", ["no concrete candidate for required tool alias " + group["alias"] +
                                 " was present in any complete model-visible schema snapshot"])
    if evaluation.get("success") == "失败" and required and not observed:
        states = list(availability.get("status", {}).values()) + [
            item.get("status") for item in availability.get("required_alias_groups", [])]
        if not unavailable and not unavailable_groups and states and all(x == "not_observed" for x in states):
            add("J", "suspected", ["required-tool availability could not be assessed because schema snapshots were missing/truncated"])
        else:
            add("C", "suspected", ["required tool was available or availability was otherwise observed, but no tool call was observed"])
    selection_evidence = tool_selection_evidence(scenario.get("eval") or {}, events)
    executed_redundant = selection_evidence["redundant_tools_executed"]
    if executed_redundant:
        add("I", "suspected", ["scenario-designated redundant-candidate calls observed in trace: " + ", ".join(executed_redundant)])
    successful_results = sum(sum(1 for result in rows if not result["isError"])
                             for rows in native_results.values()) + sum(
        1 for row in events if row.get("kind") == "tool_result" and
        not tool_error_evidence([row]))
    multi_step = len(required) > 1 or len(scenario.get("messages") or []) > 1
    outcome = evaluation.get("success")
    reason = str(evaluation.get("reason") or "")
    if multi_step and successful_results >= 2 and outcome == "失败":
        add("G", "suspected", [f"multi-step scenario failed after {successful_results} successful-looking tool results"])
    has_results = bool(native_results or any(row.get("kind") == "tool_result" for row in events))
    if has_results and not failures and (outcome == "失败" or (outcome == "部分成功" and "对齐不足" in reason)):
        add("F", "suspected", [f"tool results were observed without a matching tool error, but answer use appears insufficient ({outcome})"])
    if outcome == "不可验证" or (outcome in {"失败", "部分成功"} and not candidates):
        add("J", "suspected", [evaluation.get("reason") or "no deterministic completion oracle for this outcome"])
    if evaluation.get("success") == "成功" and repeated:
        add("I", "suspected", ["repeated search/fetch/navigation tools observed; semantic redundancy has not been established"])

    priority = {"K": -1, "H": 0, "A": 1, "B": 2, "D": 3, "E": 4, "C": 5, "G": 6, "F": 7, "J": 8, "I": 9}
    if budget_stop or deadline_timeout:
        # Explicit runner cutoffs are confirmed censoring conditions; keep them
        # primary without inferring why an in-flight browser/provider call stalled.
        priority["J"] = -1
    candidates.sort(key=lambda item: (priority[item["code"]], item["confidence"] != "confirmed"))
    if candidates:
        primary = candidates[0]
    elif evaluation.get("success") == "成功":
        primary = {"code": None, "category": "未发现失败根因", "confidence": "not_applicable", "evidence": []}
    else:
        primary = {"code": "J", "category": ROOT_CAUSE_LABELS["J"],
                   "confidence": "suspected", "evidence": ["no failure evidence was observed; inspect the trace manually"]}
    code = primary["code"]
    suggestions = {
        "A": ["检查工具注册表、provider 暴露 schema 与能力映射；补直调覆盖测试。"],
        "B": ["用相同安全参数单独直调失败工具，按错误码修复执行器或外部依赖。"],
        "C": ["对照场景所需工具与模型可见工具；检查工具描述和选择策略。"],
        "D": ["增加 Harness call/result correlation 测试，核对参数解析、路由和取消路径。"],
        "E": ["记录带 callId/turn-step 的结果回灌关联，并在下一模型请求中验证结果可见性。"],
        "F": ["固定工具结果建立单变量 harness 回放，核对模型引用和答案字段。"],
        "G": ["对每个子步骤做直调与 Harness 对照，再以相同结果回放完整链路。"],
        "H": ["先单独验证网络、会话或授权状态；不要绕过认证或关闭安全隔离。"],
        "I": ["逐次比较调用 query 与新增证据；先人工确认冗余再做单变量效率实验。"],
        "J": ["补确定性 oracle 或人工抽查标注；在此之前不要把样本计入已确认成功率。"],
        "K": ["保留安全确认要求；通过补充只读证据或改走直接公开 URL 降低人工步骤，不能自动批准未知页面操作。"],
    }
    failure_step = (primary["evidence"][0] if primary["evidence"] else "未观察到具体失败步骤")
    return {"primary": primary, "alternatives": candidates[1:], "failure_step": failure_step,
            "engineering_improvements": suggestions.get(code, [])}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def initialize_layer_batch(directory, batch_id, layer_mode):
    """Create a resumable offline-layer batch without requiring a preparatory command."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        atomic_json(manifest_path, {"batch_id": batch_id, "created_at": now(),
            "environment": "offline_contract_layers", "layer_mode": layer_mode,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                         text=True).stdout.strip() or None})
    return manifest_path


def select_run_batch_dir(out_root, batch_id, resume):
    """Resolve a new named batch or an existing resumable batch without path traversal."""
    if batch_id is not None:
        if not batch_id or Path(batch_id).name != batch_id or any(char in batch_id for char in ("/", "\\", ":")):
            raise ValueError("batch-id must be a single directory name")
        directory = Path(out_root) / batch_id
        if resume and not directory.is_dir():
            raise FileNotFoundError("resume batch does not exist")
        if not resume and directory.exists():
            raise FileExistsError("batch already exists; use --resume")
        return directory
    if resume:
        raise ValueError("--resume requires --batch-id")
    return Path(out_root) / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])


def agent_runtime_source_sha256(root=ROOT / "apps" / "agent-runtime"):
    """Fingerprint top-level runtime source, excluding dependencies and generated fixtures."""
    root = Path(root)
    digest = hashlib.sha256()
    suffixes = {".mjs", ".js", ".json"}
    files = sorted(path for path in root.iterdir() if path.is_file()
                   and (path.suffix in suffixes or path.name == "package-lock.json")
                   and not path.name.endswith(".test.mjs"))
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def with_prior_attempt(row, previous):
    """Keep a failed/retried run's complete record instead of overwriting it."""
    history = list(previous.get("attempt_history", []))
    history.append({k: v for k, v in previous.items() if k != "attempt_history"})
    row["attempt_history"] = history
    return row


def retry_allowed(row, maximum_retries):
    """A retry count excludes the original attempt; missing legacy counts are reconstructed."""
    attempts = row.get("attempt_count")
    if not isinstance(attempts, int) or attempts < 1:
        attempts = len(row.get("attempt_history", [])) + 1
    return max(0, attempts - 1) < maximum_retries


def run_offline_layers(batch_dir, only="all"):
    """Run deterministic offline foundations and persist each layer separately."""
    layer_dir = batch_dir / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)
    api_python = ROOT / "apps" / "api" / ".venv" / "Scripts" / "python.exe"
    executable = str(api_python) if api_python.is_file() else sys.executable
    results = {}
    if only in {"all", "direct"} and not (layer_dir / "direct-tools.summary.json").exists():
        output = layer_dir / "direct-tools.json"
        proc = subprocess.run([executable, str(ROOT / "tools" / "audit_agent_eval.py"), "--output", str(output)],
                              cwd=ROOT, capture_output=True, text=True, timeout=180)
        results["tool_foundation"] = {"status": "passed" if proc.returncode == 0 else "failed",
            "mode": "mock_synthetic_direct_domain_tool_calls", "exit_code": proc.returncode,
            "stdout": safe(proc.stdout), "stderr": safe(proc.stderr),
            "cases": json.loads(output.read_text(encoding="utf-8")).get("total") if output.exists() else None,
            "note": "内存合成数据直调工具/transport；不代表联网、认证或真实 Agent 能力"}
        atomic_json(layer_dir / "direct-tools.summary.json", results["tool_foundation"])
    if only in {"all", "direct"} and not (layer_dir / "campus-auth-contract.summary.json").exists():
        test_files = ["tests/test_agent_auth_resume.py", "tests/test_campus_browser_bridge.py",
                      "tests/test_campus_credentials.py", "tests/test_campus_persistent_grants.py",
                      "tests/test_campus_webvpn_channel.py"]
        proc = subprocess.run([executable, "-m", "pytest", "-q", *test_files],
                              cwd=ROOT / "apps" / "api", capture_output=True, text=True, timeout=240)
        output_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        match = re.search(r"(\d+) passed(?:, (\d+) skipped)?", output_text)
        results["campus_auth_contract"] = {"status": "passed" if proc.returncode == 0 else "failed",
            "mode": "mock_auth_browser_grant_session_recovery_webvpn_contract", "exit_code": proc.returncode,
            "cases_passed": int(match.group(1)) if match else None,
            "cases_skipped": int(match.group(2) or 0) if match else None,
            "test_files": test_files, "stdout": safe(proc.stdout), "stderr": safe(proc.stderr),
            "note": "隔离数据库与合成 cookies/credentials，覆盖授权持久化、认证过期恢复、凭据隔离和 WebVPN 适配器；不启动真实浏览器、不访问校园网络、不证明自然语言 Agent 端到端恢复"}
        atomic_json(layer_dir / "campus-auth-contract.summary.json", results["campus_auth_contract"])
    if only in {"all", "harness"} and not (layer_dir / "harness-dispatch.summary.json").exists():
        proc = subprocess.run([executable, "-m", "pytest", "-q", "tests/test_campus_bench_harness.py"],
                              cwd=ROOT / "apps" / "api", capture_output=True, text=True, timeout=180)
        results["harness_dispatch"] = {"status": "passed" if proc.returncode == 0 else "failed",
            "mode": "mock_provider_real_api_tool_round_trip", "exit_code": proc.returncode,
            "stdout": safe(proc.stdout), "stderr": safe(proc.stderr),
            "note": "真实 FastAPI model/tool endpoints、domain executor、journal 与 tool-result feed-forward；provider 仅用固定 mock response，不含真实 Harness 子进程或外网"}
        atomic_json(layer_dir / "harness-dispatch.summary.json", results["harness_dispatch"])
    return results


def run_live_tool_probe(api, batch_dir, public_notices=False, private_courses=False,
                        academic_year=None, semester=None):
    """Probe local API/tools; optional public notice read or one redacted private course sync."""
    probes = []
    def request(name, method, path, payload=None):
        started = time.perf_counter()
        try:
            code, body = api.call(method, path, payload)
            item = {"name": name, "http_status": code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
            if code < 200 or code >= 300:
                item.update(status="failed", evidence=settings_safe(body))
            else:
                item["status"] = "passed"
                if name == "campus_status":
                    item["evidence"] = {key: safe(body.get(key)) for key in
                        ("connected", "browser_session_authorized", "login_mode", "webvpn", "service")}
                    item["workspace_state"] = ("connected_with_browser_grant" if body.get("connected") and body.get("browser_session_authorized")
                                                else "connected_without_browser_grant" if body.get("connected")
                                                else "disconnected_with_browser_grant" if body.get("browser_session_authorized")
                                                else "disconnected_without_browser_grant")
                elif name == "connections":
                    item["evidence"] = [{key: safe(row.get(key)) for key in
                        ("id", "status", "last_checked_at", "error")} for row in body.get("items", [])]
                    item["capability_status"] = ("degraded" if any(row.get("status") != "ok"
                                                                    for row in body.get("items", []))
                                                  else "ready")
                elif name == "public_notices":
                    error = body.get("error") if isinstance(body, dict) else None
                    records = body.get("records", []) if isinstance(body, dict) else []
                    completeness = body.get("completeness") if isinstance(body, dict) else None
                    item["evidence"] = {"source": body.get("source"), "platform": body.get("platform"),
                        "record_count": len(records), "fetched_at": body.get("fetched_at"),
                        "completeness": completeness, "warnings": safe(body.get("warnings", [])),
                        "error": safe(error), "sample_records": safe(records[:3])}
                    item["capability_status"] = ("blocked_authorization" if isinstance(error, dict) and
                        str(error.get("code", "")).startswith(("AUTH", "SESSION")) else
                        "degraded" if error else "ready" if completeness == "complete" else "partial")
                elif name == "private_courses":
                    error = body.get("error") if isinstance(body, dict) else None
                    records = body.get("records", []) if isinstance(body, dict) else []
                    freshness = body.get("freshness")
                    item["capability_status"] = ("ready" if not error and freshness == "fresh" and body.get("completeness") == "complete" else
                                                  "partial" if not error and freshness == "fresh" else
                                                  "blocked_authorization" if isinstance(error, dict) and
                                                  str(error.get("code", "")).startswith(("AUTH", "SESSION")) else
                                                  "degraded")
                    item["evidence"] = {"capability": "courses", "platform": "zdbk", "term": course_query,
                        "record_count": len(records), "fetched_at": body.get("fetched_at"),
                        "freshness": freshness, "completeness": body.get("completeness"),
                        "error_code": error.get("code") if isinstance(error, dict) else None,
                        "error_retryable": error.get("retryable") if isinstance(error, dict) else None,
                        "course_content_in_probe_artifact": False}
                else:
                    item["evidence"] = "HTTP response received"
            probes.append(item)
        except Exception as exc:
            probes.append({"name": name, "status": "failed", "duration_ms": round((time.perf_counter()-started)*1000,2),
                           "error": {"type": type(exc).__name__, "message": safe(str(exc))}})
    request("health", "GET", "/api/v1/health")
    request("campus_status", "GET", "/api/v1/agent/campus/status")
    request("connections", "GET", "/api/v1/connections")
    if public_notices:
        request("public_notices", "POST", "/api/v1/agent/campus/sync",
                {"capability": "notices", "query": {"limit": 6}, "platform": "zdbk_public"})
    if private_courses:
        # Explicit opt-in only. CampusWorkspace bounds automatic re-login to one attempt;
        # only counts/status leave this process, never course records or account data.
        if academic_year is None or semester is None:
            from connectors.campus.academic_defaults import fallback_terms
            current = next((term for term in fallback_terms({}, dt.datetime.now().date()) if term.get("current")), None)
            if not current:
                raise ValueError("无法从本地学期规则确定当前学期；请显式提供 --academic-year 和 --semester")
            academic_year = academic_year or current["year"]
            semester = semester or current["semester"]
        course_query = {"year": str(academic_year), "semester": str(semester)}
        request("private_courses", "POST", "/api/v1/agent/campus/sync",
                {"capability": "courses", "query": course_query, "platform": "zdbk"})
    transport_status = "passed" if probes and all(x["status"] == "passed" for x in probes) else "failed"
    capability_states = [x.get("capability_status") for x in probes
                         if x.get("name") in {"public_notices", "private_courses"} and x.get("capability_status")]
    capability_status = ("not_assessed" if not capability_states else
                         "ready" if all(state == "ready" for state in capability_states) else
                         "partially_ready" if any(state in {"ready", "partial"} for state in capability_states) else
                         "blocked_authorization" if "blocked_authorization" in capability_states else "degraded")
    result = {"status": "completed" if transport_status == "passed" else "failed",
              "transport_status": transport_status, "capability_status": capability_status,
              "mode": "real_local_api_and_registered_tools", "created_at": now(), "probes": probes,
              "note": "真实本机 API/连接器探测；可选的一次公开通知读取或一次私有课程只读刷新；私有结果只记录状态与数量，不等同于模型端到端评测"}
    layer_dir = batch_dir / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(layer_dir / "live-tool-probe.json", result)
    return result


def build_report(batch_dir):
    rows = []
    try:
        manifest = json.loads((Path(batch_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    resumptions = manifest.get("resumptions") if isinstance(manifest.get("resumptions"), list) else []
    latest_preflight = (resumptions[-1].get("api_preflight") if resumptions and isinstance(resumptions[-1], dict)
                        else manifest.get("api_preflight"))
    current_catalog = load_catalog()
    current_scenarios = {item["id"]: item for item in current_catalog["scenarios"]}
    current_catalog_hash = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    for path in sorted((batch_dir / "runs").glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            row["trace"] = [privacy_event(e) for e in row.get("trace", [])]
            if isinstance(row.get("final_output"), str):
                row["final_output"] = safe(row["final_output"])
            history = row.get("attempt_history", [])
            row["attempt_count"] = len(history) + 1
            row["attempt_history_summary"] = [{"attempt": i + 1, "status": item.get("status"),
                "success": (item.get("evaluation") or {}).get("success"),
                "duration_ms": (item.get("timings") or {}).get("end_to_end_ms"),
                "model_calls": (item.get("metrics") or {}).get("model_calls"),
                "tool_calls": (item.get("metrics") or {}).get("tool_calls"),
                "budget_stop": item.get("budget_stop"), "error": item.get("error"),
                "trace_available": bool(item.get("trace")),
                "trace_note": item.get("trace_unavailable_reason")}
                for i, item in enumerate(history)]
            events = row.get("trace", [])
            row["tool_availability_checks"] = tool_availability_checks(
                row.get("scenario", {}), events)
            row["current_tool_schema_comparison"] = compare_model_schemas_to_local(row.get("trace", []))
            if row.get("environment") == "real_api" and events:
                observed_user_actions = pending_user_actions(events)
                if observed_user_actions:
                    row["status"] = "needs_user"
                turn = {"status": row.get("status"), "response": row.get("final_output", ""), "error": row.get("error")}
                row["metrics"] = metrics(events)
                row["metrics"]["user_action_requests"] = observed_user_actions
                row["metrics"]["human_intervention_events"] = (row["metrics"].get("human_confirmations", 0)
                                                                    + observed_user_actions)
                if observed_user_actions:
                    row.setdefault("layers", {}).setdefault("user_experience", {}).update(
                        status="action_required", action_requests=observed_user_actions)
                scenario = row.get("scenario", {})
                current = current_scenarios.get(row.get("scenario_id"))
                same_prompts = bool(current) and scenario.get("input") == current.get("input") and (scenario.get("messages") or [scenario.get("input")]) == (current.get("messages") or [current.get("input")])
                eval_scenario = current if same_prompts else scenario
                if same_prompts:
                    row.setdefault("evaluation_as_run", row.get("evaluation"))
                    row["evaluation_provenance"] = {"mode": "current_catalog_reevaluation",
                        "catalog_sha256": current_catalog_hash,
                        "prompt_sha256": hashlib.sha256(json.dumps(current.get("messages") or [current.get("input")], ensure_ascii=False).encode("utf-8")).hexdigest(),
                        "rubric_sha256": hashlib.sha256(json.dumps(current.get("eval"), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                        "condition": "scenario input/messages unchanged; current scoring rubric applied to saved trace"}
                else:
                    row["evaluation_provenance"] = {"mode": "as_run_scenario_snapshot",
                        "condition": "current scenario prompt sequence differs; current rubric not applied"}
                if not row.get("final_output_redacted"):
                    row["evaluation"] = evaluate(eval_scenario, turn, events, row.get("budget_stop"))
                    if (row["evaluation"].get("source_grounding") or {}).get("status") == "aligned":
                        row["evaluation"]["hallucination"] = "citation URLs, labels and dates aligned to observed tool results; topical relevance and source completeness still need human review"
                    else:
                        row["evaluation"]["hallucination"] = "not_automatically_verifiable; human source audit required for factual claims"
                else:
                    row["evaluation"] = {"success": "不可验证", "correctness": "不可验证",
                        "reason": "最终回答因账户数据脱敏，无法由报告重评；不能把历史分数当作已确认结果",
                        "tool_quality": "需人工抽查", "efficiency": "仍可按轨迹统计耗时",
                        "autonomy": "需人工抽查", "failure_category": "J_账户数据已脱敏，当前无法核验结果",
                        "observed_tools": event_tools(events),
                        "tool_selection": tool_selection_evidence((eval_scenario.get("eval") or {}), events),
                        "hallucination": "not_automatically_verifiable; human source audit required for factual claims"}
                    # Private assignment answers are scored in-memory before redaction.
                    # Preserve only its aggregate oracle result when rebuilding reports.
                    prior_oracle = (row.get("evaluation_as_run") or {}).get("assignment_date_evidence") or {}
                    apply_redacted_assignment_oracle(row["evaluation"], prior_oracle)
                    course_oracle = (row.get("evaluation_as_run") or {}).get("course_directory_evidence") or {}
                    apply_redacted_course_directory_oracle(row["evaluation"], course_oracle)
                    source_oracle = (row.get("evaluation_as_run") or {}).get("public_source_oracle") or {}
                    apply_redacted_public_source_oracle(row["evaluation"], source_oracle)
                    prior_selection = (row.get("evaluation_as_run") or {}).get("tool_selection") or {}
                    apply_redacted_forbidden_scope_evidence(row["evaluation"],
                        prior_selection.get("forbidden_argument_calls") or [])
                tool_quality, efficiency, autonomy = execution_dimensions(
                    eval_scenario, events, row.get("metrics", {}), row.get("timings", {}),
                    len((eval_scenario.get("messages") or [eval_scenario.get("input")]) if eval_scenario else []))
                row["evaluation"].update(tool_quality=tool_quality, efficiency=efficiency, autonomy=autonomy)
                if row.get("budget_stop"):
                    row["evaluation"]["failure_category"] = "I_触发基准调用预算"
                row["diagnosis"] = diagnose(eval_scenario, turn, events, row["evaluation"], row.get("budget_stop"))
                row["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                atomic_json(path, row)
            rows.append(row)
        except (OSError, ValueError):
            continue
    values = [x for x in rows if x.get("environment") == "real_api"]
    count = len(values)
    succeeded = sum(1 for x in values if (x.get("evaluation") or {}).get("success") == "成功")
    partial = sum(1 for x in values if (x.get("evaluation") or {}).get("success") == "部分成功")
    durations = sorted(x.get("timings", {}).get("end_to_end_ms", 0) for x in values if x.get("timings"))
    first_journal = sorted(x["timings"]["first_journal_event_ms"] for x in values
                           if isinstance(x.get("timings", {}).get("first_journal_event_ms"), (int, float)))
    first_runtime = sorted(x["timings"]["first_runtime_event_ms"] for x in values
                           if isinstance(x.get("timings", {}).get("first_runtime_event_ms"), (int, float)))
    def percentile(q):
        if not durations: return None
        return durations[min(len(durations)-1, max(0, math.ceil(len(durations)*q)-1))]
    def percentile_of(values_, q):
        if not values_: return None
        return values_[min(len(values_)-1, max(0, math.ceil(len(values_)*q)-1))]
    model_calls = [x.get("metrics", {}).get("model_calls", 0) for x in values]
    input_tokens = [x.get("metrics", {}).get("tokens_input_observed", 0) for x in values]
    output_tokens = [x.get("metrics", {}).get("tokens_output_observed", 0) for x in values]
    tool_times = [x.get("metrics", {}).get("tool_time_estimated_ms") for x in values if x.get("metrics", {}).get("tool_time_estimated_ms") is not None]
    model_times = [x.get("metrics", {}).get("model_time_estimated_ms") for x in values if x.get("metrics", {}).get("model_time_estimated_ms") is not None]
    availability_rows = [x.get("tool_availability_checks", {}) for x in values]
    catalog_scenarios = load_catalog()["scenarios"]
    catalog_count = len(catalog_scenarios)
    summary = {"runs": count, "attempts": sum(x.get("attempt_count", 1) for x in values), "success": succeeded, "partial": partial,
               "scenario_count_in_catalog": catalog_count,
               "runnable_scenarios_in_catalog": sum(1 for x in catalog_scenarios
                                                     if x["run_status"] == "runnable" and "real" in x["modes"]),
               "mock_runnable_scenarios_in_catalog": sum(1 for x in catalog_scenarios
                                                          if x["run_status"] == "runnable" and "mock" in x["modes"]),
               "blocked_scenarios": [{"id": x["id"], "reason": x["run_block_reason"]} for x in catalog_scenarios if x["run_status"] != "runnable"],
               "failed": sum(1 for x in values if (x.get("evaluation") or {}).get("success") == "失败"),
               "unverifiable": sum(1 for x in values if (x.get("evaluation") or {}).get("success") == "不可验证"),
               "runs_with_unavailable_required_tools": sum(1 for x in availability_rows if x.get("unavailable_count", 0)),
               "unavailable_required_tool_count": sum(x.get("unavailable_count", 0) for x in availability_rows),
               "runs_with_unobserved_tool_availability": sum(1 for x in availability_rows if x.get("not_observed_count", 0)),
               "mean_ms": round(sum(durations)/len(durations), 2) if durations else None,
               "p50_ms": percentile(.5), "p95_ms": percentile(.95),
               "first_journal_event_ms_mean": round(sum(first_journal)/len(first_journal), 2) if first_journal else None,
               "first_journal_event_ms_p50": percentile_of(first_journal, .5),
               "first_journal_event_ms_p95": percentile_of(first_journal, .95),
               "first_runtime_event_ms_mean": round(sum(first_runtime)/len(first_runtime), 2) if first_runtime else None,
               "first_runtime_event_ms_p50": percentile_of(first_runtime, .5),
               "first_runtime_event_ms_p95": percentile_of(first_runtime, .95),
               "mean_tool_calls": round(sum(x.get("metrics",{}).get("tool_calls",0) for x in values)/count,2) if count else None,
               "mean_model_calls": round(sum(model_calls)/count,2) if count else None,
               "mean_tool_time_ms_observed": round(sum(tool_times)/len(tool_times),2) if tool_times else None,
               "mean_model_time_ms_observed": round(sum(model_times)/len(model_times),2) if model_times else None,
               "mean_input_tokens": round(sum(input_tokens)/count,2) if count else None,
               "mean_output_tokens": round(sum(output_tokens)/count,2) if count else None,
               "total_web_blocked_provider_results": sum(x.get("metrics", {}).get("web_blocked_provider_results", 0) for x in values),
               "mean_web_blocked_provider_results": round(sum(x.get("metrics", {}).get("web_blocked_provider_results", 0) for x in values)/count, 2) if count else None,
               "total_web_blocked_repeat_shortcuts": sum(x.get("metrics", {}).get("web_blocked_repeat_shortcuts", 0) for x in values),
               "runs_with_web_blocked_repeat_shortcuts": sum(x.get("metrics", {}).get("web_blocked_repeat_shortcuts", 0) > 0 for x in values),
               "mean_estimated_cost": None,
               **confirmation_summary(values)}
    categories = {}
    for row in values:
        category = row.get("scenario", {}).get("category", "未知")
        group = categories.setdefault(category, {"runs": 0, "success": 0, "partial": 0, "failed": 0})
        group["runs"] += 1
        result = (row.get("evaluation") or {}).get("success")
        if result == "成功": group["success"] += 1
        elif result == "部分成功": group["partial"] += 1
        elif result == "失败": group["failed"] += 1
    layer_file = batch_dir / "layers" / "direct-tools.summary.json"
    auth_contract_file = batch_dir / "layers" / "campus-auth-contract.summary.json"
    harness_file = batch_dir / "layers" / "harness-dispatch.summary.json"
    direct_layer = json.loads(layer_file.read_text(encoding="utf-8")) if layer_file.exists() else {"status":"not_run"}
    auth_contract_layer = json.loads(auth_contract_file.read_text(encoding="utf-8")) if auth_contract_file.exists() else {"status":"not_run"}
    harness_layer = json.loads(harness_file.read_text(encoding="utf-8")) if harness_file.exists() else {"status":"not_run"}
    mock_e2e_file = batch_dir / "layers" / "mock-agent-e2e.json"
    mock_agent_e2e = json.loads(mock_e2e_file.read_text(encoding="utf-8")) if mock_e2e_file.exists() else {"status":"not_run"}
    live_probe_file = batch_dir / "layers" / "live-tool-probe.json"
    live_probe = json.loads(live_probe_file.read_text(encoding="utf-8")) if live_probe_file.exists() else {"status":"not_run"}
    atomic_json(batch_dir / "summary.json", {"batch_id": batch_dir.name, "generated_at": now(), "summary": summary, "categories": categories,
                                               "layers": {"tool_foundation": direct_layer,
                                                          "api_preflight": latest_preflight or {"status": "not_recorded"},
                                                          "campus_auth_contract": auth_contract_layer,
                                                          "live_tool_probe": live_probe,
                                                          "harness_dispatch": harness_layer,
                                                          "mock_agent_e2e": mock_agent_e2e,
                                                          "agent_task": {"status": "per_run", "method": "trace-aware rubric; open factual claims need independent verification"},
                                                          "user_experience": {"status": "needs_human_review", "note": "automated event counts do not prove clicks, repeated login, or perceived wait"}}, "runs": rows})
    html = """<!doctype html><meta charset=utf-8><title>Campus Agent Benchmark</title><style>body{font:15px system-ui;margin:2rem auto;max-width:1100px;color:#172033}header{background:#eff6ff;padding:1.2rem;border-radius:12px}.cards{display:flex;flex-wrap:wrap;gap:12px}.card{padding:14px;background:#f4f5f7;border-radius:10px;min-width:130px}table{border-collapse:collapse;width:100%;margin-top:20px}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}details{max-width:560px}pre{white-space:pre-wrap;max-height:420px;overflow:auto;background:#f7f7f7;padding:10px}</style><main><h1>Campus Agent Benchmark</h1><div id=app>Loading…</div></main><script>const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));fetch('summary.json').then(r=>r.json()).then(d=>{const s=d.summary;const cards=[['Catalog scenarios',s.scenario_count_in_catalog],['Runnable scenarios',s.runnable_scenarios_in_catalog],['Runs',s.runs],['Attempts',s.attempts],['Success',s.success],['Partial',s.partial],['Failed',s.failed],['Unverifiable',s.unverifiable],['Runs with missing required tools',s.runs_with_unavailable_required_tools],['Missing required tool observations',s.unavailable_required_tool_count],['Mean ms',s.mean_ms],['P50 ms',s.p50_ms],['P95 ms',s.p95_ms],['Mean tools',s.mean_tool_calls],['Mean interventions',s.mean_human_interventions]];const layers=d.layers;document.querySelector('#app').innerHTML='<header><b>Batch '+esc(d.batch_id)+'</b><p>Direct tools: '+esc(layers.tool_foundation.status)+' (synthetic). Campus auth/session/WebVPN contracts: '+esc(layers.campus_auth_contract.status)+' ('+esc(layers.campus_auth_contract.cases_passed||0)+' mock tests). Live probe transport: '+esc((layers.live_tool_probe||{}).transport_status||layers.live_tool_probe.status)+', capability: '+esc((layers.live_tool_probe||{}).capability_status||'not assessed')+'. Harness bridge: '+esc(layers.harness_dispatch.status)+' (mock-provider contract). Harness runtime E2E: '+esc(layers.mock_agent_e2e.status)+' (scripted model, real local Harness/tool, synthetic data only; campus services not contacted). Agent: '+esc(layers.agent_task.status)+'; UX: '+esc(layers.user_experience.status)+'. Dynamic facts require independent review.</p><section class=cards>'+cards.map(x=>'<div class=card><b>'+esc(x[0])+'</b><br>'+esc(x[1])+'</div>').join('')+'</section><details><summary>Harness runtime E2E evidence</summary><pre>'+esc(JSON.stringify(layers.mock_agent_e2e,null,2))+'</pre></details></header><table><thead><tr><th>Scenario</th><th>Category</th><th>Result</th><th>Duration</th><th>Tools</th><th>Details</th></tr></thead><tbody>'+d.runs.map(x=>'<tr><td>'+esc(x.scenario_id)+'</td><td>'+esc((x.scenario||{}).category||'')+'</td><td>'+esc((x.evaluation||{}).success||x.status)+'</td><td>'+esc((x.timings||{}).end_to_end_ms||'—')+'</td><td>'+esc((x.metrics||{}).tool_calls||0)+'</td><td><details><summary>Trace and output</summary><b>Tool availability on every model call</b><pre>'+esc(JSON.stringify(x.tool_availability_checks||{},null,2))+'</pre><b>User turns</b><pre>'+esc(((x.scenario||{}).messages||[(x.scenario||{}).input||'']).join('\n→\n'))+'</pre><b>Goal and success criteria</b><pre>'+esc(JSON.stringify({goal:(x.scenario||{}).goal,success_conditions:(x.scenario||{}).success_conditions,required_tools:(x.scenario||{}).required_tools,allowed_alternatives:(x.scenario||{}).allowed_alternatives,intervention:(x.scenario||{}).intervention,capability_state:(x.scenario||{}).capability_state,environment:(x.scenario||{}).environment},null,2))+'</pre><b>Evaluation</b><pre>'+esc(JSON.stringify(x.evaluation||{},null,2))+'</pre><b>Evaluation provenance</b><pre>'+esc(JSON.stringify(x.evaluation_provenance||{},null,2))+'</pre><b>Output</b><pre>'+esc(x.final_output||'')+'</pre><b>Diagnosis</b><pre>'+esc(JSON.stringify(x.diagnosis||{},null,2))+'</pre><b>Attempt history</b><pre>'+esc(JSON.stringify(x.attempt_history_summary||[],null,2))+'</pre><b>Trace</b><pre>'+esc(JSON.stringify(x.trace||[],null,2))+'</pre></details></td></tr>').join('')+'</tbody></table>'})</script>"""
    html = html.replace("['Mean interventions',s.mean_human_interventions]",
                        "['Mean journal confirmations',s.mean_recorded_confirmation_events],['Mean user action requests',s.mean_user_action_requests],['Mean observed interventions',s.mean_human_interventions],['WEB_BLOCKED provider results',s.total_web_blocked_provider_results],['Same-host short-circuits',s.total_web_blocked_repeat_shortcuts],['Runs with short-circuit',s.runs_with_web_blocked_repeat_shortcuts]")
    html = html.replace("['P95 ms',s.p95_ms]", "['P95 ms',s.p95_ms],['First journal event mean ms',s.first_journal_event_ms_mean],['First runtime event mean ms',s.first_runtime_event_ms_mean]")
    html = html.replace("Direct tools: ", "API health: '+esc((layers.api_preflight||{}).status||'not recorded')+'. Loaded API Agent source: '+esc((layers.api_preflight||{}).agent_source_status||'not reported')+'. Direct tools: ")
    html = html.replace("<b>Tool availability on every model call</b>", "<b>Tool availability on every model call</b><pre>'+esc(JSON.stringify(x.tool_availability_checks||{},null,2))+'</pre><b>Current checkout vs observed tool schemas</b><pre>'+esc(JSON.stringify(x.current_tool_schema_comparison||{},null,2))+'</pre><b>Tool availability on every model call</b>")
    html = html.replace("Dynamic facts require independent review.</p>",
                        "Dynamic facts require independent review. '+esc(s.confirmation_observation_note)+'</p>")
    (batch_dir / "index.html").write_text(html, encoding="utf-8")
    return summary


def mock_e2e_layer_summary(batch_dir: Path) -> dict | None:
    """Aggregate the separate scripted-model E2E layer without mixing it into real runs."""
    path = batch_dir / "layers" / "mock-agent-e2e.json"
    try:
        layer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repetitions = layer.get("repetitions") if isinstance(layer, dict) else None
    if not isinstance(repetitions, list) or not repetitions:
        return None
    valid = [row for row in repetitions if isinstance(row, dict)]
    evidence = [row.get("evidence") for row in valid if isinstance(row.get("evidence"), dict)]

    def stats(values):
        numbers = sorted(value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool))
        return {"mean": round(sum(numbers) / len(numbers), 2) if numbers else None,
                "range": [round(numbers[0], 2), round(numbers[-1], 2)] if numbers else None}

    return {
        "scenario_id": layer.get("scenario_id"),
        "mode": layer.get("mode"),
        "repeat_count": len(valid),
        "passed_runs": sum(row.get("status") == "passed" and row.get("verified_success") is True for row in valid),
        "failed_runs": sum(row.get("status") == "failed" for row in valid),
        "elapsed_ms": stats([row.get("elapsed_ms") for row in valid]),
        "model_calls": stats([item.get("model_call_count") for item in evidence]),
        "domain_tool_calls": stats([item.get("tool_result_count") for item in evidence]),
        "initial_visible_tool_schemas": stats([
            (item.get("model_steps") or [{}])[0].get("available_tool_count") for item in evidence
            if item.get("model_steps")]),
        "review_schema_hidden_runs": sum(item.get("review_tool_schema_hidden_initially") is True for item in evidence),
        "tool_result_returned_runs": sum(item.get("provider_received_tool_result_before_final") is True for item in evidence),
        "harness_call_sequences": [item.get("harness_tool_calls", []) for item in evidence],
    }


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run controlled Agent tasks through the real local API")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--api")
    run.add_argument("--allow-unmatched-api-source", action="store_true",
                     help="仅用于诊断未报告/不匹配源码指纹的运行服务；结果不可归因到当前工作树")
    run.add_argument("--provider-id", required=False)
    run.add_argument("--repeat", type=int)
    run.add_argument("--concurrency", type=int)
    run.add_argument("--timeout", type=int)
    run.add_argument("--max-model-calls-per-run", type=int)
    run.add_argument("--max-total-model-calls", type=int)
    run.add_argument("--seed", type=int)
    run.add_argument("--scenario", action="append")
    run.add_argument("--defer-tool-group", choices=("campus_reviews",), action="append", default=None,
                     help="Enable a gated benchmark-only tool exposure treatment for each conversation")
    run.add_argument("--batch-id")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--max-retries", type=int, help="Maximum reruns of one failed task within its batch (initial attempt is not counted)")
    run.add_argument("--limit", type=int, help="Cap number of scenario/repeat tasks for a low-cost smoke run")
    run.add_argument("--max-cost-usd", type=float, default=0.0, help="Operator acknowledgement only; no billing cap without trusted provider pricing")
    run.add_argument("--prewarm-lead-seconds", type=float,
                     help="Wait after creating each conversation before its first turn, to measure the existing async runner prewarm")
    run.add_argument("--pause-poll-seconds", type=float, default=1.0)
    run.add_argument("--skip-direct-tools", action="store_true", help="Skip offline synthetic tool and campus auth/session mock suites; Harness contract still runs")
    layers = sub.add_parser("layers", help="Run offline tool, campus auth/session and Harness contract layers")
    layers.add_argument("--batch-id", required=True)
    layers.add_argument("--only", choices=("all", "direct", "harness"), default="all")
    mock_e2e = sub.add_parser("mock-e2e", help="Run the isolated scripted-model E2E through the normal Agent API and real Harness")
    mock_e2e.add_argument("--batch-id", required=True)
    mock_e2e.add_argument("--timeout", type=int, default=120)
    mock_e2e.add_argument("--scenario", choices=("course-list-smoke", "JOB-01", "BUS-03", "BUS-12", "MULTI-02", "ERR-02", "ERR-09", "ERR-10", "ERR-11", "TOOL-REV-01"), default="course-list-smoke")
    mock_e2e.add_argument("--defer-tool-group", choices=("campus_reviews",),
                              help="Run the campus_reviews exposure treatment for TOOL-REV-01 or MULTI-02")
    mock_e2e.add_argument("--repeat", type=int, default=3)
    probe = sub.add_parser("probe", help="Probe live local tool/API status without a model call")
    probe.add_argument("--api", default="http://127.0.0.1:8000")
    probe.add_argument("--batch-id")
    probe.add_argument("--public-notices", action="store_true", help="Make at most one credential-free read from the registered public campus notices source")
    probe.add_argument("--private-courses", action="store_true", help="Explicitly make one read-only zdbk course sync; allows the app's bounded single auto-relogin, stores only counts/status in the probe artifact")
    probe.add_argument("--academic-year", help="Optional ZDBK course sync year, e.g. 2026 (defaults to the app's current-term heuristic when --private-courses is used)")
    probe.add_argument("--semester", help="Optional exact ZDBK semester code, e.g. '1|秋'; must be paired with --academic-year")
    show = sub.add_parser("list")
    show.add_argument("--json", action="store_true")
    for name in ("pause", "resume"):
        ctl = sub.add_parser(name)
        ctl.add_argument("--batch-id", required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--target-tool", action="append", default=[],
                         help="Expected changed tool in a one-factor schema experiment; repeat per tool")
    report = sub.add_parser("report")
    report.add_argument("--batch-id", required=True)
    args = parser.parse_args()
    catalog = load_catalog()
    if args.command == "list":
        print(json.dumps(catalog if args.json else [{k:s.get(k) for k in ("id","category","complexity","capability_state","capability","run_status","run_block_reason","modes","mock_status")} for s in catalog["scenarios"]], ensure_ascii=False, indent=2))
        return 0
    if args.command in {"pause", "resume"}:
        directory = OUT_ROOT / args.batch_id
        if not directory.is_dir():
            parser.error("batch 不存在")
        flag = directory / "PAUSE"
        if args.command == "pause": flag.write_text("paused", encoding="utf-8")
        else: flag.unlink(missing_ok=True)
        print(f"{args.command}: {args.batch_id}")
        return 0
    if args.command == "report":
        directory = OUT_ROOT / args.batch_id
        if not directory.is_dir(): parser.error("batch 不存在")
        print(json.dumps(build_report(directory), ensure_ascii=False))
        return 0
    if args.command == "layers":
        directory = OUT_ROOT / args.batch_id
        initialize_layer_batch(directory, args.batch_id, args.only)
        results = run_offline_layers(directory, args.only)
        build_report(directory)
        print(json.dumps({"batch_id": args.batch_id, "layers": results}, ensure_ascii=False, indent=2))
        return 0 if all(item.get("status") == "passed" for item in results.values()) else 1
    if args.command == "mock-e2e":
        if not 10 <= args.timeout <= 300 or not 1 <= args.repeat <= 10:
            parser.error("--timeout must be 10..300 seconds and --repeat 1..10")
        if args.defer_tool_group and args.scenario not in {"TOOL-REV-01", "MULTI-02"}:
            parser.error("--defer-tool-group is supported only for TOOL-REV-01 and MULTI-02")
        if args.scenario != "course-list-smoke":
            fixture_scenario = next((s for s in catalog["scenarios"] if s["id"] == args.scenario), None)
            if not fixture_scenario or "mock" not in fixture_scenario.get("modes", []) or not fixture_scenario.get("mock_fixture"):
                parser.error("所选场景没有已实现的 mock fixture")
        directory = OUT_ROOT / args.batch_id
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "manifest.json", {"batch_id": args.batch_id, "created_at": now(),
            "environment": "isolated_scripted_model_real_api_harness_synthetic_data",
            "scenario_ids": [args.scenario] if args.scenario != "course-list-smoke" else [],
            "repeat": args.repeat, "timeout_seconds": args.timeout,
            "benchmark_tool_groups": [args.defer_tool_group] if args.defer_tool_group else [],
            "api_gate_required": bool(args.defer_tool_group),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                         text=True).stdout.strip() or None})
        layer_dir = directory / "layers"
        layer_dir.mkdir(parents=True, exist_ok=True)
        repetitions = []
        for repetition in range(1, args.repeat + 1):
            try:
                proc = subprocess.run([sys.executable, str(ROOT / "tools" / "campus_bench" / "mock_e2e.py"),
                                       "--timeout", str(args.timeout), "--scenario", args.scenario]
                                      + (["--defer-tool-group", args.defer_tool_group] if args.defer_tool_group else []),
                                      cwd=ROOT, capture_output=True,
                                      text=True, encoding="utf-8", timeout=args.timeout + 180, check=False)
                item = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {
                    "status": "failed", "verified_success": False, "failure_code": "empty_runner_output"}
                if proc.returncode and item.get("status") == "passed":
                    item.update(status="failed", verified_success=False, failure_code="runner_exit_code_mismatch")
            except subprocess.TimeoutExpired:
                item = {"status": "failed", "verified_success": False, "failure_code": "mock_e2e_process_timeout"}
            except (OSError, ValueError, IndexError) as exc:
                item = {"status": "failed", "verified_success": False, "failure_code": type(exc).__name__}
            item["repetition"] = repetition
            repetitions.append(item)
        evidence = {"status": "passed" if all(x.get("verified_success") for x in repetitions) else "failed",
                    "verified_success": bool(repetitions) and all(x.get("verified_success") for x in repetitions),
                    "repeat": args.repeat, "repetitions": repetitions}
        evidence["mode"] = "mock_scenario_e2e_not_real_model_or_campus_service"
        evidence["scenario_id"] = args.scenario if args.scenario != "course-list-smoke" else None
        evidence["benchmark_tool_groups"] = [args.defer_tool_group] if args.defer_tool_group else []
        atomic_json(layer_dir / "mock-agent-e2e.json", evidence)
        summary = build_report(directory)
        print(json.dumps({"batch_id": args.batch_id, "mock_agent_e2e": evidence,
                          "summary": summary, "report": str(directory / "index.html")},
                         ensure_ascii=False, indent=2))
        return 0 if evidence.get("verified_success") else 1
    if args.command == "probe":
        if bool(args.academic_year) != bool(args.semester):
            parser.error("--academic-year 与 --semester 必须同时提供")
        if (args.academic_year or args.semester) and not args.private_courses:
            parser.error("--academic-year/--semester 仅能与 --private-courses 一起使用")
        try:
            loopback_only(args.api)
        except ValueError as exc:
            parser.error(str(exc))
        batch_id = args.batch_id or ("live-probe-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        directory = OUT_ROOT / batch_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "runs").mkdir(exist_ok=True)
        result = run_live_tool_probe(Api(args.api, timeout=60), directory, args.public_notices,
                                     args.private_courses, args.academic_year, args.semester)
        report = build_report(directory)
        print(json.dumps({"batch_id": batch_id, "probe": result, "summary": report}, ensure_ascii=False, indent=2))
        return 0 if result["transport_status"] == "passed" else 1
    if args.command == "compare":
        before, after = (OUT_ROOT / args.before / "summary.json", OUT_ROOT / args.after / "summary.json")
        if not before.is_file() or not after.is_file():
            parser.error("两个 batch 都需要存在 summary.json")
        left, right = json.loads(before.read_text(encoding="utf-8")), json.loads(after.read_text(encoding="utf-8"))
        def read_manifest(batch_id):
            path = OUT_ROOT / batch_id / "manifest.json"
            try: return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError): return {}
        before_manifest, after_manifest = read_manifest(args.before), read_manifest(args.after)
        mock_before = mock_e2e_layer_summary(OUT_ROOT / args.before)
        mock_after = mock_e2e_layer_summary(OUT_ROOT / args.after)
        if mock_before and mock_after and mock_before.get("scenario_id") == mock_after.get("scenario_id"):
            def delta(after_value, before_value):
                return round(after_value - before_value, 2) if isinstance(after_value, (int, float)) and isinstance(before_value, (int, float)) else None
            mock_comparison = {
                "comparison_type": "scripted_mock_harness",
                "before": args.before, "after": args.after,
                "scenario_id": mock_before["scenario_id"],
                "before_tool_groups": before_manifest.get("benchmark_tool_groups", []),
                "after_tool_groups": after_manifest.get("benchmark_tool_groups", []),
                "comparability": {
                    "same_scenario": True,
                    "same_repeat": before_manifest.get("repeat") == after_manifest.get("repeat"),
                    "same_timeout": before_manifest.get("timeout_seconds") == after_manifest.get("timeout_seconds"),
                    "same_runner_hash": before_manifest.get("runner_sha256") == after_manifest.get("runner_sha256"),
                    "same_environment": before_manifest.get("environment") == after_manifest.get("environment"),
                    "model": "scripted mock; no real-model inference",
                    "data": "synthetic fixture; no live campus service",
                },
                "before_summary": mock_before,
                "after_summary": mock_after,
                "deltas_after_minus_before": {
                    "passed_runs": delta(mock_after["passed_runs"], mock_before["passed_runs"]),
                    "mean_elapsed_ms": delta(mock_after["elapsed_ms"]["mean"], mock_before["elapsed_ms"]["mean"]),
                    "mean_model_calls": delta(mock_after["model_calls"]["mean"], mock_before["model_calls"]["mean"]),
                    "mean_domain_tool_calls": delta(mock_after["domain_tool_calls"]["mean"], mock_before["domain_tool_calls"]["mean"]),
                    "mean_initial_visible_tool_schemas": delta(mock_after["initial_visible_tool_schemas"]["mean"], mock_before["initial_visible_tool_schemas"]["mean"]),
                },
                "interpretation": "Deterministic fixture/Harness behavior only. Do not count these pass rates as real Agent success or infer real-model quality, cost, or campus reliability.",
            }
            after_dir = OUT_ROOT / args.after
            after_dir.mkdir(parents=True, exist_ok=True)
            (after_dir / "comparison.json").write_text(json.dumps(mock_comparison, ensure_ascii=False, indent=2), encoding="utf-8")
            rows = []
            for label, key in (("Mock oracle passes", "passed_runs"), ("Mean elapsed ms", "elapsed_ms"),
                               ("Mean model calls", "model_calls"), ("Mean domain tool calls", "domain_tool_calls"),
                               ("Mean initial visible schemas", "initial_visible_tool_schemas")):
                left_value = mock_before[key] if key == "passed_runs" else mock_before[key]["mean"]
                right_value = mock_after[key] if key == "passed_runs" else mock_after[key]["mean"]
                rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in
                    (label, left_value, right_value, delta(right_value, left_value))) + "</tr>")
            page = ("<!doctype html><meta charset=utf-8><title>Mock Harness batch comparison</title>"
                    "<style>body{font:15px system-ui;margin:2rem auto;max-width:1000px;color:#172033}header{background:#fff7ed;padding:1rem;border-radius:10px}table{border-collapse:collapse;width:100%;margin-top:20px}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}</style>"
                    "<h1>Scripted Mock Harness Comparison</h1><header>Before: " + html.escape(args.before) +
                    "<br>After: " + html.escape(args.after) + "<p>" + html.escape(mock_comparison["interpretation"]) +
                    "</p><pre>" + html.escape(json.dumps(mock_comparison["comparability"], ensure_ascii=False, indent=2)) +
                    "</pre></header><table><thead><tr><th>Metric</th><th>Before</th><th>After</th><th>Δ</th></tr></thead><tbody>" +
                    "".join(rows) + "</tbody></table>")
            (after_dir / "comparison.html").write_text(page, encoding="utf-8")
            print(json.dumps({"comparison_json": str(after_dir / "comparison.json"),
                              "comparison_html": str(after_dir / "comparison.html"), **mock_comparison}, ensure_ascii=False, indent=2))
            return 0
        paired_keys = ("api", "provider_id", "repeat", "concurrency", "timeout_seconds",
                       "max_model_calls_per_run", "max_total_model_calls", "max_retries", "seed",
                       "scenario_ids", "catalog_sha256", "runner_sha256", "git_head",
                       "api_agent_source_sha256_loaded")
        api_source_verified = bool(before_manifest.get("api_agent_source_sha256_loaded") and
                                   after_manifest.get("api_agent_source_sha256_loaded"))
        same_non_exposure_config = (all(before_manifest.get(key) == after_manifest.get(key) for key in paired_keys)
                                    and api_source_verified)
        before_schema = schema_snapshot_summary(OUT_ROOT / args.before)
        after_schema = schema_snapshot_summary(OUT_ROOT / args.after)
        schema_factor = schema_factor_comparison(before_schema, after_schema, args.target_tool)
        target_tool_schema_details = schema_factor["target_tool_schema_details"]
        non_target_differences = schema_factor["non_target_tool_schema_differences"]
        target_factor_verified = schema_factor["expected_one_factor_schema_change_verified"]
        before_schema_public = {key: value for key, value in before_schema.items() if key != "per_tool"}
        after_schema_public = {key: value for key, value in after_schema.items() if key != "per_tool"}
        before_tool_groups = before_manifest.get("benchmark_tool_groups", [])
        after_tool_groups = after_manifest.get("benchmark_tool_groups", [])
        comparability = {"same_provider_id": before_manifest.get("provider_id") == after_manifest.get("provider_id"),
                         "same_git_head": before_manifest.get("git_head") == after_manifest.get("git_head"),
                         "same_catalog_hash": before_manifest.get("catalog_sha256") == after_manifest.get("catalog_sha256"),
                         "same_runner_hash": before_manifest.get("runner_sha256") == after_manifest.get("runner_sha256"),
                         "same_loaded_api_agent_source": (before_manifest.get("api_agent_source_sha256_loaded") == after_manifest.get("api_agent_source_sha256_loaded")
                                                           if api_source_verified else None),
                         "before_api_agent_source_status": before_manifest.get("api_agent_source_status"),
                         "after_api_agent_source_status": after_manifest.get("api_agent_source_status"),
                         "same_agent_runtime_source": (before_manifest.get("agent_runtime_source_sha256") == after_manifest.get("agent_runtime_source_sha256")
                                                       if before_manifest.get("agent_runtime_source_sha256") and after_manifest.get("agent_runtime_source_sha256") else None),
                         "before_schema_snapshot_summary": before_schema_public,
                         "after_schema_snapshot_summary": after_schema_public,
                         "expected_schema_target_tools": schema_factor["target_tools"],
                         "target_tool_schema_details": target_tool_schema_details,
                         "non_target_tool_schema_differences": non_target_differences,
                         "expected_one_factor_schema_change_verified": target_factor_verified,
                         "same_observed_schema_fingerprints": (before_schema["distinct_sha256"] == after_schema["distinct_sha256"]
                                                               if before_schema["distinct_sha256"] and after_schema["distinct_sha256"] else None),
                         "each_batch_schema_consistent": (len(before_schema["distinct_sha256"]) <= 1 and
                                                          len(after_schema["distinct_sha256"]) <= 1),
                         "before_tool_groups": before_tool_groups,
                         "after_tool_groups": after_tool_groups,
                         "only_tool_exposure_treatment_changed": bool(before_tool_groups != after_tool_groups
                                                                        and same_non_exposure_config),
                         "same_non_exposure_config": same_non_exposure_config,
                         "before_repeat_setting": before_manifest.get("repeat"),
                         "after_repeat_setting": after_manifest.get("repeat"),
                         "account_cache_network_state": "not captured; must be matched/recorded manually",
                         "warnings": []}
        if not comparability["same_catalog_hash"]:
            comparability["warnings"].append("Catalog hashes differ; compare only scenarios with matching prompt and rubric fingerprints.")
        if not comparability["same_runner_hash"]:
            comparability["warnings"].append("Runner hashes differ; timing/trace collection may have changed.")
        if not api_source_verified:
            comparability["warnings"].append("At least one batch lacks a loaded API Agent source fingerprint; Python executor code equivalence is unverified.")
        elif not comparability["same_loaded_api_agent_source"]:
            comparability["warnings"].append("Loaded API Agent source fingerprints differ; Python tool executor behavior is not code-paired.")
        if comparability["same_agent_runtime_source"] is None:
            comparability["warnings"].append("At least one batch lacks an Agent runtime source fingerprint; dirty-worktree runtime code equivalence is unverified.")
        elif not comparability["same_agent_runtime_source"]:
            comparability["warnings"].append("Agent runtime source fingerprints differ; tool execution/recovery behavior is not code-paired.")
        if not comparability["same_provider_id"]:
            comparability["warnings"].append("Provider IDs differ; model comparison is not paired.")
        if not comparability["each_batch_schema_consistent"]:
            if target_factor_verified:
                comparability["warnings"].append("Full snapshot hashes changed within batches due staged tool exposure; per-tool fingerprints show the requested target schemas were stable within each condition and non-target schemas matched.")
            else:
                comparability["warnings"].append("A batch exposed multiple distinct tool-schema snapshots; treat its repetitions as a mixed runtime condition.")
        if (comparability["same_observed_schema_fingerprints"] is False
                and before_tool_groups == after_tool_groups and not target_factor_verified):
            comparability["warnings"].append("Observed tool-schema fingerprints differ despite matching tool-group settings; tool selection and latency deltas are confounded by runtime schema drift.")
        comparability["warnings"].append("Account, cache, and live-source state are not reliably paired across these batches; latency/call deltas are descriptive, not causal.")
        def grouped(rows):
            out = {}
            for row in rows:
                out.setdefault(row["scenario_id"], []).append(row)
            return out
        a, b = grouped(left.get("runs", [])), grouped(right.get("runs", []))
        def mean(values):
            return round(sum(values) / len(values), 2) if values else None
        def value_range(values):
            return [round(min(values), 2), round(max(values), 2)] if values else None
        def percentile_value(values, q):
            ordered = sorted(values)
            return round(ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * q) - 1))], 2) if ordered else None
        def fingerprint(row, field):
            provenance = row.get("evaluation_provenance") or {}
            saved = provenance.get(field + "_sha256")
            if saved: return saved
            scenario = row.get("scenario") or {}
            value = (scenario.get("messages") or [scenario.get("input")]) if field == "prompt" else scenario.get("eval")
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() if value is not None else None
        changes = []
        for key in sorted(a.keys() | b.keys()):
            before_rows, after_rows = a.get(key, []), b.get(key, [])
            prompt_matched = bool(before_rows and after_rows and fingerprint(before_rows[0], "prompt") == fingerprint(after_rows[0], "prompt"))
            rubric_matched = bool(before_rows and after_rows and fingerprint(before_rows[0], "rubric") == fingerprint(after_rows[0], "rubric"))
            before_scores = [(x.get("evaluation") or {}).get("success") for x in before_rows]
            after_scores = [(x.get("evaluation") or {}).get("success") for x in after_rows]
            before_ranks = [rank(x) for x in before_scores if rank(x) is not None] if rubric_matched else []
            after_ranks = [rank(x) for x in after_scores if rank(x) is not None] if rubric_matched else []
            before_mean = sum(before_ranks) / len(before_ranks) if before_ranks else None
            after_mean = sum(after_ranks) / len(after_ranks) if after_ranks else None
            delta = (after_mean - before_mean) if before_mean is not None and after_mean is not None else None
            before_ms = [x.get("timings", {}).get("end_to_end_ms") for x in before_rows if isinstance(x.get("timings", {}).get("end_to_end_ms"), (int, float))]
            after_ms = [x.get("timings", {}).get("end_to_end_ms") for x in after_rows if isinstance(x.get("timings", {}).get("end_to_end_ms"), (int, float))]
            before_tools = [x.get("metrics", {}).get("tool_calls") for x in before_rows if isinstance(x.get("metrics", {}).get("tool_calls"), (int, float))]
            after_tools = [x.get("metrics", {}).get("tool_calls") for x in after_rows if isinstance(x.get("metrics", {}).get("tool_calls"), (int, float))]
            before_models = [x.get("metrics", {}).get("model_calls") for x in before_rows if isinstance(x.get("metrics", {}).get("model_calls"), (int, float))]
            after_models = [x.get("metrics", {}).get("model_calls") for x in after_rows if isinstance(x.get("metrics", {}).get("model_calls"), (int, float))]
            before_human = [x.get("metrics", {}).get("human_confirmations") for x in before_rows if isinstance(x.get("metrics", {}).get("human_confirmations"), (int, float))]
            after_human = [x.get("metrics", {}).get("human_confirmations") for x in after_rows if isinstance(x.get("metrics", {}).get("human_confirmations"), (int, float))]
            before_actions = [x.get("metrics", {}).get("user_action_requests") for x in before_rows if isinstance(x.get("metrics", {}).get("user_action_requests"), (int, float))]
            after_actions = [x.get("metrics", {}).get("user_action_requests") for x in after_rows if isinstance(x.get("metrics", {}).get("user_action_requests"), (int, float))]
            before_interventions = [x.get("metrics", {}).get("human_intervention_events") for x in before_rows if isinstance(x.get("metrics", {}).get("human_intervention_events"), (int, float))]
            after_interventions = [x.get("metrics", {}).get("human_intervention_events") for x in after_rows if isinstance(x.get("metrics", {}).get("human_intervention_events"), (int, float))]
            before_web_blocked = [x.get("metrics", {}).get("web_blocked_provider_results") for x in before_rows if isinstance(x.get("metrics", {}).get("web_blocked_provider_results"), (int, float))]
            after_web_blocked = [x.get("metrics", {}).get("web_blocked_provider_results") for x in after_rows if isinstance(x.get("metrics", {}).get("web_blocked_provider_results"), (int, float))]
            before_shortcuts = [x.get("metrics", {}).get("web_blocked_repeat_shortcuts") for x in before_rows if isinstance(x.get("metrics", {}).get("web_blocked_repeat_shortcuts"), (int, float))]
            after_shortcuts = [x.get("metrics", {}).get("web_blocked_repeat_shortcuts") for x in after_rows if isinstance(x.get("metrics", {}).get("web_blocked_repeat_shortcuts"), (int, float))]
            before_ms_mean, after_ms_mean = mean(before_ms), mean(after_ms)
            before_ms_p95, after_ms_p95 = percentile_value(before_ms, .95), percentile_value(after_ms, .95)
            before_tools_mean, after_tools_mean = mean(before_tools), mean(after_tools)
            before_models_mean, after_models_mean = mean(before_models), mean(after_models)
            before_human_mean, after_human_mean = mean(before_human), mean(after_human)
            before_actions_mean, after_actions_mean = mean(before_actions), mean(after_actions)
            before_interventions_mean, after_interventions_mean = mean(before_interventions), mean(after_interventions)
            before_web_blocked_mean, after_web_blocked_mean = mean(before_web_blocked), mean(after_web_blocked)
            before_shortcuts_mean, after_shortcuts_mean = mean(before_shortcuts), mean(after_shortcuts)
            time_delta_pct = ((after_ms_mean - before_ms_mean) / before_ms_mean * 100) if before_ms_mean not in (None, 0) and after_ms_mean is not None else None
            p95_delta_pct = ((after_ms_p95 - before_ms_p95) / before_ms_p95 * 100) if before_ms_p95 not in (None, 0) and after_ms_p95 is not None else None
            efficiency_change = ("mixed_mean_faster_tail_slower" if time_delta_pct is not None and time_delta_pct < 0 and p95_delta_pct is not None and p95_delta_pct > 0 else
                                "mixed_mean_slower_tail_faster" if time_delta_pct is not None and time_delta_pct > 0 and p95_delta_pct is not None and p95_delta_pct < 0 else
                                "faster" if time_delta_pct is not None and time_delta_pct < 0 else
                                "slower" if time_delta_pct is not None and time_delta_pct > 0 else "unchanged_or_unpaired")
            changes.append({"scenario_id": key, "before": before_scores, "after": after_scores,
                            "before_n": len(before_scores), "after_n": len(after_scores),
                            "before_scored_n": len(before_ranks), "after_scored_n": len(after_ranks),
                            "before_mean_rank": round(before_mean, 3) if before_mean is not None else None,
                            "after_mean_rank": round(after_mean, 3) if after_mean is not None else None,
                            "mean_rank_delta": round(delta, 3) if delta is not None else None,
                            "before_mean_ms": before_ms_mean, "after_mean_ms": after_ms_mean,
                            "before_p95_ms": before_ms_p95, "after_p95_ms": after_ms_p95,
                            "p95_ms_delta_percent": round(p95_delta_pct, 1) if p95_delta_pct is not None else None,
                            "before_ms_range": value_range(before_ms), "after_ms_range": value_range(after_ms),
                            "mean_ms_delta": round(after_ms_mean-before_ms_mean, 2) if after_ms_mean is not None and before_ms_mean is not None else None,
                            "mean_ms_delta_percent": round(time_delta_pct, 1) if time_delta_pct is not None else None,
                            "before_mean_tool_calls": before_tools_mean, "after_mean_tool_calls": after_tools_mean,
                            "mean_tool_calls_delta": round(after_tools_mean-before_tools_mean, 2) if after_tools_mean is not None and before_tools_mean is not None else None,
                            "before_mean_model_calls": before_models_mean, "after_mean_model_calls": after_models_mean,
                            "mean_model_calls_delta": round(after_models_mean-before_models_mean, 2) if after_models_mean is not None and before_models_mean is not None else None,
                            "before_mean_human_confirmations": before_human_mean, "after_mean_human_confirmations": after_human_mean,
                            "mean_human_confirmations_delta": round(after_human_mean-before_human_mean, 2) if after_human_mean is not None and before_human_mean is not None else None,
                            "before_mean_user_action_requests": before_actions_mean, "after_mean_user_action_requests": after_actions_mean,
                            "mean_user_action_requests_delta": round(after_actions_mean-before_actions_mean, 2) if after_actions_mean is not None and before_actions_mean is not None else None,
                            "before_mean_human_interventions": before_interventions_mean, "after_mean_human_interventions": after_interventions_mean,
                            "mean_human_interventions_delta": round(after_interventions_mean-before_interventions_mean, 2) if after_interventions_mean is not None and before_interventions_mean is not None else None,
                            "before_mean_web_blocked_provider_results": before_web_blocked_mean,
                            "after_mean_web_blocked_provider_results": after_web_blocked_mean,
                            "mean_web_blocked_provider_results_delta": round(after_web_blocked_mean-before_web_blocked_mean, 2) if after_web_blocked_mean is not None and before_web_blocked_mean is not None else None,
                            "before_mean_web_blocked_repeat_shortcuts": before_shortcuts_mean,
                            "after_mean_web_blocked_repeat_shortcuts": after_shortcuts_mean,
                            "mean_web_blocked_repeat_shortcuts_delta": round(after_shortcuts_mean-before_shortcuts_mean, 2) if after_shortcuts_mean is not None and before_shortcuts_mean is not None else None,
                            "prompt_matched": prompt_matched, "rubric_matched": rubric_matched,
                            "efficiency_change": efficiency_change,
                            "change": "improved" if delta is not None and delta > 0 else "regressed" if delta is not None and delta < 0 else "unchanged_or_unpaired" if rubric_matched else "rubric_mismatch"})
        comparison = {"before": args.before, "after": args.after,
                      "comparability": comparability,
                      "before_summary": left.get("summary"), "after_summary": right.get("summary"), "scenarios": changes}
        after_dir = OUT_ROOT / args.after
        (after_dir / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = []
        for item in changes:
            rows.append("<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else '—'))}</td>" for value in (
                item["scenario_id"], item["before"], item["after"], item["change"], item["mean_rank_delta"],
                item["before_mean_ms"], item["after_mean_ms"], item["mean_ms_delta_percent"],
                item["before_p95_ms"], item["after_p95_ms"], item["p95_ms_delta_percent"], item["efficiency_change"],
                item["before_mean_tool_calls"], item["after_mean_tool_calls"], item["mean_model_calls_delta"],
                item["mean_user_action_requests_delta"], item["mean_human_interventions_delta"],
                item["mean_web_blocked_provider_results_delta"], item["mean_web_blocked_repeat_shortcuts_delta"])) + "</tr>")
        page = """<!doctype html><meta charset=utf-8><title>Campus Agent batch comparison</title>
<style>body{font:15px system-ui;margin:2rem auto;max-width:1250px;color:#172033}header{background:#eff6ff;padding:1.2rem;border-radius:12px}table{border-collapse:collapse;width:100%;margin-top:20px}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}tr:nth-child(even){background:#f8fafc}</style>
<h1>Campus Agent batch comparison</h1><header><b>Before: """ + html.escape(args.before) + """</b><br><b>After: """ + html.escape(args.after) + """</b>
<p>Success-rubric change and efficiency are reported separately. Per-scenario score changes require matching prompt and rubric fingerprints. Samples are descriptive; unmatched model, cache, account, or source conditions prevent causal conclusions.</p><details><summary>Batch comparability</summary><pre>""" + html.escape(json.dumps(comparability, ensure_ascii=False, indent=2)) + """</pre></details></header>
<table><thead><tr><th>Scenario</th><th>Before results</th><th>After results</th><th>Task change</th><th>Rank Δ</th><th>Before ms mean</th><th>After ms mean</th><th>Latency Δ%</th><th>Before P95 ms</th><th>After P95 ms</th><th>P95 Δ%</th><th>Efficiency</th><th>Before tools</th><th>After tools</th><th>Δ model calls</th><th>Δ needs_user</th><th>Δ observed interventions</th><th>Δ blocked provider results</th><th>Δ repeat short-circuits</th></tr></thead><tbody>""" + "".join(rows) + "</tbody></table>"
        (after_dir / "comparison.html").write_text(page, encoding="utf-8")
        print(json.dumps({"comparison_json": str(after_dir / "comparison.json"),
                          "comparison_html": str(after_dir / "comparison.html"), **comparison}, ensure_ascii=False, indent=2))
        return 0
    existing_manifest = None
    if args.resume:
        if not args.batch_id:
            parser.error("--resume 需要 --batch-id")
        manifest_path = OUT_ROOT / args.batch_id / "manifest.json"
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parser.error("无法读取待恢复批次 manifest")
    args.api = args.api or ((existing_manifest or {}).get("api") if args.resume else None) or "http://127.0.0.1:8000"
    args.provider_id = args.provider_id or ((existing_manifest or {}).get("provider_id") if args.resume else None)
    args.repeat = args.repeat or ((existing_manifest or {}).get("repeat") if args.resume else None) or 3
    args.concurrency = args.concurrency or ((existing_manifest or {}).get("concurrency") if args.resume else None) or 1
    args.timeout = args.timeout or ((existing_manifest or {}).get("timeout_seconds") if args.resume else None) or 600
    args.max_model_calls_per_run = args.max_model_calls_per_run or ((existing_manifest or {}).get("max_model_calls_per_run") if args.resume else None) or 24
    args.max_total_model_calls = args.max_total_model_calls or ((existing_manifest or {}).get("max_total_model_calls") if args.resume else None) or 120
    args.prewarm_lead_seconds = (args.prewarm_lead_seconds if args.prewarm_lead_seconds is not None else
                                 ((existing_manifest or {}).get("prewarm_lead_seconds", 0) if args.resume else 0))
    prior_retries = (existing_manifest or {}).get("max_retries") if args.resume else None
    args.max_retries = args.max_retries if args.max_retries is not None else (prior_retries if prior_retries is not None else 1)
    args.seed = args.seed if args.seed is not None else ((existing_manifest or {}).get("seed") if args.resume else None) or 20261016
    if args.resume and args.defer_tool_group is not None and args.defer_tool_group != existing_manifest.get("benchmark_tool_groups", []):
        parser.error("恢复批次不能更改工具暴露实验条件")
    args.defer_tool_group = (args.defer_tool_group if args.defer_tool_group is not None else
                             ((existing_manifest or {}).get("benchmark_tool_groups", []) if args.resume else []))
    if not 1 <= args.repeat <= 10 or not 1 <= args.concurrency <= 2 or not 10 <= args.timeout <= 3600 or not 1 <= args.max_model_calls_per_run <= 128 or not 1 <= args.max_total_model_calls <= 1000 or not 0 <= args.max_retries <= 5 or not 0 <= args.prewarm_lead_seconds <= 30:
        parser.error("repeat 1..10, concurrency 1..2, timeout 10..3600, per-run model calls 1..128, total calls 1..1000, retries 0..5")
    if args.max_cost_usd < 0:
        parser.error("max-cost-usd must be >= 0")
    if not args.provider_id:
        print("缺少 --provider-id。请先在本机 Agent 设置中配置并启用模型；benchmark 不读取密钥。", file=sys.stderr)
        return 2
    try:
        loopback_only(args.api)
    except ValueError as exc:
        parser.error(str(exc))
    resume_ids = (existing_manifest or {}).get("scenario_ids", [])
    requested_ids = args.scenario if args.scenario else (resume_ids if args.resume else None)
    chosen = [s for s in catalog["scenarios"] if not requested_ids or s["id"] in requested_ids]
    if requested_ids and {s["id"] for s in chosen} != set(requested_ids):
        parser.error("存在未知场景 ID")
    real_unsupported = [s for s in chosen if "real" not in s.get("modes", [])]
    if requested_ids and real_unsupported:
        parser.error("所选场景仅支持隔离 mock-e2e：" + ", ".join(s["id"] for s in real_unsupported))
    if not requested_ids:
        chosen = [s for s in chosen if "real" in s.get("modes", [])]
    blocked = [s for s in chosen if s["run_status"] != "runnable"]
    if requested_ids and blocked:
        details = "; ".join(f"{s['id']}: {s['run_block_reason']}" for s in blocked)
        parser.error("所选场景缺少可复现环境，未启动调用：" + details)
    chosen = [s for s in chosen if s["run_status"] == "runnable"]
    if not chosen:
        parser.error("所选场景当前均不可运行；请先满足 catalog 中列出的前置条件")
    if args.resume and not set(s["id"] for s in chosen) <= set(resume_ids):
        parser.error("恢复批次不能添加原批次之外的场景")
    tasks = [(s, i) for s in chosen for i in range(1, args.repeat + 1)]
    if args.limit:
        tasks = tasks[:max(1, args.limit)]
    if not args.resume and not args.scenario and not args.limit and args.max_cost_usd <= 0:
        parser.error("完整批次请显式设置 --max-cost-usd；先用 --scenario/--limit 做 smoke run")
    try:
        batch_dir = select_run_batch_dir(OUT_ROOT, args.batch_id, args.resume)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        parser.error(str(exc))
    api_check = api_preflight(Api(args.api, min(10, args.timeout)))
    if api_check["status"] != "passed":
        print("本机 Agent API 预检失败；未创建场景运行记录。请先启动服务后重试。详情：" +
              json.dumps(api_check, ensure_ascii=False), file=sys.stderr)
        return 2
    source_gate = api_source_gate(api_check, args.allow_unmatched_api_source)
    if source_gate["status"] == "blocked":
        print("API 源码指纹预检未通过；未调用模型或创建场景记录。详情：" +
              json.dumps({"api_preflight": api_check, "source_gate": source_gate}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    provider_check = provider_preflight(Api(args.api, min(10, args.timeout)), args.provider_id)
    if provider_check["status"] != "passed":
        print("模型 Provider 预检失败；未创建场景运行记录或调用模型。请确认指定 Provider 存在、已启用且已配置密钥。详情：" +
              json.dumps(provider_check, ensure_ascii=False), file=sys.stderr)
        return 2
    exposure_check = tool_exposure_preflight(Api(args.api, min(10, args.timeout)), args.defer_tool_group,
                                             provider_check.get("runtime_provider"))
    if exposure_check["status"] != "passed":
        print("请求的工具暴露实验未由 API 显式启用；未创建批次或调用模型。详情：" +
              json.dumps(exposure_check, ensure_ascii=False), file=sys.stderr)
        return 2
    if not args.resume:
        batch_dir.mkdir(parents=True)
        (batch_dir / "runs").mkdir()
    if args.resume:
        manifest = existing_manifest
        manifest.setdefault("resumptions", []).append({"started_at": now(), "api": args.api,
            "provider_id": args.provider_id, "repeat": args.repeat, "concurrency": args.concurrency,
            "timeout_seconds": args.timeout, "max_model_calls_per_run": args.max_model_calls_per_run,
            "max_total_model_calls": args.max_total_model_calls, "max_retries": args.max_retries, "seed": args.seed,
            "prewarm_lead_seconds": args.prewarm_lead_seconds,
            "scenario_filter": args.scenario, "retry_failed": args.retry_failed,
            "benchmark_tool_groups": args.defer_tool_group,
            "api_preflight": api_check, "provider_preflight": provider_check,
            "api_source_gate": source_gate,
            "api_agent_source_sha256_loaded": api_check.get("agent_source_sha256_loaded"),
            "api_agent_source_status": api_check.get("agent_source_status"),
            "tool_exposure_preflight": exposure_check})
        manifest["status"] = "running"
        manifest["runner_sha256_current"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        manifest["agent_runtime_source_sha256_current"] = agent_runtime_source_sha256()
    else:
        manifest = {"batch_id": batch_dir.name, "created_at": now(), "api": args.api,
                    "provider_id": args.provider_id, "repeat": args.repeat, "concurrency": args.concurrency,
                    "timeout_seconds": args.timeout, "max_model_calls_per_run": args.max_model_calls_per_run,
                    "max_total_model_calls": args.max_total_model_calls, "max_retries": args.max_retries,
                    "seed": args.seed, "prewarm_lead_seconds": args.prewarm_lead_seconds,
                    "scenario_ids": [x[0]["id"] for x in tasks],
                    "benchmark_tool_groups": args.defer_tool_group,
                    "catalog_sha256": hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
                    "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "agent_runtime_source_sha256": agent_runtime_source_sha256(),
                    "api_preflight": api_check,
                    "api_source_gate": source_gate,
                    "api_agent_source_sha256_loaded": api_check.get("agent_source_sha256_loaded"),
                    "api_agent_source_status": api_check.get("agent_source_status"),
                    "provider_preflight": provider_check,
                    "tool_exposure_preflight": exposure_check,
                    "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None,
                    "working_tree_dirty": bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()),
                    "status": "running"}
    atomic_json(batch_dir / "manifest.json", manifest)
    layer_results = run_offline_layers(batch_dir, "harness" if args.skip_direct_tools else "all")
    for layer_name, result in layer_results.items():
        print(f"{layer_name}: {result['status']} ({result['mode']})")
    done = set()
    for path in (batch_dir / "runs").glob("*.json"):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            key = (old["scenario_id"], old["run_index"])
            if args.retry_failed and old.get("evaluation", {}).get("success") not in {"成功", "部分成功"}:
                if retry_allowed(old, args.max_retries):
                    continue
                done.add(key)
                continue
            done.add(key)
        except Exception:
            continue
    pending = [(s, i) for s, i in tasks if (s["id"], i) not in done]
    pause_file = batch_dir / "PAUSE"
    budget = CallBudget(args.max_total_model_calls)
    def execute(item):
        scenario, index = item
        while pause_file.exists():
            time.sleep(max(0.2, args.pause_poll_seconds))
        run_key = f"{scenario['id']}-{index}"
        if budget.exhausted.is_set():
            row = {"scenario_id": scenario["id"], "run_index": index, "status": "not_run_budget_exhausted",
                   "environment": "real_api", "scenario": scenario,
                   "diagnosis": {"confirmed": [], "hypotheses": ["批次总模型调用预算已耗尽"], "evidence": [],
                                 "next_experiment": "提高预算或单独运行剩余场景"}, "finished_at": now()}
            return row
        session_api = Api(args.api, min(30, args.timeout))
        rng = random.Random(args.seed + index + sum(map(ord, scenario["id"])))
        global MAX_MODEL_CALLS_PER_RUN
        MAX_MODEL_CALLS_PER_RUN = args.max_model_calls_per_run
        return summarize_run(scenario, session_api, args.provider_id, args.timeout, index,
                             rng.randrange(1 << 31), budget, run_key, args.defer_tool_group,
                             args.prewarm_lead_seconds)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(execute, item): item for item in pending}
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                filename = f"{row['scenario_id']}-run{row['run_index']:02}.json"
                run_path = batch_dir / "runs" / filename
                if run_path.is_file():
                    try:
                        previous = json.loads(run_path.read_text(encoding="utf-8"))
                        row = with_prior_attempt(row, previous)
                    except (OSError, ValueError):
                        row["attempt_history"] = [{"status": "prior_attempt_unreadable"}]
                atomic_json(run_path, row)
                print(f"{row['scenario_id']} run {row['run_index']}: {row.get('evaluation',{}).get('success',row['status'])}")
    except KeyboardInterrupt:
        manifest["status"] = "paused"
        atomic_json(batch_dir / "manifest.json", manifest)
        build_report(batch_dir)
        print(f"已暂停并保存；继续：python tools/campus_bench/runner.py run --resume --batch-id {batch_dir.name} --provider-id {args.provider_id}")
        return 130
    manifest["status"] = "complete"
    atomic_json(batch_dir / "manifest.json", manifest)
    summary = build_report(batch_dir)
    print(f"Batch: {batch_dir.name}; summary={summary}; report={batch_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

