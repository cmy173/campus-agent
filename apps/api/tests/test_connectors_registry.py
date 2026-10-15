"""单测：平台连接器默认注册（app.connectors_registry）。

背景（本测试防的回归）：协调者 2026-10-11 实测发现——API 进程**既不注册适配器、
也不读环境变量默认值**时，`POST /connections/{id}/check` 与 `/sync` 返回 502
`DEPENDENCY_UNAVAILABLE`，表现为"功能坏了"而非"配置漏了"。本模块把默认注册做进
启动流程，使默认部署开箱可用。

覆盖：
  - 默认注册成功且幂等，平台 → "模块:属性" 可机械核对；
  - xzzd/zykt 两个解析面（sync + check）都可用；
  - zykt 使用独立桥接通道，真实登录状态不被旧占位实现覆盖；
  - 服务缺失、操作未实现与认证过期的可重试性和恢复提示正确；
  - 字幕桥接仅传业务标识；
  - `STUDY_SYNC_ADAPTERS` 环境变量可覆盖默认注册。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app import connectors_registry as cr  # noqa: E402
from app import sync_engine  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    """用例间隔离：注册表是进程级全局，逐例清空后重注册。"""
    sync_engine._REGISTRY.clear()  # noqa: SLF001
    sync_engine._CHECK_REGISTRY.clear()  # noqa: SLF001
    cr.REGISTERED.clear()
    cr.PROTOCOL_FACTORIES.clear()
    yield
    sync_engine._REGISTRY.clear()  # noqa: SLF001
    sync_engine._CHECK_REGISTRY.clear()  # noqa: SLF001
    cr.REGISTERED.clear()
    cr.PROTOCOL_FACTORIES.clear()


def test_default_adapters_registered_and_idempotent() -> None:
    first = cr.register_default_adapters()
    again = cr.register_default_adapters()
    assert first == again, "重复注册结果必须一致（幂等）"
    assert first["xzzd"] == "connectors.bridge:XzzdBridgeAdapter"
    assert first["zykt"] == "connectors.bridge:ZyktBridgeAdapter"


def test_both_platforms_resolve_on_both_faces() -> None:
    cr.register_default_adapters()
    for platform in ("xzzd", "zykt"):
        assert callable(sync_engine.resolve_sync_adapter(platform))
        assert callable(sync_engine.resolve_check_adapter(platform))


def test_zykt_routes_check_to_its_own_service(monkeypatch) -> None:
    import connectors.bridge as bridge
    calls = []
    def request(self, op, body, *, timeout):
        calls.append((self._base_url, op, body))
        return {"platform": "zykt", "state": "auth_required", "checked_at": "2026-10-11T00:00:00Z",
                "action": {"type": "user_login", "entry_url": "https://classroom.zju.edu.cn/", "message": "登录"}, "detail": "尚未登录"}
    monkeypatch.setattr(bridge.ZyktBridgeAdapter, "_request", request)
    cr.register_default_adapters()
    result = sync_engine.resolve_check_adapter("zykt")({"platform": "zykt", "connection_id": "c"})
    assert result["state"] == "auth_required"
    assert calls[0][0] == "http://127.0.0.1:8766"
    assert calls[0][2]["platform"] == "zykt"


def test_dependency_failure_is_service_unavailable() -> None:
    from app.models import Connection
    conn = Connection(id="c", platform="zykt", status="ok")
    sync_engine.record_check_failure(conn, sync_engine.AdapterError("DEPENDENCY_UNAVAILABLE", "本地服务未启动", retryable=False))
    assert conn.status == "service_unavailable"
    assert conn.last_error["retryable"] is False
    assert conn.recovery_action is None


def test_unsupported_is_not_retryable_network_error() -> None:
    from app.models import Connection
    conn = Connection(id="c", platform="zykt", status="ok")
    sync_engine.apply_connection_state(conn, {"platform": "zykt", "state": "unsupported", "checked_at": "2026-10-11T00:00:00Z", "action": None, "detail": "未实现"})
    assert conn.status == "unsupported"
    assert conn.last_error["code"] == "NOT_IMPLEMENTED"
    assert conn.last_error["retryable"] is False


def test_zykt_transcript_uses_business_id_only(monkeypatch) -> None:
    import connectors.bridge as bridge
    calls = []
    def request(self, op, body, *, timeout):
        calls.append((op, body))
        return {"segments": []}
    monkeypatch.setattr(bridge.ZyktBridgeAdapter, "_request", request)
    bridge.ZyktBridgeAdapter(connection_id="c").get_transcript("sub-1")
    assert calls == [("transcript", {"platform": "zykt", "connection_id": "c", "sub_id": "sub-1"})]


def test_env_override_replaces_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """STUDY_SYNC_ADAPTERS 显式配置必须能覆盖默认注册。"""
    monkeypatch.setattr(
        sync_engine.config, "SYNC_ADAPTERS",
        {"zykt": "connectors.bridge:XzzdBridgeAdapter"},
        raising=True,
    )
    registered = cr.register_default_adapters()
    assert registered["zykt"] == "connectors.bridge:XzzdBridgeAdapter", "环境变量应覆盖默认"
    assert registered["xzzd"] == "connectors.bridge:XzzdBridgeAdapter", "未覆盖的平台保持默认"


def test_xzzd_bridge_carries_no_session_material_and_constructs_offline() -> None:
    """红线 + 离线可构造：桥接源码无会话物料关键词；无会话服务时**构造**必须成功。

    注意：此处只断言构造与入口解析（不调用 check_connection）——
    真正调用会去连本机 8765 的受控浏览器会话服务，属联调/真实链路，不放在单测里。
    """
    import connectors.bridge as bridge  # noqa: PLC0415

    source = Path(bridge.__file__).read_text(encoding="utf-8").lower()
    for banned in ("cookie=", "set-cookie", "authorization:", "bearer ", "token="):
        assert banned not in source, f"桥接源码出现会话物料关键词：{banned}"

    adapter = bridge.XzzdBridgeAdapter(platform="xzzd", connection_id="conn_x")
    assert adapter._base_url.startswith("http://127.0.0.1")  # noqa: SLF001 —— 仅回环
    assert not hasattr(adapter, "_session"), "桥接不得持有会话对象"

    cr.register_default_adapters()
    assert callable(sync_engine.resolve_check_adapter("xzzd"))
    assert callable(sync_engine.resolve_sync_adapter("xzzd"))
