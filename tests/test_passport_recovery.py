"""CAS cookie and safe error regression tests; no real credentials or login."""
from email.message import Message
from http.cookiejar import CookieJar
import json
import unittest
from unittest.mock import patch
from urllib.request import Request

from connectors.campus.campus import CampusClient, CampusError, CAS


def issue_cookie(client, host, value, name='_csrf', domain='.zju.edu.cn', path='/'):
    class Response:
        def info(self):
            headers=Message()
            headers.add_header('Set-Cookie', f'{name}={value}; Domain={domain}; Path={path}; Secure; HttpOnly')
            return headers
    client.jar.extract_cookies(Response(),Request('https://'+host+path))


class PassportRecoveryProtocolTests(unittest.TestCase):
    def test_parent_domain_cookies_are_bound_to_issuer_and_do_not_overwrite_other_sites(self):
        client=CampusClient()
        for host,label in [('zjuam.zju.edu.cn','cas'),('eta.zju.edu.cn','eta'),('zdbk.zju.edu.cn','zdbk')]:
            issue_cookie(client,host,'synthetic-'+label)
        self.assertEqual(len(list(client.jar)),3)
        for host,label in [('zjuam.zju.edu.cn','cas'),('eta.zju.edu.cn','eta'),('zdbk.zju.edu.cn','zdbk')]:
            request=Request('https://'+host+'/cas/login')
            client.jar.add_cookie_header(request)
            self.assertEqual(request.get_header('Cookie'),'_csrf=synthetic-'+label)
        issue_cookie(client,'zjuam.zju.edu.cn','synthetic-invalid',name='unsafe',domain='.evil.example')
        self.assertEqual(len(list(client.jar)),3)

    def test_correct_credential_protocol_keeps_csrf_from_prepare_through_password_submission(self):
        client=CampusClient()
        submissions=[]
        def request(url,form=None):
            if form is None and url == CAS+'login':
                issue_cookie(client,'zjuam.zju.edu.cn','synthetic-csrf')
                return 200,{},b'<input name="execution" value="synthetic-execution">'
            if url.endswith('getPubKey'):
                return 200,{},json.dumps({'modulus':'c'*128,'exponent':'10001'}).encode()
            probe=Request(url)
            client.jar.add_cookie_header(probe)
            self.assertIn('_csrf=synthetic-csrf',probe.get_header('Cookie') or '')
            submissions.append(form)
            issue_cookie(client,'zjuam.zju.edu.cn','synthetic-master','iPlanetDirectoryPro')
            return 302,{},b''
        client.request=request
        client.prepare()
        client.authenticate_passport('synthetic-student','synthetic-correct-password','1234')
        self.assertTrue(client.passport_authenticated)
        self.assertEqual(len(submissions),1)
        self.assertNotEqual(submissions[0]['password'],'synthetic-correct-password')
        for host in ('eta.zju.edu.cn','zdbk.zju.edu.cn'):
            probe=Request('https://'+host+'/')
            client.jar.add_cookie_header(probe)
            self.assertFalse(probe.has_header('Cookie'))

    def test_rejected_auth_does_not_blame_password_or_leak_body_and_classifies_status(self):
        cases=[(403,b'private secret body','ACCESS_REJECTED'),(429,b'ticket=SECRET','RATE_LIMITED'),
               (921,b'{"message":"SECRET"}','UPSTREAM_STATUS_UNKNOWN'),
               (500,b'Cookie=SECRET','UPSTREAM_ERROR'),(200,'验证码不正确'.encode(),'CAPTCHA_REQUIRED'),
               (200,b'csrf validation failed; SECRET','AUTH_CONTEXT_INVALID'),
               (200,'需要二次认证'.encode(),'INTERACTION_REQUIRED'),
               (200,'<input name="authcode">验证码 <input name="execution">'.encode(),'CAS_REJECTED')]
        for status,body,code in cases:
            with self.subTest(status=status,code=code):
                client=CampusClient();client.execution='synthetic'
                with patch.object(client,'request',side_effect=[(200,{},json.dumps({'modulus':'c'*128,'exponent':'10001'}).encode()),(status,{},body)]) as request:
                    with self.assertRaises(CampusError) as caught:
                        client.authenticate_passport('synthetic','synthetic-password')
                self.assertEqual(caught.exception.code,code)
                self.assertNotIn('SECRET',json.dumps(caught.exception.public()))
                self.assertFalse(client.passport_authenticated)
                self.assertEqual(request.call_count,2)  # One public key, one password submission.


if __name__ == '__main__':unittest.main()
