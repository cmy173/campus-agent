"""Export the additive Agent API and its referenced schemas from FastAPI."""
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/api'))
from app.main import app  # noqa: E402


def references(value):
    if isinstance(value, dict):
        if '$ref' in value:
            yield value['$ref'].rsplit('/', 1)[-1]
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


spec = app.openapi()
paths = {key: value for key, value in spec['paths'].items() if key.startswith('/api/v1/agent/')}
schemas = {}
pending = set(references(paths))
while pending:
    name = pending.pop()
    if name in schemas:
        continue
    schemas[name] = spec['components']['schemas'][name]
    pending.update(references(schemas[name]))
result = {'openapi': '3.1.0', 'info': {'title': 'Campus Agent additive contract', 'version': '0.2.0-local'},
          'paths': paths, 'components': {'schemas': {name: schemas[name] for name in sorted(schemas)}}}
(ROOT / 'contracts/openapi/agent-openapi.yaml').write_text(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), encoding='utf-8')
print(f'Exported {len(paths)} Agent paths and {len(schemas)} schemas')
