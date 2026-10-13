import json
from unittest.mock import patch
import pytest
from app.providers import call_chat_completions, run_connection_test, ProviderCallError


class Response:
    status_code = 200
    def __init__(self, message, finish='stop'):
        self.data = {'choices': [{'message': message, 'finish_reason': finish}]}
        self.content = json.dumps(self.data).encode()
    def json(self):
        return self.data


def test_deepseek_probe_disables_thinking_and_has_sufficient_output_budget():
    with patch('app.providers.requests.post', return_value=Response({'content': 'pong'})) as post:
        result = run_connection_test('https://api.deepseek.com/v1', 'deepseek-flash', 'test-key', 30)
    assert result['ok']
    assert post.call_args.kwargs['json']['thinking'] == {'type': 'disabled'}
    assert post.call_args.kwargs['json']['max_tokens'] == 8192


def test_reasoning_only_confirms_connectivity_but_is_not_a_generated_paper():
    with patch('app.providers.requests.post', return_value=Response({'content': '', 'reasoning_content': 'test reasoning'}, 'length')):
        assert run_connection_test('https://other.example/v1', 'reasoning-model', 'test-key', 30)['ok']
        with pytest.raises(ProviderCallError) as error:
            call_chat_completions('https://other.example/v1', 'reasoning-model', 'test-key', 30, [])
        assert error.value.code == 'PROVIDER_OUTPUT_TRUNCATED'


def test_empty_response_is_not_silently_accepted_and_generic_services_get_no_vendor_parameter():
    with patch('app.providers.requests.post', return_value=Response({'content': ''})) as post:
        result = run_connection_test('https://other.example/v1', 'model', 'test-key', 30)
    assert not result['ok'] and result['error']['code'] == 'PROVIDER_INVALID_RESPONSE'
    assert 'thinking' not in post.call_args.kwargs['json']
