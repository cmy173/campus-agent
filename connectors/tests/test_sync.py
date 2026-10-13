#!/usr/bin/env python3
"""测试：同步编排与故障恢复（A7 验收项）。

覆盖：
  - 有界重试 + 指数退避（仅 retryable 错误；退避序列 1s/2s…）；
  - 重试耗尽：阶段一上抛；阶段二记为该条目 transient_error（attempts = max_retries + 1）；
  - AUTH_EXPIRED（retryable=false）立即上抛、不循环、不等待；恢复路径随 details.action；
  - 限速：默认 0.5 rps（最小间隔 2s）、可配置、非法值拒绝；
  - 协作式取消：前置取消零平台调用；条目间取消携带已完成断点；取消后凭游标恢复；
  - 两阶段分离：sync_course 不触发 fetch，fetch_contents 不触发 sync；单项错误原样保留；
  - 单项失败隔离：ok/gone/denied 终态不重试；transient 有界重试；非重试错误映射终态并继续；连接级错误中止整批；
  - 游标原样透传（不透明，编排器不解析）。

运行：
  python connectors/tests/test_sync.py
  python -m pytest connectors/tests/test_sync.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.adapters.base import AdapterError  # noqa: E402
from connectors.sync import SyncCancelled, SyncOrchestrator  # noqa: E402

SYNC_OK = {
    "platform": "test",
    "connection_id": "conn_test",
    "course_id": "crs_1",
    "mode": "incremental",
    "cursor": {"previous": None, "next": "cur_1"},
    "upserts": {"resources": [], "assignments": [], "recordings": []},
    "deletions": [],
    "errors": [],
    "stats": {"scanned": 0, "upserted": 0, "deleted": 0, "failed": 0},
    "observed_at": "2026-10-08T09:10:00+08:00",
}


def fetch_ok(resource_id: str = "res_1") -> dict:
    return {
        "status": "ok",
        "handle": {
            "kind": "local_file",
            "ref": f"mock-staging/{resource_id}",
            "expires_at": "2026-10-08T09:40:00+08:00",
            "sha256": "sha256:" + "0" * 64,
            "size": 1024,
        },
        "denial": None,
    }


FETCH_GONE = {"status": "gone", "handle": None, "denial": None}
FETCH_TRANSIENT = {"status": "transient_error", "handle": None, "denial": None}
FETCH_DENIED = {
    "status": "denied",
    "handle": None,
    "denial": {"reason": "preview_only", "message": "仅预览", "retryable": False},
}


def adapter_error(code="RATE_LIMITED", *, retryable=True, details=None, message="合成错误"):
    return AdapterError(code, message, retryable=retryable, details=details)


class FakeClock:
    """假时钟：sleep 推进并记录，供验证退避与限速，不真实等待。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def _raise(err: Exception):
    def fn():
        raise err

    return fn


def _const(value):
    def fn():
        return value

    return fn


class ScriptedAdapter:
    """脚本化测试替身：按脚本顺序返回/抛错；记录每次调用参数。"""

    platform = "test"
    connection_id = "conn_test"
    connector_version = "test"

    def __init__(self, sync_script=None, fetch_script=None) -> None:
        self.sync_calls: list[dict] = []
        self.fetch_calls: list[str] = []
        self._sync_script = list(sync_script or [])
        self._fetch_script = list(fetch_script or [])

    @staticmethod
    def _next(script: list, default):
        item = script.pop(0) if script else default
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return item

    def sync_course(self, course_id, since_cursor=None, kinds=None):
        self.sync_calls.append(
            {"course_id": course_id, "since_cursor": since_cursor, "kinds": kinds}
        )
        return self._next(self._sync_script, SYNC_OK)

    def fetch_resource(self, resource_id):
        self.fetch_calls.append(resource_id)
        return self._next(self._fetch_script, fetch_ok(resource_id))


