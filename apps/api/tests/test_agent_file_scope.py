"""Internal file delivery uses server authority, never model-supplied path grants.

All files, including outside-workspace fixtures, live in audit_regression's isolated
synthetic tree. No Harness process, browser, user files or network is involved.
"""
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.errors import ApiError
from test_agent_gateway import gateway, conversation, turn, internal  # noqa: F401


def active(gateway, attachments=None):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    if attachments is None:
        tid = turn(client, cid)
    else:
        response = client.post(f'/api/v1/agent/conversations/{cid}/turns',
                               headers={'Idempotency-Key': 'selected-turn'},
                               json={'text': 'use selected fixtures', 'attachment_ids': attachments})
        assert response.status_code == 202, response.text
        tid = response.json()['id']
    workspace = runtime.store.path.parent / 'workspace'
    workspace.mkdir(exist_ok=True)
    runtime.workspaces[tid] = workspace
    return client, runtime, cid, tid, tokens[tid], workspace


def publish(state, path, key='deliver', **extra):
    client, _, cid, _, token, _ = state
    return internal(client, token, 'files/publish',
                    {'conversation_id': cid, 'call_id': key, 'path': str(path), **extra})


def assert_denied(response):
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'SCOPE_DENIED'


@pytest.mark.parametrize('absolute', [False, True])
def test_workspace_delivery_public_download_and_immutable_replay(gateway, absolute):
    state = active(gateway)
    client, runtime, cid, _, _, workspace = state
    source = workspace / 'nested' / 'result.txt'
    source.parent.mkdir()
    source.write_text('synthetic original', encoding='utf-8')
    requested = source if absolute else Path('nested/result.txt')
    delivered = publish(state, requested)
    assert delivered.status_code == 200, delivered.text
    item = delivered.json()
    source.write_text('later changes', encoding='utf-8')
    assert publish(state, requested).json() == item
    source.unlink()
    assert publish(state, requested).json() == item  # snapshot survives source removal
    assert len(runtime.files.list(cid)) == 1
    base = f'/api/v1/agent/conversations/{cid}/files/{item["id"]}'
    assert client.get(base + '/content').content == b'synthetic original'
    assert client.get(base + '/preview').json()['text'] == 'synthetic original'
    stranger = TestClient(app, base_url='http://127.0.0.1')
    try:
        assert stranger.get(base + '/content').status_code == 404
    finally:
        stranger.close()
    other = workspace / 'other.txt'
    other.write_text('different fixture', encoding='utf-8')
    conflict = publish(state, other)
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('relative', [False, True])
def test_absolute_external_and_parent_traversal_rejected_before_read(gateway, monkeypatch, relative):
    state = active(gateway)
    _, runtime, cid, _, _, workspace = state
    outside = workspace.parent / 'outside.txt'
    outside.write_text('synthetic outside', encoding='utf-8')
    def no_read(*args, **kwargs):
        pytest.fail('out-of-scope file must not be opened or saved')
    monkeypatch.setattr(runtime.files, 'save', no_read)
    monkeypatch.setattr('app.agent_bridge.files.os.open', no_read)
    assert_denied(publish(state, Path('../outside.txt') if relative else outside))
    assert runtime.files.list(cid) == []


def test_similar_workspace_prefix_is_not_authority(gateway):
    state = active(gateway)
    *_, workspace = state
    outside = workspace.with_name(workspace.name + '-private') / 'fixture.txt'
    outside.parent.mkdir()
    outside.write_text('synthetic prefix sibling', encoding='utf-8')
    assert_denied(publish(state, outside))


@pytest.mark.parametrize('field', ['workspace', 'attachment_ids', 'authorized_paths', 'sandbox_permissions'])
def test_model_parameters_cannot_grant_path_authority(gateway, field):
    state = active(gateway)
    *_, workspace = state
    outside = workspace.parent / 'fixture.txt'
    outside.write_text('synthetic external', encoding='utf-8')
    assert publish(state, outside, **{field: str(outside)}).status_code == 400
    assert_denied(publish(state, outside, key='without-forged-field'))


