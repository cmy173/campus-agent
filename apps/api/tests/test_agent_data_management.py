"""Synthetic UUID-isolated chat journal only; no live accounts, DB or runtime."""
from __future__ import annotations

import io
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_bridge.data_management import DataManagement
from app.agent_bridge.files import ConversationFiles
from app.agent_bridge.schedules import Scheduler, ScheduleCreate
from app.agent_bridge.store import Store
from app.errors import ApiError, error_response
from app.routers.agent import browser_owner
from app.routers.agent_data import install_agent_data_router


@pytest.fixture
def isolated(tmp_path):
    root = tmp_path / str(uuid.uuid4())
    root.mkdir()
    store = Store(root / 'agent' / 'journal.sqlite')
    runtime = SimpleNamespace(store=store, home=root / 'agent' / 'harness', files=ConversationFiles(store),
                              lock=threading.RLock(), conversation_locks={}, threads={}, processes={},
                              stop_threads={}, runners={})
    def discard(cid, process):
        runtime.runners.pop(cid, None)
        store.revoke_grant(cid)
    runtime._discard_runner = discard
    return root, runtime, DataManagement(runtime)


def chat(runtime, owner='owner-a', text='SYNTHETIC_HISTORY_SECRET', status='succeeded'):
    cid = runtime.store.create(owner, {'provider': {'id': 'synthetic'}, 'course_ids': [], 'send_to_provider': True})['id']
    turn, _ = runtime.store.enqueue(cid, str(uuid.uuid4()), text, None)
    if status != 'queued':
        if status == 'running':
            runtime.store.running(turn['id'])
        elif status == 'stopping':
            runtime.store.request_stop(turn['id'], 'cancelled')
        else:
            runtime.store.finish(turn['id'], status, response=text + '-response')
    return cid, turn['id']


def test_settings_owner_scoped_persistent_and_separate_from_runtime_settings(isolated):
    root, runtime, manager = isolated
    settings = root / 'agent' / 'settings.json'
    settings.write_text('{"search":{"api_key":"synthetic-kept-key"},"workspace":"synthetic"}', encoding='utf-8')
    original = settings.read_bytes()
    assert manager.settings('owner-a')['history_retention_days'] == 30
    manager.save_settings('owner-a', 0)
    assert DataManagement(runtime).settings('owner-a')['history_retention_days'] == 0
    assert manager.settings('owner-b')['history_retention_days'] == 30
    assert settings.read_bytes() == original
    view = manager.settings('owner-a')
    assert view['scope'] == 'owner_chat_history'
    assert view['expiry_execution'] == 'history_read_or_explicit_purge'
    assert {'api_keys', 'campus_session', 'learning_database', 'user_workspace_and_source_files'} <= set(view['preserved'])
    for invalid in (-1, 36501, True, '30', 3.5):
        with pytest.raises(ApiError):
            manager.save_settings('owner-a', invalid)


