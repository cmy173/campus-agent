"""Local agent journal. Browser ownership and runtime capabilities are not model input.

Separate from the learning DB so installing the agent cannot migrate student data.
One API process owns runtime workers; interrupted turns are never replayed on boot.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..errors import ApiError
from .limits import RunLimits


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path: Path, limits: RunLimits | None = None):
        self.limits = limits or RunLimits.from_env()
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, settings TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
              request_key TEXT NOT NULL, request_hash TEXT NOT NULL, text TEXT NOT NULL,
              status TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', error TEXT,
              token_hash TEXT UNIQUE, model_calls INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(conversation_id, request_key));
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL REFERENCES turns(id),
              kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tool_calls (
              turn_id TEXT NOT NULL REFERENCES turns(id), call_id TEXT NOT NULL,
              request_hash TEXT NOT NULL, result TEXT NOT NULL,
              PRIMARY KEY(turn_id, call_id));
            CREATE TABLE IF NOT EXISTS native_tool_leases (
              turn_id TEXT NOT NULL REFERENCES turns(id), call_id TEXT NOT NULL,
              name TEXT NOT NULL, PRIMARY KEY(turn_id, call_id));
            CREATE TABLE IF NOT EXISTS conversation_files (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
              kind TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
              sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, storage_name TEXT NOT NULL,
              request_key TEXT NOT NULL, created_at TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '',
              UNIQUE(conversation_id, request_key));
            CREATE TABLE IF NOT EXISTS runtime_grants (
              token_hash TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_recovery (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
              source_turn_id TEXT NOT NULL REFERENCES turns(id), request_hash TEXT NOT NULL,
              payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting_auth',
              probe_requested INTEGER NOT NULL DEFAULT 0, next_probe_at REAL NOT NULL DEFAULT 0,
              claim_token TEXT, lease_until REAL NOT NULL DEFAULT 0, probe_result TEXT, error TEXT,
              continuation_turn_id TEXT REFERENCES turns(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(source_turn_id, request_hash));
            CREATE INDEX IF NOT EXISTS auth_recovery_pending ON auth_recovery(status, next_probe_at);
            CREATE INDEX IF NOT EXISTS auth_recovery_conversation ON auth_recovery(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS conversations_owner_updated
              ON conversations(owner, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS turns_conversation_created
              ON turns(conversation_id, created_at, id);
            """)
            if 'attachment_ids' not in {r[1] for r in db.execute('PRAGMA table_info(turns)')}:
                db.execute("ALTER TABLE turns ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]'")
            if 'stop_target' not in {r[1] for r in db.execute('PRAGMA table_info(turns)')}:
                db.execute('ALTER TABLE turns ADD COLUMN stop_target TEXT')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connection() as db:
            # No prewarmed process survives the API ownership epoch.
            db.execute('DELETE FROM runtime_grants')
            db.execute("UPDATE turns SET status='interrupted',token_hash=NULL,stop_target=NULL,error=?,updated_at=? "
                       "WHERE status IN ('queued','running','stopping')",
                       (encoded({"code": "RUNTIME_INTERRUPTED", "message": "服务已重启；已保留调用记录，认证暂停的只读任务会在连接恢复后继续，其他中断操作需核对状态", "retryable": True}), now()))

    def create(self, owner: str, settings: dict) -> dict:
        cid, stamp = "conv_" + secrets.token_hex(16), now()
        with self.connection() as db:
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", (cid, digest(owner), encoded(settings), stamp, stamp))
        return self.conversation(cid, owner)

    @staticmethod
    def _conversation(row):
        return {"id": row["id"], "settings": json.loads(row["settings"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def conversation(self, cid: str, owner: str | None = None) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if row is None or (owner is not None and row["owner"] != digest(owner)):
            raise ApiError(404, "NOT_FOUND", "会话不存在")
        return self._conversation(row)

    def conversations(self, owner: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM conversations WHERE owner=? ORDER BY updated_at DESC LIMIT 100", (digest(owner),)).fetchall()
        return [self._conversation(row) for row in rows]

    def grant_imported_courses(self, cid: str, courses: list[dict]) -> None:
        """Trusted bridge only: extend this conversation by verified import receipts.

        Recheck connection authority under a write transaction. No model-supplied
        owner/scope IDs and no other conversation's grants are accepted here.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT settings FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise ApiError(404, "NOT_FOUND", "会话不存在")
            settings = json.loads(row["settings"])
            allowed = set(settings["connection_ids"])
            if any(item["connection_id"] not in allowed for item in courses):
                raise ApiError(403, "SCOPE_DENIED", "导入来源已不在会话授权范围")
            settings["course_ids"] = sorted(set(settings["course_ids"]) | {item["id"] for item in courses})
            db.execute("UPDATE conversations SET settings=?,updated_at=? WHERE id=?", (encoded(settings), now(), cid))

    def conversation_page(self, owner: str, *, cursor: str | None = None,
                          search: str = "", limit: int = 50) -> dict:
        """Owner-scoped, literal text search and keyset pagination.

        Cursor contents are not authority: every query still filters the owner. Bind
        it to the owner and search to avoid accidentally mixing paged query results.
        No prompts or response bodies are returned by search; only the first question
        summary, already visible to the authenticated conversation owner.
        """
        if not 1 <= limit <= 100 or len(search) > 200:
            raise ApiError(400, "INVALID_INPUT", "历史查询范围无效")
        search = search.strip()
        owner_hash, search_hash = digest(owner), digest(search)
        clauses, parameters = ["c.owner=?"], [owner_hash]
        if cursor is not None:
            try:
                if len(cursor) > 1024:
                    raise ValueError()
                value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
                if (not isinstance(value, dict) or set(value) != {"v", "t", "i", "q", "o"}
                        or value["v"] != 1 or value["q"] != search_hash or value["o"] != owner_hash
                        or not isinstance(value["t"], str) or not isinstance(value["i"], str)
                        or len(value["t"]) > 64 or not value["i"].startswith("conv_") or len(value["i"]) > 128):
                    raise ValueError()
                datetime.fromisoformat(value["t"])
            except (ValueError, TypeError, UnicodeError, binascii.Error):
                raise ApiError(400, "INVALID_INPUT", "历史查询游标已失效，请重新搜索") from None
            clauses.append("(c.updated_at < ? OR (c.updated_at = ? AND c.id < ?))")
            parameters.extend([value["t"], value["t"], value["i"]])
        if search:
            # instr is literal (%, _ are not SQL wildcards). Case-insensitive for
            # ASCII; Chinese matching is unaffected by SQLite's lower().
            clauses.append("EXISTS (SELECT 1 FROM turns s WHERE s.conversation_id=c.id "
                           "AND (instr(lower(s.text),lower(?))>0 OR instr(lower(s.response),lower(?))>0))")
            parameters.extend([search, search])
        with self.connection() as db:
            rows = db.execute("SELECT c.*, (SELECT substr(t.text,1,80) FROM turns t "
                              "WHERE t.conversation_id=c.id ORDER BY t.created_at,t.id LIMIT 1) AS title "
                              "FROM conversations c WHERE " + " AND ".join(clauses) +
                              " ORDER BY c.updated_at DESC,c.id DESC LIMIT ?", parameters + [limit + 1]).fetchall()
        page = rows[:limit]
        items = [self._conversation(row) | {"title": " ".join(row["title"].split()) if row["title"] else None} for row in page]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(encoded({"v": 1, "t": last["updated_at"], "i": last["id"],
                                                          "q": search_hash, "o": owner_hash}).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _turn(row):
        return {key: row[key] for key in ("id", "conversation_id", "text", "status", "response", "created_at", "updated_at")} | {
            "error": json.loads(row["error"]) if row["error"] else None,
            "attachment_ids": json.loads(row['attachment_ids'])}

    def turns(self, cid: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY created_at", (cid,)).fetchall()
        return [self._turn(row) for row in rows]

    def turn(self, tid: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "对话轮次不存在")
        return self._turn(row)

    def enqueue(self, cid: str, key: str, text: str, course_id: str | None, clear_course_context: bool = False, action_context: dict | None = None, attachment_ids: list[str] | None = None, goal_resume: dict | None = None) -> tuple[dict, str | None]:
        request = {"text": text, "course_id": course_id}
        attachment_ids = list(dict.fromkeys(attachment_ids or []))
        if attachment_ids:
            request['attachment_ids'] = attachment_ids
        if clear_course_context:
            request["clear_course_context"] = True
        if action_context is not None:
            request["action_context"] = action_context
        if goal_resume is not None:
            request['goal_resume'] = goal_resume
        fingerprint = digest(encoded(request))
        tid, token, stamp = "turn_" + secrets.token_hex(16), secrets.token_urlsafe(32), now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM turns WHERE conversation_id=? AND request_key=?", (cid, key)).fetchone()
            if old:
                if old["request_hash"] != fingerprint:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一请求键不能用于不同消息")
                return self._turn(old), None
            if db.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status IN ('queued','running','stopping')", (cid,)).fetchone():
                raise ApiError(409, "TURN_IN_PROGRESS", "当前会话仍在处理上一条消息")
            for file_id in attachment_ids:
                if not db.execute('SELECT 1 FROM conversation_files WHERE id=? AND conversation_id=?', (file_id, cid)).fetchone():
                    raise ApiError(404, 'NOT_FOUND', '附件不属于当前会话或已不可用')
            settings = json.loads(db.execute("SELECT settings FROM conversations WHERE id=?", (cid,)).fetchone()[0])
            if course_id is not None:
                if course_id not in settings["course_ids"]:
                    raise ApiError(403, "SCOPE_DENIED", "该课程不在会话范围内")
                settings["current_course_id"] = course_id
            elif clear_course_context:
                settings["current_course_id"] = None
            db.execute("UPDATE conversations SET settings=?,updated_at=? WHERE id=?", (encoded(settings), stamp, cid))
            db.execute("INSERT INTO turns (id,conversation_id,request_key,request_hash,text,status,token_hash,created_at,updated_at,attachment_ids) "
                       "VALUES (?,?,?,?,?,'queued',?,?,?,?)", (tid, cid, key, fingerprint, text, digest(token), stamp, stamp, encoded(attachment_ids)))
            if action_context is not None:
                db.execute("INSERT INTO events (turn_id,kind,data,created_at) VALUES (?,?,?,?)",
                           (tid, "accepted_action", encoded(action_context), stamp))
            if goal_resume is not None:
                db.execute('INSERT INTO events (turn_id,kind,data,created_at) VALUES (?,?,?,?)',
                           (tid, 'accepted_goal_resume', encoded(goal_resume), stamp))
        return self.turn(tid), token

    def runtime(self, token: str, cid: str | None = None) -> tuple[dict, dict]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM turns WHERE token_hash=? AND status IN ('queued','running')", (digest(token),)).fetchone()
        if row is None or (cid is not None and row["conversation_id"] != cid):
            raise ApiError(403, "RUNTIME_DENIED", "运行时授权已失效或不属于当前会话")
        return self._turn(row), self.conversation(row["conversation_id"])

    def grant(self, cid: str) -> str:
        """Mint (or replace) the bridge token a prewarmed runtime boots with.

        A prewarmed runner has to authenticate before the conversation's first turn
        exists, so the grant is scoped to the conversation rather than to a turn. Only
        the local loopback runtime ever sees it.
        """
        token = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute('DELETE FROM runtime_grants WHERE conversation_id=?', (cid,))
            db.execute('INSERT INTO runtime_grants (token_hash, conversation_id, created_at) VALUES (?,?,?)',
                       (digest(token), cid, now()))
        return token

    def granted(self, token: str):
        """The conversation a live grant belongs to, or None."""
        with self.connection() as db:
            row = db.execute('SELECT conversation_id FROM runtime_grants WHERE token_hash=?', (digest(token),)).fetchone()
        return self.conversation(row['conversation_id']) if row is not None else None

    def revoke_grant(self, cid: str):
        with self.connection() as db:
            db.execute('DELETE FROM runtime_grants WHERE conversation_id=?', (cid,))

    def bind_token(self, tid: str, token: str) -> bool:
        """Point this turn at an already-owned runtime's bridge token.

        A runner process that is reused across turns of one conversation keeps the
        bridge token it was started with, so the next turn must become addressable by
        that same token instead of the fresh one minted in `enqueue`. The previous turn
        released its hash when it settled, so the unique column stays free.
        """
        with self.connection() as db:
            return bool(db.execute("UPDATE turns SET token_hash=?,updated_at=? "
                                   "WHERE id=? AND status IN ('queued','running')",
                                   (digest(token), now(), tid)).rowcount)

    def running(self, tid: str) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE turns SET status='running',updated_at=? WHERE id=? AND status='queued'", (now(), tid)).rowcount)

    def finish(self, tid: str, status: str, *, response: str = "", error: dict | None = None):
        with self.connection() as db:
            db.execute("UPDATE turns SET status=?,response=?,error=?,token_hash=NULL,updated_at=? "
                       "WHERE id=? AND status IN ('queued','running')", (status, response, encoded(error) if error else None, now(), tid))

    def request_stop(self, tid: str, target: str, error: dict | None = None) -> bool:
        if target not in ('cancelled', 'failed'):
            raise ValueError('Invalid stop result')
        with self.connection() as db:
            return bool(db.execute("UPDATE turns SET status='stopping',stop_target=?,error=?,token_hash=NULL,updated_at=? "
                "WHERE id=? AND status IN ('queued','running')", (target, encoded(error) if error else None, now(), tid)).rowcount)

    def complete_stop(self, tid: str):
        with self.connection() as db:
            db.execute("UPDATE turns SET status=stop_target,stop_target=NULL,updated_at=? WHERE id=? AND status='stopping'", (now(), tid))

    def event(self, tid: str, kind: str, data: dict):
        with self.connection() as db:
            db.execute("INSERT INTO events (turn_id,kind,data,created_at) VALUES (?,?,?,?)", (tid, kind, encoded(data), now()))

    def events(self, tid: str, after: int = 0) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT 100", (tid, after)).fetchall()
        items = [{"seq": r["seq"], "kind": r["kind"], "data": json.loads(r["data"]), "created_at": r["created_at"]} for r in rows]
        return {"items": items, "next_cursor": items[-1]["seq"] if items else after}

    def last_failure(self, tid: str) -> dict | None:
        """The concrete model failure this turn already recorded, if any.

        The runtime reports the precise provider/budget failure through
        `/internal/model-error` (journaled as `model_error`) *before* the harness aborts the
        turn, and the harness frame that follows only says HARNESS_FAILED. Keeping the
        recorded cause lets the turn error stay actionable, e.g. BUDGET_EXCEEDED instead of
        "运行时未完成".
        """
        with self.connection() as db:
            row = db.execute("SELECT data FROM events WHERE turn_id=? AND kind='model_error' "
                             "ORDER BY seq DESC LIMIT 1", (tid,)).fetchone()
        return json.loads(row['data']) if row is not None else None

    def child(self, tid: str, child_id: str) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT kind,data FROM events WHERE turn_id=? AND kind IN ('subagent.started','subagent.finished') ORDER BY seq", (tid,)).fetchall()
        child = None
        for row in rows:
            data = json.loads(row['data'])
            if data.get('childSessionId') != child_id:
                continue
            if row['kind'] == 'subagent.started':
                child = {'parent_id': data['parentSessionId'], 'finished': False}
            elif child:
                child['finished'] = True
        if child is None:
            raise ApiError(404, 'NOT_FOUND', '本轮没有这个子任务')
        return child

    def reserve_model(self, tid: str):
        with self.connection() as db:
            changed = db.execute("UPDATE turns SET model_calls=model_calls+1 WHERE id=? AND status='running' AND model_calls<?", (tid, self.limits.model_calls)).rowcount
        if not changed:
            raise ApiError(409, "BUDGET_EXCEEDED", "本轮模型调用达到上限或已停止")

    def cached_call(self, tid: str, call_id: str, request: dict) -> dict | None:
        """Look up a replay of `request` under the trusted call id.

        `request` must already be the gateway's *cache identity* (business arguments only, see
        `tools.cache_arguments`). This method and `cache_call` must always receive the same
        normalisation: hashing raw arguments on one side and normalised ones on the other is what
        made an identical replay of a first call carrying `wait_seconds` report a conflict.
        """
        with self.connection() as db:
            row = db.execute("SELECT * FROM tool_calls WHERE turn_id=? AND call_id=?", (tid, call_id)).fetchone()
            count = self._tool_count(db, tid)
        if row:
            if row["request_hash"] != digest(encoded(request)):
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "工具调用 ID 已用于不同参数")
            return json.loads(row["result"])
        if count >= self.limits.tool_calls:
            raise ApiError(409, "BUDGET_EXCEEDED", "本轮工具调用达到上限")
        return None

    @staticmethod
    def _tool_count(db, tid: str) -> int:
        return db.execute('SELECT (SELECT COUNT(*) FROM tool_calls WHERE turn_id=?) + '
                          '(SELECT COUNT(*) FROM native_tool_leases WHERE turn_id=?)', (tid, tid)).fetchone()[0]

    def reserve_native_tool(self, tid: str, call_id: str, name: str):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT name FROM native_tool_leases WHERE turn_id=? AND call_id=?', (tid, call_id)).fetchone()
            if row:
                if row['name'] != name:
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '通用工具调用 ID 已用于不同工具')
                return
            if not db.execute("SELECT 1 FROM turns WHERE id=? AND status='running'", (tid,)).fetchone():
                raise ApiError(409, 'TURN_STOPPED', '本轮已停止')
            if self._tool_count(db, tid) >= self.limits.tool_calls:
                raise ApiError(409, 'BUDGET_EXCEEDED', '本轮工具调用达到配置上限')
            db.execute('INSERT INTO native_tool_leases VALUES (?,?,?)', (tid, call_id, name))

    def cache_call(self, tid: str, call_id: str, request: dict, result: dict):
        """Store one call outcome under the same cache identity `cached_call` reads.

        Idempotent by design: a call whose previous envelope said "you may retry this" is executed
        again under the same call id, and the retry must be able to store its real outcome. A
        *different* business request under the same id stays refused.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request_hash FROM tool_calls WHERE turn_id=? AND call_id=?",
                             (tid, call_id)).fetchone()
            # Idempotent by design: a call whose previous envelope said "you may retry this"
            # is executed again under the same call id, and the retry must be able to store its
            # real outcome. A *different* request under the same id stays refused.
            if row is not None:
                if row["request_hash"] != digest(encoded(request)):
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "工具调用 ID 已用于不同参数")
                db.execute("DELETE FROM tool_calls WHERE turn_id=? AND call_id=?", (tid, call_id))
            db.execute("INSERT INTO tool_calls VALUES (?,?,?,?)", (tid, call_id, digest(encoded(request)), encoded(result)))

    def recovery_events(self, tid: str) -> list[dict]:
        """Full journal, not the public endpoint's first 100 event page."""
        with self.connection() as db:
            rows = db.execute('SELECT * FROM events WHERE turn_id=? ORDER BY seq', (tid,)).fetchall()
        return [{"seq": r['seq'], "kind": r['kind'], "data": json.loads(r['data']),
                 "created_at": r['created_at']} for r in rows]

    @staticmethod
    def _recovery(row):
        item = dict(row)
        for key in ('payload', 'probe_result', 'error'):
            item[key] = json.loads(item[key]) if item[key] else None
        return item

    def save_auth_recovery(self, tid: str, payload: dict) -> dict:
        fingerprint = digest(encoded(payload['pending_tool']))
        checkpoint_id, stamp = 'auth_' + secrets.token_hex(16), now()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            source = db.execute('SELECT conversation_id FROM turns WHERE id=?', (tid,)).fetchone()
            if source is None:
                raise ApiError(404, 'NOT_FOUND', '认证恢复的来源轮次不存在')
            db.execute('INSERT OR IGNORE INTO auth_recovery '
                       '(id,conversation_id,source_turn_id,request_hash,payload,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (checkpoint_id, source['conversation_id'], tid, fingerprint, encoded(payload), stamp, stamp))
            row = db.execute('SELECT * FROM auth_recovery WHERE source_turn_id=? AND request_hash=?',
                             (tid, fingerprint)).fetchone()
        return self._recovery(row)

    def auth_recoveries(self, cid: str | None = None, *, pending: bool = False) -> list[dict]:
        clauses, args = [], []
        if cid is not None:
            clauses.append('conversation_id=?')
            args.append(cid)
        if pending:
            clauses.append("status IN ('waiting_auth','ready')")
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connection() as db:
            rows = db.execute('SELECT * FROM auth_recovery' + where + ' ORDER BY created_at,id', args).fetchall()
        return [self._recovery(row) for row in rows]

    def auth_checkpoint(self, checkpoint_id: str) -> dict:
        with self.connection() as db:
            row = db.execute('SELECT * FROM auth_recovery WHERE id=?', (checkpoint_id,)).fetchone()
        if row is None:
            raise ApiError(404, 'NOT_FOUND', '认证恢复任务不存在')
        return self._recovery(row)

    def restart_auth_recovery(self):
        """Only the single API runtime owner calls this at boot; re-probe stale ready reads."""
        with self.connection() as db:
            db.execute("UPDATE auth_recovery SET status='waiting_auth',probe_requested=1,claim_token=NULL,lease_until=0 "
                       "WHERE status IN ('waiting_auth','ready')")

    def request_auth_probes(self):
        with self.connection() as db:
            db.execute("UPDATE auth_recovery SET probe_requested=1,updated_at=? WHERE status='waiting_auth'", (now(),))

    def claim_auth_recovery(self, checkpoint_id: str, moment: float, lease_seconds: float = 120) -> dict | None:
        """SQLite claim + conversation-idle check prevents concurrent probes/launches."""
        claim = secrets.token_hex(16)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_recovery WHERE id=?', (checkpoint_id,)).fetchone()
            if (row is None or row['status'] not in ('waiting_auth', 'ready')
                    or (row['claim_token'] and row['lease_until'] > moment)
                    or (row['status'] == 'waiting_auth' and
                        (not row['probe_requested'] or row['next_probe_at'] > moment))):
                return None
            if db.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status IN ('queued','running','stopping')",
                          (row['conversation_id'],)).fetchone():
                return None
            db.execute('UPDATE auth_recovery SET claim_token=?,lease_until=?,updated_at=? WHERE id=?',
                       (claim, moment + lease_seconds, now(), checkpoint_id))
            row = db.execute('SELECT * FROM auth_recovery WHERE id=?', (checkpoint_id,)).fetchone()
        return self._recovery(row)

    def settle_auth_probe(self, checkpoint_id: str, claim: str, *, result: dict | None,
                          error: dict | None, next_probe_at: float) -> bool:
        with self.connection() as db:
            return bool(db.execute('UPDATE auth_recovery SET status=?,probe_result=?,error=?,probe_requested=0,'
                                   'next_probe_at=?,claim_token=NULL,lease_until=0,updated_at=? '
                                   'WHERE id=? AND claim_token=?',
                                   ('ready' if error is None else 'waiting_auth', encoded(result) if result is not None else None,
                                    encoded(error) if error else None, next_probe_at, now(), checkpoint_id, claim)).rowcount)

    def release_auth_claim(self, checkpoint_id: str, claim: str):
        with self.connection() as db:
            db.execute('UPDATE auth_recovery SET claim_token=NULL,lease_until=0 WHERE id=? AND claim_token=?',
                       (checkpoint_id, claim))

    def enqueue_auth_recovery(self, checkpoint_id: str, claim: str, context: dict) -> tuple[dict, str] | None:
        """Claim, checkpoint-id request key, accepted context and turn are one transaction.

        No action acceptance or native goal-resume authority is inherited. The same
        official Harness conversation/session ID supplies the previous history.
        """
        tid, token, stamp = 'turn_' + secrets.token_hex(16), secrets.token_urlsafe(32), now()
        text = '校园认证已由用户完成；继续原任务。使用可信恢复上下文中的已取得结果与待处理只读结果，不重复已完成步骤，不执行写入或登录。'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_recovery WHERE id=? AND claim_token=? AND status=\'ready\'',
                             (checkpoint_id, claim)).fetchone()
            if row is None:
                return None
            if db.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status IN ('queued','running','stopping')",
                          (row['conversation_id'],)).fetchone():
                return None
            key = 'auth-resume:' + checkpoint_id
            old = db.execute('SELECT id FROM turns WHERE conversation_id=? AND request_key=?',
                             (row['conversation_id'], key)).fetchone()
            if old:
                db.execute("UPDATE auth_recovery SET status='resumed',continuation_turn_id=?,claim_token=NULL,lease_until=0,updated_at=? WHERE id=?",
                           (old['id'], stamp, checkpoint_id))
                return None
            db.execute('INSERT INTO turns (id,conversation_id,request_key,request_hash,text,status,token_hash,created_at,updated_at) '
                       "VALUES (?,?,?,?,?,'queued',?,?,?)", (tid, row['conversation_id'], key, digest(encoded(context)), text, digest(token), stamp, stamp))
            db.execute('INSERT INTO events (turn_id,kind,data,created_at) VALUES (?,?,?,?)',
                       (tid, 'accepted_auth_resume', encoded(context), stamp))
            db.execute("UPDATE auth_recovery SET status='resumed',continuation_turn_id=?,claim_token=NULL,lease_until=0,updated_at=? WHERE id=?",
                       (tid, stamp, checkpoint_id))
            db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (stamp, row['conversation_id']))
        return self.turn(tid), token

    def cancel_auth_recovery(self, checkpoint_id: str, claim: str):
        with self.connection() as db:
            db.execute("UPDATE auth_recovery SET status='cancelled',claim_token=NULL,lease_until=0,probe_requested=0,updated_at=? "
                       'WHERE id=? AND claim_token=?', (now(), checkpoint_id, claim))

    def auth_launch_failed(self, checkpoint_id: str):
        with self.connection() as db:
            db.execute("UPDATE auth_recovery SET status='failed',error=?,updated_at=? WHERE id=?",
                       (encoded({'code': 'RECOVERY_LAUNCH_FAILED', 'message': '认证恢复启动失败，检查点已保留', 'retryable': True}), now(), checkpoint_id))
