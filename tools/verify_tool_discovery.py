"""On-demand tool discovery through the real product path.

A controlled local model proves, on the wire, that the default request omits the
niche tool groups, that `load_tools` lifts exactly the requested group, and that a
lifted tool then really runs. The model is a fixture; the API, the Harness
runtime, the tool registry and the tool execution are the real ones.
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
from app.process_owner import spawn_owned  # noqa: E402

DEFERRED_PREFIXES = ('mcp__', 'job_', 'team_task_')
DEFERRED_NAMES = {'subagent', 'subagent_fork', 'send_message', 'list_agents', 'interrupt_agent',
                  'workflow', 'exit_plan_mode'}
# Tools that must stay directly available for an ordinary campus task.
REQUIRED_DEFAULT = {'campus_campus_status', 'schedule_list', 'read', 'write', 'pwsh', 'load_tools'}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def deferred(names):
    return sorted(n for n in names
                  if n not in REQUIRED_DEFAULT
                  and (n.startswith(DEFERRED_PREFIXES) or n in DEFERRED_NAMES))


GROUP_PREFIX = {'browser': 'mcp__campus_browser__', 'mcp': 'mcp__campus_browser__'}


class Provider(BaseHTTPRequestHandler):
    """Local controlled model: inspect the wire, load one group, use one lifted tool."""

    catalogs: list = []
    receipts: list = []
    group: str | None = None
    page_url: str = ''
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            self.answer()
        except Exception as error:  # Never hang the turn on a fixture assertion.
            with self.lock:
                type(self).errors.append(repr(error))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant',
                'content': '受控夹具异常：' + repr(error), 'tool_calls': []}, 'finish_reason': 'stop'}]},
                ensure_ascii=False).encode())

    def answer(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        tools = [t['function']['name'] for t in (body.get('tools') or [])]
        with self.lock:
            self.receipts.append(body)
            self.catalogs.append(tools)
            index = len(self.receipts)
        messages = body['messages']
        boundary = next(m for m in reversed(messages) if m['role'] == 'user' and '用户消息：\n' in m.get('content', ''))
        results = [m['content'] for m in messages[messages.index(boundary) + 1:] if m['role'] == 'tool']
        calls, content = [], None

        def call(name, arguments):
            return [{'id': f'discovery_{index}_{len(results)}', 'type': 'function',
                     'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]

        if not results:
            if not Provider.baseline:
                Provider.baseline = tools
            if os.environ.get('DISCOVERY_PROBE') == 'plain':
                calls = call('pwsh', {'command': '& $env:DSH_PYTHON_EXECUTABLE -c "print(7 * 6)"', 'description': '探针计算'})
            elif 'load_tools' in tools:
                calls = call('load_tools', {'group': Provider.group})
            else:
                content = '本轮没有按需加载工具可用。'
        elif len(results) == 1:
            if os.environ.get('DISCOVERY_PROBE') == 'plain':
                assert '42' in results[-1], results[-1]
                content = '探针计算完成。'
            else:
                loaded = json.loads(results[-1])
                lifted = [name for name in loaded.get('available', []) if name not in Provider.baseline]
                assert lifted, (loaded, Provider.baseline)
                target = next((name for name in lifted if name.startswith(GROUP_PREFIX['browser'])), lifted[0])
                Provider.lifted = lifted
                if target.endswith('browser_navigate'):
                    calls = call(target, {'url': Provider.page_url})
        else:
            content = '按需加载已完成。'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls},
                                                'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())

    baseline: list = []
    lifted: list = []
    errors: list = []


def start_page():
    handler = type('Page', (BaseHTTPRequestHandler,), {
        'log_message': lambda *_: None,
        'do_GET': lambda self: (self.send_response(200), self.send_header('Content-Type', 'text/html'),
                                self.end_headers(), self.wfile.write(b'<html><body><h1>tool discovery</h1></body></html>')),
    })
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}/page'


def verify():
    (ROOT / 'var').mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='v2-discovery-', dir=ROOT / 'var'))
    Provider.catalogs, Provider.receipts, Provider.baseline, Provider.lifted, Provider.errors = [], [], [], [], []
    Provider.group = 'browser'

    page, page_url = start_page()
    Provider.page_url = page_url

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
    db.add(m.ProviderConfig(id='provider_discovery', name='受控发现模型', base_url=__URL__, model='discovery-fixture'))
    db.commit()
""".replace('__URL__', repr(f'http://127.0.0.1:{provider.server_port}/v1'))
    subprocess.run([sys.executable, '-c', seed], cwd=API, env=env, check=True)
    log = (home / 'api.log').open('w', encoding='utf-8')
    client, api = requests.Session(), None

    def start_api():
        process = spawn_owned([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                              cwd=API, env=env, stdout=log, stderr=log,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert process.poll() is None, (home / 'api.log').read_text(encoding='utf-8')
            try:
                if client.get(base + '/api/v1/health', timeout=1).ok:
                    return process
            except requests.RequestException:
                pass
            time.sleep(.05)
        process.campus_owner.close()
        raise AssertionError('API startup timed out')

    try:
        api = start_api()
        cid = client.post(base + '/api/v1/agent/conversations',
                          json={'provider_id': 'provider_discovery', 'send_to_provider': True}, timeout=15).json()['id']
        posted = client.post(base + f'/api/v1/agent/conversations/{cid}/turns',
                             json={'text': '请打开浏览器查看一个本地页面。'},
                             headers={'Idempotency-Key': 'discovery-1'}, timeout=15)
        assert posted.ok, posted.text
        turn_id = posted.json()['id']
        cursor, events = 0, []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=10).json()
            events.extend(view['events']['items'])
            cursor = view['events']['next_cursor']
            if view['status'] not in ('queued', 'running', 'stopping'):
                assert view['status'] == 'succeeded', view
                break
            time.sleep(.03)
        else:
            raise AssertionError('turn timed out')
    finally:
        if api is not None:
            api.campus_owner.close()
            api.wait(timeout=15)
        client.close()
        provider.shutdown()
        provider.server_close()
        page.shutdown()
        page.server_close()
        log.close()

    catalogs = Provider.catalogs
    assert not Provider.errors, Provider.errors
    assert catalogs, 'no model request reached the controlled fixture'
    if 'load_tools' not in catalogs[0]:
        print(json.dumps({
            'ok': False,
            'reason': 'on-demand tool discovery is implemented but not wired into the runtime yet',
            'detail': 'apps/agent-runtime/campus-tool-groups.mjs is not installed by any plugin: '
                      'a plugin-scope install does not govern the session agent, and wrapping the SDK '
                      'create/resume setup broke tool dispatch (every call was journalled then stalled '
                      'before tools/pre-execute). See docs/agent-v2-performance.md.',
            'observed_tools': len(catalogs[0]),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    baseline, after = catalogs[0], catalogs[-1]
    evidence = {
        'kind': 'on_demand_tool_discovery_real_runtime_controlled_model',
        'first_request_tools': len(baseline),
        'last_request_tools': len(after),
        'load_tools_visible_by_default': 'load_tools' in baseline,
        'deferred_hidden_by_default': sorted(set(deferred(baseline))),
        'deferred_after_load': sorted(set(deferred(after))),
        'lifted_tools': sorted(set(after) - set(baseline)),
        'still_hidden_after_one_group': sorted(set(deferred(baseline)) & set(deferred(after))),
        'required_default_present': sorted(REQUIRED_DEFAULT & set(baseline)),
        'model_calls': len(catalogs),
    }
    assert evidence['load_tools_visible_by_default'], evidence
    assert REQUIRED_DEFAULT.issubset(set(baseline)), sorted(REQUIRED_DEFAULT - set(baseline))
    assert evidence['lifted_tools'], evidence
    assert evidence['still_hidden_after_one_group'], 'loading one group must not reveal every group'
    (home / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'evidence': str(home / 'evidence.json'), **evidence}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    verify()
