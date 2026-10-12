"""SQLite implementations; platform adapters never import this module."""
import json


class SnapshotRepository:
    def __init__(self, store):
        self.store = store

    def read(self, owner):
        with self.store.connect() as db:
            row = db.execute('SELECT snapshot FROM campus_snapshots WHERE owner=?', (owner,)).fetchone()
        return json.loads(row['snapshot']) if row else {'profile': None, 'courses': None, 'grades': None}

    def write(self, owner, snapshot):
        with self.store.connect() as db:
            db.execute('INSERT INTO campus_snapshots(owner,snapshot) VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET snapshot=excluded.snapshot',
                       (owner, json.dumps(snapshot, ensure_ascii=False)))

    def delete(self, owner):
        with self.store.connect() as db:
            db.execute('DELETE FROM campus_snapshots WHERE owner=?', (owner,))


class SQLiteCacheRepository:
    def __init__(self, store):
        self.store = store

    def read(self, owner, platform, capability, scope):
        with self.store.connect() as db:
            row = db.execute('SELECT result FROM connector_cache WHERE owner=? AND platform=? AND capability=? AND scope=?',
                             (owner, platform, capability, scope)).fetchone()
        return json.loads(row['result']) if row else None

    def write(self, owner, platform, capability, scope, result):
        with self.store.connect() as db:
            db.execute('INSERT INTO connector_cache(owner,platform,capability,scope,result) VALUES(?,?,?,?,?) '
                'ON CONFLICT(owner,platform,capability,scope) DO UPDATE SET result=excluded.result',
                (owner, platform, capability, scope, json.dumps(result, ensure_ascii=False)))

    def scopes(self, owner, platform, capability):
        with self.store.connect() as db:
            rows = db.execute('SELECT scope FROM connector_cache WHERE owner=? AND platform=? AND capability=?',
                              (owner, platform, capability)).fetchall()
        return [row['scope'] for row in rows]

    def delete(self, owner):
        with self.store.connect() as db:
            db.execute('DELETE FROM connector_cache WHERE owner=?', (owner,))
