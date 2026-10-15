"""Repository-root pytest configuration.

Makes the migrated `connectors.*` packages importable for the connector regression
suite in `tests/` regardless of the directory pytest is invoked from. The API suite
has its own equivalent bootstrap in `apps/api/app/__init__.py`.

Credential/agent-data isolation is applied here too: a connector test must never read
this machine's real campus or WebVPN credentials, and must never reach the school.
"""
from pathlib import Path
import os
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

_SCRATCH = Path(tempfile.gettempdir()) / f"campus-connector-tests-{uuid.uuid4().hex}"
os.environ.setdefault("STUDY_SECRETS_DIR", str(_SCRATCH / "secrets"))
os.environ.setdefault("CAMPUS_AGENT_HOME", str(_SCRATCH / "agent"))
