"""Owner-scoped chat retention and deletion, never the learning database.

Only journal rows, owned Harness homes and registered attachment/output copies are
managed. User-selected workspaces, source files, browser profiles, campus sessions,
provider/search/MCP credentials, backups and external provider logs are not erased.
TTL is request-driven, not a promise of a timer running while the app is closed.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..errors import ApiError
from .store import digest, encoded

DEFAULT_RETENTION_DAYS = 30
ACTIVE_STATUSES = ('queued', 'running', 'stopping')


def _no_links(path: Path) -> None:
    # Check the unresolved chain too: realpath alone hides symlinks/junctions.
    for entry in (*reversed(path.parents), path):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(metadata.st_mode) or
                getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ApiError(409, 'DATA_PATH_UNSAFE', '历史目录包含链接或重解析点，未完成删除；请检查应用目录')


def _contained(root: Path, target: Path, *, direct: bool = True) -> Path:
    """Reject traversal/links before resolving, including missing target paths."""
    root = Path(os.path.abspath(root))
    _no_links(root)
    _no_links(target)
    lexical = Path(os.path.abspath(target))
    real_root, resolved = root.resolve(), target.resolve()
    if (lexical == root or not lexical.is_relative_to(root) or resolved == real_root or
            not resolved.is_relative_to(real_root) or (direct and resolved.parent != real_root)):
        raise ApiError(409, 'DATA_PATH_UNSAFE', '无法验证历史文件的应用目录边界，未完成删除')
    return lexical


def _check_tree(root: Path, target: Path) -> None:
    _contained(root, target)
    if not target.exists():
        return
    if not target.is_dir():
        raise ApiError(409, 'DATA_PATH_UNSAFE', '历史运行目录不是普通目录，未完成删除')
    for directory, directories, files in os.walk(target, followlinks=False):
        for name in directories + files:
            _contained(root, Path(directory) / name, direct=False)


class DataManagement:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        for suffix in ('', '-wal', '-shm', '-journal'):
            _no_links(Path(str(self.store.path) + suffix))
        # Separate metadata table: do not rewrite Runtime SettingsStore JSON.
        with self.store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS agent_data_settings (
              owner TEXT PRIMARY KEY, history_retention_days INTEGER NOT NULL DEFAULT 30);
            CREATE TABLE IF NOT EXISTS agent_data_cleanup (
              conversation_id TEXT PRIMARY KEY, owner TEXT NOT NULL, storage_names TEXT NOT NULL);
            ''')

    def retention_days(self, owner: str) -> int:
        with self.store.connection() as db:
            row = db.execute('SELECT history_retention_days FROM agent_data_settings WHERE owner=?',
                             (digest(owner),)).fetchone()
        return row[0] if row else DEFAULT_RETENTION_DAYS

    def settings(self, owner: str) -> dict:
        with self.store.connection() as db:
            count = db.execute('SELECT COUNT(*) FROM conversations WHERE owner=?', (digest(owner),)).fetchone()[0]
            pending = db.execute('SELECT COUNT(*) FROM agent_data_cleanup WHERE owner=?', (digest(owner),)).fetchone()[0]
        return {
            'history_retention_days': self.retention_days(owner),
            'default_history_retention_days': DEFAULT_RETENTION_DAYS,
            'history_count': count, 'pending_cleanup_count': pending,
            'scope': 'owner_chat_history', 'expiry_basis': 'conversation_updated_at',
            'expiry_execution': 'history_read_or_explicit_purge',
            'managed_copies': ['agent_journal', 'conversation_harness_home', 'registered_conversation_files'],
            'preserved': ['campus_session', 'browser_profile_and_ledger', 'api_keys', 'runtime_settings',
                          'learning_database', 'user_workspace_and_source_files'],
            'description': '仅管理当前浏览器所有者的聊天历史、工具记录、恢复与定时任务记录、运行目录和附件/交付副本。默认保留30天，0表示长期保留；按会话最后更新时间计算，在读取数据管理历史或手动清理时执行，不保证关闭应用期间定时清理。',
            'erasure_limits': '使用 SQLite secure_delete 和 VACUUM 清理应用可控数据库残留；不保证 SSD/文件系统快照、备份、已下载文件、外部服务日志或用户工作目录的物理擦除。共享浏览器档案及其操作/下载账本不属于聊天历史删除范围。',
        }

    def save_settings(self, owner: str, days: int) -> dict:
        if type(days) is not int or not 0 <= days <= 36500:
            raise ApiError(400, 'INVALID_INPUT', '历史保留天数应为0到36500的整数，0表示长期保留')
        with self.store.connection() as db:
            db.execute('INSERT INTO agent_data_settings VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET '
                       'history_retention_days=excluded.history_retention_days', (digest(owner), days))
        return self.settings(owner)

    def history(self, owner: str, *, cursor=None, search='', limit=50) -> dict:
        # An active expired conversation refuses purge, rather than silently
        # pretending it is erased or cancelling somebody's in-flight work.
        result = self.purge_expired(owner)
        return self.store.conversation_page(owner, cursor=cursor, search=search, limit=limit) | {'purge': result}

    def purge_expired(self, owner: str, *, moment: datetime | None = None) -> dict:
        days = self.retention_days(owner)
        if days == 0:
            return self._receipt([], expiry_disabled=True)
        moment = moment or datetime.now(timezone.utc)
        cutoff = moment - timedelta(days=days)
        with self.store.connection() as db:
            rows = db.execute('SELECT id,updated_at FROM conversations WHERE owner=?', (digest(owner),)).fetchall()
        ids = []
        for row in rows:
            try:
                updated = datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00'))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    ids.append(row['id'])
            except (ValueError, TypeError):
                # Invalid metadata isn't authority to erase a conversation.
                continue
        return self._delete(owner, ids, expired_before=cutoff)

    def delete_conversation(self, owner: str, cid: str) -> dict:
        return self._delete(owner, [cid], require_one=True)

    def delete_history(self, owner: str) -> dict:
        with self.store.connection() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM conversations WHERE owner=?', (digest(owner),))]
            ids += [r[0] for r in db.execute('SELECT conversation_id FROM agent_data_cleanup WHERE owner=?', (digest(owner),))]
        return self._delete(owner, list(dict.fromkeys(ids)))

    @staticmethod
    def _receipt(ids, **extra):
        return {'deleted_conversation_ids': ids, 'deleted_count': len(ids), 'scope': 'owner_chat_history',
                'cleanup_complete': True, **extra}

    def _paths(self, cid: str, names: list[str]) -> tuple[Path, list[Path]]:
        if not cid or Path(cid).name != cid or '/' in cid or '\\' in cid or cid in ('.', '..'):
            raise ApiError(409, 'DATA_PATH_UNSAFE', '历史运行目录标识无效，未完成删除')
        home = _contained(self.runtime.home, self.runtime.home / cid)
        _check_tree(self.runtime.home, home)
        paths = []
        for name in names:
            if not name or Path(name).name != name or '/' in name or '\\' in name or name in ('.', '..'):
                raise ApiError(409, 'DATA_PATH_UNSAFE', '历史附件路径无效，未完成删除')
            path = _contained(self.runtime.files.directory, self.runtime.files.directory / name)
            if path.exists() and not path.is_file():
                raise ApiError(409, 'DATA_PATH_UNSAFE', '历史附件不是普通文件，未完成删除')
            paths.append(path)
        return home, paths

    def _busy(self, ids: list[str], active_turn_ids=None):
        raise ApiError(409, 'HISTORY_ACTIVE', '聊天仍在运行或停止收尾中；请先停止相关对话，等待完成后再删除',
                       details={'conversation_ids': ids, 'active_turn_ids': active_turn_ids or []})

    def _delete(self, owner: str, ids: list[str], *, require_one=False, expired_before=None) -> dict:
        if not ids:
            return self._receipt([])
        owner_hash = digest(owner)
        runtime = self.runtime
        # Authorize before touching runtime locks or revealing busy state. A
        # foreign owner's active session must still look like a missing session.
        with self.store.connection() as db:
            for cid in ids:
                row = db.execute('SELECT owner FROM conversations WHERE id=?', (cid,)).fetchone()
                pending = db.execute('SELECT owner FROM agent_data_cleanup WHERE conversation_id=?', (cid,)).fetchone()
                if (row is not None and row[0] != owner_hash) or (pending is not None and pending[0] != owner_hash):
                    raise ApiError(404, 'NOT_FOUND', '会话不存在')
                if row is None and pending is None and require_one:
                    raise ApiError(404, 'NOT_FOUND', '会话不存在')
        for suffix in ('', '-wal', '-shm', '-journal'):
            _no_links(Path(str(self.store.path) + suffix))
        cleanup = {}
        # Same lock used by prewarm, turns and authentication recovery. Scheduler's
        # dispatch lock prevents a retained schedule snapshot from enqueueing later.
        with ExitStack() as stack:
            scheduler = getattr(runtime, 'schedules', None)
            if scheduler is not None:
                stack.enter_context(scheduler.lock)
            with runtime.lock:
                locks = [(cid, runtime.conversation_locks.setdefault(cid, threading.Lock())) for cid in sorted(ids)]
            for cid, lock in locks:
                if not lock.acquire(blocking=False):
                    self._busy([cid])
                stack.callback(lock.release)
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                for cid in ids:
                    row = db.execute('SELECT owner,updated_at FROM conversations WHERE id=?', (cid,)).fetchone()
                    pending = db.execute('SELECT owner,storage_names FROM agent_data_cleanup WHERE conversation_id=?', (cid,)).fetchone()
                    if (row is not None and row['owner'] != owner_hash) or (pending is not None and pending['owner'] != owner_hash):
                        raise ApiError(404, 'NOT_FOUND', '会话不存在')
                    if row is None and pending is None:
                        if require_one:
                            raise ApiError(404, 'NOT_FOUND', '会话不存在')
                        continue
                    if row is not None and expired_before is not None:
                        updated = datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00'))
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        if updated >= expired_before:
                            continue  # Revalidate TTL after obtaining the write lease.
                    active = db.execute("SELECT id FROM turns WHERE conversation_id=? AND status IN ('queued','running','stopping')", (cid,)).fetchall()
                    if active:
                        self._busy([cid], [r[0] for r in active])
                    tids = [r[0] for r in db.execute('SELECT id FROM turns WHERE conversation_id=?', (cid,))]
                    with runtime.lock:
                        if any(tid in runtime.threads or tid in runtime.processes or tid in runtime.stop_threads for tid in tids):
                            self._busy([cid], tids)
                    names = [r[0] for r in db.execute('SELECT storage_name FROM conversation_files WHERE conversation_id=?', (cid,))]
                    if pending:
                        names += json.loads(pending['storage_names'])
                    names = list(dict.fromkeys(names))
                    self._paths(cid, names)  # Preflight every target before deleting any row.
                    cleanup[cid] = names
            # Stop idle owned runners first; do not leave a live writer for an erased home.
            for cid in cleanup:
                with runtime.lock:
                    handle = runtime.runners.get(cid)
                if handle is not None:
                    try:
                        runtime._discard_runner(cid, handle['proc'])
                    except Exception:
                        raise ApiError(409, 'HISTORY_RUNTIME_BUSY', '运行目录仍未释放，未完成删除；请等待后重试') from None
            with self.store.connection() as db:
                db.execute('PRAGMA secure_delete=ON')
                db.execute('BEGIN IMMEDIATE')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for cid, names in cleanup.items():
                    # An HTTP enqueue can race the initial preflight. Recheck within
                    # this transaction; deletion and all journal copies are atomic.
                    active = db.execute("SELECT id FROM turns WHERE conversation_id=? AND status IN ('queued','running','stopping')", (cid,)).fetchall()
                    if active:
                        self._busy([cid], [r[0] for r in active])
                    # Uploads can register new copies between preflight and this
                    # write transaction. Include them before deleting their rows.
                    names[:] = list(dict.fromkeys(names + [r[0] for r in db.execute(
                        'SELECT storage_name FROM conversation_files WHERE conversation_id=?', (cid,))]))
                    self._paths(cid, names)
                    for name in names:
                        if db.execute('SELECT 1 FROM conversation_files WHERE storage_name=? AND conversation_id<>?', (name, cid)).fetchone():
                            raise ApiError(409, 'DATA_PATH_UNSAFE', '附件副本与另一会话共享路径，未完成删除')
                    db.execute('INSERT INTO agent_data_cleanup VALUES (?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET storage_names=excluded.storage_names',
                               (cid, owner_hash, encoded(names)))
                    if 'schedule_requests' in tables:
                        # Cached response is a full schedule copy, including prompt.
                        # Inspect structured IDs, never a LIKE prefix or global delete.
                        for row in db.execute('SELECT request_key,response FROM schedule_requests').fetchall():
                            try:
                                response = json.loads(row['response'])
                            except (TypeError, ValueError):
                                continue
                            if isinstance(response, dict) and response.get('conversation_id') == cid:
                                db.execute('DELETE FROM schedule_requests WHERE request_key=?', (row['request_key'],))
                    for table in ('auth_recovery', 'schedule_occurrences', 'schedules'):
                        if table in tables:
                            db.execute(f'DELETE FROM {table} WHERE conversation_id=?', (cid,))
                    for table in ('events', 'tool_calls', 'native_tool_leases'):
                        db.execute(f'DELETE FROM {table} WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id=?)', (cid,))
                    db.execute('DELETE FROM conversation_files WHERE conversation_id=?', (cid,))
                    db.execute('DELETE FROM runtime_grants WHERE conversation_id=?', (cid,))
                    db.execute('DELETE FROM turns WHERE conversation_id=?', (cid,))
                    db.execute('DELETE FROM conversations WHERE id=? AND owner=?', (cid, owner_hash))
            failed = None
            try:
                for cid, names in cleanup.items():
                    home, files = self._paths(cid, names)  # Revalidate immediately before filesystem writes.
                    for path in files:
                        _contained(runtime.files.directory, path)
                        path.unlink(missing_ok=True)
                    if home.exists():
                        _check_tree(runtime.home, home)
                        shutil.rmtree(home)
                    with self.store.connection() as db:
                        db.execute('PRAGMA secure_delete=ON')
                        db.execute('DELETE FROM agent_data_cleanup WHERE conversation_id=? AND owner=?', (cid, owner_hash))
            except (OSError, ApiError, RuntimeError) as error:
                failed = error
            # VACUUM cannot run inside a transaction. The journal is the only DB
            # touched. In WAL mode truncate its owned WAL after compaction too.
            try:
                with self.store.connection() as db:
                    db.execute('PRAGMA secure_delete=ON')
                    db.execute('VACUUM')
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except Exception:
                # Preserve retry authority even if file cleanup already succeeded
                # and its earlier tombstone was removed before VACUUM failed.
                with self.store.connection() as db:
                    for cid, names in cleanup.items():
                        db.execute('INSERT OR IGNORE INTO agent_data_cleanup VALUES (?,?,?)', (cid, owner_hash, encoded(names)))
                raise ApiError(503, 'HISTORY_COMPACTION_INCOMPLETE', '聊天记录已删除，但数据库残留压缩未完成；请重试清理',
                               retryable=True, details={'deleted_conversation_ids': list(cleanup), 'cleanup_complete': False}) from None
            if failed is not None:
                raise ApiError(503, 'HISTORY_CLEANUP_INCOMPLETE', '聊天记录已删除，但运行目录或附件副本尚未完全清理；请重试删除',
                               retryable=True, details={'deleted_conversation_ids': list(cleanup), 'cleanup_complete': False}) from None
        return self._receipt(list(cleanup))
