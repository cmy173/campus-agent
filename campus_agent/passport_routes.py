"""Passport is the entry identity; only trusted browser sessions restore offline caches."""
from datetime import datetime
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .campus import CampusError
from .core import TZ

DEVICE_SECONDS = 30 * 86400
FLOW_COOKIE = 'campus_passport_flow'


def install_passport_routes(app, store, campus, authenticate, hash_password):
    pending, active, completed, attempts, guard = {}, {}, {}, {}, threading.RLock()

    def close():
        with guard:
            for flow in pending.values():
                flow['client'].close()
            pending.clear()
            for flow in active.values():
                flow['cancelled'].set()
            completed.clear()

    def throttle(action, limit):
        with guard:
            recent = [t for t in attempts.get(action, []) if t > time.monotonic()-60]
            if len(recent) >= limit:
                raise HTTPException(429, '登录操作过于频繁，请一分钟后再试')
            attempts[action] = recent + [time.monotonic()]

    async def payload(request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except Exception:
            raise HTTPException(400, '请求必须为 JSON 对象')

    def prepare(old_flow):
        with guard:
            for key in list(pending):
                if key == old_flow or pending[key]['expires'] <= time.monotonic():
                    pending.pop(key)['client'].close()
            if old_flow in active:
                active[old_flow]['cancelled'].set()
            for key in list(completed):
                if completed[key]['expires'] <= time.monotonic():
                    completed.pop(key)
            if len(pending)+len(active) >= 24:
                raise CampusError('FLOW_LIMIT', '待登录流程过多，请稍后重试')
        client = campus.factory()
        try:
            client.start_operation()
            client.prepare()
            try:
                captcha = client.captcha('cas')
            except CampusError as exc:
                if exc.code != 'CAPTCHA_UNAVAILABLE':
                    raise
                captcha = None
        except Exception:
            client.close()
            raise
        flow = secrets.token_hex(32)
        with guard:
            pending[flow] = {'client': client, 'expires': time.monotonic()+300, 'cancelled':threading.Event()}
        return {'flow_id': flow, 'captcha': captcha}

    @app.post('/api/auth/passport/prepare')
    async def passport_prepare(request: Request):
        throttle('prepare', 6)
        try:
            result = await run_in_threadpool(prepare, request.cookies.get(FLOW_COOKIE, ''))
        except CampusError as exc:
            raise HTTPException(409, exc.public()) from None
        if await request.is_disconnected():
            with guard:
                abandoned = pending.pop(result['flow_id'],None)
            if abandoned:
                abandoned['client'].close()
            raise HTTPException(409, {'code':'FLOW_CANCELLED','message':'登录准备已取消'})
        response = JSONResponse(result)
        response.set_cookie(FLOW_COOKIE, result['flow_id'], max_age=300, httponly=True,
                            samesite='strict', path='/api/auth/passport')
        return response

    @app.post('/api/auth/passport/cancel')
    async def passport_cancel(request: Request):
        data = await payload(request)
        flow_id = data.get('flow_id')
        if set(data)-{'flow_id'} or not isinstance(flow_id,str) or not re.fullmatch(r'[0-9a-f]{64}',flow_id):
            raise HTTPException(400, '登录流程格式无效')
        cookie = request.cookies.get(FLOW_COOKIE,'')
        with guard:
            issued = completed.get(flow_id)
            local_digest = hashlib.sha256(request.cookies.get('campus_session','').encode()).hexdigest()
            if not hmac.compare_digest(cookie,flow_id) and not (issued and hmac.compare_digest(local_digest,issued['token'])):
                raise HTTPException(409, {'code':'FLOW_EXPIRED','message':'此浏览器没有对应登录流程'})
            flow = pending.pop(flow_id,None)
            running = active.get(flow_id)
            if running:
                running['cancelled'].set()
            issued = completed.pop(flow_id,None)
        if flow:
            flow['client'].close()
        if issued:
            with store.connect() as db:
                db.execute('DELETE FROM sessions WHERE token=?',(issued['token'],))
            await run_in_threadpool(campus.disconnect_client,issued['owner'],issued['client'])
        response = JSONResponse({'ok':True})
        response.delete_cookie(FLOW_COOKIE,path='/api/auth/passport')
        if issued:
            response.delete_cookie('campus_session',path='/')
        return response

    def finish(flow_id, username, password, code, legacy_owner):
        with guard:
            flow = pending.pop(flow_id, None)
            if flow:
                active[flow_id] = flow
        if not flow:
            raise CampusError('FLOW_EXPIRED', '登录流程已失效，请刷新验证码后重试')
        client = flow['client']
        client.cancel_event = flow['cancelled']
        owner, adopted = None, False
        try:
            if flow['expires'] <= time.monotonic():
                raise CampusError('FLOW_EXPIRED', '登录流程已过期，请重新准备登录')
            client.start_operation()
            client.authenticate_passport(username, password, code)
            # No local password hash is ever derived from the campus password.
            with guard, store.connect() as db:
                if flow['cancelled'].is_set():
                    raise CampusError('FLOW_CANCELLED','登录已取消，请重新准备登录')
                account = db.execute('SELECT owner FROM passport_accounts WHERE identifier=?', (username,)).fetchone()
                if legacy_owner:
                    bound = db.execute('SELECT identifier FROM passport_accounts WHERE owner=?', (legacy_owner,)).fetchone()
                    if bound or (account and account['owner'] != legacy_owner):
                        raise CampusError('ACCOUNT_ALREADY_BOUND', '资料账号或通行证已有绑定，未自动合并资料')
                    owner = legacy_owner
                    db.execute('INSERT INTO passport_accounts VALUES(?,?)', (username, owner))
                elif account:
                    owner = account['owner']
                else:
                    owner = secrets.token_hex(16)
                    db.execute('INSERT INTO users VALUES(?,?,?)',
                               (owner, 'passport:'+username, hash_password(secrets.token_urlsafe(32))))
                    db.execute('INSERT INTO passport_accounts VALUES(?,?)', (username, owner))
            campus.adopt_passport(owner, client)
            adopted = True
            if flow['cancelled'].is_set():
                raise CampusError('FLOW_CANCELLED','登录已取消，请重新准备登录')
        except Exception:
            if adopted:
                campus.disconnect_client(owner,client)
            else:
                client.close()
            raise
        return owner

    @app.post('/api/auth/passport/login')
    async def passport_login(request: Request):
        throttle('login', 3)
        data = await payload(request)
        username, password, code, flow = (data.get('username'), data.get('password'),
                                          data.get('authcode', ''), data.get('flow_id'))
        link = data.get('link_legacy', False)
        if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_@.+\-]{1,128}', username)
                or not isinstance(password, str) or not 1 <= len(password) <= 128
                or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9]{0,12}', code)
                or not isinstance(flow, str) or not re.fullmatch(r'[0-9a-f]{64}', flow)
                or not isinstance(link, bool)):
            raise HTTPException(400, '通行证账号、密码、验证码或流程格式无效')
        if not hmac.compare_digest(request.cookies.get(FLOW_COOKIE, ''), flow):
            raise HTTPException(409, {'code': 'FLOW_EXPIRED', 'message': '当前浏览器的登录流程已变化，请刷新验证码后重试'})
        previous = None
        if link:
            previous = authenticate(request, True)
        else:
            try:
                previous = authenticate(request)
            except HTTPException:
                pass
        owner = None
        try:
            owner = await run_in_threadpool(finish, flow, username, password, code, previous['owner'] if link else None)
            password = ''
            data['password'] = ''
            # Keep the flow cancellable while the first bounded batch is in flight.
            result = await run_in_threadpool(campus.connect_and_sync, owner)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            digest = hashlib.sha256(token.encode()).hexdigest()
            with guard, store.connect() as db:
                running = active[flow]
                if running['cancelled'].is_set():
                    raise CampusError('FLOW_CANCELLED','登录已取消，保留已有资料')
                if previous:
                    db.execute('DELETE FROM sessions WHERE token=?', (previous['token'],))
                db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
                db.execute('INSERT INTO sessions(token,owner,csrf,expires) VALUES(?,?,?,?)',
                           (digest, owner, csrf, time.time()+DEVICE_SECONDS))
                completed[flow] = {'owner':owner,'token':digest,'client':running['client'],'expires':time.monotonic()+300}
            if previous and previous['owner'] != owner:
                await run_in_threadpool(campus.disconnect, previous['owner'])
            store.log(owner, 'passport.login', 'ok', datetime.now(TZ).isoformat())
            store.log(owner, 'campus.sync', 'incomplete' if result['snapshot'].get('warnings') else 'ok', datetime.now(TZ).isoformat())
            response = JSONResponse({'username': username, 'csrf': csrf, 'mode': 'passport', 'campus': result})
            response.set_cookie('campus_session', token, max_age=DEVICE_SECONDS, httponly=True, samesite='strict', path='/')
            response.delete_cookie(FLOW_COOKIE, path='/api/auth/passport')
            return response
        except CampusError as exc:
            raise HTTPException(409, exc.public()) from None
        except sqlite3.IntegrityError:
            raise HTTPException(409, '账号绑定发生冲突，未合并资料，请重新登录') from None
        finally:
            password = ''
            data['password'] = ''
            with guard:
                running = active.pop(flow,None)
            if running and running['cancelled'].is_set() and owner:
                await run_in_threadpool(campus.disconnect_client,owner,running['client'])

    @app.post('/api/campus/reconnect')
    async def reconnect(request: Request):
        owner = authenticate(request, True)['owner']
        throttle('reconnect', 3)
        try:
            return await run_in_threadpool(campus.connect_and_sync, owner)
        except CampusError as exc:
            raise HTTPException(409, exc.public()) from None

    return close
