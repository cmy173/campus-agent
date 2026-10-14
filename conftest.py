"""Repository-root pytest configuration.

Makes the migrated `connectors.*` packages importable for the connector regression
suite in `tests/` regardless of the directory pytest is invoked from. The API suite
has its own equivalent bootstrap in `apps/api/app/__init__.py`.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
