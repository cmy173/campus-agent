"""Real multipart, snapshot and owned-download boundaries; no SDK/model calls."""
import hashlib
from pathlib import Path

from test_agent_gateway import gateway, conversation, turn, internal  # noqa: F401


def upload(client, cid, name='资料.md', data=b'upload-marker-731', key='upload-one'):
    return client.post(f'/api/v1/agent/conversations/{cid}/files',
                       files={'file': (name, data)}, headers={'Idempotency-Key': key})


def test_upload_selection_preview_download_and_owner(gateway):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    response = upload(client, cid)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item['sha256'] == hashlib.sha256(b'upload-marker-731').hexdigest()
    assert 'path' not in item and 'storage_name' not in item
    assert upload(client, cid).json()['id'] == item['id']
    assert upload(client, cid, data=b'changed').json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    base = f"/api/v1/agent/conversations/{cid}/files/{item['id']}"
    assert client.get(base + '/preview').json()['text'] == 'upload-marker-731'
    download = client.get(base + '/content')
    assert download.content == b'upload-marker-731' and 'attachment' in download.headers['Content-Disposition']
    sent = client.post(f'/api/v1/agent/conversations/{cid}/turns', headers={'Idempotency-Key': 'attached-turn'},
                       json={'text': '处理附件', 'attachment_ids': [item['id']]})
    assert sent.status_code == 202, sent.text
    assert sent.json()['attachment_ids'] == [item['id']]
    refs = runtime.files.references(sent.json())
    assert Path(refs[0]['path']).read_bytes() == b'upload-marker-731'
    assert client.post(f'/api/v1/agent/conversations/{cid}/turns', headers={'Idempotency-Key': 'attached-turn'},
                       json={'text': '处理附件'}).json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    other = conversation(client)
    assert client.get(base.replace(cid, other) + '/content').status_code == 404
    client.cookies.clear()
    assert client.get(base + '/content').status_code == 404


def test_publish_snapshot_survives_source_change_and_replay(gateway, tmp_path):
    client, runtime, tokens, _ = gateway
    cid, source = conversation(client), tmp_path / '成果.md'
    source.write_text('# 原始成果\noutput-marker-926', encoding='utf-8')
    tid = turn(client, cid)
    runtime.workspaces[tid] = tmp_path
    req = {'conversation_id': cid, 'call_id': 'child:publish-one', 'path': source.name}
    result = internal(client, tokens[tid], 'files/publish', req)
    assert result.status_code == 200, result.text
    item = result.json()
    source.write_text('new content', encoding='utf-8')
    assert internal(client, tokens[tid], 'files/publish', req).json() == item
    assert internal(client, tokens[tid], 'files/publish', req | {'path': 'different.md'}).json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    body = client.get(f"/api/v1/agent/conversations/{cid}/files/{item['id']}/content").content
    assert 'output-marker-926' in body.decode() and b'new content' not in body
    runtime.cancel(tid)
    assert internal(client, tokens[tid], 'files/publish', req).status_code == 403


def test_upload_limit_and_literal_html_preview(gateway, monkeypatch):
    client, runtime, _, _ = gateway
    monkeypatch.setattr('app.agent_bridge.files.MAX_UPLOAD_BYTES', 8)
    cid = conversation(client)
    assert upload(client, cid, data=b'123456789').status_code == 413
    assert not list(runtime.files.directory.iterdir())
    monkeypatch.setattr('app.agent_bridge.files.MAX_UPLOAD_BYTES', 1000)
    item = upload(client, cid, name='unsafe.html', data=b'<script>attack()</script>').json()
    base = f"/api/v1/agent/conversations/{cid}/files/{item['id']}"
    assert client.get(base + '/preview').json()['text'] == '<script>attack()</script>'
    assert 'attachment' in client.get(base + '/content?inline=true').headers['Content-Disposition']
    other = conversation(client)
    response = client.post(f'/api/v1/agent/conversations/{other}/turns', headers={'Idempotency-Key': 'foreign'},
                           json={'text': '错误附件', 'attachment_ids': [item['id']]})
    assert response.status_code == 404
