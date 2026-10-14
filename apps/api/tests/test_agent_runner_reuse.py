"""The owned runtime process is reused across a conversation's turns.

Rebuilding the whole profile (and every MCP child) before each turn made every turn
after the first pay a multi-second startup; see docs/agent-v2-performance.md. These
tests pin the reuse decision itself: same conversation + same profile = same process,
and every case that must replace the process really does.
"""
from __future__ import annotations

import io
import json
import time

import pytest

from app.agent_bridge import runtime as runtime_module
from app.agent_bridge.store import Store


class FakeStore:
    def __init__(self):
        self.bound = []
        self.grants = {}
        self.limits = type('Limits', (), {'max_output_tokens': 16384})()

    def bind_token(self, tid, token):
        self.bound.append((tid, token))
        return True

    def turn(self, tid):
        return {'id': tid, 'conversation_id': 'conv_reuse', 'status': 'running'}

    def conversation(self, cid, owner=None):
        return {'id': cid, 'settings': {'provider': {'id': 'provider_a'}, 'course_ids': []}}

    def grant(self, cid):
        token = 'grant-' + cid
        self.grants[cid] = token
        return token

    def granted(self, token):
        for cid, value in self.grants.items():
            if value == token:
                return self.conversation(cid)
        return None

    def revoke_grant(self, cid):
        self.grants.pop(cid, None)

    def events(self, tid, after=0):
        return {'items': []}


class FakeProcess:
    def __init__(self, pid=4242, stdout=''):
        self.pid = pid
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout)
        self.campus_owner = None
        self.alive = True

    def poll(self):
        return None if self.alive else 1

    def wait(self, timeout=None):
        self.alive = False
        return 0

    def terminate(self):
        self.alive = False


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    instance = runtime_module.Runtime(Store(tmp_path / 'journal.sqlite'), tmp_path / 'harness',
                                      'http://127.0.0.1:8931')
    instance.store = FakeStore()
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(FakeProcess(pid=5000 + len(spawned)))
        return spawned[-1]

    monkeypatch.setattr(runtime_module, 'spawn_owned', spawn)
    instance.spawned = spawned
    return instance


def acquire(instance, tid, cid='conv_reuse', signature='sig-a', token='tok-one', resume=None):
    return instance._acquire_runner(cid, tid, {'CAMPUS_BRIDGE_TOKEN': token}, signature, resume)


def test_second_turn_reuses_the_same_process_and_rebinds_its_token(runtime):
    first, spawned_first = acquire(runtime, 'turn_1')
    assert spawned_first and first['token'] == 'tok-one'

    second, spawned_again = acquire(runtime, 'turn_2')
    assert spawned_again is False, 'the second turn must not start another runner'
    assert second is first
    assert len(runtime.spawned) == 1
    # The warm process answers to the token it was started with, so this turn is bound to it.
    assert runtime.store.bound == [('turn_2', 'tok-one')]
    assert first['turns'] == 0

    runtime._retain_runner('conv_reuse', first['proc'])
    assert first['turns'] == 1


def test_changed_profile_replaces_the_process(runtime):
    first, _ = acquire(runtime, 'turn_1')
    second, spawned = acquire(runtime, 'turn_2', signature='sig-b')
    assert spawned is True and second['proc'] is not first['proc']
    assert len(runtime.spawned) == 2
    assert runtime.store.bound == [], 'a fresh process keeps its own token; nothing is rebound'


def test_dead_process_is_replaced(runtime):
    first, _ = acquire(runtime, 'turn_1')
    first['proc'].alive = False
    second, spawned = acquire(runtime, 'turn_2')
    assert spawned is True and second['proc'] is not first['proc']


def test_goal_resume_always_starts_a_clean_process(runtime):
    first, _ = acquire(runtime, 'turn_1')
    second, spawned = acquire(runtime, 'turn_2', resume={'goal': {'id': 'goal-1'}})
    assert spawned is True and second['proc'] is not first['proc']


