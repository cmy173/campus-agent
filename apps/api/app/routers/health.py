"""健康检查（契约 /api/v1/health，HealthResponse）。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from ..config import APP_VERSION

router = APIRouter(tags=["health"])


def _agent_source_fingerprint() -> str:
    """Hash the Python sources that implement the campus Agent executors."""
    root = Path(__file__).resolve().parents[4]
    source_roots = (root / "apps" / "api" / "app",
                    root / "connectors" / "campus",
                    root / "packages" / "learning" / "src" / "learning")
    files = sorted(path for base in source_roots if base.is_dir()
                   for path in base.rglob("*.py")
                   if "__pycache__" not in path.parts)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


# Compute once at import/startup. Hashing files inside the request handler would
# report edited on-disk sources even if this process still has older code loaded.
AGENT_SOURCE_FINGERPRINT = _agent_source_fingerprint()


@router.get("/health")
def get_health() -> dict:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "agent_source_sha256": AGENT_SOURCE_FINGERPRINT,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
