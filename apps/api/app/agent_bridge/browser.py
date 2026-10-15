"""Lazy owned browser daemon; cookie/profile persistence is independent of turns."""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import queue
import secrets
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

import requests

from ..config import REPO_ROOT
from ..errors import ApiError
from ..process_owner import spawn_owned


# Defense in depth before starting the browser subprocess. The owned daemon
# separately checks its live tool catalog and inspects interaction targets.
BLOCKED_MODEL_BROWSER_TOOLS = frozenset({
    'browser_evaluate', 'browser_run_code_unsafe', 'browser_file_upload', 'browser_drop', 'browser_drag',
})


def validate_model_browser_call(name: str | None, arguments: dict | None = None) -> None:
    args = arguments or {}
    if (name in BLOCKED_MODEL_BROWSER_TOOLS
            or (name == 'browser_handle_dialog' and args.get('accept') is not False)
            or (name == 'browser_network_request' and args.get('part') != 'response-body')):
        raise ApiError(403, 'BROWSER_APPROVAL_REQUIRED',
                       '该工具可能绕过提交确认或直接传出文件；请使用页面预览工具，或由用户在浏览器中完成')


class BrowserField(BaseModel):
    name: str
    value: str


class BrowserPreview(BaseModel):
    page_url: str
    kind: Literal['explicit'] | None = None
    action_url: str | None = None
    method: str | None = None
    fields: list[BrowserField] = Field(default_factory=list)
    is_submit: bool | None = None
    button: str | None = None
    description: str | None = None
    recipient: str | None = None
    content: str | None = None
    attachments: list[str] = Field(default_factory=list)
    target_text: str | None = None


class BrowserReview(BaseModel):
    id: str
    conversation_id: str
    status: Literal['pending', 'executing', 'executed', 'rejected', 'stale', 'uncertain']
    preview: BrowserPreview
    created_at: str
    result: dict[str, Any] | None = None


class BrowserPage(BaseModel):
    index: int
    url: str
    title: str


class BrowserStatus(BaseModel):
    running: bool
    headed: bool
    pages: list[BrowserPage]
    reviews: list[BrowserReview]


class BrowserReceipt(BaseModel):
    action: BrowserReview
    message: str | None = None


class BrowserOpenResult(BaseModel):
    running: bool
    headed: bool


class BrowserStopResult(BaseModel):
    running: bool
    interrupted: bool = False


