"""Synthetic passport identity, automatic sync, migration and offline authorization."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import time
import threading
import unittest

from fastapi.testclient import TestClient
from campus_agent.campus import CampusError
from campus_agent.web import create_app, password_matches
from test_campus import FakeCampus, PROFILE


class FakePassport(FakeCampus):
    created=[]
    def __init__(self):
        super().__init__()
        self.passport_authenticated=False
        self.connect_error=None
        self.password_submissions=0
        self.catalog=[{'year':str(2026-i),'semester':'1','term':str(2026-i)+' · 来源学期','current':i==0} for i in range(8)]
        self.created.append(self)
    def authenticate_passport(self,username,password,authcode):
        self.password_submissions+=1
        if password=='wrong':raise CampusError('CAS_REJECTED','认证未通过')
        self.passport_authenticated=True
    def connect_zdbk(self):
        if self.connect_error:raise self.connect_error
    def close(self):
        super().close()
        self.passport_authenticated=False


class PassportTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.tmp=self.stack.enter_context(TemporaryDirectory())
        self.app=create_app(self.tmp)
        self.app.state.campus.factory=FakePassport
        self.client=self.stack.enter_context(TestClient(self.app,base_url='http://127.0.0.1'))
        self.client.headers['X-Campus-Request']='1'
        FakePassport.created=[]
    def tearDown(self):self.stack.close()
    def prepare(self):
        r=self.client.post('/api/auth/passport/prepare',json={})
        self.assertEqual(r.status_code,200,r.text)
        self.flow=r.json()['flow_id']
        return FakePassport.created[-1]
    def login(self,username='synthetic-student',password='synthetic-campus-secret',link=False):
        return self.client.post('/api/auth/passport/login',json={'flow_id':self.flow,'username':username,
            'password':password,'authcode':'1234','link_legacy':link})
    def accept(self,response):
        self.assertEqual(response.status_code,200,response.text)
        self.client.headers['X-CSRF-Token']=response.json()['csrf']
        return response.json()

    def test_passport_login_automatically_syncs_first_batch_and_resumes_without_password(self):
        fake=self.prepare()
        result=self.accept(self.login())
        self.assertEqual(result['mode'],'passport')
        self.assertEqual(result['campus']['snapshot']['sync_progress']['completed'],6)
        self.assertTrue(result['campus']['auto_continue'])
        self.assertEqual(result['campus']['snapshot']['profile']['data']['name'],PROFILE['name'])
        final=self.client.post('/api/campus/sync',json={'resume':True}).json()
        self.assertEqual(final['snapshot']['sync_progress']['completed'],8)
        self.assertTrue(final['snapshot']['courses']['complete'])
        self.assertEqual(fake.password_submissions,1)
        self.assertEqual(self.client.get('/api/me').json()['mode'],'passport')
        with self.app.state.store.connect() as db:
            dump='\n'.join(db.iterdump())
            stored=db.execute('SELECT password FROM users').fetchone()[0]
        for secret in ('synthetic-campus-secret',self.flow,'iPlanetDirectoryPro'):
            self.assertNotIn(secret,dump)
        self.assertFalse(password_matches('synthetic-campus-secret',stored))

    def test_wrong_password_or_different_browser_never_opens_cached_identity(self):
        self.prepare()
        self.assertEqual(self.login(password='wrong').status_code,409)
        self.assertEqual(self.client.get('/api/me').status_code,401)
        with self.app.state.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
        self.prepare()
        other=self.stack.enter_context(TestClient(self.app,base_url='http://127.0.0.1'))
        other.headers['X-Campus-Request']='1'
        r=other.post('/api/auth/passport/login',json={'flow_id':self.flow,'username':'synthetic-student','password':'secret'})
        self.assertEqual(r.status_code,409)
        self.accept(self.login())
        self.assertEqual(other.get('/api/campus').status_code,401)

    def test_campus_connection_failure_preserves_cache_and_uses_no_password_retry(self):
        self.prepare();self.accept(self.login())
        fake=self.prepare()
        fake.connect_error=CampusError('NETWORK_ERROR','连接失败')
        result=self.accept(self.login())
        self.assertTrue(result['campus']['snapshot']['grades']['stale'])
        self.assertTrue(result['campus']['snapshot']['courses']['data'])
        self.assertTrue(all(row['stale'] for row in result['campus']['snapshot']['courses']['data']))
        self.assertFalse(result['campus']['connected'])
        self.assertEqual(result['campus']['snapshot']['warnings'][0]['stage'],'service_login')
        self.assertEqual(fake.password_submissions,1)
        fake.connect_error=None
        refreshed=self.client.post('/api/campus/reconnect',json={})
        self.assertEqual(refreshed.status_code,200)
        self.assertFalse(refreshed.json()['snapshot']['grades']['stale'])
        self.assertEqual(fake.password_submissions,1)
        fake.connect_error=CampusError('NETWORK_ERROR','连接失败')
        failed=self.client.post('/api/campus/reconnect',json={}).json()
        self.assertFalse(failed['connected'])
        self.assertFalse(failed['can_resume'])
        self.assertTrue(failed['passport_connected'])
        self.assertEqual(fake.password_submissions,1)

    def test_restart_defaults_to_cache_only_for_authenticated_browser_and_logout_revokes(self):
        self.prepare();self.accept(self.login())
        owner=next(iter(self.app.state.campus.states))
        self.app.state.campus.disconnect(owner)
        self.assertEqual(self.client.get('/api/me').json()['mode'],'offline')
        self.assertIsNotNone(self.client.get('/api/campus').json()['snapshot']['grades'])
        r=self.client.post('/api/campus/reconnect',json={})
        self.assertEqual(r.status_code,409)
        # Re-create the app as a process restart, retaining only this browser's cookie.
        restarted=create_app(self.tmp)
        offline=self.stack.enter_context(TestClient(restarted,base_url='http://127.0.0.1'))
        offline.cookies.update(self.client.cookies)
        self.assertEqual(offline.get('/api/me').json()['mode'],'offline')
        self.assertIsNotNone(offline.get('/api/campus').json()['snapshot']['profile'])
        self.assertEqual(self.client.post('/api/auth/logout').status_code,200)
        self.assertEqual(offline.get('/api/me').status_code,401)

    def test_account_switch_isolated_and_matching_legacy_name_not_auto_merged(self):
        local=self.accept(self.client.post('/api/auth/register',json={'username':'synthetic-student','password':'old-local-secret'}))
        self.client.post('/api/documents',content=b'local-only',headers={'X-Filename':'local.txt','Content-Type':'application/octet-stream'})
        self.prepare();self.accept(self.login())
        self.assertEqual(self.client.get('/api/documents').json(),[])
        self.prepare();self.accept(self.login(username='other-student'))
        self.assertEqual(self.client.get('/api/me').json()['username'],'other-student')
        with self.app.state.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],3)

    def test_explicit_legacy_binding_requires_csrf_and_keeps_document_owner(self):
        self.accept(self.client.post('/api/auth/register',json={'username':'legacy-owner','password':'old-local-secret'}))
        r=self.client.post('/api/documents',content=b'local-only',headers={'X-Filename':'local.txt','Content-Type':'application/octet-stream'})
        doc=r.json()['id']
        self.prepare()
        self.client.headers['X-CSRF-Token']='wrong'
        self.assertEqual(self.login(link=True).status_code,403)
        self.client.headers['X-CSRF-Token']=self.client.get('/api/me').json()['csrf']
        self.accept(self.login(link=True))
        self.assertEqual(self.client.get('/api/documents').json()[0]['id'],doc)
        self.assertEqual(self.client.get('/api/me').json()['username'],'synthetic-student')
        with self.app.state.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)

    def test_flow_expiry_invalid_input_and_rate_limits(self):
        self.prepare()
        # A flow is consumed even if upstream validation fails; never retry credentials automatically.
        self.assertEqual(self.login(password='wrong').status_code,409)
        self.assertEqual(self.login().status_code,409)
        self.assertEqual(self.login(username='bad/name').status_code,400)
        self.assertEqual(self.login().status_code,429)
        self.assertEqual(self.client.get('/static/passport.js').status_code,200)

    def test_cancel_prepared_flow_closes_it_and_never_submits_password(self):
        fake=self.prepare()
        response=self.client.post('/api/auth/passport/cancel',json={'flow_id':self.flow})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.password_submissions,0)
        self.assertEqual(self.login().status_code,409)
        self.assertEqual(self.client.get('/api/me').status_code,401)

    def test_different_browser_cannot_cancel_another_login(self):
        self.prepare()
        other=self.stack.enter_context(TestClient(self.app,base_url='http://127.0.0.1'))
        other.headers['X-Campus-Request']='1'
        self.assertEqual(other.post('/api/auth/passport/cancel',json={'flow_id':self.flow}).status_code,409)
        self.accept(self.login())

    def test_cancel_during_authentication_never_binds_account_or_creates_session(self):
        fake=self.prepare()
        entered,release=threading.Event(),threading.Event()
        original=fake.authenticate_passport
        def waiting(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('Synthetic test timed out')
            original(*args)
        fake.authenticate_passport=waiting
        cancel=self.stack.enter_context(TestClient(self.app,base_url='http://127.0.0.1'))
        cancel.headers['X-Campus-Request']='1';cancel.cookies.update(self.client.cookies)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.login)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(cancel.post('/api/auth/passport/cancel',json={'flow_id':self.flow}).status_code,200)
            finally:release.set()
            response=future.result(timeout=5)
        self.assertEqual(response.json()['detail']['code'],'FLOW_CANCELLED')
        self.assertTrue(fake.closed)
        self.assertEqual(fake.password_submissions,1)
        with self.app.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)

    def test_cancel_during_first_sync_preserves_previous_local_session_and_documents(self):
        self.accept(self.client.post('/api/auth/register',json={'username':'legacy','password':'independent-local-password'}))
        self.client.post('/api/documents',content=b'keep my local file',headers={'X-Filename':'local.txt','Content-Type':'application/octet-stream'})
        fake=self.prepare()
        entered,release=threading.Event(),threading.Event()
        def waiting():
            entered.set()
            if not release.wait(5):raise RuntimeError('Synthetic test timed out')
        fake.connect_zdbk=waiting
        cancel=self.stack.enter_context(TestClient(self.app,base_url='http://127.0.0.1'))
        cancel.headers['X-Campus-Request']='1';cancel.cookies.update(self.client.cookies)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.login)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(cancel.post('/api/auth/passport/cancel',json={'flow_id':self.flow}).status_code,200)
            finally:release.set()
            response=future.result(timeout=5)
        self.assertEqual(response.json()['detail']['code'],'FLOW_CANCELLED')
        self.assertEqual(self.client.get('/api/me').json()['username'],'legacy')
        self.assertEqual(len(self.client.get('/api/documents').json()),1)
        self.assertTrue(fake.closed)
        with self.app.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)

    def test_late_cancel_revokes_only_session_issued_by_this_flow(self):
        fake=self.prepare();self.accept(self.login())
        response=self.client.post('/api/auth/passport/cancel',json={'flow_id':self.flow})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(fake.closed)
        self.assertEqual(self.client.get('/api/me').status_code,401)
        with self.app.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM campus_snapshots').fetchone()[0],1)
