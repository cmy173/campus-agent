"""场次详情接线使用合成响应；不声称已验证当前真实平台。"""
import pytest

from connectors.adapters.base import AdapterError
from connectors.adapters.zykt import ZyktAdapter, map_transcript
from connectors.tests.fakes import ScriptedTransport, json_response, text_response
from connectors.tests.test_zykt import catalogue_xml, catalogue_item


def test_detail_sync_uses_bound_platform_id_and_populates_duration(monkeypatch):
    monkeypatch.setattr("connectors.adapters.zykt.time.sleep", lambda _: None)
    transport = ScriptedTransport([
        text_response(catalogue_xml(catalogue_item("40011", "Lecture"))),
        json_response({"result": {"data": {"sub_id": "40011", "duration": "1800"}}}),
    ])
    adapter = ZyktAdapter("conn_zykt", transport=transport,
                          resolve_external_id=lambda _: "12345", detail_sync=True)
    result = adapter.sync_course("crs_internal")
    recording = result["upserts"]["recordings"][0]
    assert recording["duration_seconds"] == 1800
    assert "course_id=12345&sub_id=40011" in recording["canonical_url"]
    assert transport.calls[1]["params"] == {"course_id": "12345", "sub_id": "40011"}


def test_detail_auth_failure_does_not_become_unknown_duration_success(monkeypatch):
    monkeypatch.setattr("connectors.adapters.zykt.time.sleep", lambda _: None)
    transport = ScriptedTransport([
        text_response(catalogue_xml(catalogue_item("40011", "Lecture"))),
        json_response({}, status=401),
    ])
    adapter = ZyktAdapter("conn_zykt", transport=transport, detail_sync=True)
    with pytest.raises(AdapterError) as error:
        adapter.sync_course("12345")
    assert error.value.code == "AUTH_EXPIRED"


def test_transcript_rejects_invalid_timestamps():
    result = map_transcript({"list": {"all_content": [
        {"BeginSec": -1, "EndSec": 2, "Text": "negative"},
        {"BeginSec": 5, "EndSec": 2, "Text": "reversed"},
        {"BeginSec": float("inf"), "EndSec": 9, "Text": "infinite"},
        {"BeginSec": 0, "EndSec": 2, "Text": "valid"},
    ]}}, recording_id="40011")
    assert [s["text"] for s in result["segments"]] == ["valid"]
