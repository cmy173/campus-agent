"""SQLite persistence for the migrated ZJU connectors.

The connector package owns its own cache schema; the application layer may compose
additional tables on top of it (see `apps/api/app/campus_workspace.CacheStore` for
the historical-import tables). No passwords, sessions or credential tables live here:
the connector keeps authenticated sessions in memory only and the browser owns login.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

CAMPUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS campus_snapshots(owner TEXT PRIMARY KEY,snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connector_cache(owner TEXT NOT NULL,platform TEXT NOT NULL,
 capability TEXT NOT NULL,scope TEXT NOT NULL,result TEXT NOT NULL,
 PRIMARY KEY(owner,platform,capability,scope));
"""


class SQLiteStore:
    """Minimal store contract used by the migrated repositories: `connect()` only."""

    def __init__(self, path: Path, *, schema: str = CAMPUS_SCHEMA):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(schema)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()
