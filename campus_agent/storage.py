"""SQLite persistence. Every domain query is scoped to a server-authenticated owner."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, kind TEXT NOT NULL, sha TEXT NOT NULL, content BLOB NOT NULL,
 status TEXT NOT NULL, error TEXT, warnings TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL,
 UNIQUE(owner, sha));
CREATE TABLE IF NOT EXISTS chunks (
 id TEXT PRIMARY KEY, document TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, document TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 chunk TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
 title TEXT NOT NULL, due TEXT, raw_date TEXT, state TEXT NOT NULL DEFAULT 'pending',
 reviewed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 action TEXT NOT NULL, result TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_doc_owner ON documents(owner);
CREATE INDEX IF NOT EXISTS ix_chunk_document ON chunks(document);
CREATE INDEX IF NOT EXISTS ix_task_document ON tasks(document);
"""

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("UPDATE documents SET status='failed', error='上次解析被中断，请删除后重新上传' WHERE status IN ('queued','processing')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def log(self, owner, action, result, now):
        with self.connect() as db:
            db.execute('INSERT INTO audit(owner,action,result,created) VALUES(?,?,?,?)',
                       (owner, action, result, now))
