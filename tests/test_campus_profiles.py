"""Synthetic ETA/profile protocol tests; no student credentials or live requests."""
from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import urllib.request

from connectors.campus.campus import (CampusClient, CampusError, CampusManager, CAS, SERVICE,
                                      CONNECTOR_REVISION, merge_profile, stale_profile)
from connectors.campus.campus_profiles import (ETA_BASE, ETA_SERVICE, ETA_CURRENT, ETA_ROLES, ETA_COMMON,
    PROFILE_KEYS, ProfilePage, basic_fields, form_fields, schema_shape, self_values, form_data_request,
    eta_encode, eta_decode, eta_profile, zdbk_profile)
from connectors.campus.campus_store import SQLiteStore
from test_campus import FakeCampus, PROFILE, COURSES, cookie


class FakePassport(FakeCampus):
    """Synthetic passport session: no real credentials, no live requests."""
    created = []

    def __init__(self):
        super().__init__()
        self.passport_authenticated = False
        self.connect_error = None
        self.password_submissions = 0
        self.catalog = [{'year': str(2026-i), 'semester': '1', 'term': str(2026-i)+' · 来源学期', 'current': i == 0} for i in range(8)]
        self.created.append(self)

    def authenticate_passport(self, username, password, authcode):
        self.password_submissions += 1
        if password == 'wrong':
            raise CampusError('CAS_REJECTED', '认证未通过')
        self.passport_authenticated = True

    def connect_zdbk(self):
        if self.connect_error:
            raise self.connect_error

    def close(self):
        super().close()
        self.passport_authenticated = False


FORM = {'groupFields': [{'fields': [
    {'fieldCode': 'xh', 'defaultValue': 'SYNTHETIC-STUDENT'},
    {'fieldCode': 'xm', 'defaultValue': '合成学生'},
    {'fieldCode': 'yxdm', 'fieldName': '学院', 'defaultValue': ['DEMO'], 'defaultLabel': ['ETA 测试学院']},
    {'fieldCode': 'zymc', 'defaultValue': 'ETA 测试专业'},
    {'fieldCode': 'bjmc', 'defaultValue': '测试班'},
    {'fieldCode': 'njdm', 'defaultValue': '2026'},
    {'fieldCode': 'sfzh', 'fieldName': '身份证号', 'defaultValue': 'discard-identity'},
    {'fieldCode': 'phone', 'defaultValue': 'discard-phone'},
]}]}


