"""Server-owned capability selection, authorization, synchronization and cache policy."""
from dataclasses import asdict
from datetime import datetime
import json
import threading
import time

from pydantic import ValidationError
from .core import TZ
from .campus_common import CampusError
from .campus_repository import SQLiteCacheRepository
from .connector_contracts import (UserContext, QueryResult, CoursesQuery, GradesQuery,
    ProfileQuery, NoticesQuery, Course, Grade, Profile, Notice, standard_error)
from .connectors.zdbk import ZdbkAdapter
from .connectors.eta import EtaAdapter
from .connectors.notices import PublicNoticesAdapter
from .campus_endpoints import TIMETABLE, GRADES

CONTRACTS = {'courses': (CoursesQuery, Course), 'grades': (GradesQuery, Grade),
             'profile': (ProfileQuery, Profile), 'notices': (NoticesQuery, Notice)}


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
        self.registry = registry or ConnectorRegistry((ZdbkAdapter(), EtaAdapter(), PublicNoticesAdapter()))
        self.guard = threading.Lock()
        self.attempts = {}

    def _resolve(self, capability, query, context, platform):
        if not isinstance(context, UserContext):
            raise CampusError('FORBIDDEN', '需要服务端身份上下文')
        adapter = self.registry.select(capability, platform)
        if not set(adapter.spec.permissions) <= context.permissions:
            raise CampusError('FORBIDDEN', '缺少读取权限')
        query_type, record_type = CONTRACTS[capability]
        # Reject structurally similar query types: no owner, URL, cookies or cursor inputs.
        if isinstance(query, dict):
            query = query_type.model_validate(query)
        if type(query) is not query_type:
            raise ValueError('Query type does not match capability')
        scope = json.dumps(query.model_dump(), sort_keys=True, ensure_ascii=False)
        return adapter, query, record_type, scope

    def _blank(self, capability, platform):
        safe_capability = capability if capability in CONTRACTS else 'unknown'
        if platform in self.registry.adapters:
            safe_platform = platform
        elif platform is None:
            safe_platform = self.registry.defaults.get(safe_capability, 'unknown')
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

    def query(self, capability, query, context, *, platform=None):
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
                  'grades': ('selection_id','term','code','name'), 'profile': ('student_id',), 'notices': ('id',)}[capability]
        return tuple(record.get(k, '') for k in fields)

    def sync(self, capability, query, context, *, platform=None):
        try:
            adapter, query, record_type, scope = self._resolve(capability, query, context, platform)
        except CampusError as exc:
            result = self._blank(capability, platform)
            result.error = standard_error(exc)
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
                if not empty_retained:
                    result.fetched_at = result.last_attempt_at
                result.completeness = 'partial' if empty_retained else incoming.completeness
                result.freshness = 'stale' if empty_retained or any(r.get('stale') or
                    any(v.get('stale') for v in r.get('field_sources',{}).values()) for r in rows) else 'fresh'
                result.warnings, result.error, result.next_cursor = warnings, None, None
            except (CampusError, ValidationError, ValueError, TypeError) as exc:
                result.error = standard_error(exc)
                result.freshness = 'stale' if result.fetched_at else 'unknown'
                if result.error.code.value == 'AUTH_EXPIRED':
                    self.sessions.expire_platform(adapter.spec.platform_id, context.owner)
            self.repository.write(context.owner, adapter.spec.platform_id, capability, scope, result.model_dump(mode='json'))
            return result

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