def make_orchestrator(adapter, **kwargs):
    clock = FakeClock()
    kwargs.setdefault("sleep", clock.sleep)
    kwargs.setdefault("clock", clock.clock)
    return SyncOrchestrator(adapter, **kwargs), clock


class TestRetry(unittest.TestCase):
    def test_retry_then_success(self):
        adapter = ScriptedAdapter(
            sync_script=[adapter_error(), adapter_error(), SYNC_OK]
        )
        orch, clock = make_orchestrator(adapter)
        result = orch.sync_course("crs_1")
        self.assertEqual(SYNC_OK, result)
        self.assertEqual(3, len(adapter.sync_calls))
        # 退避 1s、2s 递增；第二次尝试前限速补足 1s（默认最小间隔 2s）
        self.assertEqual([1.0, 1.0, 2.0], clock.sleeps)

    def test_retry_exhausted_raises(self):
        err = adapter_error()
        adapter = ScriptedAdapter(sync_script=[err, err, err, err])
        orch, clock = make_orchestrator(adapter, max_retries=2)
        with self.assertRaises(AdapterError) as ctx:
            orch.sync_course("crs_1")
        self.assertEqual("RATE_LIMITED", ctx.exception.code)
        self.assertEqual(3, len(adapter.sync_calls))  # 1 次首调 + max_retries 次重试

    def test_auth_expired_no_loop_no_wait(self):
        err = adapter_error(
            "AUTH_EXPIRED",
            retryable=False,
            details={
                "action": {
                    "type": "user_login",
                    "entry_url": "https://xzzd.example/login",
                    "message": "请重新登录",
                }
            },
        )
        adapter = ScriptedAdapter(sync_script=[err, SYNC_OK])
        orch, clock = make_orchestrator(adapter)
        with self.assertRaises(AdapterError) as ctx:
            orch.sync_course("crs_1")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual("user_login", ctx.exception.error["details"]["action"]["type"])
        self.assertEqual(1, len(adapter.sync_calls))  # 不循环重试
        self.assertEqual([], clock.sleeps)  # 不等待

    def test_non_retryable_other_code_raises_immediately(self):
        adapter = ScriptedAdapter(
            sync_script=[adapter_error("VALIDATION_FAILED", retryable=False), SYNC_OK]
        )
        orch, _ = make_orchestrator(adapter)
        with self.assertRaises(AdapterError):
            orch.sync_course("crs_1")
        self.assertEqual(1, len(adapter.sync_calls))


class TestThrottle(unittest.TestCase):
    def test_default_rate_is_0_5_rps(self):
        adapter = ScriptedAdapter()
        orch, clock = make_orchestrator(adapter)
        orch.fetch_contents(["res_1", "res_2"])
        self.assertEqual(2, len(adapter.fetch_calls))
        self.assertEqual([2.0], clock.sleeps)  # 第二次调用前补足 2s

    def test_rate_configurable(self):
        adapter = ScriptedAdapter()
        orch, clock = make_orchestrator(adapter, rate_limit_rps=2.0)
        orch.fetch_contents(["res_1", "res_2"])
        self.assertEqual([0.5], clock.sleeps)

    def test_invalid_config_rejected(self):
        adapter = ScriptedAdapter()
        with self.assertRaises(ValueError):
            SyncOrchestrator(adapter, rate_limit_rps=0)
        with self.assertRaises(ValueError):
            SyncOrchestrator(adapter, max_retries=-1)


