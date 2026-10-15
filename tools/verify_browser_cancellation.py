"""Real browser JavaScript cancellation; local effects, no model or account."""
from __future__ import annotations

import json
import argparse
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.agent_bridge.browser import BrowserHost  # noqa: E402
from app.errors import ApiError  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--cpu', action='store_true', help='Verify stopping code that blocks the browser daemon event loop')
args = parser.parse_args()
directory = Path(tempfile.mkdtemp(prefix='v2-browser-stop-', dir=ROOT / 'var'))
started = threading.Event()
late = threading.Event()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/started':
            started.set()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<title>Owned cancellation fixture</title>')

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path == '/late':
            late.set()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')


fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=fixture.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{fixture.server_port}'
host = BrowserHost(directory / 'browser')
outcome = {}
evidence = {'kind': 'actual_browser_script_cancellation_no_model', 'directory': str(directory), 'cpu_bound': args.cpu}
try:
    host.rpc('stop_fixture', 'call', name='browser_navigate', arguments={'url': base})
    code = f'''async (page) => {{
      await fetch({json.dumps(base + '/started')});
      await new Promise(resolve => setTimeout(resolve, 3000));
      await fetch({json.dumps(base + '/late')}, {{method: 'POST', body: 'synthetic-effect'}});
      return 'late code completed';
    }}'''
    if args.cpu:
        code = f'async (page) => {{ console.log("ordinary stdout"); console.log(JSON.stringify({{kind:"activity",data:null}})); await fetch({json.dumps(base + "/started")}); while (true) {{}} }}'

    def execute():
        try:
            outcome['result'] = host.rpc('stop_fixture', 'call', name='browser_run_code_unsafe',
                arguments={'code': code}, turn_id='owned-script-stop')
        except ApiError as error:
            outcome['error'] = error.code

    worker = threading.Thread(target=execute)
    worker.start()
    assert started.wait(15), 'Actual JavaScript did not begin'
    owned = host.process
    before_stop = time.monotonic()
    evidence['stop_response'] = host.rpc('stop_fixture', 'stop', turn_id='owned-script-stop')
    worker.join(timeout=10)
    assert not worker.is_alive(), 'Browser call did not reach a terminal outcome'
    evidence['stop_seconds'] = round(time.monotonic() - before_stop, 3)
    evidence['call_outcome'] = outcome
    assert owned.poll() is not None and owned.campus_owner.active() == 0
    evidence['owned_processes_quiescent'] = True
    evidence['late_effect_after_stop'] = late.wait(timeout=4)
    assert not evidence['late_effect_after_stop'], 'JavaScript continued a real operation after the browser reported stopped'
    evidence['ok'] = True
finally:
    host.close()
    fixture.shutdown()
    fixture.server_close()
    (directory / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'evidence': str(directory / 'evidence.json'), **evidence}, ensure_ascii=False))
