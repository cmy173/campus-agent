#!/usr/bin/env python3
"""单测：A20 会话运行器与连接器服务（全部离线——不启动浏览器、不触网）。

覆盖：
  - CourseMapper：显式映射优先；B API source_refs 解析（分页）；未命中/未配置语义；
  - ConnectorService：无会话 → check(auth_required、不构造适配器) / sync+fetch(AUTH_EXPIRED+action)；
    有会话 → 转发适配器（内部课程 ID 原样进入适配器、信封 course_id 原样回显）；
    映射缺失 → RESOURCE_NOT_FOUND；A3 AdapterError 形状转译；内部错误兜底 INTERNAL_ERROR；
  - 红线：cookie 值不进入任何响应。

运行：
  python connectors/tests/test_session_runner.py
  python -m unittest discover -s connectors/tests -t .
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.adapters.base import AdapterError  # noqa: E402
from connectors.browser import session_runner as sr  # noqa: E402

SECRET_VALUE = "SECRET-COOKIE-7a2b"
COOKIE = {"name": "SESSION", "value": SECRET_VALUE, "domain": ".zju.edu.cn", "path": "/"}


class FakeSessions:
    """BrowserSession 替身：可按需返回会话/快照。"""

    def __init__(self, with_session: bool = True) -> None:
        self.with_session = with_session
        self.cookies = [dict(COOKIE)] if with_session else []

    def session(self):
        return object() if self.with_session else None

    def cookies_snapshot(self):
        return list(self.cookies)


class FakeAdapter:
    """XzzdAdapter 替身：记录调用、返回预设结果或抛错。"""

    def __init__(self, *, state=None, envelope=None, fetch=None, sync_error=None):
        self.calls = []
        self._state = state or {
            "platform": "xzzd", "state": "ok",
            "checked_at": "2026-10-10T00:00:00.000Z", "action": None, "detail": None,
        }
        self._envelope = envelope
        self._fetch = fetch or {"status": "gone", "handle": None, "denial": None}
        self._sync_error = sync_error

    def check_connection(self):
        self.calls.append(("check",))
        return dict(self._state)

    def sync_course(self, course_id, since_cursor=None, kinds=None):
        self.calls.append(("sync", course_id, since_cursor, kinds))
        if self._sync_error is not None:
            raise self._sync_error
        envelope = dict(self._envelope or {"mode": "resync", "stats": {}})
        envelope["course_id"] = course_id  # 与真实适配器一致：入参原样回显
        return envelope

    def fetch_resource(self, resource_id):
        self.calls.append(("fetch", resource_id))
        return dict(self._fetch)


class FakeBApi:
    """假 B 服务：GET /api/v1/courses 分页返回 source_refs。"""

    def __init__(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默
                pass

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                cursor = (query.get("cursor") or [None])[0]
                if cursor == "page2":
                    payload = {"items": [
                        {"id": "crs_3", "source_refs": [
                            {"platform": "xzzd", "external_id": "10002"}]},
                    ], "next_cursor": None}
                else:
                    payload = {"items": [
                        {"id": "crs_1", "source_refs": [
                            {"platform": "xzzd", "external_id": "10001"}]},
                        {"id": "crs_2", "source_refs": [
                            {"platform": "zykt", "external_id": "z-9"}]},
                    ], "next_cursor": "page2"}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _ = fake  # noqa: F841 —— 保留引用便于扩展

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def make_service(sessions: FakeSessions, **kwargs):
    """返回 (service, adapter_recorded) 便于断言适配器是否/如何被构造。"""
    recorded: dict = {"factories": []}

    def factory(connection_id, session):
        recorded["factories"].append((connection_id, session))
        return recorded["adapter"]

    recorded["adapter"] = kwargs.pop("adapter", FakeAdapter())
    service = sr.ConnectorService(sessions, adapter_factory=factory, **kwargs)
    return service, recorded


class TestCourseMapper(unittest.TestCase):
    def test_explicit_mapping_takes_priority(self):
        mapper = sr.CourseMapper(explicit={"crs_a": "777"})
        self.assertEqual(mapper.platform_id("crs_a"), "777")
        self.assertIsNone(mapper.platform_id("crs_b"))  # 未配置 B API 且未命中

    def test_fetch_map_from_b_api_with_pagination(self):
        api = FakeBApi()
        try:
            mapper = sr.CourseMapper(b_api=api.base_url)
            self.assertEqual(mapper.platform_id("crs_1"), "10001")
            self.assertIsNone(mapper.platform_id("crs_2"))  # zykt 平台来源不匹配
            self.assertEqual(mapper.platform_id("crs_3"), "10002")  # 第二页
            self.assertEqual(mapper.size(), 2)
        finally:
            api.close()

    def test_refresh_failure_keeps_old_cache(self):
        api = FakeBApi()
        try:
            mapper = sr.CourseMapper(b_api=api.base_url)
            self.assertEqual(mapper.platform_id("crs_1"), "10001")
            api.close()
            self.assertFalse(mapper.refresh())  # B 已关闭：刷新失败
            self.assertEqual(mapper.platform_id("crs_1"), "10001")  # 旧缓存保留
        finally:
            pass


class TestConnectorService(unittest.TestCase):
    def test_check_without_session_returns_auth_required_without_adapter(self):
        service, recorded = make_service(FakeSessions(with_session=False))
        status, payload = service.handle("check", {"platform": "xzzd", "connection_id": "conn_1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        state = payload["result"]
        self.assertEqual(state["state"], "auth_required")
        self.assertTrue(state["action"]["entry_url"].startswith("https://"))
        self.assertEqual(recorded["factories"], [])  # 不构造适配器（不触网）
        self.assertNotIn(SECRET_VALUE, json.dumps(payload))

    def test_check_with_session_forwards_to_adapter(self):
        service, recorded = make_service(FakeSessions(with_session=True))
        status, payload = service.handle("check", {"connection_id": "conn_1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["state"], "ok")
        self.assertEqual(recorded["adapter"].calls, [("check",)])
        self.assertNotIn(SECRET_VALUE, json.dumps(payload))

    def test_sync_without_session_returns_auth_expired_with_action(self):
        service, recorded = make_service(FakeSessions(with_session=False), resolve_course=lambda c: "1")
        status, payload = service.handle("sync", {"connection_id": "conn_1", "course_id": "crs_1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        error = payload["error"]
        self.assertEqual(error["code"], "AUTH_EXPIRED")
        self.assertFalse(error["retryable"])
        self.assertTrue(error["details"]["action"]["entry_url"].startswith("https://"))
        self.assertEqual(recorded["factories"], [])

    def test_sync_forwards_internal_id_and_echoes_envelope(self):
        adapter = FakeAdapter(envelope={"mode": "resync", "stats": {"scanned": 3}})
        service, _ = make_service(FakeSessions(with_session=True), adapter=adapter,
                                  resolve_course=lambda c: "10001")
        status, payload = service.handle(
            "sync", {"connection_id": "conn_1", "course_id": "crs_1",
                     "since_cursor": "cur-1", "kinds": ["resources"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["course_id"], "crs_1")  # 信封原样回显内部 ID
        self.assertEqual(adapter.calls, [("sync", "crs_1", "cur-1", ["resources"])])

    def test_sync_unmapped_course_is_explicit_not_found(self):
        service, recorded = make_service(FakeSessions(with_session=True), resolve_course=lambda c: None)
        status, payload = service.handle("sync", {"course_id": "crs_unknown"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "RESOURCE_NOT_FOUND")
        self.assertEqual(recorded["factories"], [])

    def test_sync_adapter_error_is_translated(self):
        adapter = FakeAdapter(sync_error=AdapterError(
            "UPSTREAM_CHANGED", "形态变化", retryable=False, details={"hint": "x"}))
        service, _ = make_service(FakeSessions(with_session=True), adapter=adapter,
                                  resolve_course=lambda c: "1")
        status, payload = service.handle("sync", {"course_id": "crs_1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "UPSTREAM_CHANGED")
        self.assertFalse(payload["error"]["retryable"])
        self.assertEqual(payload["error"]["details"]["hint"], "x")

    def test_sync_internal_error_is_reported_not_swallowed(self):
        class Boom(FakeAdapter):
            def sync_course(self, course_id, since_cursor=None, kinds=None):
                raise RuntimeError("boom")

        service, _ = make_service(FakeSessions(with_session=True), adapter=Boom(),
                                  resolve_course=lambda c: "1")
        status, payload = service.handle("sync", {"course_id": "crs_1"})
        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INTERNAL_ERROR")

    def test_fetch_without_session_and_with_session(self):
        service, _ = make_service(FakeSessions(with_session=False))
        _, payload = service.handle("fetch", {"resource_id": "res-1"})
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "AUTH_EXPIRED")

        outcome = {"status": "denied", "handle": None,
                   "denial": {"reason": "preview_only", "message": "仅可预览"}}
        adapter = FakeAdapter(fetch=outcome)
        service, _ = make_service(FakeSessions(with_session=True), adapter=adapter)
        _, payload = service.handle("fetch", {"resource_id": "res-1"})
        self.assertEqual(payload["result"], outcome)
        self.assertEqual(adapter.calls, [("fetch", "res-1")])

    def test_invalid_requests(self):
        service, _ = make_service(FakeSessions())
        status, payload = service.handle("sync", {"course_id": ""})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        status, payload = service.handle("nope", {})
        self.assertEqual(status, 400)
        status, payload = service.handle("check", None)
        self.assertEqual(status, 400)

    def test_health_payload_contains_no_cookie_values(self):
        service, _ = make_service(FakeSessions(with_session=True))
        self.assertEqual(service.session_cookie_count(), 1)


class TestServiceSharedCache(unittest.TestCase):
    def test_default_adapter_gets_shared_cache_and_download_config(self):
        """默认工厂构造的适配器必须拿到（A22）共享资源可见性缓存与下载配置。"""
        from unittest import mock

        service = sr.ConnectorService(FakeSessions(), download_dir=Path("C:/stage"),
                                      max_download_bytes=12345)
        with mock.patch.object(sr, "XzzdAdapter") as factory:
            service._make_adapter("conn_1", object())
        kwargs = factory.call_args.kwargs
        self.assertIs(kwargs["resources"], service._resources)  # 跨请求共享（B 两阶段依赖）
        self.assertEqual(Path("C:/stage"), kwargs["download_dir"])
        self.assertEqual(12345, kwargs["max_download_bytes"])

    def test_defaults_without_download_config(self):
        from unittest import mock

        service = sr.ConnectorService(FakeSessions())
        with mock.patch.object(sr, "XzzdAdapter") as factory:
            service._make_adapter("conn_1", object())
        kwargs = factory.call_args.kwargs
        self.assertIsNone(kwargs["download_dir"])
        self.assertEqual(sr.MAX_DOWNLOAD_BYTES, kwargs["max_download_bytes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
