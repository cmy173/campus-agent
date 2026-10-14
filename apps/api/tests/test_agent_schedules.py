"""Run the real background scheduler against the real persistent turn journal.

The executor is controlled to isolate scheduling from paid model latency. End-to-end
Harness tool and model execution is an additional integration gate, not claimed here.
"""
import json
import threading
import time
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.agent_bridge.schedules import Scheduler, ScheduleCreate, ScheduleUpdate, Timing, next_time
from app.agent_bridge.store import Store
from app.errors import ApiError


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.value


class Executor:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.lock = threading.RLock()
        self.fail = False

    def start(self, turn, token):
        if self.fail:
            raise RuntimeError('controlled startup failure')
        self.store.runtime(token, turn['conversation_id'])
        self.store.running(turn['id'])
        with self.lock:
            self.calls.append(turn)
        self.store.finish(turn['id'], 'succeeded', response='scheduler execution complete')

    def call_lock(self, tid):
        return self.lock


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), 'actual background scheduler did not reach expected state'


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / 'journal.sqlite')
    conversation = store.create('owner', {'course_ids': [], 'current_course_id': None})
    clock = Clock()
    executor = Executor(store)
    scheduler = Scheduler(store, executor, clock=clock, poll_seconds=.01)
    yield store, conversation['id'], clock, executor, scheduler
    scheduler.close()


def spec(kind='after', policy='latest', **values):
    return ScheduleCreate(title='检查通知', prompt='读取通知并整理截止时间',
                          timing=Timing(kind=kind, time_zone='Asia/Shanghai', **values), missed_policy=policy)


def test_real_thread_dispatch_and_receipt(setup):
    store, cid, clock, executor, scheduler = setup
    row = scheduler.create(cid, spec(seconds=2), request_key='create')
    assert row['next_local'] == '2026-10-15T08:00:02+08:00'
    assert scheduler.create(cid, spec(seconds=2), request_key='create') == row
    scheduler.start()
    clock.value += 3
    scheduler.wake.set()
    wait_for(lambda: len(executor.calls) == 1)
    history = scheduler.history(cid, row['id'])
    assert history[0]['turn_status'] == 'succeeded'
    assert history[0]['state'] == 'admitted'
    assert row['id'] in executor.calls[0]['text']
    clock.value += 1000
    scheduler.wake.set()
    scheduler.close()
    assert len(store.turns(cid)) == 1


def test_restart_latest_and_skip_real_threads(setup):
    store, cid, clock, executor, scheduler = setup
    latest = scheduler.create(cid, spec('every', seconds=60))
    skip = scheduler.create(cid, spec('every', policy='skip', seconds=60))
    clock.value += 301
    scheduler.start()
    wait_for(lambda: len(executor.calls) == 1)
    assert scheduler.history(cid, latest['id'])[0]['scheduled_at'].endswith('00:05:00+00:00')
    assert scheduler.history(cid, skip['id'])[0]['error_code'] == 'MISSED_SKIP'
    scheduler.close()
    restarted = Scheduler(store, executor, clock=clock, poll_seconds=.01)
    restarted.start()
    restarted.close()
    assert len(executor.calls) == 1
    assert len(store.turns(cid)) == 1


def test_busy_defers_same_occurrence_then_disable(setup):
    store, cid, clock, executor, scheduler = setup
    turn, token = store.enqueue(cid, 'ordinary', 'busy', None)
    row = scheduler.create(cid, spec(seconds=1))
    scheduler.start()
    clock.value += 2
    scheduler.wake.set()
    wait_for(lambda: scheduler.history(cid, row['id']) and scheduler.history(cid, row['id'])[0]['error_code'] == 'TURN_IN_PROGRESS')
    current = scheduler.list(cid)[0]
    scheduler.update(cid, row['id'], ScheduleUpdate(revision=current['revision'], enabled=False))
    store.finish(turn['id'], 'succeeded', response='ordinary finished')
    scheduler.close()
    assert scheduler.history(cid, row['id'])[0]['state'] == 'cancelled'
    assert not executor.calls


def test_admission_crash_gap_no_replay(setup):
    store, cid, clock, executor, scheduler = setup
    row = scheduler.create(cid, spec(seconds=1))
    def crash(occurrence, turn):
        scheduler.stopping.set()
        raise RuntimeError('simulated process loss after durable enqueue')
    scheduler.after_enqueue = crash
    scheduler.start()
    clock.value += 2
    scheduler.wake.set()
    wait_for(lambda: len(store.turns(cid)) == 1)
    scheduler.close()
    assert scheduler.history(cid, row['id'])[0]['state'] == 'dispatching'
    store.recover()
    restarted = Scheduler(store, executor, clock=clock, poll_seconds=.01)
    restarted.start()
    restarted.close()
    history = restarted.history(cid, row['id'])[0]
    assert history['state'] == 'admitted'
    assert history['turn_status'] == 'interrupted'
    assert history['error_code'] == 'RECOVERED_NO_REPLAY'
    assert len(store.turns(cid)) == 1
    assert not executor.calls


