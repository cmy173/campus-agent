"""Resolve the frozen Python environment to hash-locked wheels for this host.

Run with the new API virtualenv's Python. Versions never change: metadata comes
from the exact PyPI version, and wheel tags are matched to the current Python.
The resulting runtime lock excludes the editable local learning package, which
the launcher installs separately without isolated build dependency resolution.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import urllib.request
import urllib.error

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
TAGS = {tag: index for index, tag in enumerate(sys_tags())}


def resolve(line: str):
    requirement = Requirement(line)
    if requirement.marker and not requirement.marker.evaluate():
        return None
    specs = list(requirement.specifier)
    if len(specs) != 1 or specs[0].operator != '==':
        raise ValueError(f'Not an exact version: {line}')
    version = specs[0].version
    url = f'https://pypi.org/pypi/{requirement.name}/{version}/json'
    cache = ROOT / 'var/install-metadata' / f'{requirement.name}-{version}.json'
    if cache.exists():
        metadata = json.loads(cache.read_text(encoding='utf-8'))
    else:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    metadata = json.load(response)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(metadata), encoding='utf-8')
                break
            except urllib.error.URLError as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                    raise RuntimeError(f'Exact version metadata unavailable: {line} (HTTP {exc.code})') from None
                if attempt == 2:
                    raise RuntimeError(f'Metadata request failed after retries: {line}') from None
    candidates = []
    for item in metadata['urls']:
        if not item['filename'].endswith('.whl') or item.get('yanked'):
            continue
        _, _, _, tags = parse_wheel_filename(item['filename'])
        ranks = [TAGS[tag] for tag in tags if tag in TAGS]
        if ranks:
            candidates.append((min(ranks), item))
    if not candidates:
        raise ValueError(f'No host-compatible wheel: {line}')
    item = min(candidates, key=lambda candidate: candidate[0])[1]
    return f"{requirement.name} @ {item['url']} --hash=sha256:{item['digests']['sha256']}"


def main():
    lines = (ROOT / 'apps/api/requirements-lock.txt').read_text(encoding='utf-8').splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.startswith(('#', '-e'))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved = list(pool.map(resolve, requirements))
    target = ROOT / 'apps/api/requirements-wheels-win-py312.txt'
    target.write_text('# Exact requirements-lock.txt versions, Windows x64 / CPython 3.12.\n'
                      '# Regenerate for this host with tools/lock_python_wheels.py; do not use on other hosts.\n'
                      + '\n'.join(line for line in resolved if line) + '\n', encoding='utf-8')
    print(f'Locked {sum(line is not None for line in resolved)} compatible wheels')


if __name__ == '__main__':
    main()
