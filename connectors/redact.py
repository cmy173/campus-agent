#!/usr/bin/env python3
"""记录前脱敏统一入口（A8）。

约束（A1 §3.4-9 实测：智云某端点把 JWT 放在 URL 查询参数，载荷含敏感声明）：
  - 任何日志/遥测在调用前必须经本模块处理；
  - **绝不记录完整 URL 与 Referer**：URL 的查询参数与片段一律剔除、userinfo 剥离；
  - Cookie / Set-Cookie / Authorization / Bearer / Basic / JWT 片段 / CAS 票据一律剔除；
  - 失败也保持可用（纯字符串处理，不抛异常）。

用法：
    from connectors.redact import redact, redact_url, RedactingFilter
    logger.info("fetch url=%s", redact_url(url))     # 单 URL
    logger.info("sync result=%s", redact(result))    # dict/list 递归
    logging.getLogger("connectors").addFilter(RedactingFilter())  # 兜底：任何日志调用前强制脱敏
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "<redacted>"

# 键名（大小写不敏感）命中即整值剔除（用于 dict/headers 脱敏）
SENSITIVE_KEYS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "token", "access_token", "refresh_token", "id_token", "jwt",
    "password", "passwd", "pwd", "secret", "client_secret",
    "api_key", "apikey", "session", "session_id", "sessionid",
    "ticket", "signature", "sig", "credential", "credentials",
    "private_key", "x-auth-token",
})

# 查询参数名（大小写不敏感、整体相等）命中即掩蔽其值（用于非 URL 文本中的遗留参数）
_QUERY_SENSITIVE = SENSITIVE_KEYS | {"code", "key"}

_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://[^\s\"'<>()\[\]{}（）「」【】]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]{4,}){2}\b")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_HEADER_RE = re.compile(
    r"(?im)^([ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*).*$"
)
_QUERY_PARAM_RE = re.compile(r"([?&])([A-Za-z0-9_.\-\[\]]+)=([^&\s#\"'<>]*)")
_CAS_TICKET_RE = re.compile(r"\bST-[A-Za-z0-9\-]{8,}\b")


def redact_url(url: str) -> str:
    """剔除 URL 的查询参数与片段，并剥离 userinfo；相对 URL 仅保留路径。"""
    try:
        parts = urlsplit(url)
        if not parts.scheme and not parts.netloc:
            return urlunsplit(("", "", parts.path, "", ""))
        host = parts.hostname or ""
        if ":" in host:  # IPv6
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port is not None else "")
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        # 异常端口或 IPv6 不应让日志记录导致业务退出；也不能原样回显凭据。
        return REDACTED


def _mask_query(match: re.Match[str]) -> str:
    sep, name, value = match.group(1), match.group(2), match.group(3)
    if name.lower() in _QUERY_SENSITIVE:
        return f"{sep}{name}={REDACTED}"
    return match.group(0)


def redact_text(text: str) -> str:
    """剔除字符串中的完整 URL（含查询参数）、认证头、Bearer/Basic、JWT、CAS 票据。"""
    def _url_sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(".,;:!?。，；：！？、")
        return redact_url(trimmed) + raw[len(trimmed):]

    text = _URL_RE.sub(_url_sub, text)
    text = _HEADER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _CAS_TICKET_RE.sub(REDACTED, text)
    text = _QUERY_PARAM_RE.sub(_mask_query, text)
    return text


def redact_mapping(value: Any) -> Any:
    """递归脱敏 dict/list/tuple：敏感键整值剔除，字符串值按 redact_text 处理。"""
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and k.lower() in SENSITIVE_KEYS
                else redact_mapping(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact_mapping(v) for v in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact(value: Any) -> Any:
    """统一入口：str/dict/list/tuple 按类型脱敏；其他类型原样返回。"""
    return redact_mapping(value)


class RedactingFilter(logging.Filter):
    """logging 兜底：在记录发出前对 msg 与 args 强制脱敏（挂到 logger 上）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 先脱敏结构化参数，再格式化，再处理最终文本。不能直接解析含 %s/%d 的 URL 模板。
        if not isinstance(record.msg, str):
            record.msg = redact_mapping(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_mapping(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_mapping(a) for a in record.args)
            else:
                record.args = redact_mapping(record.args)
        try:
            record.msg = redact_text(record.getMessage())
        except (TypeError, ValueError, KeyError):
            record.msg = REDACTED + "（日志格式异常）"
        record.args = ()
        return True
