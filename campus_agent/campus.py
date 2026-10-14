"""Local, opt-in undergraduate CAS connector. No passwords or cookies on disk.

Protocol references and verification boundaries: docs/campus-login.md.
Only the fixed CAS, ZDBK and ETA services below can be contacted.
"""
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlencode, urljoin, urlsplit, parse_qs
import base64
import hashlib
import http.client
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from .core import TZ
from .academic_defaults import academic_year, semester_order, SEMESTER_PATTERN, fallback_terms, resolve_calendar, calendar_family, merge_term_catalog
from .campus_profiles import ETA_SERVICE, PROFILE_KEYS, basic_fields, eta_profile, zdbk_profile
from .campus_repository import SnapshotRepository, SQLiteCacheRepository

from .campus_common import CampusError, _Text, plain, decode_page
from .campus_endpoints import *
from .campus_endpoints import _REQUEST_STAGES, _JSON_PATHS
from .campus_network import CampusTransport, check_url, _NoRedirect, _transport_failure
from .connectors.zdbk_protocol import ZdbkProtocol, _TermOptions, _field, normalize_profile, normalize_course, normalize_grade

from .campus_auth import CampusAuthentication, _Inputs, encrypt_password


class CampusClient(CampusAuthentication, CampusTransport, ZdbkProtocol):
    def profiles(self, expected_id=''):
        """Independent, bounded sources. An ETA failure does not expire ZDBK."""
        records, warnings = [], []
        deadline = self.deadline
        self.deadline = min(deadline, time.monotonic()+35)
        try:
            for adapter, source, stage in ((zdbk_profile, '本科教务网 · 个人信息', 'profile_zdbk'),
                                            (eta_profile, 'ETA · 个人基本信息', 'profile_eta')):
                if adapter == eta_profile and not (self.passport_authenticated or getattr(self,'eta_authenticated',False)):
                    continue
                try:
                    result = adapter(self, expected_id) if adapter == eta_profile else adapter(self)
                    if result:
                        fields, url = result
                        identity = fields.get('student_id')
                        if expected_id and identity and expected_id != identity:
                            raise CampusError('PROFILE_IDENTITY_MISMATCH', '个人信息来源的学号不一致，未合并资料')
                        expected_id = expected_id or identity or ''
                        records.append({'data': fields, 'source': source, 'url': url} |
                            ({'invalidated_fields':getattr(self,'eta_invalidated_fields',[]),
                              'field_urls':getattr(self,'eta_field_urls',{})} if adapter==eta_profile else {}))
                        if adapter == eta_profile:
                            warnings.extend(getattr(self,'eta_followup_warnings',[]))
                            missing = [k for k in PROFILE_KEYS if not fields.get(k)]
                            if missing:
                                missing_form=not getattr(self,'eta_form_available',True)
                                warnings.append({'code':'PROFILE_FORM_UNAVAILABLE' if missing_form else 'PROFILE_PARTIAL','stage':stage,
                                    'message':'ETA 身份已验证，但未取得可解析的个人基本信息表单；保留已验证信息和缓存' if missing_form else
                                              'ETA 本人信息已读取，但部分基本字段仍未取得；详见同步诊断',
                                    'diagnostic':getattr(self,'eta_field_diagnostic',{})})
                except CampusError as exc:
                    # Business sessions are independent: failure of this adapter
                    # must not discard grades or stop the timetable query.
                    warning = exc.public() | {'stage': stage}
                    if exc.code == 'SESSION_EXPIRED':
                        warning.update(code='PROFILE_SESSION_EXPIRED', message='基本信息来源会话失效，保留缓存；其他教务查询继续')
                    warnings.append(warning)
        finally:
            self.deadline = deadline
        return records, warnings

def stale_profile(snapshot):
    profile = snapshot.get('profile')
    if not profile:
        return
    profile['stale'] = True
    sources = profile.setdefault('field_sources', {})
    for key in PROFILE_KEYS:
        if profile.get('data', {}).get(key):
            sources.setdefault(key, {'source': '本科教务网 · 上次资料', 'url': TIMETABLE,
                                     'fetched_at': profile.get('fetched_at')})['stale'] = True


