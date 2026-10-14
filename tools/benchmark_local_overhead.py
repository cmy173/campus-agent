"""Local overhead benchmark for the Campus V2 product path.

Measures the locally-owned part of a turn on this machine with an entirely local
controlled model provider, so remote model latency is excluded by construction:
startup, first visible event, per-tool latency, model request count, tool call
count and extra confirmation count for a plain local task.

The same command repeated on the same machine produces the numbers to compare
before/after a change; repeat runs replace the baseline file.

    python tools/benchmark_local_overhead.py [--label baseline] [--runs 3] [--task local-files]

Set `CAMPUS_BENCH_ROOT` to measure another checkout (for example the retained
pre-change workbench) with the same controlled local model.

Two controlled tasks are available because the pre-change workbench and the current
product do not expose the same model-visible tools:

* `local-files` (default): `write` -> `read` -> `pwsh`. Only the current product can run
  it, because the retained first-round workbench exposed no local file/shell tool at all.
* `campus-status`: one `campus_connections_status` call then the answer. Both the
  retained first-round workbench and the current product declare that tool, so this is
  the task to use for the before/after comparison required by completion condition 9.
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
TASK = 'local-files'
TASKS = ('local-files', 'campus-status')


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
    tool_names: list = []
    tool_results: list = []
    final_answers: list = []
    gaps: list = []
    last_response_at = None
    skip_gap = False
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        now = time.monotonic()
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.lock:
            self.requests_seen.append(now)
            tools = body.get('tools') or []
            encoded = json.dumps(tools, ensure_ascii=False)
            self.tool_payload.append({'count': len(tools), 'bytes': len(encoded.encode('utf-8'))})
            if tools:
                Provider.tool_names = [t.get('function', {}).get('name') for t in tools]
        # Gap between the previous answer sent to the runtime and this request arriving:
        # the fixture replies instantly, so this is the local tool round trip (tool
        # execution + dispatch). Kept because it is measurable on both sides even when a
        # checkout does not project tool/call + tool/result events.
        if Provider.skip_gap:
            Provider.skip_gap = False
        elif Provider.last_response_at is not None:
            Provider.gaps.append(round((now - Provider.last_response_at) * 1000, 1))
        messages = body['messages']
        boundary = next(m for m in reversed(messages) if m['role'] == 'user' and '用户消息：\n' in m.get('content', ''))
        results = [m['content'] for m in messages[messages.index(boundary) + 1:] if m['role'] == 'tool']
        calls = []
        content = None

        def call(name, arguments):
            return [{'id': 'bench_' + str(len(results)) + '_' + name, 'type': 'function',
                     'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]

        if TASK == 'campus-status':
            # The same controlled task on both sides: one declared campus tool read, then the answer.
            if results:
                self.tool_results.append(results[-1][:800])
                payload = json.loads(results[-1])
                assert payload.get('status') == 'ok' and not payload.get('error'), payload
            if not results:
                calls = call('campus_connections_status', {})
            else:
                assert len(results) == 1, results
                content = '校园连接状态已读取。'
                self.final_answers.append(content)
        elif not results:
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
            self.final_answers.append(content)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls},
                                                'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
        Provider.last_response_at = time.monotonic()


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def run_once(home: Path, iteration: int) -> dict:
    Provider.requests_seen = []
    Provider.tool_payload = []
    Provider.tool_names = []
    Provider.tool_results = []
    Provider.final_answers = []
    Provider.gaps = []
    Provider.last_response_at = None
    Provider.skip_gap = True
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
            requests_before = len(Provider.requests_seen)
            answers_before = len(Provider.final_answers)
            results_before = len(Provider.tool_results)
            gaps_before = len(Provider.gaps)
            Provider.skip_gap = True
            enqueued = time.monotonic()
            posted = client.post(base + f'/api/v1/agent/conversations/{cid}/turns',
                                 json={'text': (f'请读取当前校园连接状态并汇报（第 {run + 1} 次）。' if TASK == 'campus-status'
                                                else f'请把本地结果写入文件并核对（第 {run + 1} 次）。')},
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
                          'enqueued_to_first_request_ms': (Provider.requests_seen[requests_before] - enqueued) * 1000
                          if len(Provider.requests_seen) > requests_before else None,
                          'last_response_to_terminal_ms': (time.monotonic() - Provider.last_response_at) * 1000
                          if Provider.last_response_at is not None else None,
                          'enqueued_to_turn_finished_ms': (time.monotonic() - enqueued) * 1000,
                          'tool_calls': len(calls),
                          'tool_ms': {name: round(sum(values) / len(values), 1) for name, values in latencies.items()},
                          'model_requests': len(Provider.requests_seen) - requests_before,
                          'tool_results_seen': len(Provider.tool_results) - results_before,
                          'tool_roundtrip_ms': Provider.gaps[gaps_before:],
                          'final_answers': len(Provider.final_answers) - answers_before,
                          'events': len(events)})
        result['turns'] = turns
        result['model_requests_total'] = len(Provider.requests_seen)
        result['tool_calls_total'] = sum(turn['tool_calls'] for turn in turns)
        # Tool schemas shipped on every request: the measured cost of the current
        # all-schemas presentation (see docs/agent-v2-performance.md).
        result['tool_schema_count'] = Provider.tool_payload[0]['count'] if Provider.tool_payload else 0
        result['tool_schema_bytes'] = Provider.tool_payload[0]['bytes'] if Provider.tool_payload else 0
        result['tool_schema_names'] = list(Provider.tool_names)
        result['tool_results'] = list(Provider.tool_results)
        # Plain local operations must not raise an approval/confirmation prompt.
        confirmations = client.get(base + f'/api/v1/agent/conversations/{cid}/actions', timeout=10).json()
        result['pending_confirmations'] = len(confirmations.get('items', []))
        if TASK == 'local-files':
            result['downloaded_bytes_match'] = (home / 'workspace' / FILENAME).read_text(encoding='utf-8') == CONTENT
        else:
            result['downloaded_bytes_match'] = None
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
    parser.add_argument('--task', default='local-files', choices=TASKS)
    args = parser.parse_args()
    global TASK
    TASK = args.task
    (ROOT / 'var').mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='v2-bench-', dir=ROOT / 'var'))
    runs = [run_once(home, index + 1) for index in range(args.runs)]
    summary = {
        'task': TASK,
        'startup_ms_median': round(statistics.median([r['startup_ms'] for r in runs]), 1),
        'first_event_ms_median': round(statistics.median(
            [t['enqueued_to_first_event_ms'] for r in runs for t in r['turns']]), 1),
        'turn_ms_median': round(statistics.median(
            [t['enqueued_to_turn_finished_ms'] for r in runs for t in r['turns']]), 1),
        'first_request_ms_median': round(statistics.median(
            [t['enqueued_to_first_request_ms'] for r in runs for t in r['turns']]), 1),
        'tail_ms_median': round(statistics.median(
            [t['last_response_to_terminal_ms'] for r in runs for t in r['turns']]), 1),
        'model_requests_per_turn': round(statistics.median(
            [t['model_requests'] for r in runs for t in r['turns']]), 1),
        'tool_calls_per_turn': round(statistics.median(
            [t['tool_calls'] for r in runs for t in r['turns']]), 1),
        'tool_ms_p50': {name: percentile([value for r in runs for t in r['turns'] for value in [t['tool_ms'].get(name)] if value], .5)
                        for name in ('write', 'read', 'pwsh')} if TASK == 'local-files' else {
                            name: percentile([value for r in runs for t in r['turns'] for value in [t['tool_ms'].get(name)] if value], .5)
                            for name in sorted({name for r in runs for t in r['turns'] for name in t['tool_ms']})},
        'tool_roundtrip_ms_p50': percentile(
            [value for r in runs for t in r['turns'] for value in t['tool_roundtrip_ms']], .5),
        'tool_roundtrip_ms_all': [[value for t in r['turns'] for value in t['tool_roundtrip_ms']] for r in runs],
        'pending_confirmations': [r['pending_confirmations'] for r in runs],
        'model_requests_total': [r['model_requests_total'] for r in runs],
        'tool_schema_count': [r['tool_schema_count'] for r in runs],
        'tool_schema_bytes': [r['tool_schema_bytes'] for r in runs],
    }
    # Correctness gate: the controlled task must actually have run, on both sides of the
    # comparison. A faster number with a broken task is not a result.
    if TASK == 'local-files':
        ok = all(r['downloaded_bytes_match'] for r in runs)
    else:
        ok = all(turn['tool_results_seen'] == 1 and turn['final_answers'] == 1
                 for r in runs for turn in r['turns'])
    evidence = {'kind': 'local_overhead_controlled_local_model', 'label': args.label, 'task': TASK,
                'started_at': datetime.now(timezone.utc).isoformat(), 'runs': runs, 'summary': summary,
                'notes': ['The model provider is a local fixture: no remote network latency is included.',
                          'Confirmations counted from pending browser/plan actions (plain local tools must stay 0).',
                          'model_requests is counted per turn (delta inside the turn), not cumulatively.',
                          'tool_roundtrip_ms is measured at the fixture: the gap between the reply that '
                          'requests a tool and the request that carries its result. tool_calls/tool_ms come '
                          'from projected harness events and can be 0 on a checkout that does not project '
                          'tool/call events even though the tool really ran (verify tool_results instead).',
                          'The same script drives the current product and the retained pre-change workbench; '
                          'task campus-status is the one both declare on the wire.']}
    path = home / f'benchmark-{args.label}.json'
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': ok, 'label': args.label, 'task': TASK, 'summary': summary, 'evidence': str(path)},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