def test_before_enqueue_crash_gap_restores_pending(setup):
    store, cid, clock, executor, scheduler = setup
    row = scheduler.create(cid, spec(seconds=1))
    scheduler.start()
    scheduler.close()
    # Durable outbox at the point after materialization, before enqueue.
    with store.connection() as db:
        db.execute('INSERT INTO schedule_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   ('occ_controlled', row['id'], 1, cid, clock.value + 1, row['title'], row['prompt'], 'dispatching', None, None, clock.value))
        db.execute('UPDATE schedules SET enabled=0,next_at=NULL WHERE id=?', (row['id'],))
    restarted = Scheduler(store, executor, clock=clock, poll_seconds=.01)
    restarted.start()
    try:
        wait_for(lambda: len(executor.calls) == 1)
    finally:
        restarted.close()
    assert len(store.turns(cid)) == 1


def test_failed_start_never_replays(setup):
    store, cid, clock, executor, scheduler = setup
    executor.fail = True
    row = scheduler.create(cid, spec(seconds=1))
    scheduler.start()
    clock.value += 2
    scheduler.wake.set()
    wait_for(lambda: scheduler.history(cid, row['id']) and scheduler.history(cid, row['id'])[0].get('turn_status') == 'failed')
    scheduler.close()
    executor.fail = False
    restarted = Scheduler(store, executor, clock=clock, poll_seconds=.01)
    restarted.start()
    restarted.close()
    assert len(store.turns(cid)) == 1
    assert not executor.calls


def test_edit_cas_delete_and_authority(setup):
    store, cid, clock, executor, scheduler = setup
    row = scheduler.create(cid, spec('every', seconds=60))
    changed = scheduler.update(cid, row['id'], ScheduleUpdate(revision=1, enabled=False), request_key='edit')
    assert not changed['enabled']
    assert scheduler.update(cid, row['id'], ScheduleUpdate(revision=1, enabled=False), request_key='edit') == changed
    with pytest.raises(ApiError) as conflict:
        scheduler.update(cid, row['id'], ScheduleUpdate(revision=1, title='stale'))
    assert conflict.value.code == 'SCHEDULE_CONFLICT'
    other = store.create('other', {'course_ids': []})['id']
    with pytest.raises(ApiError) as denied:
        scheduler.delete(other, row['id'], 2)
    assert denied.value.status_code == 404
    deleted = scheduler.delete(cid, row['id'], 2)
    assert deleted['deleted'] and not scheduler.list(cid)
    assert scheduler.history(cid, row['id']) == []


def test_daily_weekly_dst_gap_overlap_and_offset():
    utc = lambda value: datetime.fromisoformat(value).timestamp()
    rule = {'kind': 'daily', 'time_zone': 'America/New_York', 'time': '02:30:00'}
    assert next_time(rule, utc('2026-03-08T00:00:00+00:00')) == utc('2026-03-09T06:30:00+00:00')
    rule['time'] = '01:30:00'
    earlier = next_time(rule, utc('2026-11-01T00:00:00+00:00'))
    assert earlier == utc('2026-11-01T05:30:00+00:00')
    assert next_time(rule, earlier) == utc('2026-11-02T06:30:00+00:00')
    weekly = {'kind': 'weekly', 'time_zone': 'Asia/Shanghai', 'time': '09:00:00', 'weekdays': [1]}
    assert next_time(weekly, utc('2026-10-15T00:00:00+00:00')) == utc('2026-10-19T01:00:00+00:00')


@pytest.mark.parametrize('timing', [
    dict(kind='daily', time_zone='invalid', time='09:00:00'),
    dict(kind='at', time_zone='Asia/Shanghai', at='2026-10-15T09:00:00'),
    dict(kind='every', time_zone='Asia/Shanghai', seconds=5),
    dict(kind='weekly', time_zone='Asia/Shanghai', time='09:00:00', weekdays=[1, 1]),
    dict(kind='after', time_zone='Asia/Shanghai', seconds=5, time='09:00:00'),
    dict(kind='daily', time_zone='Asia/Shanghai', time='9:00:00'),
])
def test_invalid_timing(timing):
    with pytest.raises(ValidationError):
        Timing(**timing)


def test_owned_http_and_runtime_tool_authority(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from app.routers import agent, agent_schedules
    store, cid, clock, executor, scheduler = setup
    cid = store.create('o' * 43, {'course_ids': [], 'current_course_id': None})['id']
    executor.schedules = scheduler
    monkeypatch.setattr(agent, 'get_runtime', lambda: executor)
    monkeypatch.setattr(agent_schedules, 'get_runtime', lambda: executor)
    app = FastAPI()
    app.include_router(agent_schedules.router, prefix='/api/v1')
    @app.exception_handler(ApiError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={'error': {'code': exc.code}})
    with TestClient(app, base_url='http://127.0.0.1', cookies={'campus_agent_owner': 'o' * 43}) as client:
        path = '/api/v1/agent/conversations/' + cid + '/schedules'
        created = client.post(path, json=spec(seconds=2).model_dump(), headers={'Idempotency-Key': 'http-create'})
        assert created.status_code == 200
        assert client.get(path).json()['items'][0]['id'] == created.json()['id']
        other = store.create('other', {'course_ids': []})['id']
        assert client.get('/api/v1/agent/conversations/' + other + '/schedules').status_code == 404
        turn, token = store.enqueue(cid, 'tool', 'create reminder', None)
        store.running(turn['id'])
        payload = {'conversation_id': cid, 'call_id': 'tool1', 'operation': 'create', 'arguments': spec(seconds=4).model_dump()}
        headers = {'Authorization': 'Bearer ' + token}
        tool = client.post('/api/v1/agent/internal/schedules', json=payload, headers=headers)
        assert tool.status_code == 200
        assert client.post('/api/v1/agent/internal/schedules', json=payload, headers=headers).json() == tool.json()
        invalid = dict(payload, call_id='invalid', arguments={'title': 'bad'})
        assert client.post('/api/v1/agent/internal/schedules', json=invalid, headers=headers).status_code == 400
        foreign = dict(payload, conversation_id=other)
        assert client.post('/api/v1/agent/internal/schedules', json=foreign, headers=headers).status_code == 403
        store.finish(turn['id'], 'cancelled')
        assert client.post('/api/v1/agent/internal/schedules', json=payload, headers=headers).status_code == 403
