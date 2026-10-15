"""Do not report stopped until both the worker and its API mutation have ended."""
import threading
import time
import pytest

from app.agent_bridge.store import Store
from test_agent_gateway import gateway, conversation, turn, internal  # noqa: F401


@pytest.mark.parametrize('lock_prefix', ['', 'browser-call:'])
def test_cancel_waits_for_worker_and_original_inflight_lock(gateway, lock_prefix):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    worker_release, api_release, api_entered = threading.Event(), threading.Event(), threading.Event()
    worker = threading.Thread(target=lambda: worker_release.wait(5))
    runtime.threads[tid] = worker
    worker.start()
    original_lock = runtime.call_lock(lock_prefix + tid)
    def mutation():
        with original_lock:
            api_entered.set()
            api_release.wait(5)
    api_worker = threading.Thread(target=mutation)
    api_worker.start()
    assert api_entered.wait(2)
    try:
        response = client.post(f'/api/v1/agent/turns/{tid}/cancel')
        assert response.json()['status'] == 'stopping'
        assert internal(client, tokens[tid], 'model-lease', {'conversation_id': cid}).status_code == 403
        blocked = client.post(f'/api/v1/agent/conversations/{cid}/turns', json={'text': '不能先启动新工具'}, headers={'Idempotency-Key': 'new-while-stopping'})
        assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'TURN_IN_PROGRESS'
        runtime.store.finish(tid, 'succeeded', response='迟到的结果不能覆盖停止请求')
        assert runtime.store.turn(tid)['status'] == 'stopping'
        worker_release.set()
        worker.join(2)
        # Runner teardown removes this entry; the stop worker must retain the
        # original lock rather than recreating an unlocked object here.
        runtime.call_locks.pop(lock_prefix + tid, None)
        assert runtime.store.turn(tid)['status'] == 'stopping'
        api_release.set()
        api_worker.join(2)
        deadline = time.monotonic() + 2
        while runtime.store.turn(tid)['status'] == 'stopping' and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.store.turn(tid)['status'] == 'cancelled'
    finally:
        worker_release.set(); api_release.set()
        worker.join(5); api_worker.join(5)
        runtime.threads.pop(tid, None)


def test_restart_of_stopping_turn_is_interrupted_not_falsely_stopped(gateway):
    client, runtime, _, _ = gateway
    tid = turn(client, conversation(client))
    assert runtime.store.request_stop(tid, 'cancelled')
    recovered = Store(runtime.store.path)
    recovered.recover()
    assert recovered.turn(tid)['status'] == 'interrupted'


def test_incomplete_stop_can_retry_without_starting_new_work(gateway, monkeypatch):
    client, runtime, _, _ = gateway
    tid = turn(client, conversation(client))
    runtime.store.request_stop(tid, 'cancelled')
    runtime.store.event(tid, 'stop_incomplete', {'message': 'injected cleanup failure'})
    runtime.cancel(tid)
    assert runtime.store.turn(tid)['status'] == 'cancelled'
    assert runtime.store.events(tid)['items'][-1]['kind'] == 'stop_requested'
