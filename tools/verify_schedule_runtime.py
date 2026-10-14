"""Real product FastAPI -> Harness scheduling tool -> background scheduler -> Harness.

Only model responses are controlled. Real SDK tools write/read/publish a real file;
real API restart proves the same occurrence is not admitted or executed twice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'apps' / 'api'
sys.path.insert(0, str(API))
from app.process_owner import spawn_owned


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def verify():
    (ROOT / 'var').mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='v2-schedule-runtime-', dir=ROOT / 'var'))
    receipts = []
    expected = '真实 Harness 定时成果 849\n'
    filename = 'scheduled-result849.md'

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            receipts.append(body)
            messages = body['messages']
            boundary = next(m for m in reversed(messages) if m['role'] == 'user' and '用户消息：\n' in m.get('content', ''))
            results = [m['content'] for m in messages[messages.index(boundary) + 1:] if m['role'] == 'tool']
            calls, content = [], None
            def call(name, arguments):
                return [{'id': 'schedule_probe_' + str(len(receipts)), 'type': 'function',
                         'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]
            if '[SCHEDULE_EXEC_849]' not in boundary['content']:
                if not results:
                    calls = call('schedule_create', {'title': '定时资料整理', 'prompt': '[SCHEDULE_EXEC_849] 创建真实 Markdown 文件并交付给我。',
                                 'timing': {'kind': 'after', 'time_zone': 'Asia/Shanghai', 'seconds': 2}, 'missed_policy': 'latest'})
                else:
                    task = json.loads(results[-1])
                    assert task['execution_scope'] == 'application_running'
                    content = '已创建定时任务：' + task['id'] + '；执行时间：' + task['next_local'] + '；时区：' + task['timing']['time_zone'] + '；错过策略：' + task['missed_policy']
            elif not results:
                calls = call('write', {'file_path': filename, 'content': expected})
            elif len(results) == 1:
                calls = call('read', {'file_path': filename})
            elif len(results) == 2:
                assert '849' in results[-1], results[-1]
                calls = call('publish_file', {'path': filename})
            else:
                published = json.loads(results[-1])
                assert published['name'] == filename
                content = '定时任务已完成，真实文件已生成并交付：' + published['name']
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls},
                                                    'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, PYTHONUTF8='1', STUDY_DATABASE_URL='sqlite:///' + (home / 'domain.sqlite').as_posix(),
               STUDY_FILES_DIR=str(home / 'files'), STUDY_SECRETS_DIR=str(home / 'secrets'),
               CAMPUS_AGENT_HOME=str(home / 'agent'), CAMPUS_BRIDGE_URL=base,
               CAMPUS_WORKSPACE_DIR=str(home / 'workspace'), STUDY_WORKER_INPROC='0')
    seed = """
from app import models as m
from app.db import Base, engine, SessionLocal
Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(m.ProviderConfig(id='provider_schedule_fixture', name='受控调度模型', base_url=__URL__, model='schedule-fixture'))
    db.commit()
