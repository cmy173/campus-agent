"""Translate actual Harness messages to the existing saved Chat Completions provider.

Only this trusted process reads the provider key. No provider URL/key/model comes
from the conversation model or the runtime bridge payload.
"""
from __future__ import annotations

import json

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
    if cfg is None or snapshot(cfg) != settings["provider"]:
        raise ApiError(409, "PROVIDER_CHANGED", "模型配置已变化，请新建会话并重新确认发送范围")
    if not settings["send_to_provider"]:
        raise ApiError(403, "CONSENT_REQUIRED", "尚未授权向所选模型发送消息和工具返回的资料")
    return cfg


def runtime_configuration(db, settings: dict) -> dict:
    cfg = validated_config(db, settings)
    url = urlsplit(cfg.base_url)
    official = url.scheme == 'https' and url.hostname == 'api.deepseek.com' and url.port in (None, 443) and url.path.rstrip('/') in ('', '/v1')
    # Returned only to the owned subprocess environment, never model/log/UI.
    return {'provider': 'deepseek-official' if official else 'campus-model', 'model': cfg.model,
            'key': secrets_store.get_key(cfg.id) if official else None}


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
    payload = {"model": cfg.model, "messages": messages_for_provider(messages), "stream": False,
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
