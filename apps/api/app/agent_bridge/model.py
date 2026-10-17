"""Translate actual Harness messages to the existing saved Chat Completions provider.

Only this trusted process reads the provider key. No provider URL/key/model comes
from the conversation model or the runtime bridge payload.
"""
from __future__ import annotations

import json
import hashlib
import re

# Context and tool output are untrusted material, not a channel for credentials.
# Never fingerprint secrets here: redact values before any provider serialization.
_REDACTED = '[REDACTED]'
_AUTH_FIELD = re.compile(r'(?:authorization|proxy.?authorization|cookie|set.?cookie|password|passwd|pwd|secret|(?:api|access|refresh|id|auth|session|csrf|xsrf)[_-]?(?:key|token)|credential|client.?secret|private.?key|bearer|token)', re.I)
_AUTH_VALUE = re.compile(
    r'(?i)(\b(?:authorization|proxy-authorization)\s*[:=]\s*(?:Bearer|Basic)\s+)[^\s,;\"\'<>]+'
    r'|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+'
    r'|\bsk-[A-Za-z0-9_-]{8,}'
    r'|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
)
_AUTH_ASSIGNMENT = re.compile(
    r'(?i)([\"\']?(?:authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|password|passwd|pwd|secret|(?:api|access|refresh|id|auth|session|csrf|xsrf)[_-]?(?:key|token)|credential|client[-_]?secret|private[-_]?key|token)[\"\']?\s*[:=]\s*)'
    r'(?:\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\'|[^\s,;\]\}]+)'
)

TOOL_SCHEMA_SNAPSHOT_MAX_CHARS = 64_000


