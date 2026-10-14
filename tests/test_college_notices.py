"""Synthetic college pages and transport. No real campus HTTP in automated tests."""
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from campus_agent.campus import CampusManager
from campus_agent.campus_common import CampusError
from campus_agent.campus_college_sources import CKC
from campus_agent.campus_network import PublicPageTransport, NetworkPolicy
from campus_agent.connector_contracts import CollegeNoticesQuery, CollegeNotice, UserContext
from campus_agent.connectors.college_notices import CkcNoticesAdapter, parse_list, parse_article
from campus_agent.core import TZ
from campus_agent.storage import Store
from campus_agent.web import create_app


URL1 = 'https://webplus.zju.edu.cn/_s597/2026/1010/c78853a1234567/page.psp'
URL2 = 'https://webplus.zju.edu.cn/_s597/2026/1009/c78853a1234568/page.psp'
ATTACHMENT = 'https://webplus.zju.edu.cn/_upload/article/files/ab/cd/1234/abc-123.docx'


def listing(urls=(URL1, URL2)):
    return ('<ul class="news_list list2">' + ''.join(
        f'<li><span class="news_title"><a href="{url}" title="合成通知">截断标题</a></span>'
        '<span class="news_meta">2026-10-10</span></li>' for url in urls) + '</ul>').encode()


def article(body='报名截止：2026-10-15 18:00。'):
    return (f'<h1 class="arti_title">合成通知</h1><h2 class="arti_title"></h2>'
        '<span class="arti_update">发布时间：2026-10-10</span>'
        f'<div class="wp_articlecontent"><p>{body}</p><script>secret-script-text</script>'
        f'<p><a href="{ATTACHMENT}">合成附件.docx</a></p>'
        '<p><a href="https://example.invalid/private?ticket=never-return">外站附件</a></p></div>'
        '<footer>页脚报名截止：2026-10-01 10:00。</footer>').encode()


class FakePages:
    def __init__(self):
        self.pages = {CKC.listing:listing(), URL1:article(), URL2:article('提交截止：下周五。')}
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result, {'Content-Type':'text/html; charset=utf-8'}


def profile(manager, owner, college='竺可桢学院', stamp='2026-10-12T10:00:00+08:00'):
    manager.repository.write(owner, {'profile':{'data':{'college':college}, 'fetched_at':stamp,
        'field_sources':{'college':{'source':'ETA · 个人基本信息','fetched_at':stamp,'stale':False}}}})


class CollegeParserTests(unittest.TestCase):
    def test_plain_body_provenance_and_links(self):
        rows = parse_list(listing(), {})
        self.assertEqual(rows[0]['title'], '合成通知')
        record = CollegeNotice.model_validate(parse_article(article(), {}, rows[0])).model_dump()
        self.assertIn('2026-10-15 18:00',record['body'])
        self.assertNotIn('secret-script-text',record['body'])
        self.assertNotIn('页脚',record['body'])
        self.assertNotIn('ticket=',json.dumps(record))
        self.assertEqual(record['attachments'],[{'name':'合成附件.docx','url':ATTACHMENT}])
        self.assertTrue(record['has_unread_content'])

    def test_source_boundary_empty_and_protocol_change(self):
        for url in ('https://evil.invalid/a', URL1+'?ticket=secret', URL1.replace('_s597','_s999'),
                    URL1.replace('https:','http:'), URL1.replace('webplus.zju.edu.cn','user@webplus.zju.edu.cn')):
            with self.subTest(url=url), self.assertRaises(CampusError):
                parse_list(listing((url,)),{})
        for body in (b'<html>login</html>', b'<ul class="news_list"></ul>', listing().replace(b'2026-10-10',b'2026-99-99')):
            with self.assertRaises(CampusError): parse_list(body,{})
        with self.assertRaises(CampusError):
            parse_article(b'<h1>Login</h1>',{},parse_list(listing(),{})[0])

    def test_inline_markup_keeps_dates_and_image_only_is_not_success(self):
        row = parse_list(listing(),{})[0]
        result = parse_article(article('提交截止：<span>2026</span>-<span>10</span>-15 <b>18:00</b>。'),{},row)
        self.assertIn('2026-10-15 18:00',result['body'])
        with self.assertRaises(CampusError):
            parse_article(b'<h1 class="arti_title">Test</h1><span class="arti_update">'+
                '发布时间：2026-10-10'.encode()+b'</span><div class="wp_articlecontent"><img src="/private.png"></div>',{},row)

    def test_query_cannot_supply_identity_or_source(self):
        for data in ({'college':'竺可桢学院'},{'owner':'alice'},{'url':URL1},{'limit':6},{'limit':True},{'limit':'3'}):
            with self.subTest(data=data),self.assertRaises(ValidationError):
                CollegeNoticesQuery.model_validate(data)

    def test_transport_policy_has_no_credentials_or_redirects(self):
        with patch.dict('os.environ',{'CAMPUS_PUBLIC_NETWORK_POLICY':'system','CAMPUS_NETWORK_POLICY':'direct'}):
            with patch('campus_agent.campus_network.build_opener',wraps=__import__('campus_agent.campus_network',fromlist=['build_opener']).build_opener) as build:
                transport = PublicPageTransport(lambda url: url==URL1)
                build.assert_called_once_with('system')
            self.assertFalse(any(isinstance(h,urllib.request.HTTPCookieProcessor) for h in transport.opener.handlers))
            for status, code in ((302,'PROTOCOL_CHANGED'),(403,'FORBIDDEN'),(429,'RATE_LIMITED')):
                error = urllib.error.HTTPError(URL1,status,'ticket=secret',{'Location':'https://evil.invalid/?ticket=secret'},None)
                with patch.object(transport.opener,'open',side_effect=error) as request:
                    with self.assertRaises(CampusError) as raised: transport.get(URL1)
                    self.assertEqual(raised.exception.code,code)
                    self.assertNotIn('secret',str(raised.exception))
                    self.assertIsNone(request.call_args.args[0].get_header('Cookie'))
                    self.assertEqual(request.call_count,1)
            with patch.object(transport.opener,'open',side_effect=TimeoutError()):
                with self.assertRaises(CampusError) as raised: transport.get(URL1)
                self.assertEqual(raised.exception.code,'TIMEOUT')
            with patch.object(transport.opener,'open') as request:
                with self.assertRaises(CampusError): transport.get('https://evil.invalid/')
                request.assert_not_called()


class CollegeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'campus.sqlite3')
        with self.store.connect() as db:
            db.executemany('INSERT INTO users VALUES(?,?,?)',[('alice','alice','synthetic'),('bob','bob','synthetic')])
        self.manager = CampusManager(self.store)
        self.addCleanup(self.manager.close)
        self.service = self.manager.service
        self.pages = FakePages()
        self.service.registry.adapters['ckc_public'] = CkcNoticesAdapter(self.pages)
        self.alice, self.bob = UserContext('alice'), UserContext('bob')
        profile(self.manager,'alice')

    def sync(self, limit=3):
        return self.service.sync('college_notices',CollegeNoticesQuery(limit=limit),self.alice)

    def test_auto_selection_is_local_and_isolated_without_campus_session(self):
        initial = self.service.query('college_notices',CollegeNoticesQuery(),self.alice)
        self.assertEqual(initial.selection.status,'matched')
        self.assertEqual(self.pages.calls,[])
        result = self.sync()
        self.assertIsNone(result.error)
        self.assertEqual(result.platform,'ckc_public')
        self.assertEqual(len(result.records),2)
        bob = self.service.query('college_notices',CollegeNoticesQuery(),self.bob)
        self.assertEqual(bob.selection.status,'missing')
        self.assertEqual(bob.records,[])
        profile(self.manager,'bob')
        self.assertEqual(self.service.query('college_notices',CollegeNoticesQuery(),self.bob).records,[])
        with patch.object(self.pages,'get',side_effect=AssertionError('Query must be offline')):
            self.service.disconnect('ckc_public',self.alice)
            self.assertEqual(self.service.query('college_notices',CollegeNoticesQuery(),self.alice).records,result.records)
            other = CampusManager(self.store)
            self.addCleanup(other.close)
            self.assertEqual(other.service.query('college_notices',CollegeNoticesQuery(),self.alice).records,result.records)

    def test_changed_unknown_college_never_falls_back_or_returns_old_rows(self):
        self.sync()
        self.pages.calls.clear()
        profile(self.manager,'alice','未接入学院','2026-10-13T10:00:00+08:00')
        result = self.sync()
        self.assertEqual(result.selection.status,'unsupported')
        self.assertEqual(result.records,[])
        self.assertEqual(result.error.code,'UNSUPPORTED')
        self.assertEqual(self.pages.calls,[])
        self.assertEqual(self.service.sync('college_notices',{},self.alice,platform='ckc_public').records,[])
        self.assertEqual(len(self.service.repository.scopes('alice','ckc_public','college_notices')),1)

    def test_new_empty_field_and_conflicting_evidence_disable_matching(self):
        self.service.repository.write('alice','eta','profile','{}',{
            'records':[{'college':''}], 'fetched_at':'2026-10-13T10:00:00+08:00'})
        self.assertEqual(self.sync().selection.status,'missing')
        self.service.repository.write('alice','eta','profile','{}',{
            'records':[{'college':'其他学院'}], 'fetched_at':'2026-10-12T10:00:00+08:00'})
        self.assertEqual(self.sync().selection.status,'ambiguous')
        self.assertEqual(self.pages.calls,[])

    def test_partial_detail_failure_preserves_cache_and_error_then_recovers(self):
        original = self.sync()
        self.pages.pages[URL1] = article('报名截止：2026-10-20 18:00。')
        self.pages.pages[URL2] = CampusError('RATE_LIMITED','ticket=never-return')
        partial = self.sync()
        self.assertEqual(len(partial.records),2)
        self.assertEqual(partial.error.code,'RATE_LIMITED')
        self.assertEqual(partial.completeness,'partial')
        self.assertIn('2026-10-20',partial.records[0]['body'])
        self.assertTrue(partial.records[1]['stale'])
        self.assertEqual(partial.records[1]['fetched_at'],original.records[1]['fetched_at'])
        self.assertNotIn('never-return',partial.model_dump_json())
        self.pages.pages[URL2] = article('提交截止：下周五。')
        recovered = self.sync()
        self.assertIsNone(recovered.error)
        self.assertEqual(len(recovered.records),2)
        self.assertFalse(any(r['stale'] for r in recovered.records))

    def test_list_empty_first_detail_failure_and_narrow_scope_preserve_rows(self):
        original = self.sync()
        self.pages.pages[CKC.listing] = b'<ul class="news_list"></ul>'
        empty = self.sync()
        self.assertEqual(empty.records,original.records)
        self.assertEqual(empty.fetched_at,original.fetched_at)
        self.assertEqual(empty.error.code,'PROTOCOL_CHANGED')
        self.pages.pages[CKC.listing] = listing()
        self.pages.pages[URL1] = CampusError('TIMEOUT','synthetic')
        failure = self.sync()
        self.assertEqual(len(failure.records),2)
        self.assertEqual(failure.fetched_at,original.fetched_at)
        self.assertEqual(failure.error.code,'TIMEOUT')
        self.pages.pages[URL1] = article()
        self.assertEqual(len(self.sync(limit=1).records),1)
        self.assertEqual(len(self.service.query('college_notices',CollegeNoticesQuery(),self.alice).records),2)

    def test_permissions_and_explicit_wrong_platform(self):
        forbidden = self.service.sync('college_notices',{},UserContext('alice',frozenset()))
        self.assertEqual(forbidden.error.code,'FORBIDDEN')
        self.assertIsNone(forbidden.selection)
        wrong = self.service.sync('college_notices',{},self.alice,platform='zdbk_public')
        self.assertEqual(wrong.error.code,'UNSUPPORTED')
        self.assertEqual(self.pages.calls,[])


