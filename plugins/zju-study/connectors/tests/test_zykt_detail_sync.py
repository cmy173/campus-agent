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


def test_repeated_sync_reuses_shared_detail_cache_without_wait(monkeypatch):
    monkeypatch.setattr('connectors.adapters.zykt.time.sleep', lambda _: pytest.fail('cached sync waited'))
    shared = {}
    catalogue = catalogue_xml(catalogue_item('40011', 'Lecture'))
    first = ScriptedTransport([text_response(catalogue), json_response({'result': {'data': {'sub_id': '40011', 'duration': '1800'}}})])
    ZyktAdapter('conn', transport=first, detail_sync=True, detail_cache=shared).sync_course('12345')
    second = ScriptedTransport([text_response(catalogue)])
    result = ZyktAdapter('conn', transport=second, detail_sync=True, detail_cache=shared).sync_course('12345')
    assert result['upserts']['recordings'][0]['duration_seconds'] == 1800
    assert len(second.calls) == 1


def test_future_sessions_do_not_fetch_details_or_sleep(monkeypatch):
    import json
    from datetime import datetime, timezone
    monkeypatch.setattr('connectors.adapters.zykt.time.sleep', lambda _: pytest.fail('future session waited'))
    transport = ScriptedTransport([text_response(json.dumps({'list': [
        {'sub_id': 'future', 'title': 'Future', 'start_at': 1893456000}]}))])
    result = ZyktAdapter('conn', transport=transport, detail_sync=True,
                         now=lambda: datetime(2026, 10, 15, tzinfo=timezone.utc)).sync_course('12345')
    assert len(transport.calls) == 1
    assert result['upserts']['recordings'][0]['title'] == 'Future'


def test_expired_cache_fetches_again(monkeypatch):
    monkeypatch.setattr('connectors.adapters.zykt.time.sleep', lambda _: None)
    shared = {}
    catalogue = catalogue_xml(catalogue_item('40011', 'Lecture'))
    transport = ScriptedTransport([text_response(catalogue), json_response({'result': {'data': {'sub_id': '40011', 'duration': '1800'}}})])
    ZyktAdapter('conn', transport=transport, detail_sync=True, detail_cache=shared).sync_course('12345')
    key = next(iter(shared)); fingerprint, _, detail = shared[key]; shared[key] = (fingerprint, 0, detail)
    transport = ScriptedTransport([text_response(catalogue), json_response({'result': {'data': {'sub_id': '40011', 'duration': '1900'}}})])
    result = ZyktAdapter('conn', transport=transport, detail_sync=True, detail_cache=shared).sync_course('12345')
    assert len(transport.calls) == 2
    assert result['upserts']['recordings'][0]['duration_seconds'] == 1900