def test_transaction_erases_every_owned_journal_and_file_copy_preserves_other_owner(isolated):
    root, runtime, manager = isolated
    cid, tid = chat(runtime)
    other, other_tid = chat(runtime, 'owner-b', 'OTHER_OWNER_KEPT')
    store = runtime.store
    store.event(tid, 'tool_result', {'data': 'SYNTHETIC_HISTORY_SECRET'})
    store.cache_call(tid, 'call', {}, {'result': 'SYNTHETIC_HISTORY_SECRET'})
    store.grant(cid)
    store.save_auth_recovery(tid, {'pending_tool': {'name': 'synthetic'}, 'history': 'SYNTHETIC_HISTORY_SECRET'})
    with store.connection() as db:
        db.execute('INSERT INTO native_tool_leases VALUES (?,?,?)', (tid, 'native', 'synthetic'))
    scheduler = Scheduler(store, runtime, clock=lambda: 1000)
    runtime.schedules = scheduler
    spec = ScheduleCreate(title='synthetic', prompt='SYNTHETIC_HISTORY_SECRET', timing={'kind': 'after', 'time_zone': 'UTC', 'seconds': 60})
    scheduled = scheduler.create(cid, spec, request_key=cid + ':request')
    scheduler.create(other, spec, request_key=other + ':request')
    scheduler._materialize()
    # Supply an occurrence even before its due time to exercise the outbox copy.
    with store.connection() as db:
        db.execute('INSERT INTO schedule_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   ('synthetic-occurrence', scheduled['id'], 1, cid, 1060, 'synthetic', 'SYNTHETIC_HISTORY_SECRET', 'pending', None, None, 1000))
    source = root / 'user-workspace' / 'keep.txt'
    source.parent.mkdir()
    source.write_text('USER_SOURCE_KEPT', encoding='utf-8')
    file = runtime.files.save(cid, io.BytesIO(b'SYNTHETIC_HISTORY_SECRET'), 'input.txt', 'input', 'file')
    output = runtime.files.save(cid, io.BytesIO(b'SYNTHETIC_HISTORY_SECRET-output'), 'output.txt', 'output', 'output', str(source))
    keep = runtime.files.save(other, io.BytesIO(b'OTHER_OWNER_KEPT'), 'keep.txt', 'input', 'keep')
    file_path = runtime.files.get(cid, file['id'])[1]
    output_path = runtime.files.get(cid, output['id'])[1]
    keep_path = runtime.files.get(other, keep['id'])[1]
    home = runtime.home / cid
    (home / 'profile' / 'logs').mkdir(parents=True)
    (home / 'profile' / 'logs' / 'history.json').write_text('SYNTHETIC_HISTORY_SECRET', encoding='utf-8')
    (home / 'tmp').mkdir()
    (home / 'tmp' / 'scratch.txt').write_text('SYNTHETIC_HISTORY_SECRET', encoding='utf-8')
    other_home = runtime.home / other
    other_home.mkdir()
    (other_home / 'keep.txt').write_text('OTHER_OWNER_KEPT', encoding='utf-8')
    protected = [root / 'learning.sqlite', root / 'campus-session.json', root / 'api-keys.json']
    for path in protected:
        path.write_bytes(b'PROTECTED_SYNTHETIC_DATA')
    runtime.runners[cid] = {'proc': object()}
    receipt = manager.delete_conversation('owner-a', cid)
    assert receipt['cleanup_complete'] and receipt['deleted_count'] == 1
    assert not home.exists() and not file_path.exists() and not output_path.exists()
    assert other_home.exists() and keep_path.read_bytes() == b'OTHER_OWNER_KEPT'
    assert source.read_text(encoding='utf-8') == 'USER_SOURCE_KEPT'
    assert all(path.read_bytes() == b'PROTECTED_SYNTHETIC_DATA' for path in protected)
    assert cid not in runtime.runners
    with store.connection() as db:
        for table in ('conversations', 'turns', 'events', 'tool_calls', 'native_tool_leases', 'conversation_files', 'runtime_grants',
                      'auth_recovery', 'schedules', 'schedule_occurrences', 'schedule_requests', 'agent_data_cleanup'):
            # No selected conversation ID or history payload survives in any copy.
            rows = [list(row) for row in db.execute(f'SELECT * FROM {table}')]
            assert cid not in json.dumps(rows)
            if table not in ('schedules', 'schedule_requests'):
                assert 'SYNTHETIC_HISTORY_SECRET' not in json.dumps(rows)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert store.turn(other_tid)['conversation_id'] == other
    # secure_delete+VACUUM removes known prompt bytes from the controlled DB file.
    assert b'SYNTHETIC_HISTORY_SECRET-response' not in store.path.read_bytes()


def test_owner_isolation_single_and_all_history(isolated):
    _, runtime, manager = isolated
    cid, _ = chat(runtime)
    other, _ = chat(runtime, 'owner-b')
    with pytest.raises(ApiError) as failure:
        manager.delete_conversation('owner-b', cid)
    assert failure.value.status_code == 404
    assert runtime.store.conversation(cid, 'owner-a')['id'] == cid
    runtime.conversation_locks[cid] = threading.Lock()
    runtime.conversation_locks[cid].acquire()
    try:
        with pytest.raises(ApiError) as hidden_busy:
            manager.delete_conversation('owner-b', cid)
        assert hidden_busy.value.status_code == 404  # never reveal another owner's busy state
    finally:
        runtime.conversation_locks[cid].release()
    assert manager.delete_history('owner-a')['deleted_conversation_ids'] == [cid]
    assert runtime.store.conversation(other, 'owner-b')['id'] == other
    assert manager.delete_history('owner-a')['deleted_count'] == 0


@pytest.mark.parametrize('status', ['queued', 'running', 'stopping'])
def test_active_turn_refuses_all_deletion_before_any_changes(isolated, status):
    _, runtime, manager = isolated
    settled, _ = chat(runtime)
    active, tid = chat(runtime, status=status)
    with pytest.raises(ApiError) as failure:
        manager.delete_history('owner-a')
    assert failure.value.status_code == 409 and failure.value.code == 'HISTORY_ACTIVE'
    assert tid in failure.value.details['active_turn_ids']
    assert runtime.store.turn(tid)['status'] == status  # never auto-stop or lie
    assert len(runtime.store.conversations('owner-a')) == 2
    assert runtime.store.conversation(settled)['id'] == settled


def test_terminal_status_still_refuses_worker_or_session_lease(isolated):
    _, runtime, manager = isolated
    cid, tid = chat(runtime)
    runtime.threads[tid] = object()
    with pytest.raises(ApiError) as failure:
        manager.delete_conversation('owner-a', cid)
    assert failure.value.status_code == 409
    runtime.threads.clear()
    lock = runtime.conversation_locks[cid]
    lock.acquire()
    try:
        with pytest.raises(ApiError) as failure:
            manager.delete_conversation('owner-a', cid)
        assert failure.value.status_code == 409
    finally:
        lock.release()
    assert manager.delete_conversation('owner-a', cid)['cleanup_complete']


def test_expiry_uses_last_activity_owner_and_zero_means_long_term(isolated):
    _, runtime, manager = isolated
    expired, _ = chat(runtime)
    fresh, _ = chat(runtime)
    boundary, _ = chat(runtime)
    other, _ = chat(runtime, 'owner-b')
    moment = datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=30)
    with runtime.store.connection() as db:
        db.execute('UPDATE conversations SET updated_at=? WHERE id IN (?,?)', ((cutoff - timedelta(seconds=1)).isoformat(), expired, other))
        db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (cutoff.isoformat(), boundary))
    result = manager.purge_expired('owner-a', moment=moment)
    assert result['deleted_conversation_ids'] == [expired]
    assert {r['id'] for r in runtime.store.conversations('owner-a')} == {fresh, boundary}
    assert runtime.store.conversation(other, 'owner-b')
    manager.save_settings('owner-a', 0)
    assert manager.purge_expired('owner-a', moment=moment + timedelta(days=365))['deleted_count'] == 0
    assert manager.purge_expired('owner-a')['expiry_disabled'] is True