class TestCancel(unittest.TestCase):
    def test_pre_cancel_makes_zero_calls(self):
        adapter = ScriptedAdapter()
        orch = SyncOrchestrator(adapter, sleep=lambda s: None, clock=lambda: 0.0)
        orch.cancel()
        self.assertTrue(orch.cancelled)
        with self.assertRaises(SyncCancelled):
            orch.sync_course("crs_1")
        with self.assertRaises(SyncCancelled):
            orch.fetch_contents(["res_1"])
        self.assertEqual([], adapter.sync_calls)
        self.assertEqual([], adapter.fetch_calls)

    def test_cancel_between_items_keeps_completed(self):
        adapter = ScriptedAdapter(fetch_script=[fetch_ok("res_1"), fetch_ok("res_2")])
        orch, _ = make_orchestrator(adapter)

        def on_outcome(payload):
            if payload["resource_id"] == "res_1":
                orch.cancel()

        with self.assertRaises(SyncCancelled) as ctx:
            orch.fetch_contents(["res_1", "res_2", "res_3"], on_outcome=on_outcome)
        self.assertEqual(["res_1"], ctx.exception.completed)
        self.assertEqual(["res_1"], adapter.fetch_calls)  # res_2 未取

    def test_resume_after_cancel_uses_saved_cursor(self):
        adapter = ScriptedAdapter(sync_script=[SYNC_OK])
        orch1 = SyncOrchestrator(adapter, sleep=lambda s: None, clock=lambda: 0.0)
        orch1.cancel()
        with self.assertRaises(SyncCancelled):
            orch1.sync_course("crs_1", since_cursor="opaque:cur:01")

        orch2, _ = make_orchestrator(adapter)  # 新编排器 = 恢复
        result = orch2.sync_course("crs_1", since_cursor="opaque:cur:01")
        self.assertEqual(SYNC_OK, result)
        # 游标原样透传（不解析、不改写）
        self.assertEqual("opaque:cur:01", adapter.sync_calls[-1]["since_cursor"])
        self.assertEqual("crs_1", adapter.sync_calls[-1]["course_id"])

    def test_cursor_and_kinds_pass_through(self):
        adapter = ScriptedAdapter()
        orch, _ = make_orchestrator(adapter)
        orch.sync_course("crs_1", since_cursor="cur_9f2e64aa", kinds=["resources"])
        self.assertEqual(
            {"course_id": "crs_1", "since_cursor": "cur_9f2e64aa", "kinds": ["resources"]},
            adapter.sync_calls[-1],
        )


class TestTwoPhase(unittest.TestCase):
    def test_sync_does_not_fetch(self):
        adapter = ScriptedAdapter()
        orch, _ = make_orchestrator(adapter)
        orch.sync_course("crs_1")
        self.assertEqual([], adapter.fetch_calls)

    def test_fetch_does_not_sync(self):
        adapter = ScriptedAdapter()
        orch, _ = make_orchestrator(adapter)
        orch.fetch_contents(["res_1"])
        self.assertEqual([], adapter.sync_calls)

    def test_single_item_errors_preserved(self):
        result = dict(SYNC_OK)
        result["errors"] = [
            {
                "code": "ITEM_FETCH_FAILED",
                "message": "单项失败",
                "retryable": True,
                "details": {"external_id": "7100001"},
                "request_id": "req_test",
            }
        ]
        result["mode"] = "partial"
        adapter = ScriptedAdapter(sync_script=[result])
        orch, _ = make_orchestrator(adapter)
        # errors 非空不视为失败：原样返回
        self.assertEqual(result, orch.sync_course("crs_1"))