@pytest.mark.parametrize('directory', [False, True])
def test_real_symlink_escape_and_cached_substitution_rejected(gateway, directory):
    state = active(gateway)
    *_, workspace = state
    outside = workspace.parent / 'outside'
    outside.mkdir()
    target = outside / 'fixture.txt'
    target.write_text('synthetic symlink target', encoding='utf-8')
    link = workspace / ('linked-dir' if directory else 'linked.txt')
    original = workspace / 'original.txt'
    original.write_text('initial snapshot', encoding='utf-8')
    assert publish(state, original).status_code == 200
    try:
        link.symlink_to(outside if directory else target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f'Host cannot create symlinks: {exc.winerror if hasattr(exc, "winerror") else exc.errno}')
    try:
        assert_denied(publish(state, link / 'fixture.txt' if directory else link, key='linked'))
        original.unlink()
        original.symlink_to(target)
        assert_denied(publish(state, original))  # cached ID does not waive scope checks
    finally:
        link.unlink()
        if original.is_symlink():
            original.unlink()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction/reparse-point integration')
def test_windows_junction_and_link_dotdot_are_rejected(gateway):
    state = active(gateway)
    *_, workspace = state
    outside = workspace.parent / 'junction-target'
    outside.mkdir()
    (outside / 'fixture.txt').write_text('synthetic junction target', encoding='utf-8')
    (workspace / 'fixture.txt').write_text('inside', encoding='utf-8')
    link = workspace / 'junction'
    # Native directory junctions do not require Windows symlink privileges. Inherited
    # stdio avoids subprocess pipe access under the restricted audit environment.
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)], check=True)
    try:
        assert_denied(publish(state, link / 'fixture.txt'))
        assert_denied(publish(state, link / '..' / 'fixture.txt', key='dotdot'))
    finally:
        assert link.parent == workspace and link.is_junction()
        link.rmdir()  # remove only our junction, never its synthetic target


def test_symlink_mode_rejected_even_when_target_would_be_inside_workspace(gateway, monkeypatch):
    state = active(gateway)
    *_, workspace = state
    linked = workspace / 'synthetic-link.txt'
    linked.write_text('inside fixture', encoding='utf-8')
    assert publish(state, linked).status_code == 200
    original_lstat = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == linked:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert_denied(publish(state, linked))  # existing immutable snapshot does not waive checks
    assert_denied(publish(state, linked, key='new-call'))


def test_legacy_cached_external_delivery_does_not_grant_authority(gateway):
    state = active(gateway)
    _, runtime, cid, tid, _, workspace = state
    outside = workspace.parent / 'legacy.txt'
    outside.write_text('synthetic legacy source', encoding='utf-8')
    with outside.open('rb') as source:
        runtime.files.save(cid, source, outside.name, 'output', 'publish:' + tid + ':deliver', str(outside))
    assert_denied(publish(state, outside))


def test_missing_and_non_file_workspace_sources_are_not_delivered(gateway):
    state = active(gateway)
    *_, workspace = state
    for source in [workspace / 'missing.txt', workspace]:
        response = publish(state, source)
        assert response.status_code == 404, response.text
        assert response.json()['error']['code'] == 'NOT_FOUND'


def test_opened_file_identity_mismatch_is_rejected_before_copy(gateway, monkeypatch):
    state = active(gateway)
    *_, workspace = state
    source = workspace / 'fixture.txt'
    source.write_text('synthetic fixture', encoding='utf-8')
    monkeypatch.setattr('app.agent_bridge.files.os.path.samestat', lambda *args: False)
    assert_denied(publish(state, source))


