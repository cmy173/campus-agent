#!/usr/bin/env python3
"""适配器协议与共享类型（A3 五操作面的落地约定）。

输出形状以 contracts/schemas/ 为准：
  - check_connection -> connection-state.schema.json
  - get_capabilities -> capabilities.schema.json
  - list_courses     -> {"items": Course[], "next_cursor": str|null, "errors": Error[]}（A3 §3.3；items 对 course.schema.json）
  - sync_course      -> sync-result.schema.json
  - fetch_resource   -> fetch-outcome.schema.json

connection 上下文（connection_id / 会话物料）由适配器实例持有（构造参数），
不逐调用传入；适配器不接触业务数据库，不落盘会话。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, runtime_checkable


class AdapterError(Exception):
    """连接级致命错误：载荷形状为 error.schema.json#/$defs/Error。

    单项失败不抛此异常——应作为 sync-result.errors 的条目返回（A4 单项失败隔离）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        details: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.error: dict[str, Any] = {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details,
            "request_id": request_id or "req_adapter",
        }

    @property
    def code(self) -> str:
        return self.error["code"]

    @property
    def retryable(self) -> bool:
        return self.error["retryable"]


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台适配器协议（A3 §3）。实现方：mock（M1 主通道）、xzzd / zykt（真实）。"""

    platform: str
    connection_id: str
    connector_version: str

    def check_connection(self) -> dict[str, Any]:
        """检查连接状态（ConnectionState）。认证失效不重试、不循环。"""
        ...

    def get_capabilities(self) -> dict[str, Any]:
        """能力清单（Capabilities）。未实测的能力必须为 unknown，不得声明为 supported。"""
        ...

    def list_courses(
        self, cursor: Optional[str] = None, term: Optional[str] = None
    ) -> dict[str, Any]:
        """课程列表（分页信封）。游标不透明；单项错误进 errors，不中断整页。"""
        ...

    def sync_course(
        self,
        course_id: str,
        since_cursor: Optional[str] = None,
        kinds: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """同步一门课程（SyncResult）。空响应/失败/限流不产生 deletions。"""
        ...

    def fetch_resource(self, resource_id: str) -> dict[str, Any]:
        """获取资源内容（FetchOutcome）。受限资源返回 denial，不抛错、不绕过。"""
        ...