class BrowserHost:
    def __init__(self, home: Path):
        self.home = home
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.url = ''
        self.token = ''
        self.closing = False
        self.active_operation = None
        self.unsafe_turns = set()

    def _read_frames(self, proc, ready):
        seen_ready = False
        try:
            for line in proc.stdout:
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue  # Ordinary code stdout is not a control frame.
                if not isinstance(frame, dict):
                    continue
                if frame.get('ready') and not seen_ready:
                    seen_ready = True
                    ready.put(frame)
                elif frame.get('kind') == 'activity':
                    with self.lock:
                        if self.process is proc:
                            payload = json.dumps(frame.get('data'), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                            expected = hmac.new(self.token.encode(), payload, hashlib.sha256).hexdigest()
                            if hmac.compare_digest(expected, str(frame.get('proof', ''))):
                                self.active_operation = frame.get('data')
        except (OSError, ValueError):
            pass

    def _start(self):
        with self.lock:
            if self.closing:
                raise ApiError(503, 'BROWSER_CLOSED', '应用正在关闭，浏览器操作已停止')
            if self.process is not None and self.process.poll() is None:
                return
            if self.process is not None:
                stale, self.process = self.process, None
                stale.campus_owner.close()
                for stream in (stale.stdin, stale.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
                self.active_operation = None
                self.unsafe_turns.clear()
            node = shutil.which('node')
            root = REPO_ROOT / 'apps' / 'agent-runtime'
            if not node or not (root / 'node_modules/@playwright/mcp/package.json').is_file():
                raise ApiError(503, 'DEPENDENCY_UNAVAILABLE', '请先安装锁定的浏览器运行依赖', retryable=True)
            self.home.mkdir(parents=True, exist_ok=True)
            self.token = secrets.token_urlsafe(32)
            keys = ('PATH', 'PATHEXT', 'SystemRoot', 'ComSpec', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                    'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'SystemDrive',
                    'TEMP', 'TMP', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                    'CAMPUS_BROWSER_CHANNEL', 'CAMPUS_BROWSER_EXECUTABLE', 'CAMPUS_BROWSER_PROXY')
            env = {k: os.environ[k] for k in keys if k in os.environ}
            scratch = self.home / 'tmp'
            scratch.mkdir(exist_ok=True)
            env.update(CAMPUS_BROWSER_HOME=str(self.home.resolve()), CAMPUS_BROWSER_TOKEN=self.token,
                       TEMP=str(scratch.resolve()), TMP=str(scratch.resolve()))
            proc = spawn_owned([node, str(root / 'browser-host.mjs')], cwd=root, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.process = proc
            ready = queue.Queue(maxsize=1)
            threading.Thread(target=self._read_frames, args=(proc, ready), daemon=True).start()
            try:
                frame = ready.get(timeout=30)
                if not frame.get('ready') or not frame.get('url', '').startswith('http://127.0.0.1:'):
                    raise ValueError('Invalid browser readiness')
                self.url = frame['url']
            except (ValueError, queue.Empty):
                self.close(terminal=False)
                raise ApiError(503, 'BROWSER_START_FAILED', '浏览器服务未启动，请检查 Node 和锁定依赖', retryable=True) from None

    def rpc(self, cid: str, method: str, **arguments):
        if method == 'call':
            validate_model_browser_call(arguments.get('name'), arguments.get('arguments'))
        if method != 'campus-session':
            self._start()
        with self.lock:
            if method == 'campus-session' and (self.process is None or self.process.poll() is not None):
                raise ApiError(409, 'BROWSER_AUTH_REQUIRED', '请先在浏览器窗口中完成校园登录')
            url, token = self.url, self.token
            if method == 'call' and arguments.get('name') == 'browser_run_code_unsafe':
                self.unsafe_turns.add(arguments.get('turn_id') or 'manual')
        try:
            response = requests.post(url + '/rpc', headers={'Authorization': 'Bearer ' + token},
                json={'conversation_id': cid, 'method': method, **arguments}, timeout=2 if method == 'stop' else 150, allow_redirects=False)
            data = response.json()
        except (requests.RequestException, ValueError):
            if method == 'stop':
                with self.lock:
                    current = self.active_operation
                    tid = arguments.get('turn_id')
                    owned = not tid or (current and current.get('turn_id') == tid) or (not current and tid in self.unsafe_turns)
                if owned:
                    self.close(terminal=False, force=True)
                    return {'running': False, 'interrupted': True, 'forced': True}
            # A lost response may follow a real submit: never replay automatically.
            raise ApiError(502, 'BROWSER_CONNECTION_LOST', '浏览器连接中断；若刚提交，请先核对页面回执再操作', retryable=True) from None
        if not response.ok or not data.get('ok'):
            code = data.get('code', 'BROWSER_OPERATION_FAILED')
            raise ApiError(409, code, data.get('message', '浏览器操作未完成'), retryable=code not in ('BROWSER_ACTION_CHANGED', 'BROWSER_APPROVAL_REQUIRED', 'BROWSER_AUTH_REQUIRED', 'NOT_FOUND'))
        if method == 'stop' and data['result'].get('reset_required'):
            self.close(terminal=False, force=True)
        return data['result']

    def campus_cookies_private(self) -> list[dict]:
        """Owner-only cookie handoff, not a model/API response. Never boot a browser."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise ApiError(409, 'BROWSER_AUTH_REQUIRED', '请先在浏览器窗口中完成校园登录')
            # Hold the owner lock so teardown cannot race this private RPC into a restart.
            value = self.rpc('campus-session', 'campus-session')
        cookies = value.get('cookies') if isinstance(value, dict) else None
        if not isinstance(cookies, list) or len(cookies) > 2048:
            raise ApiError(502, 'BROWSER_SESSION_INVALID', '浏览器校园会话暂时无法读取')
        allowed = {'zjuam.zju.edu.cn', 'zdbk.zju.edu.cn', 'eta.zju.edu.cn', 'zju.edu.cn'}
        selected = []
        for item in cookies:
            if not isinstance(item, dict):
                continue
            domain = str(item.get('domain', '')).lstrip('.').lower()
            if domain in allowed and isinstance(item.get('name'), str) and isinstance(item.get('value'), str):
                selected.append({k: item[k] for k in ('name', 'value', 'domain', 'path', 'expires', 'secure', 'httpOnly', 'sameSite') if k in item})
        return selected

    def clear_session_private(self) -> dict:
        """Owner-UI only: erase this app's browser profile and its local copies.

        This is the shared app window, not the user's own Edge profile. It never
        starts a browser, refuses while any tool operation is live, and reports a
        component-level receipt instead of claiming a clean wipe it could not do.
        """
        with self.lock:
            if self.active_operation:
                raise ApiError(409, 'BROWSER_BUSY', '浏览器正在执行操作；请等本次操作结束后再清除会话')
            proc = self.process
            self.process = None
            self.url = self.token = ''
        if proc is not None:
            self.close(terminal=False, force=True)
        elif not self.closing:
            # Nothing is running: an already-live daemon would have been in self.process.
            pass
        home = self.home.resolve()
        components = ('profile', 'downloads', 'outputs', 'tmp', 'actions.json')
        cleared, missing = [], []
        for name in components:
            target = home / name
            try:
                self._verify_owned(home, target)
                if not target.exists():
                    missing.append(name)
                    continue
                self._remove(target)
                cleared.append(name)
            except ApiError:
                raise
            except OSError:
                raise ApiError(503, 'BROWSER_CLEAR_INCOMPLETE',
                               '浏览器会话未能完全清除，请关闭相关窗口后重试', retryable=True,
                               details={'cleared': cleared, 'pending': name}) from None
        for name in ('downloads', 'outputs', 'tmp'):
            # The daemon may never have run, so the owned home can be absent.
            (home / name).mkdir(parents=True, exist_ok=True)
        return {'cleared': True, 'components': cleared, 'absent': missing,
                'note': '已清除助手浏览器的登录会话与本地记录；用户自己的 Edge 配置未受影响，校园授权需重新确认。'}

    def _verify_owned(self, home: Path, target: Path) -> None:
        """Refuse links/reparse points and anything outside the owned browser home."""
        # Walk from the target upward and stop after the owned home, so the target
        # itself and every component down to the home are all inspected.
        for entry in (target, *target.parents):
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                    raise ApiError(409, 'BROWSER_PATH_UNSAFE', '浏览器目录包含链接或重解析点，未执行清除')
            if entry == home:
                break
        try:
            target.resolve().relative_to(home)
        except ValueError:
            raise ApiError(409, 'BROWSER_PATH_UNSAFE', '浏览器清除目标超出应用目录，未执行清除') from None

    @staticmethod
    def _remove(target: Path) -> None:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    def peek(self, cid: str, *, saved_only=False):
        with self.lock:
            alive = self.process is not None and self.process.poll() is None
        if alive and not saved_only:
            return self.rpc(cid, 'status')
        path = self.home / 'actions.json'
        try:
            ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'reviews': [], 'downloads': []}
        except (OSError, ValueError):
            raise ApiError(503, 'BROWSER_STATE_UNREADABLE', '浏览器记录暂时无法读取，原配置仍保留', retryable=True) from None
        reviews = [{k: v for k, v in item.items() if k not in ('call', 'fingerprint', 'private_state')}
                   for item in ledger['reviews'] if item['conversation_id'] == cid]
        for review in reviews:
            if review['status'] == 'executing' and not alive:
                review['status'] = 'uncertain'
        return {'running': False, 'headed': False, 'pages': [], 'reviews': reviews,
                'downloads': [item for item in ledger['downloads'] if item['conversation_id'] == cid]}

    def close(self, *, terminal=True, force=False):
        with self.lock:
            self.closing = self.closing or terminal
            proc, self.process = self.process, None
            self.url = self.token = ''
            self.active_operation = None
            self.unsafe_turns.clear()
        if proc is None:
            return
        try:
            if force:
                proc.campus_owner.terminate()
            proc.stdin.close()
            proc.wait(timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
                else:
                    proc.kill()
                proc.wait(timeout=5)
        finally:
            proc.campus_owner.close()
            proc.stdout.close()


def deliver_downloads(runtime, cid: str, status: dict, tid: str | None = None):
    """Publish successful owned downloads using the existing immutable file store."""
    for item in status.pop('downloads', []):
        key = 'browser-download:' + item['id']
        with runtime.store.connection() as db:
            previous = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? AND request_key=?', (cid, key)).fetchone()
        if previous:
            result = runtime.files.public(previous)
        else:
            path = Path(item['path']).resolve()
            owned = (runtime.browser.home / 'downloads' / cid).resolve()
            if not path.is_relative_to(owned) or not path.is_file():
                continue
            with path.open('rb') as source:
                result = runtime.files.save(cid, source, item['name'], 'output', key, str(path))
        if tid:
            with runtime.store.connection() as db:
                previous_event = db.execute("SELECT 1 FROM events WHERE turn_id=? AND kind='file.published' AND json_extract(data,'$.file.id')=?",
                    (tid, result['id'])).fetchone()
                if not previous_event:
                    from .store import encoded, now
                    db.execute('INSERT INTO events(turn_id,kind,data,created_at) VALUES(?,?,?,?)',
                        (tid, 'file.published', encoded({'file': result}), now()))
    return status
