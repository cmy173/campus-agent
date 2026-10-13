"""Package current working sources, excluding personal runtime data and experimental pilots."""
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'dist/github'
OUT.mkdir(parents=True, exist_ok=True)
stamp='20261012'
base='zju-study-workbench-'+stamp
selected=[]
for name in subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=ROOT).decode('utf-8').splitlines():
    name=name.replace('\\','/')
    if name in ('.gitignore','.gitattributes') or name.startswith(('apps/api/','apps/web/','connectors/','packages/learning/','contracts/','tools/','docs/release/')):
        if any(part in name.split('/') for part in ('.venv','node_modules','__pycache__','.pytest_cache','dist','var')):
            continue
        if name.startswith(('connectors/docs/','apps/api/docs/')) or '/handoff/' in name:
            continue
        if name.startswith('tools/pilot_') or name.endswith('/measure_optics_tokens.py'):
            continue
        if ROOT.joinpath(name).is_file():selected.append(name)
selected=sorted(set(selected))
# Scan exact locally stored provider keys without displaying them.
sys.path.insert(0,str(ROOT/'apps/api'))
from app import secrets_store
keys=[]
for keyfile in (ROOT/'var/secrets').glob('provider-*.key'):
    key=secrets_store.get_key(keyfile.stem.removeprefix('provider-'))
    if key:keys.append(key.encode())
for name in selected:
    data=ROOT.joinpath(name).read_bytes()
    if any(key in data for key in keys):raise RuntimeError('A provider key was found in a selected source file; packaging stopped')
    if name.endswith(('.db','.key','.env','.mp4','.wav')):raise RuntimeError('Unexpected runtime file selected')
readme=(ROOT/'docs/release/README-github.md').read_bytes()
for variant in ('source','windows'):
    folder=OUT/(base+'-'+variant)
    assert not folder.exists(),'Preserve existing package; choose a new stamp before repackaging'
    folder.mkdir()
    for name in selected:
        target=folder/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
    (folder/'README.md').write_bytes(readme)
    if variant=='windows':shutil.copy2(ROOT/'浙大学习工作台-修复版.exe',folder/'浙大学习工作台-修复版.exe')
    manifest={str(path.relative_to(folder)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest()
              for path in folder.rglob('*') if path.is_file()}
    (folder/'PACKAGE-MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=OUT/(folder.name+'.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in folder.rglob('*'):
            if path.is_file():z.write(path,folder.name+'/'+str(path.relative_to(folder)))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert len(z.namelist())==len(manifest)+1
    print(json.dumps({'archive':str(archive),'files':len(manifest)+1,'bytes':archive.stat().st_size,
                      'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},ensure_ascii=False))
