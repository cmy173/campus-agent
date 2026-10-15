"""Opt-in REAL model + actual API/Harness over an isolated synthetic workspace.

Only the saved, enabled official provider config/key is reused, read-only. No
personal campus records, browser profiles or school sessions enter these tasks.
This calls a paid model. Usage is observed, never invented from fixture counters.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from verify_real_scenarios import Session, ROOT, API


CASES = [
    ('courses', '列出本机课程库中2026秋的课程名称和ID，仅查询本机缓存。', ['campus_courses_list']),
    ('search_read', '在本机资料中检索“原始数据附件”，读取命中的完整原文，告诉我报告必须包含什么并引用片段ID。',
     ['campus_materials_search', 'campus_materials_read']),
    ('unknown_deadline', '查询本机缓存中合成实验课程的作业截止时间；未知就明确说明，不要用当前日期推断，也不要联网刷新。',
     ['campus_assignments_list']),
    ('public_fetch', '请实际访问 https://example.org/ ，给出页面标题及一句内容摘要，附实际访问来源链接。', ['web_fetch']),
    ('latest_notices', '主动联网搜索浙大最新校园通知，核对一条通知的发布日期并给出官方来源；无法查证就说明具体限制。不要登录、不要把登录页当通知。', ['web_search']),
    ('auth_gap', '我还没有连接学在浙大，请查一下有哪些课程资料；如需本人登录请说明，不要让我粘贴密码或声称已经读过课件。',
     ['campus_connections_status']),
    ('plan_chain', '读取本机合成实验课程的学习任务和长期可用时间，为明天19:00到20:00生成一个学习预览。只给预览不要应用；学校课表未核验要说明。',
     ['campus_tasks_list', 'campus_availability_read', 'campus_plans_preview']),
]


def provider_config():
    path = ROOT / 'var/personal.db'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('select id,name,base_url,model,protocol,timeout_seconds from provider_configs where enabled=1').fetchall()
    return next((dict(row) for row in rows if urlsplit(row['base_url']).hostname == 'api.deepseek.com'), None)


def visibility(home, cid):
    """Read only this synthetic session's native request headers, not model guesses."""
    for path in (home / 'agent/harness').rglob('*.jsonl'):
        with path.open(encoding='utf-8') as stream:
            first = json.loads(next(stream))
            if first.get('id') != cid:
                continue
            headers = [json.loads(line) for line in stream]
        definitions = [item['data']['header']['tools'] for item in headers if item.get('type') == 'request/header']
        names = lambda tools: [item['name'] for item in tools]
        return {'initial_visible_tools': names(definitions[0]) if definitions else [],
                'visible_tools': sorted({name for items in definitions for name in names(items)}),
                'initial_schema_bytes': len(json.dumps(definitions[0], ensure_ascii=False).encode()) if definitions else None,
                'context_window': next((item['data'].get('contextWindow') for item in headers if item.get('type') == 'request/context'), None)}
    return {'visible_tools': [], 'initial_visible_tools': [], 'initial_schema_bytes': None, 'context_window': None}


class SyntheticSession(Session):
    def prepare(self):
        self.home.mkdir(parents=True)
        for name in ('files', 'workspace', 'agent', 'campus', 'tmp'):
            (self.home / name).mkdir()
        env = dict(os.environ, PYTHONUTF8='1',
            STUDY_DATABASE_URL='sqlite:///' + (self.home / 'domain.sqlite').as_posix(),
            STUDY_FILES_DIR=str(self.home / 'files'), STUDY_SECRETS_DIR=str(ROOT / 'var/secrets'),
            CAMPUS_AGENT_HOME=str(self.home / 'agent'), CAMPUS_WORKSPACE_DIR=str(self.home / 'workspace'),
            CAMPUS_DATA_DIR=str(self.home / 'campus'), CAMPUS_BRIDGE_URL=self.base,
            STUDY_WORKER_ENABLED='0', STUDY_WORKER_INPROC='0',
            CAMPUS_MAX_MODEL_CALLS=str(self.model_budget), CAMPUS_MAX_TOOL_CALLS='40', CAMPUS_TURN_TIMEOUT_SECONDS='150',
            TEMP=str(self.home / 'tmp'), TMP=str(self.home / 'tmp'))
        (self.home / 'provider.json').write_text(json.dumps(self.provider), encoding='utf-8')
        seed = '''import json,os
from pathlib import Path
from app.db import Base,engine,SessionLocal
from app import models as m
Base.metadata.create_all(engine)
cfg=json.loads(Path(os.environ['STUDY_DATABASE_URL'].removeprefix('sqlite:///')).with_name('provider.json').read_text())
with SessionLocal() as db:
 db.add(m.ProviderConfig(**cfg)); db.add(m.Course(id='course_synthetic',title='合成实验课程',term='2026秋')); db.flush()
 db.add(m.Assignment(id='assignment_synthetic',course_id='course_synthetic',title='合成实验报告',due_precision='unknown',revision=1))
 db.add(m.LearningTask(id='task_synthetic',course_id='course_synthetic',title='准备实验报告材料',type='reading',estimated_minutes=45,due_precision='unknown',status='pending'))
 db.add(m.Availability(id=1,timezone='Asia/Shanghai',weekly_windows=[{'weekday':d,'start':'19:00','end':'21:00'} for d in range(1,8)],date_exceptions=[],daily_limit_minutes=120))
 db.add(m.Resource(id='resource_synthetic',course_id='course_synthetic',kind='document',title='合成实验要求',version=2,parse_status='ready'));db.flush()
 db.add(m.ContentChunk(id='chunk_synthetic',resource_id='resource_synthetic',resource_version=2,ordinal=0,text='实验报告需要方法说明与原始数据附件。',locator={'paragraph':1},provenance={'kind':'synthetic'}));db.commit()
'''
        subprocess.run([sys.executable, '-c', seed], cwd=API, env=env, check=True, capture_output=True)
        return env

    def conversation(self):
        response = self.client.post(self.base + '/api/v1/agent/conversations', json={'provider_id': self.provider['id']}, timeout=30)
        response.raise_for_status()
        self.conversation_id = response.json()['id']
        return self.conversation_id


