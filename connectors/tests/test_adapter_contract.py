#!/usr/bin/env python3
"""契约测试：mock 适配器五操作面（A3/A4 验收项）。

覆盖：
  - 五方法输出对 contracts/schemas/ 全量校验；
  - 确定性可重放（同一调用序列字节一致）；
  - 连续两次 sync 不产生重复资源；凭游标再同步=无新增、无删除；
  - 同名新版本识别为版本变更（version/hash 变化，同 external_id）；
  - 空响应/失败不产生删除（失败=抛 AdapterError，不产出任何删除事件）；
  - 游标失效 → mode=resync 全量对照；
  - 受限资源 fetch 返回 denial 而非抛错；auth/network/blocked 的恢复语义；
  - mock 输出显著标注（connector_version 含 -mock），防止误当真实采集。

运行：
  python connectors/tests/test_adapter_contract.py
  python -m pytest connectors/tests/test_adapter_contract.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.adapters.base import AdapterError, PlatformAdapter  # noqa: E402
from connectors.adapters.mock import MockAdapter  # noqa: E402
from connectors.tests.validate_samples import load, validator  # noqa: E402

SE_CONNECTION_STATE = validator("connection-state.schema.json")
SE_CAPABILITIES = validator("capabilities.schema.json")
SE_COURSE = validator("course.schema.json")
SE_SYNC_RESULT = validator("sync-result.schema.json")
SE_FETCH_OUTCOME = validator("fetch-outcome.schema.json")
SE_ERROR = validator("error.schema.json", "/$defs/Error")

XZZD_COURSE = "crs_3f8a21"
ZYKT_COURSE = "crs_9b6c40"


def assert_valid(tc: unittest.TestCase, v, obj, label: str) -> None:
    errs = sorted(v.iter_errors(obj), key=lambda e: e.path)
    tc.assertEqual([], [f"{label}: {e.message}" for e in errs], label)


def external_ids(upserts: dict) -> list[str]:
    ids = []
    for group in upserts.values():
        ids.extend(item["source_ref"]["external_id"] for item in group)
    return ids


class TestProtocol(unittest.TestCase):
    def test_mock_implements_protocol(self):
        self.assertIsInstance(MockAdapter(), PlatformAdapter)

    def test_unknown_scenario_rejected(self):
        with self.assertRaises(ValueError):
            MockAdapter(scenario="nope")


class TestCheckConnection(unittest.TestCase):
    def test_default_ok(self):
        payload = MockAdapter().check_connection()
        assert_valid(self, SE_CONNECTION_STATE, payload, "check_connection")
        self.assertEqual("ok", payload["state"])
        self.assertIsNone(payload["action"])
        self.assertIsNone(payload["detail"])

    def test_auth_required_carries_recovery_action(self):
        payload = MockAdapter(scenario="auth_required").check_connection()
        assert_valid(self, SE_CONNECTION_STATE, payload, "check_connection/auth")
        self.assertEqual("auth_required", payload["state"])
        self.assertEqual("user_login", payload["action"]["type"])
        self.assertEqual("https://course.zju.edu.cn/ua/login", payload["action"]["entry_url"])

    def test_non_ok_states(self):
        for scenario, expected in (
            ("network_error", "network_error"),
            ("blocked", "blocked"),
        ):
            payload = MockAdapter(scenario=scenario).check_connection()
            assert_valid(self, SE_CONNECTION_STATE, payload, f"check_connection/{scenario}")
            self.assertEqual(expected, payload["state"])

    def test_checked_at_is_virtual_clock(self):
        adapter = MockAdapter(now="2026-10-08T09:10:00+08:00")
        self.assertEqual("2026-10-08T09:10:00+08:00", adapter.check_connection()["checked_at"])


class TestCapabilities(unittest.TestCase):
    def test_xzzd_capabilities_valid_and_marked_mock(self):
        payload = MockAdapter(platform="xzzd").get_capabilities()
        assert_valid(self, SE_CAPABILITIES, payload, "capabilities")
        self.assertEqual("xzzd", payload["platform"])
        self.assertTrue(payload["connector_version"].endswith("-mock"))
        self.assertEqual("zjuam-cas", payload["auth"]["sso"])

    def test_zykt_capabilities_valid(self):
        payload = MockAdapter(platform="zykt").get_capabilities()
        assert_valid(self, SE_CAPABILITIES, payload, "capabilities/zykt")
        self.assertEqual("zjuam-oauth2", payload["auth"]["sso"])


class TestListCourses(unittest.TestCase):
    def test_xzzd_courses_valid(self):
        envelope = MockAdapter(platform="xzzd").list_courses()
        self.assertEqual({"items", "next_cursor", "errors"}, set(envelope))
        self.assertIsNone(envelope["next_cursor"])
        self.assertEqual([], envelope["errors"])
        self.assertEqual(2, len(envelope["items"]))
        for item in envelope["items"]:
            assert_valid(self, SE_COURSE, item, "list_courses/item")
            self.assertTrue(any(r["platform"] == "xzzd" for r in item["source_refs"]))

    def test_zykt_courses_filtered_by_platform(self):
        envelope = MockAdapter(platform="zykt").list_courses()
        self.assertEqual([ZYKT_COURSE], [c["id"] for c in envelope["items"]])

    def test_term_filter(self):
        adapter = MockAdapter(platform="xzzd")
        self.assertEqual(2, len(adapter.list_courses(term="2026-2027-秋冬")["items"]))
        self.assertEqual(0, len(adapter.list_courses(term="无此学期")["items"]))

    def test_replay_deterministic(self):
        a = json.dumps(MockAdapter(platform="xzzd").list_courses(), sort_keys=True)
        b = json.dumps(MockAdapter(platform="xzzd").list_courses(), sort_keys=True)
        self.assertEqual(a, b)


class TestSyncCourse(unittest.TestCase):
    def test_snapshot_schema_valid_and_echoes_ids(self):
        result = MockAdapter(connection_id="conn_test").sync_course(XZZD_COURSE)
        assert_valid(self, SE_SYNC_RESULT, result, "sync_course")
        self.assertEqual("conn_test", result["connection_id"])
        self.assertEqual(XZZD_COURSE, result["course_id"])
        self.assertEqual("incremental", result["mode"])
        self.assertIsNone(result["cursor"]["previous"])
        self.assertTrue(result["cursor"]["next"])
        self.assertEqual([], result["deletions"])
        self.assertEqual([], result["errors"])

    def test_snapshot_contains_expected_sample_items(self):
        result = MockAdapter().sync_course(XZZD_COURSE)
        ids = external_ids(result["upserts"])
        self.assertIn("xzzd-file-33001", ids)          # res_01a7（同名新版本样例）
        self.assertIn("xzzd-file-33002", ids)          # res_01b2
        self.assertIn("xzzd-file-33003", ids)          # res_01b3（重复文件）
        self.assertIn("xzzd-assign-55001", ids)        # asg_a101
        self.assertNotIn("zykt-sub-40011", ids)        # 平台过滤：zykt 条目不进 xzzd 同步
        self.assertEqual(len(ids), len(set(ids)), "同一同步内 external_id 不得重复")

    def test_two_consecutive_syncs_no_duplicates_no_deletions(self):
        adapter = MockAdapter()
        first = adapter.sync_course(XZZD_COURSE)
        second = adapter.sync_course(XZZD_COURSE, since_cursor=first["cursor"]["next"])
        third = adapter.sync_course(XZZD_COURSE, since_cursor=second["cursor"]["next"])

        for result in (second, third):
            assert_valid(self, SE_SYNC_RESULT, result, "sync_course/next")
            self.assertEqual([], result["deletions"])
            self.assertEqual(
                {"resources": [], "assignments": [], "recordings": []}, result["upserts"]
            )
        self.assertEqual(first["cursor"]["next"], second["cursor"]["previous"])
        self.assertEqual(json.dumps(second, sort_keys=True), json.dumps(third, sort_keys=True))

    def test_replay_same_sequence_identical_output(self):
        def run():
            adapter = MockAdapter()
            r0 = adapter.sync_course(XZZD_COURSE)
            r1 = adapter.sync_course(XZZD_COURSE, since_cursor=r0["cursor"]["next"])
            return json.dumps([r0, r1], sort_keys=True)

        self.assertEqual(run(), run())

    def test_version_update_recognized(self):
        adapter = MockAdapter(scenario="version_update")
        first = adapter.sync_course(XZZD_COURSE)
        second = adapter.sync_course(XZZD_COURSE, since_cursor=first["cursor"]["next"])
        for result in (first, second):
            assert_valid(self, SE_SYNC_RESULT, result, "sync_course/version")

        before = next(u for u in first["upserts"]["resources"] if u["title"] == "第03讲-线性表.pdf")
        after = next(u for u in second["upserts"]["resources"] if u["title"] == "第03讲-线性表.pdf")
        self.assertEqual(before["source_ref"]["external_id"], after["source_ref"]["external_id"])
        self.assertEqual(1, before["version"])
        self.assertEqual(2, after["version"])
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        self.assertIsNotNone(after["content_hash"])

    def test_empty_scenario_never_deletes(self):
        adapter = MockAdapter(scenario="empty")
        first = adapter.sync_course(XZZD_COURSE)
        second = adapter.sync_course(XZZD_COURSE, since_cursor=first["cursor"]["next"])
        for result in (first, second):
            assert_valid(self, SE_SYNC_RESULT, result, "sync_course/empty")
            self.assertEqual([], result["deletions"])
            self.assertEqual(0, result["stats"]["upserted"])

    def test_partial_failure_matches_sample_and_isolation(self):
        sample = load("sync-result.json")
        result = MockAdapter(scenario="partial_failure").sync_course(XZZD_COURSE)
        assert_valid(self, SE_SYNC_RESULT, result, "sync_course/partial")
        self.assertEqual(sample["upserts"], result["upserts"])
        self.assertEqual(sample["deletions"], result["deletions"])
        self.assertEqual(sample["errors"], result["errors"])
        self.assertEqual(sample["stats"], result["stats"])
        # 单项失败隔离：删除仅来自显式信号，错误不抹除有效 upsert
        self.assertTrue(all(d["reason"] == "explicit_delete_signal" for d in result["deletions"]))
        self.assertEqual(3, result["stats"]["upserted"])
        self.assertEqual(1, result["stats"]["failed"])

    def test_unknown_cursor_falls_back_to_resync(self):
        result = MockAdapter().sync_course(XZZD_COURSE, since_cursor="cur_bogus")
        assert_valid(self, SE_SYNC_RESULT, result, "sync_course/resync")
        self.assertEqual("resync", result["mode"])
        self.assertEqual("cur_bogus", result["cursor"]["previous"])
        self.assertTrue(result["upserts"]["resources"], "resync 应返回全量对照")

    def test_kinds_filter(self):
        result = MockAdapter().sync_course(XZZD_COURSE, kinds=("resources",))
        assert_valid(self, SE_SYNC_RESULT, result, "sync_course/kinds")
        self.assertTrue(result["upserts"]["resources"])
        self.assertEqual([], result["upserts"]["assignments"])
        self.assertEqual([], result["upserts"]["recordings"])

    def test_zykt_course_sync(self):
        result = MockAdapter(platform="zykt").sync_course(ZYKT_COURSE)
        assert_valid(self, SE_SYNC_RESULT, result, "sync_course/zykt")
        ids = external_ids(result["upserts"])
        self.assertIn("zykt-sub-40011", ids)

    def test_auth_required_raises_without_deletions(self):
        adapter = MockAdapter(scenario="auth_required")
        with self.assertRaises(AdapterError) as ctx:
            adapter.sync_course(XZZD_COURSE)
        err = ctx.exception.error
        assert_valid(self, SE_ERROR, err, "AdapterError/auth")
        self.assertEqual("AUTH_EXPIRED", err["code"])
        self.assertFalse(err["retryable"])
        self.assertEqual("user_login", err["details"]["action"]["type"])

    def test_network_error_retryable(self):
        with self.assertRaises(AdapterError) as ctx:
            MockAdapter(scenario="network_error").sync_course(XZZD_COURSE)
        assert_valid(self, SE_ERROR, ctx.exception.error, "AdapterError/network")
        self.assertEqual("NETWORK_ERROR", ctx.exception.error["code"])
        self.assertTrue(ctx.exception.error["retryable"])

    def test_blocked_not_retryable(self):
        with self.assertRaises(AdapterError) as ctx:
            MockAdapter(scenario="blocked").sync_course(XZZD_COURSE)
        assert_valid(self, SE_ERROR, ctx.exception.error, "AdapterError/blocked")
        self.assertEqual("PERMISSION_DENIED", ctx.exception.error["code"])
        self.assertFalse(ctx.exception.error["retryable"])


class TestFetchResource(unittest.TestCase):
    def test_full_access_returns_handle(self):
        result = MockAdapter().fetch_resource("res_01a7")
        assert_valid(self, SE_FETCH_OUTCOME, result, "fetch/ok")
        self.assertEqual("ok", result["status"])
        self.assertEqual("local_file", result["handle"]["kind"])
        self.assertEqual(
            load("resources.json")["resources"][0]["content_hash"], result["handle"]["sha256"]
        )
        self.assertEqual(1893000, result["handle"]["size"])
        self.assertTrue(result["handle"]["ref"].startswith("mock-staging/"))
        self.assertIsNone(result["denial"])

    def test_preview_only_returns_denial_not_error(self):
        result = MockAdapter().fetch_resource("res_01d4")
        assert_valid(self, SE_FETCH_OUTCOME, result, "fetch/denied")
        self.assertEqual("denied", result["status"])
        self.assertIsNone(result["handle"])
        self.assertEqual("preview_only", result["denial"]["reason"])

    def test_unknown_resource_gone(self):
        result = MockAdapter().fetch_resource("res_nope")
        assert_valid(self, SE_FETCH_OUTCOME, result, "fetch/gone")
        self.assertEqual("gone", result["status"])
        self.assertIsNone(result["handle"])
        self.assertIsNone(result["denial"])

    def test_transient_error_scenario(self):
        result = MockAdapter(scenario="network_error").fetch_resource("res_01a7")
        assert_valid(self, SE_FETCH_OUTCOME, result, "fetch/transient")
        self.assertEqual("transient_error", result["status"])

    def test_auth_scenario_raises(self):
        with self.assertRaises(AdapterError) as ctx:
            MockAdapter(scenario="auth_required").fetch_resource("res_01a7")
        self.assertEqual("AUTH_EXPIRED", ctx.exception.error["code"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
