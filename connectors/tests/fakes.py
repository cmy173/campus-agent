#!/usr/bin/env python3
"""测试替身（xzzd/zykt 适配器离线单测共用；不触网、无真实凭据）。"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..adapters.transport import TransportResponse


class ScriptedTransport:
    """返回排队响应并记录请求的传输替身（响应用尽或遇预期外请求时断言失败）。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> TransportResponse:
        self.calls.append(
            {"method": method, "path": path, "params": params, "json_body": json_body}
        )
        if not self.responses:
            raise AssertionError(f"未预期的请求：{method} {path}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def json_response(payload: Any, status: int = 200) -> TransportResponse:
    return TransportResponse(status_code=status, text=json.dumps(payload, ensure_ascii=False))


def text_response(text: str, status: int = 200) -> TransportResponse:
    return TransportResponse(status_code=status, text=text)
