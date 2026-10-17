"""A mistaken --out must not destroy the evidence being rejudged."""
import json
import subprocess
import sys
from pathlib import Path


def test_rejudge_refuses_an_output_that_is_also_an_input(tmp_path):
    root = Path(__file__).resolve().parents[3]
    source = tmp_path / 'source.json'
    original = json.dumps({'kind': 'synthetic-original-evidence', 'judge_version': 1, 'sessions': []}).encode()
    source.write_bytes(original)
    run = subprocess.run([sys.executable, '-X', 'utf8', str(root / 'tools/verify_campus_readonly_live.py'),
                          '--rejudge', '--rejudge-input', str(source), '--out', str(source)],
                         cwd=root, capture_output=True, text=True, timeout=30)
    assert source.read_bytes() == original, 'rejudge overwrote its original input evidence'
    assert run.returncode != 0, 'input/output alias must be rejected before writing'
