"""Authenticated, CSRF-protected campus operations, isolated from model tools."""
import re
import threading
import time
from datetime import date

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .campus import CampusError, CampusManager
from .academic_defaults import SEMESTER_PATTERN
from .core import TZ
from datetime import datetime


def install_campus_routes(app, store, authenticate):
    manager = CampusManager(store)
    attempts, guard = {}, threading.Lock()

    def throttle(owner, action, limit):
        with guard:
            key = (owner, action)
            recent = [t for t in attempts.get(key, []) if t > time.monotonic()-60]
            if len(recent) >= limit:
                raise HTTPException(429, '校园操作过于频繁，请一分钟后再试')
            attempts[key] = recent + [time.monotonic()]

    async def payload(request):
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Exception:
            raise HTTPException(400, '请求必须为 JSON 对象')

    async def execute(owner, action, function, *args):
        try:
            result = await run_in_threadpool(function, owner, *args)
        except CampusError as exc:
            store.log(owner, 'campus.' + action, exc.code, datetime.now(TZ).isoformat())
            # Campus expiry must not log the user out of the local workbench.
            raise HTTPException(409, exc.public()) from None
        outcome = 'ok'
        if action == 'sync' and isinstance(result, dict) and (result.get('snapshot') or {}).get('warnings'):
            outcome = 'incomplete'
        if action == 'calendar' and isinstance(result, dict):
            outcome = ((result.get('calendar_result') or {}).get('error') or {}).get('code', 'ok')
        store.log(owner, 'campus.' + action, outcome, datetime.now(TZ).isoformat())
        return result

    @app.get('/api/campus')
    async def status(request: Request):
        owner = authenticate(request)['owner']
        return await run_in_threadpool(manager.status, owner)

    @app.post('/api/campus/prepare')
    async def prepare(request: Request):
        owner = authenticate(request, True)['owner']
        data = await payload(request)
        if data.get('consent') is not True:
            raise HTTPException(400, '请先同意使用校园连接读取本人教务信息')
        throttle(owner, 'prepare', 6)
        return await execute(owner, 'prepare', manager.prepare)

    @app.post('/api/campus/login')
    async def login(request: Request):
        owner = authenticate(request, True)['owner']
        data = await payload(request)
        username, password = data.get('username'), data.get('password')
        flow, code = data.get('flow_id'), data.get('authcode', '')
        if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_@.+\-]{1,128}', username)
                or not isinstance(password, str) or not 1 <= len(password) <= 128
                or not isinstance(flow, str) or not re.fullmatch(r'[0-9a-f]{64}', flow)
                or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9]{0,12}', code)):
            raise HTTPException(400, '校园账号、密码或验证码格式无效')
        if data.get('consent') is not True:
            raise HTTPException(400, '请先同意使用校园连接读取本人教务信息')
        throttle(owner, 'login', 3)
        return await execute(owner, 'login', manager.login, flow, username, password, code)

    @app.post('/api/campus/sync')
    async def sync(request: Request):
        owner = authenticate(request, True)['owner']
        data = await payload(request)
        code, resume = data.get('captcha', ''), data.get('resume', False)
        if ('year' in data or 'semester' in data or not isinstance(resume, bool)
                or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9]{0,12}', code)):
            raise HTTPException(400, '同步自动查询全部可选学期；验证码只能包含字母和数字，resume 必须为布尔值')
        throttle(owner, 'resume' if resume else 'sync', 60 if resume else 3)
        return await execute(owner, 'sync', manager.sync, code, resume)

    @app.post('/api/campus/captcha')
    async def captcha(request: Request):
        owner = authenticate(request, True)['owner']
        throttle(owner, 'captcha', 6)
        return await execute(owner, 'captcha', manager.captcha)

    @app.get('/api/campus/academics')
    async def academics(request: Request):
        owner = authenticate(request)['owner']
        day, term = request.query_params.get('day'), request.query_params.get('term')
        try:
            if day and (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not 2000 <= date.fromisoformat(day).year <= 2099):
                raise ValueError()
            if term and not re.fullmatch(r'(19|20)\d{2}/'+SEMESTER_PATTERN, term):
                raise ValueError()
        except ValueError:
            raise HTTPException(400, '日期或学期格式无效')
        return await run_in_threadpool(manager.academic_view, owner, day, term)

    @app.post('/api/campus/calendar')
    async def calendar(request: Request):
        owner = authenticate(request, True)['owner']
        data = await payload(request)
        year, semester, refresh = data.get('year'), data.get('semester'), data.get('refresh', False)
        if (not isinstance(year, str) or not re.fullmatch(r'(19|20)\d{2}', year)
                or not isinstance(semester, str) or not re.fullmatch(SEMESTER_PATTERN, semester)
                or data.get('campus') != 'main' or not isinstance(refresh, bool)):
            raise HTTPException(400, '请选择已同步学期和主校区本科校历；独立院历暂不适用')
        throttle(owner, 'calendar', 3)
        return await execute(owner, 'calendar', manager.calendar, year, semester, refresh)

    @app.delete('/api/campus')
    async def disconnect(request: Request):
        owner = authenticate(request, True)['owner']
        await execute(owner, 'disconnect', manager.disconnect, True)
        return {'ok': True}

    return manager
