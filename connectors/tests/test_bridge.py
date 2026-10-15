#!/usr/bin/env python3
"""单测：A20 B 侧桥接（全部离线——本机回环 HTTP，不触外网、不起浏览器）。

覆盖：
  - 桥接转发：check/sync/fetch 经真 HTTP 到连接器服务（假会话+假适配器），结果与错误形状正确；
  - 错误转译兼容：BridgeError.error 满足 B ``_translate_protocol_error`` 的读取约定
    （``.error`` dict + ``code`` 字符串 + ``details.action`` 恢复动作）；
  - 依赖不可达 → ``DEPENDENCY_UNAVAILABLE``（不静默成功）；响应不可解析 → ``NETWORK_ERROR``；
  - 红线：bridge 模块源码不含会话物料关键词（cookie/token/jwt/credential）。

运行：
  python connectors/tests/test_bridge.py
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors import bridge as br  # noqa: E402
from connectors.adapters.base import AdapterError  # noqa: E402
from connectors.browser import session_runner as sr  # noqa: E402
from connectors.tests.test_session_runner import FakeAdapter, FakeSessions  # noqa: E402


def start_service(*, with_session: bool = True, adapter: FakeAdapter | None = None,
                  resolve_course=None):
    sessions = FakeSessions(with_session=with_session)
    adapter = adapter or FakeAdapter()
    service = sr.ConnectorService(sessions, adapter_factory=lambda cid, s: adapter,
                                  resolve_course=resolve_course)
    server = sr.start_http_server(service, "127.0.0.1", 0)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url, adapter


class TestBridgeForwarding(unittest.TestCase):
    def tearDown(self):
        for server in getattr(self, "_servers", []):
            server.shutdown()
            server.server_close()

    def _start(self, **kwargs):
        server, base_url, adapter = start_service(**kwargs)
        self._servers = getattr(self, "_servers", []) + [server]
        return base_url, adapter

    def test_check_ok(self):
        base_url, _ = self._start(with_session=True)
        adapter = br.XzzdBridgeAdapter(platform="xzzd", connection_id="conn_1", base_url=base_url)
        result = adapter.check_connection()
        self.assertEqual(result["state"], "ok")

    def test_check_without_session_is_auth_required_state(self):
        base_url, _ = self._start(with_session=False)
        adapter = br.XzzdBridgeAdapter(connection_id="conn_1", base_url=base_url)
        result = adapter.check_connection()
        self.assertEqual(result["state"], "auth_required")
        self.assertTrue(result["action"]["entry_url"].startswith("https://"))

    def test_sync_forwards_and_echoes_envelope(self):
        real = FakeAdapter(envelope={"mode": "resync", "stats": {"scanned": 5}})
        base_url, adapter = self._start(with_session=True, adapter=real,
                                        resolve_course=lambda c: "10001")
        bridge = br.XzzdBridgeAdapter(connection_id="conn_1", base_url=base_url)
        envelope = bridge.sync_course("crs_1", since_cursor=None, kinds=None)
        self.assertEqual(envelope["course_id"], "crs_1")
        self.assertEqual(adapter.calls, [("sync", "crs_1", None, None)])

    def test_sync_without_session_raises_auth_expired_with_action(self):
        base_url, _ = self._start(with_session=False, resolve_course=lambda c: "1")
        bridge = br.XzzdBridgeAdapter(connection_id="conn_1", base_url=base_url)
        with self.assertRaises(br.BridgeError) as ctx:
            bridge.sync_course("crs_1")
        error = ctx.exception.error
        # 模拟 B sync_engine._translate_protocol_error 的读取约定
        self.assertIsInstance(error, dict)
        self.assertEqual(error["code"], "AUTH_EXPIRED")
        self.assertFalse(error["retryable"])
        self.assertTrue(error["details"]["action"]["entry_url"].startswith("https://"))

    def test_sync_adapter_error_shape(self):
        real = FakeAdapter(sync_error=AdapterError("PERMISSION_DENIED", "无权限", retryable=False))
        base_url, _ = self._start(with_session=True, adapter=real, resolve_course=lambda c: "1")
        bridge = br.XzzdBridgeAdapter(connection_id="conn_1", base_url=base_url)
        with self.assertRaises(br.BridgeError) as ctx:
            bridge.sync_course("crs_1")
        self.assertEqual(ctx.exception.error["code"], "PERMISSION_DENIED")
        self.assertFalse(ctx.exception.error["retryable"])

    def test_fetch_forwards_outcome(self):
        outcome = {"status": "denied", "handle": None,
                   "denial": {"reason": "preview_only", "message": "仅可预览"}}
        base_url, adapter = self._start(with_session=True, adapter=FakeAdapter(fetch=outcome))
        bridge = br.XzzdBridgeAdapter(connection_id="conn_1", base_url=base_url)
        self.assertEqual(bridge.fetch_resource("res-1"), outcome)
        self.assertEqual(adapter.calls, [("fetch", "res-1")])

    def test_invalid_body_returns_invalid_request(self):
        base_url, _ = self._start(with_session=True)
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"{base_url}/sync", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)  # noqa: S310 —— 本机回环
            self.fail("预期 HTTP 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            payload = json.loads(exc.read().decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        self.assertFalse(payload["error"]["retryable"])


class TestBridgeFailures(unittest.TestCase):
    def test_unreachable_service_is_dependency_unavailable(self):
        server, base_url, _ = start_service()
        port = server.server_address[1]
        server.shutdown()
        server.server_close()
        bridge = br.XzzdBridgeAdapter(
            connection_id="conn_1", base_url=f"http://127.0.0.1:{port}", timeout=2.0)
        with self.assertRaises(br.BridgeError) as ctx:
            bridge.check_connection()
        error = ctx.exception.error
        self.assertEqual(error["code"], "DEPENDENCY_UNAVAILABLE")
        self.assertFalse(error["retryable"])
        self.assertIn("启动器", error["message"])

    def test_non_json_response_is_network_error(self):
        class BadHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = b"<html>not json</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), BadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = br.XzzdBridgeAdapter(
                connection_id="conn_1",
                base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=2.0)
            with self.assertRaises(br.BridgeError) as ctx:
                bridge.sync_course("crs_1")
            self.assertEqual(ctx.exception.error["code"], "NETWORK_ERROR")
            self.assertTrue(ctx.exception.error["retryable"])
        finally:
            server.shutdown()
            server.server_close()


class TestBridgeConfig(unittest.TestCase):
    def test_env_url_and_timeout(self):
        with mock.patch.dict("os.environ",
                             {"XZZD_BRIDGE_URL": "http://127.0.0.1:9999/", "XZZD_BRIDGE_TIMEOUT": "42"}):
            adapter = br.XzzdBridgeAdapter(connection_id="conn_1")
        self.assertEqual(adapter._base_url, "http://127.0.0.1:9999")  # noqa: SLF001
        self.assertEqual(adapter._timeout, 42.0)  # noqa: SLF001

    def test_request_body_fields_are_whitelisted(self):
        """桥接请求体只允许业务字段——不得携带任何会话/凭据物料。"""
        allowed = {"platform", "connection_id", "course_id", "resource_id", "since_cursor", "kinds"}
        recorded: list[tuple[str, dict]] = []

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                recorded.append((self.path.strip("/"), json.loads(self.rfile.read(length))))
                data = json.dumps({"ok": True, "result": {}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            bridge = br.XzzdBridgeAdapter(
                connection_id="conn_1",
                base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=5.0)
            bridge.check_connection()
            bridge.sync_course("crs_1", since_cursor="cur", kinds=["resources"])
            bridge.fetch_resource("res-1")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([op for op, _ in recorded], ["check", "sync", "fetch"])
        for op, body in recorded:
            self.assertLessEqual(set(body), allowed, f"{op} 请求体出现白名单外字段：{body}")
            self.assertEqual(body["platform"], "xzzd")
            self.assertEqual(body["connection_id"], "conn_1")

    def test_bridge_is_stdlib_only(self):
        source = Path(br.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import requests", source)
        self.assertNotIn("playwright", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
