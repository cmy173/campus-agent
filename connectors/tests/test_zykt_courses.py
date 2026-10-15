"""官网课程发现：合成响应验证，不代表真人账号联调。"""
from datetime import datetime, timezone
import unittest
from requests import Session
from unittest.mock import patch

from connectors.adapters.zykt import ZyktAdapter
from connectors.adapters.base import AdapterError
from connectors.browser.session_runner import ConnectorService
from connectors.tests.fakes import ScriptedTransport, json_response
from connectors.tests.validate_samples import validator

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def envelope(rows, **meta):
    return {"success": True, "result": {"err": 200, "data": {"models": rows, "_meta": meta}}}


class CourseDiscoveryTests(unittest.TestCase):
    def test_sparse_later_observation_does_not_erase_teacher(self):
        payload = {"code": 0, "list": [
            {"day": "2026-09-03", "course": [{"course_subject_id": "123", "title": "数学", "lecturer_name": "张老师"}]},
            {"day": "2026-09-04", "course": [{"course_subject_id": "123", "title": "数学", "lecturer_name": "李老师"}]},
            {"day": "2026-09-05", "course": [{"course_subject_id": "123", "title": "数学"}]},
        ]}
        adapter, _ = self.adapter(payload)
        assert adapter.list_courses()["items"][0]["teacher"] == "张老师、李老师"

    def adapter(self, *responses):
        transport = ScriptedTransport([json_response(r) for r in responses])
        return ZyktAdapter("conn_z", transport=transport, now=lambda: NOW), transport

    def test_current_months_dedup_and_exclude_future(self):
        payload = {"code": 0, "list": [
            {"day": "2026-09-03", "course": [{"course_subject_id": "123", "title": "数学"}]},
            {"day": "2026-09-04", "course": [{"course_subject_id": "123", "title": "数学"}]},
            {"day": "2026-10-12", "course": [{"course_subject_id": "456", "title": "尚未开课"}]},
        ]}
        adapter, transport = self.adapter(payload, {"code": 0, "list": []}, envelope([]))
        first = adapter.list_courses()
        self.assertEqual(["数学"], [r["title"] for r in first["items"]])
        self.assertEqual("123", first["items"][0]["source_refs"][0]["external_id"])
        self.assertEqual([], list(validator("course.schema.json").iter_errors(first["items"][0])))
        second = adapter.list_courses(first["next_cursor"])
        third = adapter.list_courses(second["next_cursor"])
        self.assertIsNone(third["next_cursor"])
        self.assertEqual({"month": "2026-9"}, transport.calls[0]["params"])
        self.assertEqual({"month": "2026-10"}, transport.calls[1]["params"])

    def test_all_history_pages_and_collection(self):
        adapter, transport = self.adapter(envelope([
            {"course_id": "old", "course_title": "历史课程", "learn_at": int(NOW.timestamp())}], pageCount=2),
            envelope([]), envelope([{"id": "collect", "title": "收藏课程"}]))
        first = adapter.list_courses("all:recent:1:", term="all")
        self.assertEqual("历史课程", first["items"][0]["title"])
        self.assertTrue(first["next_cursor"].startswith("all:recent:2:"))
        second = adapter.list_courses(first["next_cursor"], term="all")
        third = adapter.list_courses(second["next_cursor"], term="all")
        self.assertEqual("收藏课程", third["items"][0]["title"])
        self.assertIsNone(third["next_cursor"])
        self.assertIn("recent-learning", transport.calls[0]["path"])
        self.assertIn("course-collect", transport.calls[2]["path"])

    def test_recent_watch_time_cannot_identify_current_term(self):
        adapter, _ = self.adapter(envelope([
            {"course_id": "old", "course_title": "旧课程", "course_begin": 1665621600, "learn_at": int(NOW.timestamp())},
            {"course_id": "unknown", "course_title": "日期不明", "learn_at": int(NOW.timestamp())}]))
        result = adapter.list_courses("current:recent:1:")
        self.assertEqual([], result["items"])

    def test_month_failure_is_partial_not_empty_success(self):
        adapter, _ = self.adapter({"code": 999})
        result = adapter.list_courses()
        self.assertTrue(result["errors"])
        self.assertIsNotNone(result["next_cursor"])

    def test_auth_error_is_not_silent_empty_list(self):
        adapter, _ = self.adapter({"success": True, "result": {"err": 401}})
        with self.assertRaises(AdapterError) as caught:
            adapter.list_courses("all:recent:1:", term="all")
        self.assertEqual("AUTH_EXPIRED", caught.exception.code)

    def test_serialized_session_header_is_only_in_transport(self):
        session = Session()
        session.cookies.set("_token", '%7Bi%3A0%3Bs%3A6%3A%22_token%22%3Bi%3A1%3Bs%3A10%3A%22fake-value%22%3B%7D', domain="classroom.zju.edu.cn")
        service = ConnectorService(object())
        service.platform = "zykt"
        with patch("connectors.browser.session_runner.ZyktAdapter") as factory:
            service._make_adapter("conn_z", session)
        transport = factory.call_args.kwargs["transport"]
        self.assertEqual({"Accept": "application/json", "Authorization": "fake-value"}, transport._headers("/courseapi/v3/o-course/recent-learning"))
        self.assertEqual({"Accept": "application/json", "Authorization": "Bearer fake-value"}, transport._headers("/courseapi/v2/course-live/get-my-course-month"))
        self.assertEqual({}, transport._headers("/other"))
        self.assertEqual({"token": "fake-value"}, transport._params_for_path("/courseapi/v3/portal-home-setting/get-sub-info"))
        self.assertEqual({"token": "fake-value"}, transport._params_for_path("/courseapi/v3/web-socket/search-trans-result"))
        self.assertEqual({}, transport._params_for_path("/other"))
        self.assertNotIn("Authorization", session.headers)
        session.close()


if __name__ == "__main__":
    unittest.main()
