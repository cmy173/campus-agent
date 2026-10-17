#!/usr/bin/env python3
"""契约测试：zykt 适配器骨架（A6 验收项；全部为离线合成响应，不触网、无真实凭据）。

覆盖：
  - epoch 秒（含数字字符串）→ ISO 8601 UTC；缺失/非法 → None；
  - video_list 按键对象 → 按键数值升序数组（归一）；
  - Yii2 风格 XML 解析：目录条目、success=false、非法 XML；
  - 转写 list.all_content{BeginSec,EndSec,Text,TransText} → transcript-sample 结构；
  - sync_course 骨架：目录消费、显式删除信号、**到期消失不判故障**（缺失不产生删除）、错误隔离；
  - check_connection / capabilities / list_courses 骨架语义 / fetch_resource（视频不下载红线）。

运行：
  python connectors/tests/test_zykt.py
  python -m pytest connectors/tests/test_zykt.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.adapters.base import AdapterError, PlatformAdapter  # noqa: E402
from connectors.adapters.zykt import (  # noqa: E402
    LOGIN_ENTRY_URL,
    ZyktAdapter,
    epoch_to_iso,
    map_sub_detail,
    map_transcript,
    normalize_video_list,
    parse_catalogue_xml,
)
from connectors.tests.fakes import ScriptedTransport, json_response, text_response  # noqa: E402
from connectors.tests.validate_samples import validator  # noqa: E402

SE_CONNECTION_STATE = validator("connection-state.schema.json")
SE_CAPABILITIES = validator("capabilities.schema.json")
SE_SYNC_RESULT = validator("sync-result.schema.json")
SE_FETCH_OUTCOME = validator("fetch-outcome.schema.json")

FIXED_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = "2026-10-09T12:00:00Z"


def assert_valid(tc: unittest.TestCase, v, obj, label: str) -> None:
    errs = sorted(v.iter_errors(obj), key=lambda e: e.path)
    tc.assertEqual([], [f"{label}: {e.message}" for e in errs], label)


def make_adapter(responses) -> tuple[ZyktAdapter, ScriptedTransport]:
    transport = ScriptedTransport(responses)
    adapter = ZyktAdapter("conn_z", transport=transport, now=lambda: FIXED_NOW)
    return adapter, transport


def catalogue_xml(*items: str) -> str:
    body = "".join(items)
    return f"<response><success>true</success><result>{body}</result></response>"


def catalogue_item(sub_id: str, title: str, *, sub_delete: str = "0", start_at: str = "1665621600") -> str:
    return (
        f"<item><sub_id>{sub_id}</sub_id><title>{title}</title>"
        f"<status>6</status><type>course_live</type><start_at>{start_at}</start_at>"
        f"<sub_public>1</sub_public><sub_show>1</sub_show><sub_delete>{sub_delete}</sub_delete>"
        f"<lecturer_name>某老师</lecturer_name><room>东1-101</room></item>"
    )


class TestEpochToIso(unittest.TestCase):
    def test_int_and_string(self):
        self.assertEqual("2022-10-13T00:40:00Z", epoch_to_iso(1665621600))
        self.assertEqual("2022-10-13T00:40:00Z", epoch_to_iso("1665621600"))
        self.assertEqual("2022-10-13T03:00:00Z", epoch_to_iso("1665630000"))

    def test_invalid_is_none(self):
        for value in (None, "", "abc", True, [], {}, "1.6e999"):
            self.assertIsNone(epoch_to_iso(value), repr(value))


class TestNormalizeVideoList(unittest.TestCase):
    def test_numeric_keys_sorted_ascending(self):
        raw = {
            "10": {"time_offset": 800, "duration": 400, "type": "mp4", "status": "ok"},
            "2": {"time_offset": 200, "duration": 400, "type": "mp4", "status": "ok"},
            "1": {"time_offset": 0, "duration": 200, "type": "mp4", "status": "ok"},
        }
        parts = normalize_video_list(raw)
        self.assertEqual(["1", "2", "10"], [p["key"] for p in parts])
        self.assertEqual(0, parts[0]["time_offset"])

    def test_missing_or_other_shapes(self):
        self.assertEqual([], normalize_video_list(None))
        self.assertEqual([], normalize_video_list("junk"))
        passthrough = normalize_video_list([{"time_offset": 1}])
        self.assertEqual(1, len(passthrough))
        self.assertNotIn("key", passthrough[0])

    def test_non_numeric_keys_after_numeric(self):
        parts = normalize_video_list({"a": 1, "2": 2})
        self.assertEqual(["2", "a"], [p["key"] for p in parts])


class TestParseCatalogueXml(unittest.TestCase):
    def test_items_parsed(self):
        items = parse_catalogue_xml(catalogue_xml(
            catalogue_item("40011", "第01讲"),
            catalogue_item("40012", "第02讲", sub_delete="1"),
        ))
        self.assertEqual(2, len(items))
        self.assertEqual("40011", items[0]["sub_id"])
        self.assertEqual("第01讲", items[0]["title"])
        self.assertEqual("1665621600", items[0]["start_at"])
        self.assertEqual("1", items[1]["sub_delete"])

    def test_success_false_raises(self):
        with self.assertRaises(AdapterError) as ctx:
            parse_catalogue_xml("<response><success>false</success></response>")
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)

    def test_invalid_xml_raises(self):
        with self.assertRaises(AdapterError) as ctx:
            parse_catalogue_xml("<response><broken>")
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)


class TestMapTranscript(unittest.TestCase):
    PAYLOAD = {
        "list": {
            "all_content": [
                {"BeginSec": 15, "EndSec": 19, "Text": "上一讲我们介绍了图的广度优先遍历"},
                {"BeginSec": 19, "EndSec": 26, "Text": "这一讲来看深度优先遍历",
                 "TransText": "In this lecture we look at depth-first traversal"},
                {"BeginSec": "x", "EndSec": 1, "Text": "bad"},
            ]
        }
    }

    def test_segments_mapped(self):
        result = map_transcript(self.PAYLOAD, recording_id="rec_1")
        self.assertEqual("rec_1", result["recording_id"])
        self.assertEqual("zh-CN", result["language"])
        self.assertEqual("platform", result["provenance"])
        self.assertEqual(2, len(result["segments"]))
        first = result["segments"][0]
        self.assertEqual({"start_seconds": 15, "end_seconds": 19,
                          "text": "上一讲我们介绍了图的广度优先遍历"}, first)
        self.assertEqual("In this lecture we look at depth-first traversal",
                         result["segments"][1]["translated_text"])

    def test_shape_mismatch_raises(self):
        with self.assertRaises(AdapterError) as ctx:
            map_transcript({"list": {}})
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)
        with self.assertRaises(AdapterError):
            map_transcript([])


class TestMapSubDetail(unittest.TestCase):
    def test_detail_normalized(self):
        detail = {
            "sub_id": "40011",
            "sub_title": "第01讲",
            "sub_type": "course_live",
            "sub_status": "2",
            "start_at": 1665621600,
            "end_at": "1665630000",
            "duration": "3600",
            "is_allow_video_drag": "1",
            "is_allow_speed_up": "0",
            "video_list": {"2": {"time_offset": 200}, "1": {"time_offset": 0}},
        }
        result = map_sub_detail(detail)
        self.assertEqual("2022-10-13T00:40:00Z", result["started_at"])
        self.assertEqual("2022-10-13T03:00:00Z", result["ended_at"])
        self.assertEqual(3600, result["duration_seconds"])
        self.assertTrue(result["allow_video_drag"])
        self.assertFalse(result["allow_speed_up"])
        self.assertEqual(["1", "2"], [p["key"] for p in result["video_parts"]])

    def test_missing_sub_id_raises(self):
        with self.assertRaises(AdapterError) as ctx:
            map_sub_detail({"sub_title": "x"})
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)


class TestCheckConnection(unittest.TestCase):
    def test_ok(self):
        adapter, transport = make_adapter([json_response({"success": True, "result": {}})])
        state = adapter.check_connection()
        self.assertEqual("ok", state["state"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")
        self.assertEqual("/courseapi/v3/o-course/recent-learning", transport.calls[0]["path"])

    def test_401_auth_required(self):
        adapter, _ = make_adapter([text_response("x", status=401)])
        state = adapter.check_connection()
        self.assertEqual("auth_required", state["state"])
        self.assertEqual(LOGIN_ENTRY_URL, state["action"]["entry_url"])
        self.assertEqual("user_login", state["action"]["type"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_200_html_auth_required(self):
        adapter, _ = make_adapter([text_response("<html>SSO</html>")])
        state = adapter.check_connection()
        self.assertEqual("auth_required", state["state"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_429_blocked(self):
        adapter, _ = make_adapter([text_response("x", status=429)])
        state = adapter.check_connection()
        self.assertEqual("blocked", state["state"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_500_network_error(self):
        adapter, _ = make_adapter([text_response("x", status=500)])
        state = adapter.check_connection()
        self.assertEqual("network_error", state["state"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")


class TestCapabilitiesAndList(unittest.TestCase):
    def test_capabilities(self):
        adapter, _ = make_adapter([])
        caps = adapter.get_capabilities()
        assert_valid(self, SE_CAPABILITIES, caps, "Capabilities")
        self.assertEqual("zjuam-oauth2", caps["auth"]["sso"])
        self.assertEqual("supported", caps["features"]["recordings"]["status"])
        self.assertEqual("supported", caps["features"]["subtitles"]["status"])
        self.assertEqual("unsupported", caps["features"]["assignments"]["status"])

    def test_implements_protocol(self):
        adapter, _ = make_adapter([])
        self.assertIsInstance(adapter, PlatformAdapter)

    def test_invalid_course_cursor_does_not_request(self):
        adapter, _ = make_adapter([])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses(cursor="broken")
        self.assertEqual("VALIDATION_FAILED", ctx.exception.code)


class TestSyncCourseSkeleton(unittest.TestCase):
    def test_catalogue_to_recordings_with_deletion(self):
        adapter, transport = make_adapter([text_response(catalogue_xml(
            catalogue_item("40011", "第01讲"),
            catalogue_item("40012", "第02讲", sub_delete="1"),
            "<item><sub_id>40013</sub_id></item>",  # 缺标题 → 单项错误
        ))])
        result = adapter.sync_course("crs_2")
        assert_valid(self, SE_SYNC_RESULT, result, "SyncResult")
        self.assertEqual("resync", result["mode"])
        self.assertTrue(result["cursor"]["next"].startswith("zykt-full:"))
        self.assertEqual(OBSERVED_AT, result["observed_at"])
        self.assertEqual(1, len(result["upserts"]["recordings"]))
        recording = result["upserts"]["recordings"][0]
        self.assertEqual("第01讲", recording["title"])
        self.assertEqual("2022-10-13T00:40:00Z", recording["started_at"])
        self.assertEqual("unknown", recording["transcript_status"])
        self.assertIsNone(recording["subtitle_languages"])
        self.assertEqual([], result["upserts"]["resources"])
        self.assertEqual([], result["upserts"]["assignments"])
        self.assertEqual(1, len(result["deletions"]))
        self.assertEqual("recording", result["deletions"][0]["kind"])
        self.assertEqual("40012", result["deletions"][0]["external_id"])
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual({"scanned": 3, "upserted": 1, "deleted": 1, "failed": 1}, result["stats"])
        self.assertEqual("/courseapi/v2/course/catalogue", transport.calls[0]["path"])
        self.assertEqual("crs_2", transport.calls[0]["params"]["course_id"])

    def test_expired_replay_vanishing_not_a_fault(self):
        # 第一次：40011 + 40012；第二次：仅 40011（40012 到期消失）→ 不产生删除、不报错（A6-⑤）
        adapter, _ = make_adapter([
            text_response(catalogue_xml(
                catalogue_item("40011", "第01讲"),
                catalogue_item("40012", "第02讲"),
            )),
            text_response(catalogue_xml(catalogue_item("40011", "第01讲"))),
        ])
        first = adapter.sync_course("crs_2")
        self.assertEqual(2, len(first["upserts"]["recordings"]))
        second = adapter.sync_course("crs_2", since_cursor=first["cursor"]["next"])
        assert_valid(self, SE_SYNC_RESULT, second, "SyncResult")
        self.assertEqual([], second["deletions"])
        self.assertEqual([], second["errors"])
        self.assertEqual(1, len(second["upserts"]["recordings"]))
        self.assertEqual(first["cursor"]["next"], second["cursor"]["previous"])

    def test_kinds_filter_skips_catalogue(self):
        adapter, transport = make_adapter([])
        result = adapter.sync_course("crs_2", kinds=["resources"])
        self.assertEqual([], transport.calls)
        self.assertEqual([], result["upserts"]["recordings"])
        assert_valid(self, SE_SYNC_RESULT, result, "SyncResult")

    def test_platform_id_mapping(self):
        transport = ScriptedTransport([
            text_response(catalogue_xml(catalogue_item("40011", "第01讲"))),
        ])
        adapter = ZyktAdapter(
            "conn_z", transport=transport, now=lambda: FIXED_NOW,
            resolve_external_id=lambda cid: "zbkc-77" if cid == "crs_2" else None,
        )
        result = adapter.sync_course("crs_2")
        self.assertEqual("crs_2", result["course_id"])
        self.assertEqual("zbkc-77", transport.calls[0]["params"]["course_id"])


class TestFetchAndTranscript(unittest.TestCase):
    def _synced(self):
        adapter, transport = make_adapter([
            text_response(catalogue_xml(catalogue_item("40011", "第01讲"))),
            json_response(TestMapTranscript.PAYLOAD),
        ])
        adapter.sync_course("crs_2")
        return adapter, transport

    def test_unknown_id_gone(self):
        adapter, _ = self._synced()
        outcome = adapter.fetch_resource("no-such-sub")
        assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
        self.assertEqual("gone", outcome["status"])

    def test_known_video_not_downloaded(self):
        adapter, _ = self._synced()
        with self.assertRaises(AdapterError) as ctx:
            adapter.fetch_resource("40011")
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)
        self.assertIn("红线 3", ctx.exception.error["message"])

    def test_get_transcript(self):
        adapter, transport = self._synced()
        transcript = adapter.get_transcript("40011", recording_id="rec_1")
        self.assertEqual(2, len(transcript["segments"]))
        self.assertEqual("rec_1", transcript["recording_id"])
        call = transport.calls[-1]
        self.assertEqual("/courseapi/v3/web-socket/search-trans-result", call["path"])
        self.assertEqual({"sub_id": "40011", "format": "json"}, call["params"])

    def test_transcript_auth_expired(self):
        adapter, _ = make_adapter([text_response("x", status=401)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.get_transcript("40011")
        self.assertEqual("AUTH_EXPIRED", ctx.exception.code)
        self.assertEqual("user_login", ctx.exception.error["details"]["action"]["type"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
