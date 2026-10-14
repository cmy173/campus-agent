"""平台与回环连接的代理隔离；全部使用合成会话或本地 HTTP。"""
from pathlib import Path

from connectors.browser.session_runner import BrowserSession
from connectors.bridge import XzzdBridgeAdapter
from connectors.tests.test_bridge import start_service


def test_local_bridge_bypasses_system_proxy(monkeypatch):
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"http": "http://127.0.0.1:1"})
    monkeypatch.setattr("urllib.request.proxy_bypass", lambda host: False)
    server, url, _ = start_service(with_session=True)
    try:
        assert XzzdBridgeAdapter(base_url=url, timeout=2).check_connection()["state"] == "ok"
    finally:
        server.shutdown()
        server.server_close()


def test_platform_session_defaults_to_direct_with_tls_verification(monkeypatch):
    monkeypatch.delenv("STUDY_PLATFORM_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    browser = BrowserSession(profile_dir=Path("unused"))
    browser._snapshot = [{"name": "SESSION", "value": "synthetic", "domain": ".zju.edu.cn"}]
    with browser.session() as session:
        assert session.trust_env is False
        assert session.verify is True
        settings = session.merge_environment_settings("https://courses.zju.edu.cn/", {}, None, None, None)
        assert not settings["proxies"]


def test_explicit_platform_proxy_is_respected(monkeypatch):
    monkeypatch.setenv("STUDY_PLATFORM_PROXY", "http://127.0.0.1:9000")
    browser = BrowserSession(profile_dir=Path("unused"))
    browser._snapshot = [{"name": "SESSION", "value": "synthetic", "domain": ".zju.edu.cn"}]
    with browser.session() as session:
        assert session.proxies["https"] == "http://127.0.0.1:9000"
        assert session.verify is True
