"""Adopt the verified core source snapshot into the primary checkout.

Existing executable files are never silently overwritten. Differing historical
documentation is retained under docs/history/first-round-source. Runtime data
stays separate from source and comes from the local preservation snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = {'apps', 'connectors', 'contracts', 'packages', 'tools'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    args = parser.parse_args()
    core = args.snapshot.resolve() / 'core'
    manifest = json.loads((core / 'manifest-private.json').read_text(encoding='utf-8'))
    source = core / 'source.zip'
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest['source']['sha256']:
        raise SystemExit('Source archive hash mismatch')
    report = {'source_root': manifest['root'], 'source_head': manifest['head'],
              'snapshot': args.snapshot.resolve().as_posix(), 'adopted': [], 'historical_docs': [],
              'unchanged': [], 'deferred': [], 'runtime_data': []}
    writes = []
    with zipfile.ZipFile(source) as archive:
        for item in manifest['source']['files']:
            name = item['path']
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise SystemExit('Unsafe archive path')
            if relative.parts[0] not in CODE_DIRS | {'docs'} and name not in ('.gitattributes', 'PACKAGE-MANIFEST.json'):
                report['deferred'].append(name)
                continue
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != item['sha256']:
                raise SystemExit('File hash mismatch')
            dest = ROOT / name
            if dest.exists():
                if dest.read_bytes() == content:
                    report['unchanged'].append(name)
                    continue
                if relative.parts[0] != 'docs':
                    raise SystemExit(f'Executable source collision: {name}')
                dest = ROOT / 'docs/history/first-round-source' / relative.relative_to('docs')
                report['historical_docs'].append(name)
            else:
                report['adopted'].append(name)
            if dest.exists() and dest.read_bytes() != content:
                raise SystemExit(f'Historical document collision: {dest}')
            writes.append((dest, content))
    # All collisions and hashes were checked before mutating any source.
    for dest, content in writes:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    with zipfile.ZipFile(core / 'local-data-private.zip') as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if '..' in path.parts or path.is_absolute():
                raise SystemExit('Unsafe data path')
            if name == 'var/personal.db' or name.startswith(('var/secrets/', 'var/agent/', 'var/personal-files/', 'var/files/')):
                dest = ROOT / name
                if dest.exists():
                    raise SystemExit(f'Runtime data already present, cannot overwrite: {name}')
                dest.parent.mkdir(parents=True, exist_ok=True)
                if name == 'var/personal.db':
                    shutil.copyfile(core / 'sqlite-consistent' / name, dest)
                else:
                    dest.write_bytes(archive.read(name))
                report['runtime_data'].append(name)
    # Do not publish a credential file inventory in source-controlled evidence.
    report['runtime_data'] = {'copied_files': len(report['runtime_data']),
                              'source': 'private preservation snapshot',
                              'personal_db': 'var/personal.db', 'old_db_preserved': 'data/campus.sqlite3'}
    (ROOT / 'docs/agent-v2-adoption.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({key: len(report[key]) for key in ('adopted', 'historical_docs', 'unchanged', 'deferred')}))


if __name__ == '__main__':
    main()
