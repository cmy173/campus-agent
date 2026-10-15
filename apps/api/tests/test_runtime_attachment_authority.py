"""Per-turn attachment authority reaches the owned runner; never the shared directory."""
from __future__ import annotations

import io

import pytest

from app.agent_bridge.store import Store
from test_agent_gateway import gateway, conversation  # noqa: F401


def test_runner_input_and_signature_carry_only_this_turn_selected_attachments(gateway):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    uploaded = client.post(f'/api/v1/agent/conversations/{cid}/files',
                           headers={'Idempotency-Key': 'attach-one'},
                           files={'file': ('notes.txt', io.BytesIO(b'synthetic notes'), 'text/plain')})
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()['id']

    selected = client.post(f'/api/v1/agent/conversations/{cid}/turns',
                           headers={'Idempotency-Key': 'turn-with-attachment'},
                           json={'text': 'read the attached notes', 'attachment_ids': [file_id]})
    assert selected.status_code == 202, selected.text
    selected = selected.json()
    # A second conversation keeps this independent of the one-active-turn rule.
    plain_cid = conversation(client)
    plain = client.post(f'/api/v1/agent/conversations/{plain_cid}/turns',
                        headers={'Idempotency-Key': 'turn-without-attachment'},
                        json={'text': 'no attachment here'})
    assert plain.status_code == 202, plain.text
    plain = plain.json()

    with_attachment = runtime.store.turn(selected['id'])
    without_attachment = runtime.store.turn(plain['id'])
    paths = runtime._attachment_paths(with_attachment)
    assert len(paths) == 1 and paths[0].endswith('.txt')
    # The shared conversation-files directory itself is never granted.
    assert runtime._attachment_paths(without_attachment) == []
    assert all(not p.rstrip('\\/').endswith('conversation-files') for p in paths)

    configuration = {'provider': 'campus-model', 'model': 'fixture', 'provider_id': 'provider_a', 'key_revision': 1}
    runtime_settings = {'search': {}, 'skill_dirs': [], 'mcp_servers': []}
    first = runtime._runner_signature(configuration, runtime_settings, runtime.store.path.parent, paths)
    second = runtime._runner_signature(configuration, runtime_settings, runtime.store.path.parent, [])
    assert first != second, 'a turn with attachments must not reuse the attachment-free runner'
    # Key rotation also replaces the process without ever fingerprinting the key.
    rotated = runtime._runner_signature({**configuration, 'key_revision': 2}, runtime_settings, runtime.store.path.parent, paths)
    assert rotated != first


def test_attachment_authority_is_not_persisted_into_conversation_history(gateway):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    uploaded = client.post(f'/api/v1/agent/conversations/{cid}/files',
                           headers={'Idempotency-Key': 'attach-two'},
                           files={'file': ('private.txt', io.BytesIO(b'synthetic'), 'text/plain')})
    file_id = uploaded.json()['id']
    turn = client.post(f'/api/v1/agent/conversations/{cid}/turns',
                       headers={'Idempotency-Key': 'turn-attach-two'},
                       json={'text': 'summarize', 'attachment_ids': [file_id]}).json()
    body = client.get(f'/api/v1/agent/turns/{turn["id"]}').text
    assert 'conversation-files' not in body and file_id in body