def test_an_idle_conversation_loses_its_runner(runtime, monkeypatch):
    stale, _ = acquire(runtime, 'turn_1', cid='conv_old')
    monkeypatch.setattr(runtime_module, 'RUNNER_IDLE_SECONDS', 60.0)
    stale['last_used'] = time.monotonic() - 120
    fresh, spawned = acquire(runtime, 'turn_2', cid='conv_new')
    assert spawned is True
    assert 'conv_old' not in runtime.runners, 'a conversation idle past the limit is dropped'
    assert 'conv_new' in runtime.runners
    assert stale['proc'].alive is False


def test_the_oldest_conversation_is_evicted_past_the_live_cap(runtime, monkeypatch):
    monkeypatch.setattr(runtime_module, 'RUNNER_MAX_LIVE', 1)
    first, _ = acquire(runtime, 'turn_1', cid='conv_a')
    first['last_used'] = time.monotonic() - 5
    second, _ = acquire(runtime, 'turn_2', cid='conv_b')
    assert list(runtime.runners) == ['conv_b']
    assert first['proc'].alive is False and second['proc'].alive is True


def test_close_stops_warm_runners(runtime):
    first, _ = acquire(runtime, 'turn_1')
    runtime.close()
    assert runtime.runners == {}
    assert first['proc'].alive is False


def prewarm(runtime, cid='conv_reuse'):
    return runtime.prewarm(cid)


def test_prewarm_boots_the_runtime_and_the_first_turn_reuses_it(runtime, monkeypatch):
    runtime.home = runtime.store and runtime.home  # keep the fixture path
    monkeypatch.setattr(runtime, '_runtime_inputs',
                        lambda settings: ({'provider': 'p', 'model': 'm', 'key': None},
                                          {'search': {'key': None, 'base_url': '', 'model': ''}, 'skill_dirs': [],
                                           'mcp_servers': [], 'workspace': ''}, runtime.home))
    monkeypatch.setattr(runtime, '_runner_env', lambda cid, token, *rest: {'CAMPUS_BRIDGE_TOKEN': token})
    monkeypatch.setattr(runtime, '_runner_signature', lambda *rest: 'sig')
    spawned = []

    def spawn(*args, **kwargs):
        process = FakeProcess(pid=6000 + len(spawned),
                              stdout=json.dumps({'kind': 'prewarm', 'data': {'ms': 1}, 'turn': 1}) + '\n')
        spawned.append(process)
        return process

    monkeypatch.setattr(runtime_module, 'spawn_owned', spawn)
    prewarm(runtime)
    handle = runtime.runners['conv_reuse']
    assert handle['proc'] is spawned[0]
    assert handle['turns'] == 1, 'the prewarm consumed the first frame slot'
    assert '"prewarm": true' in spawned[0].stdin.getvalue()
    assert runtime.store.grants['conv_reuse'] == handle['token']

    # The next turn must reuse the booted process and be addressable by its grant token.
    reused, newly = acquire(runtime, 'turn_1', signature='sig', token='turn-token')
    assert newly is False and reused is handle
    assert runtime.store.bound == [('turn_1', handle['token'])]


def test_prewarm_failure_discards_the_process(runtime, monkeypatch):
    monkeypatch.setattr(runtime, '_runtime_inputs',
                        lambda settings: ({'provider': 'p', 'model': 'm', 'key': None},
                                          {'search': {'key': None, 'base_url': '', 'model': ''}, 'skill_dirs': [],
                                           'mcp_servers': [], 'workspace': ''}, runtime.home))
    monkeypatch.setattr(runtime, '_runner_env', lambda cid, token, *rest: {'CAMPUS_BRIDGE_TOKEN': token})
    monkeypatch.setattr(runtime, '_runner_signature', lambda *rest: 'sig')
    monkeypatch.setattr(runtime_module, 'spawn_owned',
                        lambda *a, **k: FakeProcess(pid=7001, stdout=''))  # EOF: boot never reported
    prewarm(runtime, 'conv_broken')
    assert runtime.runners == {}
    assert 'conv_broken' not in runtime.store.grants
