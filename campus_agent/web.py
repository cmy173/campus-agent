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
from . import llm
from .services import extract_tasks, retrieve
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
    attempts, attempts_lock = {}, threading.Lock()
    model_attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True, cancel_futures=True)
        campus.close()
        close_passport_flows()

    app = FastAPI(title='Campus Agent 本地版', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(RequestGuard)
    app.state.store = store

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
            return [dict(r) for r in db.execute('''SELECT t.*, d.name, c.locator, c.text FROM tasks t
                JOIN documents d ON d.id=t.document JOIN chunks c ON c.id=t.chunk
                WHERE d.owner=? ORDER BY t.due IS NULL,t.due,t.id''', (owner,))]

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
                record = db.execute('SELECT kind FROM documents WHERE id=? AND owner=?', (doc_id, owner)).fetchone()
                if not record:  # Deleted while parsing: never recreate derived data.
                    return
                for position, chunk in enumerate(output['chunks']):
                    chunk_id = uid()
                    db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)', (chunk_id, doc_id, position, chunk['locator'], chunk['text']))
                    if record['kind'] == 'notice':
                        for task in extract_tasks(chunk['text']):
                            db.execute('INSERT INTO tasks(id,document,chunk,title,due,raw_date) VALUES(?,?,?,?,?,?)',
                                       (uid(), doc_id, chunk_id, task['title'], task['due'], task['raw_date']))
                db.execute("UPDATE documents SET status='ready',warnings=? WHERE id=? AND owner=?",
                           (json.dumps(output['warnings'], ensure_ascii=False), doc_id, owner))
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
        if name not in ('app.js', 'campus.js', 'academics.js', 'passport.js', 'style.css', 'academics.css'):
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
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(token), owner, csrf, time.time()+43200))
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
        user(request)
        return llm.configuration()

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
        sha = hashlib.sha256(content).hexdigest()
        with store.connect() as db:
            duplicate = db.execute('SELECT id FROM documents WHERE owner=? AND sha=?', (account['owner'], sha)).fetchone()
            if duplicate:
                return JSONResponse({'id': duplicate['id'], 'duplicate': True}, status_code=200)
            count = db.execute('SELECT COUNT(*) FROM documents WHERE owner=?', (account['owner'],)).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM documents WHERE status IN ('queued','processing')").fetchone()[0]
            if count >= 100 or active >= 10:
                raise HTTPException(429, '资料数达到 100 份或解析队列已满，请稍后再试或删除旧资料')
            doc_id = uid()
            db.execute('INSERT INTO documents(id,owner,name,kind,sha,content,status,created) VALUES(?,?,?,?,?,?,?,?)',
                       (doc_id, account['owner'], name, kind, sha, content, 'queued', now()))
        pool.submit(parse_document, doc_id, account['owner'])
        store.log(account['owner'], 'document.upload', 'queued', now())
        return {'id': doc_id, 'duplicate': False}

    @app.get('/api/documents/{doc_id}')
    def get_document(doc_id: str, request: Request):
        account = user(request)
        row = document(account['owner'], doc_id)
        with store.connect() as db:
            chunks = [dict(r) for r in db.execute('''SELECT c.id,c.locator,c.text FROM chunks c JOIN documents d ON c.document=d.id
                WHERE c.document=? AND d.owner=? ORDER BY c.position''', (doc_id, account['owner']))]
        return public_document(row) | {'chunks': chunks}

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
        if state not in ('pending', 'done'):
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
