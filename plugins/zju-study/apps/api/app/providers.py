"""AI 服务：base_url 规范化、OpenAI 兼容 Chat Completions 调用与连接测试。

安全约束（红线）：
- 模型请求仅由 B 侧发起；密钥只从本地密钥存储按需读取，不写入数据库/响应/日志/导出；
- 错误 message/details 只含状态码与错误码，不含密钥、请求头、上游响应体原文；
- 不跟随重定向；响应体有大小上限；base_url 规范化规则见 contracts/schemas/provider-config.schema.json。
"""
from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from .errors import ApiError

CHAT_SUFFIX = "/chat/completions"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TEST_PROMPT = "这是一次连接测试。请只回复：pong"
TEST_MAX_TOKENS = 8192

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _bad_url(reason: str) -> ApiError:
    return ApiError(
        400,
        "VALIDATION_FAILED",
        f"base_url 无效：{reason}",
        details={"fields": [{"field": "base_url", "reason": reason}]},
    )


def _is_local_http_host(host: str) -> bool:
    if host in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def normalize_base_url(raw: str) -> str:
    """按契约规则规范化（去空白/末尾斜杠、仅 http(s)、空路径补 /v1、拒绝端点后缀与 v1/v1）。

    返回规范化后的 base_url（实际请求地址 = base_url + /chat/completions）。
    """
    text = raw.strip()
    if not text:
        raise _bad_url("不能为空")
    if any(ch.isspace() for ch in text):
        raise _bad_url("地址中不得包含空白字符")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise _bad_url("仅支持 http/https 地址")
    if parts.username or parts.password:
        raise _bad_url("地址中不得包含用户名/密码")
    if parts.query:
        raise _bad_url("地址不得包含查询串")
    if parts.fragment:
        raise _bad_url("地址不得包含片段")
    host = parts.hostname
    if not host:
        raise _bad_url("缺少主机名")
    if host.lower() in ("platform.deepseek.com", "chat.deepseek.com"):
        raise _bad_url("这是 DeepSeek 网页/管理后台地址，请使用 https://api.deepseek.com/v1，并填写实际支持的模型标识")
    try:
        port = parts.port
    except ValueError:
        raise _bad_url("端口号不合法") from None
    if parts.scheme == "http" and not _is_local_http_host(host):
        raise _bad_url("非本机/内网地址必须使用 https")
    path = parts.path.rstrip("/")
    low = path.lower()
    if low.endswith(CHAT_SUFFIX):
        raise _bad_url("请填写服务根地址或 /v1 地址，不要包含 /chat/completions")
    if "/v1/v1" in low:
        raise _bad_url("路径出现重复的 /v1，请只保留一个")
    if not path:
        path = "/v1"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{parts.scheme}://{netloc}{path}"


def chat_endpoint(base_url: str) -> str:
    return base_url + CHAT_SUFFIX


