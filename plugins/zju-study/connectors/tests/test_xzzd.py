#!/usr/bin/env python3
"""契约测试：xzzd 真实适配器（A5 验收项；全部为离线合成响应，不触网、无真实凭据）。

覆盖：
  - 时间：ISO 8601（Z）→ 契约 UTC 归一；偏移量换算；非法/缺失 → None（不猜测）；
  - access_level 真值表（allow_download / origin_allow_download 三态）；
  - submitted/submitted_status → submission_status 映射（A5b 校正；未知值 → unknown）；
  - check_connection 各状态（ok / auth_required+action / blocked / network_error / 传输异常）；
  - list_courses：分页信封消费（pages/page/page_size）、游标、无效游标、term 拒绝、单项错误隔离；
  - sync_course：全量对照（resync）、字段映射（uploads[] 附件载荷）、显式删除信号
    （uploads[].deleted，同时失效可见性缓存）、空响应无删除、kinds 过滤、分页合并、
    页数超限、结果过 sync-result schema；
  - fetch_resource（A22）：restricted/preview_only → denial（不抛错）；未观测 → gone；
    full → 发签 → 流式下载 → sha256 暂存件（handle=local_file；两次获取确定性同名同哈希）；
    护栏 PD-1（元数据前置 + 下载中途双重把关）、发签 403、下载通道 403/404/401/429/5xx/
    网络异常映射、缺 download_dir / 缺 upload id / 传输层无下载能力的错误码；
    签名 URL（含 token）不出现在任何返回值（redaction 断言）。
    RequestsTransport.download 的本体行为（原子落盘/中途护栏清理）见 test_transport.py。
  - 错误映射：401/419 → AUTH_EXPIRED（含 details.action）；403 → PERMISSION_DENIED（含平台
    message，A5b 实测）；429 → RATE_LIMITED；5xx（含实测 500 HTML 形态）/ 传输异常 →
    NETWORK_ERROR；404 携带平台 message；200 HTML → AUTH_EXPIRED（推断）。

运行：
  python connectors/tests/test_xzzd.py
  python -m pytest connectors/tests/test_xzzd.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.adapters.base import AdapterError, PlatformAdapter  # noqa: E402
from connectors.adapters.transport import DownloadError, DownloadTooLarge  # noqa: E402
from connectors.adapters.xzzd import (  # noqa: E402
    CONNECTOR_VERSION,
    LOGIN_ENTRY_URL,
    MAX_DOWNLOAD_BYTES,
    XzzdAdapter,
    map_access_level,
    map_resource_kind,
    map_submission_status,
    to_contract_time,
)
from connectors.tests.fakes import ScriptedTransport, json_response, text_response  # noqa: E402
from connectors.tests.validate_samples import validator  # noqa: E402

SE_CONNECTION_STATE = validator("connection-state.schema.json")
SE_CAPABILITIES = validator("capabilities.schema.json")
SE_COURSE = validator("course.schema.json")
SE_SYNC_RESULT = validator("sync-result.schema.json")
SE_FETCH_OUTCOME = validator("fetch-outcome.schema.json")
SE_ERROR = validator("error.schema.json", "/$defs/Error")

FIXED_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = "2026-10-09T12:00:00Z"
SIGN_URL = "https://media.example.invalid/download/file/71000111?timestamp=1&token=SECRET-TOKEN-9f2a"


def assert_valid(tc: unittest.TestCase, v, obj, label: str) -> None:
    errs = sorted(v.iter_errors(obj), key=lambda e: e.path)
    tc.assertEqual([], [f"{label}: {e.message}" for e in errs], label)


class DownloadFake(ScriptedTransport):
    """request 用排队响应；download 按排队载荷写盘（模拟 DownloadTransport 语义）。"""

    def __init__(self, responses, payloads=()):
        super().__init__(responses)
        self.payloads = list(payloads)  # bytes（成功）| Exception（失败）
        self.download_calls: list[dict] = []

    def download(self, url, dest, *, max_bytes=None, chunk_size=65536):
        self.download_calls.append({"url": url, "dest": Path(dest), "max_bytes": max_bytes})
        if not self.payloads:
            raise AssertionError(f"未预期的下载请求：{url}")
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        if max_bytes is not None and len(item) > max_bytes:
            raise DownloadTooLarge(max_bytes)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(item)
        return len(item), hashlib.sha256(item).hexdigest()


def make_adapter(responses, **kwargs) -> tuple[XzzdAdapter, ScriptedTransport]:
    transport = kwargs.pop("transport", None) or ScriptedTransport(responses)
    kwargs.setdefault("now", lambda: FIXED_NOW)
    adapter = XzzdAdapter("conn_x", transport=transport, **kwargs)
    return adapter, transport


def courseware(cid, name, *, title=None, deleted=None, uploads=None, **upload_over):
    """合成响应：A5b 实测形态（活动 title + uploads[] 附件；许可/删除信号在附件级）。"""
    upload = {
        "id": cid * 10 + 1,
        "name": name,
        "size": 1024,
        "type": "application/pdf",
        "key": f"k/{cid}",
        "status": "ready",
        "allow_download": True,
        "origin_allow_download": True,
        "deleted": False,
    }
    upload.update(upload_over)
    if deleted is not None:
        upload["deleted"] = deleted
    return {
        "id": cid,
        "title": name if title is None else title,
        "type": "courseware",
        "updated_at": "2026-10-08T09:10:00Z",
        "created_at": "2026-09-01T00:00:00Z",
        "uploads": uploads if uploads is not None else [upload],
    }


def homework(hid, title, **over):
    """合成响应：A5b 实测形态（submitted bool + submitted_status 字符串；列表载荷无 status_code）。"""
    item = {
        "id": hid,
        "title": title,
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-10-20T15:59:00Z",
        "deadline": "2026-10-20T15:59:00Z",
        "data": {"description": "要求正文"},
        "homework_type": "file_upload",
        "submitted": True,
        "submitted_status": "",
    }
    item.update(over)
    return item


class TestTimeConversion(unittest.TestCase):
    def test_utc_z_passthrough(self):
        self.assertEqual("2026-10-08T09:10:00Z", to_contract_time("2026-10-08T09:10:00Z"))

    def test_offset_normalized_to_utc(self):
        self.assertEqual("2026-10-08T01:10:00Z", to_contract_time("2026-10-08T09:10:00+08:00"))

    def test_naive_treated_as_utc(self):
        self.assertEqual("2026-10-08T09:10:00Z", to_contract_time("2026-10-08T09:10:00"))

    def test_invalid_or_missing_is_none(self):
        for value in (None, "", "not-a-time", 123, [], {}):
            self.assertIsNone(to_contract_time(value), repr(value))


class TestAccessLevel(unittest.TestCase):
    def test_full_when_allow_true(self):
        self.assertEqual("full", map_access_level(True, False))
        self.assertEqual("full", map_access_level("true", "0"))
        self.assertEqual("full", map_access_level(1, None))

    def test_preview_only_when_allow_false_origin_true(self):
        self.assertEqual("preview_only", map_access_level(False, True))
        self.assertEqual("preview_only", map_access_level("0", "1"))

    def test_restricted_conservative(self):
        self.assertEqual("restricted", map_access_level(False, False))
        self.assertEqual("restricted", map_access_level(None, None))
        self.assertEqual("restricted", map_access_level(None, True))  # 有效标记缺失 → 保守
        self.assertEqual("restricted", map_access_level("weird", "weird"))


class TestSubmissionStatus(unittest.TestCase):
    def test_bool_is_primary_source(self):
        self.assertEqual("submitted", map_submission_status(True, ""))
        self.assertEqual("not_submitted", map_submission_status(False, ""))
        self.assertEqual("submitted", map_submission_status(True, None))
        self.assertEqual("submitted", map_submission_status("true", ""))

    def test_graded_upgrades_from_string(self):
        self.assertEqual("graded", map_submission_status(True, "graded"))

    def test_string_fallback_when_bool_missing(self):
        self.assertEqual("submitted", map_submission_status(None, "submitted"))
        self.assertEqual("submitted", map_submission_status(None, " Submitted "))
        self.assertEqual("not_submitted", map_submission_status(None, "unsubmitted"))

    def test_unknown_is_conservative(self):
        for submitted, status in ((None, "weird"), (None, ""), (None, None), (None, 3), ("weird", "已提交")):
            self.assertEqual("unknown", map_submission_status(submitted, status), repr((submitted, status)))


class TestResourceKind(unittest.TestCase):
    def test_extension_heuristic(self):
        self.assertEqual("slides", map_resource_kind("第01讲.ppt"))
        self.assertEqual("document", map_resource_kind("讲义.pdf"))
        self.assertEqual("scan", map_resource_kind("扫描件.JPG"))
        self.assertEqual("other", map_resource_kind("archive.zip"))
        self.assertEqual("other", map_resource_kind(None))


class TestCheckConnection(unittest.TestCase):
    def test_ok(self):
        adapter, transport = make_adapter([json_response({"page": 1, "courses": []})])
        state = adapter.check_connection()
        self.assertEqual("ok", state["state"])
        self.assertIsNone(state["action"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")
        self.assertEqual("POST", transport.calls[0]["method"])
        self.assertEqual("/api/my-courses", transport.calls[0]["path"])

    def test_http_200_html_infers_auth_required(self):
        adapter, _ = make_adapter([text_response("<html>SSO 登录页</html>")])
        state = adapter.check_connection()
        self.assertEqual("auth_required", state["state"])
        self.assertEqual(LOGIN_ENTRY_URL, state["action"]["entry_url"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_401_auth_required_with_action(self):
        adapter, _ = make_adapter([text_response("unauthorized", status=401)])
        state = adapter.check_connection()
        self.assertEqual("auth_required", state["state"])
        self.assertEqual("user_login", state["action"]["type"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_403_blocked_not_auth(self):
        adapter, _ = make_adapter([text_response("forbidden", status=403)])
        state = adapter.check_connection()
        self.assertEqual("blocked", state["state"])
        self.assertIn("403", state["detail"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_429_blocked(self):
        adapter, _ = make_adapter([text_response("too many", status=429)])
        state = adapter.check_connection()
        self.assertEqual("blocked", state["state"])
        self.assertIn("429", state["detail"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_503_network_error(self):
        adapter, _ = make_adapter([text_response("boom", status=503)])
        state = adapter.check_connection()
        self.assertEqual("network_error", state["state"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")

    def test_transport_exception_network_error(self):
        adapter, _ = make_adapter([TimeoutError("timeout")])
        state = adapter.check_connection()
        self.assertEqual("network_error", state["state"])
        assert_valid(self, SE_CONNECTION_STATE, state, "ConnectionState")


class TestCapabilities(unittest.TestCase):
    def test_shape_and_honesty(self):
        adapter, _ = make_adapter([])
        caps = adapter.get_capabilities()
        assert_valid(self, SE_CAPABILITIES, caps, "Capabilities")
        self.assertEqual(CONNECTOR_VERSION, caps["connector_version"])
        self.assertEqual("user_session", caps["auth"]["mode"])
        self.assertEqual("zjuam-cas", caps["auth"]["sso"])
        self.assertEqual("supported", caps["features"]["attachment_download"]["status"])  # A22 实测后升级
        self.assertEqual("supported", caps["features"]["assignments"]["status"])

    def test_implements_protocol(self):
        adapter, _ = make_adapter([])
        self.assertIsInstance(adapter, PlatformAdapter)


class TestListCourses(unittest.TestCase):
    def test_pagination_cursor_flow(self):
        adapter, transport = make_adapter([
            json_response({
                "start": 0, "end": 1, "page": 1, "page_size": 20, "pages": 2, "total": 3,
                "courses": [{"id": 1001, "name": "数据结构",
                             "semester": {"id": 3, "name": "2026-2027-1"},
                             "instructors": [{"id": 9, "name": "某老师"}],
                             "updated_at": "2026-10-08T09:10:00+08:00"}],
            }),
            json_response({
                "start": 1, "end": 3, "page": 2, "page_size": 20, "pages": 2, "total": 3,
                "courses": [{"id": 1002, "name": "操作系统"}, {"id": 1003, "name": "算法"}],
            }),
        ])
        first = adapter.list_courses()
        self.assertEqual(1, len(first["items"]))
        self.assertEqual("p2", first["next_cursor"])
        self.assertEqual([], first["errors"])
        course = first["items"][0]
        assert_valid(self, SE_COURSE, course, "Course")
        self.assertEqual("1001", course["id"])
        self.assertEqual("2026-2027-1", course["term"])
        self.assertEqual("某老师", course["teacher"])
        self.assertEqual("2026-10-08T01:10:00Z", course["updated_at"])
        self.assertEqual(OBSERVED_AT, course["created_at"])  # 缺省回退 observed_at

        second = adapter.list_courses(cursor=first["next_cursor"])
        self.assertEqual(2, len(second["items"]))
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(2, transport.calls[1]["json_body"]["page"])

    def test_instructors_and_semester_mapping(self):
        adapter, _ = make_adapter([
            json_response({"pages": 1, "courses": [{
                "id": 2001, "name": "课程",
                "instructors": [{"id": 9, "name": "张老师"}, {"id": 10, "name": "李老师"}],
                "semester": {"id": 3, "name": "2026-2027秋冬", "real_name": "2026-2027学年秋冬学期"},
            }]})
        ])
        course = adapter.list_courses()["items"][0]
        assert_valid(self, SE_COURSE, course, "Course")
        self.assertEqual("张老师、李老师", course["teacher"])
        self.assertEqual("2026-2027秋冬", course["term"])

    def test_item_errors_isolated(self):
        adapter, _ = make_adapter([
            json_response({"pages": 1, "courses": [{"id": 1001, "name": "正常"}, {"id": 1002}]})
        ])
        result = adapter.list_courses()
        self.assertEqual(1, len(result["items"]))
        self.assertEqual(1, len(result["errors"]))
        err = result["errors"][0]
        assert_valid(self, SE_ERROR, err, "Error")
        self.assertEqual("UPSTREAM_CHANGED", err["code"])
        self.assertEqual("1002", err["details"]["external_id"])

    def test_array_key_fallback(self):
        adapter, _ = make_adapter([json_response({"pages": 1, "data": [{"id": 7, "name": "课"}]})])
        result = adapter.list_courses()
        self.assertEqual(1, len(result["items"]))

    def test_invalid_cursor_rejected(self):
        adapter, _ = make_adapter([])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses(cursor="cur_unknown")
        self.assertEqual("VALIDATION_FAILED", ctx.exception.code)

    def test_term_rejected_with_reason(self):
        adapter, _ = make_adapter([])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses(term="2026-2027-1")
        self.assertEqual("VALIDATION_FAILED", ctx.exception.code)
        self.assertIn("A1 §5-4", ctx.exception.error["message"])


class TestSyncCourse(unittest.TestCase):
    def test_full_mapping_and_schema(self):
        adapter, transport = make_adapter([
            json_response({
                "pages": 1,
                "activities": [
                    courseware(7100001, "第01讲-绪论.pdf"),
                    courseware(7100002, "第02讲-线性表.pptx",
                               allow_download="0", origin_allow_download="1"),
                    courseware(7100003, "第03讲-栈.pdf",
                               allow_download="0", origin_allow_download="0"),
                    courseware(7100004, "已删除.pdf", deleted=True),
                    {"id": 7100005, "uploads": [{"id": 1, "deleted": False}]},  # 无标题/文件名 → 单项错误
                ],
            }),
            json_response({
                "pages": 1,
                "homework_activities": [
                    homework(7100001, "作业一"),
                    homework(7100002, "作业二", deadline=None, end_time=None,
                             submitted=None, submitted_status="weird"),
                ],
            }),
        ])
        result = adapter.sync_course("crs_1")
        assert_valid(self, SE_SYNC_RESULT, result, "SyncResult")
        self.assertEqual("resync", result["mode"])
        self.assertIsNone(result["cursor"]["previous"])
        self.assertTrue(result["cursor"]["next"].startswith("xzzd-full:"))
        self.assertEqual(OBSERVED_AT, result["observed_at"])

        resources = result["upserts"]["resources"]
        self.assertEqual(3, len(resources))
        by_id = {r["source_ref"]["external_id"]: r for r in resources}
        self.assertEqual("第01讲-绪论.pdf", by_id["7100001"]["title"])
        self.assertEqual("full", by_id["7100001"]["access_level"])
        self.assertEqual("document", by_id["7100001"]["kind"])
        self.assertEqual(1024, by_id["7100001"]["size_bytes"])
        self.assertEqual("application/pdf", by_id["7100001"]["mime_type"])
        self.assertEqual("preview_only", by_id["7100002"]["access_level"])
        self.assertEqual("slides", by_id["7100002"]["kind"])
        self.assertEqual("restricted", by_id["7100003"]["access_level"])
        self.assertIsNone(by_id["7100001"]["content_hash"])

        assignments = result["upserts"]["assignments"]
        self.assertEqual(2, len(assignments))
        self.assertEqual("submitted", assignments[0]["submission_status"])
        self.assertEqual("2026-10-20T15:59:00Z", assignments[0]["due_at"])
        self.assertEqual("datetime", assignments[0]["due_precision"])
        self.assertEqual("要求正文", assignments[0]["instructions"])
        self.assertEqual("unknown", assignments[1]["submission_status"])
        self.assertIsNone(assignments[1]["due_at"])
        self.assertEqual("unknown", assignments[1]["due_precision"])

        self.assertEqual([], result["upserts"]["recordings"])
        self.assertEqual(1, len(result["deletions"]))
        deletion = result["deletions"][0]
        self.assertEqual("resource", deletion["kind"])
        self.assertEqual("7100004", deletion["external_id"])
        self.assertEqual("explicit_delete_signal", deletion["reason"])
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual("7100005", result["errors"][0]["details"]["external_id"])
        self.assertEqual({"scanned": 7, "upserted": 5, "deleted": 1, "failed": 1}, result["stats"])

        self.assertEqual("/api/course/crs_1/coursewares", transport.calls[0]["path"])
        self.assertIn("conditions", transport.calls[0]["params"])
        self.assertEqual("/api/courses/crs_1/homework-activities", transport.calls[1]["path"])
        self.assertEqual("false", transport.calls[1]["params"]["reloadPage"])

    def test_platform_id_mapping(self):
        transport = ScriptedTransport([
            json_response({"pages": 1, "activities": []}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        adapter = XzzdAdapter(
            "conn_x", transport=transport, now=lambda: FIXED_NOW,
            resolve_external_id=lambda cid: "9001" if cid == "crs_1" else None,
        )
        result = adapter.sync_course("crs_1")
        self.assertEqual("crs_1", result["course_id"])  # 回显内部 ID
        self.assertEqual("/api/course/9001/coursewares", transport.calls[0]["path"])

        with self.assertRaises(AdapterError) as ctx:
            adapter.sync_course("crs_missing")
        self.assertEqual("RESOURCE_NOT_FOUND", ctx.exception.code)

    def test_empty_response_no_deletions(self):
        adapter, _ = make_adapter([
            json_response({"pages": 1, "activities": []}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        result = adapter.sync_course("crs_1")
        assert_valid(self, SE_SYNC_RESULT, result, "SyncResult")
        self.assertEqual([], result["deletions"])
        self.assertEqual([], result["errors"])
        self.assertEqual({"scanned": 0, "upserted": 0, "deleted": 0, "failed": 0}, result["stats"])

    def test_kinds_filter_skips_requests(self):
        adapter, transport = make_adapter([json_response({"pages": 1, "homework_activities": []})])
        result = adapter.sync_course("crs_1", kinds=["assignments"])
        self.assertEqual(1, len(transport.calls))
        self.assertEqual("/api/courses/crs_1/homework-activities", transport.calls[0]["path"])
        self.assertEqual([], result["upserts"]["resources"])
        self.assertEqual([], result["upserts"]["recordings"])
        assert_valid(self, SE_SYNC_RESULT, result, "SyncResult")

    def test_multi_page_merge(self):
        adapter, transport = make_adapter([
            json_response({"pages": 2, "activities": [courseware(1, "a.pdf")]}),
            json_response({"pages": 2, "activities": [courseware(2, "b.pdf")]}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        result = adapter.sync_course("crs_1")
        self.assertEqual(2, len(result["upserts"]["resources"]))
        # 两页分别以 page=1 / page=2 请求
        self.assertEqual(1, transport.calls[0]["params"]["page"])
        self.assertEqual(2, transport.calls[1]["params"]["page"])

    def test_pages_over_limit_raises(self):
        adapter, _ = make_adapter([json_response({"pages": 101, "activities": []})])
        with self.assertRaises(AdapterError) as ctx:
            adapter.sync_course("crs_1")
        self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)

    def test_since_cursor_echoed_previous(self):
        adapter, _ = make_adapter([
            json_response({"pages": 1, "activities": []}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        result = adapter.sync_course("crs_1", since_cursor="xzzd-full:prev")
        self.assertEqual("xzzd-full:prev", result["cursor"]["previous"])

    def test_activity_without_uploads_skipped(self):
        adapter, _ = make_adapter([
            json_response({"pages": 1, "activities": [
                {"id": 7200001, "title": "讨论活动", "type": "discussion"},
            ]}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        result = adapter.sync_course("crs_1")
        self.assertEqual([], result["upserts"]["resources"])
        self.assertEqual([], result["deletions"])
        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["stats"]["scanned"])

    def test_multi_upload_selection_and_deletion(self):
        deleted_file = {"id": 2, "name": "旧版.pdf", "deleted": True, "size": 10, "type": "application/pdf"}
        live_file = {"id": 3, "name": "新版.pdf", "deleted": False, "size": 20, "type": "application/pdf",
                     "allow_download": True, "origin_allow_download": True}
        adapter, _ = make_adapter([
            json_response({"pages": 1, "activities": [
                {"id": 7200002, "title": "多附件活动", "uploads": [deleted_file, live_file]},
                {"id": 7200003, "title": "全删活动", "uploads": [
                    {"id": 4, "name": "x.pdf", "deleted": True},
                    {"id": 5, "name": "y.pdf", "deleted": True},
                ]},
            ]}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        result = adapter.sync_course("crs_1")
        resources = result["upserts"]["resources"]
        self.assertEqual(1, len(resources))
        self.assertEqual("7200002", resources[0]["source_ref"]["external_id"])
        self.assertEqual(20, resources[0]["size_bytes"])
        self.assertEqual("full", resources[0]["access_level"])
        self.assertEqual(["7200003"], [d["external_id"] for d in result["deletions"]])

    def test_title_prefers_activity_title(self):
        adapter, _ = make_adapter([
            json_response({"pages": 1, "activities": [
                courseware(7300001, "L1.pdf", title="第01讲 绪论"),
            ]}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        result = adapter.sync_course("crs_1")
        resource = result["upserts"]["resources"][0]
        self.assertEqual("第01讲 绪论", resource["title"])
        self.assertEqual("document", resource["kind"])  # kind 由文件名扩展名判定


class TestFetchResource(unittest.TestCase):
    def _synced(self, *, download_dir=None, payloads=(), max_download_bytes=MAX_DOWNLOAD_BYTES,
                extra_responses=()):
        """合成同步：full（7100011）/ preview_only（7100012）/ restricted（7100013）。"""
        responses = [
            json_response({"pages": 1, "activities": [
                courseware(7100011, "可下载.pdf"),
                courseware(7100012, "仅预览.pdf", allow_download="0", origin_allow_download="1"),
                courseware(7100013, "受限.pdf", allow_download="0", origin_allow_download="0"),
            ]}),
            json_response({"pages": 1, "homework_activities": []}),
        ] + list(extra_responses)
        transport = DownloadFake(responses, payloads)
        adapter = XzzdAdapter(
            "conn_x", transport=transport, now=lambda: FIXED_NOW,
            download_dir=download_dir, max_download_bytes=max_download_bytes,
        )
        adapter.sync_course("crs_1")
        return adapter, transport

    def test_restricted_denial_not_raise(self):
        adapter, _ = self._synced()
        outcome = adapter.fetch_resource("7100013")
        assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
        self.assertEqual("denied", outcome["status"])
        self.assertEqual("permission_denied", outcome["denial"]["reason"])

    def test_preview_only_denial(self):
        adapter, _ = self._synced()
        outcome = adapter.fetch_resource("7100012")
        assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
        self.assertEqual("denied", outcome["status"])
        self.assertEqual("preview_only", outcome["denial"]["reason"])

    def test_unknown_id_gone(self):
        adapter, _ = self._synced()
        outcome = adapter.fetch_resource("no-such-id")
        assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
        self.assertEqual("gone", outcome["status"])

    def test_full_download_ok_end_to_end_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"PK-fake-zip-content" * 500
            adapter, transport = self._synced(
                download_dir=Path(tmp),
                payloads=[payload],
                extra_responses=[json_response({"url": SIGN_URL})],
            )
            outcome = adapter.fetch_resource("7100011")
            assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
            self.assertEqual("ok", outcome["status"])
            handle = outcome["handle"]
            self.assertEqual("local_file", handle["kind"])
            self.assertEqual("xzzd-7100011.pdf", handle["ref"])
            self.assertEqual(f"sha256:{hashlib.sha256(payload).hexdigest()}", handle["sha256"])
            self.assertEqual(len(payload), handle["size"])
            self.assertEqual("2026-10-10T12:00:00Z", handle["expires_at"])  # FIXED_NOW + 24h
            self.assertEqual(payload, (Path(tmp) / handle["ref"]).read_bytes())
            # 发签：GET 上传发签端点（upload id = 活动 id×10+1）
            sign_call = transport.calls[2]
            self.assertEqual("GET", sign_call["method"])
            self.assertEqual("/api/uploads/71000111/download-url-for-ai", sign_call["path"])
            # 下载：使用发签返回的绝对 URL 与 PD-1 护栏参数
            self.assertEqual(SIGN_URL, transport.download_calls[0]["url"])
            self.assertEqual(MAX_DOWNLOAD_BYTES, transport.download_calls[0]["max_bytes"])
            # 凭据红线：签名 URL（含 token）绝不进入返回值
            self.assertNotIn("SECRET-TOKEN", json.dumps(outcome))

    def test_fetch_twice_deterministic_ref_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"same-bytes"
            adapter, transport = self._synced(
                download_dir=Path(tmp),
                payloads=[payload, payload],
                extra_responses=[json_response({"url": SIGN_URL}), json_response({"url": SIGN_URL})],
            )
            first = adapter.fetch_resource("7100011")
            second = adapter.fetch_resource("7100011")
            self.assertEqual("ok", first["status"])
            self.assertEqual(first["handle"]["ref"], second["handle"]["ref"])
            self.assertEqual(first["handle"]["sha256"], second["handle"]["sha256"])
            self.assertEqual(payload, (Path(tmp) / first["handle"]["ref"]).read_bytes())

    def test_metadata_guardrail_denies_without_sign(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 合成 size=1024 > 护栏 512 → 不触网路（无发签、无下载调用）
            adapter, transport = self._synced(download_dir=Path(tmp), max_download_bytes=512)
            outcome = adapter.fetch_resource("7100011")
            assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
            self.assertEqual("denied", outcome["status"])
            self.assertEqual("permission_denied", outcome["denial"]["reason"])
            self.assertIn("文件超过下载大小限制", outcome["denial"]["message"])
            self.assertEqual(2, len(transport.calls))  # 仅两条同步请求
            self.assertEqual([], transport.download_calls)

    def test_midstream_guardrail_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 元数据 1024 ≤ 2048 通过前置护栏；实际载荷 4096 > 2048 触发中途护栏
            adapter, transport = self._synced(
                download_dir=Path(tmp), payloads=[b"x" * 4096], max_download_bytes=2048,
                extra_responses=[json_response({"url": SIGN_URL})],
            )
            outcome = adapter.fetch_resource("7100011")
            assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
            self.assertEqual("denied", outcome["status"])
            self.assertIn("文件超过下载大小限制", outcome["denial"]["message"])
            self.assertFalse((Path(tmp) / "xzzd-7100011.pdf").exists())

    def test_sign_403_maps_to_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, transport = self._synced(
                download_dir=Path(tmp),
                extra_responses=[json_response({"message": "您没有权限完成此操作"}, status=403)],
            )
            outcome = adapter.fetch_resource("7100011")
            assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
            self.assertEqual("denied", outcome["status"])
            self.assertEqual("permission_denied", outcome["denial"]["reason"])
            self.assertIn("403", outcome["denial"]["message"])
            self.assertIn("您没有权限完成此操作", outcome["denial"]["message"])
            self.assertEqual([], transport.download_calls)

    def test_sign_missing_url_upstream_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, _ = self._synced(download_dir=Path(tmp), extra_responses=[json_response({})])
            with self.assertRaises(AdapterError) as ctx:
                adapter.fetch_resource("7100011")
            self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)

    def test_download_channel_status_mapping(self):
        cases = [
            (403, "denied"),
            (404, "gone"),
            (410, "gone"),
        ]
        for status, expected_status in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                adapter, _ = self._synced(
                    download_dir=Path(tmp),
                    payloads=[DownloadError(f"HTTP {status}", status_code=status)],
                    extra_responses=[json_response({"url": SIGN_URL})],
                )
                outcome = adapter.fetch_resource("7100011")
                assert_valid(self, SE_FETCH_OUTCOME, outcome, "FetchOutcome")
                self.assertEqual(expected_status, outcome["status"])
                if expected_status == "denied":
                    self.assertEqual("permission_denied", outcome["denial"]["reason"])

        for status, code, retryable in [
            (401, "AUTH_EXPIRED", False),
            (419, "AUTH_EXPIRED", False),
            (429, "RATE_LIMITED", True),
            (503, "NETWORK_ERROR", True),
        ]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                adapter, _ = self._synced(
                    download_dir=Path(tmp),
                    payloads=[DownloadError(f"HTTP {status}", status_code=status)],
                    extra_responses=[json_response({"url": SIGN_URL})],
                )
                with self.assertRaises(AdapterError) as ctx:
                    adapter.fetch_resource("7100011")
                self.assertEqual(code, ctx.exception.code)
                self.assertEqual(retryable, ctx.exception.retryable)
                if code == "AUTH_EXPIRED":
                    self.assertEqual("user_login", ctx.exception.error["details"]["action"]["type"])

    def test_download_transport_exception_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, _ = self._synced(
                download_dir=Path(tmp), payloads=[ConnectionError("reset")],
                extra_responses=[json_response({"url": SIGN_URL})],
            )
            with self.assertRaises(AdapterError) as ctx:
                adapter.fetch_resource("7100011")
            self.assertEqual("NETWORK_ERROR", ctx.exception.code)
            self.assertTrue(ctx.exception.retryable)

    def test_download_dir_required_for_full(self):
        adapter, _ = self._synced(download_dir=None)
        with self.assertRaises(AdapterError) as ctx:
            adapter.fetch_resource("7100011")
        self.assertEqual("VALIDATION_FAILED", ctx.exception.code)

    def test_transport_without_download_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = ScriptedTransport([
                json_response({"pages": 1, "activities": [courseware(7100011, "可下载.pdf")]}),
                json_response({"pages": 1, "homework_activities": []}),
            ])
            adapter = XzzdAdapter("conn_x", transport=transport, now=lambda: FIXED_NOW,
                                  download_dir=Path(tmp))
            adapter.sync_course("crs_1")
            with self.assertRaises(AdapterError) as ctx:
                adapter.fetch_resource("7100011")
            self.assertEqual("VALIDATION_FAILED", ctx.exception.code)
            self.assertIn("流式下载", ctx.exception.error["message"])

    def test_missing_upload_id_upstream_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = DownloadFake([
                json_response({"pages": 1, "activities": [{
                    "id": 7100020, "title": "无id.pdf",
                    "uploads": [{"name": "无id.pdf", "size": 10,
                                 "allow_download": True, "origin_allow_download": True,
                                 "deleted": False}],
                }]}),
                json_response({"pages": 1, "homework_activities": []}),
            ])
            adapter = XzzdAdapter("conn_x", transport=transport, now=lambda: FIXED_NOW,
                                  download_dir=Path(tmp))
            adapter.sync_course("crs_1")
            with self.assertRaises(AdapterError) as ctx:
                adapter.fetch_resource("7100020")
            self.assertEqual("UPSTREAM_CHANGED", ctx.exception.code)

    def test_deleted_signal_invalidates_cache(self):
        adapter, _ = self._synced()
        self.assertEqual("denied", adapter.fetch_resource("7100012")["status"])
        # 第二次同步：7100012 变为显式删除 → 缓存失效 → gone（不再按 preview_only 判定）
        adapter._transport.responses.extend([
            json_response({"pages": 1, "activities": [
                courseware(7100012, "仅预览.pdf", deleted=True),
            ]}),
            json_response({"pages": 1, "homework_activities": []}),
        ])
        adapter.sync_course("crs_1")
        self.assertEqual("gone", adapter.fetch_resource("7100012")["status"])


class TestErrorMapping(unittest.TestCase):
    def test_401_with_action_detail(self):
        adapter, _ = make_adapter([text_response("x", status=401)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        err = ctx.exception
        self.assertEqual("AUTH_EXPIRED", err.code)
        self.assertFalse(err.retryable)
        self.assertEqual("user_login", err.error["details"]["action"]["type"])
        assert_valid(self, SE_ERROR, err.error, "Error")

    def test_419_auth_expired(self):
        adapter, _ = make_adapter([text_response("x", status=419)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        self.assertEqual("AUTH_EXPIRED", ctx.exception.code)

    def test_403_permission_denied_with_message(self):
        adapter, _ = make_adapter(
            [json_response({"message": "您没有权限完成此操作"}, status=403)]
        )
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        err = ctx.exception
        self.assertEqual("PERMISSION_DENIED", err.code)
        self.assertFalse(err.retryable)
        self.assertEqual(403, err.error["details"]["http_status"])
        self.assertEqual("您没有权限完成此操作", err.error["details"]["message"])
        assert_valid(self, SE_ERROR, err.error, "Error")

    def test_429_rate_limited_retryable(self):
        adapter, _ = make_adapter([text_response("x", status=429)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        self.assertEqual("RATE_LIMITED", ctx.exception.code)
        self.assertTrue(ctx.exception.retryable)

    def test_404_carries_platform_message(self):
        adapter, _ = make_adapter([json_response({"message": "未找到资源"}, status=404)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        err = ctx.exception
        self.assertEqual("RESOURCE_NOT_FOUND", err.code)
        self.assertEqual(404, err.error["details"]["http_status"])
        self.assertEqual("未找到资源", err.error["details"]["message"])
        assert_valid(self, SE_ERROR, err.error, "Error")

    def test_500_network_error_retryable(self):
        adapter, _ = make_adapter([text_response("x", status=500)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        self.assertEqual("NETWORK_ERROR", ctx.exception.code)
        self.assertTrue(ctx.exception.retryable)

    def test_transport_exception_wrapped(self):
        adapter, _ = make_adapter([ConnectionError("reset")])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        self.assertEqual("NETWORK_ERROR", ctx.exception.code)
        self.assertTrue(ctx.exception.retryable)

    def test_http_200_html_infers_auth_expired(self):
        adapter, _ = make_adapter([text_response("<html>登录</html>")])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_courses()
        self.assertEqual("AUTH_EXPIRED", ctx.exception.code)
        self.assertIn("SSO", ctx.exception.error["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
