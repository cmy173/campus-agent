"""Local agent journal. Browser ownership and runtime capabilities are not model input.

Separate from the learning DB so installing the agent cannot migrate student data.
One API process owns runtime workers; interrupted turns are never replayed on boot.
"""
from __future__ import annotations

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
            """)

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
            db.execute("UPDATE turns SET status='interrupted',token_hash=NULL,error=?,updated_at=? "
                       "WHERE status IN ('queued','running')",
                       (encoded({"code": "RUNTIME_INTERRUPTED", "message": "服务已重启；已保留调用记录，请重新发送本轮请求", "retryable": True}), now()))

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

    @staticmethod
    def _turn(row):
        return {key: row[key] for key in ("id", "conversation_id", "text", "status", "response", "created_at", "updated_at")} | {
            "error": json.loads(row["error"]) if row["error"] else None}

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

    def enqueue(self, cid: str, key: str, text: str, course_id: str | None, clear_course_context: bool = False, action_context: dict | None = None) -> tuple[dict, str | None]:
        request = {"text": text, "course_id": course_id}
        if clear_course_context:
            request["clear_course_context"] = True
        if action_context is not None:
            request["action_context"] = action_context
        fingerprint = digest(encoded(request))
        tid, token, stamp = "turn_" + secrets.token_hex(16), secrets.token_urlsafe(32), now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM turns WHERE conversation_id=? AND request_key=?", (cid, key)).fetchone()
            if old:
                if old["request_hash"] != fingerprint:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一请求键不能用于不同消息")
                return self._turn(old), None
            if db.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status IN ('queued','running')", (cid,)).fetchone():
                raise ApiError(409, "TURN_IN_PROGRESS", "当前会话仍在处理上一条消息")
            settings = json.loads(db.execute("SELECT settings FROM conversations WHERE id=?", (cid,)).fetchone()[0])
            if course_id is not None:
                if course_id not in settings["course_ids"]:
                    raise ApiError(403, "SCOPE_DENIED", "该课程不在会话范围内")
                settings["current_course_id"] = course_id
            elif clear_course_context:
                settings["current_course_id"] = None
            db.execute("UPDATE conversations SET settings=?,updated_at=? WHERE id=?", (encoded(settings), stamp, cid))
            db.execute("INSERT INTO turns (id,conversation_id,request_key,request_hash,text,status,token_hash,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,'queued',?,?,?)", (tid, cid, key, fingerprint, text, digest(token), stamp, stamp))
            if action_context is not None:
                db.execute("INSERT INTO events (turn_id,kind,data,created_at) VALUES (?,?,?,?)",
                           (tid, "accepted_action", encoded(action_context), stamp))
        return self.turn(tid), token

    def runtime(self, token: str, cid: str | None = None) -> tuple[dict, dict]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM turns WHERE token_hash=? AND status IN ('queued','running')", (digest(token),)).fetchone()
        if row is None or (cid is not None and row["conversation_id"] != cid):
            raise ApiError(403, "RUNTIME_DENIED", "运行时授权已失效或不属于当前会话")
        return self._turn(row), self.conversation(row["conversation_id"])

    def running(self, tid: str) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE turns SET status='running',updated_at=? WHERE id=? AND status='queued'", (now(), tid)).rowcount)

    def finish(self, tid: str, status: str, *, response: str = "", error: dict | None = None):
        with self.connection() as db:
            db.execute("UPDATE turns SET status=?,response=?,error=?,token_hash=NULL,updated_at=? "
                       "WHERE id=? AND status IN ('queued','running')", (status, response, encoded(error) if error else None, now(), tid))

    def event(self, tid: str, kind: str, data: dict):
        with self.connection() as db:
            db.execute("INSERT INTO events (turn_id,kind,data,created_at) VALUES (?,?,?,?)", (tid, kind, encoded(data), now()))

    def events(self, tid: str, after: int = 0) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT 100", (tid, after)).fetchall()
        items = [{"seq": r["seq"], "kind": r["kind"], "data": json.loads(r["data"]), "created_at": r["created_at"]} for r in rows]
        return {"items": items, "next_cursor": items[-1]["seq"] if items else after}

    def reserve_model(self, tid: str):
        with self.connection() as db:
            changed = db.execute("UPDATE turns SET model_calls=model_calls+1 WHERE id=? AND status='running' AND model_calls<?", (tid, self.limits.model_calls)).rowcount
        if not changed:
            raise ApiError(409, "BUDGET_EXCEEDED", "本轮模型调用达到上限或已停止")

    def cached_call(self, tid: str, call_id: str, request: dict) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM tool_calls WHERE turn_id=? AND call_id=?", (tid, call_id)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM tool_calls WHERE turn_id=?", (tid,)).fetchone()[0]
        if row:
            if row["request_hash"] != digest(encoded(request)):
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "工具调用 ID 已用于不同参数")
            return json.loads(row["result"])
        if count >= self.limits.tool_calls:
            raise ApiError(409, "BUDGET_EXCEEDED", "本轮工具调用达到上限")
        return None

    def cache_call(self, tid: str, call_id: str, request: dict, result: dict):
        with self.connection() as db:
            db.execute("INSERT INTO tool_calls VALUES (?,?,?,?)", (tid, call_id, digest(encoded(request)), encoded(result)))
