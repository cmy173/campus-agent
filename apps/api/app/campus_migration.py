"""Read-only legacy inventory and reentrant exact-data import with guarded rollback.

No legacy Store is instantiated: its constructor upgrades the source and changes
interrupted rows. Credentials, session cookies, password hashes and audit logs are
never selected or copied. Unknown tables are reported, not silently discarded.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .errors import ApiError

EXCLUDED = {"users", "passport_accounts", "sessions", "campus_credentials", "audit"}
SUPPORTED = {"documents", "chunks", "tasks", "document_sources", "notice_analyses",
             "campus_snapshots", "connector_cache"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@contextmanager
def readonly_source(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")  # one consistent logical snapshot, including a live WAL
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def inventory(path):
    with readonly_source(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {table: db.execute('SELECT count(*) FROM "'+table.replace('"','""')+'"').fetchone()[0]
                  for table in tables if not table.startswith("sqlite_")}
        owners = [r[0] for r in db.execute("SELECT id FROM users ORDER BY id")] if "users" in tables else []
        owner_counts = {owner: {table: len(_rows(db, table, owner)) for table in sorted(tables & SUPPORTED)} for owner in owners}
        passport_owners = [r[0] for r in db.execute("SELECT owner FROM passport_accounts ORDER BY owner")] if "passport_accounts" in tables else []
        return {"owners": owners, "owner_counts": owner_counts, "passport_owners": passport_owners, "counts": counts,
                "supported": sorted(tables & SUPPORTED), "excluded": sorted(tables & EXCLUDED),
                "unmapped": sorted(tables-SUPPORTED-EXCLUDED-{"sqlite_sequence"}),
                "source_read_only": True}


def _rows(db, table, owner):
    if table in {"chunks", "tasks"}:
        return db.execute(f"SELECT x.* FROM {table} x JOIN documents d ON d.id=x.document WHERE d.owner=?", (owner,)).fetchall()
    return db.execute(f"SELECT * FROM {table} WHERE owner=?", (owner,)).fetchall()


def import_legacy(path, store, owner, *, include_school_cache=False):
    """Explicitly maps one legacy opaque owner to the one local workspace.

    Exact records preserve time precision, evidence, confirmation, task state,
    chunks, extraction diagnostics and original bytes. They stay distinguishable
    from newly observed school facts; source credentials are never migrated.
    """
    if Path(path).resolve() == store.path.resolve():
        raise ValueError("Source and destination must differ")
    with readonly_source(path) as source:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "users" not in tables or not source.execute("SELECT 1 FROM users WHERE id=?", (owner,)).fetchone():
            raise ValueError("Explicit source owner does not exist")
        unknown = tables-SUPPORTED-EXCLUDED-{"sqlite_sequence"}
        if unknown:
            raise ValueError("Unmapped tables require review: "+", ".join(sorted(unknown)))
        collected = []
        for table in sorted(tables & SUPPORTED):
            for row in _rows(source, table, owner):
                payload = dict(row)
                content = payload.pop("content", None)
                if content is not None:
                    content = bytes(content)
                    payload["content_sha256"] = hashlib.sha256(content).hexdigest()
                    payload["content_bytes"] = len(content)
                # Non-ID multi-column rows get a deterministic opaque record key.
                record_id = str(payload.get("id") or payload.get("document") or hashlib.sha256(canonical(payload).encode()).hexdigest())
                if table in {"notice_analyses", "connector_cache"}:
                    record_id = hashlib.sha256(canonical(payload).encode()).hexdigest()
                collected.append((table, record_id, payload, content))
        collected.sort(key=lambda r: (r[0], r[1], canonical(r[2])))
    fingerprint = hashlib.sha256(canonical([[t, i, p] for t, i, p, _ in collected]).encode()).hexdigest()
    batch_id = "mig_"+hashlib.sha256((fingerprint+owner+str(include_school_cache)).encode()).hexdigest()[:32]
    counts = {t: sum(1 for row in collected if row[0] == t) for t in sorted(SUPPORTED)}
    manifest = {"counts": counts, "mapping": {"source_owner": owner, "target_owner": "local"},
                "school_cache_imported": include_school_cache, "credentials_imported": False,
                "notes_feedback_plans": "No such tables in supported legacy schema; unknown tables block import",
                "status": "imported", "domain_promotion": "explicit course and time estimate required"}
    with store.connect() as target:
        found = target.execute("SELECT manifest FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if found:
            return {"batch_id": batch_id, "replayed": True, **json.loads(found[0])}
        target.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?)", (batch_id, fingerprint, owner, "local", canonical(manifest), datetime.now(timezone.utc).isoformat()))
        for table, record_id, payload, content in collected:
            target.execute("INSERT INTO imported_records VALUES(?,?,?,?,?)", (batch_id, table, record_id, canonical(payload), content))
            if not include_school_cache or table not in {"campus_snapshots", "connector_cache"}:
                continue
            if table == "campus_snapshots":
                key = {"owner": "local"}
                snapshot = json.loads(payload["snapshot"])
                for section in (snapshot.get(k) for k in ("profile", "courses", "grades")):
                    if isinstance(section, dict):
                        section["stale"] = True
                        for field in section.get("field_sources", {}).values():
                            field["stale"] = True
                        for term in section.get("terms", []):
                            term["stale"] = True
                        for record in section.get("data", []) if isinstance(section.get("data"), list) else []:
                            if isinstance(record, dict): record["stale"] = True
                for calendar in snapshot.get("calendars", {}).values():
                    calendar["stale"] = True
                after = {"snapshot": canonical(snapshot)}
            else:
                key = {"owner": "local", **{k: payload[k] for k in ("platform", "capability", "scope")}}
                cached_result = json.loads(payload["result"])
                cached_result["freshness"] = "stale" if cached_result.get("fetched_at") else "unknown"
                for record in cached_result.get("records", []):
                    record["stale"] = True
                after = {"result": canonical(cached_result)}
            where = " AND ".join(k+"=?" for k in key)
            existing = target.execute(f"SELECT {','.join(after)} FROM {table} WHERE {where}", tuple(key.values())).fetchone()
            before = dict(existing) if existing else None
            # Never silently replace an existing person's or newer observation's cache.
            if before is not None and before != after:
                raise ValueError("Destination school cache differs; import without --school-cache or clear it explicitly")
            target.execute("INSERT INTO import_changes VALUES(?,?,?,?,?)", (batch_id, table, canonical(key), canonical(before) if before else None, canonical(after)))
            if before is None:
                fields = key | after
                target.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(fields.values()))
    return {"batch_id": batch_id, "replayed": False, **manifest}


def rollback_import(store, batch_id):
    with store.connect() as db:
        batch = db.execute("SELECT 1 FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            return {"batch_id": batch_id, "removed": False}
        changes = db.execute("SELECT * FROM import_changes WHERE batch_id=?", (batch_id,)).fetchall()
        for change in changes:
            key, after = json.loads(change["key_json"]), json.loads(change["after_json"])
            where = " AND ".join(k+"=?" for k in key)
            current = db.execute(f"SELECT {','.join(after)} FROM {change['table_name']} WHERE {where}", tuple(key.values())).fetchone()
            if current is None or dict(current) != after:
                raise ValueError("Imported cache changed after import; rollback refused without discarding new work")
        for change in changes:
            key = json.loads(change["key_json"])
            where = " AND ".join(k+"=?" for k in key)
            if change["before_json"] is None:
                db.execute(f"DELETE FROM {change['table_name']} WHERE {where}", tuple(key.values()))
        db.execute("DELETE FROM import_changes WHERE batch_id=?", (batch_id,))
        db.execute("DELETE FROM imported_records WHERE batch_id=?", (batch_id,))
        db.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    return {"batch_id": batch_id, "removed": True}


def list_imports(store):
    with store.connect() as db:
        rows = db.execute("SELECT id,manifest,created_at FROM import_batches ORDER BY created_at").fetchall()
        documents = db.execute("SELECT batch_id,record_id,payload FROM imported_records WHERE table_name='documents'").fetchall()
    return {"items": [{"id": r["id"], "created_at": r["created_at"], **json.loads(r["manifest"]),
        "documents": [{"id": d["record_id"], "name": json.loads(d["payload"]).get("name"),
                       "status": json.loads(d["payload"]).get("status"),
                       "content_bytes": json.loads(d["payload"]).get("content_bytes")}
                      for d in documents if d["batch_id"] == r["id"]]} for r in rows]}


def read_imported_document(store, batch_id, document_id):
    with store.connect() as db:
        doc = db.execute("SELECT payload FROM imported_records WHERE batch_id=? AND table_name='documents' AND record_id=?", (batch_id, document_id)).fetchone()
        if not doc:
            raise ApiError(404, "NOT_FOUND", "导入的资料不存在")
        related = db.execute("SELECT table_name,payload FROM imported_records WHERE batch_id=? AND table_name IN ('chunks','tasks','document_sources','notice_analyses')", (batch_id,)).fetchall()
    result = {"document": json.loads(doc[0]), "chunks": [], "tasks": [], "document_sources": [], "notice_analyses": []}
    for row in related:
        item = json.loads(row["payload"])
        if item.get("document") == document_id:
            result[row["table_name"]].append(item)
    result["chunks"].sort(key=lambda item: item.get("position", 0))
    result["source"] = "legacy_import"
    return result


def imported_content(store, batch_id, document_id):
    with store.connect() as db:
        row = db.execute("SELECT payload,content FROM imported_records WHERE batch_id=? AND table_name='documents' AND record_id=?", (batch_id, document_id)).fetchone()
    if not row:
        raise ApiError(404, "NOT_FOUND", "导入的资料不存在")
    return bytes(row["content"] or b""), str(json.loads(row["payload"]).get("name") or "imported-document")
