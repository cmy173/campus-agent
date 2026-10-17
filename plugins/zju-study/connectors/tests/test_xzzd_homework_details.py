from connectors.adapters.xzzd import XzzdAdapter
from connectors.tests.fakes import ScriptedTransport, json_response


def test_empty_homework_list_description_is_filled_from_activity_details():
    transport = ScriptedTransport([
        json_response({'pages': 1, 'homework_activities': [{'id': 12, 'title': '第二次作业', 'data': {'description': None}}]}),
        json_response({'id': 12, 'data': {'description': '<p>请计算下面的积分。</p>'}}),
    ])
    result = XzzdAdapter('conn_x', transport=transport)._sync_homework('123', '2026-10-16T00:00:00Z')
    assert result[1][0]['instructions'] == '<p>请计算下面的积分。</p>'
    assert transport.calls[-1]['path'] == '/api/activities/12'
    assert not result[3]


def test_detail_failure_keeps_metadata_without_erasing_existing_body():
    transport = ScriptedTransport([
        json_response({'pages': 1, 'homework_activities': [{'id': 13, 'title': '第三次作业', 'data': {}}]}),
        json_response({'message': 'temporary'}, status=503),
    ])
    result = XzzdAdapter('conn_x', transport=transport)._sync_homework('123', '2026-10-16T00:00:00Z')
    assert result[1][0]['title'] == '第三次作业'
    assert 'instructions' not in result[1][0]
    assert result[3][0]['code'] == 'NETWORK_ERROR'


def test_existing_body_does_not_add_detail_requests():
    transport = ScriptedTransport([json_response({'pages': 1, 'homework_activities': [{'id': 14, 'title': '第一次作业', 'data': {'description': '题目正文'}}]})])
    result = XzzdAdapter('conn_x', transport=transport)._sync_homework('123', '2026-10-16T00:00:00Z')
    assert result[1][0]['instructions'] == '题目正文'
    assert len(transport.calls) == 1
