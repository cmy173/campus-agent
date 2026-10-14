"""SQLite persistence. Every domain query is scoped to a server-authenticated owner."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passport_accounts (
 identifier TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE);
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
CREATE TABLE IF NOT EXISTS document_sources (
 document TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
 owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 platform TEXT NOT NULL, record_id TEXT NOT NULL, origin TEXT NOT NULL,
 college TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, published_at TEXT NOT NULL DEFAULT '',
 body_sha256 TEXT NOT NULL DEFAULT '', attachments TEXT NOT NULL DEFAULT '[]',
 has_unread_content INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_analyses (
 document TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 body_sha256 TEXT NOT NULL, extractor TEXT NOT NULL, prompt_version TEXT NOT NULL,
 model TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', result_at TEXT,
 error TEXT, attempts INTEGER NOT NULL DEFAULT 0, usage TEXT NOT NULL DEFAULT '{}',
 updated TEXT NOT NULL,
 PRIMARY KEY(document, body_sha256, extractor, prompt_version, model));
CREATE INDEX IF NOT EXISTS ix_doc_owner ON documents(owner);
CREATE INDEX IF NOT EXISTS ix_chunk_document ON chunks(document);
CREATE INDEX IF NOT EXISTS ix_task_document ON tasks(document);
CREATE INDEX IF NOT EXISTS ix_analysis_owner ON notice_analyses(owner);
CREATE TABLE IF NOT EXISTS campus_snapshots (
 owner TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connector_cache (
 owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 platform TEXT NOT NULL, capability TEXT NOT NULL, scope TEXT NOT NULL,
 result TEXT NOT NULL,
 PRIMARY KEY(owner, platform, capability, scope));
CREATE TABLE IF NOT EXISTS campus_credentials (
 owner TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 webvpn_username TEXT NOT NULL,
 webvpn_password_encrypted TEXT NOT NULL,
 zjuam_username TEXT NOT NULL,
 zjuam_password_encrypted TEXT NOT NULL,
 encryption_version INTEGER NOT NULL DEFAULT 1,
 webvpn_cookies TEXT, webvpn_expires_at TEXT,
 auto_sync_enabled INTEGER NOT NULL DEFAULT 1,
 sync_interval_minutes INTEGER NOT NULL DEFAULT 30,
 last_sync_at TEXT, last_sync_status TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 last_used_at TEXT);
"""

class Store:
    # Additive only: existing rows keep their meaning, older databases are upgraded in place.
    TASK_COLUMNS = (('origin', "TEXT NOT NULL DEFAULT 'rules'"), ('event_ref', "TEXT NOT NULL DEFAULT ''"),
        ('analysis_key', "TEXT NOT NULL DEFAULT ''"), ('evidence', "TEXT NOT NULL DEFAULT '[]'"),
        ('time_kind', "TEXT NOT NULL DEFAULT ''"), ('time_precision', "TEXT NOT NULL DEFAULT ''"),
        ('location', "TEXT NOT NULL DEFAULT ''"), ('needs_attachment', 'INTEGER NOT NULL DEFAULT 0'),
        ('attachment_refs', "TEXT NOT NULL DEFAULT '[]'"), ('derived_note', "TEXT NOT NULL DEFAULT ''"))
    
    CREDENTIAL_COLUMNS = (
        ('webvpn_cookies', "TEXT"),
        ('webvpn_expires_at', "TEXT"),
        ('auto_sync_enabled', 'INTEGER NOT NULL DEFAULT 1'),
        ('sync_interval_minutes', 'INTEGER NOT NULL DEFAULT 30'),
        ('last_sync_at', "TEXT"),
        ('last_sync_status', "TEXT")
    )

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            existing_tasks = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            for name, declaration in self.TASK_COLUMNS:
                if name not in existing_tasks:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {declaration}')

            # WebVPN session and sync state live per account, not per browser session.
            existing_creds = {row['name'] for row in db.execute('PRAGMA table_info(campus_credentials)')}
            for name, declaration in self.CREDENTIAL_COLUMNS:
                if name not in existing_creds:
                    db.execute(f'ALTER TABLE campus_credentials ADD COLUMN {name} {declaration}')

            db.execute("UPDATE documents SET status='failed', error='上次解析被中断，请删除后重新上传' WHERE status IN ('queued','processing')")
            # An interrupted run must not look in progress forever; the last result survives.
            db.execute("""UPDATE notice_analyses SET status='failed', updated=updated,
                error='{"code":"INTERRUPTED","message":"服务重启导致整理中断，请重试"}'
                WHERE status='running'""")

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
