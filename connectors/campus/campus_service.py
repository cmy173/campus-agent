"""Server-owned capability selection, authorization, synchronization and cache policy."""
from dataclasses import asdict
from datetime import datetime
import json
import re
import threading
import time

from pydantic import ValidationError
from .timezone import TZ
from .campus_common import CampusError
from .campus_repository import SQLiteCacheRepository
from .connector_contracts import (UserContext, QueryResult, CoursesQuery, GradesQuery,
    ProfileQuery, NoticesQuery, CollegeNoticesQuery, CollegeNotice, CollegeSelection,
    Course, Grade, Profile, Notice, standard_error)
from .connectors.zdbk import ZdbkAdapter
from .connectors.eta import EtaAdapter
from .connectors.notices import PublicNoticesAdapter
from .connectors.college_notices import CkcNoticesAdapter
from .campus_college_sources import normalize_college
from .campus_endpoints import TIMETABLE, GRADES

CONTRACTS = {'courses': (CoursesQuery, Course), 'grades': (GradesQuery, Grade),
             'profile': (ProfileQuery, Profile), 'notices': (NoticesQuery, Notice),
             'college_notices': (CollegeNoticesQuery, CollegeNotice)}

# Mirrors CollegeNotice.id; a client may only name a record it was already shown.
RECORD_ID = r'[a-z][a-z0-9_]{0,31}:\d{1,12}'


class ConnectorRegistry:
    def __init__(self, adapters=()):
        self.adapters, self.defaults = {}, {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter):
        spec = adapter.spec
        if spec.platform_id in self.adapters or not spec.read_only:
            raise ValueError('Duplicate platform or unsupported writable connector')
        for capability in spec.capabilities:
            if capability not in CONTRACTS:
                raise ValueError('Register an explicit query and record contract first')
        self.adapters[spec.platform_id] = adapter
        for capability in spec.capabilities:
            self.defaults.setdefault(capability, spec.platform_id)

    def select(self, capability, platform=None):
        adapter = self.adapters.get(self.defaults.get(capability) if platform is None else platform)
        if not adapter or capability not in adapter.spec.capabilities:
            raise CampusError('UNSUPPORTED', '未知平台或能力')
        return adapter

    def describe(self):
        return [asdict(adapter.spec) for adapter in self.adapters.values()]


