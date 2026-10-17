"""Export reviewable GitHub sources and an optional adjacent-source Windows release.
No git index mutation; includes current untracked production sources via an allowlist.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ALLOWED_PREFIXES=('apps/api/','apps/web/','connectors/','packages/learning/','contracts/','tools/','plugins/zju-study/','docs/release/')
ALLOWED_ROOTS={'.gitignore','.gitattributes','.mcp.json.example','README.md','plugin.json'}
BLOCKED_PARTS={'.git','.venv','venv','node_modules','__pycache__','.pytest_cache','dist','var','.cache','build','.worktrees'}
BLOCKED_SUFFIXES={'.db','.sqlite3','.key','.pem','.p12','.log','.mp4','.mp3','.wav','.exe','.pyc'}


def source_files():
    raw=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT)
    selected=[]
    for name in raw.decode('utf-8').split('\0'):
        name=name.replace('\\','/')
        if not name or not (name in ALLOWED_ROOTS or name.startswith(ALLOWED_PREFIXES)):
            continue
        path=Path(name)
        if any(part in BLOCKED_PARTS for part in path.parts) or path.suffix.lower() in BLOCKED_SUFFIXES:
            continue
        if path.name.startswith('.env') and not path.name.endswith('.example'):
            continue
        if name.startswith(('connectors/docs/','apps/api/docs/','tools/pilot_')) or '/handoff/' in name or name.endswith('/measure_optics_tokens.py'):
            continue
        target=ROOT/path
        if target.is_file() and not target.is_symlink():
            selected.append(name)
    return sorted(set(selected))


def scan_for_private_data(selected):
    sys.path.insert(0,str(ROOT/'apps/api'))
    from app import secrets_store
    keys=[]
    for keyfile in (ROOT/'var/secrets').glob('provider-*.key'):
        key=secrets_store.get_key(keyfile.stem.removeprefix('provider-'))
        if key:
            keys.append(key.encode('utf-8'))
    # Known local identifiers are checked without printing their values.
    forbidden_ids=[]
    database=ROOT/'var/personal.db'
    if database.is_file():
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
            for table in ('provider_configs','review_papers'):
                for row in connection.execute('SELECT id FROM '+table):
                    forbidden_ids.append(str(row[0]).encode('utf-8'))
    for name in selected:
        data=(ROOT/name).read_bytes()
        if any(value in data for value in keys+forbidden_ids):
            raise RuntimeError('Private local value found in selected source; packaging stopped: '+name)
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data):
            raise RuntimeError('Private key material found; packaging stopped: '+name)
    return {'selected_source_files':len(selected),'provider_key_match':False,'known_local_identifier_match':False,
            'runtime_directories_included':False,'browser_profiles_included':False,'experimental_pilots_included':False}


def build_package(selected, output, name, windows, audit):
    folder=output/name
    archive=output/(name+'.zip')
    if folder.exists() or archive.exists():
        raise RuntimeError('Package already exists; choose a new --name (existing exports are preserved)')
    folder.mkdir(parents=True)
    for name in selected:
        destination=folder/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,destination)
    if windows:
        launcher=ROOT/'浙大学习工作台-修复版.exe'
        if not launcher.is_file():
            raise RuntimeError('Build or supply the Windows launcher first')
        shutil.copy2(launcher,folder/launcher.name)
    (folder/'PACKAGE-AUDIT.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    manifest={path.relative_to(folder).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(folder.rglob('*')) if path.is_file()}
    (folder/'PACKAGE-MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(folder.rglob('*')):
            if path.is_file():zipped.write(path,folder.name+'/'+path.relative_to(folder).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        assert len(zipped.namelist()) == len(manifest)+1
        for entry in zipped.namelist():
            relative=Path(entry).parts[1:]
            assert not any(part in BLOCKED_PARTS for part in relative)
    return {'archive':str(archive),'directory':str(folder),'files':len(manifest)+1,
            'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name',default='zju-study-plugin-0.3.0-alpha.1-20261016')
    parser.add_argument('--output',type=Path,default=ROOT/'dist/github')
    parser.add_argument('--source-only',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9._-]+',args.name):raise ValueError('Invalid package name')
    args.output.mkdir(parents=True,exist_ok=True)
    selected=source_files();audit=scan_for_private_data(selected)
    reports=[build_package(selected,args.output,args.name+'-source',False,audit)]
    if not args.source_only:reports.append(build_package(selected,args.output,args.name+'-windows',True,audit))
    for report in reports:print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
