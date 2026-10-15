"""Authentication recovery uses isolated SQLite, callback probes and fake SDK frames.

No test opens a browser, logs into campus, spawns Node or contacts a model/network.
"""
from __future__ import annotations

import copy
import io
import json
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models as m
from app.agent_bridge import model, recovery, runtime as runtime_module, tools
from app.agent_bridge.runtime import Runtime
from app.agent_bridge.store import Store
from app.db import Base
from app.errors import ApiError


def auth_error(code='AUTH_EXPIRED'):
    return {'status': 'error', 'data': {}, 'error': {'code': code, 'message': '学校会话失效', 'retryable': True}}


def success(label='pending-read'):
    return {'status': 'ok', 'data': {'records': [{'id': label}]}, 'error': None}


@pytest.fixture
def tmp_path():
    # Python 3.12 applies a restrictive Windows DACL to mkdir(mode=0700).
    # pytest's default temp factory uses that mode, which drops the sandbox
    # AppContainer grant. Ordinary directories inherit the existing scoped grant;
    # this does NOT edit permissions or access any path outside the workspace.
    root = Path(__file__).resolve().parents[3]
    path = root / ('.auth-resume-test-' + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        import shutil
        resolved = path.resolve()
        assert resolved.parent == root.resolve() and resolved.name.startswith('.auth-resume-test-')
        shutil.rmtree(resolved)


@pytest.fixture
def subject(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + (tmp_path / 'domain.sqlite').as_posix(), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        provider = m.ProviderConfig(id='provider_auth', name='合成', base_url='http://127.0.0.1:19191/v1', model='mock')
        db.add_all([provider, m.Course(id='course_a', title='课程A'), m.Course(id='course_b', title='课程B')])
        db.commit()
        settings = {'provider': model.snapshot(provider), 'send_to_provider': True, 'course_ids': ['course_a'],
                    'connection_ids': [], 'job_ids': [], 'current_course_id': 'course_a'}
    monkeypatch.setattr(runtime_module, 'SessionLocal', lambda: Session(engine))
    # Drive monitor synchronously except tests explicitly restoring the one worker.
    monkeypatch.setattr(recovery.AuthenticationRecovery, '_ensure_worker', lambda self: None)
    monkeypatch.setattr(runtime_module, 'spawn_owned', lambda *a, **k: pytest.fail('must not spawn a real runtime'))
    monkeypatch.setattr(tools, 'execute', lambda *a, **k: pytest.fail('must explicitly mock the pending read'))
    runtime = Runtime(Store(tmp_path / 'journal.sqlite'), tmp_path / 'harness', 'http://127.0.0.1:19190')
    cid = runtime.store.create('owner-auth', settings)['id']
    source, _ = runtime.store.enqueue(cid, 'source-request', '先读课表，再读成绩，汇总完整结果', None)
    runtime.store.running(source['id'])
    source = runtime.store.turn(source['id'])
    launches = []
    def start(turn, token):
        launches.append((turn, token))
        runtime.store.running(turn['id'])
    monkeypatch.setattr(runtime, 'start', start)
    runtime.test_cid, runtime.test_source, runtime.test_launches = cid, source, launches
    runtime.test_engine, runtime.test_settings = engine, settings
    yield runtime
    runtime.close()
    engine.dispose()


def defer(runtime, arguments=None, name='campus.query', code='AUTH_EXPIRED'):
    source = runtime.test_source
    arguments = arguments or {'capability': 'grades', 'query': {'term': '2026秋'}}
    result = auth_error(code)
    runtime.store.event(source['id'], 'tool_result', {'name': name, 'arguments': arguments, 'result': result})
    return runtime.defer_authentication(source, name, arguments, result)


def settle(runtime):
    runtime.store.finish(runtime.test_source['id'], 'succeeded', response='已取得课表，成绩需校园重新认证；无需重发任务。')


def checkpoint(runtime):
    return runtime.store.auth_recoveries(runtime.test_cid)[0]


def test_multistep_expired_auth_continues_once_with_preserved_results_and_same_cid(subject, monkeypatch):
    runtime = subject
    runtime.store.event(runtime.test_source['id'], 'tool_result',
                        {'name': 'campus.query', 'arguments': {'capability': 'courses'}, 'result': success('courses-complete')})
    goal_state = {'id': 'original-goal', 'objective': '汇总课表与成绩', 'revision': 2, 'phase': 'active'}
    runtime.store.event(runtime.test_source['id'], 'goal.state', {'goal': goal_state})
    saved = defer(runtime)
    assert saved['status'] == 'waiting_auth'
    probes = []
    def execute(name, arguments, db, ctx):
        probes.append((name, copy.deepcopy(arguments), ctx))
        return success('grades-complete')
    monkeypatch.setattr(tools, 'execute', execute)
    settle(runtime)
    runtime.auth_recovery.run_once()
    assert not probes and not runtime.test_launches, 'an auth failure itself must never request a probe'
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert len(probes) == len(runtime.test_launches) == 1
    assert probes[0][0:2] == ('campus.query', {'capability': 'grades', 'query': {'term': '2026秋'}})
    assert probes[0][2].course_ids == frozenset({'course_a'})
    turn, token = runtime.test_launches[0]
    assert token and turn['conversation_id'] == runtime.test_cid
    context = runtime.auth_resume_context(turn)
    assert context['original_goal'] == runtime.test_source['text']
    assert context['original_turn_id'] == runtime.test_source['id']
    assert context['original_goal_state'] == goal_state
    assert context['pending_result']['error']['code'] == 'AUTH_EXPIRED'
    assert context['pending_read_result'] == success('grades-complete')
    assert context['received_results'][0]['result'] == success('courses-complete')
    assert runtime.store.turn(runtime.test_source['id'])['response'].startswith('已取得课表')
    assert checkpoint(runtime)['status'] == 'resumed'
    assert runtime.recovery_tool_allowed(turn, 'campus.query')
    assert not runtime.recovery_tool_allowed(turn, 'campus.notice.import')
    assert not runtime.recovery_tool_allowed(turn, 'tasks.build')
    with runtime.store.connection() as db:
        assert db.execute('SELECT request_key FROM turns WHERE id=?', (turn['id'],)).fetchone()[0] == 'auth-resume:' + saved['id']


@pytest.mark.parametrize('code', sorted(recovery.AUTH_ERRORS))
def test_only_structured_auth_codes_checkpoint(subject, code):
    assert defer(subject, code=code)['status'] == 'waiting_auth'
    assert subject.defer_authentication(subject.test_source, 'campus.query', {'capability': 'grades'}, success()) is None
    assert subject.defer_authentication(subject.test_source, 'campus.query', {'capability': 'grades'},
                                       {'status': 'error', 'data': {'text': code}, 'error': None}) is None


def test_active_original_turn_and_owned_session_teardown_block_probe_and_launch(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    probes = []
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: probes.append(cp) or success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert not probes and not runtime.test_launches
    settle(runtime)
    runtime.threads[runtime.test_source['id']] = threading.current_thread()
    runtime.auth_recovery.run_once()
    assert not probes
    runtime.threads.clear()
    owner = runtime.conversation_locks[runtime.test_cid]
    owner.acquire()
    try:
        runtime.auth_recovery.run_once()
        assert not probes
    finally:
        owner.release()
    runtime.auth_recovery.run_once()
    assert len(probes) == len(runtime.test_launches) == 1


def test_still_expired_waits_for_new_auth_notification_and_persistent_30s_cooldown(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    clock = [1000.0]
    monkeypatch.setattr(recovery.time, 'time', lambda: clock[0])
    probes = []
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: probes.append(cp) or auth_error())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert len(probes) == 1 and checkpoint(runtime)['status'] == 'waiting_auth'
    assert checkpoint(runtime)['next_probe_at'] == 1030
    for _ in range(20):
        runtime.resume_authenticated_tasks()
        runtime.auth_recovery.run_once()
    assert len(probes) == 1
    clock[0] = 1031
    runtime.auth_recovery.run_once()
    assert len(probes) == 2
    clock[0] = 1200
    for _ in range(20):
        runtime.auth_recovery.run_once()
    assert len(probes) == 2, 'not a 30s network retry loop'
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: probes.append(cp) or success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert len(probes) == 3 and len(runtime.test_launches) == 1


def test_restart_waiting_checkpoint_probes_then_resumes_without_user_resend(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    runtime.close()
    reopened_store = Store(runtime.store.path)
    restarted = Runtime(reopened_store, runtime.home, runtime.bridge_url)
    # Same ordering as API startup: Runtime initializes then store.recover interrupts
    # abandoned original turns. They must not probe before that terminal transition.
    launches, probes = [], []
    monkeypatch.setattr(restarted, 'start', lambda turn, token: launches.append((turn, token)))
    monkeypatch.setattr(restarted, 'probe_authenticated_read', lambda cp: probes.append(cp) or success())
    try:
        restarted.auth_recovery.run_once()
        assert not probes
        reopened_store.recover()
        restarted.auth_recovery.run_once()
        assert reopened_store.turn(runtime.test_source['id'])['status'] == 'interrupted'
        assert len(probes) == len(launches) == 1
        restarted.auth_recovery.run_once()
        assert len(launches) == 1
    finally:
        restarted.close()


def test_ready_checkpoint_revalidates_pending_after_restart(subject, monkeypatch):
    runtime = subject
    cp = defer(runtime)
    settle(runtime)
    runtime.resume_authenticated_tasks()
    claim = runtime.store.claim_auth_recovery(cp['id'], recovery.time.time())
    runtime.store.settle_auth_probe(cp['id'], claim['claim_token'], result=success('stale-probe'), error=None, next_probe_at=0)
    runtime.close()
    restarted = Runtime(Store(runtime.store.path), runtime.home, runtime.bridge_url)
    launches, probes = [], []
    monkeypatch.setattr(restarted, 'start', lambda turn, token: launches.append(turn))
    monkeypatch.setattr(restarted, 'probe_authenticated_read', lambda cp: probes.append(cp) or auth_error('SESSION_EXPIRED'))
    try:
        restarted.auth_recovery.run_once()
        assert len(probes) == 1 and not launches
        assert restarted.store.auth_checkpoint(cp['id'])['status'] == 'waiting_auth'
    finally:
        restarted.close()


@pytest.mark.parametrize('name,args', [('campus.notice.import', {'record_id': 'notice', 'course_id': 'course_a'}),
                                    ('tasks.build', {'course_id': 'course_a'}), ('courses.list', {})])
def test_write_and_non_campus_tools_never_checkpoint_or_probe(subject, name, args):
    assert subject.defer_authentication(subject.test_source, name, args, auth_error()) is None
    assert subject.store.auth_recoveries() == []
    subject.resume_authenticated_tasks()
    subject.auth_recovery.run_once()
    assert subject.test_launches == []


def test_schema_effects_not_tool_name_decides_sync_replay(subject, monkeypatch):
    original = tools.schemas
    monkeypatch.setattr(tools, 'schemas', lambda: [s | {'effects': 'write'} if s['name'] == 'campus.sync' else s for s in original()])
    assert subject.defer_authentication(subject.test_source, 'campus.sync', {'capability': 'grades'}, auth_error()) is None
    monkeypatch.setattr(tools, 'schemas', lambda: [s | {'effects': 'read'} if s['name'] == 'campus.sync' else s for s in original()])
    assert subject.defer_authentication(subject.test_source, 'campus.sync', {'capability': 'grades'}, auth_error())['status'] == 'waiting_auth'


def test_cancelled_original_turn_is_never_resumed_after_auth(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    runtime.store.request_stop(runtime.test_source['id'], 'cancelled')
    runtime.store.complete_stop(runtime.test_source['id'])
    calls = []
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: calls.append(cp) or success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert not calls and not runtime.test_launches
    assert checkpoint(runtime)['status'] == 'cancelled'


def test_two_conversations_do_not_mix_pending_results_or_unauthorized_course_scope(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    other_settings = copy.deepcopy(runtime.test_settings)
    other_settings['course_ids'] = ['course_b']
    other_settings['current_course_id'] = 'course_b'
    other_cid = runtime.store.create('owner-auth', other_settings)['id']
    other_turn, _ = runtime.store.enqueue(other_cid, 'other-source', '只读另一会话课程', None)
    runtime.store.running(other_turn['id'])
    runtime.defer_authentication(other_turn, 'campus.query', {'capability': 'courses'}, auth_error())
    runtime.store.finish(other_turn['id'], 'succeeded', response='waiting')
    probes = []
    def execute(name, args, db, ctx):
        probes.append((ctx.conversation_id, ctx.course_ids))
        return success(ctx.conversation_id)
    monkeypatch.setattr(tools, 'execute', execute)
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert set(probes) == {(runtime.test_cid, frozenset({'course_a'})), (other_cid, frozenset({'course_b'}))}
    assert len(runtime.test_launches) == 2
    for turn, _ in runtime.test_launches:
        context = runtime.auth_resume_context(turn)
        assert context['pending_read_result']['data']['records'][0]['id'] == turn['conversation_id']
    assert {c['conversation_id'] for c in runtime.store.auth_recoveries(runtime.test_cid)} == {runtime.test_cid}


def test_later_resume_auth_expiry_preserves_original_goal_and_narrow_scope(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    continuation = runtime.test_launches[0][0]
    with runtime.store.connection() as db:
        settings = copy.deepcopy(runtime.test_settings)
        settings['course_ids'].append('course_b')
        db.execute('UPDATE conversations SET settings=? WHERE id=?', (json.dumps(settings), runtime.test_cid))
    second = runtime.defer_authentication(continuation, 'campus.query', {'capability': 'courses'}, auth_error())
    second = runtime.store.auth_checkpoint(second['id'])
    assert second['payload']['original_goal'] == runtime.test_source['text']
    assert second['payload']['original_turn_id'] == runtime.test_source['id']
    assert second['payload']['scope']['course_ids'] == ['course_a']
    assert second['source_turn_id'] == continuation['id']
    assert len(runtime.test_launches) == 1


def test_provider_disabled_stays_waiting_without_executing_pending_read(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    with Session(runtime.test_engine) as db:
        db.get(m.ProviderConfig, 'provider_auth').enabled = False
        db.commit()
    calls = []
    monkeypatch.setattr(tools, 'execute', lambda *a: calls.append(a) or success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert not calls and not runtime.test_launches
    assert checkpoint(runtime)['status'] == 'waiting_auth'
    assert checkpoint(runtime)['error']['code'] == 'PROVIDER_DISABLED'


def test_snapshot_scope_intersects_current_db_scope_and_never_expands_from_query(subject, monkeypatch):
    runtime = subject
    defer(runtime, arguments={'capability': 'grades', 'query': {'course_id': 'course_b'}})
    settle(runtime)
    with runtime.store.connection() as db:
        settings = copy.deepcopy(runtime.test_settings)
        settings['course_ids'].append('course_b')
        db.execute('UPDATE conversations SET settings=? WHERE id=?', (json.dumps(settings), runtime.test_cid))
    observed = []
    monkeypatch.setattr(tools, 'execute', lambda name, args, db, ctx: observed.append(ctx.course_ids) or success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert observed == [frozenset({'course_a'})]


def test_nested_auth_error_in_partial_probe_never_resumes(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: {'status': 'partial', 'error': None,
                        'data': {'error': {'code': 'AUTH_REQUIRED'}}})
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert checkpoint(runtime)['status'] == 'waiting_auth'
    assert checkpoint(runtime)['error']['code'] == 'AUTH_REQUIRED'
    assert not runtime.test_launches


def test_probe_failure_and_launch_failure_preserve_checkpoint(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    def fail(cp):
        raise OSError('authorization=never-persist-this-secret')
    monkeypatch.setattr(runtime, 'probe_authenticated_read', fail)
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    cp = checkpoint(runtime)
    assert cp['status'] == 'waiting_auth' and cp['payload']['original_goal']
    assert 'never-persist-this-secret' not in json.dumps(cp)
    with runtime.store.connection() as db:
        db.execute('UPDATE auth_recovery SET next_probe_at=0 WHERE id=?', (cp['id'],))
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: success())
    def fail_start(turn, token):
        raise OSError('mock launch failure')
    monkeypatch.setattr(runtime, 'start', fail_start)
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    assert checkpoint(runtime)['status'] == 'failed'
    assert checkpoint(runtime)['payload']['pending_tool']['name'] == 'campus.query'
    assert runtime.store.turn(checkpoint(runtime)['continuation_turn_id'])['status'] == 'failed'


def test_more_than_100_events_and_late_source_events_are_preserved_and_secrets_are_not(subject, monkeypatch):
    runtime = subject
    for i in range(120):
        runtime.store.event(runtime.test_source['id'], 'progress', {'step': i, 'cookie': 'secret-cookie',
                            'text': 'authorization=secret-bearer'})
    defer(runtime)
    runtime.store.event(runtime.test_source['id'], 'late_result', {'text': 'Harness original output retained'})
    settle(runtime)
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    cp = checkpoint(runtime)
    assert len(cp['payload']['received_events']) == 121
    assert 'secret-cookie' not in json.dumps(cp) and 'secret-bearer' not in json.dumps(cp)
    assert recovery.scrub('https://school.test/page?token=secret-url') == '[REDACTED URL]'
    context = runtime.auth_resume_context(runtime.test_launches[0][0])
    assert len(context['received_events']) == 122
    assert context['received_events'][-1]['kind'] == 'late_result'
    public = runtime.recovery_status(runtime.test_cid)
    assert 'original_goal' not in json.dumps(public) and 'pending_result' not in json.dumps(public)


def test_sensitive_or_invalid_pending_arguments_are_not_checkpointed(subject):
    assert subject.defer_authentication(subject.test_source, 'campus.query',
                                       {'capability': 'grades', 'query': {'password': 'secret'}}, auth_error()) is None
    assert subject.defer_authentication(subject.test_source, 'campus.query', {'capability': 'unknown'}, auth_error()) is None
    assert subject.store.auth_recoveries() == []


def test_claim_transaction_excludes_competing_monitor_and_enqueue_is_checkpoint_idempotent(subject):
    runtime = subject
    saved = defer(runtime)
    settle(runtime)
    runtime.resume_authenticated_tasks()
    first = runtime.store.claim_auth_recovery(saved['id'], 1000)
    assert first
    assert Store(runtime.store.path).claim_auth_recovery(saved['id'], 1001) is None
    assert not runtime.store.settle_auth_probe(saved['id'], 'wrong-claim', result=success(), error=None, next_probe_at=0)
    assert runtime.store.settle_auth_probe(saved['id'], first['claim_token'], result=success(), error=None, next_probe_at=0)
    ready = runtime.store.claim_auth_recovery(saved['id'], 1001)
    context = ready['payload'] | {'checkpoint_id': ready['id'], 'read_only': True}
    queued = runtime.store.enqueue_auth_recovery(saved['id'], ready['claim_token'], context)
    assert queued
    assert runtime.store.enqueue_auth_recovery(saved['id'], ready['claim_token'], context) is None
    assert len(runtime.store.turns(runtime.test_cid)) == 2


def test_checkpoint_deduplicates_repeated_failure_for_identical_pending_args(subject):
    first, second = defer(subject), defer(subject)
    assert first['id'] == second['id']
    assert len(subject.store.auth_recoveries()) == 1


def test_single_constant_worker_is_stopped_by_runtime_close(subject, monkeypatch):
    runtime = subject
    # Restore the real method saved below while keeping all callbacks mocked.
    monkeypatch.setattr(recovery.AuthenticationRecovery, '_ensure_worker', REAL_ENSURE_WORKER)
    for _ in range(25):
        defer(runtime)
    worker = runtime.auth_recovery.thread
    assert worker and worker.is_alive()
    for _ in range(25):
        runtime.resume_authenticated_tasks()
    assert runtime.auth_recovery.thread is worker
    runtime.close()
    assert not worker.is_alive()


REAL_ENSURE_WORKER = recovery.AuthenticationRecovery._ensure_worker


def test_resumed_sdk_frame_contains_trusted_context_same_official_history_without_goal_authority(subject, monkeypatch):
    runtime = subject
    defer(runtime)
    settle(runtime)
    monkeypatch.setattr(runtime, 'probe_authenticated_read', lambda cp: success())
    runtime.resume_authenticated_tasks()
    runtime.auth_recovery.run_once()
    turn, token = runtime.test_launches[0]
    # _run_owned expects queued; fake start used running. Reset only this synthetic
    # turn, then drive the actual payload path with a fake runner and output frame.
    with runtime.store.connection() as db:
        db.execute("UPDATE turns SET status='queued' WHERE id=?", (turn['id'],))
    process = type('Process', (), {})()
    process.stdin = io.StringIO()
    process.stdout = io.StringIO(json.dumps({'kind': 'result', 'turn': 1,
                                'data': {'conversation_id': runtime.test_cid, 'response': '原目标已汇总'}}) + '\n')
    process.poll = lambda: None
    handle = {'proc': process, 'turns': 0, 'capture': None}
    monkeypatch.setattr(runtime, '_runtime_inputs', lambda settings: ({'provider': 'mock', 'model': 'mock', 'key': None},
                        {'search': {'key': None, 'base_url': '', 'model': ''}, 'skill_dirs': [], 'mcp_servers': []}, runtime.home))
    monkeypatch.setattr(runtime, '_runner_env', lambda *a: {})
    monkeypatch.setattr(runtime, '_runner_signature', lambda *a: 'mock')
    monkeypatch.setattr(runtime, '_acquire_runner', lambda *a: (handle, False))
    monkeypatch.setattr(runtime, '_retain_runner', lambda *a: None)
    monkeypatch.setattr(runtime.browser, 'peek', lambda *a, **k: {'reviews': []})
    runtime._run_owned(turn, token)
    payload = json.loads(process.stdin.getvalue())
    assert payload['conversation_id'] == runtime.test_cid
    assert payload['home'] == str(runtime.home / runtime.test_cid)
    trusted = json.loads(payload['text'].split('\n')[1])
    assert trusted['resume_context']['checkpoint_id'] == checkpoint(runtime)['id']
    assert trusted['resume_context']['pending_read_result'] == success()
    assert not any(e['kind'] in ('accepted_action', 'accepted_goal_resume') for e in runtime.store.events(turn['id'])['items'])
    assert runtime.store.turn(turn['id'])['status'] == 'succeeded'
