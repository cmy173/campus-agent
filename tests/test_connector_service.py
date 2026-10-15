"""Behavioral tests with synthetic records / transport only; no campus login."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookiejar import Cookie
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from pydantic import ValidationError
from connectors.campus.campus import CampusClient, CampusError, CampusManager
from connectors.campus.campus_service import CampusService, ConnectorRegistry
from connectors.campus.connector_contracts import (UserContext, CoursesQuery, GradesQuery, NoticesQuery,
    ProfileQuery, ConnectorSpec, FetchResult, standard_error)
from connectors.campus.connectors.notices import PublicNoticesAdapter, BOARD, parse_board
from connectors.campus.campus_network import build_opener, PlatformBudget
from connectors.campus.campus_store import SQLiteStore
from test_campus import FakeCampus, COURSES, GRADES


NOTICE_ID = 'A'*32
BOARD_HTML = f'''<ul class="log-list"><li><a href="/jwglxt/xtgl/xwck_ckLoginNews.html?xwbh={NOTICE_ID}&amp;doType=save"><font>置顶</font> 合成通知</a><span>[2026-10-12 10:00:00]</span></li></ul>'''


class FakeAdapter:
    spec = ConnectorSpec('zdbk_public', ('notices',), 'public')
    def __init__(self):
        self.rows = [{'id':NOTICE_ID, 'title':'合成通知', 'source':'https://zdbk.zju.edu.cn/'}]
        self.error, self.calls, self.active, self.max_active = None, 0, 0, 0
        self.complete = 'complete'
    def fetch(self, capability, query, session):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active,self.active)
        try:
            if self.error: raise self.error
            return FetchResult(list(self.rows),self.complete)
        finally:
            self.active -= 1


class ConnectorServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteStore(Path(self.tmp.name)/'campus.sqlite3')
        # The retired local-account user table is gone: owners are plain workspace keys.
        self.manager = CampusManager(self.store, FakeCampus)
        self.addCleanup(self.manager.close)
        self.service = self.manager.service
        self.adapter = FakeAdapter()
        self.service.registry.adapters['zdbk_public'] = self.adapter
        self.alice, self.bob = UserContext('alice'), UserContext('bob')

    def test_query_is_offline_even_when_expired_or_disconnected(self):
        first = self.service.query('notices',NoticesQuery(),self.alice)
        self.assertEqual(first.freshness,'unknown')
        self.assertEqual(self.adapter.calls,0)
        self.service.sync('notices',NoticesQuery(),self.alice)
        self.adapter.error = CampusError('TIMEOUT','ticket=secret')
        with patch('connectors.campus.campus_service.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat('2099-01-01T00:00:00+08:00')
            cached = self.service.query('notices',NoticesQuery(),self.alice)
        self.assertEqual(cached.freshness,'stale')
        self.assertEqual(len(cached.records),1)
        self.assertEqual(self.adapter.calls,1)

    def test_owners_and_query_ranges_are_isolated(self):
        self.service.sync('notices',NoticesQuery(),self.alice)
        self.assertEqual(self.service.query('notices',NoticesQuery(),self.bob).records,[])
        self.assertEqual(self.service.query('notices',NoticesQuery(limit=1),self.alice).records,[])
        self.assertIsNone(self.service.query('grades',GradesQuery(),self.bob).fetched_at)

    def test_failed_connector_preserves_timestamp_records_and_other_connector(self):
        first = self.service.sync('notices',NoticesQuery(),self.alice)
        self.adapter.error = CampusError('NETWORK_ERROR','https://proxy:secret@evil/?ticket=ST-secret',{'reason':'TIMEOUT'})
        failed = self.service.sync('notices',NoticesQuery(),self.alice)
        self.assertEqual(failed.error.code,'TIMEOUT')
        self.assertEqual(failed.fetched_at,first.fetched_at)
        self.assertEqual(failed.records,first.records)
        self.assertGreaterEqual(failed.last_attempt_at,first.last_attempt_at)
        self.assertNotIn('ST-secret',failed.model_dump_json())
        self.manager.states['alice'] = {'client':FakeCampus(),'connected':True,'expires':time.monotonic()+100}
        self.assertIsNone(self.service.sync('grades',GradesQuery(),self.alice).error)

    def test_partial_empty_repeated_sync_never_withdraws_known_records(self):
        self.adapter.rows.append({'id':'B'*32,'title':'合成通知 B'})
        first = self.service.sync('notices',NoticesQuery(),self.alice)
        self.adapter.rows = self.adapter.rows[:1]
        self.adapter.complete = 'partial'
        for _ in range(2):
            result = self.service.sync('notices',NoticesQuery(),self.alice)
            self.assertEqual(len(result.records),2)
            self.assertEqual(result.completeness,'partial')
        self.adapter.rows = []
        result = self.service.sync('notices',NoticesQuery(),self.alice)
        self.assertEqual(len(result.records),2)
        self.assertEqual(result.freshness,'stale')
        self.assertIn('EMPTY_CACHE_RETAINED',[w['code'] for w in result.warnings])
        self.service.disconnect('zdbk_public',self.alice)
        self.assertEqual(len(self.service.query('notices',NoticesQuery(),self.alice).records),2)
        self.manager.disconnect('alice',True)
        self.assertEqual(self.service.query('notices',NoticesQuery(),self.alice).records,[])

    def test_unknown_capability_platform_and_untrusted_params_never_fetch(self):
        for capability,platform in [('unknown',None),('notices','https://evil'),('notices','eta'),('notices','')]:
            result = self.service.sync(capability,{},self.alice,platform=platform)
            self.assertEqual(result.error.code,'UNSUPPORTED')
        self.assertEqual(self.service.query('notices',{},UserContext('alice',frozenset())).error.code,'FORBIDDEN')
        self.assertEqual(self.service.query('notices',{}, {'owner':'alice'}).error.code,'FORBIDDEN')
        for query in ({'owner_id':'bob'}, {'url':'https://evil'}, {'limit':True}, {'limit':7}):
            with self.assertRaises(ValidationError): self.service.sync('notices',query,self.alice)
        self.assertEqual(self.adapter.calls,0)

    def test_session_expiry_disconnects_only_one_site_and_keeps_cas_cache(self):
        client = CampusClient()
        client.passport_authenticated = client.authenticated = client.eta_authenticated = True
        self.manager.states['alice'] = {'client':client,'connected':True,'expires':time.monotonic()+100}
        for host in ('zdbk.zju.edu.cn','eta.zju.edu.cn'):
            client.jar.set_cookie(Cookie(0,'JSESSIONID','synthetic',None,False,host,True,False,'/',True,True,None,True,None,None,{}))
        with patch.object(client,'grades',side_effect=CampusError('SESSION_EXPIRED','secret')):
            result = self.service.sync('grades',GradesQuery(),self.alice)
        self.assertEqual(result.error.code,'AUTH_EXPIRED')
        self.assertTrue(client.eta_authenticated)
        self.assertTrue(client.passport_authenticated)
        self.assertEqual([c.domain for c in client.jar],['eta.zju.edu.cn'])
        self.assertFalse(self.manager.states['alice']['connected'])
        self.assertIsNone(self.manager._state('bob'))

    def test_single_owner_sync_is_serialized(self):
        original = self.adapter.fetch
        active, seen = 0, []
        def slow(*args):
            nonlocal active
            active += 1; seen.append(active)
            time.sleep(.02)
            try: return original(*args)
            finally: active -= 1
        self.adapter.fetch = slow
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:self.service.sync('notices',NoticesQuery(),self.alice),range(4)))
        self.assertEqual(max(seen),1)
        self.assertTrue(all(len(r.records)==1 for r in results))

    def test_platform_quota_is_shared_across_accounts_without_network_retry(self):
        self.service.attempts['zdbk_public']=[time.monotonic()]*30
        self.assertEqual(self.service.sync('notices',NoticesQuery(),self.alice).error.code,'RATE_LIMITED')
        self.assertEqual(self.service.sync('notices',NoticesQuery(),self.bob).error.code,'RATE_LIMITED')
        self.assertEqual(self.adapter.calls,0)
        budget=PlatformBudget(limit=1)
        budget.take('eta')
        budget.take('zdbk')
        with self.assertRaises(CampusError) as caught:budget.take('eta')
        self.assertEqual(caught.exception.code,'RATE_LIMITED')

    def test_legacy_partial_and_empty_grades_retain_old_success(self):
        fake=FakeCampus()
        self.manager.states['alice']={'client':fake,'connected':True,'expires':time.monotonic()+100}
        self.manager.sync('alice')
        snapshot=self.manager._snapshot('alice')
        snapshot['grades']['data'].append(GRADES[0] | {'code':'OLD','name':'其他合成成绩'})
        self.manager._save('alice',snapshot)
        fake.complete=False
        result=self.manager.sync('alice')['snapshot']['grades']
        self.assertEqual(len(result['data']),2)
        stamp=result['fetched_at']
        with patch.object(fake,'grades',return_value=([],True,0)):
            empty=self.manager.sync('alice')['snapshot']['grades']
        self.assertEqual(len(empty['data']),2)
        self.assertEqual(empty['fetched_at'],stamp)
        self.assertFalse(empty['complete'])
        self.assertTrue(empty['stale'])

    def test_legacy_profile_only_exposes_eta_fields_and_preserves_field_evidence(self):
        self.manager._save('alice',{'profile':{'data':{'student_id':'SYNTHETIC','college':'教务学院','major':'ETA专业'},
            'field_sources':{'student_id':{'source':'ETA · 本人信息','url':'https://eta.zju.edu.cn/','stale':False},
                             'major':{'source':'ETA · 本人信息','url':'https://eta.zju.edu.cn/','stale':False},
                             'college':{'source':'本科教务','url':'https://zdbk.zju.edu.cn/','stale':False}}}})
        result=self.service.query('profile',ProfileQuery(),self.alice)
        self.assertNotIn('college',result.records[0])
        self.assertEqual(result.records[0]['field_sources']['major']['source'],'https://eta.zju.edu.cn/')

    def test_adapter_contract_rejects_credential_source_and_discards_secret_warning(self):
        self.adapter.rows[0]['source']='https://zdbk.zju.edu.cn/?ticket=SECRET'
        result=self.service.sync('notices',NoticesQuery(),self.alice)
        self.assertEqual(result.error.code,'PROTOCOL_CHANGED')
        self.assertNotIn('SECRET',result.model_dump_json())
        self.adapter.rows[0]['source']='https://zdbk.zju.edu.cn/'
        self.adapter.fetch=lambda *args:FetchResult(self.adapter.rows,'partial',[{'code':'UPSTREAM','message':'Cookie=SECRET','url':'https://evil/?ticket=SECRET'}])
        result=self.service.sync('notices',NoticesQuery(),self.alice)
        self.assertNotIn('SECRET',result.model_dump_json())

    def test_error_classes_are_distinct_and_messages_do_not_contain_upstream_data(self):
        cases = [('AUTH_REQUIRED',{},'AUTH_REQUIRED'),('SESSION_EXPIRED',{},'AUTH_EXPIRED'),
            ('CAPTCHA_REQUIRED',{},'INTERACTION_REQUIRED'),('ACCESS_REJECTED',{'http_status':403},'FORBIDDEN'),
            ('ACCESS_REJECTED',{'http_status':429},'RATE_LIMITED'),('NETWORK_ERROR',{'reason':'DNS'},'NETWORK_UNREACHABLE'),
            ('NETWORK_ERROR',{'reason':'TIMEOUT'},'TIMEOUT'),('ETA_PROTOCOL_CHANGED',{},'PROTOCOL_CHANGED'),
            ('UNSUPPORTED_SERVICE',{},'UNSUPPORTED')]
        for code,diag,expected in cases:
            with self.subTest(code=code,diag=diag):
                result = standard_error(CampusError(code,'Cookie=secret ticket=secret',diag))
                self.assertEqual(result.code,expected)
                self.assertNotIn('secret',result.model_dump_json())

    def test_partial_or_interrupted_grades_preserve_previous_and_retry_from_first_page(self):
        client = CampusClient()
        self.manager.states['alice']={'client':client,'connected':True,'expires':time.monotonic()+100}
        self.manager._save('alice',{'grades':{'data':GRADES+[GRADES[0] | {'name':'其他合成课程','code':'OTHER'}],
            'fetched_at':datetime.now().astimezone().isoformat(),'stale':False,'complete':True}})
        with patch.object(client,'grades',return_value=(GRADES,False,2)):
            result=self.service.sync('grades',GradesQuery(),self.alice)
        self.assertEqual(len(result.records),2)
        with patch.object(client,'json_request',side_effect=[{'items':[{'kcmc':'A','kch':'A'}]*100,'totalCount':200},
                CampusError('NETWORK_ERROR','interrupted')]) as request:
            result=self.service.sync('grades',GradesQuery(),self.alice)
        self.assertEqual(len(result.records),2)
        self.assertEqual(request.call_args_list[0].args[1]['queryModel.currentPage'],1)
        self.assertIsNone(result.next_cursor)

    def test_scoped_courses_are_composed_by_dashboard_without_losing_other_terms(self):
        self.manager.states['alice']={'client':FakeCampus(),'connected':True,'expires':time.monotonic()+100}
        self.manager._save('alice',{'courses':{'data':[], 'terms':[{'year':'2026','semester':'3',
            'term':'秋冬','current':True,'data':COURSES,'stale':False,'fetched_at':'2026-01-01T00:00:00+08:00'}]}})
        self.service.sync('courses',CoursesQuery(year='2025',semester='12'),self.alice)
        self.assertEqual(len(self.service.query('courses',CoursesQuery(),self.alice).records),2)
        view=self.manager.academic_view('alice',term='2025/12')
        self.assertEqual(view['agenda']['term_key'],'2025/12')
        self.assertTrue(self.manager._snapshot('alice')['courses']['terms'][0]['data'])

    def test_partial_scoped_courses_upgrade_legacy_rows_without_duplicate_or_deletion(self):
        fake=FakeCampus()
        fake.timetable_warnings=[{'code':'PARTIAL_TIMETABLE'}]
        fake.catalog[0]['query_year']='2026-2027'
        self.manager.states['alice']={'client':fake,'connected':True,'expires':time.monotonic()+100}
        self.manager._save('alice',{'courses':{'terms':[{'year':'2026','semester':'3','term':'秋冬',
            'data':COURSES+[COURSES[0] | {'name':'其他合成课程','code':'OLD'}],
            'fetched_at':'2026-01-01T00:00:00+08:00','stale':False}]}})
        for _ in range(2):
            result=self.service.sync('courses',CoursesQuery(year='2026',semester='3'),self.alice)
            self.assertEqual(len(result.records),2)
            self.assertEqual(result.completeness,'partial')
            self.assertEqual(result.freshness,'stale')
            old=next(r for r in result.records if r['code']=='OLD')
            self.assertEqual(old['fetched_at'],'2026-01-01T00:00:00+08:00')
        self.assertEqual(fake.queried,[('2026-2027','3')]*2)

    def test_registration_of_another_public_adapter_needs_no_existing_platform_branch(self):
        other=FakeAdapter()
        other.spec=ConnectorSpec('other_public',('notices',),'public')
        self.service.registry.register(other)
        result=self.service.sync('notices',NoticesQuery(),self.alice,platform='other_public')
        self.assertIsNone(result.error)
        self.assertEqual(result.platform,'other_public')
        self.assertEqual(self.service.query('notices',NoticesQuery(),self.alice).records,[])

    def test_changed_timetable_identity_rejects_data_and_expires_only_zdbk(self):
        fake=FakeCampus()
        fake.eta_authenticated=True
        self.manager.states['alice']={'client':fake,'connected':True,'expires':time.monotonic()+100}
        self.manager._save('alice',{'profile':{'data':{'student_id':'OTHER-SYNTHETIC'}}})
        result=self.service.sync('courses',CoursesQuery(year='2026',semester='3'),self.alice)
        self.assertEqual(result.error.code,'AUTH_EXPIRED')
        self.assertEqual(result.records,[])
        self.assertTrue(fake.eta_authenticated)
        self.assertFalse(self.manager.states['alice']['connected'])


class PublicNoticeTransportTests(unittest.TestCase):
    def test_parser_preserves_business_id_date_and_does_not_guess_deadline(self):
        rows=parse_board(BOARD_HTML.encode(),{'Content-Type':'text/html; charset=utf-8'})
        self.assertEqual(rows[0]['id'],NOTICE_ID)
        self.assertEqual(rows[0]['published_at'],'2026-10-12 10:00:00')
        self.assertNotIn('deadline',rows[0])
        with self.assertRaises(CampusError):parse_board(b'<html>changed</html>',{})
        with self.assertRaises(CampusError):parse_board(BOARD_HTML.replace('/jwglxt/xtgl/','https://evil/').encode(),{})
        with self.assertRaises(CampusError):parse_board(BOARD_HTML.replace('&amp;doType=save','&amp;doType=save&amp;ticket=secret').encode(),{})

    def test_public_sync_uses_only_fixed_board_without_cookie_or_redirect_following(self):
        class Response(BytesIO):
            code=200
            headers={'Content-Type':'text/html; charset=utf-8'}
        adapter=PublicNoticesAdapter()
        with patch.object(adapter.opener,'open',return_value=Response(BOARD_HTML.encode())) as opened:
            result=adapter.fetch('notices',NoticesQuery())
        self.assertEqual(result.completeness,'partial')
        request=opened.call_args.args[0]
        self.assertEqual(request.full_url,BOARD)
        self.assertFalse(request.has_header('Cookie'))
        for status,code in [(302,'PROTOCOL_CHANGED'),(429,'RATE_LIMITED'),(403,'FORBIDDEN')]:
            with patch.object(adapter.opener,'open',side_effect=urllib.error.HTTPError(BOARD,status,'secret',{},None)):
                with self.assertRaises(CampusError) as caught:adapter.fetch('notices',NoticesQuery())
            self.assertEqual(caught.exception.code,code)

    def test_network_system_and_direct_only_change_campus_clients(self):
        class Reached(Exception):pass
        with patch.dict(os.environ,{'https_proxy':'http://127.0.0.1:18765'}),patch('urllib.request.proxy_bypass',return_value=False):
            for policy,expected in [('direct','zdbk.zju.edu.cn'),('system','127.0.0.1:18765')]:
                with patch.object(urllib.request.HTTPSHandler,'do_open',side_effect=Reached) as opened:
                    with self.assertRaises(Reached):build_opener(policy).open('https://zdbk.zju.edu.cn/')
                self.assertEqual(opened.call_args.args[1].host,expected)
            with patch.object(urllib.request.HTTPSHandler,'do_open',side_effect=Reached) as opened:
                with self.assertRaises(Reached):urllib.request.build_opener().open('https://api.openai.com/')
            self.assertEqual(opened.call_args.args[1]._tunnel_host,'api.openai.com')
        with self.assertRaises(ValueError):build_opener('webvpn')


class ConnectorSessionTests(unittest.TestCase):
    """Owner/platform scoping and disconnect semantics at the service boundary."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteStore(Path(self.tmp.name)/'campus.sqlite3')
        self.manager = CampusManager(self.store, FakeCampus)
        self.addCleanup(self.manager.close)
        self.service = self.manager.service
        self.adapter = FakeAdapter()
        self.service.registry.adapters['zdbk_public'] = self.adapter
        self.alice, self.bob = UserContext('alice'), UserContext('bob')

    def test_identity_and_platform_are_not_taken_from_the_call_and_disconnect_keeps_cache(self):
        self.service.sync('notices', NoticesQuery(), self.alice)
        self.assertEqual(len(self.service.query('notices', NoticesQuery(), self.alice).records), 1)
        # A caller-supplied unknown platform never fetches and never invents records.
        wrong = self.service.sync('notices', NoticesQuery(), self.alice, platform='evil')
        self.assertEqual(wrong.error.code, 'UNSUPPORTED')
        # Scope cannot be widened by the caller: another owner sees nothing.
        self.assertEqual(self.service.query('notices', NoticesQuery(), self.bob).records, [])
        self.manager.disconnect('alice')
        self.assertEqual(len(self.service.query('notices', NoticesQuery(), self.alice).records), 1)


if __name__ == '__main__':unittest.main()