def test_lazy_history_read_purges_expired_without_background_timer(isolated):
    _, runtime, manager = isolated
    expired, _ = chat(runtime)
    fresh, _ = chat(runtime)
    with runtime.store.connection() as db:
        db.execute('UPDATE conversations SET updated_at=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), expired))
    assert runtime.store.conversation(expired)  # no imaginary background worker
    page = manager.history('owner-a')
    assert page['purge']['deleted_conversation_ids'] == [expired]
    assert [r['id'] for r in page['items']] == [fresh]


@pytest.mark.parametrize('unsafe', ['traversal', 'root-link', 'nested-link', 'file-link'])
def test_containment_and_symlinks_reject_before_journal_changes(isolated, unsafe):
    root, runtime, manager = isolated
    cid, _ = chat(runtime)
    outside = root / 'outside'
    outside.mkdir()
    protected = outside / 'keep.txt'
    protected.write_text('DO_NOT_TOUCH', encoding='utf-8')
    home = runtime.home / cid
    home.mkdir(parents=True)
    item = runtime.files.save(cid, io.BytesIO(b'SYNTHETIC'), 'file.txt', 'input', 'file')
    path = runtime.files.get(cid, item['id'])[1]
    try:
        if unsafe == 'traversal':
            with runtime.store.connection() as db:
                db.execute('UPDATE conversation_files SET storage_name=? WHERE id=?', ('../../outside/keep.txt', item['id']))
        elif unsafe == 'root-link':
            home.rmdir()
            home.symlink_to(outside, target_is_directory=True)
        elif unsafe == 'nested-link':
            (home / 'linked').symlink_to(outside, target_is_directory=True)
        else:
            path.unlink()
            path.symlink_to(protected)
    except OSError as error:
        pytest.skip(f'Symlink creation unavailable on this test host: {error}')
    with pytest.raises(ApiError) as failure:
        manager.delete_conversation('owner-a', cid)
    assert failure.value.code == 'DATA_PATH_UNSAFE'
    assert runtime.store.conversation(cid)['id'] == cid
    assert protected.read_text(encoding='utf-8') == 'DO_NOT_TOUCH'


def test_shared_storage_name_cannot_delete_another_owners_file(isolated):
    _, runtime, manager = isolated
    cid, _ = chat(runtime)
    other, _ = chat(runtime, 'owner-b')
    selected = runtime.files.save(cid, io.BytesIO(b'SYNTHETIC'), 'a.txt', 'input', 'a')
    protected = runtime.files.save(other, io.BytesIO(b'OTHER_OWNER_KEPT'), 'b.txt', 'input', 'b')
    protected_path = runtime.files.get(other, protected['id'])[1]
    with runtime.store.connection() as db:
        db.execute('UPDATE conversation_files SET storage_name=? WHERE id=?', (protected_path.name, selected['id']))
    with pytest.raises(ApiError) as failure:
        manager.delete_conversation('owner-a', cid)
    assert failure.value.code == 'DATA_PATH_UNSAFE'
    assert runtime.store.conversation(cid)['id'] == cid
    assert protected_path.read_bytes() == b'OTHER_OWNER_KEPT'


def test_reparse_point_is_rejected_even_when_host_cannot_create_symlinks(isolated, monkeypatch):
    root, runtime, manager = isolated
    cid, _ = chat(runtime)
    home = runtime.home / cid
    home.mkdir(parents=True)
    nested = home / 'simulated-junction'
    nested.mkdir()
    original = Path.lstat
    def metadata(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == nested:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', metadata)
    with pytest.raises(ApiError) as failure:
        manager.delete_conversation('owner-a', cid)
    assert failure.value.code == 'DATA_PATH_UNSAFE'
    assert runtime.store.conversation(cid)['id'] == cid
    assert home.exists()


def test_all_owner_history_has_no_legacy_hundred_conversation_limit(isolated):
    _, runtime, manager = isolated
    ids = [runtime.store.create('owner-a', {'course_ids': []})['id'] for _ in range(105)]
    other = runtime.store.create('owner-b', {'course_ids': []})['id']
    result = manager.delete_history('owner-a')
    assert result['deleted_count'] == 105 and set(result['deleted_conversation_ids']) == set(ids)
    assert runtime.store.conversations('owner-a') == []
    assert runtime.store.conversation(other, 'owner-b')['id'] == other


def test_failed_filesystem_cleanup_is_reported_and_can_retry(isolated, monkeypatch):
    _, runtime, manager = isolated
    cid, _ = chat(runtime)
    item = runtime.files.save(cid, io.BytesIO(b'SYNTHETIC'), 'file.txt', 'input', 'file')
    path = runtime.files.get(cid, item['id'])[1]
    real_unlink = Path.unlink
    def fail(target, *args, **kwargs):
        if target == path:
            raise PermissionError('synthetic controlled failure')
        return real_unlink(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(ApiError) as failure:
        manager.delete_conversation('owner-a', cid)
    assert failure.value.code == 'HISTORY_CLEANUP_INCOMPLETE'
    assert failure.value.details['cleanup_complete'] is False
    assert manager.settings('owner-a')['pending_cleanup_count'] == 1
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    assert manager.delete_conversation('owner-a', cid)['cleanup_complete'] is True
    assert not path.exists()
    assert manager.settings('owner-a')['pending_cleanup_count'] == 0


def test_installed_router_reuses_local_owner_origin_and_strict_settings(isolated, monkeypatch):
    _, runtime, _ = isolated
    monkeypatch.setattr('app.routers.agent_data.get_runtime', lambda: runtime)
    app = FastAPI()
    @app.exception_handler(ApiError)
    async def handle(request, error):
        return error_response(request, error.status_code, error.code, error.message, error.retryable, error.details)
    install_agent_data_router(app, browser_owner)
    prefix = '/api/v1/agent/data'
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get(prefix + '/settings', headers={'Origin': 'https://untrusted.test'}).status_code == 403
        value = client.get(prefix + '/settings')
        assert value.status_code == 200 and value.json()['history_retention_days'] == 30
        owner = client.cookies.get('campus_agent_owner')
        cid, _ = chat(runtime, owner)
        assert client.put(prefix + '/settings', json={'history_retention_days': 0}).status_code == 200
        assert client.get(prefix + '/history').json()['items'][0]['id'] == cid
        assert client.put(prefix + '/settings', json={'history_retention_days': True}).status_code == 422
        assert client.put(prefix + '/settings', json={'history_retention_days': 30, 'api_keys': []}).status_code == 422
        assert client.post(prefix + '/history/purge-expired', json={}).json()['expiry_disabled'] is True
        client.cookies.clear()
        assert client.delete(prefix + '/history/' + cid).status_code == 404
        assert client.delete(prefix + '/history').json()['deleted_count'] == 0
        client.cookies.set('campus_agent_owner', owner, domain='127.0.0.1', path='/api/v1/agent')
        assert client.delete(prefix + '/history/' + cid).json()['cleanup_complete'] is True
    with TestClient(app, base_url='http://remote.test') as remote:
        assert remote.get(prefix + '/settings').status_code == 403
