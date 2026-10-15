"""实际 run_serve 启动路径：替代浏览器，不登录平台；真实 HTTP 服务和日志过滤器。"""
import json
from types import SimpleNamespace
import urllib.request

import pytest
from connectors.browser import session_runner as sr


@pytest.mark.parametrize("platform", ["xzzd", "zykt"])
def test_both_platforms_log_ready_and_serve_health_without_crashing(monkeypatch, tmp_path, platform):
    servers = []
    browsers = []
    original = sr.start_http_server

    def start_server(*args):
        server = original(*args)
        servers.append(server)
        return server

    class Browser:
        def __init__(self, **kwargs):
            self.stopped = False
            browsers.append(self)
        def start(self):
            pass
        def cookies_snapshot(self):
            return []
        def pump_once(self):
            with urllib.request.urlopen(f"http://127.0.0.1:{servers[0].server_port}/health", timeout=2) as response:
                result = json.load(response)["result"]
            assert result["platform"] == platform
            assert not result["session_available"]
            raise KeyboardInterrupt
        def stop(self):
            self.stopped = True

    monkeypatch.setattr(sr, "BrowserSession", Browser)
    monkeypatch.setattr(sr, "start_http_server", start_server)
    args = SimpleNamespace(course_map=[], platform=platform, b_api=None,
                           profile_dir=tmp_path / "profile", channel="msedge", rate_limit=.5,
                           download_dir=tmp_path / "downloads", max_download_bytes=1000,
                           host="127.0.0.1", port=0)
    try:
        assert sr.run_serve(args) == 0
        assert browsers[0].stopped
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