def run_case(session, case):
    ident, prompt, expected = case
    start = time.monotonic()
    cid = session.conversation()
    response = session.client.post(session.base + f'/api/v1/agent/conversations/{cid}/turns',
             json={'text': prompt}, headers={'Idempotency-Key': 'audit-' + ident}, timeout=30)
    response.raise_for_status()
    tid = response.json()['id']
    events, cursor = [], 0
    while time.monotonic() - start < 175:
        view = session.client.get(session.base + f'/api/v1/agent/turns/{tid}', params={'after': cursor}, timeout=10).json()
        events.extend(view['events']['items']); cursor = view['events']['next_cursor']
        if view['status'] not in ('queued', 'running', 'stopping'):
            break
        time.sleep(.35)
    else:
        session.client.post(session.base + f'/api/v1/agent/turns/{tid}/cancel', json={}, timeout=15)
    calls, outcomes, visible, usage, types, retries = [], [], set(), [], {}, []
    for event in events:
        if event['kind'] != 'harness_event':
            continue
        native = event['data'].get('event', {})
        kind, data = native.get('type'), native.get('data', {})
        types[kind] = types.get(kind, 0) + 1
        if kind == 'tool/call': calls.append(data['name'])
        if kind == 'tool/result': outcomes.append({'is_error': bool(data.get('message', {}).get('isError')),
              'call_id': data.get('message', {}).get('toolCallId')})
        if kind == 'llm/retry': retries.append(data.get('failure', {}).get('code'))
        if 'usage' in data: usage.append(data['usage'])
    # Only synthetic task traces and public-page results are stored here.
    (session.home / f'{ident}-trace.json').write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf-8')
    journal = session.home / 'agent/journal.sqlite'
    with sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True) as db:
        model_calls = db.execute('select model_calls from turns where id=?', (tid,)).fetchone()[0]
    result = {'id': ident, 'mode': 'real-official-model/synthetic-domain', 'status': view['status'],
        'expected_tools': expected, 'actual_tools': calls, 'tool_outcomes': outcomes,
        'expected_tools_used': set(expected).issubset(calls), 'model_calls': model_calls,
        'elapsed_ms': round((time.monotonic() - start) * 1000), 'provider_usage': usage or None,
        'cost_currency': None, 'human_interventions': 0, 'error': view.get('error'),
        'response': view.get('response', ''), 'event_types': types, 'retries': retries}
    result.update(visibility(session.home, cid))
    print(json.dumps({k: v for k, v in result.items() if k not in ('response', 'event_types', 'tool_outcomes')}, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True, help='authorize paid official model calls on synthetic tasks')
    parser.add_argument('--case', choices=[case[0] for case in CASES], help='repeat just one diagnostic case')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/audit/transition-live-model.json')
    parser.add_argument('--model-calls', type=int, choices=range(1, 17), default=8)
    args = parser.parse_args()
    provider = provider_config()
    if not provider:
        raise SystemExit('No enabled official provider; no credential guessed or endpoint changed.')
    home = ROOT / '.test-tmp' / ('transition-live-' + uuid.uuid4().hex)
    session = SyntheticSession(home, 'transition')
    session.provider = provider
    session.model_budget = args.model_calls
    results = []
    try:
        env = session.prepare(); session.start(env)
        for case in CASES:
            if args.case and case[0] != args.case:
                continue
            results.append(run_case(session, case))
            if (results[-1].get('error') or {}).get('code') in {'PROVIDER_AUTH_FAILED', 'PROVIDER_KEY_MISSING', 'PROVIDER_DISABLED'}:
                break
    finally:
        session.stop()
        report = {'mode': 'real-official-model/synthetic-domain', 'model': provider['model'],
                  'model_budget': session.model_budget, 'scratch': str(home), 'no_personal_records': True, 'results': results}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('EVIDENCE=' + str(home), flush=True)