class ProfileProtocolTests(unittest.TestCase):
    def test_eta_repeated_sync_reuses_verified_site_session_and_rechecks_identity(self):
        client,calls=self.eta_client()
        eta_profile(client,'SYNTHETIC-STUDENT')
        eta_profile(client,'SYNTHETIC-STUDENT')
        self.assertEqual(len([u for u,_ in calls if 'ticket=ST-synthetic' in u]),1)
        self.assertEqual(len([u for u,_ in calls if u==ETA_CURRENT]),2)
        self.assertTrue(client.eta_authenticated)
        with self.assertRaises(CampusError) as caught:eta_profile(client,'OTHER-STUDENT')
        self.assertEqual(caught.exception.code,'PROFILE_IDENTITY_MISMATCH')
        self.assertFalse(client.eta_authenticated)

    def test_eta_existing_cookie_is_verified_and_site_session_survives_cas_expiry(self):
        client,calls=self.eta_client()
        client.jar.set_cookie(cookie('JSESSIONID','eta.zju.edu.cn',path='/zftal-xgxt-web'))
        with patch.object(client,'service_callback',side_effect=AssertionError('Must reuse ETA cookie')):
            eta_profile(client,'SYNTHETIC-STUDENT')
            client.passport_authenticated=False
            records,warnings=client.profiles('SYNTHETIC-STUDENT')
        self.assertEqual(len(records),1);self.assertEqual(warnings,[])
        self.assertTrue(client.eta_authenticated)
        client.close();self.assertFalse(client.eta_authenticated)
        self.assertEqual(len(client.jar),0)

    def test_eta_confirmed_expiry_renews_once_but_network_or_access_failures_do_not(self):
        client,calls=self.eta_client();client.eta_authenticated=True
        original=client.request;expired=[True]
        def request(url,*args,**kwargs):
            if url==ETA_CURRENT and expired and expired.pop():
                return 302,{'Location':'https://zjuam.zju.edu.cn/cas/login'},b''
            return original(url,*args,**kwargs)
        client.request=request
        eta_profile(client,'SYNTHETIC-STUDENT')
        self.assertEqual(len([u for u,_ in calls if 'ticket=ST-synthetic' in u]),1)
        for failure in (CampusError('NETWORK_ERROR','超时',{'reason':'TIMEOUT'}),
                        CampusError('ETA_ACCESS_REJECTED','访问拒绝'),CampusError('ETA_PROTOCOL_CHANGED','格式变化')):
            client,_=self.eta_client();client.eta_authenticated=True
            with patch.object(client,'request',side_effect=failure),patch.object(client,'service_callback') as renew:
                with self.assertRaises(CampusError):eta_profile(client)
                renew.assert_not_called()
            self.assertTrue(client.eta_authenticated)
            self.assertTrue(client.passport_authenticated)

    def test_eta_wait_is_bounded_and_other_sites_keep_their_timeout(self):
        class Response(BytesIO):
            code=200;headers={}
        for url,navigation,remaining,expected in ((ETA_SERVICE.replace('http:','https:'),True,100,20),
                (ETA_CURRENT,False,100,20),(ETA_CURRENT,False,5,5),(CAS+'login',False,100,12)):
            client=CampusClient();client.deadline=remaining
            with patch('connectors.campus.campus.time.monotonic',return_value=0),patch.object(client.opener,'open',return_value=Response(b'{}')) as opened,patch('connectors.campus.campus.time.sleep'):
                client.request(url,read_body=not navigation)
                self.assertEqual(opened.call_args.kwargs['timeout'],expected)

    def test_fresh_eta_callback_timeout_keeps_cas_and_does_not_retry_ticket(self):
        client,_=self.eta_client()
        failure=CampusError('NETWORK_ERROR','ETA 超时',{'stage':'profile_eta','reason':'TIMEOUT','path':urlsplit(ETA_SERVICE).path})
        with patch.object(client,'request_navigation',side_effect=failure) as navigate,patch.object(client,'service_callback',return_value=ETA_SERVICE.replace('http:','https:')) as renew:
            records,warnings=client.profiles()
        self.assertEqual(records,[]);self.assertEqual(warnings[0]['code'],'NETWORK_ERROR')
        self.assertEqual(navigate.call_count,1);self.assertEqual(renew.call_count,1)
        self.assertTrue(client.passport_authenticated)
        self.assertFalse(client.eta_authenticated)

    def test_hometown_jgdm_is_never_college_and_bad_cache_is_withdrawn(self):
        form={'fields':[{'fieldCode':'jgdm','fieldName':'籍贯','editType':'cascader',
                         'defaultValue':'P,C,D','defaultLabel':['合成省','合成市','合成县']} ]}
        self.assertFalse(form_fields(form)['college'])
        client,_=self.eta_client(form=form,module_code='xsxg')
        records,_=client.profiles()
        self.assertEqual(records[0]['invalidated_fields'],['college'])
        url=records[0]['url']
        snapshot={}
        merge_profile(snapshot,{'data':{'college':'错误地址','student_id':'SYNTHETIC-STUDENT'},'source':records[0]['source'],'url':url})
        merge_profile(snapshot,records[0])
        self.assertFalse(snapshot['profile']['data']['college'])
        self.assertNotIn('college',snapshot['profile']['field_sources'])
        merge_profile(snapshot,{'data':{'college':'教务学院'},'source':'本科教务网','url':'https://zdbk.zju.edu.cn/self'})
        merge_profile(snapshot,records[0])
        self.assertEqual(snapshot['profile']['data']['college'],'教务学院')

    def test_discovered_wdzd_fills_only_academic_fields_with_separate_sources(self):
        academic={'fields':[{'fieldCode':'bmdm|zydm|nj|bjdm','defaultValue':'C,M,2026,B',
                            'defaultLabel':['学籍学院','学籍专业','2026','学籍班级']},
                           {'fieldCode':'phone','defaultValue':'discard-phone'}]}
        client,calls=self.eta_client(form={'fields':[{'fieldCode':'xm','defaultValue':'合成学生'}]},academic=academic)
        fields,_=eta_profile(client)
        self.assertEqual([fields[k] for k in ('college','major','year','class_name')],['学籍学院','学籍专业','2026','学籍班级'])
        self.assertTrue(client.eta_field_urls['college'].endswith('/wdzd/default.zf'))
        self.assertTrue(client.eta_field_urls['name'].endswith('/userInfo/default.zf'))
        request=next(u for u,_ in calls if '/find/wdzd/' in u)
        self.assertEqual(parse_qs(urlsplit(request).query)['dataId'],[eta_encode('SYNTHETIC-STUDENT')])
        self.assertNotIn('discard-phone',json.dumps(fields))
        snapshot={};records,_=client.profiles();merge_profile(snapshot,records[0])
        self.assertEqual(snapshot['profile']['field_sources']['college']['url'],client.eta_field_urls['college'])

    def test_wdzd_identity_failure_discards_profile_and_access_failure_keeps_other_fields(self):
        for academic in ({'xh':'OTHER-STUDENT'}, {'fields':[{'fieldCode':'xh','defaultValue':'OTHER-STUDENT'}]}):
            client,_=self.eta_client(academic=academic)
            with self.assertRaises(CampusError) as caught:eta_profile(client)
            self.assertEqual(caught.exception.code,'PROFILE_IDENTITY_MISMATCH')
        client,_=self.eta_client(academic=CampusError('ETA_ACCESS_REJECTED','拒绝'))
        fields,_=eta_profile(client)
        self.assertEqual(fields['college'],'ETA 测试学院')
        self.assertEqual(client.eta_followup_warnings[0]['code'],'ETA_ACCESS_REJECTED')

    def test_wdzd_declared_actual_values_keep_own_record_and_module_scope(self):
        academic={'fields':[{'fieldCode':'zydm','fieldName':'专业','editType':'select'}],
                  'formDataUrl':'/teacher/xshx/getGrxx.zf?formCode=wdzd'}
        client,calls=self.eta_client(form={'fields':[]},academic=academic,
                                   actual={'xh':'SYNTHETIC-STUDENT','zydm':'M','zy':'实际学籍专业'})
        fields,_=eta_profile(client)
        self.assertEqual(fields['major'],'实际学籍专业')
        actual_url=next(u for u,_ in calls if '/getGrxx.zf' in u)
        query=parse_qs(urlsplit(actual_url).query)
        self.assertEqual(query['formCode'],['wdzd'])
        self.assertEqual(query['dataId'],[eta_encode('SYNTHETIC-STUDENT')])
        for url in ('/teacher/xshx/getGrxx.zf?formCode=familyInfo',
                    '/dynamic/form/data/userInfo/default.zf',
                    '/dynamic/form/data/wdzd/save.zf'):
            with self.subTest(url=url), self.assertRaises(CampusError):
                form_data_request({'formDataUrl':url},'SYNTHETIC-STUDENT','wdzd')

    def test_live_observed_flat_fields_layout_is_read_without_default_probe(self):
        form={'formCode':'userInfo','extendCode':'xsxg','fields':FORM['groupFields'][0]['fields'],
              'formDataUrl':'','tableDataUrl':'','showType':'form'}
        client,calls=self.eta_client(form=form,module_code='xsxg')
        fields,path=eta_profile(client)
        self.assertEqual(fields['college'],'ETA 测试学院')
        self.assertEqual(fields['major'],'ETA 测试专业')
        self.assertEqual(fields['class_name'],'测试班');self.assertEqual(fields['year'],'2026')
        self.assertTrue(path.endswith('/userInfo/xsxg.zf'))
        self.assertEqual(client.eta_field_diagnostic['schema_shape']['layout'],'flat')
        self.assertEqual(len([u for u,_ in calls if '/dynamic/form/' in u]),1)
        self.assertTrue(client.eta_form_available)

    def test_flat_fields_actual_declaration_and_identity_guards_still_apply(self):
        form={'fields':[{'fieldCode':'custom_sid','fieldName':'学号'},
                        {'fieldCode':'xydm','fieldName':'学院'}],
              'formDataUrl':'/teacher/xshx/getGrxx.zf'}
        client,_=self.eta_client(form=form,actual={'xh':'SYNTHETIC-STUDENT','xydm':'C','xy':'实际学院'})
        fields,_=eta_profile(client);self.assertEqual(fields['college'],'实际学院')
        client,_=self.eta_client(form=form,actual={'custom_sid':'OTHER-STUDENT'})
        with self.assertRaises(CampusError):eta_profile(client)

    def test_codec_matches_independent_node_aes_vectors(self):
        self.assertEqual(eta_encode('SYNTHETIC-STUDENT'), 'tfzyyLte7M0oqZriQb8fUyd/w8w8M7Ob9yJPY85xYkY=')
        self.assertEqual(eta_encode('1234567890123456'), '4Ec+sJzObFwnLPYhDo02ew==')
        self.assertEqual(eta_encode('示例'), 'h1kCYrAT69kHrVGO6IRskg==')
        vectors = [
            '/uLK2RgnLeQrWUooIRqsuM/1FUs6+ZdWBNU5WCOmEeBCKwX9YXeLj9hzTBErRf95cSmbf11wf1ixJgrIG8owq7QzjfxhWU+G32qC4FW9B4k=',
            'bl6nUcLQrdU35iseOZ6G8FBiSihA1G4LZcYOmYN+CCbFkaJ5tQ4xD36ArGs7jTfqz3c5yakqxuJp8vvG7h6ZEetwOP8GRQFgDcbq2Ry4tZQ=']
        expected = {'code': 0, 'data': {'xh': 'SYNTHETIC-STUDENT', 'xymc': '测试学院'}}
        for raw in vectors + [json.dumps(vectors[0]), json.dumps(expected)]:
            self.assertEqual(eta_decode(raw.encode()), expected)

    def test_bad_codec_reports_safe_error(self):
        for raw in (b'<html>secret-body</html>', b'"c2VjcmV0LWJvZHk="', b'null', b'[]'):
            with self.assertRaises(CampusError) as caught:
                eta_decode(raw)
            self.assertEqual(caught.exception.code, 'ETA_PROTOCOL_CHANGED')
            self.assertNotIn('secret-body', json.dumps(caught.exception.public()))

    def test_form_labels_aliases_and_whitelist(self):
        result = form_fields(FORM)
        self.assertEqual(result['college'], 'ETA 测试学院')
        self.assertEqual(set(result), set(PROFILE_KEYS))
        self.assertNotIn('discard-', json.dumps(result))
        self.assertEqual(basic_fields({'JGMC': '', 'XYMC': '学院', 'NJ': '2026'})['college'], '学院')
        self.assertEqual(form_fields({'groupFields': [{'fields': {'bad': 1}}, None]})['name'], '')
        result = form_fields({'groupFields': [{'fields': [{'fieldName': '学院', 'defaultValue': ['C'],
            'options': [{'value': 'C', 'label': '选项学院'}]}]}]})
        self.assertEqual(result['college'], '选项学院')
        self.assertEqual(basic_fields({'zymc': 'CODE@@@专业'})['major'], '专业')

    def test_profile_html_reads_fields_and_discards_other_inputs(self):
        page = ProfilePage('''<input name="xh" value="SYNTHETIC-STUDENT">
            <input name="token" value="discard-token"><table><tr><th>学院：</th><td>测试学院</td></tr></table>
            <select name="bjmc"><option>其他班</option><option selected>当前班</option></select>''')
        fields = basic_fields(page.values)
        self.assertEqual(fields['college'], '测试学院')
        self.assertEqual(fields['class_name'], '当前班')
        self.assertNotIn('token', page.values)

    def test_eta_http_service_identifier_only_upgrades_identical_callback(self):
        client = CampusClient(); client.passport_authenticated = True
        with patch.object(client, 'request', return_value=(302, {'Location': ETA_SERVICE+'?ticket=ST-synthetic'}, b'')) as sent:
            callback = client.service_callback('eta')
        self.assertEqual(parse_qs(urlsplit(sent.call_args.args[0]).query)['service'], [ETA_SERVICE])
        self.assertEqual(callback, ETA_SERVICE.replace('http:', 'https:')+'?ticket=ST-synthetic')
        for location in ('http://evil.test'+urlsplit(ETA_SERVICE).path+'?ticket=x',
                         ETA_SERVICE.replace('eta.', 'user@eta.')+'?ticket=x',
                         ETA_SERVICE.replace('.cn/', '.cn:8080/')+'?ticket=x',
                         ETA_SERVICE+'?ticket=x#fragment', ETA_BASE+'/other.zf?ticket=x', ETA_SERVICE):
            with self.subTest(location=location), patch.object(client, 'request', return_value=(302, {'Location': location}, b'')):
                with self.assertRaises(CampusError): client.service_callback('eta')

    def test_eta_never_receives_cas_master_cookie(self):
        client = CampusClient()
        client.jar.set_cookie(cookie('iPlanetDirectoryPro', '.zju.edu.cn'))
        client.jar.set_cookie(cookie('JSESSIONID', 'eta.zju.edu.cn', path='/zftal-xgxt-web'))
        eta = urllib.request.Request(ETA_CURRENT); client.jar.add_cookie_header(eta)
        self.assertIn('JSESSIONID', eta.get_header('Cookie'))
        self.assertNotIn('iPlanetDirectoryPro', eta.get_header('Cookie'))
        cas = urllib.request.Request(CAS+'login'); client.jar.add_cookie_header(cas)
        self.assertIn('iPlanetDirectoryPro', cas.get_header('Cookie'))

    def eta_client(self, current=None, roles=None, form=None, actual=None, module_code='default',templates=None,academic=None):
        client = CampusClient(); client.passport_authenticated = True
        calls = []
        callback = ETA_SERVICE.replace('http:', 'https:')+'?ticket=ST-synthetic'
        def request(url, post=None, **kwargs):
            calls.append((url, post))
            if url == callback: return 200, {}, b'<html>landing</html>'
            path = urlsplit(url).path
            if url == ETA_CURRENT: data = {'xh': 'SYNTHETIC-STUDENT', 'xm': '合成学生'} if current is None else current
            elif url == ETA_ROLES: data = roles if roles is not None else 0
            elif path.endswith('switchRole.zf'): data = {}
            elif path.endswith('commonGrxx.zf'):
                data = {'modules': [{'formCode': 'familyInfo', 'extendCode': 'other'},
                                    {'formCode': 'userInfo', 'extendCode': module_code}]}
                if academic is not None:data['modules'].append({'formCode':'wdzd','extendCode':'default'})
            elif '/dynamic/form/find/userInfo/' in path:
                code=path.rsplit('/',1)[-1][:-3]
                data=templates.get(code,{}) if templates is not None else FORM if form is None else form
                if isinstance(data,CampusError):raise data
            elif path.endswith('/dynamic/form/find/wdzd/default.zf'):
                if isinstance(academic,CampusError):raise academic
                data=academic
            elif path.endswith('/teacher/xshx/getGrxx.zf'): data = actual
            else: raise AssertionError('Unexpected endpoint: '+path)
            return 200, {}, json.dumps({'code': 0, 'data': data}).encode()
        client.request = request
        client.service_callback = lambda service: callback if service == 'eta' else None
        return client, calls

    def test_declared_actual_data_populates_empty_schema_and_uses_display_labels(self):
        form = {'formDataUrl':'/teacher/xshx/getGrxx.zf','dataId':'own-record',
            'groupFields':[{'fields':[
                {'fieldCode':'xh','fieldName':'学号'}, {'fieldCode':'xm','fieldName':'姓名'},
                {'fieldCode':'xydm','fieldName':'学院','editType':'cascader'},
                {'fieldCode':'zydm','fieldName':'专业','editType':'select',
                 'defaultLabel':['过时专业'],'options':[{'value':'M','label':'实际专业'}]},
                {'fieldCode':'bjdm','fieldName':'班级','editType':'select'},
                {'fieldCode':'nj_id','fieldName':'年级'}]}]}
        actual = {'xh':'SYNTHETIC-STUDENT','xm':'实际姓名','xydm':['C'],'xy':['实际学院'],
                  'zydm':'M','bjdm':'B','bj':'实际班级','nj_id':'2026','sfzh':'discard-identity'}
        client,calls=self.eta_client(form=form,actual={'formData':actual})
        fields,path=eta_profile(client)
        self.assertEqual(fields,dict(zip(PROFILE_KEYS,
            ['SYNTHETIC-STUDENT','实际姓名','实际学院','实际专业','实际班级','2026'])))
        self.assertEqual(path,ETA_BASE+'/teacher/xshx/getGrxx.zf')
        query=parse_qs(urlsplit(calls[-1][0]).query,keep_blank_values=True)
        self.assertEqual(query,{'dataId':[eta_encode('own-record')],'gnczid':['']})
        self.assertTrue(client.eta_field_diagnostic['data_request'])
        self.assertEqual(set(client.eta_field_diagnostic['field_status'].values()),{'available'})
        self.assertNotIn('实际学院',json.dumps(client.eta_field_diagnostic))
        self.assertNotIn('discard-',json.dumps(fields))

    def test_actual_data_self_identity_and_single_record_only(self):
        form={'formDataUrl':'/teacher/xshx/getGrxx.zf',
              'groupFields':[{'fields':[{'fieldCode':'custom_sid','fieldName':'学号'}]}]}
        for actual in ({'xh':'OTHER-STUDENT'}, {'custom_sid':'OTHER-STUDENT'},
                       [{'xh':'SYNTHETIC-STUDENT'},{'xh':'OTHER-STUDENT'}]):
            client,_=self.eta_client(form=form,actual=actual)
            with self.subTest(actual=actual),self.assertRaises(CampusError):eta_profile(client)
        self.assertEqual(self_values({'rows':[{'xh':'SYNTHETIC-STUDENT'}]},'SYNTHETIC-STUDENT'),
                         {'xh':'SYNTHETIC-STUDENT'})

    def test_form_data_declaration_cannot_expand_to_other_hosts_or_write_or_list(self):
        for source in ('https://evil.test/teacher/xshx/getGrxx.zf',
                       'http://eta.zju.edu.cn/zftal-xgxt-web/teacher/xshx/getGrxx.zf',
                       '//user@eta.zju.edu.cn/zftal-xgxt-web/teacher/xshx/getGrxx.zf',
                       '/teacher/xshx/getGrxxList.zf','/teacher/xshx/saveGrxx.zf',
                       '/dynamic/form/find/familyInfo/default.zf',
                       '/teacher/xshx/getGrxx.zf?xh=OTHER-STUDENT',
                       '/teacher/xshx/getGrxx.zf?formCode=familyInfo',
                       '/teacher/xshx/getGrxx.zf?offset=1',
                       '/teacher/xshx/../xshx/getGrxx.zf'):
            client,calls=self.eta_client(form={'formDataUrl':source})
            with self.subTest(source=source),self.assertRaises(CampusError):eta_profile(client)
            self.assertFalse(any(urlsplit(u).path.endswith('getGrxx.zf') for u,_ in calls))
        url,_=form_data_request({'formDataUrl':'/teacher/xshx/getGrxx.zf?xh=SYNTHETIC-STUDENT'},'SYNTHETIC-STUDENT')
        self.assertEqual(parse_qs(urlsplit(url).query)['xh'],[eta_encode('SYNTHETIC-STUDENT')])

    def test_unlabelled_codes_and_explicit_empty_actual_do_not_become_profile_values(self):
        form={'groupFields':[{'fields':[{'fieldCode':'xydm','fieldName':'学院','editType':'select',
                                        'defaultLabel':['过时学院'],'defaultValue':'OLD'}]}]}
        for actual in ({'xydm':'UNLABELLED'},{'xydm':''}):
            self.assertFalse(form_fields(form,actual)['college'])

    def test_composite_class_cascader_inline_labels_fill_four_fields(self):
        form={'groupFields':[{'fields':[{'fieldCode':'bmdm|zydm|nj|bjdm',
            'fieldName':'就读信息','editType':'custom','customCode':'classCascader',
            'defaultValue':['C','M','2026','B'],
            'defaultLabel':['合成学院','合成专业','2026','合成班级']}]}]}
        fields=form_fields(form)
        self.assertEqual([fields[k] for k in ('college','major','year','class_name')],
                         ['合成学院','合成专业','2026','合成班级'])
        shape=schema_shape(form)
        self.assertEqual(shape['basic_fields'][0]['fields'],['college','major','year','class_name'])
        self.assertTrue(shape['basic_fields'][0]['composite'])
        self.assertNotIn('合成学院',json.dumps(shape))
        client,_=self.eta_client(form=form)
        fields,_=eta_profile(client)
        self.assertFalse(client.eta_field_diagnostic['data_request'])
        self.assertEqual(fields['college'],'合成学院')

    def test_composite_actual_overrides_schema_and_does_not_misalign_labels(self):
        form={'groupFields':[{'fields':[{'fieldCode':'bmdm|zydm|nj|bjdm',
            'defaultValue':['OLD_C','OLD_M','2025','OLD_B'],
            'defaultLabel':['旧学院','旧专业','2025','旧班级']}]}]}
        actual={'bmdm':'C','bm':'新学院','zydm':'M','zy':'新专业','nj':'2026','bjdm':'B','bj':'新班级'}
        fields=form_fields(form,actual)
        self.assertEqual([fields[k] for k in ('college','major','year','class_name')],
                         ['新学院','新专业','2026','新班级'])
        form['groupFields'][0]['fields'][0]['defaultLabel']=['只有班级名']
        fields=form_fields(form)
        self.assertFalse(fields['college']);self.assertFalse(fields['major']);self.assertFalse(fields['class_name'])
        self.assertEqual(fields['year'],'2025')

    def test_inline_actual_container_pairs_names_and_encoded_display_values(self):
        form={'curDatas':{'bmdm':'C','bm':'学院','zydm':'M@@@专业'},
              'groupFields':[{'fields':[{'fieldCode':'bmdm'},{'fieldCode':'zydm'}]}]}
        fields=form_fields(form)
        self.assertEqual(fields['college'],'学院');self.assertEqual(fields['major'],'专业')

    def test_composite_identity_mismatch_is_not_overwritten_by_current_user(self):
        form={'groupFields':[{'fields':[{'fieldCode':'xh|bmdm',
                                        'defaultValue':['OTHER-STUDENT','C']}]}]}
        client,_=self.eta_client(form=form)
        with self.assertRaises(CampusError) as caught:eta_profile(client)
        self.assertEqual(caught.exception.code,'PROFILE_IDENTITY_MISMATCH')

    def test_composite_nested_options_resolve_only_selected_path_labels(self):
        form={'groupFields':[{'fields':[{'fieldCode':'bmdm|zydm|nj|bjdm',
            'defaultValue':['C','M','2026','B'], 'options':[
                {'value':'OTHER','label':'其他学院'}, {'value':'C','label':'学院','children':[
                    {'value':'M','label':'专业','children':[{'value':'2026','label':'2026','children':[
                        {'value':'B','label':'班级'}]}]}]}]}]}]}
        fields=form_fields(form)
        self.assertEqual([fields[k] for k in ('college','major','year','class_name')],['学院','专业','2026','班级'])
        form['groupFields'][0]['fields'][0]['defaultValue'][0]='UNMATCHED'
        fields=form_fields(form)
        self.assertFalse(fields['college']);self.assertFalse(fields['major']);self.assertFalse(fields['class_name'])

    def test_partial_eta_result_has_safe_field_status_diagnostic(self):
        client,_=self.eta_client(form={'xh':'SYNTHETIC-STUDENT'})
        records,warnings=client.profiles()
        self.assertEqual(records[0]['data']['student_id'],'SYNTHETIC-STUDENT')
        warning=next(w for w in warnings if w['code']=='PROFILE_PARTIAL')
        self.assertEqual(warning['diagnostic']['field_status']['college'],'unavailable')
        self.assertNotIn('SYNTHETIC-STUDENT',json.dumps(warning))

    def test_eta_own_userinfo_only_and_encrypted_self_id(self):
        client, calls = self.eta_client()
        fields, path = eta_profile(client, 'SYNTHETIC-STUDENT')
        self.assertEqual(fields['college'], 'ETA 测试学院')
        self.assertEqual(set(fields), set(PROFILE_KEYS))
        request_url = calls[-1][0]
        self.assertEqual(parse_qs(urlsplit(request_url).query)['dataId'], [eta_encode('SYNTHETIC-STUDENT')])
        self.assertNotIn('?', path)
        self.assertNotIn('familyInfo', str(calls))

    def test_wrapped_and_serialized_definitions_are_parsed_without_other_modules(self):
        for form in ({'formData':FORM},{'definition':{'form':json.dumps(FORM)}},
                     {'formDatas':FORM,'familyInfo':{'xh':'OTHER-STUDENT'}},
                     {'groupFields':[],'form':FORM}):
            client,calls=self.eta_client(form=form)
            fields,_=eta_profile(client)
            self.assertEqual(fields['college'],'ETA 测试学院')
            self.assertEqual(len([u for u,_ in calls if '/dynamic/form/' in u]),1)

    def test_wrapped_definition_uses_sibling_own_values_and_checks_identity(self):
        definition={'groupFields':[{'fields':[{'fieldCode':'xydm','fieldName':'学院'}]}]}
        for identity in ('SYNTHETIC-STUDENT','OTHER-STUDENT'):
            client,_=self.eta_client(form={'form':definition,'data':{'xh':identity,'xydm':'C','xy':'实际学院'}})
            if identity=='SYNTHETIC-STUDENT':
                fields,_=eta_profile(client);self.assertEqual(fields['college'],'实际学院')
            else:
                with self.assertRaises(CampusError):eta_profile(client)

    def test_empty_xsxg_uses_only_known_default_with_same_self_identifier(self):
        client,calls=self.eta_client(module_code='xsxg',templates={'xsxg':{},'default':FORM})
        fields,path=eta_profile(client)
        self.assertEqual(fields['college'],'ETA 测试学院')
        self.assertTrue(path.endswith('/userInfo/default.zf'))
        form_calls=[u for u,_ in calls if '/dynamic/form/' in u]
        self.assertEqual(len(form_calls),2)
        for u in form_calls:self.assertEqual(parse_qs(urlsplit(u).query)['dataId'],[eta_encode('SYNTHETIC-STUDENT')])

    def test_missing_both_forms_reports_unavailable_and_current_user_provenance(self):
        client,_=self.eta_client(module_code='xsxg',templates={'xsxg':{'unknown-private-key':'discard-value'},'default':{}})
        records,warnings=client.profiles()
        self.assertEqual(records[0]['url'],ETA_CURRENT)
        warning=next(w for w in warnings if w['code']=='PROFILE_FORM_UNAVAILABLE')
        self.assertEqual(warning['diagnostic']['reason'],'FORM_SCHEMA_MISSING')
        self.assertEqual(len(warning['diagnostic']['form_attempts']),2)
        self.assertNotIn('unknown-private-key',json.dumps(warning))
        self.assertNotIn('discard-value',json.dumps(warning))
        self.assertNotIn('SYNTHETIC-STUDENT',json.dumps(warning))

    def test_failed_default_preserves_verified_fields_and_business_session(self):
        error=CampusError('ETA_UNAVAILABLE','不可用',{'stage':'profile_eta','reason':'HTTP_STATUS','http_status':503})
        client,_=self.eta_client(module_code='xsxg',templates={'xsxg':{},'default':error})
        client.authenticated=True
        records,warnings=client.profiles()
        self.assertEqual(records[0]['data']['name'],'合成学生')
        self.assertIn('ETA_UNAVAILABLE',[w['code'] for w in warnings])
        self.assertTrue(client.authenticated);self.assertTrue(client.passport_authenticated)

    def test_wrapper_or_fallback_foreign_identity_is_rejected(self):
        for form,templates in (({'xh':'OTHER-STUDENT','form':FORM},None),
                               ({},{'xsxg':{},'default':{'xh':'OTHER-STUDENT','form':FORM}})):
            client,_=self.eta_client(form=form,module_code='xsxg',templates=templates)
            with self.assertRaises(CampusError) as caught:eta_profile(client)
            self.assertEqual(caught.exception.code,'PROFILE_IDENTITY_MISMATCH')

    def test_eta_landing_200_is_not_authentication(self):
        client, calls = self.eta_client(current=0)
        with self.assertRaises(CampusError) as caught: eta_profile(client)
        self.assertEqual(caught.exception.code, 'ETA_UNAVAILABLE')
        self.assertFalse(any('/dynamic/form/' in url for url, _ in calls))

    def test_eta_selects_only_own_default_role(self):
        roles = {'zgh': 'SYNTHETIC-STUDENT', 'xm': '合成学生', 'mrjsdm': 'student',
                 'jsList': [{'jsdm': 'teacher'}, {'jsdm': 'student'}]}
        for current in (0, {}):
            client, calls = self.eta_client(current=current, roles=roles)
            fields, _ = eta_profile(client)
            self.assertEqual(fields['student_id'], roles['zgh'])
            posts = [post for _, post in calls if post is not None]
            self.assertEqual(posts, [{'jsdm': 'student'}])

    def test_identity_mismatch_prevents_reading_another_student_form(self):
        client, calls = self.eta_client(current={'xh': 'ANOTHER-STUDENT'})
        with self.assertRaises(CampusError) as caught: eta_profile(client, 'SYNTHETIC-STUDENT')
        self.assertEqual(caught.exception.code, 'PROFILE_IDENTITY_MISMATCH')
        self.assertFalse(any('/dynamic/form/' in url for url, _ in calls))
        client, _ = self.eta_client(form={'xh': 'ANOTHER-STUDENT', 'xymc': '错误学院'})
        with self.assertRaises(CampusError): eta_profile(client)

    def test_zdbk_personal_page_comes_from_authenticated_menu(self):
        client = CampusClient()
        client.modules = [{'gnmkmc': '个人信息', 'dyym': '/jwglxt/xsxxxggl/xsgrxxwh_cxXsgrxx.html', 'gnmkdm': 'N100801'}]
        with patch.object(client, 'page_request', return_value='<input name="xymc" value="测试学院">') as page:
            fields, source = zdbk_profile(client)
        self.assertEqual(fields['college'], '测试学院')
        self.assertIn('gnmkdm=N100801', page.call_args.args[0])
        self.assertNotIn('?', source)
        client.modules[0]['dyym'] = 'https://evil.test/profile'
        self.assertIsNone(zdbk_profile(client))

    def test_eta_headers_and_safe_diagnostics(self):
        class Response(BytesIO):
            code=200; headers={}
        client = CampusClient()
        with patch.object(client.opener, 'open', return_value=Response(b'{}')) as opened:
            client.request(ETA_CURRENT)
        request = opened.call_args.args[0]
        self.assertIsNone(request.get_header('X-requested-with'))
        self.assertEqual(urlsplit(request.get_header('Referer')).hostname, 'eta.zju.edu.cn')
        with patch.object(client.opener, 'open', side_effect=TimeoutError('secret-ticket')):
            with self.assertRaises(CampusError) as caught:
                client.request(ETA_BASE+'/dynamic/form/find/userInfo/default.zf?dataId=secret-ticket')
        public = caught.exception.public()
        self.assertEqual(public['diagnostic']['stage'], 'profile_eta')
        self.assertNotIn('secret-ticket', json.dumps(public))

    def test_eta_callback_navigation_and_api_headers_are_distinct(self):
        class Response(BytesIO):
            code=200; headers={}
        for url, form, accept in ((ETA_SERVICE.replace('http:', 'https:')+'?ticket=synthetic', None, 'text/html'),
                                   (ETA_CURRENT, None, 'application/json'),
                                   (ETA_BASE+'/teacher/xtgl/login/switchRole.zf', {'jsdm':'student'}, 'application/json')):
            client = CampusClient()
            with patch.object(client.opener, 'open', return_value=Response(b'{}')) as opened:
                client.request(url, form)
            request = opened.call_args.args[0]
            self.assertIsNone(request.get_header('X-requested-with'))
            self.assertTrue(request.get_header('Accept').startswith(accept))
            self.assertEqual(request.get_header('Origin'), 'https://eta.zju.edu.cn' if form else None)

    def test_large_eta_landing_is_not_read_and_large_form_keeps_only_basic_fields(self):
        class Landing(BytesIO):
            code=200; headers={'Content-Type':'text/html','Content-Length':'9000000'}
            def read(self, *args):raise AssertionError('Navigation body should not be read')
        class Response(BytesIO):
            code=200; headers={'Content-Type':'application/json'}
        client = CampusClient(); client.passport_authenticated = True
        callback = ETA_SERVICE.replace('http:', 'https:')+'?ticket=secret-ticket'
        client.service_callback = lambda _: callback
        large_form = FORM | {'unused_dictionary': 'not-stored'*220_000}
        def open_request(request, **kwargs):
            url = request.full_url
            if url == callback:return Landing()
            if url == ETA_CURRENT:data={'xh':'SYNTHETIC-STUDENT'}
            elif url.startswith(ETA_COMMON):data={'modules':[{'formCode':'userInfo','extendCode':'default'}]}
            elif '/dynamic/form/find/userInfo/' in url:data=large_form
            else:raise AssertionError(urlsplit(url).path)
            return Response(json.dumps({'code':0,'data':data}).encode())
        with patch.object(client.opener, 'open', side_effect=open_request):
            fields, source = eta_profile(client)
        self.assertEqual(fields['college'], 'ETA 测试学院')
        self.assertNotIn('not-stored', json.dumps(fields))
        self.assertNotIn('ticket', source)

    def test_eta_over_limit_reports_cap_and_path_without_body_or_query(self):
        class Response(BytesIO):
            code=200; headers={'Content-Type':'application/json; charset=utf-8'}
        client = CampusClient()
        url = ETA_BASE+'/dynamic/form/find/userInfo/default.zf?dataId=secret-id'
        with patch.object(client.opener, 'open', return_value=Response(b'x'*8_000_001)):
            with self.assertRaises(CampusError) as caught:client.request(url)
        public = caught.exception.public()
        self.assertEqual(public['code'], 'RESPONSE_TOO_LARGE')
        self.assertEqual(public['diagnostic']['limit_bytes'], 8_000_000)
        self.assertEqual(public['diagnostic']['observed_bytes'], 8_000_001)
        self.assertEqual(public['diagnostic']['content_kind'], 'json')
        self.assertEqual(public['diagnostic']['path'], urlsplit(url).path)
        self.assertNotIn('secret-id', json.dumps(public))

    def test_eta_rejection_checked_before_body_size_or_read(self):
        class Rejected(BytesIO):
            code=901; headers={'Content-Length':'9000000'}
            def read(self, *args):raise AssertionError('Rejected body should not be read')
        client = CampusClient()
        with patch.object(client.opener, 'open', return_value=Rejected()):
            with self.assertRaises(CampusError) as caught:client.request(ETA_CURRENT)
        self.assertEqual(caught.exception.code, 'ETA_ACCESS_REJECTED')

    def test_eta_901_stops_source_and_reports_safe_status_path(self):
        class Rejected(BytesIO):
            code=901; headers={}
        client = CampusClient(); client.authenticated = client.passport_authenticated = True
        client.start_operation()
        def open_request(request, **kwargs):
            if urlsplit(request.full_url).hostname == 'zjuam.zju.edu.cn':
                raise urllib.error.HTTPError(request.full_url, 302, '',
                    {'Location': ETA_SERVICE+'?ticket=secret-ticket'}, BytesIO())
            return Rejected(b'secret-response')
        with patch.object(client.opener, 'open', side_effect=open_request) as opened:
            records, warnings = client.profiles()
        self.assertEqual(records, [])
        self.assertEqual(opened.call_count, 2)
        warning = warnings[0]
        self.assertEqual(warning['code'], 'ETA_ACCESS_REJECTED')
        self.assertEqual(warning['diagnostic']['http_status'], 901)
        self.assertEqual(warning['diagnostic']['path'], urlsplit(ETA_SERVICE).path)
        self.assertNotIn('secret-', json.dumps(warning))
        self.assertTrue(client.passport_authenticated); self.assertTrue(client.authenticated)

    def test_synthetic_eta_blocked_response_keeps_status_diagnostic(self):
        client, calls = self.eta_client()
        client.request = lambda *args, **kwargs: (922, {}, b'secret-response')
        with self.assertRaises(CampusError) as caught: eta_profile(client)
        self.assertEqual(caught.exception.diagnostic['http_status'], 922)
        self.assertNotIn('secret-', json.dumps(caught.exception.public()))

    def test_eta_expiry_does_not_expire_existing_zdbk_session(self):
        client = CampusClient(); client.authenticated = client.passport_authenticated = True
        with patch('connectors.campus.campus.eta_profile', side_effect=CampusError('SESSION_EXPIRED', '过期')):
            records, warnings = client.profiles()
        self.assertEqual(records, [])
        self.assertEqual(warnings[0]['code'], 'PROFILE_SESSION_EXPIRED')
        self.assertTrue(client.authenticated)
        with patch.object(client, 'request', return_value=(403, {}, b'rejected')):
            with self.assertRaises(CampusError) as caught: client.service_callback('eta')
        self.assertEqual(caught.exception.code, 'SERVICE_ACCESS_REJECTED')
        self.assertTrue(client.passport_authenticated)

    def test_field_provenance_priority_and_stale_cache(self):
        snapshot = {}
        merge_profile(snapshot, {'data': PROFILE, 'source': 'old', 'url': SERVICE})
        stale_profile(snapshot)
        merge_profile(snapshot, {'data': {'college': 'ETA 新学院'}, 'source': 'ETA', 'url': ETA_CURRENT})
        merge_profile(snapshot, {'data': PROFILE | {'name': '最新姓名'}, 'source': '教务', 'url': SERVICE}, protect_fresh=True)
        self.assertEqual(snapshot['profile']['data']['college'], 'ETA 新学院')
        self.assertEqual(snapshot['profile']['data']['name'], '最新姓名')
        stale_profile(snapshot)
        merge_profile(snapshot, {'data': {'name': '再次更新'}, 'source': '教务', 'url': SERVICE})
        self.assertTrue(snapshot['profile']['field_sources']['college']['stale'])
        self.assertFalse(snapshot['profile']['field_sources']['name']['stale'])
        self.assertEqual(snapshot['profile']['field_sources']['college']['source'], 'ETA')
        with self.assertRaises(CampusError):
            merge_profile(snapshot, {'data': {'student_id': 'ANOTHER-STUDENT'}, 'source': 'ETA', 'url': ETA_CURRENT})
        self.assertEqual(snapshot['profile']['data']['student_id'], PROFILE['student_id'])