class TestItemIsolation(unittest.TestCase):
    def test_mixed_outcomes_do_not_abort_batch(self):
        adapter = ScriptedAdapter(
            fetch_script=[
                fetch_ok("res_a"),
                FETCH_TRANSIENT,
                fetch_ok("res_b"),
                FETCH_GONE,
                FETCH_TRANSIENT,
                FETCH_TRANSIENT,
            ]
        )
        orch, _ = make_orchestrator(adapter, max_retries=1)
        results = orch.fetch_contents(["res_a", "res_b", "res_c", "res_d"])
        self.assertEqual(
            ["res_a", "res_b", "res_b", "res_c", "res_d", "res_d"],
            adapter.fetch_calls,
        )
        self.assertEqual(["ok", "ok", "gone", "transient_error"],
                         [r["outcome"]["status"] for r in results])
        self.assertEqual([1, 2, 1, 2], [r["attempts"] for r in results])
        # 重试成功：res_b 第二次返回 ok
        self.assertEqual("mock-staging/res_b", results[1]["outcome"]["handle"]["ref"])

    def test_exception_exhaustion_records_error(self):
        adapter = ScriptedAdapter(
            fetch_script=[
                adapter_error("NETWORK_ERROR"),
                adapter_error("NETWORK_ERROR"),
                fetch_ok(),
            ]
        )
        orch, _ = make_orchestrator(adapter, max_retries=1)
        results = orch.fetch_contents(["res_x"])
        self.assertEqual("transient_error", results[0]["outcome"]["status"])
        self.assertEqual(2, results[0]["attempts"])  # 1 次首调 + 1 次重试
        self.assertEqual("NETWORK_ERROR", results[0]["error"]["code"])

    def test_denied_not_retried(self):
        adapter = ScriptedAdapter(fetch_script=[FETCH_DENIED])
        orch, _ = make_orchestrator(adapter)
        results = orch.fetch_contents(["res_x"])
        self.assertEqual("denied", results[0]["outcome"]["status"])
        self.assertEqual("preview_only", results[0]["outcome"]["denial"]["reason"])
        self.assertEqual(1, results[0]["attempts"])
        self.assertEqual(["res_x"], adapter.fetch_calls)

    def test_upstream_changed_aborts_batch(self):
        adapter = ScriptedAdapter(
            fetch_script=[
                fetch_ok("res_a"),
                adapter_error("UPSTREAM_CHANGED", retryable=False),
                fetch_ok("res_c"),
            ]
        )
        orch, _ = make_orchestrator(adapter)
        with self.assertRaises(AdapterError) as ctx:
            orch.fetch_contents(["res_a", "res_b", "res_c"])
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)
        self.assertEqual(["res_a", "res_b"], adapter.fetch_calls)  # res_c 未取

    def test_permission_denied_maps_and_continues(self):
        adapter = ScriptedAdapter(
            fetch_script=[
                fetch_ok("res_a"),
                adapter_error("PERMISSION_DENIED", retryable=False, message="平台拒绝"),
                fetch_ok("res_c"),
            ]
        )
        orch, _ = make_orchestrator(adapter)
        results = orch.fetch_contents(["res_a", "res_b", "res_c"])
        self.assertEqual(["res_a", "res_b", "res_c"], adapter.fetch_calls)  # 继续其他项
        self.assertEqual("denied", results[1]["outcome"]["status"])
        self.assertEqual("permission_denied", results[1]["outcome"]["denial"]["reason"])
        self.assertEqual("PERMISSION_DENIED", results[1]["error"]["code"])
        self.assertEqual("ok", results[2]["outcome"]["status"])

    def test_resource_not_found_maps_gone(self):
        adapter = ScriptedAdapter(
            fetch_script=[
                adapter_error("RESOURCE_NOT_FOUND", retryable=False),
                fetch_ok("res_b"),
            ]
        )
        orch, _ = make_orchestrator(adapter)
        results = orch.fetch_contents(["res_a", "res_b"])
        self.assertEqual("gone", results[0]["outcome"]["status"])
        self.assertEqual("RESOURCE_NOT_FOUND", results[0]["error"]["code"])
        self.assertEqual("ok", results[1]["outcome"]["status"])

    def test_fetch_auth_expired_single_call_then_abort(self):
        adapter = ScriptedAdapter(
            fetch_script=[adapter_error("AUTH_EXPIRED", retryable=False), fetch_ok()]
        )
        orch, clock = make_orchestrator(adapter)
        with self.assertRaises(AdapterError):
            orch.fetch_contents(["res_a", "res_b"])
        self.assertEqual(["res_a"], adapter.fetch_calls)
        self.assertEqual([], clock.sleeps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
