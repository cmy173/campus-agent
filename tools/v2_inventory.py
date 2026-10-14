"""Write route and database-count evidence without account contents."""
from __future__ import annotations
import ast
import json
from pathlib import Path
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def routes(directory):
    records = []
    if not directory.exists():
        # The legacy application was retired; the inventory reports an empty set
        # instead of failing, so a post-retirement baseline stays reproducible.
        return records
    for path in sorted(directory.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {'get', 'post', 'patch', 'put', 'delete', 'websocket'}:
                    if d.args and isinstance(d.args[0], ast.Constant):
                        records.append({'file': path.relative_to(ROOT).as_posix(), 'function': node.name,
                                        'method': d.func.attr.upper(), 'path': d.args[0].value})
    return records


def database(path):
    if not path.exists():
        return {'exists': False}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
        return {'exists': True, 'integrity_check': db.execute('PRAGMA integrity_check').fetchone()[0], 'table_counts': counts}


def main():
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    result = {'implementation_directory': ROOT.as_posix(), 'branch': git('branch', '--show-current'),
              'preserved_commit': git('rev-parse', 'HEAD'),
              'new_remote_ui_reference': git('rev-parse', 'origin/feature/blue-chat-ui'),
              'old_routes': routes(ROOT / 'campus_agent'), 'new_routes': routes(ROOT / 'apps/api/app/routers'),
              'databases': {name: database(path) for name, path in {'old': ROOT / 'data/campus.sqlite3', 'core': ROOT / 'var/personal.db'}.items()}}
    (ROOT / 'docs/agent-v2-baseline.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'old_routes': len(result['old_routes']), 'new_routes': len(result['new_routes']), 'databases': result['databases']}, indent=2))


if __name__ == '__main__':
    main()
