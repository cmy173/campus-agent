#!/usr/bin/env python3
"""questionbanks 测试共用：脚本化取页传输替身 + 夹具装载（不触网）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from connectors.questionbanks.types import PageResponse

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "questionbanks"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def page(status_code: int = 200, text: str = "", *,
         headers: Optional[dict[str, str]] = None,
         truncated: bool = False) -> PageResponse:
    return PageResponse(status_code=status_code, text=text,
                        headers={k.lower(): v for k, v in (headers or {}).items()},
                        truncated=truncated)


def redirect(location: str, status_code: int = 302,
             headers: Optional[dict[str, str]] = None) -> PageResponse:
    merged = dict(headers or {})
    merged["Location"] = location
    return page(status_code, "", headers=merged)


class ScriptedPageTransport:
    """按 URL 脚本返回响应并记录每次请求（含请求头，用于凭据泄漏断言）。

    routes: {url: PageResponse | Exception | [item, ...]}；列表按序消费，
    用尽或遇到未登记 URL 时断言失败（暴露测试未预期的网络行为）。
    """

    def __init__(self, routes: Optional[dict[str, Any]] = None) -> None:
        self.routes: dict[str, list[Any]] = {}
        for url, value in (routes or {}).items():
            self.routes[url] = list(value) if isinstance(value, list) else [value]
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, params: Optional[dict[str, Any]] = None,
                json_body: Optional[dict[str, Any]] = None,
                headers: Optional[dict[str, str]] = None,
                max_bytes: Optional[int] = None) -> PageResponse:
        self.calls.append({"method": method, "url": url, "params": params,
                           "json_body": json_body, "headers": dict(headers or {}),
                           "max_bytes": max_bytes})
        queue = self.routes.get(url)
        if not queue:
            raise AssertionError(f"未预期的请求：{method} {url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def calls_for(self, url: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["url"] == url]


class FakeClock:
    """记录 sleep 调用的假睡眠（重试/Retry-After 测试用；不真实等待）。"""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
