"""One local scheduler, persistent occurrence outbox, and the existing Harness Runtime.

Occurrence identity survives edits, dispatch crashes and application restart. An admitted
turn is never replayed: an interrupted/uncertain model execution needs a new user request.
This guarantees admission deduplication, not exactly-once arbitrary external effects.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ApiError
from .store import encoded

UTC = timezone.utc


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Timing(Strict):
    kind: Literal['at', 'after', 'every', 'daily', 'weekly']
    time_zone: str = Field(min_length=1, max_length=100)
    at: str | None = Field(default=None, max_length=128)
    seconds: int | None = Field(default=None, ge=1, le=315360000)
    time: str | None = Field(default=None, max_length=8)
    weekdays: list[int] | None = None

    @model_validator(mode='after')
    def checked(self):
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('需要有效 IANA 时区，例如 Asia/Shanghai')
        expected = {'at': {'at'}, 'after': {'seconds'}, 'every': {'seconds'},
                    'daily': {'time'}, 'weekly': {'time', 'weekdays'}}[self.kind]
        supplied = {k for k in ('at', 'seconds', 'time', 'weekdays') if getattr(self, k) is not None}
        if supplied != expected:
            raise ValueError('时间规则的字段不匹配')
        if self.kind == 'at':
            try:
                value = datetime.fromisoformat(self.at.replace('Z', '+00:00'))
                if value.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ValueError('at 需要带 UTC 偏移的 ISO 日期时间')
        if self.kind == 'every' and self.seconds < 60:
            raise ValueError('重复任务最小间隔为 60 秒')
        if self.time is not None:
            try:
                if not re.fullmatch(r'\d{2}:\d{2}:\d{2}', self.time):
                    raise ValueError()
                datetime.strptime(self.time, '%H:%M:%S')
            except ValueError:
                raise ValueError('本地时间格式应为 HH:MM:SS')
        if self.weekdays is not None and (not self.weekdays or len(set(self.weekdays)) != len(self.weekdays)
                or any(type(day) is not int or day < 1 or day > 7 for day in self.weekdays)):
            raise ValueError('weekdays 需要不重复的 ISO 星期数 1–7')
        return self


class ScheduleCreate(Strict):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=6000)
    timing: Timing
    missed_policy: Literal['latest', 'skip'] = 'latest'

    @model_validator(mode='after')
    def nonblank(self):
        self.title, self.prompt = self.title.strip(), self.prompt.strip()
        if not self.title or not self.prompt:
            raise ValueError('标题和任务指令不能为空')
        return self


class ScheduleUpdate(Strict):
    revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=6000)
    timing: Timing | None = None
    missed_policy: Literal['latest', 'skip'] | None = None
    enabled: bool | None = None


class ScheduleDelete(Strict):
    revision: int = Field(ge=1)


class ScheduleView(Strict):
    id: str
    conversation_id: str
    revision: int
    title: str
    prompt: str
    timing: Timing
    missed_policy: Literal['latest', 'skip']
    enabled: bool
    deleted: bool
    next_at: str | None
    next_local: str | None
    execution_scope: Literal['application_running']
    created_at: str
    updated_at: str


class ScheduleListView(Strict):
    items: list[ScheduleView]
    execution_scope: Literal['application_running']


class OccurrenceView(Strict):
    id: str
    schedule_id: str
    schedule_revision: int
    conversation_id: str
    scheduled_at: str
    title: str
    prompt: str
    state: Literal['pending', 'dispatching', 'admitted', 'skipped', 'cancelled', 'failed']
    turn_id: str | None
    error_code: str | None
    created_at: str
    turn_status: Literal['queued', 'running', 'stopping', 'succeeded', 'failed', 'cancelled', 'interrupted'] | None = None


class ScheduleHistoryView(Strict):
    items: list[OccurrenceView]


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def wall_candidate(day, rule):
    """Skip DST gaps; use the earlier instant only in a repeated wall-clock hour."""
    h, m, s = (int(part) for part in rule['time'].split(':'))
    naive = datetime(day.year, day.month, day.day, h, m, s)
    zone = ZoneInfo(rule['time_zone'])
    value = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    return value.timestamp() if value.astimezone(zone).replace(tzinfo=None) == naive else None


def next_time(rule, after: float) -> float:
    kind = rule['kind']
    if kind == 'at':
        target = datetime.fromisoformat(rule['at'].replace('Z', '+00:00')).timestamp()
        if target <= after:
            raise ApiError(400, 'SCHEDULE_INVALID_TIME', '一次任务必须在未来执行')
        return target
    if kind in ('after', 'every'):
        return after + rule['seconds']
    day = datetime.fromtimestamp(after, ZoneInfo(rule['time_zone'])).date()
    for offset in range(370):
        candidate_day = day + timedelta(days=offset)
        if kind == 'weekly' and candidate_day.isoweekday() not in rule['weekdays']:
            continue
        target = wall_candidate(candidate_day, rule)
        if target is not None and target > after:
            return target
    raise ApiError(400, 'SCHEDULE_INVALID_TIME', '无法在一年内找到下次执行时间')


def due_and_next(rule, first: float, now: float):
    """Latest missed occurrence only. No unbounded backlog after sleeping for years."""
    if rule['kind'] in ('at', 'after'):
        return first, None
    if rule['kind'] == 'every':
        latest = first + int((now - first) // rule['seconds']) * rule['seconds']
        return latest, latest + rule['seconds']
    zone = ZoneInfo(rule['time_zone'])
    day = datetime.fromtimestamp(now, zone).date()
    for offset in range(370):
        candidate_day = day - timedelta(days=offset)
        if rule['kind'] == 'weekly' and candidate_day.isoweekday() not in rule['weekdays']:
            continue
        target = wall_candidate(candidate_day, rule)
        if target is not None and first <= target <= now:
            return target, next_time(rule, now)
    return first, next_time(rule, now)


class Scheduler:
    def __init__(self, store, runtime, *, clock=time.time, poll_seconds=1.0, after_enqueue=None):
        self.store, self.runtime, self.clock = store, runtime, clock
        self.poll_seconds, self.after_enqueue = poll_seconds, after_enqueue
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        with self.store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS schedules (
              id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
              revision INTEGER NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL,
              timing TEXT NOT NULL, missed_policy TEXT NOT NULL, enabled INTEGER NOT NULL,
              deleted INTEGER NOT NULL DEFAULT 0, next_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_occurrences (
              id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES schedules(id),
              schedule_revision INTEGER NOT NULL, conversation_id TEXT NOT NULL,
              scheduled_at REAL NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL,
              state TEXT NOT NULL, turn_id TEXT, error_code TEXT, created_at REAL NOT NULL,
              UNIQUE(schedule_id, scheduled_at));
            CREATE TABLE IF NOT EXISTS schedule_requests (
              request_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response TEXT NOT NULL);
            ''')

    def _view(self, row):
        rule = json.loads(row['timing'])
        target = row['next_at']
        local = datetime.fromtimestamp(target, ZoneInfo(rule['time_zone'])).isoformat() if target is not None else None
        return {'id': row['id'], 'conversation_id': row['conversation_id'], 'revision': row['revision'],
                'title': row['title'], 'prompt': row['prompt'], 'timing': rule,
                'missed_policy': row['missed_policy'], 'enabled': bool(row['enabled']),
                'deleted': bool(row['deleted']), 'next_at': stamp(target) if target is not None else None,
                'next_local': local, 'execution_scope': 'application_running',
                'created_at': stamp(row['created_at']), 'updated_at': stamp(row['updated_at'])}

    def list(self, cid):
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM schedules WHERE conversation_id=? AND deleted=0 ORDER BY created_at', (cid,)).fetchall()
        return [self._view(row) for row in rows]

    def history(self, cid, sid, limit=50):
        with self.store.connection() as db:
            if not db.execute('SELECT 1 FROM schedules WHERE id=? AND conversation_id=?', (sid, cid)).fetchone():
                raise ApiError(404, 'NOT_FOUND', '定时任务不存在')
            rows = db.execute('SELECT * FROM schedule_occurrences WHERE schedule_id=? ORDER BY scheduled_at DESC LIMIT ?', (sid, limit)).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry['scheduled_at'] = stamp(row['scheduled_at'])
            entry['created_at'] = stamp(row['created_at'])
            if row['turn_id']:
                entry['turn_status'] = self.store.turn(row['turn_id'])['status']
            result.append(entry)
        return result

    def _cached(self, db, key, request):
        if key is None:
            return None
        row = db.execute('SELECT * FROM schedule_requests WHERE request_key=?', (key,)).fetchone()
        if row:
            if row['request_hash'] != hashlib.sha256(encoded(request).encode()).hexdigest():
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一请求键不能用于不同定时任务操作')
            return json.loads(row['response'])

    def _receipt(self, db, key, request, result):
        if key is not None:
            db.execute('INSERT INTO schedule_requests VALUES (?,?,?)',
                       (key, hashlib.sha256(encoded(request).encode()).hexdigest(), encoded(result)))

    def create(self, cid, spec: ScheduleCreate, *, request_key=None):
        self.store.conversation(cid)
        request = {'operation': 'create', 'conversation_id': cid, 'data': spec.model_dump()}
        with self.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            cached = self._cached(db, request_key, request)
            if cached is not None:
                return cached
            now = self.clock()
            rule = spec.timing.model_dump(exclude_none=True)
            sid = 'sched_' + secrets.token_hex(16)
            db.execute('INSERT INTO schedules VALUES (?,?,1,?,?,?,?,1,0,?,?,?)',
                       (sid, cid, spec.title, spec.prompt, encoded(rule), spec.missed_policy, next_time(rule, now), now, now))
            result = self._view(db.execute('SELECT * FROM schedules WHERE id=?', (sid,)).fetchone())
            self._receipt(db, request_key, request, result)
        self.wake.set()
        return result

    def update(self, cid, sid, spec: ScheduleUpdate, *, request_key=None):
        return self._change(cid, sid, spec, False, request_key)

    def delete(self, cid, sid, revision, *, request_key=None):
        return self._change(cid, sid, ScheduleUpdate(revision=revision), True, request_key)

    def _change(self, cid, sid, spec, delete, key):
        request = {'operation': 'delete' if delete else 'update', 'conversation_id': cid, 'id': sid, 'data': spec.model_dump()}
        with self.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            cached = self._cached(db, key, request)
            if cached is not None:
                return cached
            row = db.execute('SELECT * FROM schedules WHERE id=? AND conversation_id=? AND deleted=0', (sid, cid)).fetchone()
            if row is None:
                raise ApiError(404, 'NOT_FOUND', '定时任务不存在')
            if row['revision'] != spec.revision:
                raise ApiError(409, 'SCHEDULE_CONFLICT', '任务已变化，请先刷新版本')
            title = row['title'] if spec.title is None else spec.title.strip()
            prompt = row['prompt'] if spec.prompt is None else spec.prompt.strip()
            if not title or not prompt:
                raise ApiError(400, 'INVALID_INPUT', '标题和任务指令不能为空')
            now = self.clock()
            rule = json.loads(row['timing']) if spec.timing is None else spec.timing.model_dump(exclude_none=True)
            enabled = bool(row['enabled']) if spec.enabled is None else spec.enabled
            target = row['next_at']
            if spec.timing is not None or (enabled and not row['enabled']):
                target = next_time(rule, now)
            db.execute('UPDATE schedules SET revision=revision+1,title=?,prompt=?,timing=?,missed_policy=?,enabled=?,deleted=?,next_at=?,updated_at=? WHERE id=?',
                       (title, prompt, encoded(rule), spec.missed_policy or row['missed_policy'], enabled and not delete, delete, target, now, sid))
            # Accepted turns remain inspectable; stopping them is a separate explicit action.
            db.execute("UPDATE schedule_occurrences SET state='cancelled',error_code='SCHEDULE_CHANGED' WHERE schedule_id=? AND state='pending'", (sid,))
            result = self._view(db.execute('SELECT * FROM schedules WHERE id=?', (sid,)).fetchone())
            self._receipt(db, key, request, result)
        self.wake.set()
        return result

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stopping.clear()
            self._recover()
            self._materialize(startup=True)
            self.thread = threading.Thread(target=self._loop, name='campus-scheduler', daemon=True)
            self.thread.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError('Scheduler dispatch is still draining')

    def _loop(self):
        while not self.stopping.is_set():
            try:
                with self.lock:
                    self._materialize()
                    self._dispatch()
            except Exception:
                # No raw provider inputs or exceptions in logs. Durable rows remain recoverable.
                self.last_error = 'SCHEDULER_FAILED'
            self.wake.wait(self.poll_seconds)
            self.wake.clear()

    def _recover(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM schedule_occurrences WHERE state='dispatching'").fetchall()
            for row in rows:
                turn = db.execute('SELECT id,status FROM turns WHERE conversation_id=? AND request_key=?', (row['conversation_id'], 'schedule:' + row['id'])).fetchone()
                if turn:
                    db.execute("UPDATE schedule_occurrences SET state='admitted',turn_id=?,error_code='RECOVERED_NO_REPLAY' WHERE id=?", (turn['id'], row['id']))
                else:
                    db.execute("UPDATE schedule_occurrences SET state='pending',error_code=NULL WHERE id=?", (row['id'],))
            db.execute("UPDATE schedule_occurrences SET state='skipped',error_code='MISSED_SKIP' "
                       "WHERE state='pending' AND schedule_id IN (SELECT id FROM schedules WHERE missed_policy='skip')")

    def _materialize(self, startup=False):
        now = self.clock()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM schedules WHERE enabled=1 AND deleted=0 AND next_at<=?', (now,)).fetchall()
            for row in rows:
                rule = json.loads(row['timing'])
                target, following = due_and_next(rule, row['next_at'], now)
                oid = 'occ_' + hashlib.sha256((row['id'] + ':' + str(int(round(target * 1000000)))).encode()).hexdigest()
                skip = startup and row['missed_policy'] == 'skip'
                # Only the latest unadmitted repeat is useful; don't drain stale queued reminders.
                db.execute("UPDATE schedule_occurrences SET state='skipped',error_code='SUPERSEDED' WHERE schedule_id=? AND state='pending'", (row['id'],))
                db.execute('INSERT OR IGNORE INTO schedule_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (oid, row['id'], row['revision'], row['conversation_id'], target, row['title'], row['prompt'], 'skipped' if skip else 'pending', None, 'MISSED_SKIP' if skip else None, now))
                db.execute('UPDATE schedules SET next_at=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?',
                           (following, following is not None, now, row['id']))

    def _dispatch(self):
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM schedule_occurrences WHERE state='pending' ORDER BY scheduled_at LIMIT 20").fetchall()
        for row in rows:
            if self.stopping.is_set():
                return
            with self.store.connection() as db:
                if not db.execute("UPDATE schedule_occurrences SET state='dispatching' WHERE id=? AND state='pending'", (row['id'],)).rowcount:
                    continue
            text = ('[本机定时任务；不是新的审批授权。涉及对外提交仍需具体确认。]\n'
                    + encoded({'schedule_id': row['schedule_id'], 'occurrence_id': row['id'], 'scheduled_at': stamp(row['scheduled_at']), 'title': row['title']})
                    + '\n任务：' + row['prompt'])
            try:
                turn, token = self.store.enqueue(row['conversation_id'], 'schedule:' + row['id'], text, None)
            except ApiError as exc:
                state = 'pending' if exc.code == 'TURN_IN_PROGRESS' else 'failed'
                with self.store.connection() as db:
                    db.execute('UPDATE schedule_occurrences SET state=?,error_code=? WHERE id=?', (state, exc.code, row['id']))
                continue
            # Deterministic request key closes the two-database-operation crash gap.
            # Hook is exclusively for simulating process loss in verification.
            if self.after_enqueue:
                self.after_enqueue(row, turn)
            with self.store.connection() as db:
                db.execute("UPDATE schedule_occurrences SET state='admitted',turn_id=?,error_code=? WHERE id=?",
                           (turn['id'], None if token else 'EXISTING_NO_REPLAY', row['id']))
            if token:
                try:
                    self.runtime.start(turn, token)
                except Exception:
                    self.store.finish(turn['id'], 'failed', error={'code': 'SCHEDULE_START_FAILED', 'message': '定时任务未启动；不会自动重放', 'retryable': False})
                    with self.store.connection() as db:
                        db.execute("UPDATE schedule_occurrences SET error_code='SCHEDULE_START_FAILED' WHERE id=?", (row['id'],))


def get_scheduler(runtime):
    # Called by API lifecycle and routers; the Runtime owns shutdown ordering.
    with runtime.lock:
        if not hasattr(runtime, 'schedules'):
            runtime.schedules = Scheduler(runtime.store, runtime)
        return runtime.schedules