class CollegeRoutesTests(unittest.TestCase):
    def test_authenticated_flow_import_dedup_tasks_and_other_owner(self):
        with TemporaryDirectory() as directory:
            app = create_app(directory)
            pages = FakePages()
            app.state.campus_service.registry.adapters['ckc_public'] = CkcNoticesAdapter(pages)
            with TestClient(app,base_url='http://127.0.0.1') as client:
                self.assertEqual(client.get('/static/college-notices.js').status_code,200)
                self.assertIn('/static/college-notices.js',client.get('/').text)
                client.headers['X-Campus-Request']='1'
                account = client.post('/api/auth/register',json={'username':'alice','password':'synthetic-pass'}).json()
                client.headers['X-CSRF-Token']=account['csrf']
                with app.state.store.connect() as db:
                    owner=db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
                profile(app.state.campus,owner)
                path='/api/campus/sync/college_notices'
                for query in ({'owner_id':'other'},{'college':'竺可桢学院'},{'url':URL1},{'limit':7}):
                    self.assertEqual(client.post(path,json={'query':query}).status_code,400)
                records=client.post(path,json={'query':{}}).json()['records']
                record=records[0]
                calls=len(pages.calls)
                self.assertEqual(client.get('/api/campus/query/college_notices').json()['records'],records)
                self.assertEqual(len(pages.calls),calls)
                content=('官方来源：'+record['source']+'\n\n'+record['body']).encode()
                headers={'X-Filename':quote('合成学院通知.txt'),'X-Document-Kind':'notice'}
                uploaded=client.post('/api/documents',content=content,headers=headers).json()
                self.assertEqual(client.post('/api/documents',content=content,headers=headers).json()['id'],uploaded['id'])
                until=time.monotonic()+15
                while time.monotonic()<until:
                    doc=client.get('/api/documents/'+uploaded['id']).json()
                    if doc['status'] in ('ready','failed'): break
                    time.sleep(.04)
                self.assertEqual(doc['status'],'ready',doc)
                tasks=client.get('/api/tasks').json()
                self.assertEqual(len(tasks),1)
                self.assertEqual(tasks[0]['due'],'2026-10-15T18:00:00+08:00')
                self.assertEqual(tasks[0]['reviewed'],0)
                self.assertIn(record['source'],client.get('/api/documents/'+uploaded['id']+'/download').text)
                client.post('/api/auth/logout')
                self.assertEqual(client.get('/api/campus/query/college_notices').status_code,401)
                bob=client.post('/api/auth/register',json={'username':'bob','password':'synthetic-pass'}).json()
                client.headers['X-CSRF-Token']=bob['csrf']
                self.assertEqual(client.get('/api/campus/query/college_notices').json()['records'],[])
                self.assertEqual(client.get('/api/tasks').json(),[])


if __name__ == '__main__':
    unittest.main()
