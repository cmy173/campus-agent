"""Synthetic CAS/ZDBK responses only; never require a real student password."""
from contextlib import ExitStack
from datetime import datetime
from http.cookiejar import Cookie
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit
import json
import http.client
import socket
import ssl
import time
import unittest
import urllib.error

from fastapi.testclient import TestClient
from campus_agent.campus import (CampusClient, CampusError, CAS, BASE, MENU, SERVICE, GRADES as GRADES_URL, TIMETABLE_INDEX,
                                  check_url, encrypt_password, normalize_course, normalize_profile)
from campus_agent.web import create_app


PROFILE = {'student_id': 'SYNTHETIC-STUDENT', 'name': '合成学生', 'college': '测试学院',
           'major': '测试专业', 'class_name': '演示班', 'year': '2026'}
COURSES = [{'name': '合成课程', 'code': 'DEMO', 'teacher': '测试教师', 'location': '测试教室',
            'weekday': '1', 'periods': '1-2', 'weeks': '1-16', 'season': '秋',
            'start_period': '', 'duration': '', 'odd_even': ''}]
GRADES = [{'name': '合成课程', 'code': 'DEMO', 'term': '2025-2026-1',
           'score': '优秀', 'credits': '0', 'gpa': '0'}]


class FakeCampus:
    def __init__(self):
        self.closed = False
        self.error = None
        self.complete = True
        self.catalog = [{'year': '2026', 'semester': '3', 'term': '2026-2027 · 秋冬', 'current': True},
                        {'year': '2025', 'semester': '12', 'term': '2025-2026 · 春夏', 'current': False}]
        self.queried = []
        self.empty = set()
        self.discovery_error = None

    def start_operation(self): pass
    def prepare(self): pass
    def captcha(self, kind): return 'data:image/png;base64,c3ludGhldGlj'
    def close(self): self.closed = True
    def login(self, username, password, authcode):
        if password == 'reject-me':
            raise CampusError('CAS_REJECTED', '校园认证未通过')
    def terms(self):
        if self.discovery_error:
            raise self.discovery_error
        return [dict(t) for t in self.catalog]
    def timetable(self, *args):
        self.queried.append(args[:2])
        if self.error:
            raise self.error
        return PROFILE, [] if args[:2] in self.empty else COURSES
    def grades(self): return GRADES, self.complete, 1 if self.complete else 2


class CampusWebTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        clock = self.stack.enter_context(patch('campus_agent.campus.datetime', wraps=datetime))
        clock.now.return_value = datetime.fromisoformat('2026-10-09T12:00:00+08:00')
        self.tmp = self.stack.enter_context(TemporaryDirectory())
        self.app = create_app(self.tmp)
        self.manager = self.app.state.campus
        self.manager.factory = FakeCampus
        self.client = self.stack.enter_context(TestClient(self.app, base_url='http://127.0.0.1'))
        self.client.headers['X-Campus-Request'] = '1'
        self.register('alice')

    def tearDown(self): self.stack.close()

    def register(self, username):
        r = self.client.post('/api/auth/register', json={'username': username, 'password': 'local-test-pass'})
        self.assertEqual(r.status_code, 200)
        self.client.headers['X-CSRF-Token'] = r.json()['csrf']

    def connect(self):
        r = self.client.post('/api/campus/prepare', json={'consent': True})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post('/api/campus/login', json={'flow_id': r.json()['flow_id'], 'username': 'synthetic',
            'password': 'campus-secret-test', 'authcode': '', 'consent': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['connected'])

    def sync(self):
        return self.client.post('/api/campus/sync', json={})

    def test_connect_sync_logout_and_cached_snapshot(self):
        self.connect()
        r = self.sync()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['snapshot']['profile']['data']['name'], PROFILE['name'])
        self.assertEqual({c['year'] for c in r.json()['snapshot']['courses']['data']}, {'2026', '2025'})
        self.assertTrue(r.json()['snapshot']['courses']['complete'])
        self.assertEqual(r.json()['snapshot']['grades']['data'][0]['credits'], '0')
        state = next(iter(self.manager.states.values()))
        self.client.post('/api/auth/logout')
        self.assertTrue(state['client'].closed)
        r = self.client.post('/api/auth/login', json={'username': 'alice', 'password': 'local-test-pass'})
        self.client.headers['X-CSRF-Token'] = r.json()['csrf']
        r = self.client.get('/api/campus').json()
        self.assertFalse(r['connected'])
        self.assertTrue(r['cached'])
        self.assertIsNotNone(r['snapshot']['grades'])

    def test_owner_isolation_flow_and_clear_data(self):
        self.connect()
        self.sync()
        alice_state = next(iter(self.manager.states.values()))
        self.register('bob')
        r = self.client.get('/api/campus').json()
        self.assertFalse(r['connected'])
        self.assertIsNone(r['snapshot']['profile'])
        r = self.client.post('/api/campus/login', json={'flow_id': 'a'*64, 'username': 'synthetic',
            'password': 'campus-secret-test', 'consent': True})
        self.assertEqual(r.status_code, 409)
        self.client.delete('/api/campus')
        self.assertFalse(alice_state['client'].closed)
        r = self.client.post('/api/auth/login', json={'username': 'alice', 'password': 'local-test-pass'})
        self.client.headers['X-CSRF-Token'] = r.json()['csrf']
        self.client.delete('/api/data')
        self.assertTrue(alice_state['client'].closed)
        self.assertIsNone(self.client.get('/api/campus').json()['snapshot']['grades'])

    def test_consent_csrf_and_no_secrets_in_storage_or_audit(self):
        r = self.client.post('/api/campus/prepare', json={'consent': False})
        self.assertEqual(r.status_code, 400)
        for path in ('/api/campus/prepare', '/api/campus/login', '/api/campus/sync', '/api/campus/captcha'):
            r = self.client.post(path, json={}, headers={'X-CSRF-Token': 'wrong'})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.delete('/api/campus', headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
        self.connect()
        self.sync()
        with self.app.state.store.connect() as db:
            saved = '\n'.join(db.iterdump())
        for secret in ('campus-secret-test', 'iPlanetDirectoryPro', 'flow_id', 'execution', 'authcode'):
            self.assertNotIn(secret, saved)
        audit = self.client.get('/api/audit').text
        self.assertNotIn(PROFILE['student_id'], audit)
        self.assertNotIn(PROFILE['name'], audit)

    def test_partial_failure_expiry_and_captcha_do_not_fake_success(self):
        self.connect()
        self.sync()
        state = next(iter(self.manager.states.values()))
        state['client'].error = CampusError('CAPTCHA_REQUIRED', '需要验证码')
        state['client'].complete = False
        result = self.sync().json()
        self.assertTrue(result['snapshot']['courses']['stale'])
        self.assertFalse(result['snapshot']['grades']['complete'])
        self.assertEqual({w['code'] for w in result['snapshot']['warnings']}, {'CAPTCHA_REQUIRED', 'INCOMPLETE_GRADES'})
        self.assertEqual(self.client.post('/api/campus/captcha').status_code, 200)
        state['client'].error = CampusError('SESSION_EXPIRED', '会话失效')
        result = self.sync().json()
        self.assertFalse(result['connected'])
        self.assertTrue(result['snapshot']['grades']['stale'])
        self.assertEqual(self.client.get('/api/me').status_code, 200)

    def test_disconnect_and_new_login_remove_old_student_data(self):
        self.connect()
        self.sync()
        self.client.delete('/api/campus')
        self.assertFalse(self.manager.states)
        self.assertIsNone(self.client.get('/api/campus').json()['snapshot']['profile'])
        self.connect()
        self.sync()
        self.connect()
        self.assertIsNone(self.client.get('/api/campus').json()['snapshot']['grades'])

    def test_expired_flow_input_validation_and_login_throttle(self):
        prepared = self.client.post('/api/campus/prepare', json={'consent': True}).json()
        state = next(iter(self.manager.states.values()))
        state['expires'] = time.monotonic()-1
        r = self.client.post('/api/campus/login', json={'flow_id': prepared['flow_id'], 'username': 'synthetic',
            'password': 'campus-secret-test', 'consent': True})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(state['client'].closed)
        for body in ({'year': '2026', 'semester': '1'}, {'year': {}, 'semester': '3'}, {'year': '2026', 'semester': []}):
            self.assertEqual(self.client.post('/api/campus/sync', json=body).status_code, 400)
        body = {'flow_id': 'a'*64, 'username': 'synthetic', 'password': 'test', 'consent': True}
        for _ in range(2):
            self.assertEqual(self.client.post('/api/campus/login', json=body).status_code, 409)
        self.assertEqual(self.client.post('/api/campus/login', json=body).status_code, 429)

    def test_all_terms_batched_resume_and_empty_terms_are_preserved(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        fake.catalog = [{'year': str(2026-i), 'semester': '3', 'term': str(2026-i)+' · 秋冬', 'current': i==0}
                        for i in range(8)]
        fake.empty.add(('2025', '3'))
        first = self.sync().json()
        self.assertTrue(first['auto_continue'])
        self.assertFalse(first['snapshot']['courses']['complete'])
        self.assertEqual(first['snapshot']['sync_progress']['completed'], 6)
        with patch.object(fake, 'terms', side_effect=AssertionError('resume must not rediscover')):
            second = self.client.post('/api/campus/sync', json={'resume': True}).json()
        self.assertFalse(second['can_resume'])
        self.assertTrue(second['snapshot']['courses']['complete'])
        self.assertEqual(len(second['snapshot']['courses']['terms']), 8)
        self.assertEqual(len(second['snapshot']['courses']['data']), 7)
        self.assertEqual(len(fake.queried), 8)
        self.assertEqual(second['snapshot']['sync_progress']['pending'], [])

    def test_shorter_catalog_preserves_known_semesters_and_cached_courses(self):
        self.connect();self.sync()
        owner=next(iter(self.manager.states));fake=self.manager.states[owner]['client']
        before=self.client.get('/api/campus').json()['snapshot']['courses']
        fake.catalog=[fake.catalog[0]]
        fake.queried=[]
        result=self.sync().json()['snapshot']
        self.assertEqual(len(result['courses']['catalog']),2)
        self.assertEqual(len(result['courses']['terms']),2)
        self.assertEqual(result['sync_progress']['total'],1)
        self.assertEqual(fake.queried,[('2026','3')])
        old=next(t for t in result['courses']['terms'] if t['year']=='2025')
        self.assertEqual(old['data'],before['terms'][1]['data'])
        self.assertEqual(old['fetched_at'],before['terms'][1]['fetched_at'])
        self.assertTrue(old['stale']);self.assertFalse(old['available'])
        self.assertIn('CACHED_TERM_RANGE',{w['code'] for w in result['warnings']})
        self.assertFalse(result['courses']['complete'])
        view=self.client.get('/api/campus/academics?term=2025/12').json()['agenda']
        self.assertEqual(view['term_key'],'2025/12')
        self.assertTrue(view['stale'])

    def test_sync_does_not_consume_an_adapters_cached_catalog(self):
        self.connect();fake=next(iter(self.manager.states.values()))['client']
        catalog=fake.catalog
        with patch.object(fake,'terms',return_value=catalog):
            self.sync();second=self.sync().json()['snapshot']
        self.assertEqual(len(catalog),2)
        self.assertEqual(second['sync_progress']['total'],2)
        self.assertTrue(second['courses']['complete'])

    def test_empty_response_keeps_nonempty_cache_and_later_valid_update_replaces_it(self):
        self.connect();self.sync()
        owner=next(iter(self.manager.states));fake=self.manager.states[owner]['client']
        before=self.client.get('/api/campus').json()['snapshot']['courses']
        fake.empty={('2026','3'),('2025','12')}
        result=self.sync().json()['snapshot']
        self.assertEqual(result['courses']['data'],[r|{'stale':True} for r in before['data']])
        self.assertEqual(result['courses']['fetched_at'],before['fetched_at'])
        self.assertEqual(result['sync_progress']['completed'],0)
        self.assertEqual(len(result['sync_progress']['failed']),2)
        self.assertEqual(len([w for w in result['warnings'] if w['code']=='EMPTY_TIMETABLE_CACHE']),2)
        for old,new in zip(before['terms'],result['courses']['terms']):
            self.assertEqual(old['fetched_at'],new['fetched_at'])
        with patch.object(fake,'timetable',return_value=(PROFILE,[COURSES[0]|{'name':'新课程','location':'新教室'}])):
            updated=self.sync().json()['snapshot']['courses']
        self.assertTrue(updated['complete'])
        self.assertTrue(all(r['name']=='新课程' for r in updated['data']))
        self.assertFalse(updated['stale'])

    def test_discovered_but_unqueried_terms_survive_a_smaller_next_catalog(self):
        self.connect();fake=next(iter(self.manager.states.values()))['client']
        fake.catalog=[{'year':str(2026-i),'semester':'3','term':str(2026-i),'current':i==0} for i in range(8)]
        with patch.object(fake,'timetable',side_effect=CampusError('CAPTCHA_REQUIRED','需要验证码')):
            first=self.sync().json()['snapshot']['courses']
        self.assertEqual(len(first['catalog']),8);self.assertEqual(first['terms'],[])
        fake.catalog=fake.catalog[:1]
        second=self.sync().json()['snapshot']['courses']
        self.assertEqual(len(second['catalog']),8)
        self.assertEqual(len(second['terms']),1)
        self.assertEqual(len(second['data']),1)
        self.assertFalse(second['complete'])

    def test_partial_course_merge_retains_original_time_for_cached_rows(self):
        self.connect();self.sync()
        owner=next(iter(self.manager.states));fake=self.manager.states[owner]['client']
        snapshot=self.manager._snapshot(owner)
        for term in snapshot['courses']['terms']:
            term['fetched_at']='2026-09-01T00:00:00+08:00'
        self.manager._save(owner,snapshot)
        fake.timetable_warnings=[{'code':'PARTIAL_TIMETABLE','message':'一条未解析'}]
        with patch.object(fake,'timetable',return_value=(PROFILE,[COURSES[0]|{'code':'NEW','name':'可用新课程'}])):
            result=self.sync().json()['snapshot']['courses']
        old=[r for r in result['data'] if r['code']=='DEMO']
        self.assertEqual(len(old),2)
        self.assertTrue(all(r['stale'] and r['fetched_at']=='2026-09-01T00:00:00+08:00' for r in old))
        self.assertTrue(any(r['code']=='NEW' and not r['stale'] for r in result['data']))

    def test_course_cache_and_catalog_survive_restart_but_not_account_switch(self):
        self.connect();self.sync()
        before=self.client.get('/api/campus').json()['snapshot']['courses']
        self.manager.close()
        restarted=create_app(self.tmp)
        offline=self.stack.enter_context(TestClient(restarted,base_url='http://127.0.0.1'))
        offline.cookies.update(self.client.cookies)
        result=offline.get('/api/campus').json()
        self.assertFalse(result['connected'])
        self.assertEqual(result['snapshot']['courses']['catalog'],before['catalog'])
        self.assertEqual(result['snapshot']['courses']['data'],before['data'])
        self.register('bob')
        self.assertIsNone(self.client.get('/api/campus').json()['snapshot']['courses'])

    def test_consecutive_http_server_errors_pause_without_discarding_cache_or_pending(self):
        self.connect();self.sync()
        fake=next(iter(self.manager.states.values()))['client']
        fake.catalog=[{'year':str(2026-i),'semester':'3','term':str(2026-i),'current':i==0} for i in range(8)]
        before=self.client.get('/api/campus').json()['snapshot']['courses']['data']
        error=CampusError('UPSTREAM_ERROR','服务器错误',{'http_status':500,'reason':'HTTP_STATUS'})
        with patch.object(fake,'timetable',side_effect=error) as sent:
            result=self.sync().json()
        self.assertEqual(sent.call_count,3)
        self.assertFalse(result['auto_continue']);self.assertTrue(result['can_resume'])
        self.assertEqual(len(result['snapshot']['sync_progress']['pending']),5)
        self.assertEqual(len(result['snapshot']['sync_progress']['failed']),3)
        self.assertEqual(result['snapshot']['courses']['data'][0]['name'],before[0]['name'])
        self.assertTrue(any(w['code']=='SERVER_ERRORS_PAUSED' for w in result['snapshot']['warnings']))
        resumed=self.client.post('/api/campus/sync',json={'resume':True}).json()
        self.assertFalse(resumed['auto_continue']);self.assertFalse(resumed['can_resume'])
        self.assertEqual(len(resumed['snapshot']['sync_progress']['pending']),0)

    def test_one_slow_semester_keeps_cache_and_does_not_block_later_terms(self):
        self.connect();self.sync()
        fake=next(iter(self.manager.states.values()))['client']
        fake.catalog=[{'year':str(2026-i),'semester':'3','term':str(2026-i),'current':i==0} for i in range(8)]
        original=fake.timetable
        error=CampusError('NETWORK_ERROR','超时',{'stage':'timetable','reason':'TIMEOUT'})
        def read(year,semester,captcha=''):
            if year=='2025':raise error
            return original(year,semester,captcha)
        with patch.object(fake,'timetable',side_effect=read):
            first=self.sync().json()
            final=self.client.post('/api/campus/sync',json={'resume':True}).json()
        self.assertTrue(first['auto_continue'])
        self.assertFalse(final['can_resume']);self.assertFalse(final['snapshot']['courses']['complete'])
        self.assertEqual(final['snapshot']['sync_progress']['completed'],7)
        self.assertTrue(any(t['year']=='2025' and t['stale'] for t in final['snapshot']['courses']['terms']))
        self.assertTrue(any(t['year']=='2019' and not t['stale'] for t in final['snapshot']['courses']['terms']))

    def test_repeated_timetable_timeouts_pause_at_three_and_other_transport_errors_stop(self):
        self.connect()
        fake=next(iter(self.manager.states.values()))['client']
        fake.catalog=[{'year':str(2026-i),'semester':'3','term':str(2026-i),'current':i==0} for i in range(8)]
        for reason,expected_calls in (('TIMEOUT',3),('DNS',1)):
            with patch.object(fake,'timetable',side_effect=CampusError('NETWORK_ERROR','失败',{'reason':reason})) as sent:
                result=self.sync().json()
            self.assertEqual(sent.call_count,expected_calls);self.assertFalse(result['auto_continue'])
            self.assertTrue(result['can_resume'])

    def test_discovery_failure_keeps_cache_and_does_not_claim_all_terms(self):
        self.connect()
        self.sync()
        fake = next(iter(self.manager.states.values()))['client']
        fake.discovery_error = CampusError('TERM_DISCOVERY_FAILED', '未发现学期')
        result = self.sync().json()
        self.assertFalse(result['snapshot']['courses']['complete'])
        self.assertTrue(result['snapshot']['courses']['stale'])
        self.assertGreater(len(result['snapshot']['courses']['data']), 2)
        self.assertTrue(any('|' in s for _,s in fake.queried))
        self.assertFalse(result['snapshot']['grades']['stale'])
        self.assertTrue(result['can_resume'])

    def test_known_semester_contract_uses_default_calendar_without_blocking(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        fake.catalog = [{'year':'2026','semester':'1','term':'2026-2027 · 来源学期','current':True}]
        result=self.sync().json()
        self.assertEqual(fake.queried,[('2026','1')])
        self.assertTrue(result['snapshot']['courses']['complete'])
        view=self.client.get('/api/campus/academics?term=2026/1').json()
        self.assertEqual(view['agenda']['term_key'],'2026/1')
        self.assertTrue(view['agenda']['dated'])
        self.assertEqual(view['agenda']['source_status'],'fallback')
        with patch.object(self.manager.calendar_client,'fetch',side_effect=CampusError('CALENDAR_UNAVAILABLE','离线')):
            self.assertEqual(self.client.post('/api/campus/calendar',json={
                'year':'2026','semester':'1','campus':'main'}).status_code,200)

    def test_unsupported_catalog_options_do_not_block_known_terms_or_claim_complete(self):
        self.connect()
        fake=next(iter(self.manager.states.values()))['client']
        fake.catalog=[{'year':str(2026-i),'semester':'3','term':str(2026-i),'current':i==0} for i in range(7)]
        fake.term_warnings=[{'code':'UNSUPPORTED_TERM_OPTIONS','stage':'terms','message':'范围不完整'}]
        first=self.sync().json()
        self.assertTrue(first['auto_continue'])
        final=self.client.post('/api/campus/sync',json={'resume':True}).json()
        self.assertEqual(final['snapshot']['sync_progress']['completed'],7)
        self.assertFalse(final['can_resume'])
        self.assertFalse(final['snapshot']['courses']['complete'])
        self.assertFalse(final['snapshot']['courses']['stale'])
        self.assertFalse(final['snapshot']['grades']['stale'])
        self.assertEqual(final['snapshot']['warnings'][0]['code'],'UNSUPPORTED_TERM_OPTIONS')

    def test_sync_failures_keep_separate_stages_and_safe_diagnostics(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        diagnostic = {'stage':'terms','method':'GET','host':'zdbk.zju.edu.cn',
                      'reason':'TIMEOUT','elapsed_ms':12000}
        fake.discovery_error = CampusError('NETWORK_ERROR','学期列表请求超时', diagnostic)
        with patch.object(fake,'grades',side_effect=CampusError('NETWORK_ERROR','成绩连接失败')):
            result = self.sync().json()
        warnings=result['snapshot']['warnings']
        self.assertEqual([w['stage'] for w in warnings], ['grades','terms','terms'])
        self.assertEqual(warnings[1]['diagnostic'], diagnostic)
        self.assertIsNotNone(result['snapshot']['profile'])
        self.assertIsNone(result['snapshot']['grades'])
        self.assertTrue(result['auto_continue'])
        with self.app.state.store.connect() as db:
            audit=db.execute("SELECT result FROM audit WHERE action='campus.sync' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(audit['result'],'incomplete')

    def test_interrupted_batch_resumes_without_overwriting_other_terms(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        original = fake.timetable
        def interrupt(year, semester, captcha):
            if year == '2025':
                raise CampusError('CAPTCHA_REQUIRED', '需要验证码')
            return original(year, semester, captcha)
        with patch.object(fake, 'timetable', side_effect=interrupt):
            first = self.sync().json()
        self.assertEqual(first['snapshot']['sync_progress']['completed'], 1)
        self.assertTrue(first['can_resume'])
        self.assertFalse(first['auto_continue'])
        second = self.client.post('/api/campus/sync', json={'resume': True, 'captcha': '1234'}).json()
        self.assertTrue(second['snapshot']['courses']['complete'])
        self.assertEqual(fake.queried, [('2026', '3'), ('2025', '12')])

    def test_unknown_student_identity_never_mixes_academic_data(self):
        self.connect()
        self.sync()
        fake = next(iter(self.manager.states.values()))['client']
        with patch.object(fake, 'timetable', return_value=(PROFILE | {'student_id': 'other'}, COURSES)):
            result = self.sync().json()
        self.assertFalse(result['connected'])
        self.assertEqual(result['snapshot']['profile']['data']['student_id'], PROFILE['student_id'])
        self.assertTrue(all(t['stale'] for t in result['snapshot']['courses']['terms']))

    def test_legacy_snapshot_migration_and_current_profile_priority(self):
        self.connect()
        owner = next(iter(self.manager.states))
        self.manager._save(owner, {'profile': None, 'grades': None, 'courses': {
            'year': '2023', 'semester': '3', 'data': COURSES, 'fetched_at': '2023-10-01T00:00:00+08:00'}})
        fake = self.manager.states[owner]['client']
        def timetable(year, semester, captcha):
            return PROFILE | {'major': '当前专业' if year == '2026' else '历史专业'}, COURSES
        with patch.object(fake, 'timetable', side_effect=timetable):
            result = self.sync().json()['snapshot']
        self.assertEqual(result['profile']['data']['major'], '当前专业')
        self.assertEqual(len(result['courses']['terms']), 3)
        old = next(t for t in result['courses']['terms'] if t['year'] == '2023')
        self.assertTrue(old['stale'])
        self.assertTrue(next(c for c in result['courses']['data'] if c['year'] == '2023')['stale'])

    def test_grade_failure_warning_survives_course_batch_continuation(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        fake.catalog = [{'year': str(2026-i), 'semester': '3', 'term': str(2026-i), 'current': i==0}
                        for i in range(7)]
        with patch.object(fake, 'grades', side_effect=CampusError('NETWORK_ERROR', '成绩连接失败')):
            first = self.sync().json()
        self.assertTrue(first['auto_continue'])
        final = self.client.post('/api/campus/sync', json={'resume': True}).json()
        self.assertTrue(final['snapshot']['courses']['complete'])
        self.assertIsNone(final['snapshot']['grades'])
        self.assertEqual(final['snapshot']['warnings'][0]['code'], 'NETWORK_ERROR')

    def test_academic_views_calendar_csrf_owner_and_failure(self):
        from test_academics import CONFIG
        self.connect(); self.sync()
        body = {'year':'2026','semester':'3','campus':'main'}
        self.assertEqual(self.client.post('/api/campus/calendar', json=body, headers={'X-CSRF-Token':'wrong'}).status_code, 403)
        with patch.object(self.manager.calendar_client, 'fetch', return_value=CONFIG):
            self.assertEqual(self.client.post('/api/campus/calendar', json=body).status_code, 200)
        view = self.client.get('/api/campus/academics?day=2026-10-09').json()
        self.assertTrue(view['agenda']['dated'])
        self.assertIn('today_agenda', view)
        with patch.object(self.manager.calendar_client, 'fetch', side_effect=CampusError('CALENDAR_NOT_VERIFIED','未核对')):
            self.client.post('/api/campus/calendar', json=body)
        cached = self.client.get('/api/campus/academics?day=2026-10-09').json()['agenda']
        self.assertTrue(cached['dated'])
        self.assertEqual(cached['source_status'],'cache')
        for query in ('day=2026-02-30','day=bad','term=2026/0','term=2026/1000','term=2026/-1'):
            self.assertEqual(self.client.get('/api/campus/academics?'+query).status_code, 400)
        self.register('bob')
        self.assertEqual(self.client.get('/api/campus/academics').json()['grades']['overall']['records'], 0)
        self.assertEqual(self.client.post('/api/campus/calendar', json=body).status_code, 409)

    def test_academic_static_assets_and_calendar_cleanup(self):
        from test_academics import CONFIG
        for name in ('academics.js','academics.css'):
            response = self.client.get('/static/'+name)
            self.assertEqual(response.status_code, 200)
            self.assertIn("script-src 'self'", response.headers['content-security-policy'])
        self.assertEqual(self.client.get('/static/unknown.js').status_code, 404)
        self.connect(); self.sync()
        with patch.object(self.manager.calendar_client, 'fetch', return_value=CONFIG):
            self.client.post('/api/campus/calendar', json={'year':'2026','semester':'3','campus':'main'})
        self.client.delete('/api/campus')
        self.assertIsNone(self.client.get('/api/campus/academics').json()['agenda']['calendar'])
        self.assertNotIn('calendars', self.client.get('/api/campus').json()['snapshot'])


def cookie(name, domain, value='synthetic-cookie', path='/'):
    return Cookie(0, name, value, None, False, domain, True, domain.startswith('.'), path, True,
                  True, None, True, None, None, {}, False)


class CampusProtocolTests(unittest.TestCase):
    def test_http_error_metadata_precedes_body_decode_and_preserves_query_contract(self):
        from campus_agent.campus import TIMETABLE
        client=CampusClient()
        with patch.object(client,'request',return_value=(500,{'Content-Type':'text/html'},b'\xffsecret-body')):
            with self.assertRaises(CampusError) as caught:client.timetable('2026-2027','1|秋')
        d=caught.exception.diagnostic
        self.assertEqual(d['http_status'],500);self.assertEqual(d['reason'],'HTTP_STATUS')
        self.assertEqual(d['query_year'],'2026-2027');self.assertEqual(d['query_semester'],'1|秋')
        self.assertEqual(d['path'],urlsplit(TIMETABLE).path)
        self.assertNotIn('secret-',json.dumps(caught.exception.public()))

    def test_non_json_error_includes_endpoint_without_body_or_captcha(self):
        client=CampusClient()
        with patch.object(client,'request',return_value=(200,{'Content-Type':'text/html'},b'<html>secret-body</html>')):
            with self.assertRaises(CampusError) as caught:client.timetable('2026-2027','1|秋','secret-captcha')
        d=caught.exception.diagnostic
        self.assertEqual(d['reason'],'NON_JSON');self.assertEqual(d['http_status'],200)
        self.assertNotIn('secret-',json.dumps(caught.exception.public()))

    def test_page_encoding_keeps_semester_source_and_label_without_replacement(self):
        client=CampusClient()
        html='''<select name="xnm"><option selected value="2026">2026-2027</option></select>
            <select name="xqm"><option selected value="1|秋">秋季</option></select>'''
        for headers in ({'Content-Type':'text/html; charset=gb2312'},{},
                        {'Content-Type':'text/html; charset=UTF-8'}):
            with self.subTest(headers=headers),patch.object(client,'request',return_value=(200,headers,html.encode('gbk'))):
                terms=client.terms()
            self.assertEqual(terms[0]['semester'],'1|秋')
            self.assertIn('秋季',terms[0]['term']);self.assertFalse(client.term_warnings)
        with patch.object(client,'request',return_value=(200,{},html.encode())):
            self.assertEqual(client.terms()[0]['semester'],'1|秋')

    def test_replacement_character_is_rejected_with_encoding_shape(self):
        client=CampusClient()
        html='''<select name="xnm"><option value="2026">2026-2027</option></select>
            <select name="xqm"><option value="1">可查询</option><option value="2|�">损坏选项</option></select>'''
        with patch.object(client,'request',return_value=(200,{},html.encode())):terms=client.terms()
        self.assertEqual(len(terms),1)
        shape=client.term_warnings[0]['diagnostic']['option_shapes'][0]
        self.assertEqual(shape['components'][1]['kind'],'replacement')
        self.assertEqual(shape['page_encoding'],'utf-8-sig')

    def test_zero_error_is_not_failure_and_true_errors_have_safe_diagnostics(self):
        from campus_agent.campus import TIMETABLE
        client=CampusClient()
        for flag in (0,'0',False,'false'):
            with patch.object(client,'page_request',return_value=json.dumps({'error':flag,'kbList':[]})):
                self.assertEqual(client.timetable('2026','1|秋')[1],[])
        payload={'success':False,'error':'secret-ticket','msg':'secret-password','xsxx':{'xh':'secret-id'}}
        with patch.object(client,'page_request',return_value=json.dumps(payload)):
            with self.assertRaises(CampusError) as caught:client.timetable('2026','1|秋')
        d=caught.exception.diagnostic
        self.assertEqual(d['path'],urlsplit(TIMETABLE).path)
        self.assertEqual(d['query_semester'],'1|秋')
        self.assertEqual(d['query_year'],'2026')
        self.assertEqual(d['response_shape']['kbList'],'missing')
        self.assertNotIn('secret-',json.dumps(caught.exception.public()))

    def test_upstream_captcha_hint_does_not_clear_other_site_session(self):
        client=CampusClient();client.authenticated=client.passport_authenticated=True
        with patch.object(client,'page_request',return_value=json.dumps({'success':False,'message':'请输入验证码'})):
            with self.assertRaises(CampusError) as caught:client.timetable('2026','3')
        self.assertEqual(caught.exception.code,'CAPTCHA_REQUIRED')
        self.assertEqual(caught.exception.diagnostic['reason'],'CAPTCHA_HINT')
        self.assertTrue(client.authenticated);self.assertTrue(client.passport_authenticated)

    def test_term_options_preserve_source_codes_and_ignore_aggregate_placeholders(self):
        client=CampusClient()
        html='''<select id="source-year" name="xnm"><option selected value="2026">2026-2027</select>
            <select id="source-semester" name="xqm"><option value="-1">--请选择--
            <option value="0">全部学期<option selected value="1">第一学期
            <option value="8">夏<option value="3">秋冬<option value="12">春夏
            <option value="16">短学期</select>'''
        with patch.object(client,'request',return_value=(200,{},html.encode())):
            terms=client.terms()
        self.assertEqual({t['semester'] for t in terms},{'1','8','3','12','16'})
        self.assertEqual(terms[0]['semester'],'1')
        self.assertTrue(terms[0]['current'])
        self.assertEqual(terms[0]['term'],'2026-2027 · 第一学期')
        self.assertFalse(client.term_warnings)

    def test_unqueryable_options_are_reported_without_exposing_html_or_secret_values(self):
        client=CampusClient()
        html='''<select name="xnm"><option value="2026">2026-2027</option></select>
            <select name="xqm"><option value="3">秋冬</option>
            <option value="sensitive-token-value">新选项</option><option value="-2">其他</option></select>'''
        with patch.object(client,'request',return_value=(200,{},html.encode())):
            terms=client.terms()
        self.assertEqual(len(terms),1)
        warning=client.term_warnings[0]
        self.assertEqual(warning['code'],'UNSUPPORTED_TERM_OPTIONS')
        self.assertEqual(set(warning['diagnostic']['semester_codes']),{'-2','<非数值编码>'})
        self.assertNotIn('sensitive-token-value',json.dumps(warning))
        self.assertEqual(warning['diagnostic']['option_shapes'][0]['components'],
                         [{'length':21,'kind':'other'}])
        with patch.object(client,'request',return_value=(200,{},html.replace('value="3"','value="0"').encode())):
            with self.assertRaises(CampusError) as caught:client.terms()
        self.assertEqual(caught.exception.code,'TERM_DISCOVERY_FAILED')
        self.assertNotIn('sensitive-token-value',json.dumps(caught.exception.public()))

    def test_transport_diagnostics_classification_and_secret_redaction(self):
        secret='ST-secret-password-proxy-token'
        cases=[(TimeoutError(secret),'TIMEOUT'),
               (socket.gaierror(-2,secret),'DNS'),
               (ssl.SSLCertVerificationError(1,secret),'TLS_CERTIFICATE'),
               (ssl.SSLError(1,secret),'TLS_HANDSHAKE'),
               (ConnectionResetError(secret),'CONNECTION_RESET'),
               (ConnectionRefusedError(secret),'CONNECTION_REFUSED'),
               (http.client.RemoteDisconnected(secret),'HTTP_INTERRUPTED'),
               (http.client.IncompleteRead(secret.encode()),'HTTP_INTERRUPTED'),
               (secret,'NETWORK')]
        for failure,reason in cases:
            with self.subTest(reason=reason):
                client=CampusClient()
                with patch.object(client.opener,'open',side_effect=urllib.error.URLError(failure)):
                    with self.assertRaises(CampusError) as caught:
                        client.request(SERVICE+'?ticket='+secret,{'password':secret})
                public=caught.exception.public()
                self.assertNotIn(secret,json.dumps(public))
                self.assertEqual(public['diagnostic']['reason'],reason)
                self.assertEqual(public['diagnostic']['stage'],'service_login')
                self.assertEqual(public['diagnostic']['method'],'POST')
                self.assertNotIn('?ticket',public['diagnostic']['path'])
        # Truncated responses during body reading must also be classified, not escape as HTTP 500.
        class Response(BytesIO):
            code=200
            headers={}
            def read(self,*args):raise http.client.IncompleteRead(secret.encode())
        client=CampusClient()
        with patch.object(client.opener,'open',return_value=Response()):
            with self.assertRaises(CampusError) as caught:client.request(TIMETABLE_INDEX)
        self.assertEqual(caught.exception.diagnostic['reason'],'HTTP_INTERRUPTED')
        self.assertNotIn(secret,json.dumps(caught.exception.public()))

    def test_json_query_headers_and_cas_form_separation(self):
        class Response(BytesIO):
            code=200
            headers={}
        for url,form,ajax in [(GRADES_URL,{'queryModel.currentPage':1},True),
                              (MENU,{'doType':''},True),
                              (TIMETABLE_INDEX,None,False),
                              (CAS+'login',{'password':'synthetic'},False)]:
            with self.subTest(url=url):
                client=CampusClient()
                with patch.object(client.opener,'open',return_value=Response(b'{}')) as opened:
                    client.request(url,form)
                request=opened.call_args.args[0]
                self.assertEqual(request.get_header('X-requested-with')=='XMLHttpRequest',ajax)
                self.assertEqual(request.get_header('Referer'),BASE+'xtgl/index_initMenu.html')

    def test_permission_body_is_not_reported_as_expired_login(self):
        client=CampusClient()
        with patch.object(client,'request',return_value=(200,{},'没有访问权限!'.encode())):
            with self.assertRaises(CampusError) as caught:client.json_request(MENU,{})
        self.assertEqual(caught.exception.code,'ACCESS_REJECTED')

    def test_term_discovery_uses_page_options_and_selected_current_term(self):
        client = CampusClient()
        html = '''<select name='xnm'><option value=''>全部</option>
            <option value='2024'>2024-2025</option><option selected value='2026'>2026-2027</option></select>
            <select id="xqm"><option value="3" selected>秋冬</option><option value="12">春夏</option></select>'''
        with patch.object(client, 'request', return_value=(200, {}, html.encode())):
            terms = client.terms()
        self.assertEqual(len(terms), 4)
        self.assertEqual((terms[0]['year'], terms[0]['semester']), ('2026', '3'))
        self.assertNotIn('2025', {t['year'] for t in terms})
        for html in ('<html>no options</html>', '<input name="execution">',
                     '<select id="xnm"><option value="2026">2026</option></select><select id="xqm"><option value="-2">未知</option></select>'):
            with patch.object(client, 'request', return_value=(200, {}, html.encode())):
                with self.assertRaises(CampusError): client.terms()

    def test_cas_cookie_execution_service_ticket_and_probe(self):
        client = CampusClient()
        sent = []
        def request(url, form=None):
            sent.append((url, form))
            if url == CAS+'login' and form is None:
                client.jar.set_cookie(cookie('JSESSIONID', 'zjuam.zju.edu.cn', path='/cas'))
                return 200, {}, b"<input value='synthetic-execution' name='execution'>"
            if url.endswith('getPubKey'):
                return 200, {}, json.dumps({'modulus': 'c'*128, 'exponent': '10001'}).encode()
            if url == CAS+'login':
                client.jar.set_cookie(cookie('iPlanetDirectoryPro', '.zju.edu.cn'))
                return 302, {'Location': CAS+'ignored'}, b''
            if url.startswith(CAS+'login?'):
                return 302, {'Location': SERVICE+'?ticket=ST-synthetic'}, b''
            if url.startswith(SERVICE):
                client.jar.set_cookie(cookie('JSESSIONID', 'zdbk.zju.edu.cn', path='/jwglxt'))
                return 302, {'Location': '/jwglxt/xtgl/index_initMenu.html'}, b''
            if url == MENU:
                return 200, {}, b'{"result":[{"gnmkmc":"test"}]}'
            return 200, {}, b'JS patch page'
        client.request = request
        client.prepare()
        client.login('synthetic', 'campus-secret-test', '1234')
        login = next(form for url, form in sent if url == CAS+'login' and form)
        self.assertEqual(login['execution'], 'synthetic-execution')
        self.assertEqual(login['authcode'], '1234')
        self.assertNotEqual(login['password'], 'campus-secret-test')
        self.assertTrue(client.authenticated)
        self.assertTrue(client.passport_authenticated)
        self.assertTrue(any(c.name=='iPlanetDirectoryPro' for c in client.jar))
        for url, master_expected in ((CAS+'login',True),(SERVICE,False)):
            from urllib.request import Request
            probe=Request(url)
            client.jar.add_cookie_header(probe)
            self.assertEqual('iPlanetDirectoryPro' in (probe.get_header('Cookie') or ''),master_expected)

    def test_redirect_or_cookie_alone_is_not_verified_login(self):
        for mode in ('no-cookie', 'no-probe', 'evil-callback'):
            client = CampusClient()
            client.execution = 'test'
            def request(url, form=None):
                if url.endswith('getPubKey'):
                    return 200, {}, json.dumps({'modulus': 'c'*128, 'exponent': '10001'}).encode()
                if url == CAS+'login':
                    if mode != 'no-cookie':
                        client.jar.set_cookie(cookie('iPlanetDirectoryPro', '.zju.edu.cn'))
                    return 302, {'Location': SERVICE+'?ticket=test'}, b''
                if url.startswith(CAS+'login?'):
                    target = 'https://evil.example/?ticket=test' if mode == 'evil-callback' else SERVICE+'?ticket=test'
                    return 302, {'Location': target}, b''
                return 200, {}, b'<html>JS patch only</html>'
            client.request = request
            with self.assertRaises(CampusError):
                client.login('synthetic', 'test')
            self.assertFalse(client.authenticated)

    def test_url_allowlist_and_sanitized_network_error(self):
        for url in ('http://zdbk.zju.edu.cn/', 'https://zdbk.zju.edu.cn.evil.example/',
                    'https://zdbk.zju.edu.cn:8080/', 'https://user:pass@zdbk.zju.edu.cn/',
                    'https://zdbk.zju.edu.cn:bad/', 'file:///etc/passwd'):
            with self.assertRaises(CampusError): check_url(url)
        client = CampusClient()
        with patch.object(client.opener, 'open', side_effect=urllib.error.URLError('ST-secret-cookie')):
            with self.assertRaises(CampusError) as error:
                client.request(SERVICE+'?ticket=ST-secret-cookie')
        self.assertNotIn('ST-secret-cookie', str(error.exception))

    def test_pagination_total_records_repeated_pages_and_missing_shape(self):
        client = CampusClient()
        pages = [{'items': [{'kcmc': '课程'+str(i), 'cj': '0', 'xf': '0'} for i in range(10)], 'totalCount': 18},
                 {'items': [{'kcmc': '课程'+str(i), 'cj': '优秀'} for i in range(10,18)], 'totalCount': 18}]
        with patch.object(client, 'json_request', side_effect=pages):
            rows, complete, total = client.grades()
        self.assertEqual(len(rows), 18)
        self.assertTrue(complete)
        self.assertEqual(total, 18)
        self.assertEqual(rows[0]['score'], '0')
        with patch.object(client, 'json_request', return_value=pages[0]):
            rows, complete, _ = client.grades()
        self.assertEqual(len(rows), 10)
        self.assertFalse(complete)
        with patch.object(client, 'json_request', return_value={}):
            with self.assertRaises(CampusError): client.grades()

    def test_source_field_normalization_and_unknown_profile(self):
        self.assertIsNone(normalize_profile({'XH': 'demo'}))
        self.assertEqual(normalize_profile({'XH': 'demo', 'XM': '测试', 'ZYMC': '演示'})['major'], '演示')
        course = normalize_course({'kcb':'<b>数据结构</b><br>教学班<br>教师<br>教室zwf', 'djj': '1', 'skcd': '2'})
        self.assertEqual(course['name'], '数据结构')
        self.assertEqual(course['location'], '教室')
        with self.assertRaises(CampusError): normalize_course({'kcb': None})

    def test_response_expiry_captcha_and_error_shape(self):
        client = CampusClient()
        for body, code in [(b'<input name="execution">', 'SESSION_EXPIRED'),
                           (b'{"captcha_error":true}', 'CAPTCHA_REQUIRED'),
                           (b'{"success":false}', 'UPSTREAM_ERROR'),
                           (b'not JSON', 'PROTOCOL_CHANGED')]:
            with patch.object(client, 'request', return_value=(200, {}, body)):
                with self.assertRaises(CampusError) as error:
                    client.json_request(MENU, {})
            self.assertEqual(error.exception.code, code)

    def test_captcha_image_and_response_size_validation(self):
        client = CampusClient()
        with patch.object(client, 'request', return_value=(200, {'Content-Type':'text/html'}, b'login')):
            with self.assertRaises(CampusError): client.captcha('cas')
        class Response(BytesIO):
            code = 200
            headers = {}
        with patch.object(client.opener, 'open', return_value=Response(b'x'*2_000_001)):
            with self.assertRaises(CampusError) as error: client.request(MENU)
        self.assertEqual(error.exception.code, 'RESPONSE_TOO_LARGE')

    def test_rsa_ascii_and_invalid_key(self):
        modulus = 'c'*128
        expected = format(pow(int.from_bytes(b'campus-test', 'big'), 65537, int(modulus,16)), 'x').zfill(128)
        self.assertEqual(encrypt_password('campus-test', modulus, '10001'), expected)
        with self.assertRaises(CampusError): encrypt_password('test', 'bad-key', '10001')

    def test_official_browser_rsa_long_password_fixture(self):
        # Generated with the official public security.js on 2026-10-09.
        expected = ('3ba0c685a9640076044c2bf000dcb5cdaedaa63cae76aecfe42a35af525234366'
                    'dbd9473b907f4ffee8990faaf7bd5c24ba80a5819c34a1d44d62d2abba147a4 '
                    '26ddae9ab7398dd035a7eb0f7c343706f1d293aca06c80131f48895083305aad'
                    '3e22ecc0ce7dda029ea890f6de5181c53086c431ae94ec55a00fc67e328e59b4')
        self.assertEqual(encrypt_password('x'*63, 'c'*128, '10001'), expected)
        with self.assertRaises(CampusError) as error:
            encrypt_password('中文pass', 'c'*128, '10001')
        self.assertEqual(error.exception.code, 'UNSUPPORTED_PASSWORD')


if __name__ == '__main__': unittest.main()
