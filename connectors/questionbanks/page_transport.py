#!/usr/bin/env python3
"""RequestsPageTransport：题库检索专用绝对 URL 取页传输（requests 薄封装）。

凭据红线（A 侧长期约束）：
  - 本传输**只用于第三方题库/搜索服务**，与学校会话传输完全隔离；
  - 构造时创建**全新 session** 并清空 cookie jar；即使调用方传入 session，也先清 cookie，
    且显式剔除 Cookie/Authorization 等敏感头（防御性，双重保险）；
  - 不跟随重定向（``allow_redirects=False``）；重定向由引擎手动处理，以阻断
    跨主机凭据传递与 https→http 降级。
  - 不记录完整 URL 与查询参数（调用方记录时须经 connectors.redact）。

大小护栏：``max_bytes`` 非空时以流式读取，超限即中止并裁剪，响应标记 ``truncated=True``
（不抛错——截断页仍可提取，由引擎产生警告）。
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from .types import NETWORK_ERROR, PageResponse, QuestionBankError

_SENSITIVE_HEADER_NAMES = {"cookie", "set-cookie", "authorization", "proxy-authorization"}

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 zju-workbench-questionbanks/1.0"
)


def strip_sensitive_headers(headers: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
    """剔除 Cookie/Authorization 等敏感头（大小写不敏感）。空结果返回 None。"""
    if not headers:
        return None
    kept = {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADER_NAMES}
    return kept or None


class RequestsPageTransport:
    """生产用取页传输。用法（B 侧）::

        transport = RequestsPageTransport()
        outcome = search_candidates(request, transport)
    """

    def __init__(self, *, timeout: float = 15.0, user_agent: str = _DEFAULT_USER_AGENT,
                 session: Any = None) -> None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - 依赖缺失时如实报错
            raise QuestionBankError(NETWORK_ERROR, "requests 未安装，无法执行网络取页",
                                    details={"reason": "dependency_missing"}) from exc
        self._requests = requests
        self._session = session if session is not None else requests.Session()
        # 防御：即便调用方误传带会话的 session，也先清空 cookie，绝不携带账户凭据
        try:
            self._session.cookies.clear()
        except Exception:  # noqa: BLE001 - 第三方 session 接口不一，尽力而为
            pass
        self._session.headers.pop("Cookie", None)
        self._session.headers.pop("Authorization", None)
        self._timeout = timeout
        self._user_agent = user_agent

    # -- PageTransport 协议实现 --

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        max_bytes: Optional[int] = None,
    ) -> PageResponse:
        safe_headers = strip_sensitive_headers(headers) or {}
        safe_headers.setdefault("User-Agent", self._user_agent)
        requests = self._requests
        try:
            resp = self._session.request(
                method, url, params=params, json=json_body, headers=safe_headers,
                timeout=self._timeout, allow_redirects=False, stream=True,
            )
        except requests.exceptions.Timeout as exc:
            raise QuestionBankError(NETWORK_ERROR, "请求超时", retryable=True,
                                    details={"reason": "timeout",
                                             "host": urlsplit(url).hostname}) from exc
        except requests.exceptions.SSLError as exc:
            raise QuestionBankError(NETWORK_ERROR, "TLS 校验失败", retryable=False,
                                    details={"reason": "tls",
                                             "host": urlsplit(url).hostname}) from exc
        except requests.exceptions.ConnectionError as exc:
            raise QuestionBankError(NETWORK_ERROR, "连接失败", retryable=True,
                                    details={"reason": "connection",
                                             "host": urlsplit(url).hostname}) from exc
        except requests.exceptions.RequestException as exc:
            raise QuestionBankError(NETWORK_ERROR, "请求失败", retryable=False,
                                    details={"reason": type(exc).__name__,
                                             "host": urlsplit(url).hostname}) from exc
        try:
            truncated = False
            if max_bytes is None:
                data = resp.content
            else:
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if total + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - total])
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                data = b"".join(chunks)
            encoding = resp.encoding or "utf-8"
            try:
                text = data.decode(encoding, errors="replace")
            except LookupError:
                text = data.decode("utf-8", errors="replace")
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        finally:
            resp.close()
        return PageResponse(status_code=resp.status_code, text=text,
                            headers=resp_headers, truncated=truncated, final_url=url)