def tool_schema_snapshot(items, max_chars: int = TOOL_SCHEMA_SNAPSHOT_MAX_CHARS) -> dict:
    """Capture model-visible tool contracts without logging auth-like values."""
    safe_items = redact_context(items if isinstance(items, list) else [])
    serialized = json.dumps(safe_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot = {
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "count": len(safe_items),
        "truncated": len(serialized) > max_chars,
    }
    if not snapshot["truncated"]:
        snapshot["schemas"] = safe_items
    return snapshot


def redact_context(value, credentials=()):
    """Copy nested model context, removing auth fields/patterns without logging it."""
    if isinstance(value, dict):
        return {key: _REDACTED if _AUTH_FIELD.search(str(key)) else redact_context(item, credentials)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_context(item, credentials) for item in value]
    if not isinstance(value, str):
        return value
    # JSON embedded in text/tool arguments retains its type/shape after redaction.
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(redact_context(parsed, credentials), ensure_ascii=False)
    for credential in credentials:
        if credential:
            value = value.replace(credential, _REDACTED)
    value = _AUTH_VALUE.sub(_REDACTED, value)
    value = _AUTH_ASSIGNMENT.sub(lambda match: match[1] + '\"' + _REDACTED + '\"', value)
    # URL userinfo is also authentication material; no remote origin is changed.
    return re.sub(r'(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@', r'\1[REDACTED]@', value)

import requests

from .. import secrets_store
from ..errors import ApiError
from ..models import ProviderConfig, iso_utc
from ..providers import MAX_RESPONSE_BYTES, ProviderCallError, _http_error, chat_endpoint, normalize_base_url
from .tools import schemas
from urllib.parse import urlsplit


def snapshot(cfg: ProviderConfig) -> dict:
    return {"id": cfg.id, "base_url": cfg.base_url, "model": cfg.model,
            "protocol": cfg.protocol, "timeout_seconds": cfg.timeout_seconds,
            "updated_at": iso_utc(cfg.updated_at)}


def messages_for_provider(messages: list[dict]) -> list[dict]:
    result = []
    for message in messages:
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ApiError(400, "INVALID_INPUT", "无法识别的消息角色")
        text, calls = [], []
        for block in message.get("content", []):
            if block.get("type") == "text":
                text.append(block["text"])
            elif block.get("type") == "tool-call" and role == "assistant":
                calls.append({"id": block["id"], "type": "function", "function": {
                    "name": block["name"], "arguments": block["arguments"]}})
            elif block.get("type") != "reasoning":
                raise ApiError(400, "UNSUPPORTED", "当前对话模型桥接仅支持文本与领域工具")
        item = {"role": "system" if role == "developer" else role, "content": "\n".join(text)}
        if calls:
            item["tool_calls"] = calls
        if role == "tool":
            item["tool_call_id"] = message["toolCallId"]
        result.append(item)
    return result


def validated_config(db, settings: dict) -> ProviderConfig:
    cfg = db.get(ProviderConfig, settings["provider"]["id"])
    if cfg is None:
        raise ApiError(409, "PROVIDER_CHANGED", "模型配置已删除，请选择可用服务")
    if not cfg.enabled:
        raise ApiError(403, "PROVIDER_DISABLED", "该模型服务已停用，请选择其他模型或在设置中启用")
    # Cosmetic edits / enable toggles need no new consent. Changing the external
    # recipient or protocol still cannot silently redirect an existing task.
    current = snapshot(cfg)
    prior = settings["provider"]
    if any(current.get(k) != prior.get(k) for k in ("id", "base_url", "model", "protocol")):
        raise ApiError(409, "PROVIDER_CHANGED", "模型服务地址或模型已变化，请使用所选服务新建对话")
    return cfg


def runtime_configuration(db, settings: dict) -> dict:
    cfg = validated_config(db, settings)
    url = urlsplit(cfg.base_url)
    official = url.scheme == 'https' and url.hostname == 'api.deepseek.com' and url.port in (None, 443) and url.path.rstrip('/') in ('', '/v1')
    if official and not secrets_store.get_key(cfg.id):
        # An official provider without a stored key would reuse a warm process's
        # old credential. Require a live key instead of silently continuing.
        raise ApiError(403, 'PROVIDER_KEY_MISSING', '该模型服务尚未配置密钥，请在设置中重新填写后使用')
    # Non-secret credential version: clearing/replacing a key changes the runner
    # signature without ever fingerprinting the key value.
    revision = secrets_store.key_revision(cfg.id)
    # Returned only to the owned subprocess environment, never model/log/UI.
    return {'provider': 'deepseek-official' if official else 'campus-model', 'model': cfg.model,
            'provider_id': cfg.id, 'key_revision': revision, 'key': secrets_store.get_key(cfg.id) if official else None}


def registry_schemas(items: list[dict] | None) -> list[dict]:
    domain = {s['model_name']: {'name': s['model_name'], 'description': s['description'], 'parameters': s['input_schema']} for s in schemas()}
    if items is None:
        return list(domain.values())
    result, names = [], set()
    for item in items:
        name = item.get('name')
        if not isinstance(name, str) or not name or name in names or not isinstance(item.get('parameters'), dict):
            raise ApiError(400, 'INVALID_INPUT', '运行时工具定义必须有唯一名称和参数 schema')
        names.add(name)
        if name.startswith('campus_'):
            if name not in domain or item['parameters'] != domain[name]['parameters']:
                raise ApiError(400, 'INVALID_INPUT', '校园工具定义与领域契约不一致')
            result.append(domain[name])
        else:
            result.append({'name': name, 'description': str(item.get('description', '')), 'parameters': item['parameters']})
    return result


def call_model(db, settings: dict, messages: list[dict], max_tokens: int, runtime_tools: list[dict] | None = None) -> dict:
    cfg = validated_config(db, settings)
    base = normalize_base_url(cfg.base_url)
    headers = {"Content-Type": "application/json"}
    key = secrets_store.get_key(cfg.id)
    if key:
        headers["Authorization"] = "Bearer " + key
    tool_specs = registry_schemas(runtime_tools)
    # Credentials are never part of model context: redact before serialization.
    safe_messages = redact_context(messages, (key,)) if key else redact_context(messages)
    payload = {"model": cfg.model, "messages": messages_for_provider(safe_messages), "stream": False,
               "max_tokens": max_tokens,
               "tools": [{"type": "function", "function": s} for s in tool_specs], "tool_choice": "auto"}
    try:
        response = requests.post(chat_endpoint(base), json=payload, headers=headers,
                                 timeout=(10, cfg.timeout_seconds), allow_redirects=False)
    except requests.exceptions.Timeout:
        raise ProviderCallError("PROVIDER_TIMEOUT", "模型响应超时，可以重试本轮请求", retryable=True) from None
    except requests.exceptions.RequestException:
        raise ProviderCallError("PROVIDER_UNREACHABLE", "无法连接所选模型服务", retryable=True) from None
    try:
        if 300 <= response.status_code < 400:
            raise ProviderCallError("PROVIDER_HTTP_ERROR", "模型服务发生重定向，请检查服务地址", retryable=False)
        if response.status_code >= 400:
            raise _http_error(response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("oversize")
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ProviderCallError("PROVIDER_OUTPUT_TRUNCATED", "模型输出超出本轮上限", retryable=False)
        message = choice["message"]
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise ValueError("content")
        allowed = {s["name"] for s in tool_specs}
        calls, ids = [], set()
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list) or len(raw_calls) > 8:
            raise ValueError("tool count")
        for call in raw_calls:
            function = call["function"]
            args = json.loads(function["arguments"])
            if function["name"] not in allowed or not isinstance(args, dict):
                raise ValueError("tool")
            call_id = call["id"]
            if not isinstance(call_id, str) or not call_id or len(call_id) > 128 or call_id in ids:
                raise ValueError("id")
            ids.add(call_id)
            calls.append({"id": call_id, "name": function["name"], "arguments": args})
        if not content.strip() and not calls:
            raise ValueError("empty")
        return {"content": content, "tool_calls": calls}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ProviderCallError("PROVIDER_INVALID_RESPONSE", "模型返回内容不符合文本／工具调用契约", retryable=False) from None
    finally:
        response.close()
