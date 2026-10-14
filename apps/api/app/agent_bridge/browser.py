"""Lazy owned browser daemon; cookie/profile persistence is independent of turns."""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import queue
import secrets
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

import requests

from ..config import REPO_ROOT
from ..errors import ApiError
from ..process_owner import spawn_owned


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
        self._start()
        with self.lock:
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
            raise ApiError(409, code, data.get('message', '浏览器操作未完成'), retryable=code not in ('BROWSER_ACTION_CHANGED', 'NOT_FOUND'))
        if method == 'stop' and data['result'].get('reset_required'):
            self.close(terminal=False, force=True)
        return data['result']

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
