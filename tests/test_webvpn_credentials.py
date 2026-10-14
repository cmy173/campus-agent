"""WebVPN long-term credentials: encryption, adapter protocol and management API."""
import json
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from campus_agent import campus_crypto, campus_sync
from campus_agent.campus_crypto import CredentialCipher, webvpn_encrypt_password
from campus_agent.web import create_app
from campus_agent.webvpn_adapter import WEBVPN_DO_LOGIN_URL, WebVPNAdapter


class CryptoTests(unittest.TestCase):
    """Stored secrets use AES-256-GCM; the WebVPN wire cipher matches the page script."""

    def test_roundtrip_and_tamper_detection(self):
        cipher = CredentialCipher('owner-a')
        blob = cipher.encrypt('s3cret-pass')
        self.assertEqual(cipher.decrypt(blob), 's3cret-pass')
        self.assertNotIn('s3cret-pass', json.dumps(blob))
        blob['ciphertext'] = blob['ciphertext'][:-4] + ('AAAA' if blob['ciphertext'][-4:] != 'AAAA' else 'BBBB')
        with self.assertRaises(ValueError):
            cipher.decrypt(blob)

    def test_keys_are_per_owner(self):
        self.assertNotEqual(CredentialCipher('owner-a').master_key,
                            CredentialCipher('owner-b').master_key)

    def test_webvpn_wire_format_matches_page_script(self):
        # encrypt(text,key,iv): pad with '0' to 16, AES-128-CFB(key, iv=key),
        # output hex(iv) + hex(ciphertext)[:len(text)*2]
        for password in ('abc', 'x' * 16, 'my-pass-1234567'):
            got = webvpn_encrypt_password(password)
            self.assertEqual(len(got), 32 + 2 * len(password))
            self.assertEqual(got[:32], b'wrdvpnisawesome!'.hex())
            self.assertEqual(got, webvpn_encrypt_password(password))  # deterministic

    def test_webvpn_padding_uses_zero_digits(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        password = 'abc'
        padded = password + '0' * 13
        key = b'wrdvpnisawesome!'
        encryptor = Cipher(algorithms.AES(key), modes.CFB(key)).encryptor()
        ciphertext = encryptor.update(padded.encode()) + encryptor.finalize()
        self.assertEqual(webvpn_encrypt_password(password),
                         key.hex() + ciphertext[:len(password)].hex())


class FakeResponse:
    def __init__(self, body, url=''):
        self.body, self.url = body, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body[:limit] if limit and limit > 0 else self.body

    def geturl(self):
        return self.url


class AdapterTests(unittest.TestCase):
    """Login protocol and proxied URL construction."""

    def login(self, responses):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = responses
            adapter = WebVPNAdapter()
            adapter.login('3250103732', 'webvpn-pass')
            return opener, adapter

    def test_login_posts_form_encoded_encrypted_password(self):
        opener, adapter = self.login([FakeResponse(b'<html>login</html>'),
                                      FakeResponse(json.dumps({'success': True, 'url': '/'}).encode())])
        requests = opener.return_value.open.call_args_list
        self.assertEqual(requests[0].args[0].full_url, 'https://webvpn.zju.edu.cn/login')
        login_request = requests[1].args[0]
        self.assertEqual(login_request.full_url, WEBVPN_DO_LOGIN_URL)
        self.assertIn('application/x-www-form-urlencoded',
                      login_request.get_header('Content-type'))
        body = login_request.data.decode('ascii')
        self.assertIn('user_name=3250103732', body)
        self.assertIn('password=' + webvpn_encrypt_password('webvpn-pass'), body)
        self.assertNotIn('webvpn-pass', body)
        self.assertTrue(adapter.authenticated)

    def test_login_failure_maps_error_codes_without_echoing_secrets(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [
                FakeResponse(b'<html></html>'),
                FakeResponse(json.dumps({'success': False, 'error': '1'}).encode())]
            adapter = WebVPNAdapter()
            with self.assertRaises(ValueError) as caught:
                adapter.login('3250103732', 'webvpn-pass')
        self.assertIn('密码错误', str(caught.exception))
        self.assertNotIn('webvpn-pass', str(caught.exception))

    def test_proxy_url_uses_known_host_tokens_and_rejects_others(self):
        adapter = WebVPNAdapter()
        adapter.authenticated = True
        url = adapter.proxy_url('https://zdbk.zju.edu.cn/jwglxt/x?a=1')
        self.assertTrue(url.startswith('https://webvpn.zju.edu.cn/https/'))
        self.assertIn('/jwglxt/x?a=1', url)
        with self.assertRaises(ValueError):
            adapter.proxy_url('https://unknown.zju.edu.cn/')


class ApiTests(unittest.TestCase):
    """Credential management endpoints keep secrets server-side."""

    def setUp(self):
        self.stack = ExitStack()
        base = Path(os.environ.get('CAMPUS_TEST_TMP', '.test-tmp')).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = self.stack.enter_context(TemporaryDirectory(dir=base))
        self.app = create_app(self.tmp)
        self.store = self.app.state.store
        self.client = self.stack.enter_context(TestClient(self.app, base_url='http://127.0.0.1'))
        self.client.headers['X-Campus-Request'] = '1'
        account = self.client.post('/api/auth/register',
                                   json={'username': 'alice', 'password': 'test-only-pass'}).json()
        self.client.headers['X-CSRF-Token'] = account['csrf']
        with self.store.connect() as db:
            self.owner = db.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]

    def tearDown(self):
        self.stack.close()

    def test_status_before_save_reports_no_credentials(self):
        status = self.client.get('/api/campus/webvpn/status').json()
        self.assertFalse(status['has_credentials'])

    def test_save_status_patch_and_delete_lifecycle(self):
        saved = self.client.post('/api/campus/webvpn/credentials', json={
            'webvpn_username': '3250103732', 'webvpn_password': 'webvpn-pass',
            'zjuam_password': 'cas-pass'})
        self.assertEqual(saved.status_code, 200)

        status = self.client.get('/api/campus/webvpn/status').json()
        self.assertTrue(status['has_credentials'])
        self.assertEqual(status['webvpn_username'], '3250103732')
        self.assertEqual(status['zjuam_username'], '3250103732')
        self.assertTrue(status['auto_sync'])
        self.assertEqual(status['sync_interval_minutes'], 30)

        with self.store.connect() as db:
            row = db.execute('SELECT * FROM campus_credentials WHERE owner=?', (self.owner,)).fetchone()
        raw = json.dumps(dict(row))
        self.assertNotIn('webvpn-pass', raw)
        self.assertNotIn('cas-pass', raw)
        cipher = CredentialCipher(self.owner)
        self.assertEqual(cipher.decrypt(json.loads(row['webvpn_password_encrypted'])), 'webvpn-pass')
        self.assertEqual(cipher.decrypt(json.loads(row['zjuam_password_encrypted'])), 'cas-pass')

        patched = self.client.patch('/api/campus/webvpn/sync-settings',
                                    json={'auto_sync': False, 'sync_interval_minutes': 45})
        self.assertEqual(patched.status_code, 200)
        status = self.client.get('/api/campus/webvpn/status').json()
        self.assertFalse(status['auto_sync'])
        self.assertEqual(status['sync_interval_minutes'], 45)
        self.assertEqual(self.client.patch('/api/campus/webvpn/sync-settings',
                                           json={'sync_interval_minutes': 1}).status_code, 400)

        deleted = self.client.delete('/api/campus/webvpn/credentials')
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(self.client.get('/api/campus/webvpn/status').json()['has_credentials'])

    def test_sync_requires_credentials(self):
        self.assertEqual(self.client.post('/api/campus/webvpn/sync').status_code, 404)
        self.assertEqual(self.client.patch('/api/campus/webvpn/sync-settings',
                                           json={'auto_sync': True}).status_code, 404)

    def test_scheduler_syncs_notices_and_records_status(self):
        self.client.post('/api/campus/webvpn/credentials', json={
            'webvpn_username': '3250103732', 'webvpn_password': 'webvpn-pass'})

        class FakeAdapter:
            def __init__(self):
                self.session_expiry = None

            def probe(self):
                return False

            def login(self, username, password):
                self.logged_in = (username, password)

            def get_cookies_dict(self):
                return [{'name': 'route', 'value': 'x', 'domain': 'webvpn.zju.edu.cn',
                         'path': '/', 'secure': True, 'expires': None}]

        class FakeResult:
            error = None

        service = self.app.state.campus_sync
        calls = []
        fake = FakeAdapter()
        with patch.object(campus_sync, 'WebVPNAdapter', return_value=fake), \
                patch.object(service.campus_service, 'sync',
                             side_effect=lambda *a, **k: calls.append(a) or FakeResult()):
            with self.store.connect() as db:
                row = dict(db.execute('SELECT * FROM campus_credentials WHERE owner=?',
                                      (self.owner,)).fetchone())
            service._sync_user(row)

        # The scheduler decrypts the stored secret and logs in with it.
        self.assertEqual(fake.logged_in, ('3250103732', 'webvpn-pass'))
        self.assertEqual(calls[0][0], 'college_notices')
        with self.store.connect() as db:
            row = db.execute('SELECT last_sync_status, webvpn_cookies FROM campus_credentials WHERE owner=?',
                             (self.owner,)).fetchone()
        self.assertEqual(row['last_sync_status'], 'success')
        self.assertIn('route', row['webvpn_cookies'])


if __name__ == '__main__':
    unittest.main()
