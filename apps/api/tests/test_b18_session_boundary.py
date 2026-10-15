"""B18 会话注入边界（安全）：会话物料不得出现在 API 响应、job 结果或日志。

边界模型：会话只在 A 的连接器进程与本机同步调度之间存活。B 侧消费的适配器回传
数据（信封 errors[]、AdapterError.details、ConnectionState.detail）都可能被上游误
带上会话字段——本模块验证两件事：

① 兜底脱敏（sync_engine.scrub_session_material）：键名命中的整键在落库前丢弃，
   API 面与日志中键、值均不出现；
② 各回显面（job 详情/错误、连接详情、同步触发响应、任务列表）递归扫描，
   无任何会话/凭据类键（与 test_login_alignment 的 409 通道断言互补）。

会话物料用唯一哨兵值承载：断言该值不出现在任何响应文本与日志文本中。
"""
from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from app.sync_engine import AdapterError, scrub_session_material
from test_sync_api import (  # noqa: F401 —— sync_env 作为夹具复用
    CONN_ID,
    PLATFORM,
    FakeAdapter,
    _drain_until,
    _envelope,
    _post_sync,
    _resource_item,
    sync_env,
)

FORBIDDEN_PARTS = ("cookie", "token", "session", "password", "credential", "authorization", "secret")
SECRET_VALUE = "COOKIEVALUE-s3cr3t-001"


def _assert_no_session_material(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            assert not any(part in lowered for part in FORBIDDEN_PARTS), f"{path} 含会话/凭据字段：{key}"
            _assert_no_session_material(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _assert_no_session_material(item, f"{path}[{idx}]")


def test_scrub_session_material_recursive() -> None:
    """兜底脱敏：递归剔除会话/凭据键（大小写不敏感），其余键值原样保留。"""
    data = {
        "kind": "resource",
        "Cookie": SECRET_VALUE,
        "nested": {"access_token": SECRET_VALUE, "keep": 7, "list": [{"session_id": "s"}, {"ok": 1}]},
        "action": {"type": "user_login", "entry_url": "https://example.edu/", "message": "请重新登录"},
    }
    assert scrub_session_material(data) == {
        "kind": "resource",
        "nested": {"keep": 7, "list": [{}, {"ok": 1}]},
        "action": {"type": "user_login", "entry_url": "https://example.edu/", "message": "请重新登录"},
    }
    assert scrub_session_material("原样") == "原样"
    assert scrub_session_material([{"credential": "x"}, 3]) == [{}, 3]


def test_envelope_error_details_scrubbed_before_persist(client: TestClient, sync_env: FakeAdapter) -> None:
    """信封 errors[].details 中的会话键在落库前剔除；合法字段保真。"""
    sync_env.result = _envelope(
        errors=[
            {
                "code": "PERMISSION_DENIED",
                "message": "无法获取该资源的元数据",
                "retryable": False,
                "details": {
                    "kind": "resource",
                    "external_id": "testsync-file-9",
                    "cookie": SECRET_VALUE,
                    "nested": {"access_token": SECRET_VALUE, "kept": "v"},
                },
            }
        ]
    )
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "partial_failed"  # 课程级成功、条目级失败
    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert SECRET_VALUE not in resp.text
    entry = resp.json()["errors"][0]
    assert entry["details"]["kind"] == "resource"
    assert entry["details"]["external_id"] == "testsync-file-9"
    assert entry["details"]["nested"] == {"kept": "v"}
    _assert_no_session_material(resp.json())


def test_adapter_error_details_scrubbed_on_job_and_connection(client: TestClient, sync_env: FakeAdapter) -> None:
    """AdapterError.details 会话键剔除；恢复动作（仅 user_login+https）仍完整下发。"""
    sync_env.result = AdapterError(
        "AUTH_EXPIRED",
        "登录已失效",
        retryable=False,
        details={
            "platform": PLATFORM,
            "session_cookies": {"jx": SECRET_VALUE},
            "cookie_count": 3,
            "action": {"type": "user_login", "entry_url": "https://course.example.edu/", "message": "请重新登录"},
        },
    )
    job_id = _post_sync(client).json()["job"]["id"]
    assert _drain_until(job_id) == "failed"

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert SECRET_VALUE not in json.dumps(detail)
    entry = detail["errors"][0]
    assert entry["code"] == "AUTH_EXPIRED" and entry["retryable"] is False
    assert entry["details"]["platform"] == PLATFORM
    assert entry["details"]["action"]["entry_url"] == "https://course.example.edu/"
    _assert_no_session_material(detail)

    conn = client.get(f"/api/v1/connections/{CONN_ID}").json()
    assert SECRET_VALUE not in json.dumps(conn)
    assert conn["status"] == "auth_required"
    _assert_no_session_material(conn)

    # 失效再触发 → 409 通道同样无会话物料（含响应头）
    again = _post_sync(client)
    assert again.status_code == 409
    assert "set-cookie" not in {k.lower() for k in again.headers}
    assert SECRET_VALUE not in again.text
    _assert_no_session_material(again.json())


def test_sync_surfaces_and_logs_contain_no_session_material(
    client: TestClient, sync_env: FakeAdapter, caplog
) -> None:
    """混合成功+含会话键错误的一次同步：jobs 面、连接面、日志文本均无哨兵值。"""
    sync_env.result = _envelope(
        resources=[_resource_item()],
        errors=[
            {
                "code": "UPSTREAM_ERROR",
                "message": "上游条目异常",
                "retryable": True,
                "details": {"cookie_header": SECRET_VALUE, "kind": "assignment"},
            }
        ],
    )
    with caplog.at_level(logging.DEBUG):
        job_id = _post_sync(client).json()["job"]["id"]
        assert _drain_until(job_id) == "partial_failed"

    assert SECRET_VALUE not in caplog.text
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["errors"][0]["details"] == {"kind": "assignment"}
    jobs_list = client.get("/api/v1/jobs").json()
    assert SECRET_VALUE not in json.dumps(jobs_list)
    _assert_no_session_material(jobs_list)
    connection_detail = client.get(f"/api/v1/connections/{CONN_ID}").json()
    _assert_no_session_material(connection_detail)
