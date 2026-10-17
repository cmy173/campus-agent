#!/usr/bin/env python3
"""B ↔ A 受控会话桥（A20；B 进程侧，仅标准库）。

B 进程通过环境变量 ``STUDY_SYNC_ADAPTERS`` 加载本适配器
（``{"xzzd": "connectors.bridge:XzzdBridgeAdapter"}``）：B 的 sync/check 调用经本类
转发到本机受控浏览器会话服务（``connectors.browser.session_runner serve``，
缺省 ``http://127.0.0.1:8765``，环境变量 ``XZZD_BRIDGE_URL`` 可覆盖），由会话侧执行
``XzzdAdapter`` 并回传结果。

边界与红线：
  - 本模块**不含任何会话物料**：请求体只有业务参数（platform/connection_id/course_id/
    resource_id），不接收、不转发、不记录 cookie/token；
  - 错误以 A3 形状 ``.error`` 字典随异常抛出（B ``sync_engine._translate_protocol_error``
    读取该字典转译为 B 侧 AdapterError；``AUTH_EXPIRED`` 的恢复动作在 ``details.action``）；
  - 连接器服务不可达 → ``DEPENDENCY_UNAVAILABLE``（不静默降级为“同步成功”）；
    响应不可解析/形状异常 → ``NETWORK_ERROR``（可重试），如实上抛。

超时：check 30s；sync/fetch 缺省 600s（环境变量 ``XZZD_BRIDGE_TIMEOUT`` 覆盖）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_S = 600.0
_CHECK_TIMEOUT_S = 30.0


def _utc_now_iso() -> str:
    """当前 UTC 时刻（秒级 ISO 8601，Z 结尾；与契约 timestamp 形状一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class BridgeError(Exception):
    """携带 A3 形状 error dict 的桥接异常（B ``_translate_protocol_error`` 读 ``.error``）。"""

    def __init__(self, error: dict[str, Any]) -> None:
        self.error: dict[str, Any] = dict(error)
        super().__init__(str(error.get("message") or error.get("code") or "桥接错误"))


class XzzdBridgeAdapter:
    """A3 五操作面桥接（当前 B 调用面：check_connection / sync_course / fetch_resource）。

    构造参数与 B ``sync_engine._bridge_protocol_adapter`` 的工厂调用一致（关键字
    platform/connection_id）；base_url/timeout 可显式传入（联调与测试），缺省读环境变量。
    """

    def __init__(self, platform: str = "xzzd", connection_id: str = "",
                 base_url: Optional[str] = None, timeout: Optional[float] = None) -> None:
        self._platform = platform
        self._connection_id = connection_id
        env_url = os.environ.get("XZZD_BRIDGE_URL")
        self._base_url = (base_url or env_url or DEFAULT_BRIDGE_URL).rstrip("/")
        # 回环通信不应经过系统代理，否则代理故障会被误报为连接器没启动。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if urlsplit(self._base_url).hostname in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()
        env_timeout = os.environ.get("XZZD_BRIDGE_TIMEOUT")
        try:
            self._timeout = float(timeout if timeout is not None else (env_timeout or DEFAULT_TIMEOUT_S))
        except ValueError:
            self._timeout = DEFAULT_TIMEOUT_S

    # —— A3 操作面 ——

    def check_connection(self) -> dict[str, Any]:
        return self._request("check", self._base_body(),
                             timeout=min(self._timeout, _CHECK_TIMEOUT_S))

    def list_courses(self, cursor: Optional[str] = None, term: Optional[str] = None) -> dict[str, Any]:
        return self._request("courses", {**self._base_body(), "cursor": cursor, "term": term},
                             timeout=self._timeout)


    def sync_course(self, course_id: str, since_cursor: Optional[str] = None,
                    kinds: Optional[Sequence[str]] = None) -> dict[str, Any]:
        body = self._base_body()
        body["course_id"] = course_id
        if since_cursor is not None:
            body["since_cursor"] = since_cursor
        if kinds is not None:
            body["kinds"] = list(kinds)
        return self._request("sync", body, timeout=self._timeout)

    def fetch_resource(self, resource_id: str) -> dict[str, Any]:
        body = self._base_body()
        body["resource_id"] = resource_id
        return self._request("fetch", body, timeout=self._timeout)

    def fetch_resource_bytes(self, resource_id: str) -> dict[str, Any]:
        return self._request("content", {**self._base_body(), "resource_id": resource_id, "transfer": "local_file"}, timeout=max(self._timeout, 1200))

    # —— 传输 ——

    def _base_body(self) -> dict[str, Any]:
        return {"platform": self._platform, "connection_id": self._connection_id}

    def _request(self, op: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        url = f"{self._base_url}/{op}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self._opener.open(request, timeout=timeout) as resp:  # noqa: S310 —— 本机回环
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
            except Exception:
                raise self._unreachable(f"连接器服务返回 HTTP {exc.code} 且响应不可解析") from None
        except TimeoutError:
            raise self._timeout_error(url) from None
        except (urllib.error.URLError, OSError):
            raise self._unreachable(
                f"本地连接器服务不可达（{self._base_url}）："
                "请在工作台启动器中停止后重新启动，并勾选对应平台；连接器由启动器自动管理，无需手动运行命令"
            ) from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise self._malformed("连接器服务响应不是合法 JSON") from None
        if not isinstance(payload, dict) or payload.get("ok") not in (True, False):
            raise self._malformed("连接器服务响应形状异常（缺少 ok）")
        if payload.get("ok") is True:
            result = payload.get("result")
            if not isinstance(result, dict):
                raise self._malformed("连接器服务响应缺少 result 对象")
            return result
        error = payload.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise self._malformed("连接器服务返回了非结构化错误")
        raise BridgeError(error)

    # —— 错误构造（error dict 形状与 A3 一致） ——

    def _unreachable(self, message: str) -> BridgeError:
        return BridgeError({
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": message,
            "retryable": False,
            "details": {"platform": self._platform, "endpoint": self._base_url},
        })

    def _timeout_error(self, url: str) -> BridgeError:
        return BridgeError({
            "code": "NETWORK_ERROR",
            "message": f"连接器服务请求超时（{url}，{self._timeout:.0f}s）",
            "retryable": True,
            "details": {"platform": self._platform, "endpoint": self._base_url},
        })

    def _malformed(self, message: str) -> BridgeError:
        return BridgeError({
            "code": "NETWORK_ERROR",
            "message": message,
            "retryable": True,
            "details": {"platform": self._platform, "endpoint": self._base_url},
        })


class ZyktBridgeAdapter(XzzdBridgeAdapter):
    """智云课堂独立会话通道：目录、场次详情和已有字幕；暂不批量下载视频。"""

    def __init__(self, platform: str = "zykt", connection_id: str = "",
                 base_url: Optional[str] = None, timeout: Optional[float] = None) -> None:
        super().__init__(platform=platform, connection_id=connection_id,
                         base_url=base_url or os.environ.get("ZYKT_BRIDGE_URL") or "http://127.0.0.1:8766",
                         timeout=timeout)

    def get_transcript(self, sub_id: str) -> dict[str, Any]:
        return self._request("transcript", {**self._base_body(), "sub_id": sub_id}, timeout=self._timeout)


if __name__ == "__main__":  # 自检：打印有效配置（不含任何会话物料）
    _adapter = XzzdBridgeAdapter()
    print(json.dumps({"bridge_url": _adapter._base_url,  # noqa: SLF001
                      "timeout_s": _adapter._timeout}, ensure_ascii=False))  # noqa: SLF001