class FakeProfilePassport(FakePassport):
    def __init__(self):
        super().__init__(); self.profile_calls = 0; self.profile_error = None
    def profiles(self, expected_id=''):
        self.profile_calls += 1
        if self.profile_error:
            return [], [self.profile_error.public() | {'stage': 'profile_eta'}]
        return [{'data': PROFILE | {'college': 'ETA 测试学院', 'major': 'ETA 测试专业'},
                 'source': 'ETA · 个人基本信息', 'url': ETA_BASE+'/dynamic/form/find/userInfo/default.zf'}], []
    def timetable(self, *args):
        self.queried.append(args[:2])
        return {k: PROFILE[k] if k in ('student_id', 'name') else '' for k in PROFILE_KEYS}, COURSES


class ProfileSyncTests(unittest.TestCase):
    """Migrated passport adoption + ETA profile enrichment, without the retired HTTP layer."""

    OWNER = 'local'

    def setUp(self):
        self.stack = ExitStack()
        self.tmp = self.stack.enter_context(TemporaryDirectory())
        self.store_path = Path(self.tmp) / 'campus.sqlite3'
        FakeProfilePassport.created = []
        self.manager = self.new_manager()

    def tearDown(self): self.stack.close()

    def new_manager(self):
        return CampusManager(SQLiteStore(self.store_path), client_factory=FakeProfilePassport)

    def login(self, connect_error=None):
        fake = FakeProfilePassport()
        fake.connect_error = connect_error
        fake.authenticate_passport('synthetic', 'synthetic-password', '')
        self.manager.adopt_passport(self.OWNER, fake)
        return self.manager.connect_and_sync(self.OWNER), fake

    def test_login_auto_enriches_and_resume_does_not_refetch_profile(self):
        result, fake = self.login()
        profile = result['snapshot']['profile']
        self.assertEqual(profile['data']['college'], 'ETA 测试学院')
        self.assertTrue(profile['complete'])
        self.assertEqual(profile['field_sources']['college']['source'], 'ETA · 个人基本信息')
        self.assertEqual(fake.profile_calls, 1)
        self.assertEqual(result['connector_revision'], CONNECTOR_REVISION)
        self.assertEqual(result['snapshot']['sync_revision'], CONNECTOR_REVISION)
        self.assertEqual(result['snapshot']['profile_checks'][0]['status'], 'read')
        final = self.manager.sync(self.OWNER, resume=True)
        self.assertFalse(final['auto_continue']); self.assertEqual(fake.profile_calls, 1)
        self.assertTrue(final['snapshot']['courses']['complete'])

    def test_old_snapshot_revision_is_not_rewritten_when_viewed(self):
        self.login()
        owner = self.OWNER
        snapshot = self.manager._snapshot(owner)
        snapshot.pop('sync_revision')
        self.manager._save(owner, snapshot)
        result = self.manager.status(owner)
        self.assertEqual(result['connector_revision'], CONNECTOR_REVISION)
        self.assertNotIn('sync_revision', result['snapshot'])

    def test_eta_failure_preserves_per_field_cache_and_other_sections(self):
        _, fake = self.login()
        before = self.manager.status(self.OWNER)['snapshot']['profile']['field_sources']['college']['fetched_at']
        fake.profile_error = CampusError('ETA_UNAVAILABLE', 'ETA 不可用')
        result = self.manager.sync(self.OWNER)
        profile = result['snapshot']['profile']
        self.assertEqual(profile['data']['college'], 'ETA 测试学院')
        self.assertTrue(profile['field_sources']['college']['stale'])
        self.assertEqual(profile['field_sources']['college']['fetched_at'], before)
        self.assertFalse(profile['field_sources']['name']['stale'])
        self.assertFalse(result['snapshot']['grades']['stale'])
        self.assertTrue(result['snapshot']['courses']['data'])
        self.assertTrue(result['connected'])

    def test_reported_eta_callback_timeout_retains_cache_and_does_not_stop_academics(self):
        _, fake = self.login()
        before = self.manager.status(self.OWNER)['snapshot']['profile']['field_sources']['college']['fetched_at']
        fake.profile_error = CampusError('NETWORK_ERROR', 'ETA 登录回调超时', {
            'stage': 'profile_eta', 'method': 'GET', 'host': 'eta.zju.edu.cn',
            'path': urlsplit(ETA_SERVICE).path, 'reason': 'TIMEOUT', 'elapsed_ms': 12078})
        result = self.manager.sync(self.OWNER)
        self.assertEqual(result['snapshot']['profile']['data']['college'], 'ETA 测试学院')
        self.assertEqual(result['snapshot']['profile']['field_sources']['college']['fetched_at'], before)
        self.assertTrue(result['snapshot']['profile']['field_sources']['college']['stale'])
        self.assertFalse(result['snapshot']['grades']['stale'])
        self.assertTrue(result['snapshot']['courses']['data']); self.assertTrue(result['connected'])
        failure = next(w for w in result['snapshot']['warnings'] if w['stage'] == 'profile_eta')
        self.assertEqual(failure['diagnostic']['reason'], 'TIMEOUT')

    def test_eta_works_even_when_zdbk_connection_fails(self):
        result, fake = self.login(CampusError('NETWORK_ERROR', '教务不可用'))
        self.assertFalse(result['connected']); self.assertTrue(result['passport_connected'])
        self.assertEqual(result['snapshot']['profile']['data']['college'], 'ETA 测试学院')
        self.assertEqual(fake.profile_calls, 1)
        self.assertIsNone(result['snapshot']['grades'])

    def test_cached_profile_survives_restart_and_remains_owner_isolated(self):
        self.login()
        self.manager.close()
        restarted = self.new_manager()
        try:
            cached = restarted.status(self.OWNER)
            self.assertFalse(cached['connected'])
            self.assertEqual(cached['snapshot']['profile']['data']['college'], 'ETA 测试学院')
            self.assertIsNone(restarted.status('other-owner')['snapshot']['profile'])
        finally:
            restarted.close()


if __name__ == '__main__': unittest.main()
