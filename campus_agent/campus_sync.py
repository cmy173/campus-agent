"""Background synchronization for campus data.

Periodically keeps the WebVPN session alive and pulls fresh college notices
through the public connector. Course/grade refresh over WebVPN needs
CAS-over-WebVPN support and is intentionally out of scope here; the passport
login flow already refreshes those when the user signs in on a campus network.
"""
import json
import threading
import time
from datetime import datetime
from typing import Optional

from .campus_crypto import CredentialCipher
from .connector_contracts import UserContext
from .webvpn_adapter import WebVPNAdapter


class SyncScheduler:
    """Background scheduler for campus data synchronization."""

    def __init__(self, store, campus_service):
        self.store = store
        self.campus_service = campus_service
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def _run_loop(self):
        while self.running:
            # 1-second ticks keep shutdown responsive; 1800 ticks = 30 minutes.
            for _ in range(1800):
                if not self.running:
                    return
                time.sleep(1)
            try:
                self._sync_all_users()
            except Exception as e:
                print(f'[SyncScheduler] Error in main loop: {e}')

    def _sync_all_users(self):
        with self.store.connect() as db:
            users = db.execute('''
                SELECT owner, webvpn_username, webvpn_password_encrypted, webvpn_cookies,
                       sync_interval_minutes, last_sync_at
                FROM campus_credentials
                WHERE auto_sync_enabled = 1
            ''').fetchall()

        for row in users:
            user = dict(row)
            if user.get('last_sync_at') and not self._should_sync(
                    user['last_sync_at'], user.get('sync_interval_minutes', 30)):
                continue
            try:
                self._sync_user(user)
            except Exception as e:
                print(f"[SyncScheduler] Failed to sync user {user['owner']}: {e}")
                self._update_sync_status(user['owner'], 'failed', str(e))

    def _sync_user(self, user_row: dict):
        """Refresh the WebVPN session and pull fresh college notices for one user."""
        owner = user_row['owner']

        cipher = CredentialCipher(owner)
        webvpn_password = cipher.decrypt(json.loads(user_row['webvpn_password_encrypted']))

        adapter = self._restore_webvpn_session(user_row.get('webvpn_cookies'))
        if not adapter or not adapter.probe():
            adapter = WebVPNAdapter()
            adapter.login(user_row['webvpn_username'], webvpn_password)
            self._save_webvpn_session(owner, adapter)

        with self.store.connect() as db:
            db.execute('UPDATE campus_credentials SET last_used_at=? WHERE owner=?',
                       (datetime.now().isoformat(), owner))

        # Public connector: no intranet or CAS needed; the service keeps the
        # previous cache when the remote read fails.
        result = self.campus_service.sync('college_notices', {}, UserContext(owner))
        if result.error is not None:
            raise ValueError(f'学院通知同步失败: {result.error.code.value}')
        self._update_sync_status(owner, 'success', None)

    def _restore_webvpn_session(self, cookies_json: Optional[str]) -> Optional[WebVPNAdapter]:
        if not cookies_json:
            return None
        try:
            adapter = WebVPNAdapter()
            if adapter.restore_cookies(json.loads(cookies_json)):
                return adapter
        except Exception:
            pass
        return None

    def _save_webvpn_session(self, owner: str, adapter: WebVPNAdapter):
        cookies = adapter.get_cookies_dict()
        expires_at = (datetime.fromtimestamp(adapter.session_expiry).isoformat()
                      if adapter.session_expiry else None)
        with self.store.connect() as db:
            db.execute('UPDATE campus_credentials SET webvpn_cookies=?, webvpn_expires_at=? WHERE owner=?',
                       (json.dumps(cookies), expires_at, owner))

    def _should_sync(self, last_sync_iso: str, interval_minutes: int) -> bool:
        try:
            last_sync = datetime.fromisoformat(last_sync_iso)
            return (datetime.now() - last_sync).total_seconds() / 60 >= interval_minutes
        except Exception:
            return True

    def _update_sync_status(self, owner: str, status: str, error: Optional[str]):
        now = datetime.now().isoformat()
        with self.store.connect() as db:
            db.execute('UPDATE campus_credentials SET last_sync_at=?, last_sync_status=? WHERE owner=?',
                       (now, status, owner))
            detail = f'status={status}' + (f', error={error}' if error else '')
            db.execute('INSERT INTO audit(owner, action, result, created) VALUES(?,?,?,?)',
                       (owner, 'campus.sync', detail, now))

    def trigger_manual_sync(self, owner: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM campus_credentials WHERE owner=?', (owner,)).fetchone()
        if not row:
            return {'status': 'error', 'message': '未保存 WebVPN 凭据'}
        thread = threading.Thread(target=self._safe_sync, args=(dict(row),), daemon=True)
        thread.start()
        return {'status': 'queued', 'message': '同步已开始'}

    def _safe_sync(self, user_row: dict):
        try:
            self._sync_user(user_row)
        except Exception as e:
            print(f"[SyncScheduler] Manual sync failed for {user_row['owner']}: {e}")
            self._update_sync_status(user_row['owner'], 'failed', str(e))
