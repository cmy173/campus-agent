"""Real detached children: cleanup after root exit and sibling independence."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.process_owner import spawn_owned  # noqa: E402

directory = Path(tempfile.mkdtemp(prefix='v2-owned-process-', dir=ROOT / 'var'))
script = directory / 'detached.mjs'
script.write_text('''import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
const input = createInterface({input:process.stdin});
input.once('line', line => {
  const { marker } = JSON.parse(line);
  const code = 'setTimeout(() => require("node:fs").writeFileSync(process.argv[1], "child-finished"), 3000)';
  const child = spawn(process.execPath, ['-e', code, marker], {detached:true,stdio:'ignore',windowsHide:true});
  child.unref();
  console.log(JSON.stringify({child_pid:child.pid}));
  setInterval(() => {}, 1000);
});
''', encoding='utf-8')
processes = []
evidence = {'kind': 'actual_owned_detached_processes'}
try:
    for name in ('a', 'b'):
        proc = spawn_owned([shutil.which('node'), str(script)], stdin=-1, stdout=-1, stderr=-1,
                           text=True, encoding='utf-8')
        processes.append(proc)
        proc.stdin.write(json.dumps({'marker': str(directory / (name + '.txt'))}) + '\n')
        proc.stdin.flush()
        record = json.loads(proc.stdout.readline())
        evidence[name] = {'root_pid': proc.pid, **record, 'active_before_stop': proc.campus_owner.active()}
        assert evidence[name]['active_before_stop'] >= 2
    a, b = processes
    # Only terminate the root; the ownership watcher must end its detached child.
    a.terminate()
    a.wait(timeout=5)
    deadline = time.monotonic() + 5
    while a.campus_owner.active() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert a.campus_owner.active() == 0
    assert b.poll() is None and b.campus_owner.active() >= 2
    time.sleep(3.5)
    assert not (directory / 'a.txt').exists(), 'Detached child A continued after root termination'
    assert (directory / 'b.txt').read_text() == 'child-finished', 'Stopping A interrupted unrelated sibling B'
    evidence.update(ok=True, a_quiescent=True, sibling_b_continued=True)
finally:
    for proc in processes:
        proc.campus_owner.close()
        proc.wait(timeout=5)
        proc.stdin.close(); proc.stdout.close(); proc.stderr.close()
    (directory / 'evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps({'evidence': str(directory / 'evidence.json'), **evidence}))