def merge_profile(snapshot, record, *, fill_only=False, protect_fresh=False):
    """Only six basic fields, with independent provenance for retained cache."""
    incoming = basic_fields(record.get('data'))
    if not any(incoming.values()):
        return
    old = snapshot.get('profile') or {}
    previous = old.get('data', {})
    if previous.get('student_id') and incoming.get('student_id') and previous['student_id'] != incoming['student_id']:
        raise CampusError('PROFILE_IDENTITY_MISMATCH', '个人信息来源的学号不一致，未合并资料')
    data = {key: previous.get(key, '') for key in PROFILE_KEYS}
    sources = dict(old.get('field_sources', {}))
    for key in record.get('invalidated_fields',[]):
        source=sources.get(key,{})
        if key=='college' and not incoming.get(key) and source.get('source','').startswith('ETA') and source.get('url')==record.get('url'):
            data[key]=''
            sources.pop(key,None)
    stamp = datetime.now(TZ).isoformat()
    for key, value in incoming.items():
        if not value or (data[key] and (fill_only or (protect_fresh and sources.get(key, {}).get('stale') is False))):
            continue
        data[key] = value
        sources[key] = {'source': record['source'], 'url': record.get('field_urls',{}).get(key,record['url']), 'fetched_at': stamp, 'stale': False}
    snapshot['profile'] = {'data': data, 'field_sources': sources, 'fetched_at': stamp,
                           'stale': all(m.get('stale', True) for m in sources.values()),
                           'complete': all(data.values()), 'missing_fields': [k for k,v in data.items() if not v]}


def refresh_profile(snapshot, client, warnings):
    if not callable(getattr(client, 'profiles', None)):
        return
    try:
        identity = (snapshot.get('profile') or {}).get('data', {}).get('student_id', '')
        records, failures = client.profiles(identity)
        warnings.extend(failures)
        snapshot['profile_checks'] = []
        snapshot['profile_checks'].extend({'source':w.get('stage','profile'),'status':'failed','code':w['code']}
                                         for w in failures if w['code'] != 'PROFILE_PARTIAL')
        for record in records:
            try:
                merge_profile(snapshot, record)
                snapshot['profile_checks'].append({'source':record['source'],'status':'read',
                    'field_status':{k:'available' if record.get('data',{}).get(k) else 'unavailable' for k in PROFILE_KEYS}})
            except CampusError as exc:
                warnings.append(exc.public() | {'stage': 'profile'})
                snapshot['profile_checks'].append({'source':record['source'],'status':'failed','code':exc.code})
    except CampusError as exc:
        failure = exc.public() | {'stage': 'profile'}
        if exc.code == 'SESSION_EXPIRED':
            failure['code'] = 'PROFILE_SESSION_EXPIRED'
        warnings.append(failure)
        snapshot['profile_checks'] = [{'source':'profile','status':'failed','code':failure['code']}]
    finally:
        client.start_operation()


