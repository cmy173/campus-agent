"""Export the CURRENT domain tool surface without opening personal data/credentials."""
from pathlib import Path
import json
import os
import sys
import uuid
import hashlib

ROOT = Path(__file__).resolve().parents[1]
scratch = ROOT / '.test-tmp' / ('transition-inventory-' + uuid.uuid4().hex)
scratch.mkdir(parents=True)
os.environ.update(STUDY_DATABASE_URL='sqlite:///' + (scratch / 'db.sqlite').as_posix(),
                  STUDY_FILES_DIR=str(scratch / 'files'), STUDY_SECRETS_DIR=str(scratch / 'secrets'),
                  CAMPUS_AGENT_HOME=str(scratch / 'agent'), CAMPUS_DATA_DIR=str(scratch / 'campus'))
sys.path[:0] = [str(ROOT / 'apps/api'), str(ROOT)]
from app.agent_bridge.tools import schemas  # noqa: E402

items = schemas()
source = ROOT / 'apps/api/app/agent_bridge/tools.py'
result = {'kind': 'current-code-inventory-not-live-success', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
          'domain_count': len(items), 'schemas': items}
(ROOT / 'docs/audit/transition-tool-schemas.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'domain_count': len(items), 'names': [item['name'] for item in items]}))
