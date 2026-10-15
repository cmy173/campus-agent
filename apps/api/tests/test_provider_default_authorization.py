"""Normal use relies on saved provider configuration, never per-message approvals."""
import pytest
from sqlalchemy.orm import Session
from app import models as m
from app.agent_bridge import model
from app.errors import ApiError
from test_agent_gateway import gateway, internal, turn


def test_saved_enabled_provider_runs_without_conversation_consent(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    created = client.post('/api/v1/agent/conversations', json={'provider_id': 'provider_a'})
    assert created.status_code == 201
    cid = created.json()['id']
    seen = []
    monkeypatch.setattr(model, 'call_model', lambda db, settings, messages, max_tokens, tools: seen.append(messages) or {'content': 'ok', 'tool_calls': []})
    tid = turn(client, cid)
    for _ in range(3):
        response = internal(client, tokens[tid], 'model', {'conversation_id': cid, 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '明天的安排'}]}]})
        assert response.status_code == 200
    assert len(seen) == 3


def test_disable_persists_and_stops_existing_and_new_tasks(gateway):
    client, runtime, _, engine = gateway
    cid = client.post('/api/v1/agent/conversations', json={'provider_id': 'provider_a'}).json()['id']
    response = client.patch('/api/v1/ai/providers/provider_a', json={'enabled': False})
    assert response.status_code == 200 and response.json()['enabled'] is False
    assert client.post('/api/v1/agent/conversations', json={'provider_id': 'provider_a'}).json()['error']['code'] == 'PROVIDER_DISABLED'
    with Session(engine) as db:
        with pytest.raises(ApiError, match='已停用'):
            model.validated_config(db, runtime.store.conversation(cid)['settings'])
    assert client.patch('/api/v1/ai/providers/provider_a', json={'enabled': True}).status_code == 200
    with Session(engine) as db:
        assert model.validated_config(db, runtime.store.conversation(cid)['settings']).enabled is True


def test_new_external_recipient_cannot_inherit_old_key(gateway, monkeypatch):
    from app import secrets_store
    client, _, _, _ = gateway
    cleared = []
    monkeypatch.setattr(secrets_store, 'clear_key', lambda pid: cleared.append(pid))
    assert client.patch('/api/v1/ai/providers/provider_a', json={'base_url': 'https://new-provider.example/v1'}).status_code == 200
    assert cleared == ['provider_a']
