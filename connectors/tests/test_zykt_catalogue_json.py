import json
import pytest
from connectors.adapters.zykt import ZyktAdapter, parse_catalogue_response
from connectors.adapters.base import AdapterError
from connectors.tests.fakes import ScriptedTransport, json_response


def test_observed_yii_json_catalogue_syncs_and_keeps_explicit_deletion():
    payload = {'success': True, 'result': {'code': 200, 'msg': '', 'data': [
        {'sub_id': '40011', 'title': '第一讲', 'start_at': '1665621600'},
        {'sub_id': '40012', 'title': '第二讲', 'sub_delete': '1'},
    ]}}
    transport = ScriptedTransport([json_response(payload)])
    result = ZyktAdapter('conn', transport=transport).sync_course('course')
    assert len(result['upserts']['recordings']) == 1
    assert result['upserts']['recordings'][0]['title'] == '第一讲'
    assert result['deletions'][0]['external_id'] == '40012'
    assert transport.calls[0]['params']['format'] == 'json'


def test_bom_and_grouped_json_are_supported():
    text = '\ufeff' + json.dumps({'success': True, 'result': {'data': [{'title': '九月', 'children': [{'sub_id': 1, 'sub_title': '第一讲'}]}]}})
    rows = parse_catalogue_response(text)
    assert rows[0]['sub_id'] == '1' and rows[0]['title'] == '第一讲'


@pytest.mark.parametrize('payload,code', [
    ({'success': False, 'result': {'code': 401}}, 'AUTH_EXPIRED'),
    ({'success': False, 'result': {'code': 500}}, 'UPSTREAM_CHANGED'),
    ({'success': True, 'result': {}}, 'UPSTREAM_CHANGED'),
])
def test_invalid_or_expired_envelope_is_not_empty_success(payload, code):
    with pytest.raises(AdapterError) as error:
        parse_catalogue_response(json.dumps(payload))
    assert error.value.code == code


def test_explicit_empty_list_is_valid():
    assert parse_catalogue_response('{"success":true,"result":{"code":200,"data":[]}}') == []