class CampusManager:
    """Serialize all operations per local owner, including disconnect and deletion."""
    def __init__(self, store, client_factory=CampusClient):
        from .calendar_sources import CalendarClient
        self.store, self.factory = store, client_factory
        self.repository = SnapshotRepository(store)
        self.capability_repository = SQLiteCacheRepository(store)
        self.calendar_client = CalendarClient()
        self.states, self.locks = {}, {}
        self.guard = threading.Lock()
        from .campus_service import CampusService
        self.service = CampusService(self, self.capability_repository)

    def lock(self, owner):
        with self.guard:
            return self.locks.setdefault(owner, threading.RLock())

    def _state(self, owner):
        state = self.states.get(owner)
        if state and state['expires'] <= time.monotonic():
            state['client'].close()
            del self.states[owner]
            return None
        return state

    def _snapshot(self, owner):
        return self.repository.read(owner)

    def _save(self, owner, snapshot):
        self.repository.write(owner, snapshot)

    def connector_session(self, platform, owner):
        state = self._state(owner)
        if not state:
            raise CampusError('AUTH_REQUIRED', '请登录校园通行证')
        client = state['client']
        if platform == 'zdbk' and not state.get('connected'):
            raise CampusError('AUTH_REQUIRED', '请连接教务站点')
        if platform == 'eta' and not (getattr(client, 'passport_authenticated', False) or getattr(client, 'eta_authenticated', False)):
            raise CampusError('AUTH_EXPIRED', '请重新登录通行证')
        if platform not in ('zdbk', 'eta'):
            raise CampusError('UNSUPPORTED', '尚未注册此站点的会话认证流程')
        client.expected_identity = state.get('identity') or (self._snapshot(owner).get('profile') or {}).get('data', {}).get('student_id', '')
        return client

    def platform_status(self, platform, owner):
        state = self._state(owner)
        client = state['client'] if state else None
        connected = bool(client and (state.get('connected') if platform == 'zdbk'
            else getattr(client,'eta_authenticated',False) if platform == 'eta' else False))
        return {'connected':connected, 'can_connect':platform in ('zdbk','eta') and
                bool(client and getattr(client,'passport_authenticated',False))}

    def expire_platform(self, platform, owner):
        state = self._state(owner)
        if not state:
            return
        if platform not in ('zdbk', 'eta'):
            return
        client = state['client']
        host = {'zdbk':'zdbk.zju.edu.cn', 'eta':'eta.zju.edu.cn'}[platform]
        jar = getattr(client, 'jar', ())
        for cookie in list(jar):
            if cookie.domain.lstrip('.').lower() == host:
                jar.clear(cookie.domain, cookie.path, cookie.name)
        if platform == 'zdbk':
            state['connected'] = False
            state.pop('sync_job', None)
            client.authenticated = False
        else:
            client.eta_authenticated = False

    def status(self, owner):
        with self.lock(owner):
            state = self._state(owner)
            snapshot = self._snapshot(owner)
            # Saved snapshots are always labelled as previously fetched data.
            return {'connected': bool(state and state['connected']), 'snapshot': snapshot,
                    'connector_revision': CONNECTOR_REVISION,
                    'passport_connected': bool(state and getattr(state['client'], 'passport_authenticated', state.get('passport', False))),
                    'source': '本科教务网', 'cached': True,
                    'can_resume': bool(state and state['connected'] and state.get('sync_job') and state['sync_job']['pending'])}

    def academic_view(self, owner, day=None, term=None):
        from .academics import agenda, grade_summary
        from .connector_contracts import UserContext, CoursesQuery, GradesQuery
        with self.lock(owner):
            snapshot = self._snapshot(owner)
            context = UserContext(owner)
            courses = self.service.query('courses', CoursesQuery(), context)
            grades = self.service.query('grades', GradesQuery(), context)
            # Preserve term catalog/calendar semantics while reading records through the service.
            course_section = dict(snapshot.get('courses') or {})
            course_section.update(data=courses.records, complete=courses.completeness == 'complete',
                                  stale=courses.freshness != 'fresh', fetched_at=courses.fetched_at)
            known = {(t['year'],t['semester']):dict(t) for t in course_section.get('terms', [])}
            for row in courses.records:
                key = (row.get('year'),row.get('semester'))
                if all(key):
                    known.setdefault(key, {'year':key[0], 'semester':key[1], 'term':row.get('term','')})
            course_section['terms'] = []
            for (year,semester), term_record in known.items():
                result = self.service.query('courses', CoursesQuery(year=year,semester=semester), context)
                course_section['terms'].append(term_record | {'data':result.records,
                    'stale':result.freshness != 'fresh', 'fetched_at':result.fetched_at})
            grade_section = {'data':grades.records, 'complete':grades.completeness == 'complete',
                             'stale':grades.freshness != 'fresh', 'fetched_at':grades.fetched_at}
            calendars = {k: v | {'stale': v.get('stale', False) or bool(v.get('fetched_at') and
                (datetime.now(TZ)-datetime.fromisoformat(v['fetched_at'])).total_seconds() > 6*3600)}
                for k, v in snapshot.get('calendars', {}).items()}
            for record in (snapshot.get('courses') or {}).get('terms', []):
                resolved = resolve_calendar(record, calendars)
                if resolved:
                    calendars[record['year']+'/'+record['semester']] = resolved
            return {'agenda': agenda(course_section if courses.fetched_at else None, calendars, day, term),
                    'today_agenda': agenda(course_section if courses.fetched_at else None, calendars),
                    'grades': grade_summary(grade_section if grades.fetched_at else None)}

    def calendar(self, owner, year, semester, refresh=False):
        with self.lock(owner):
            snapshot = self._snapshot(owner)
            terms = (snapshot.get('courses') or {}).get('terms', [])
            term = next((t for t in terms if t['year'] == year and t['semester'] == semester), None)
            if not term:
                raise CampusError('TERM_UNAVAILABLE', '请先同步该学期课表')
            key = year+'/'+semester
            calendars = snapshot.setdefault('calendars', {})
            try:
                calendars[key] = self.calendar_client.fetch(year, calendar_family(term) or semester, force=refresh)
            except CampusError as exc:
                old = calendars.get(key) or {}
                resolved = resolve_calendar(term, calendars) or old
                calendars[key] = resolved | {'stale': bool(old.get('verified')), 'error': exc.public(),
                    'verified': bool(resolved.get('verified')), 'usable': bool(resolved.get('usable') or resolved.get('verified')),
                    'source_status': 'cache' if old.get('verified') else 'fallback' if resolved.get('seasons') else 'unavailable',
                    'attempted_at': datetime.now(TZ).isoformat()}
            self._save(owner, snapshot)
            record = calendars[key]
            return self.status(owner) | {'calendar_result': {'verified': record.get('verified', False),
                'term': key, 'error': record.get('error')}}

    def prepare(self, owner):
        with self.lock(owner):
            old = self.states.pop(owner, None)
            if old:
                old['client'].close()
            client = self.factory()
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
            self.states[owner] = {'client': client, 'flow': flow, 'expires': time.monotonic()+300, 'connected': False}
            return {'flow_id': flow, 'captcha': captcha}

    def login(self, owner, flow, username, password, authcode):
        with self.lock(owner):
            state = self._state(owner)
            if not state or state['connected'] or state['flow'] != flow:
                raise CampusError('FLOW_EXPIRED', '登录流程已过期，请重新准备登录')
            try:
                state['client'].start_operation()
                state['client'].login(username, password, authcode)
            except Exception:
                state['client'].close()
                self.states.pop(owner, None)
                raise
            state.update(connected=True, expires=time.monotonic()+3600, flow='')
            # A new passport may represent another student: never mix old snapshots.
            self.repository.delete(owner)
            self.capability_repository.delete(owner)
            return self.status(owner)

    def adopt_passport(self, owner, client):
        with self.lock(owner):
            self.disconnect(owner)
            self.states[owner] = {'client': client, 'passport': True, 'connected': False,
                                  'flow': '', 'expires': time.monotonic()+3600}

    def connect_and_sync(self, owner):
        with self.lock(owner):
            state = self._state(owner)
            if not state or not state.get('passport'):
                raise CampusError('SESSION_EXPIRED', '请重新登录通行证；已缓存资料仍可查看')
            state['connected'] = False
            try:
                state['client'].start_operation()
                state['client'].connect_zdbk()
                state['connected'] = True
            except CampusError as exc:
                snapshot = self._snapshot(owner)
                snapshot['warnings'] = [exc.public() | {'stage': 'service_login'}]
                for key in ('profile', 'courses', 'grades'):
                    if snapshot.get(key):
                        snapshot[key]['stale'] = True
                stale_profile(snapshot)
                if snapshot.get('courses'):
                    for term in snapshot['courses'].get('terms', []):
                        term['stale'] = True
                    for row in snapshot['courses']['data']:
                        row['stale'] = True
                snapshot['attempted_at'] = datetime.now(TZ).isoformat()
                snapshot['sync_revision'] = CONNECTOR_REVISION
                # ETA can remain available even when undergraduate services fail.
                if getattr(state['client'], 'passport_authenticated', False) or getattr(state['client'], 'eta_authenticated', False):
                    state['client'].start_operation()
                    refresh_profile(snapshot, state['client'], snapshot['warnings'])
                self._save(owner, snapshot)
                if exc.code == 'SESSION_EXPIRED':
                    self.expire_platform('zdbk', owner)
                return self.status(owner) | {'auto_continue': False}
            return self.sync(owner)

    def captcha(self, owner):
        with self.lock(owner):
            state = self._state(owner)
            if not state or not state['connected']:
                raise CampusError('SESSION_EXPIRED', '请先登录校园通行证')
            state['client'].start_operation()
            return {'captcha': state['client'].captcha('timetable')}

    def sync(self, owner, captcha='', resume=False):
        with self.lock(owner):
            state = self._state(owner)
            if not state or not state['connected']:
                raise CampusError('SESSION_EXPIRED', '校园会话已失效，请重新登录；已同步数据仍可查看')
            client = state['client']
            client.start_operation()
            snapshot = self._snapshot(owner)
            previous_grades = snapshot.get('grades')
            warnings = []
            fetched = datetime.now(TZ).isoformat()
            if resume and not state.get('sync_job'):
                raise CampusError('SYNC_NOT_RESUMABLE', '本次连接没有待续同步，请重新开始同步')
            if not resume:
                state.pop('sync_job', None)
                for key in ('profile', 'courses', 'grades'):
                    if snapshot.get(key):
                        snapshot[key]['stale'] = True
                stale_profile(snapshot)
                if snapshot.get('courses'):
                    for term in snapshot['courses'].get('terms', []):
                        term['stale'] = True
                    for row in snapshot['courses']['data']:
                        row['stale'] = True
                # Personal information has dedicated sources; timetable xsxx is
                # a fallback, rather than the sole source of college/major.
                refresh_profile(snapshot, client, warnings)
                # Grades have no academic-year filter and are independent of discovery.
                try:
                    grades, complete, total = client.grades()
                    old_grades = previous_grades or {}
                    if not grades and old_grades.get('data'):
                        snapshot['grades'] = old_grades | {'stale':True, 'complete':False,
                            'attempted_at':datetime.now(TZ).isoformat()}
                        warnings.append({'code':'EMPTY_GRADES_CACHE','stage':'grades',
                            'message':'成绩返回空列表，保留上次成绩与原获取时间'})
                    else:
                        if not complete:
                            identity = lambda r: (r.get('selection_id') or '',r.get('term'),r.get('code'),r.get('name'))
                            merged = {identity(r):r | {'stale':True,'fetched_at':r.get('fetched_at',old_grades.get('fetched_at'))}
                                      for r in old_grades.get('data', [])}
                            for row in grades:
                                key = identity(row)
                                if row.get('selection_id'):
                                    merged.pop(('',*key[1:]),None)
                                merged[key] = row
                            grades = list(merged.values())
                        snapshot['grades'] = {'data': grades, 'fetched_at': datetime.now(TZ).isoformat(),
                                              'stale': any(r.get('stale',False) for r in grades),
                                              'complete': complete, 'total': total}
                    if not complete:
                        warnings.append({'code': 'INCOMPLETE_GRADES', 'message': '成绩未获取完整，请以官方成绩单为准'})
                except CampusError as exc:
                    warnings.append(exc.public() | {'stage': 'grades'})
                try:
                    if any(w['code'] == 'SESSION_EXPIRED' for w in warnings):
                        raise CampusError('SESSION_EXPIRED', '教务会话已失效，保留缓存')
                    try:
                        terms = client.terms()
                    except CampusError as exc:
                        if exc.code not in ('TERM_DISCOVERY_FAILED', 'PROTOCOL_CHANGED', 'NETWORK_ERROR', 'UPSTREAM_ERROR'):
                            raise
                        terms = fallback_terms(snapshot, datetime.now(TZ).date())
                        client.term_warnings = [exc.public() | {'stage': 'terms'},
                            {'code': 'TERM_RANGE_FALLBACK', 'stage': 'terms',
                             'message': '学期列表不可用，已用缓存、已有成绩与默认学年范围继续查询真实课表；范围暂不标记完整',
                             'source_status': 'fallback'}]
                    catalog_warnings = list(getattr(client, 'term_warnings', []))
                    warnings.extend(catalog_warnings)
                    # Migrate a previous single-term snapshot without discarding its cache.
                    old = snapshot.get('courses') or {}
                    saved = old.get('terms', [])
                    if not saved and old.get('year'):
                        saved = [{'year': old['year'], 'semester': old['semester'],
                                  'term': old['year'] + ' · ' + old['semester'],
                                  'data': old['data'], 'fetched_at': old['fetched_at']}]
                    catalog=merge_term_catalog(terms,old.get('catalog',[]),saved)
                    remembered={(t['year'],t['semester']):t for t in catalog}
                    cached_only=[t for t in catalog if not t['available']]
                    if cached_only:
                        warnings.append({'code':'CACHED_TERM_RANGE','stage':'terms',
                            'message':f'本次学期列表未包含 {len(cached_only)} 个已知学期，已保留学期记录和已有课表缓存',
                            'source_status':'cache'})
                    for term in saved:
                        term.update(remembered[(term['year'],term['semester'])])
                        term['stale'] = True
                        term['source_status']='cache'
                    snapshot['courses'] = {'data': old.get('data', []), 'terms': saved,
                        'catalog':catalog,
                        'fetched_at': old.get('fetched_at', fetched), 'stale': True, 'complete': False,
                        'scope': '按降级范围查询的真实课表' if any(t.get('range_source') == 'fallback' for t in terms)
                                 else '教务课表页面可查询的学期', 'discovered_at': fetched}
                    state['sync_job'] = {'pending': [dict(t) for t in terms], 'total': len(terms), 'completed': 0, 'failed': [],
                                         'profile_seen': False, 'catalog_complete': not catalog_warnings and not cached_only}
                except CampusError as exc:
                    warnings.append(exc.public() | {'stage': 'terms'})
                    if snapshot.get('courses'):
                        snapshot['courses']['complete'] = False
            job = state.get('sync_job')
            if job:
                if resume:
                    warnings = list(job.get('warnings', []))
                else:
                    job['warnings'] = list(warnings)
            halted = any(w['code'] == 'SESSION_EXPIRED' for w in warnings)
            server_failures=0
            timeout_failures=0
            # Bounded batches preserve the request deadline. The UI resumes only after success.
            for _ in range(6):
                if not job or not job['pending'] or halted:
                    break
                term = job['pending'][0]
                try:
                    profile, courses = client.timetable(term.get('query_year', term['year']), term['semester'], captcha)
                    server_failures=0
                    timeout_failures=0
                    # A manual retry may resolve the custom response for this
                    # still-pending term; do not leave it as a current failure.
                    def unresolved(warning):
                        status=(warning.get('diagnostic') or {}).get('http_status',0)
                        return not (warning.get('stage') == 'timetable' and warning.get('term') == term['term'] and
                                    (warning.get('code') == 'UPSTREAM_STATUS_UNKNOWN' or isinstance(status,int) and status >= 600))
                    warnings=[w for w in warnings if unresolved(w)]
                    job['warnings']=[w for w in job['warnings'] if unresolved(w)]
                    row_warnings = list(getattr(client, 'timetable_warnings', []))
                    warnings.extend(w | {'stage': 'timetable', 'term': term['term']} for w in row_warnings)
                    job['warnings'].extend(w | {'stage': 'timetable', 'term': term['term']} for w in row_warnings)
                    if profile:
                        previous = (snapshot.get('profile') or {}).get('data') or {}
                        if previous.get('student_id') and previous['student_id'] != profile['student_id']:
                            raise CampusError('SESSION_EXPIRED', '教务响应的学生身份发生变化，请重新登录')
                        # Current/default term has priority; historical responses only fill gaps.
                        merge_profile(snapshot, {'data': profile, 'source': '本科教务网 · 课表基本信息', 'url': TIMETABLE},
                                      fill_only=job['profile_seen'], protect_fresh=True)
                        job['profile_seen'] = True
                    section = snapshot['courses']
                    key = (term['year'], term['semester'])
                    old_term = next((t for t in section['terms'] if (t['year'], t['semester']) == key), {})
                    empty_cache=not courses and bool(old_term.get('data'))
                    if empty_cache:
                        warning={'code':'EMPTY_TIMETABLE_CACHE','stage':'timetable','term':term['term'],
                                 'message':'本次返回空课表，继续显示上次成功获取的课表及原获取时间',
                                 'source_status':'cache'}
                        warnings.append(warning);job['warnings'].append(warning)
                        job['failed'].append(term)
                        old_term.update(term,stale=True,source_status='cache',attempted_at=datetime.now(TZ).isoformat())
                        # Empty responses may be temporary; they cannot withdraw known courses.
                        job['pending'].pop(0)
                        continue
                    if row_warnings:
                        # Keep older courses not present in this partial response as stale rows.
                        fingerprints = {(r.get('code'), r.get('name'), r.get('weekday'), r.get('periods'), r.get('start_period')) for r in courses}
                        courses = courses + [r | {'stale': True,'fetched_at':r.get('fetched_at',old_term.get('fetched_at'))} for r in old_term.get('data', []) if
                            (r.get('code'), r.get('name'), r.get('weekday'), r.get('periods'), r.get('start_period')) not in fingerprints]
                        job['failed'].append(term)
                    section['terms'] = [t for t in section['terms'] if (t['year'], t['semester']) != key]
                    section['terms'].append(term | {'data': courses, 'fetched_at': datetime.now(TZ).isoformat(),
                                                   'stale': False, 'partial': bool(row_warnings),
                                                   'source_status':'mixed' if row_warnings else 'live'})
                    job['pending'].pop(0)
                    job['completed'] += 1
                except CampusError as exc:
                    warnings.append(exc.public() | {'stage': 'timetable', 'term': term['term']})
                    status=(exc.diagnostic or {}).get('http_status',0)
                    if exc.code == 'UPSTREAM_STATUS_UNKNOWN' or isinstance(status,int) and status >= 600:
                        job['warnings'].append(exc.public() | {'stage':'timetable', 'term':term['term']})
                        warnings.append({'code':'CUSTOM_STATUS_PAUSED','stage':'timetable',
                            'message':'教务返回未识别的自定义响应，暂停自动续传；当前学期仍待查询，保留会话和缓存，可稍后手动继续'})
                        halted = True
                    elif exc.code in ('PROTOCOL_CHANGED', 'UPSTREAM_ERROR'):
                        job['warnings'].append(exc.public() | {'stage': 'timetable', 'term': term['term']})
                        job['failed'].append(job['pending'].pop(0))
                        server_failures=server_failures+1 if isinstance(status,int) and 500 <= status <= 599 else 0
                        if server_failures>=3:
                            warnings.append({'code':'SERVER_ERRORS_PAUSED','stage':'timetable',
                                'message':'教务服务连续三次返回服务器错误，已暂停自动续传；已有资料和待查询学期保留，可稍后继续'})
                            halted=True
                    elif exc.code=='NETWORK_ERROR' and (exc.diagnostic or {}).get('reason')=='TIMEOUT':
                        # One slow historical semester must not indefinitely block all later terms.
                        job['warnings'].append(exc.public()|{'stage':'timetable','term':term['term']})
                        job['failed'].append(job['pending'].pop(0))
                        timeout_failures+=1
                        if timeout_failures>=3:
                            warnings.append({'code':'TIMETABLE_TIMEOUTS_PAUSED','stage':'timetable',
                                'message':'课表连续三次请求超时，已暂停自动续传；保留缓存和待查询学期，可稍后继续'})
                            halted=True
                    else:
                        halted = True
            if job:
                section = snapshot['courses']
                section['terms'].sort(key=lambda t: (t['year'], semester_order(t['semester'])), reverse=True)
                section['data'] = [row | {k: term[k] for k in ('year', 'semester', 'term')} |
                                   {'stale': term['stale'] or row.get('stale', False), 'fetched_at': row.get('fetched_at', term['fetched_at'])}
                                   for term in section['terms'] for row in term['data']]
                section['complete'] = not job['pending'] and not job.get('failed') and job.get('catalog_complete', True)
                section['stale'] = any(t['stale'] for t in section['terms']) or bool(job['pending'])
                stamps=[t['fetched_at'] for t in section['terms'] if t.get('fetched_at')]
                section['fetched_at'] = max(stamps) if stamps else section.get('fetched_at')
                section['attempted_at'] = datetime.now(TZ).isoformat()
                snapshot['sync_progress'] = {'completed': job['completed'], 'total': job['total'],
                                             'pending': job['pending'], 'failed': job.get('failed', [])}
                if not job['pending'] and not snapshot.get('profile'):
                    warnings.append({'code': 'PROFILE_UNAVAILABLE', 'message': '课表响应未提供完整学生基本信息，未推断缺失字段'})
            else:
                snapshot.pop('sync_progress', None)
            if any(w['code'] == 'SESSION_EXPIRED' for w in warnings):
                # Discard grades fetched in this request if the later identity/session probe failed.
                snapshot['grades'] = previous_grades
                if previous_grades:
                    previous_grades['stale'] = True
                self.expire_platform('zdbk', owner)
            snapshot['attempted_at'] = fetched
            snapshot['sync_revision'] = CONNECTOR_REVISION
            snapshot['warnings'] = warnings
            self._save(owner, snapshot)
            return self.status(owner) | {'cached': False, 'auto_continue': bool(job and job['pending'] and not halted)}

    def disconnect(self, owner, delete=False):
        with self.lock(owner):
            state = self.states.pop(owner, None)
            if state:
                state['client'].close()
            if delete:
                self.repository.delete(owner)
                self.capability_repository.delete(owner)

    def disconnect_client(self, owner, client):
        """A cancelled flow cannot revoke a newer connection for the same owner."""
        with self.lock(owner):
            state = self._state(owner)
            if state and state['client'] is client:
                self.disconnect(owner)
            else:
                client.close()

    def close(self):
        for owner in list(self.states):
            self.disconnect(owner)
