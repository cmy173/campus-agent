"""Actual child-process crash/restart plus the production background scheduler.

No paid model calls. The executor is controlled; dispatch goes through real Store
admission. Product Harness end-to-end execution is a distinct integration gate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'api'))
from app.agent_bridge.schedules import Scheduler, ScheduleCreate, Timing
from app.agent_bridge.store import Store


class Executor:
    def __init__(self, store, marker):
        self.store, self.marker = store, marker

    def start(self, turn, token):
        self.store.runtime(token, turn['conversation_id'])
        self.store.running(turn['id'])
        with self.marker.open('a', encoding='utf-8') as target:
            target.write(json.dumps({'turn_id': turn['id'], 'text': turn['text']}, ensure_ascii=False) + '\n')
        self.store.finish(turn['id'], 'succeeded', response='controlled executor completed')


def child(stage, home):
    store = Store(home / 'journal.sqlite')
    executor = Executor(store, home / 'executions.jsonl')
    clock = [1792022400.0]
    scheduler = Scheduler(store, executor, clock=lambda: clock[0], poll_seconds=.02)
    if stage == 'crash':
        cid = store.create('fixture-owner', {'course_ids': [], 'current_course_id': None})['id']
        once = scheduler.create(cid, ScheduleCreate(title='crash-gap', prompt='controlled occurrence', timing=Timing(kind='after', time_zone='Asia/Shanghai', seconds=2)))
        latest = scheduler.create(cid, ScheduleCreate(title='latest', prompt='controlled latest occurrence', timing=Timing(kind='every', time_zone='Asia/Shanghai', seconds=60)))
        skip = scheduler.create(cid, ScheduleCreate(title='skip', prompt='controlled skip occurrence', timing=Timing(kind='every', time_zone='Asia/Shanghai', seconds=60), missed_policy='skip'))
        (home / 'ids.json').write_text(json.dumps({'cid': cid, 'once': once['id'], 'latest': latest['id'], 'skip': skip['id']}), encoding='utf-8')
        scheduler.after_enqueue = lambda row, turn: os._exit(73)
        scheduler.start()
        clock[0] += 3
        scheduler.wake.set()
        time.sleep(5)
        raise AssertionError('scheduler never reached durable admission crash point')
    ids = json.loads((home / 'ids.json').read_text(encoding='utf-8'))
    store.recover()
    clock[0] += 301
    scheduler.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if scheduler.history(ids['cid'], ids['latest']) and scheduler.history(ids['cid'], ids['latest'])[0].get('turn_status') == 'succeeded':
            break
        time.sleep(.02)
    scheduler.close()
    result = {name: scheduler.history(ids['cid'], ids[name]) for name in ('once', 'latest', 'skip')}
    result['turns'] = store.turns(ids['cid'])
    assert result['once'][0]['error_code'] == 'RECOVERED_NO_REPLAY'
    assert result['once'][0]['turn_status'] == 'interrupted'
    assert len(result['latest']) == 1 and result['latest'][0]['turn_status'] == 'succeeded'
    assert len(result['skip']) == 1 and result['skip'][0]['state'] == 'skipped'
    assert len(result['turns']) == 2
    assert len((home / 'executions.jsonl').read_text(encoding='utf-8').splitlines()) == 1
    (home / 'evidence.json').write_text(json.dumps({'ok': True, 'executor': 'controlled, not paid model', 'actual_child_process_crash': 73, 'actual_background_scheduler': True, 'result': result}, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    if len(sys.argv) == 3:
        child(sys.argv[1], Path(sys.argv[2]))
        return
    home = Path(tempfile.mkdtemp(prefix='v2-scheduler-', dir=ROOT / 'var'))
    env = dict(os.environ, PYTHONUTF8='1', CAMPUS_AGENT_HOME=str(home),
               STUDY_DATABASE_URL='sqlite:///' + str(home / 'business.sqlite').replace('\\', '/'),
               STUDY_FILES_DIR=str(home / 'files'), STUDY_SECRETS_DIR=str(home / 'secrets'))
    first = subprocess.run([sys.executable, __file__, 'crash', str(home)], env=env, timeout=8, capture_output=True, text=True)
    assert first.returncode == 73, first.stderr
    second = subprocess.run([sys.executable, __file__, 'restart', str(home)], env=env, timeout=8, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    print(json.dumps({'ok': True, 'evidence': str(home / 'evidence.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