class ProviderCallError(Exception):
    """模型调用失败（PROVIDER_* 码表；message 永不包含密钥或响应体原文）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status

    def to_error_info(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _http_error(status: int) -> ProviderCallError:
    if status in (401, 403):
        return ProviderCallError(
            "PROVIDER_AUTH_FAILED", f"服务端拒绝了凭据（HTTP {status}），请检查密钥与地址", retryable=False, http_status=status
        )
    if status == 429:
        return ProviderCallError(
            "PROVIDER_RATE_LIMITED", "服务端限流（HTTP 429），稍后重试", retryable=True, http_status=status
        )
    if 400 <= status < 500:
        return ProviderCallError(
            "PROVIDER_HTTP_ERROR", f"服务端返回 HTTP {status}，请检查地址与模型名", retryable=False, http_status=status
        )
    return ProviderCallError(
        "PROVIDER_HTTP_ERROR", f"服务端返回 HTTP {status}，稍后重试", retryable=True, http_status=status
    )


def call_chat_completions(
    base_url: str,
    model: str,
    api_key: str | None,
    timeout_seconds: int,
    messages: list[dict],
    *,
    max_tokens: int = 8192,
    connection_test: bool = False,
    disable_thinking: bool = False,
    require_complete: bool = False,
) -> tuple[str, int]:
    """单次 Chat Completions 调用；返回 (首条回复内容, HTTP 状态码)。

    仅抛 ProviderCallError（结构化、无敏感内容）；调用方决定是否重试（429/超时/5xx 有界重试，401 永不）。
    """
    headers = {"Content-Type": "application/json"}
    try:
        base_url = normalize_base_url(base_url)
    except ApiError as exc:
        raise ProviderCallError("PROVIDER_HTTP_ERROR", exc.message, retryable=False) from exc
    if urlsplit(base_url).hostname == "api.deepseek.com" and model.strip().lower() == "deepseek":
        raise ProviderCallError("PROVIDER_HTTP_ERROR", "deepseek 不是完整模型标识，请填写 DeepSeek 当前提供的具体模型名", retryable=False)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    if (connection_test or disable_thinking) and urlsplit(base_url).hostname == "api.deepseek.com":
        # 连通探针无需长思考；仅针对已确认支持此参数的官方服务。
        payload["thinking"] = {"type": "disabled"}
    try:
        resp = requests.post(
            chat_endpoint(base_url),
            json=payload,
            headers=headers,
            timeout=(10, timeout_seconds),
            allow_redirects=False,
        )
    except requests.exceptions.ConnectTimeout as exc:
        raise ProviderCallError("PROVIDER_TIMEOUT", "连接服务超时（建立连接限时 10s）；请检查 API 域名、网络与代理", retryable=True) from exc
    except requests.exceptions.Timeout as exc:
        raise ProviderCallError(
            "PROVIDER_TIMEOUT", f"等待模型响应超时（{timeout_seconds}s）；可提高响应超时或使用更快的模型", retryable=True
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise ProviderCallError("PROVIDER_UNREACHABLE", "TLS 握手失败，无法连接服务地址", retryable=True) from exc
    except requests.exceptions.RequestException as exc:
        raise ProviderCallError("PROVIDER_UNREACHABLE", "无法连接服务地址", retryable=True) from exc

    status = resp.status_code
    if status in (301, 302, 303, 307, 308):
        raise ProviderCallError(
            "PROVIDER_HTTP_ERROR",
            "服务地址发生重定向（出于安全不跟随），请填写最终可达地址",
            retryable=False,
            http_status=status,
        )
    if status >= 400:
        raise _http_error(status)
    if len(resp.content) > MAX_RESPONSE_BYTES:
        raise ProviderCallError("PROVIDER_INVALID_RESPONSE", "响应体过大，非预期的 Chat Completions 响应", retryable=False, http_status=status)
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderCallError(
            "PROVIDER_INVALID_RESPONSE", "响应不是合法 JSON（地址可能不是 OpenAI 兼容服务）", retryable=False, http_status=status
        ) from exc
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ProviderCallError(
            "PROVIDER_INVALID_RESPONSE", "响应缺少 choices[0].message.content 字段", retryable=False, http_status=status
        ) from exc
    if connection_test and isinstance(message.get("reasoning_content"), str) and message["reasoning_content"].strip() and not content:
        # 有效推理响应已经证明模型接口可用；不将推理文字当作正式出卷结果。
        return "[有效模型响应]", status
    if require_complete and choice.get("finish_reason") == "length":
        raise ProviderCallError("PROVIDER_OUTPUT_TRUNCATED", "课堂总结输出未完成，未保存截断内容，请调整输出配额或使用其他模型", retryable=False, http_status=status)
    if not isinstance(content, str) or not content.strip():
        if choice.get("finish_reason") == "length":
            raise ProviderCallError("PROVIDER_OUTPUT_TRUNCATED", "输出 token 上限已耗尽，模型尚未输出正式答案；这不是连接失败", retryable=False, http_status=status)
        raise ProviderCallError("PROVIDER_INVALID_RESPONSE", "模型回复内容为空", retryable=False, http_status=status)
    return content, status


def run_connection_test(base_url: str, model: str, api_key: str | None, timeout_seconds: int) -> dict:
    """连接测试（固定短提示，不发送用户材料）；返回 TestResult 形状（远端失败也是结果本身）。"""
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not api_key:
        return {
            "ok": False,
            "checked_at": checked_at,
            "latency_ms": None,
            "http_status": None,
            "error": {
                "code": "PROVIDER_KEY_MISSING",
                "message": "尚未配置密钥，填写密钥后可测试连通",
                "retryable": False,
            },
        }
    started = time.perf_counter()
    try:
        _, status = call_chat_completions(
            base_url, model, api_key, timeout_seconds, [{"role": "user", "content": TEST_PROMPT}], max_tokens=TEST_MAX_TOKENS, connection_test=True
        )
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "checked_at": checked_at, "latency_ms": latency, "http_status": status, "error": None}
    except ProviderCallError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "checked_at": checked_at,
            "latency_ms": latency,
            "http_status": exc.http_status,
            "error": exc.to_error_info(),
        }
