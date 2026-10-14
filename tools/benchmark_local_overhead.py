"""Local overhead benchmark for the Campus V2 product path.

Measures the locally-owned part of a turn on this machine with an entirely local
controlled model provider, so remote model latency is excluded by construction:
startup, first visible event, per-tool latency, model request count, tool call
count and extra confirmation count for a plain local task.

The same command repeated on the same machine produces the numbers to compare
before/after a change; repeat runs replace the baseline file.

    python tools/benchmark_local_overhead.py [--label baseline] [--runs 3]

Set `CAMPUS_BENCH_ROOT` to measure another checkout (for example the retained
pre-change workbench) with the same controlled local model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

ROOT = Path(os.environ.get('CAMPUS_BENCH_ROOT') or Path(__file__).resolve().parents[1]).resolve()
API = ROOT / 'apps' / 'api'
sys.path.insert(0, str(API))
try:  # The owned-process owner landed with the V2 stop work; older checkouts lack it.
    from app.process_owner import spawn_owned  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - only used when measuring an older checkout
    def spawn_owned(command, **kwargs):
        class Plain:
            def __init__(self, process):
                self.process = process

            def poll(self):
                return self.process.poll()

            def wait(self, timeout=None):
                return self.process.wait(timeout=timeout)

        return Plain(subprocess.Popen(command, **{k: v for k, v in kwargs.items() if k != 'creationflags'}))

FILENAME = 'benchmark-note.txt'
CONTENT = '本地基准结果 431\n'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


class Provider(BaseHTTPRequestHandler):
    """Controlled local model: one write -> read -> local command -> answer."""

    requests_seen: list = []
    tool_payload: list = []
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.lock:
            self.requests_seen.append(time.monotonic())
            tools = body.get('tools') or []
            encoded = json.dumps(tools, ensure_ascii=False)
            self.tool_payload.append({'count': len(tools), 'bytes': len(encoded.encode('utf-8'))})
        messages = body['messages']
        boundary = next(m for m in reversed(messages) if m['role'] == 'user' and '用户消息：\n' in m.get('content', ''))
        results = [m['content'] for m in messages[messages.index(boundary) + 1:] if m['role'] == 'tool']
        calls = []
        content = None

        def call(name, arguments):
            return [{'id': 'bench_' + str(len(results)) + '_' + name, 'type': 'function',
                     'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]

        if not results:
            calls = call('write', {'file_path': FILENAME, 'content': CONTENT})
        elif len(results) == 1:
            calls = call('read', {'file_path': FILENAME})
        elif len(results) == 2:
            assert '431' in results[-1], results[-1]
            calls = call('pwsh', {'command': '& $env:DSH_PYTHON_EXECUTABLE -c "print(431)"',
                                  'description': '本地计算'})
        else:
            assert '431' in results[-1], results[-1]
            content = '本地基准任务完成。'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls},
                                                'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def run_once(home: Path, iteration: int) -> dict:
    Provider.requests_seen = []
    Provider.tool_payload = []
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
    row = db.get(m.ProviderConfig, 'provider_bench')
    if row is None:
        row = m.ProviderConfig(id='provider_bench', name='受控基准模型')
        db.add(row)
    row.base_url, row.model = __URL__, 'bench-fixture'
    db.commit()
""".replace('__URL__', repr(f'http://127.0.0.1:{provider.server_port}/v1'))
    subprocess.run([sys.executable, '-c', seed], cwd=API, env=env, check=True)
    log = (home / 'api.log').open('a', encoding='utf-8')
    client = requests.Session()
    api = None
    result: dict = {'iteration': iteration}

    def start_api():
        started = time.monotonic()
        process = spawn_owned([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                              cwd=API, env=env, stdout=log, stderr=log,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert process.poll() is None, (home / 'api.log').read_text(encoding='utf-8')
            try:
                if client.get(base + '/api/v1/health', timeout=1).ok:
                    return process, (time.monotonic() - started) * 1000
            except requests.RequestException:
                pass
            time.sleep(.05)
        other = getattr(process, 'campus_owner', None)
        if other is not None:
            other.close()
        raise AssertionError('API startup timed out')

    try:
        api, result['startup_ms'] = start_api()
        created = client.post(base + '/api/v1/agent/conversations',
                              json={'provider_id': 'provider_bench', 'send_to_provider': True}, timeout=15)
        assert created.ok, created.text
        cid = created.json()['id']

        turns = []
        for run in range(2):
            enqueued = time.monotonic()
            posted = client.post(base + f'/api/v1/agent/conversations/{cid}/turns',
                                 json={'text': f'请把本地结果写入文件并核对（第 {run + 1} 次）。'},
                                 headers={'Idempotency-Key': f'bench-{iteration}-{run}'}, timeout=15)
            assert posted.ok, posted.text
            turn_id = posted.json()['id']
            cursor, events, first_event = 0, [], None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=10).json()
                items = view['events']['items']
                cursor = view['events']['next_cursor']
                if items and first_event is None:
                    first_event = (time.monotonic() - enqueued) * 1000
                events.extend(items)
                if view['status'] not in ('queued', 'running', 'stopping'):
                    assert view['status'] == 'succeeded', view
                    break
                time.sleep(.02)
            else:
                raise AssertionError('turn timed out')
            calls = {event['data']['event']['data']['callId']: event
                     for event in events if event['kind'] == 'harness_event'
                     and event['data']['event']['type'] == 'tool/call'}
            tool_results = {event['data']['event']['data']['message']['toolCallId']: event
                            for event in events if event['kind'] == 'harness_event'
                            and event['data']['event']['type'] == 'tool/result'}
            latencies = {}
            for call_id, call_event in calls.items():
                done = tool_results.get(call_id)
                if done is None:
                    continue
                name = call_event['data']['event']['data']['name']
                latencies.setdefault(name, []).append(
                    (parse_time(done['created_at']) - parse_time(call_event['created_at'])) * 1000)
            turns.append({'enqueued_to_first_event_ms': first_event,
                          'enqueued_to_turn_finished_ms': (time.monotonic() - enqueued) * 1000,
                          'tool_calls': len(calls),
                          'tool_ms': {name: round(sum(values) / len(values), 1) for name, values in latencies.items()},
                          'model_requests': len(Provider.requests_seen),
                          'events': len(events)})
        result['turns'] = turns
        result['model_requests_total'] = len(Provider.requests_seen)
        result['tool_calls_total'] = sum(turn['tool_calls'] for turn in turns)
        # Tool schemas shipped on every request: the measured cost of the current
        # all-schemas presentation (see docs/agent-v2-performance.md).
        result['tool_schema_count'] = Provider.tool_payload[0]['count'] if Provider.tool_payload else 0
        result['tool_schema_bytes'] = Provider.tool_payload[0]['bytes'] if Provider.tool_payload else 0
        # Plain local operations must not raise an approval/confirmation prompt.
        confirmations = client.get(base + f'/api/v1/agent/conversations/{cid}/actions', timeout=10).json()
        result['pending_confirmations'] = len(confirmations.get('items', []))
        result['downloaded_bytes_match'] = (home / 'workspace' / FILENAME).read_text(encoding='utf-8') == CONTENT
    finally:
        if api is not None:
            owner = getattr(api, 'campus_owner', None)
            if owner is not None:
                owner.close()
            else:
                api.process.terminate()
            api.wait(timeout=15)
        client.close()
        provider.shutdown()
        provider.server_close()
        log.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', default='baseline')
    parser.add_argument('--runs', type=int, default=2)
    args = parser.parse_args()
    (ROOT / 'var').mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='v2-bench-', dir=ROOT / 'var'))
    runs = [run_once(home, index + 1) for index in range(args.runs)]
    summary = {
        'startup_ms_median': round(statistics.median([r['startup_ms'] for r in runs]), 1),
        'first_event_ms_median': round(statistics.median(
            [t['enqueued_to_first_event_ms'] for r in runs for t in r['turns']]), 1),
        'turn_ms_median': round(statistics.median(
            [t['enqueued_to_turn_finished_ms'] for r in runs for t in r['turns']]), 1),
        'model_requests_per_turn': round(statistics.median(
            [t['model_requests'] for r in runs for t in r['turns']]), 1),
        'tool_calls_per_turn': round(statistics.median(
            [t['tool_calls'] for r in runs for t in r['turns']]), 1),
        'tool_ms_p50': {name: percentile([value for r in runs for t in r['turns'] for value in [t['tool_ms'].get(name)] if value], .5)
                        for name in ('write', 'read', 'pwsh')},
        'pending_confirmations': [r['pending_confirmations'] for r in runs],
        'model_requests_total': [r['model_requests_total'] for r in runs],
        'tool_schema_count': [r['tool_schema_count'] for r in runs],
        'tool_schema_bytes': [r['tool_schema_bytes'] for r in runs],
    }
    evidence = {'kind': 'local_overhead_controlled_local_model', 'label': args.label,
                'started_at': datetime.now(timezone.utc).isoformat(), 'runs': runs, 'summary': summary,
                'notes': ['The model provider is a local fixture: no remote network latency is included.',
                          'Confirmations counted from pending browser/plan actions (plain local tools must stay 0).']}
    path = home / f'benchmark-{args.label}.json'
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': all(r['downloaded_bytes_match'] for r in runs), 'label': args.label,
                      'summary': summary, 'evidence': str(path)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
