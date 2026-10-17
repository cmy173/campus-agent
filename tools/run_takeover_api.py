"""Run a source-matched local Agent API against a copy of the real personal data.

The real user database is never mutated: it is copied with SQLite's online backup API
(WAL-safe) into a disposable state directory. The encrypted provider key is referenced
by ID from the existing secret store and never copied or printed.

    python tools/run_takeover_api.py --port 8018 --state-dir var/takeover-state/run1

Use this instead of `tools/dev.py` when validating real task completion while another
(old) service owns port 8000. Kill only the process you started.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / 'apps' / 'api'
LEARNING_SRC = ROOT / 'packages' / 'learning' / 'src'
for entry in (str(ROOT), str(API_ROOT), str(LEARNING_SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)
    try:
        target_connection = sqlite3.connect(str(target))
        try:
            with target_connection:
                source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8018)
    parser.add_argument('--state-dir', type=Path, default=None)
    parser.add_argument('--source-db', type=Path, default=ROOT / 'var' / 'personal.db')
    parser.add_argument('--secrets-dir', type=Path, default=ROOT / 'var' / 'secrets')
    parser.add_argument('--with-campus-cache', action='store_true', default=True)
    args = parser.parse_args()
    if not args.source_db.is_file():
        raise SystemExit(f'source database not found: {args.source_db}')
    if not args.secrets_dir.is_dir():
        raise SystemExit(f'secrets directory not found: {args.secrets_dir}')
    state = (args.state_dir or ROOT / 'var' / 'takeover-state' / time.strftime('%Y%m%d-%H%M%S')).resolve()
    state.mkdir(parents=True, exist_ok=False)
    copy_database(args.source_db, state / 'domain.sqlite')
    agent_home = state / 'agent'
    (agent_home / 'campus').mkdir(parents=True, exist_ok=True)
    (state / 'files').mkdir(parents=True, exist_ok=True)
    (state / 'workspace').mkdir(parents=True, exist_ok=True)
    # Imported campus snapshot records live beside the journal (CAMPUS_AGENT_HOME/campus).
    # Copying them preserves the real "what the app has cached" state for read-only tools.
    source_cache = ROOT / 'var' / 'agent' / 'campus' / 'workspace.sqlite3'
    if args.with_campus_cache and source_cache.is_file():
        copy_database(source_cache, agent_home / 'campus' / 'workspace.sqlite3')

    os.environ['STUDY_DATABASE_URL'] = 'sqlite:///' + (state / 'domain.sqlite').as_posix()
    os.environ['STUDY_FILES_DIR'] = str(state / 'files')
    os.environ['STUDY_SECRETS_DIR'] = str(args.secrets_dir)
    os.environ['CAMPUS_AGENT_HOME'] = str(agent_home)
    os.environ['CAMPUS_WORKSPACE_DIR'] = str(state / 'workspace')
    os.environ['CAMPUS_BRIDGE_URL'] = f'http://127.0.0.1:{args.port}'
    os.environ['CAMPUS_WEB_ORIGINS'] = f'http://127.0.0.1:{args.port}'
    os.environ['STUDY_WORKER_INPROC'] = '0'
    os.environ.setdefault('PYTHONUTF8', '1')

    import uvicorn
    from app.main import app  # noqa: E402  (environment must be set before import)

    marker = {'ready': True, 'port': args.port, 'state': str(state), 'source_db': str(args.source_db)}
    print('takeover_api_ready ' + json.dumps(marker, ensure_ascii=False), flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