class CampusService:
    def __init__(self, sessions, repository=None, registry=None):
        self.sessions = sessions
        self.snapshots = sessions.repository
        self.repository = repository or SQLiteCacheRepository(sessions.store)
        self.registry = registry or ConnectorRegistry((ZdbkAdapter(), EtaAdapter(), PublicNoticesAdapter(), CkcNoticesAdapter()))
        self.guard = threading.Lock()
        self.attempts = {}

    def _resolve(self, capability, query, context, platform):
        if not isinstance(context, UserContext):
            raise CampusError('FORBIDDEN', '需要服务端身份上下文')
        if 'campus.read' not in context.permissions:
            raise CampusError('FORBIDDEN', '缺少读取权限')
        selection = None
        if capability == 'college_notices':
            selection, selected_platform = self._college_selection(context)
            if selection.status != 'matched' or (platform is not None and platform != selected_platform):
                raise CampusError('UNSUPPORTED', '账号学院尚无可确认的通知来源')
            platform = selected_platform
        adapter = self.registry.select(capability, platform)
        if not set(adapter.spec.permissions) <= context.permissions:
            raise CampusError('FORBIDDEN', '缺少读取权限')
        query_type, record_type = CONTRACTS[capability]
        # Reject structurally similar query types: no owner, URL, cookies or cursor inputs.
        if isinstance(query, dict):
            query = query_type.model_validate(query)
        if type(query) is not query_type:
            raise ValueError('Query type does not match capability')
        scope_data = query.model_dump()
        if selection is not None:
            scope_data['college'] = normalize_college(selection.college)
        scope = json.dumps(scope_data, sort_keys=True, ensure_ascii=False)
        return adapter, query, record_type, scope

    def _college_selection(self, context):
        """Only the authenticated owner's server-stored college field selects a source.

        Prefer newer field evidence, including an explicit newer empty college.
        The legacy merged profile can include a verified ZDBK field absent from ETA.
        """
        section = self.snapshots.read(context.owner).get('profile') or {}
        source = section.get('field_sources', {}).get('college', {})
        candidates = []
        if section:
            candidates.append((source.get('fetched_at') or section.get('fetched_at') or '',
                section.get('data', {}).get('college', ''), source.get('stale', section.get('stale', True))))
        cached = self.repository.read(context.owner, 'eta', 'profile', '{}')
        if cached:
            for row in cached.get('records', []):
                evidence = row.get('field_sources', {}).get('college', {})
                # An explicit empty field invalidates older profile evidence too.
                stamp = evidence.get('fetched_at') or row.get('fetched_at') or cached.get('fetched_at') or ''
                candidates.append((stamp, row.get('college', ''), evidence.get('stale', cached.get('freshness') != 'fresh')))
        if not candidates:
            return CollegeSelection(), None
        latest = max(c[0] for c in candidates)
        choices = [c for c in candidates if c[0] == latest]
        values = {normalize_college(c[1]) for c in choices}
        if len(values) != 1:
            return CollegeSelection(status='ambiguous', profile_fetched_at=latest or None), None
        college = values.pop()
        stale = any(c[2] for c in choices)
        try:
            stale = stale or (datetime.now(TZ)-datetime.fromisoformat(latest)).total_seconds() > 6*3600
        except (ValueError, TypeError):
            stale = True
        selection = CollegeSelection(college=college, status='unsupported' if college else 'missing',
            profile_fetched_at=latest or None, profile_stale=stale)
        matches = [a.spec.platform_id for a in self.registry.adapters.values()
            if 'college_notices' in a.spec.capabilities and college and
            college in {normalize_college(n) for n in a.spec.college_names}]
        if len(matches) == 1:
            selection.status = 'matched'
            return selection, matches[0]
        if len(matches) > 1:
            selection.status = 'ambiguous'
        return selection, None

    def _run(self, method, capability, query, context, platform):
        if not isinstance(context, UserContext) or 'campus.read' not in context.permissions:
            return method(capability, query, context, platform=platform)
        # Bind profile selection and the selected cache scope under the same owner lock.
        with self.sessions.lock(context.owner):
            result = method(capability, query, context, platform=platform)
            if capability == 'college_notices':
                result.selection, _ = self._college_selection(context)
            return result

    def query(self, capability, query, context, *, platform=None):
        return self._run(self._query_locked, capability, query, context, platform)

    def sync(self, capability, query, context, *, platform=None):
        return self._run(self._sync_locked, capability, query, context, platform)

    def _blank(self, capability, platform):
        safe_capability = capability if capability in CONTRACTS else 'unknown'
        if platform in self.registry.adapters:
            safe_platform = platform
        elif platform is None:
            safe_platform = 'unknown' if capability == 'college_notices' else self.registry.defaults.get(safe_capability, 'unknown')
        else:
            safe_platform = 'unknown'
        return QueryResult(source=safe_capability, platform=safe_platform)

    @staticmethod
    def _legacy_error(snapshot, stages):
        errors = {'SESSION_EXPIRED','PROFILE_SESSION_EXPIRED','NETWORK_ERROR','ACCESS_REJECTED',
                  'UPSTREAM_ERROR','PROTOCOL_CHANGED','ETA_PROTOCOL_CHANGED','ETA_UNAVAILABLE',
                  'ETA_ACCESS_REJECTED','CAPTCHA_REQUIRED','OPERATION_TIMEOUT','RATE_LIMITED',
                  'SERVICE_LOGIN_FAILED','SERVICE_ACCESS_REJECTED','TERM_DISCOVERY_FAILED','UPSTREAM_STATUS_UNKNOWN'}
        for warning in reversed(snapshot.get('warnings', [])):
            if warning.get('stage') in stages and warning.get('code') in errors:
                return standard_error(CampusError(warning['code'], '', warning.get('diagnostic')))
        return None

    def _legacy(self, capability, query, context, platform):
        """Read existing snapshots without destructive data migration or network calls."""
        snapshot = self.snapshots.read(context.owner)
        if platform == 'zdbk' and capability in ('courses', 'grades'):
            section = snapshot.get(capability)
            if capability == 'courses' and query.year is not None:
                saved = section or {}
                section = next((t for t in saved.get('terms', [])
                    if t['year'] == query.year and t['semester'] == query.semester), None)
                if section is None and (saved.get('year'),saved.get('semester')) == (query.year,query.semester):
                    section = saved
            if section is None:
                return None
            complete = section.get('complete', not section.get('partial', False) if capability == 'courses' and query.year is not None else False)
            records = [r | {'source':TIMETABLE if capability == 'courses' else GRADES.split('?')[0],
                            'fetched_at':r.get('fetched_at') or section.get('fetched_at')} for r in section.get('data', [])]
            if capability == 'courses' and query.year is not None:
                records = [r | {'year':query.year,'semester':query.semester,'term':section.get('term',r.get('term',''))} for r in records]
            return QueryResult(source=capability, platform=platform, records=records,
                fetched_at=section.get('fetched_at'), last_attempt_at=section.get('attempted_at', snapshot.get('attempted_at')),
                freshness='stale' if section.get('stale', True) else 'fresh',
                completeness='complete' if complete else 'partial',
                error=self._legacy_error(snapshot, ('timetable','terms','service_login') if capability == 'courses' else ('grades','service_login')))
        # The merged profile cannot be attributed to ETA as a whole. Keep only ETA fields.
        if platform == 'eta' and capability == 'profile':
            section = snapshot.get('profile') or {}
            sources = section.get('field_sources', {})
            data = {key: value for key, value in section.get('data', {}).items()
                    if sources.get(key, {}).get('source', '').startswith('ETA')}
            if not data:
                return None
            available = tuple(data)
            stamps = [sources[key].get('fetched_at') for key in data if sources[key].get('fetched_at')]
            data['field_sources'] = {k:{'source':sources[k].get('url',''),
                'fetched_at':sources[k].get('fetched_at'),'stale':sources[k].get('stale',True)} for k in data}
            return QueryResult(source=capability, platform=platform, records=[data],
                fetched_at=max(stamps) if stamps else None, last_attempt_at=snapshot.get('attempted_at'),
                freshness='stale' if any(sources[k].get('stale', True) for k in available) else 'fresh',
                completeness='complete' if all(data.get(k) for k in ('student_id','name','college','major','class_name','year')) else 'partial',
                error=self._legacy_error(snapshot, ('profile_eta',)))
        return None

    def _read(self, capability, query, context, adapter, scope):
        stored = self.repository.read(context.owner, adapter.spec.platform_id, capability, scope)
        result = QueryResult.model_validate(stored) if stored else self._blank(capability, adapter.spec.platform_id)
        legacy = self._legacy(capability, query, context, adapter.spec.platform_id)
        if legacy and (not result.fetched_at or (legacy.fetched_at or '') > result.fetched_at):
            result = legacy
        # Aggregate only known scopes; this local composition performs no discovery/request.
        if capability == 'courses' and query.year is None:
            terms = {(t['year'], t['semester']) for t in (self.snapshots.read(context.owner).get('courses') or {}).get('terms', [])} if adapter.spec.platform_id == 'zdbk' else set()
            if hasattr(self.repository, 'scopes'):
                for cached_scope in self.repository.scopes(context.owner, adapter.spec.platform_id, capability):
                    term_query = CoursesQuery.model_validate_json(cached_scope)
                    if term_query.year is not None:
                        terms.add((term_query.year, term_query.semester))
            rows = list(result.records)
            for year, semester in sorted(terms):
                term_query = CoursesQuery(year=year, semester=semester)
                term_scope = json.dumps(term_query.model_dump(), sort_keys=True, ensure_ascii=False)
                section = self._read(capability, term_query, context, adapter, term_scope)
                rows = [r for r in rows if (r.get('year'),r.get('semester')) != (year,semester)]
                rows.extend(r | {'year':year,'semester':semester,'fetched_at':r.get('fetched_at') or section.fetched_at,
                                'stale':r.get('stale',False) or section.freshness != 'fresh'} for r in section.records)
                if (section.fetched_at or '') > (result.fetched_at or ''):
                    result.fetched_at = section.fetched_at
                if (section.last_attempt_at or '') > (result.last_attempt_at or ''):
                    result.last_attempt_at = section.last_attempt_at
                if section.error:
                    result.error = section.error
                if section.freshness != 'fresh':
                    result.freshness = 'stale'
                elif result.freshness == 'unknown':
                    result.freshness = 'fresh'
                if section.completeness != 'complete':
                    result.completeness = 'partial'
            result.records = rows
        if result.fetched_at:
            try:
                age = (datetime.now(TZ)-datetime.fromisoformat(result.fetched_at)).total_seconds()
                if age > 6*3600:
                    result.freshness = 'stale'
            except (ValueError, TypeError):
                result.freshness = 'unknown'
        return result

    def _query_locked(self, capability, query, context, *, platform=None):
        try:
            adapter, query, _, scope = self._resolve(capability, query, context, platform)
        except CampusError as exc:
            result = self._blank(capability, platform)
            result.error = standard_error(exc)
            return result
        with self.sessions.lock(context.owner):
            return self._read(capability, query, context, adapter, scope)

    def _throttle(self, platform):
        # Serialize quota allocation across owners; no automatic retry or channel switching.
        with self.guard:
            now = time.monotonic()
            recent = [t for t in self.attempts.get(platform, []) if t > now-60]
            if len(recent) >= 30:
                raise CampusError('RATE_LIMITED', '平台读取频率受限')
            self.attempts[platform] = recent+[now]

    @staticmethod
    def _identity(capability, record):
        fields = {'courses': ('year','semester','code','name','weekday','periods','start_period','weeks','season','location'),
                  'grades': ('selection_id','term','code','name'), 'profile': ('student_id',),
                  'notices': ('id',), 'college_notices': ('id',)}[capability]
        return tuple(record.get(k, '') for k in fields)

    def _sync_locked(self, capability, query, context, *, platform=None):
        try:
            adapter, query, record_type, scope = self._resolve(capability, query, context, platform)
        except CampusError as exc:
            # A sync that cannot resolve its platform/capability must not erase known
            # cache: this used to persist a blank row over the last good records. Report
            # the error and keep the previously cached data (labelled stale) both in this
            # response and in storage (nothing is written here).
            result = self._blank(capability, platform)
            # Exception: when the owner's college selection itself is no longer matched
            # (college changed to an unsupported/unknown one), the previously cached rows
            # belong to a source we must not serve any more. Returning them would be a
            # silent fallback to stale data for the wrong scope, so we return none.
            if capability == 'college_notices':
                result.error = standard_error(exc)
                result.freshness = 'unknown'
                return result
            previous = (self.repository.read_latest(context.owner, capability)
                        if hasattr(self.repository, 'read_latest') else None)
            if previous and previous.get('records'):
                result.records = previous.get('records')
                result.fetched_at = previous.get('fetched_at')
                result.completeness = previous.get('completeness') or 'partial'
                result.warnings = list(previous.get('warnings') or []) + [{
                    'code': 'CACHE_RETAINED',
                    'message': '本轮同步未能解析平台/能力；保留上次缓存数据（可能过期），不代表学校端当前状态'}]
            result.error = standard_error(exc)
            result.freshness = 'stale' if result.fetched_at else 'unknown'
            return result
        with self.sessions.lock(context.owner):
            result = self._read(capability, query, context, adapter, scope)
            result.last_attempt_at = datetime.now(TZ).isoformat()
            try:
                session = None if adapter.spec.authentication == 'public' else self.sessions.connector_session(adapter.spec.platform_id, context.owner)
                self._throttle(adapter.spec.platform_id)
                if session is not None:
                    session.start_operation()
                incoming = adapter.fetch(capability, query, session)
                rows = [record_type.model_validate(row).model_dump() | {'fetched_at': result.last_attempt_at}
                        for row in incoming.records]
                if capability == 'profile':
                    for row in rows:
                        for source in row['field_sources'].values():
                            source['fetched_at'] = result.last_attempt_at
                if incoming.completeness not in ('complete', 'partial', 'unknown'):
                    raise CampusError('PROTOCOL_CHANGED', '无效完整性标记')
                warning_messages = {'PARTIAL':'部分条目未确认，已保留缓存',
                    'RECENT_COLLEGE_NOTICES':'仅同步学院通知首页中的有限条目；附件、图片内容未解析，正文可能不包含全部要求',
                    'LIMITED_SCOPE':'仅公开登录页近期/置顶通知摘要；标题可能截断，未读取正文或历史列表'}
                warnings = [{'code': w.get('code','PARTIAL') if w.get('code') in warning_messages else 'PARTIAL',
                             'message': warning_messages.get(w.get('code'),warning_messages['PARTIAL'])}
                            for w in incoming.warnings or []]
                empty_retained = not rows and bool(result.records)
                if empty_retained:
                    warnings.append({'code':'EMPTY_CACHE_RETAINED', 'message':'空响应未撤销已知数据；保留上次获取时间'})
                if incoming.completeness != 'complete' or empty_retained:
                    merged = {self._identity(capability,r): r | {'stale':True} for r in result.records}
                    for row in rows:
                        identity = self._identity(capability, row)
                        if capability == 'grades' and row.get('selection_id'):
                            # Upgrade older cached rows that predate preservation of xkkh.
                            merged.pop(('', *identity[1:]), None)
                        # Missing profile fields must not overwrite established values.
                        if capability == 'profile' and identity in merged:
                            previous = merged[identity]
                            old_sources = {k:v | {'stale':True} for k,v in previous.get('field_sources',{}).items()}
                            new_sources = row.get('field_sources', {})
                            for key in incoming.invalidated_fields:
                                previous[key] = ''
                                old_sources.pop(key,None)
                            row = previous | {k:v for k,v in row.items() if v not in ('',None)}
                            row['field_sources'] = old_sources | new_sources
                        merged[identity] = row
                    rows = list(merged.values())
                else:
                    rows = list({self._identity(capability,r):r for r in rows}.values())
                result.records = rows
                if not empty_retained and (incoming.records or incoming.error is None):
                    result.fetched_at = result.last_attempt_at
                result.completeness = 'partial' if empty_retained else incoming.completeness
                result.freshness = 'stale' if empty_retained or any(r.get('stale') or
                    any(v.get('stale') for v in r.get('field_sources',{}).values()) for r in rows) else 'fresh'
                result.warnings, result.error, result.next_cursor = warnings, standard_error(incoming.error) if incoming.error else None, None
                if incoming.error:
                    result.freshness = 'stale' if result.fetched_at else 'unknown'
            except (CampusError, ValidationError, ValueError, TypeError) as exc:
                result.error = standard_error(exc)
                result.freshness = 'stale' if result.fetched_at else 'unknown'
                if result.error.code.value == 'AUTH_EXPIRED':
                    self.sessions.expire_platform(adapter.spec.platform_id, context.owner)
            if result.error is not None and not result.records:
                # A failed sync whose scope/cache lookup missed must not replace known
                # data with a blank row: keep the newest cached records, labelled stale.
                previous = (self.repository.read_latest(context.owner, capability)
                            if hasattr(self.repository, 'read_latest') else None)
                if previous and previous.get('records'):
                    result.records = previous['records']
                    result.fetched_at = result.fetched_at or previous.get('fetched_at')
                    result.completeness = 'partial'
                    result.freshness = 'stale'
                    result.warnings = list(result.warnings or []) + [{
                        'code': 'CACHE_RETAINED',
                        'message': '本轮同步未取得数据；保留上次缓存数据（可能过期），不代表学校端当前状态'}]
            self.repository.write(context.owner, adapter.spec.platform_id, capability, scope, result.model_dump(mode='json'))
            return result

    def cached_college_notice(self, context, record_id):
        """Build a notice import from this account's own validated cache only.

        The caller supplies an opaque record ID; origin URL, attachments, title and
        publication date always come from a re-validated cached record, never from the
        request. Returns None when the account has no such cached notice.
        """
        if not isinstance(context, UserContext) or 'campus.read' not in context.permissions:
            raise CampusError('FORBIDDEN', '需要服务端身份上下文')
        if not isinstance(record_id, str) or not re.fullmatch(RECORD_ID, record_id):
            raise CampusError('UNSUPPORTED', '通知记录标识无效')
        with self.sessions.lock(context.owner):
            for platform, adapter in self.registry.adapters.items():
                if 'college_notices' not in adapter.spec.capabilities:
                    continue
                for scope in self.repository.scopes(context.owner, platform, 'college_notices'):
                    cached = self.repository.read(context.owner, platform, 'college_notices', scope)
                    for row in (cached or {}).get('records', []):
                        if row.get('id') != record_id:
                            continue
                        try:
                            return platform, CollegeNotice.model_validate(row)
                        except ValidationError:
                            raise CampusError('PROTOCOL_CHANGED', '缓存通知记录无法确认') from None
        return None

    def connection_status(self, context):
        if not isinstance(context, UserContext) or 'campus.read' not in context.permissions:
            raise CampusError('FORBIDDEN', '需要服务端身份上下文')
        with self.sessions.lock(context.owner):
            return {'connectors': [spec | {'session_required':spec['authentication'] != 'public', 'configured':True} |
                ({'connected':False,'can_connect':False} if spec['authentication'] == 'public'
                 else self.sessions.platform_status(spec['platform_id'],context.owner))
                for spec in self.registry.describe()]}

    def disconnect(self, platform, context):
        if not isinstance(context, UserContext) or 'campus.read' not in context.permissions:
            raise CampusError('FORBIDDEN', '需要服务端身份上下文')
        adapter = self.registry.adapters.get(platform)
        if adapter is None:
            raise CampusError('UNSUPPORTED', '未知平台')
        with self.sessions.lock(context.owner):
            self.sessions.expire_platform(platform, context.owner)
        return self.connection_status(context)
