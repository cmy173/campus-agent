#!/usr/bin/env python3
"""契约测试：传输层下载通道（RequestsTransport.download，A22）。

用本机 stdlib HTTP 服务构造真实数据流（不触外网）：验证原子落盘（.part → 改名）、
sha256/尺寸计算、非 2xx → DownloadError(带状态码)、中途超护栏 → DownloadTooLarge 且
清理半成品、绝对 URL 不复用 base_url、重复写目标文件可覆盖。

运行：
  python connectors/tests/test_transport.py
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from connectors.adapters.transport import (  # noqa: E402
    DownloadError,
    DownloadTooLarge,
    RequestsTransport,
)


class _Handler(BaseHTTPRequestHandler):
    payload = b""
    status = 200

    def do_GET(self):  # noqa: N802
        body = type(self).payload
        status = type(self).status
        self.send_response(status)
        self.send_header("Content-Length", str(len(body) if status == 200 else 0))
        self.end_headers()
        if status == 200:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # 客户端中途中止（护栏路径）
                pass

    def log_message(self, fmt, *args):  # 静默（测试输出整洁）
        pass


class TestRequestsTransportDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.transport = RequestsTransport(
            f"http://127.0.0.1:{self.port}", requests.Session(), timeout=5.0
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _url(self) -> str:
        return f"http://127.0.0.1:{self.port}/download/file"

    def test_ok_hash_size_and_atomic_write(self):
        payload = bytes(range(256)) * 40  # 10240B
        _Handler.payload, _Handler.status = payload, 200
        dest = Path(self.tmp.name) / "sub" / "file.zip"
        dest.parent.mkdir()
        dest.write_bytes(b"stale-partial-from-previous-run")  # 旧内容应被整体替换
        size, digest = self.transport.download(self._url(), dest, max_bytes=1024 * 1024)
        self.assertEqual(len(payload), size)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
        self.assertEqual(payload, dest.read_bytes())
        self.assertFalse((Path(self.tmp.name) / "sub" / "file.zip.part").exists())

    def test_non_200_raises_with_status_and_no_dest(self):
        _Handler.payload, _Handler.status = b"denied", 403
        dest = Path(self.tmp.name) / "file.bin"
        with self.assertRaises(DownloadError) as ctx:
            self.transport.download(self._url(), dest)
        self.assertEqual(403, ctx.exception.status_code)
        self.assertFalse(dest.exists())
        self.assertFalse((Path(self.tmp.name) / "file.bin.part").exists())

    def test_over_guardrail_aborts_and_cleans(self):
        _Handler.payload, _Handler.status = b"x" * 100_000, 200
        dest = Path(self.tmp.name) / "big.bin"
        with self.assertRaises(DownloadTooLarge) as ctx:
            self.transport.download(self._url(), dest, max_bytes=10_000, chunk_size=4096)
        self.assertEqual(10_000, ctx.exception.max_bytes)
        self.assertFalse(dest.exists())
        self.assertFalse((Path(self.tmp.name) / "big.bin.part").exists())

    def test_absolute_url_not_prefixed_by_base(self):
        # base_url 指向未监听端口；download 的绝对 URL 指向本服务 → 成功（证未复用 base 前缀）
        transport = RequestsTransport("http://127.0.0.1:1", requests.Session(), timeout=5.0)
        _Handler.payload, _Handler.status = b"ok-bytes", 200
        dest = Path(self.tmp.name) / "abs.bin"
        size, _ = transport.download(self._url(), dest)
        self.assertEqual(8, size)
        self.assertEqual(b"ok-bytes", dest.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
