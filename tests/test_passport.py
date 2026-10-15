"""校园通行证（CAS）登录模块的单元测试。全部 mock 网络，不触碰真实 zjuam。"""
import json
import unittest
import urllib.error
from unittest.mock import patch

from campus_agent import passport


class FakeResponse:
    def __init__(self, status, body='', headers=None):
        self.status = status
        self._body = body.encode('utf-8')
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class FakeOpener:
    """按调用顺序返回预置响应；记录每次请求，便于断言"没有重试"。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


LOGIN_PAGE = '<form><input name="execution" value="e-abc-123"/></form>'
# 真实 CAS 用约 2048 位模数；测试同样给足够大的模数，避免正常长度的密码触发"密码过长"。
PUBKEY = json.dumps({'modulus': 'f' * 512, 'exponent': '10001'})


def opener_factory(responses):
    def build(*args, **kwargs):
        return FakeOpener(responses)
    return build


class RsaTests(unittest.TestCase):
    def test_password_to_int_is_big_endian_bytes(self):
        self.assertEqual(passport.password_to_int('A'), 65)
        self.assertEqual(passport.password_to_int('AB'), 65 * 256 + 66)

    def test_password_to_int_mirrors_utf16_high_surrogate(self):
        # JS 的 charCodeAt(0) 对 emoji 取的是高代理项，必须逐位对齐，
        # 否则含 emoji 的密码会静默对不上并被误报成"密码错误"。
        self.assertEqual(passport.password_to_int('\U0001F600'), 0xD83D)

    def test_rsa_encrypt_is_unpadded_with_hex_exponent(self):
        # 65**3 mod 0xd1 == 0xd0；指数按十六进制解析，结果按模长补零。
        self.assertEqual(passport.rsa_encrypt('A', 'd1', '3'), 'd0')

    def test_rsa_encrypt_rejects_oversized_password(self):
        with self.assertRaisesRegex(passport.PassportError, '模长'):
            passport.rsa_encrypt('way-too-long-for-this-modulus', 'd1', '3')


class LoginTests(unittest.TestCase):
    def test_success_returns_student_identity(self):
        responses = [
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(200, PUBKEY),
            FakeResponse(302, '', {'Location': 'https://zdbk.zju.edu.cn/jwglxt/?ticket=ST-xyz'}),
        ]
        with patch('urllib.request.build_opener', side_effect=opener_factory(responses)):
            identity = passport.login('3250103732', 'zju-pass')
        self.assertEqual(identity.student_id, '3250103732')

    def test_rejected_login_does_not_retry(self):
        responses = [
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(200, PUBKEY),
            FakeResponse(200, '<span id="msg">用户名或密码错误</span>'),
        ]
        opener = FakeOpener(responses)
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(passport.PassportError, '用户名或密码错误'):
                passport.login('3250103732', 'wrong-pass')
        # ① 登录页 ② 公钥 ③ 提交，共 3 次；失败后绝不再提交第 4 次（防锁号）。
        self.assertEqual(len(opener.requests), 3)

    def test_redirect_without_ticket_is_failure(self):
        responses = [
            FakeResponse(200, LOGIN_PAGE),
            FakeResponse(200, PUBKEY),
            FakeResponse(302, '', {'Location': 'https://zjuam.zju.edu.cn/cas/login'}),
        ]
        with patch('urllib.request.build_opener', side_effect=opener_factory(responses)):
            with self.assertRaisesRegex(passport.PassportError, 'service ticket'):
                passport.login('3250103732', 'zju-pass')

    def test_missing_execution_token_is_failure(self):
        responses = [FakeResponse(200, '<html>改版了</html>')]
        with patch('urllib.request.build_opener', side_effect=opener_factory(responses)):
            with self.assertRaisesRegex(passport.PassportError, 'execution'):
                passport.login('3250103732', 'zju-pass')

    def test_network_error_mentions_campus_network(self):
        responses = [urllib.error.URLError('timed out')]
        with patch('urllib.request.build_opener', side_effect=opener_factory(responses)):
            with self.assertRaisesRegex(passport.PassportError, '连不上统一身份认证'):
                passport.login('3250103732', 'zju-pass')

    def test_empty_credentials_rejected_before_any_request(self):
        with patch('urllib.request.build_opener') as build:
            with self.assertRaisesRegex(passport.PassportError, '不能为空'):
                passport.login('', 'zju-pass')
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
