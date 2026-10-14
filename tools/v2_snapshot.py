"""Preserve both V2 inputs outside the checkout, including local data.

Source files come from Git's tracked/untracked inventory. Runtime data and
environment files are stored separately and must never be committed/published.
SQLite online backups are included in addition to raw data archives.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import zipfile


OMIT = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
        '.cache', 'dist', 'build', '.mypy_cache', '.ruff_cache'}


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *args])


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def walk(directory: Path):
    for parent, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name not in OMIT]
        for name in files:
            yield Path(parent) / name


def archive(root: Path, paths: list[Path], destination: Path):
    records = []
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(set(paths)):
            relative = path.relative_to(root).as_posix()
            before = sha(path)
            z.write(path, relative)
            if sha(path) != before:
                raise RuntimeError(f'File changed during snapshot: {relative}; retry after quiescing writer')
            records.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': before})
    with zipfile.ZipFile(destination) as z:
        for item in records:
            with z.open(item['path']) as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != item['sha256']:
                raise RuntimeError('Archive verification failed')
    return {'archive': destination.name, 'sha256': sha(destination), 'files': records}


def snapshot(root: Path, target: Path):
    target.mkdir()
    inventory = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    paths = []
    for raw in inventory.split(b'\0'):
        if not raw:
            continue
        path = (root / os.fsdecode(raw)).resolve()
        if not path.is_relative_to(root):
            raise RuntimeError('Git inventory escaped root')
        if path.is_file() and not (set(path.relative_to(root).parts) & OMIT):
            paths.append(path)
    data = []
    for name in ('data', 'var'):
        directory = root / name
        if directory.exists():
            data.extend(walk(directory))
    # Environment files and device encryption metadata are local-only inputs.
    data.extend(path for path in walk(root) if path.name.startswith('.env')
                and not path.name.endswith('.example'))
    paths = [path for path in paths if path not in set(data)]
    result = {'root': root.as_posix(), 'head': git(root, 'rev-parse', 'HEAD').decode().strip(),
              'branch': git(root, 'branch', '--show-current').decode().strip(),
              'source': archive(root, paths, target / 'source.zip'),
              'local_data': archive(root, data, target / 'local-data-private.zip')}
    (target / 'status.txt').write_bytes(git(root, 'status', '--short'))
    (target / 'changes.patch').write_bytes(git(root, 'diff', '--binary', 'HEAD'))
    subprocess.run(['git', '-C', str(root), 'bundle', 'create', str(target / 'history.bundle'), '--all'], check=True)
    subprocess.run(['git', '-C', str(root), 'bundle', 'verify', str(target / 'history.bundle')],
                   check=True, capture_output=True)
    result['history_sha256'] = sha(target / 'history.bundle')
    result['sqlite'] = []
    for path in data:
        if path.suffix not in ('.db', '.sqlite', '.sqlite3'):
            continue
        if path.read_bytes()[:16] != b'SQLite format 3\0':
            continue
        relative = path.relative_to(root)
        output = target / 'sqlite-consistent' / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(output) as dest:
                source.backup(dest)
                check = dest.execute('PRAGMA integrity_check').fetchone()[0]
                if check != 'ok':
                    raise RuntimeError(f'SQLite integrity check failed: {relative}')
        result['sqlite'].append({'path': relative.as_posix(), 'backup': output.relative_to(target).as_posix(),
                                 'sha256': sha(output), 'integrity_check': check})
    (target / 'manifest-private.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    return {key: result[key] for key in ('root', 'head', 'branch')} | {
        'source_files': len(paths), 'source_sha256': result['source']['sha256'],
        'data_files': len(data), 'data_sha256': result['local_data']['sha256'],
        'sqlite_backups': len(result['sqlite']), 'verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--old', type=Path, required=True)
    parser.add_argument('--new', type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    roots = {'original': args.old.resolve(), 'core': args.new.resolve()}
    if any(destination.is_relative_to(root) for root in roots.values()):
        parser.error('Snapshot destination must be outside both input directories')
    destination.mkdir(parents=True, exist_ok=False)
    report = {'created_utc': datetime.now(timezone.utc).isoformat(),
              'destination': destination.as_posix(),
              'inputs': {name: snapshot(root, destination / name) for name, root in roots.items()}}
    (destination / 'summary.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    (destination / 'RESTORE.md').write_text(
        '# Local V2 preservation snapshot\n\n'
        'Contains private data and credential material: keep local, never publish.\n'
        'For each input: source.zip is the working source; changes.patch records tracked changes; '
        'history.bundle preserves Git references. Verify hashes against manifest-private.json.\n'
        'Restore source.zip into an empty directory, not over newer work. Local data is separate. '
        'Use sqlite-consistent/ backups for databases; preserve matching encryption salt/key files '
        'from local-data-private.zip. Do not restore expired session tokens as a new login.\n', encoding='utf-8')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
