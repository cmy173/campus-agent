"""Local-only FastAPI application. Run via python -m campus_agent.server."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from .core import MockConnector, TZ, recommend
from . import llm, notice_analysis, notice_contract, notice_model
from .campus_common import CampusError
from .connector_contracts import UserContext
from .services import retrieve
from .storage import Store
from .campus_routes import install_campus_routes

MAX_UPLOAD = 20 * 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / 'static'

def now():
    return datetime.now(TZ).isoformat()

def uid():
    return uuid.uuid4().hex

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def hash_password(value, salt=None):
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + hashed

def password_matches(value, stored):
    return hmac.compare_digest(hash_password(value, stored.split(':')[0]), stored)

class RequestGuard:
    """Bound request bytes before FastAPI parsing and reject cross-origin/remote requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}
        host = headers.get('host', '')
        client = (scope.get('client') or ('',))[0]
        allowed_host = re.fullmatch(r'(localhost|127\.0\.0\.1)(:\d+)?', host)
        if not allowed_host or client not in ('127.0.0.1', '::1', 'testclient'):
            return await JSONResponse({'detail': '仅允许本机访问'}, status_code=403)(scope, receive, send)
        if scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            origin = headers.get('origin')
            if headers.get('x-campus-request') != '1' or (origin and origin != 'http://' + host):
                return await JSONResponse({'detail': '请求来源校验失败'}, status_code=403)(scope, receive, send)
            limit = MAX_UPLOAD if scope['path'] == '/api/documents' else 16_384
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > limit:
                    return await JSONResponse({'detail': '请求过大：文件限制 20 MB'}, status_code=413)(scope, receive, send)
                if not message.get('more_body'):
                    break
            delivered = False
            original_receive = receive
            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await original_receive()
            receive = buffered_receive
        async def safe_send(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).extend([
                    (b'x-content-type-options', b'nosniff'),
                    (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'no-referrer'),
                    (b'cache-control', b'no-store'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")])
            await send(message)
        await self.app(scope, receive, safe_send)

def create_app(data_dir=None):
    directory = Path(data_dir or os.environ.get('CAMPUS_DATA_DIR', str(ROOT / 'data')))
    store = Store(directory / 'campus.sqlite3')
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='parser')
    # Separate from parsing so a slow provider call cannot block document parsing.
    analysis_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='notice')
    attempts, attempts_lock = {}, threading.Lock()
    model_attempts = {}
    analysis_attempts = {}
    # In-memory storage for user-provided API keys (not persisted, cleared on restart).
    runtime_api_keys = {}
    runtime_keys_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        # Initialize campus sync scheduler
        from .campus_sync import SyncScheduler
        sync_service = SyncScheduler(store, campus.service)
        app.state.campus_sync = sync_service
        sync_service.start()
        
        yield
        
        # Cleanup
        sync_service.stop()
        pool.shutdown(wait=True, cancel_futures=True)
        analysis_pool.shutdown(wait=True, cancel_futures=True)
        campus.close()
        close_passport_flows()

    app = FastAPI(title='Campus Agent 本地版', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(RequestGuard)
    app.state.store = store
    
    def make_extractor_factory():
        """Create an extractor factory that can access runtime API keys."""
        def extractor_factory(owner=None):
            # Get runtime key for this owner if available
            runtime_key = None
            if owner:
                with runtime_keys_lock:
                    owner_keys = runtime_api_keys.get(owner, {})
                    runtime_key = owner_keys.get('deepseek')
            return notice_model.extractor(runtime_key=runtime_key)
        return extractor_factory
    
    app.state.notice_analyses = notice_analysis.NoticeAnalysisService(store, analysis_pool.submit, make_extractor_factory())

    def user(request, mutate=False):
        token = request.cookies.get('campus_session', '')
        with store.connect() as db:
            row = db.execute('''SELECT s.*, COALESCE(p.identifier,u.username) AS username,
                p.identifier AS passport_identifier FROM sessions s JOIN users u ON u.id=s.owner
                LEFT JOIN passport_accounts p ON p.owner=u.id WHERE token=? AND expires>?''', (digest(token), time.time())).fetchone()
        if not row:
            raise HTTPException(401, '请先登录通行证；已有本机会话可查看离线缓存')
        if mutate and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
            raise HTTPException(403, '会话校验失败，请刷新页面')
        return dict(row)

    campus = install_campus_routes(app, store, user)
    app.state.campus = campus
    app.state.campus_service = campus.service
    from .passport_routes import install_passport_routes
    close_passport_flows = install_passport_routes(app, store, campus, user, hash_password)

    async def payload(request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except Exception:
            raise HTTPException(400, '请求必须为 JSON 对象')

    def document(owner, doc_id):
        with store.connect() as db:
            row = db.execute('SELECT * FROM documents WHERE id=? AND owner=?', (doc_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, '资料不存在')
        return row

    def public_document(row):
        return {k: row[k] for k in ('id', 'name', 'kind', 'status', 'error', 'created')} | {'warnings': json.loads(row['warnings'])}

    def all_tasks(owner):
        with store.connect() as db:
            rows = [dict(r) for r in db.execute('''SELECT t.*, d.name, c.locator, c.text FROM tasks t
                JOIN documents d ON d.id=t.document JOIN chunks c ON c.id=t.chunk
                WHERE d.owner=? ORDER BY t.due IS NULL,t.due,t.id''', (owner,))]
        for row in rows:
            row['evidence'] = json.loads(row['evidence'] or '[]')
            row['attachment_refs'] = json.loads(row['attachment_refs'] or '[]')
        return rows

    def store_document(db, owner, name, kind, content):
        """Return (doc_id, duplicate). Callers submit parsing only for new documents."""
        sha = hashlib.sha256(content).hexdigest()
        existing = db.execute('SELECT id FROM documents WHERE owner=? AND sha=?', (owner, sha)).fetchone()
        if existing:
            return existing['id'], True
        count = db.execute('SELECT COUNT(*) FROM documents WHERE owner=?', (owner,)).fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM documents WHERE status IN ('queued','processing')").fetchone()[0]
        if count >= 100 or active >= 10:
            raise HTTPException(429, '资料数达到 100 份或解析队列已满，请稍后再试或删除旧资料')
        doc_id = uid()
        db.execute('INSERT INTO documents(id,owner,name,kind,sha,content,status,created) VALUES(?,?,?,?,?,?,?,?)',
                   (doc_id, owner, name, kind, sha, content, 'queued', now()))
        return doc_id, False

    def parse_document(doc_id, owner):
        with store.connect() as db:
            row = db.execute('SELECT name,content FROM documents WHERE id=? AND owner=?', (doc_id, owner)).fetchone()
            if not row:
                return
            db.execute("UPDATE documents SET status='processing' WHERE id=? AND owner=?", (doc_id, owner))
        try:
            result = subprocess.run([sys.executable, '-m', 'campus_agent.parser_worker', Path(row['name']).suffix.lower()],
                                    input=bytes(row['content']), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=ROOT, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('解析进程异常退出')
            output = json.loads(result.stdout.decode('utf-8'))
            if 'error' in output:
                raise ValueError(output['error'])
            with store.connect() as db:
                record = db.execute('SELECT kind,sha FROM documents WHERE id=? AND owner=?', (doc_id, owner)).fetchone()
                if not record:  # Deleted while parsing: never recreate derived data.
                    return
                chunks = []
                for position, chunk in enumerate(output['chunks']):
                    chunk_id = uid()
                    db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)', (chunk_id, doc_id, position, chunk['locator'], chunk['text']))
                    chunks.append({'id': chunk_id, 'ref': notice_contract.paragraph_ref(position),
                                   'locator': chunk['locator'], 'text': chunk['text']})
                db.execute("UPDATE documents SET status='ready',warnings=? WHERE id=? AND owner=?",
                           (json.dumps(output['warnings'], ensure_ascii=False), doc_id, owner))
                if record['kind'] == 'notice':
                    # Offline rule extraction stays available without any model configured.
                    source = db.execute('SELECT attachments FROM document_sources WHERE document=? AND owner=?',
                                        (doc_id, owner)).fetchone()
                    app.state.notice_analyses.write_rules(db, owner, doc_id, record['sha'], chunks,
                        json.loads(source['attachments']) if source else [])
            store.log(owner, 'document.parse', 'ready', now())
        except Exception as exc:
            message = '解析超过 30 秒，请拆分文件' if isinstance(exc, subprocess.TimeoutExpired) else (str(exc) if isinstance(exc, ValueError) else '解析失败，请重试或更换文件')
            with store.connect() as db:
                db.execute("UPDATE documents SET status='failed',error=? WHERE id=? AND owner=?", (message, doc_id, owner))

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/static/{name}')
    def asset(name: str):
        if name not in ('app.js', 'campus.js', 'academics.js', 'passport.js', 'college-notices.js',
                        'notice-analysis.js', 'webvpn.js', 'style.css', 'academics.css'):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get('/api/health')
    def health():
        from .campus import CONNECTOR_REVISION
        return {'status': 'ok', 'mode': 'local-retrieval', 'version': '0.2.0',
                'campus_connector_revision': CONNECTOR_REVISION}

    @app.post('/api/auth/{action}')
    async def auth(action: str, request: Request):
        if action == 'logout':
            account = user(request, True)
            await run_in_threadpool(campus.disconnect, account['owner'])
            with store.connect() as db:
                db.execute('DELETE FROM sessions WHERE token=?', (account['token'],))
            response = JSONResponse({'ok': True})
            response.delete_cookie('campus_session', path='/')
            response.delete_cookie('campus_passport_flow', path='/api/auth/passport')
            return response
        if action not in ('login', 'register'):
            raise HTTPException(404)
        key = request.client.host
        with attempts_lock:
            recent = [t for t in attempts.get(key, []) if t > time.time()-60]
            if len(recent) >= 12:
                raise HTTPException(429, '尝试过于频繁，请一分钟后再试')
            attempts[key] = recent + [time.time()]
        data = await payload(request)
        username, password = data.get('username', ''), data.get('password', '')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_\-]{3,32}', username):
            raise HTTPException(400, '用户名使用 3–32 位英文字母、数字、下划线或短横线')
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise HTTPException(400, '密码需要 10–128 个字符')
        with store.connect() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if action == 'register':
                if row:
                    raise HTTPException(409, '用户名已存在')
                owner = uid()
                try:
                    db.execute('INSERT INTO users VALUES(?,?,?)', (owner, username, hash_password(password)))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, '用户名已存在')
            else:
                # Perform a hash even for an unknown username.
                hashed = row['password'] if row else '0'*32 + ':' + '0'*128
                if not password_matches(password, hashed) or not row:
                    raise HTTPException(401, '用户名或密码不正确')
                owner = row['id']
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions(token,owner,csrf,expires) VALUES(?,?,?,?)',
                       (digest(token), owner, csrf, time.time()+43200))
        response = JSONResponse({'username': username, 'csrf': csrf})
        response.set_cookie('campus_session', token, max_age=43200, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/api/me')
    def me(request: Request):
        account = user(request)
        mode = 'legacy'
        if account['passport_identifier']:
            mode = 'passport' if campus.status(account['owner'])['passport_connected'] else 'offline'
        return {'username': account['username'], 'csrf': account['csrf'], 'mode': mode}

    @app.get('/api/model')
    def model_configuration(request: Request):
        account = user(request)
        # Get runtime key for this owner if available
        runtime_key = None
        with runtime_keys_lock:
            owner_keys = runtime_api_keys.get(account['owner'], {})
            runtime_key = owner_keys.get('deepseek')
        
        config = llm.configuration() | {'notice': notice_model.configuration(runtime_key=runtime_key)}
        # Include runtime key status (never the actual key)
        config['notice']['has_runtime_key'] = bool(runtime_key)
        return config

    @app.post('/api/model/configure', status_code=200)
    async def configure_model(request: Request):
        """Set a runtime API key for the current user (in-memory, cleared on restart)."""
        account = user(request, True)
        data = await payload(request)
        key_type = data.get('key_type')
        key_value = (data.get('key_value') or '').strip()
        if key_type not in ('deepseek', 'openai'):
            raise HTTPException(400, '不支持的模型类型')
        if key_value and len(key_value) < 8:
            raise HTTPException(400, 'API Key 格式无效')
        with runtime_keys_lock:
            if key_value:
                runtime_api_keys[account['owner']] = {key_type: key_value}
            else:
                # Clear the key if empty value provided
                if account['owner'] in runtime_api_keys:
                    runtime_api_keys[account['owner']].pop(key_type, None)
                    if not runtime_api_keys[account['owner']]:
                        del runtime_api_keys[account['owner']]
        store.log(account['owner'], 'model.configure', f'{key_type}={"set" if key_value else "cleared"}', now())
        return {'configured': bool(key_value), 'key_type': key_type}

    @app.post('/api/campus/webvpn/credentials', status_code=200)
    async def save_campus_credentials(request: Request):
        """Save encrypted WebVPN and CAS credentials for automatic login.
        
        IMPORTANT: WebVPN account and CAS (passport) account may be different!
        - WebVPN account: Used to login to WebVPN system (network account)
        - CAS account: Used to login to campus services (academic affairs, email, etc.)
        
        For most users, both accounts are the same (student ID).
        If they differ, you must specify both separately.
        
        Expects JSON body with:
        - webvpn_username: WebVPN network account (required)
        - webvpn_password: WebVPN password (required)
        - zjuam_username: CAS/passport account (optional, defaults to webvpn_username)
        - zjuam_password: CAS/passport password (optional, defaults to webvpn_password)
        
        Example (same account):
        {
            "webvpn_username": "3250103732",
            "webvpn_password": "password"
        }
        
        Example (different accounts):
        {
            "webvpn_username": "net-account",
            "webvpn_password": "net-password",
            "zjuam_username": "3250103732",
            "zjuam_password": "cas-password"
        }
        """
        from .campus_crypto import CredentialCipher
        
        account = user(request, True)
        data = await payload(request)
        
        # Validate required fields
        if not data.get('webvpn_username') or not data.get('webvpn_password'):
            raise HTTPException(400, '缺少 WebVPN 账号或密码')
        
        # Encrypt passwords
        cipher = CredentialCipher(account['owner'])
        webvpn_encrypted = cipher.encrypt(data['webvpn_password'])
        zjuam_username = data.get('zjuam_username', data['webvpn_username'])
        zjuam_password = data.get('zjuam_password', data['webvpn_password'])
        zjuam_encrypted = cipher.encrypt(zjuam_password)
        
        now_str = now()
        auto_sync = 1 if data.get('auto_sync', True) else 0

        with store.connect() as db:
            # Upsert keeps any existing session cookies and sync history.
            db.execute('''INSERT INTO campus_credentials
                (owner, webvpn_username, webvpn_password_encrypted,
                 zjuam_username, zjuam_password_encrypted,
                 encryption_version, auto_sync_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET
                    webvpn_username=excluded.webvpn_username,
                    webvpn_password_encrypted=excluded.webvpn_password_encrypted,
                    zjuam_username=excluded.zjuam_username,
                    zjuam_password_encrypted=excluded.zjuam_password_encrypted,
                    auto_sync_enabled=excluded.auto_sync_enabled,
                    updated_at=excluded.updated_at''',
                (account['owner'],
                 str(data['webvpn_username'])[:128],
                 json.dumps(webvpn_encrypted),
                 str(zjuam_username)[:128],
                 json.dumps(zjuam_encrypted),
                 auto_sync, now_str, now_str))

        store.log(account['owner'], 'campus.credentials.save', 'success', now_str)

        return {'status': 'success', 'message': '凭据已保存，后台将尝试自动登录'}

    @app.post('/api/campus/webvpn/sync', status_code=202)
    async def trigger_campus_sync(request: Request):
        """Trigger manual synchronization of campus data.
        
        Returns immediately; sync runs in background thread.
        """
        account = user(request, True)
        
        # Check if credentials exist
        with store.connect() as db:
            cred = db.execute('SELECT owner FROM campus_credentials WHERE owner=?',
                            (account['owner'],)).fetchone()
        
        if not cred:
            raise HTTPException(404, '未保存 WebVPN 凭据，请先配置')
        
        # Trigger sync via service
        sync_service = getattr(app.state, 'campus_sync', None)
        if sync_service:
            result = sync_service.trigger_manual_sync(account['owner'])
            return result
        else:
            # Fallback: just log that sync was requested
            store.log(account['owner'], 'campus.sync.requested', 'queued', now())
            return {'status': 'queued', 'message': '同步请求已接收'}

    @app.get('/api/campus/webvpn/status')
    def get_campus_status(request: Request):
        """Get current WebVPN credential and sync status (never the secrets)."""
        account = user(request)

        with store.connect() as db:
            cred = db.execute('''SELECT webvpn_username, zjuam_username, last_used_at,
                webvpn_expires_at, auto_sync_enabled, sync_interval_minutes,
                last_sync_at, last_sync_status FROM campus_credentials WHERE owner=?''',
                (account['owner'],)).fetchone()

        if not cred:
            return {'has_credentials': False, 'auto_sync': None}

        return {
            'has_credentials': True,
            'webvpn_username': cred['webvpn_username'],
            'zjuam_username': cred['zjuam_username'],
            'last_used_at': cred['last_used_at'],
            'session_expires_at': cred['webvpn_expires_at'],
            'auto_sync': bool(cred['auto_sync_enabled']),
            'sync_interval_minutes': cred['sync_interval_minutes'],
            'last_sync_at': cred['last_sync_at'],
            'last_sync_status': cred['last_sync_status'],
        }

    @app.patch('/api/campus/webvpn/sync-settings')
    async def update_sync_settings(request: Request):
        """Toggle automatic synchronization without re-entering credentials."""
        account = user(request, True)
        data = await payload(request)
        allowed = {'auto_sync', 'sync_interval_minutes'}
        if set(data) - allowed:
            raise HTTPException(400, '只接受 auto_sync 与 sync_interval_minutes')
        auto_sync = data.get('auto_sync')
        interval = data.get('sync_interval_minutes')
        if auto_sync is not None and not isinstance(auto_sync, bool):
            raise HTTPException(400, 'auto_sync 必须为布尔值')
        if interval is not None and not (isinstance(interval, int) and 5 <= interval <= 1440):
            raise HTTPException(400, '同步间隔需为 5-1440 分钟的整数')

        sets, values = [], []
        if auto_sync is not None:
            sets.append('auto_sync_enabled=?')
            values.append(1 if auto_sync else 0)
        if interval is not None:
            sets.append('sync_interval_minutes=?')
            values.append(interval)
        if not sets:
            raise HTTPException(400, '没有可更新的设置')
        values.append(account['owner'])
        with store.connect() as db:
            cursor = db.execute(f'UPDATE campus_credentials SET {", ".join(sets)} WHERE owner=?', values)
            if cursor.rowcount == 0:
                raise HTTPException(404, '未保存 WebVPN 凭据，请先配置')
        store.log(account['owner'], 'campus.sync-settings', 'updated', now())
        return {'status': 'success'}

    @app.delete('/api/campus/webvpn/credentials')
    def delete_campus_credentials(request: Request):
        """Remove stored credentials, cookies and sync state for this account."""
        account = user(request, True)
        with store.connect() as db:
            db.execute('DELETE FROM campus_credentials WHERE owner=?', (account['owner'],))
        store.log(account['owner'], 'campus.credentials.delete', 'success', now())
        return {'status': 'success', 'message': '凭据已清除'}

    @app.get('/api/documents')
    def documents(request: Request):
        account = user(request)
        with store.connect() as db:
            rows = db.execute('SELECT id,name,kind,status,error,warnings,created FROM documents WHERE owner=? ORDER BY created DESC', (account['owner'],)).fetchall()
        return [public_document(r) for r in rows]

    @app.post('/api/documents', status_code=202)
    async def upload(request: Request):
        account = user(request, True)
        name = unquote(request.headers.get('x-filename', ''))
        if not name or len(name) > 180 or any(c in name for c in ('/', '\\', '\x00', '\r', '\n')):
            raise HTTPException(400, '文件名无效或过长')
        if Path(name).suffix.lower() not in ('.txt', '.pdf', '.docx', '.pptx'):
            raise HTTPException(400, '仅支持 TXT、PDF、DOCX、PPTX，不支持旧版 DOC/PPT')
        kind = request.headers.get('x-document-kind', 'notice')
        if kind not in ('notice', 'material'):
            raise HTTPException(400, '未知资料类型')
        content = await request.body()
        if not content:
            raise HTTPException(400, '不能上传空文件')
        with store.connect() as db:
            doc_id, duplicate = store_document(db, account['owner'], name, kind, content)
        if not duplicate:
            pool.submit(parse_document, doc_id, account['owner'])
        store.log(account['owner'], 'document.upload', 'duplicate' if duplicate else 'queued', now())
        return JSONResponse({'id': doc_id, 'duplicate': duplicate}, status_code=200 if duplicate else 202)

    def throttle_analysis(owner, action):
        with attempts_lock:
            recent = [t for t in analysis_attempts.get(owner, []) if t > time.time()-3600]
            limit = 60 if action == 'import' else 12
            if len(recent) >= limit:
                kind = '通知导入' if action == 'import' else '模型整理'
                raise HTTPException(429, f'本账号每小时最多 {limit} 次{kind}，请稍后再试')
            analysis_attempts[owner] = recent + [time.time()]

    @app.post('/api/college-notices/import', status_code=202)
    async def import_college_notice(request: Request):
        """Save a cached college notice. Provenance is server-owned: only an ID is accepted."""
        account = user(request, True)
        data = await payload(request)
        if set(data) - {'record_id'}:
            raise HTTPException(400, '只接受 record_id；来源、附件与身份都由服务端缓存决定')
        record_id = data.get('record_id')
        try:
            found = await run_in_threadpool(campus.service.cached_college_notice,
                                            UserContext(account['owner']), record_id)
        except CampusError:
            raise HTTPException(400, '通知记录标识无效') from None
        if not found:
            raise HTTPException(404, '当前账号缓存中没有这条通知，请先同步学院通知')
        platform, record = found
        throttle_analysis(account['owner'], 'import')
        name = re.sub(r'[\\/:*?"<>|\r\n\x00]', '_', record.title)[:150].strip() or '学院通知'
        content = (record.title + '\n\n' + record.body).encode('utf-8')
        with store.connect() as db:
            doc_id, duplicate = store_document(db, account['owner'], name + '.txt', 'notice', content)
            notice_analysis.save_source(db, account['owner'], doc_id, platform, record, now())
        if not duplicate:
            pool.submit(parse_document, doc_id, account['owner'])
        store.log(account['owner'], 'notice.import', 'duplicate' if duplicate else 'queued', now())
        return JSONResponse({'id': doc_id, 'duplicate': duplicate, 'name': name + '.txt',
                             'attachments': len(record.attachments),
                             'has_unread_content': record.has_unread_content},
                            status_code=200 if duplicate else 202)

    @app.get('/api/documents/{doc_id}')
    def get_document(doc_id: str, request: Request):
        account = user(request)
        row = document(account['owner'], doc_id)
        with store.connect() as db:
            chunks = [dict(r) for r in db.execute('''SELECT c.id,c.position,c.locator,c.text FROM chunks c JOIN documents d ON c.document=d.id
                WHERE c.document=? AND d.owner=? ORDER BY c.position''', (doc_id, account['owner']))]
        for chunk in chunks:
            chunk['ref'] = notice_contract.paragraph_ref(chunk.pop('position'))
        analysis = app.state.notice_analyses.read(account['owner'], doc_id)
        return public_document(row) | {'chunks': chunks, 'source': (analysis or {}).get('source'),
                                       'analyses': (analysis or {}).get('analyses', [])}

    @app.get('/api/notice-analyses')
    def notice_analyses(request: Request):
        """Every notice of this account with its provenance and analysis state."""
        account = user(request)
        return {'documents': app.state.notice_analyses.list_all(account['owner']),
                'model': notice_model.configuration()}

    @app.post('/api/documents/{doc_id}/analysis')
    async def analyze_document(doc_id: str, request: Request):
        account = user(request, True)
        data = await payload(request)
        if set(data) - {'mode', 'consent'}:
            raise HTTPException(400, '只接受 mode 与 consent')
        mode = data.get('mode', notice_analysis.MODEL)
        consent = data.get('consent') is True
        if not isinstance(mode, str) or mode not in (notice_analysis.RULES, notice_analysis.MODEL):
            raise HTTPException(400, '未知整理方式')
        throttle_analysis(account['owner'], mode)
        try:
            result = await run_in_threadpool(app.state.notice_analyses.request,
                                             account['owner'], doc_id, mode, consent)
        except notice_analysis.AnalysisError as exc:
            raise HTTPException(exc.status, exc.message) from None
        if result is None:
            raise HTTPException(404, '资料不存在')
        store.log(account['owner'], 'notice.analyze', mode, now())
        return result

    @app.get('/api/documents/{doc_id}/download')
    def download(doc_id: str, request: Request):
        account = user(request)
        row = document(account['owner'], doc_id)
        return Response(bytes(row['content']), media_type='application/octet-stream',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row['name'])})

    @app.delete('/api/documents/{doc_id}')
    def delete_document(doc_id: str, request: Request):
        account = user(request, True)
        document(account['owner'], doc_id)
        with store.connect() as db:
            db.execute('DELETE FROM documents WHERE id=? AND owner=?', (doc_id, account['owner']))
        store.log(account['owner'], 'document.delete', 'ok', now())
        return {'ok': True}

    @app.get('/api/tasks')
    def tasks(request: Request):
        return all_tasks(user(request)['owner'])

    @app.patch('/api/tasks/{task_id}')
    async def update_task(task_id: str, request: Request):
        account = user(request, True)
        data = await payload(request)
        state = data.get('state', 'pending')
        due = data.get('due')
        if state not in ('pending', 'done', 'ignored'):
            raise HTTPException(400, '未知待办状态')
        if due:
            try:
                dt = datetime.fromisoformat(due)
                if dt.tzinfo is None:
                    raise ValueError()
                due = dt.astimezone(TZ).isoformat()
            except (ValueError, TypeError):
                raise HTTPException(400, '日期需包含时区')
        with store.connect() as db:
            row = db.execute('SELECT t.id FROM tasks t JOIN documents d ON d.id=t.document WHERE t.id=? AND d.owner=?', (task_id, account['owner'])).fetchone()
            if not row:
                raise HTTPException(404, '待办不存在')
            db.execute('UPDATE tasks SET state=?,due=?,reviewed=1 WHERE id=?', (state, due, task_id))
        store.log(account['owner'], 'task.review', 'ok', now())
        return {'ok': True}

    @app.post('/api/chat')
    async def chat(request: Request):
        account = user(request, True)
        data = await payload(request)
        query = data.get('query', '')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
            raise HTTPException(400, '问题长度应为 1–2000 字')
        store.log(account['owner'], 'query', 'local', now())
        steps = ['识别查询', '仅检索当前账号资料', '返回原文依据']
        if re.search(r'(帮我|替我|自动).{0,8}(报名|提交|登录|申请)', query):
            return {'answer': '目前只支持查询和本地待办管理，不能替你操作校园平台。', 'citations': [], 'steps': ['权限边界检查'], 'mode': 'local'}
        if re.fullmatch(r'[查看列出我今天的近期有什么哪些需要关注校园通知待办截止事项作业简报？?\s]+', query) and any(w in query for w in ('待办', '截止', '简报', '通知')):
            items = [x for x in all_tasks(account['owner']) if x['state'] == 'pending']
            return {'answer': f'目前有 {len(items)} 条未完成事项。以下是规则提取结果，请核对时间与原文。' if items else '暂无未完成事项。可先以“通知”类型上传资料；课程资料不会自动生成待办。',
                    'tasks': items[:20], 'citations': [], 'steps': ['查询当前账号待办', '按截止时间排序'], 'mode': 'local'}
        with store.connect() as db:
            rows = db.execute('''SELECT c.*,d.name FROM chunks c JOIN documents d ON d.id=c.document
                WHERE d.owner=? AND d.status='ready' ''', (account['owner'],)).fetchall()
        matches = retrieve(query, rows)
        answer = '找到以下相关原文。当前为本地关键词检索，未使用大模型推理；请结合引用判断是否回答了问题。' if matches else '没有找到足够相关的资料。请上传对应材料，或使用原文中的具体关键词重试。'
        if data.get('use_model') is True and matches:
            with attempts_lock:
                recent = [t for t in model_attempts.get(account['owner'], []) if t > time.time()-3600]
                if len(recent) >= 20:
                    raise HTTPException(429, '本账号每小时最多 20 次模型请求，请使用本地模式')
                model_attempts[account['owner']] = recent + [time.time()]
            try:
                result = await run_in_threadpool(llm.answer, query, matches)
                store.log(account['owner'], 'model.query', 'ok', now())
                return result | {'steps': steps + ['OpenAI 生成', '来源 ID 校验']}
            except ValueError as exc:
                store.log(account['owner'], 'model.query', 'fallback', now())
                return {'answer': answer, 'citations': matches, 'steps': steps, 'mode': 'local', 'warning': str(exc)}
        return {'answer': answer, 'citations': matches, 'steps': steps, 'mode': 'local'}

    @app.get('/api/activities')
    def activities(request: Request, target: float = 2):
        user(request)
        if not 0 <= target <= 30:
            raise HTTPException(400, '演示目标请设为 0–30')
        clock = datetime.now(TZ)
        return recommend(MockConnector().activities(clock), clock, target)

    @app.get('/api/audit')
    def audit(request: Request):
        account = user(request)
        with store.connect() as db:
            return [dict(r) for r in db.execute('SELECT action,result,created FROM audit WHERE owner=? ORDER BY id DESC LIMIT 50', (account['owner'],))]

    @app.delete('/api/data')
    async def clear_data(request: Request):
        account = user(request, True)
        await run_in_threadpool(campus.disconnect, account['owner'], True)
        with store.connect() as db:
            db.execute('DELETE FROM documents WHERE owner=?', (account['owner'],))
            db.execute('DELETE FROM audit WHERE owner=?', (account['owner'],))
        return {'ok': True}

    return app
