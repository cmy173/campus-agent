"""B14 登录/连接建立对齐测试（CR002 结案口径）。

覆盖：① entry_url 服务端常量与连接体一致（https-only，CR001 DECISION-3）；② auth_required
连接的 recovery_action 文案为连接器窗口口径（B 侧默认 + 契约样例对齐）；③ recovery_action
sanitize 边界（仅 user_login + https；适配器文案透传；空文案回退默认）；④ 登录失效错误通道
（409 AUTH_EXPIRED，retryable=false，附 recovery_action 指引）且响应不含任何会话/凭据物料。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.platforms import ENTRY_URLS
from app.sync_engine import recovery_action_for, sanitize_recovery_action

XZZD_ENTRY = "https://course.zju.edu.cn/ua/login"
ZYKT_ENTRY = "https://classroom.zju.edu.cn/"
# CR002 §3 定稿文案（连接器窗口口径）
DEFAULT_GUIDANCE = "登录已失效，请在连接器窗口完成登录后重试"
AUTH_CONNECTION = "conn_8c1de"  # 种子：zykt，auth_required

_FORBIDDEN_KEY_PARTS = ("cookie", "token", "session", "password", "credential", "authorization", "secret")


def _assert_no_session_material(value: object) -> None:
    """递归扫描响应键名：不得出现会话/凭据类字段。"""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            assert not any(part in lowered for part in _FORBIDDEN_KEY_PARTS), f"响应含会话/凭据字段：{key}"
            _assert_no_session_material(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_session_material(item)


def test_connection_entry_urls_match_server_constants(client: TestClient) -> None:
    assert ENTRY_URLS == {"xzzd": XZZD_ENTRY, "zykt": ZYKT_ENTRY}
    items = client.get("/api/v1/connections").json()["items"]
    assert items
    for conn in items:
        assert conn["entry_url"] == ENTRY_URLS[conn["platform"]]
        assert conn["entry_url"].startswith("https://")
    detail = client.get(f"/api/v1/connections/{AUTH_CONNECTION}").json()
    assert detail["entry_url"] == ZYKT_ENTRY


def test_auth_required_connection_guidance_aligned(client: TestClient) -> None:
    detail = client.get(f"/api/v1/connections/{AUTH_CONNECTION}").json()
    assert detail["status"] == "auth_required"
    assert detail["recovery_action"] == {
        "type": "user_login",
        "entry_url": ZYKT_ENTRY,
        "message": DEFAULT_GUIDANCE,
    }
    assert detail["last_error"]["details"]["action"]["entry_url"] == ZYKT_ENTRY
    _assert_no_session_material(detail)


def test_recovery_action_defaults_and_sanitize() -> None:
    assert recovery_action_for("xzzd") == {
        "type": "user_login",
        "entry_url": XZZD_ENTRY,
        "message": DEFAULT_GUIDANCE,
    }
    assert recovery_action_for("unknown_platform") is None

    # 仅接受 user_login + https；其余一律拒绝（不接受任意 URL）
    assert sanitize_recovery_action({"type": "user_login", "entry_url": "http://insecure.example.edu/"}) is None
    assert sanitize_recovery_action({"type": "open_url", "entry_url": "https://example.edu/"}) is None
    assert sanitize_recovery_action("https://example.edu/") is None

    # 适配器文案原样透传；空/缺文案回退连接器窗口口径
    kept = sanitize_recovery_action({"type": "user_login", "entry_url": ZYKT_ENTRY, "message": "适配器文案"})
    assert kept == {"type": "user_login", "entry_url": ZYKT_ENTRY, "message": "适配器文案"}
    fallback = sanitize_recovery_action({"type": "user_login", "entry_url": ZYKT_ENTRY, "message": ""})
    assert fallback["message"] == DEFAULT_GUIDANCE


def test_auth_expired_channel_has_guidance_without_session_material(client: TestClient) -> None:
    resp = client.post(f"/api/v1/connections/{AUTH_CONNECTION}/sync")
    assert resp.status_code == 409
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    body = resp.json()
    _assert_no_session_material(body)
    error = body["error"]
    assert error["code"] == "AUTH_EXPIRED"
    assert error["retryable"] is False
    assert error["request_id"]
    assert set(error["details"]) == {"platform", "action"}  # 与契约样例口径一致
    assert error["details"]["platform"] == "zykt"
    assert error["details"]["action"] == {
        "type": "user_login",
        "entry_url": ZYKT_ENTRY,
        "message": DEFAULT_GUIDANCE,
    }
