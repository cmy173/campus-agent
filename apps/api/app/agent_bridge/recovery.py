"""Persisted, explicitly authenticated, read-only campus task continuation.

This module never logs in, opens a browser, calls a model or changes authorization.
One Runtime-owned monitor serializes work; SQLite leases and the checkpoint request
key arbitrate a notification racing another monitor. Failed probes are NOT polled:
a subsequent successful human connection notification (or boot) must request one.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from pydantic import ValidationError

from ..errors import ApiError
from . import tools

AUTH_ERRORS = frozenset({'AUTH_REQUIRED', 'AUTH_EXPIRED', 'SESSION_EXPIRED', 'BROWSER_AUTH_REQUIRED'})
PROBE_COOLDOWN_SECONDS = 30
_SECRET_KEY = re.compile(r'(?:password|passwd|secret|credential|cookie|authorization|access_token|refresh_token|api_key|session_token|csrf|otp)|^token$', re.I)
_SECRET_TEXT = re.compile(r'(?i)(\b(?:password|passwd|secret|access_token|refresh_token|api_key|session_token|cookie|authorization)\s*[:=]\s*)([^\s,;]+)|\bBearer\s+[^\s,;]+')


def scrub(value):
    """Defence in depth: connector/transport credential fields are not checkpoint data."""
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        if value.startswith(('http://', 'https://')) and contains_secret(value):
            return '[REDACTED URL]'
        return _SECRET_TEXT.sub('[REDACTED]', value)
    return value


def contains_secret(value) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEY.search(str(k)) or contains_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_secret(v) for v in value)
    if isinstance(value, str):
        if _SECRET_TEXT.search(value):
            return True
        if value.startswith(('http://', 'https://')):
            return any(_SECRET_KEY.search(k) for k, _ in parse_qsl(urlsplit(value).query))
    return False


def read_tool(name: str, *, campus_only: bool = True) -> bool:
    if name not in tools.SPECS or (campus_only and not name.startswith('campus.')):
        return False
    # SPECS currently contains (input model, description); its authoritative public
    # projection adds effects. Never infer read access from a suffix or description.
    return any(item['name'] == name and item.get('effects') == 'read' for item in tools.schemas())


def authentication_error(result) -> str | None:
    if not isinstance(result, dict):
        return None
    # Only structured domain errors, never text matching arbitrary tool output.
    candidates = [result.get('error')]
    if isinstance(result.get('data'), dict):
        candidates.append(result['data'].get('error'))
    for error in candidates:
        if isinstance(error, dict) and error.get('code') in AUTH_ERRORS:
            return error['code']
    return None


def valid_pending(pending: dict) -> bool:
    if not isinstance(pending, dict) or not read_tool(pending.get('name', '')):
        return False
    arguments = pending.get('arguments')
    if not isinstance(arguments, dict) or contains_secret(arguments):
        return False
    try:
        tools.SPECS[pending['name']][0].model_validate(arguments)
    except (ValidationError, TypeError, ValueError):
        return False
    return True


class AuthenticationRecovery:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.scan_lock = threading.Lock()
        self.worker_lock = threading.Lock()
        # Startup's ordinary Store.recover() may run immediately after construction;
        # active turns remain unclaimable until that marks them interrupted.
        if runtime.store.auth_recoveries(pending=True):
            runtime.store.restart_auth_recovery()
            self._ensure_worker()

    def _ensure_worker(self):
        with self.worker_lock:
            if self.stop.is_set():
                return
            if self.thread is None:
                self.thread = threading.Thread(target=self._monitor, name='campus-auth-recovery', daemon=True)
                self.thread.start()
            self.wake.set()

    def defer(self, turn: dict, name: str, arguments: dict, result: dict) -> dict | None:
        pending = {'name': name, 'arguments': copy.deepcopy(arguments)}
        code = authentication_error(result)
        if code is None or not valid_pending(pending) or self.stop.is_set():
            return None
        source = self.runtime.store.turn(turn['id'])
        if source['conversation_id'] != turn['conversation_id'] or source['status'] not in ('queued', 'running'):
            return None
        events = scrub(self.runtime.store.recovery_events(source['id']))
        prior = next((e['data'] for e in events if e['kind'] == 'accepted_auth_resume'), None)
        goal = prior.get('original_goal', source['text']) if prior else source['text']
        original_tid = prior.get('original_turn_id', source['id']) if prior else source['id']
        source_goal = next((e['data'].get('goal') for e in reversed(events)
                            if e['kind'] == 'goal.state' and isinstance(e['data'], dict)), None)
        payload = {'version': 1, 'original_turn_id': original_tid, 'original_goal': scrub(goal),
                   'original_goal_state': prior.get('original_goal_state') if prior else source_goal,
                   'source_turn_id': source['id'], 'source_text': scrub(source['text']),
                   'pending_tool': pending, 'pending_result': scrub(result),
                   'purpose': scrub(goal), 'received_events': events,
                   'received_results': [e['data'] for e in events if e['kind'] == 'tool_result'],
                   # IDs reference existing data only; no bridge/provider/browser secrets.
                   'scope': {k: sorted(set(self.runtime.store.conversation(source['conversation_id'])['settings'].get(k) or []) &
                                       set(prior['scope'].get(k, []))) if prior else
                                list(self.runtime.store.conversation(source['conversation_id'])['settings'].get(k) or [])
                             for k in ('course_ids', 'connection_ids')}}
        checkpoint = self.runtime.store.save_auth_recovery(source['id'], payload)
        self._ensure_worker()
        return self.public(checkpoint)

    @staticmethod
    def public(checkpoint: dict) -> dict:
        return {k: checkpoint[k] for k in ('id', 'conversation_id', 'source_turn_id', 'status',
                                          'continuation_turn_id', 'created_at', 'updated_at')} | {
            'pending_tool': checkpoint['payload']['pending_tool']['name'],
            'error': checkpoint['error'], 'next_probe_at': checkpoint['next_probe_at']}

    def status(self, cid: str) -> dict:
        self.runtime.store.conversation(cid)
        items = [self.public(c) for c in self.runtime.store.auth_recoveries(cid)]
        pending = [i for i in items if i['status'] in ('waiting_auth', 'ready')]
        return {'items': items, 'status': 'waiting_auth' if any(i['status'] == 'waiting_auth' for i in pending)
                else 'ready' if pending else 'idle'}

    def authenticated(self) -> dict:
        self.runtime.store.request_auth_probes()
        if self.runtime.store.auth_recoveries(pending=True):
            self._ensure_worker()
        return {'accepted': True}

    def _monitor(self):
        while not self.stop.is_set():
            self.wake.wait(1)
            self.wake.clear()
            if self.stop.is_set():
                break
            self.run_once()

    def run_once(self):
        """Synchronous adapter for mock tests; production has exactly one monitor."""
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            for item in self.runtime.store.auth_recoveries(pending=True):
                if self.stop.is_set():
                    break
                try:
                    self._recover(item)
                except Exception:
                    # No exception text: it can contain remote bodies/credentials.
                    # The claimed lease remains bounded; this never requests a new
                    # network probe or consumes/deletes the durable checkpoint.
                    continue
        finally:
            self.scan_lock.release()

    def _recover(self, checkpoint: dict):
        runtime, store = self.runtime, self.runtime.store
        cid, tid = checkpoint['conversation_id'], checkpoint['source_turn_id']
        with runtime.lock:
            if tid in runtime.threads or tid in runtime.processes or tid in runtime.stop_threads:
                return
            owner_lock = runtime.conversation_locks.setdefault(cid, threading.Lock())
        # finish() precedes owned-runner teardown. The source must have released its
        # official Harness session lease, not merely have a terminal journal enum.
        if not owner_lock.acquire(blocking=False):
            return
        claim = None
        try:
            checkpoint = store.claim_auth_recovery(checkpoint['id'], time.time())
            if checkpoint is None:
                return
            claim = checkpoint['claim_token']
            # A user's stop must not become fresh automatic work after later login.
            if store.turn(tid)['status'] == 'cancelled':
                store.cancel_auth_recovery(checkpoint['id'], claim)
                claim = None
                return
            pending = checkpoint['payload'].get('pending_tool')
            if not valid_pending(pending):
                store.settle_auth_probe(checkpoint['id'], claim, result=None,
                    error={'code': 'RECOVERY_READ_ONLY', 'message': '恢复检查点不再属于可恢复校园只读工具', 'retryable': False},
                    next_probe_at=time.time() + PROBE_COOLDOWN_SECONDS)
                claim = None
                return
            if checkpoint['status'] == 'waiting_auth':
                try:
                    result = runtime.probe_authenticated_read(checkpoint)
                    nested_error = result.get('data', {}).get('error') if isinstance(result, dict) and isinstance(result.get('data'), dict) else None
                    if not isinstance(result, dict) or result.get('status') not in ('ok', 'partial') or result.get('error') or nested_error:
                        code = authentication_error(result) or 'RECOVERY_PROBE_FAILED'
                        error = {'code': code, 'message': '原待处理只读查询尚未成功，已保留恢复检查点', 'retryable': True}
                    else:
                        error = None
                except ApiError as exc:
                    result = None
                    error = {'code': exc.code, 'message': '原待处理只读查询尚不可恢复，已保留检查点', 'retryable': exc.retryable}
                except Exception:
                    result = None
                    error = {'code': 'RECOVERY_PROBE_FAILED', 'message': '恢复只读检查未完成，已保留检查点', 'retryable': True}
                settled = store.settle_auth_probe(checkpoint['id'], claim, result=scrub(result), error=error,
                                                next_probe_at=time.time() + PROBE_COOLDOWN_SECONDS)
                claim = None
                if not settled or error or self.stop.is_set():
                    return
                checkpoint = store.claim_auth_recovery(checkpoint['id'], time.time())
                if checkpoint is None:
                    return
                claim = checkpoint['claim_token']
            # Revalidate the saved enabled provider and present narrower, database-
            # backed scope before enqueueing. A ready checkpoint never grants consent.
            runtime.validate_auth_recovery(checkpoint)
            if self.stop.is_set():
                return
            payload = copy.deepcopy(checkpoint['payload'])
            events = scrub(store.recovery_events(tid))
            payload.update(checkpoint_id=checkpoint['id'], conversation_id=cid, read_only=True,
                           received_events=events, received_results=[e['data'] for e in events if e['kind'] == 'tool_result'],
                           pending_read_result=checkpoint['probe_result'],
                           instruction='原待处理只读查询已由可信服务端成功执行；复用该结果与原会话官方历史继续原目标。只读，不登录、不重新授权、不重复已完成操作。')
            queued = store.enqueue_auth_recovery(checkpoint['id'], claim, payload)
            if queued:
                claim = None
                turn, token = queued
                if self.stop.is_set():
                    store.finish(turn['id'], 'interrupted', error={'code': 'RUNTIME_INTERRUPTED', 'message': '服务关闭，恢复检查点已保留', 'retryable': True})
                    store.auth_launch_failed(checkpoint['id'])
                else:
                    try:
                        runtime.start(turn, token)
                    except Exception:
                        store.finish(turn['id'], 'failed', error={'code': 'RECOVERY_LAUNCH_FAILED', 'message': '恢复轮次未能启动', 'retryable': True})
                        store.auth_launch_failed(checkpoint['id'])
        except ApiError as exc:
            if claim:
                store.settle_auth_probe(checkpoint['id'], claim, result=None,
                    error={'code': exc.code, 'message': '保存的恢复配置目前不可用，检查点已保留', 'retryable': exc.retryable},
                    next_probe_at=time.time() + PROBE_COOLDOWN_SECONDS)
                claim = None
        finally:
            if claim:
                store.release_auth_claim(checkpoint['id'], claim)
            owner_lock.release()

    def close(self):
        self.stop.set()
        self.wake.set()
        worker = self.thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=12)