def test_reparse_component_rejected_without_following_it(gateway, monkeypatch):
    state = active(gateway)
    *_, workspace = state
    folder = workspace / 'reparse-fixture'
    folder.mkdir()
    (folder / 'fixture.txt').write_text('synthetic reparse file', encoding='utf-8')
    original_lstat = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == folder:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert_denied(publish(state, folder / 'fixture.txt'))


def test_public_upload_selected_attachment_copy_is_allowed_but_not_storage_root(gateway):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    base = f'/api/v1/agent/conversations/{cid}/files'
    def upload(name, key):
        response = client.post(base, files={'file': (name, b'synthetic upload', 'text/plain')},
                               headers={'Idempotency-Key': key})
        assert response.status_code == 201, response.text
        return response.json()
    selected = upload('selected.txt', 'selected')
    assert upload('selected.txt', 'selected') == selected
    unselected = upload('unselected.txt', 'unselected')
    other_cid = conversation(client)
    alien_response = client.post(f'/api/v1/agent/conversations/{other_cid}/files',
                                files={'file': ('alien.txt', b'other conversation')},
                                headers={'Idempotency-Key': 'alien'})
    assert alien_response.status_code == 201
    alien = alien_response.json()
    response = client.post(f'/api/v1/agent/conversations/{cid}/turns',
                           headers={'Idempotency-Key': 'selected-turn'},
                           json={'text': 'use attachment', 'attachment_ids': [selected['id']]})
    assert response.status_code == 202, response.text
    tid = response.json()['id']
    workspace = runtime.store.path.parent / 'workspace'
    workspace.mkdir()
    runtime.workspaces[tid] = workspace
    state = client, runtime, cid, tid, tokens[tid], workspace
    selected_path = runtime.files.get(cid, selected['id'])[1]
    delivered = publish(state, selected_path)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()['kind'] == 'output'
    assert publish(state, selected_path).json() == delivered.json()
    assert client.get(base + '/' + delivered.json()['id'] + '/content').content == b'synthetic upload'
    assert_denied(publish(state, runtime.files.get(cid, unselected['id'])[1], key='unselected'))
    assert_denied(publish(state, runtime.files.get(other_cid, alien['id'])[1], key='alien'))
    forged = runtime.files.directory / 'unregistered.txt'
    forged.write_text('synthetic unregistered storage neighbor', encoding='utf-8')
    assert_denied(publish(state, forged, key='unregistered'))
    # Even known snapshots do not become reusable sources without user selection.
    assert_denied(publish(state, runtime.files.get(cid, delivered.json()['id'])[1], key='output'))


def test_delivery_store_reparse_cannot_redirect_workspace_copy(gateway, monkeypatch):
    state = active(gateway)
    _, runtime, cid, _, _, workspace = state
    source = workspace / 'fixture.txt'
    source.write_text('synthetic workspace source', encoding='utf-8')
    runtime.files.directory.mkdir()
    original_lstat = Path.lstat
    def lstat(candidate, *args, **kwargs):
        if candidate == runtime.files.directory:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert_denied(publish(state, source))
    assert runtime.files.list(cid) == []


def test_selected_attachment_directory_reparse_rejected_for_publish_and_download(gateway, monkeypatch):
    state = active(gateway)
    client, runtime, cid, tid, _, _ = state
    import io
    item = runtime.files.save(cid, io.BytesIO(b'synthetic attachment'), 'input.txt', 'input', 'upload:fixture')
    _, path = runtime.files.get(cid, item['id'])
    with runtime.store.connection() as db:
        db.execute('UPDATE turns SET attachment_ids=? WHERE id=?', ('["' + item['id'] + '"]', tid))
    original_lstat = Path.lstat
    def lstat(candidate, *args, **kwargs):
        if candidate == runtime.files.directory:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert publish(state, path).status_code == 404
    assert client.get(f'/api/v1/agent/conversations/{cid}/files/{item["id"]}/content').status_code == 404
    with pytest.raises(ApiError) as error:
        runtime.files.get(cid, item['id'])
    assert error.value.code == 'NOT_FOUND'
