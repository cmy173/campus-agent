#!/usr/bin/env python3
"""A-复习卷单测：取页传输与端到端（真实本地 HTTP 服务器；不触外网）。

覆盖 01-A §5：**第三方请求不带学校凭据**（无 Cookie/Authorization、跨主机重定向丢弃）、
真实重定向跟随、单页大小截断、超时映射、SSRF 防护（含 allow_private 例外）、
UTF-8 中文往返、题库 API POST 真实链路。

运行：python -m unittest connectors.tests.test_questionbanks_transport -v
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.questionbanks import (  # noqa: E402
    QuestionBankError,
    RequestsPageTransport,
    SearchRequest,
    search_candidates,
    strip_sensitive_headers,
)

SMALL_PAGE = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>t</title></head><body>"
    "<div><p>1. 计算 2+3 的值。（测试页）</p><p>A. 4</p><p>B. 5</p><p>答案：B</p></div>"
    "</body></html>"
).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    routes: dict = {}
    recorded: list = []

    def log_message(self, *args):  # 静音
        pass

    def _serve(self, method: str) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.recorded.append({
            "method": method, "path": parsed.path, "query": parsed.query,
            "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body,
        })
        route = self.routes.get(parsed.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, headers, payload, delay = route
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # 客户端超时/中断属测试预期（如 timeout 用例）

    def do_GET(self):  # noqa: N802
        self._serve("GET")

    def do_POST(self):  # noqa: N802
        self._serve("POST")


class _QuietServer(ThreadingHTTPServer):
    """测试服务器：客户端超时/中断（预期场景）不打印堆栈；其他错误照常上报。"""

    def handle_error(self, request, client_address):  # type: ignore[override]
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class LocalServer:
    def __init__(self) -> None:
        self.routes: dict = {}
        self.recorded: list = []
        handler = type("RecordingHandler", (_Handler,),
                       {"routes": self.routes, "recorded": self.recorded})
        self._server = _QuietServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def route(self, path: str, *, status: int = 200, body: bytes = b"",
              headers: dict | None = None, delay: float = 0.0) -> None:
        self.routes[path] = (status, headers or {}, body, delay)

    def headers_for(self, path: str) -> list[dict]:
        return [r["headers"] for r in self.recorded if r["path"] == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class TestCredentialIsolation(unittest.TestCase):
    """红线：第三方请求绝不携带学校会话/凭据。"""

    def setUp(self) -> None:
        self.server = LocalServer()
        self.server.route("/page", body=SMALL_PAGE)

    def tearDown(self) -> None:
        self.server.close()

    def test_fresh_session_sends_no_cookie_or_auth(self):
        transport = RequestsPageTransport()
        resp = transport.request("GET", self.server.base + "/page")
        self.assertEqual(200, resp.status_code)
        sent = self.server.headers_for("/page")[0]
        self.assertNotIn("cookie", sent)
        self.assertNotIn("authorization", sent)

    def test_injected_sensitive_headers_are_stripped(self):
        transport = RequestsPageTransport()
        transport.request("GET", self.server.base + "/page", headers={
            "Cookie": "SESSION=school-secret",
            "Authorization": "Bearer school-token",
            "X-Custom": "kept",
        })
        sent = self.server.headers_for("/page")[0]
        self.assertNotIn("cookie", sent)          # 防御性剥离，即使调用方误传
        self.assertNotIn("authorization", sent)
        self.assertEqual("kept", sent.get("x-custom"))

    def test_strip_sensitive_headers_unit(self):
        self.assertIsNone(strip_sensitive_headers({"Cookie": "x"}))
        self.assertIsNone(strip_sensitive_headers({}))
        self.assertEqual({"A": "1"}, strip_sensitive_headers({"A": "1", "Cookie": "x"}))

    def test_cross_host_redirect_drops_custom_headers_end_to_end(self):
        """跨主机重定向不携带自定义头（默认传输已剥离 Authorization/Cookie，故用 X-Bank-Key 验证）。"""
        mirror = LocalServer()
        try:
            mirror.route("/search", body=json.dumps({"results": []}).encode("utf-8"))
            self.server.route("/search", status=302,
                              headers={"Location": mirror.base + "/search"},
                              body=b"")
            transport = RequestsPageTransport()
            request = SearchRequest(query="线性代数", keywords=["线性代数"],
                                    sources=[{"source_id": "s", "kind": "search_service",
                                              "base_url": self.server.base,
                                              "headers": {"X-Bank-Key": "user-key"}}])
            outcome = search_candidates(request, transport, sleep=lambda _s: None,
                                        allow_private_addresses=True)
            self.assertEqual("ok", outcome.status)
            origin_sent = self.server.headers_for("/search")[0]
            mirror_sent = mirror.headers_for("/search")[0]
            self.assertEqual("user-key", origin_sent.get("x-bank-key"))
            self.assertNotIn("x-bank-key", mirror_sent)  # 自定义头不跨主机扩散
        finally:
            mirror.close()


class TestRealTransportBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.server = LocalServer()

    def tearDown(self) -> None:
        self.server.close()

    def test_utf8_chinese_round_trip(self):
        self.server.route("/page", body="<html><body><p>中文页码：第一章 导数</p></body></html>".encode("utf-8"))
        transport = RequestsPageTransport()
        resp = transport.request("GET", self.server.base + "/page")
        self.assertIn("第一章 导数", resp.text)

    def test_transport_does_not_follow_redirects(self):
        self.server.route("/r", status=302, headers={"Location": "/page"}, body=b"")
        self.server.route("/page", body=SMALL_PAGE)
        transport = RequestsPageTransport()
        resp = transport.request("GET", self.server.base + "/r")
        self.assertEqual(302, resp.status_code)  # 实现契约：不自动跟随
        self.assertEqual("/page", resp.header("location"))

    def test_max_bytes_truncation(self):
        big = ("OK" * 100_000).encode("ascii")
        self.server.route("/big", body=big)
        transport = RequestsPageTransport()
        resp = transport.request("GET", self.server.base + "/big", max_bytes=4096)
        self.assertTrue(resp.truncated)
        self.assertLessEqual(len(resp.text), 4096)

    def test_timeout_maps_to_questionbank_error(self):
        self.server.route("/slow", body=b"x", delay=1.2)
        transport = RequestsPageTransport(timeout=0.3)
        with self.assertRaises(QuestionBankError) as ctx:
            transport.request("GET", self.server.base + "/slow")
        self.assertEqual("NETWORK_ERROR", ctx.exception.code)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual("timeout", ctx.exception.details["reason"])


class TestEngineEndToEnd(unittest.TestCase):
    """引擎 + 真实传输：重定向跟随、截断告警、SSRF、题库 API POST 链路。"""

    def setUp(self) -> None:
        self.server = LocalServer()
        self.transport = RequestsPageTransport()

    def tearDown(self) -> None:
        self.server.close()

    def test_same_host_redirect_followed_and_extracted(self):
        self.server.route("/entry", status=302, headers={"Location": "/real"}, body=b"")
        self.server.route("/real", body=SMALL_PAGE)
        request = SearchRequest(
            query="计算", keywords=["计算"],
            sources=[{"source_id": "u", "kind": "url_import", "urls": [self.server.base + "/entry"]}])
        outcome = search_candidates(request, self.transport, sleep=lambda _s: None,
                                    allow_private_addresses=True)
        self.assertEqual("ok", outcome.status)
        self.assertEqual(1, len(outcome.candidates))
        self.assertEqual("B", outcome.candidates[0]["answer"])
        self.assertEqual(["/entry", "/real"], [r["path"] for r in self.server.recorded])

    def test_page_truncation_warning_end_to_end(self):
        filler = "<p>" + ("填" * 2000) + "</p>"
        body = ("<html><body>" + filler + "<div><p>1. 计算 1+1。（截断页）</p>"
                "<p>A. 1</p><p>B. 2</p><p>答案：B</p></div></body></html>").encode("utf-8")
        self.server.route("/big", body=body)
        request = SearchRequest(
            query="计算", keywords=["计算"], max_page_bytes=600, cache_ttl_s=0,
            sources=[{"source_id": "u", "kind": "url_import", "urls": [self.server.base + "/big"]}])
        outcome = search_candidates(request, self.transport, sleep=lambda _s: None,
                                    allow_private_addresses=True)
        self.assertIn("PAGE_TRUNCATED", [w["code"] for w in outcome.warnings])

    def test_ssrf_blocks_loopback_without_allow_private(self):
        request = SearchRequest(
            query="x", keywords=["x"],
            sources=[{"source_id": "u", "kind": "url_import", "urls": [self.server.base + "/page"]}])
        outcome = search_candidates(request, self.transport, sleep=lambda _s: None)
        self.assertEqual("failed", outcome.status)
        self.assertEqual([], self.server.recorded)  # 请求未发出
        self.assertTrue(any("阻断" in w["message"] for w in outcome.warnings))

    def test_bank_api_post_round_trip(self):
        payload = json.dumps({"items": [
            {"stem": "求 1+1 的值。", "type": "calculation", "options": [],
             "answer": "2", "knowledge_point": "算术"}]}).encode("utf-8")
        self.server.route("/api/search", body=payload)
        request = SearchRequest(
            query="算术", keywords=["算术"],
            sources=[{"source_id": "b", "kind": "bank_api",
                      "base_url": self.server.base, "search_path": "/api/search"}])
        outcome = search_candidates(request, self.transport, sleep=lambda _s: None,
                                    allow_private_addresses=True)
        self.assertEqual("ok", outcome.status)
        self.assertEqual("2", outcome.candidates[0]["answer"])
        sent = self.server.recorded[0]
        self.assertEqual("POST", sent["method"])
        body = json.loads(sent["body"].decode("utf-8"))
        self.assertEqual("算术", body["query"])
        self.assertEqual(["算术"], body["keywords"])
        self.assertNotIn("cookie", sent["headers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