""".replace('__URL__', repr(f'http://127.0.0.1:{provider.server_port}/v1'))
    subprocess.run([sys.executable, '-c', seed], cwd=API, env=env, check=True)
    log = (home / 'api.log').open('w', encoding='utf-8')
    client, api = requests.Session(), None

    def start_api():
        process = spawn_owned([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                              cwd=API, env=env, stdout=log, stderr=log,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, (home / 'api.log').read_text(encoding='utf-8')
            try:
                if client.get(base + '/api/v1/health', timeout=1).ok:
                    return process
            except requests.RequestException:
                pass
            time.sleep(.1)
        process.campus_owner.close()
        raise AssertionError('API startup timed out')

    def stop_api():
        if api is not None:
            api.campus_owner.close()
            api.wait(timeout=15)

    def get(path):
        response = client.get(base + '/api/v1/agent' + path, timeout=10)
        assert response.ok, response.text
        return response.json()

    def post(path, data, key=None):
        response = client.post(base + '/api/v1/agent' + path, json=data,
                               headers={'Idempotency-Key': key} if key else {}, timeout=15)
        assert response.ok, response.text
        return response.json()

    def wait_turn(tid):
        deadline, items, cursor = time.monotonic() + 100, [], 0
        while time.monotonic() < deadline:
            response = client.get(base + '/api/v1/agent/turns/' + tid, params={'after': cursor}, timeout=10)
            assert response.ok, response.text
            view = response.json()
            items.extend(view['events']['items'])
            cursor = view['events']['next_cursor']
            if view['status'] not in ('queued', 'running', 'stopping'):
                view['events'] = items
                assert view['status'] == 'succeeded', view
                return view
            time.sleep(.15)
        raise AssertionError('real SDK turn timed out')

    try:
        api = start_api()
        cid = post('/conversations', {'provider_id': 'provider_schedule_fixture', 'send_to_provider': True})['id']
        initial = post(f'/conversations/{cid}/turns', {'text': '请在两秒后把整理结果保存为 Markdown，采用上海时区，错过时补一次。'}, 'schedule-natural-create')
        created = wait_turn(initial['id'])
        tasks = get(f'/conversations/{cid}/schedules')['items']
        assert len(tasks) == 1
        sid = tasks[0]['id']
        assert sid in created['response'] and 'Asia/Shanghai' in created['response']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            history = get(f'/conversations/{cid}/schedules/{sid}/history')['items']
            if history and history[0]['turn_id']:
                break
            time.sleep(.15)
        assert len(history) == 1 and history[0]['turn_id'] != initial['id'], history
        executed = wait_turn(history[0]['turn_id'])
        assert (home / 'workspace' / filename).read_text(encoding='utf-8') == expected
        outputs = get(f'/conversations/{cid}/files')['items']
        assert len(outputs) == 1 and outputs[0]['name'] == filename
        downloaded = client.get(base + f'/api/v1/agent/conversations/{cid}/files/{outputs[0]["id"]}/content', timeout=10)
        assert downloaded.ok and downloaded.content == expected.encode('utf-8')
        history = get(f'/conversations/{cid}/schedules/{sid}/history')['items']
        assert len(history) == 1 and history[0]['turn_status'] == 'succeeded'
        runtime_events = [e for turn in (created, executed) for e in turn['events'] if e['kind'] == 'runtime']
        assert len(runtime_events) == 2 and all(e['data']['version'] == '0.2.0-rc.2' for e in runtime_events)
        call_names = [call['function']['name'] for receipt in receipts for call in receipt.get('tools', [])]
        expected_tools = {'schedule_create', 'schedule_list', 'schedule_update', 'schedule_delete', 'schedule_history'}
        assert expected_tools.issubset(call_names)
        before = len(receipts)
        stop_api()
        api = start_api()
        time.sleep(3)
        after = get(f'/conversations/{cid}/schedules/{sid}/history')['items']
        turns = get(f'/conversations/{cid}')['turns']
        assert len(after) == 1 and after[0]['turn_id'] == history[0]['turn_id']
        assert len(turns) == 2 and len(receipts) == before
        evidence = {'ok': True, 'kind': 'controlled-model-real-product-api-sdk-tools-scheduler-thread',
                    'runtime_version': '0.2.0-rc.2', 'model_calls': len(receipts),
                    'schedule_tools': sorted(expected_tools), 'natural_request_turn': created,
                    'scheduled_execution_turn': executed, 'history_before': history, 'history_after_restart': after,
                    'actual_file': str(home / 'workspace' / filename), 'download_matches': True,
                    'no_repeat_admission_or_model_execution_after_restart': True}
        (home / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'ok': True, 'model_calls': len(receipts), 'evidence': str(home / 'evidence.json')}, ensure_ascii=False), flush=True)
    finally:
        stop_api()
        client.close()
        provider.shutdown()
        provider.server_close()
        log.close()


if __name__ == '__main__':
    verify()
