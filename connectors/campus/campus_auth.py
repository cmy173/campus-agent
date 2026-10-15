"""CAS authentication and reviewed service ticket exchange; no persistence."""
import base64
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, parse_qs
from .campus_common import CampusError
from .campus_network import check_url
from .campus_endpoints import CAS, BASE, SERVICE, ETA_SERVICE

class _Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.values = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.values[attrs['name']] = attrs.get('value', '')


def encrypt_password(password, modulus, exponent):
    """Match the official browser's reversed, chunked single-byte RSA protocol.

    Its legacy BigInt routine is ambiguous for code units above 255. Refuse
    unsupported passwords explicitly rather than attempt a different encoding.
    """
    if not all(isinstance(x, str) and re.fullmatch(r'[0-9a-fA-F]{1,1024}', x) for x in (modulus, exponent)):
        raise CampusError('PROTOCOL_CHANGED', '校园认证公钥格式发生变化')
    n, e = int(modulus, 16), int(exponent, 16)
    if n < 3 or e < 3:
        raise CampusError('PROTOCOL_CHANGED', '校园认证公钥无效')
    if any(ord(char) > 255 for char in password):
        raise CampusError('UNSUPPORTED_PASSWORD', '当前连接暂不支持含中文或其他多字节字符的密码，请使用官方教务页面')
    chunk_size = 2 * ((n.bit_length()-1) // 16)
    if chunk_size < 2:
        raise CampusError('PROTOCOL_CHANGED', '校园认证公钥长度不符合约定')
    reversed_bytes = bytes(ord(char) for char in reversed(password))
    chunks = []
    for offset in range(0, len(reversed_bytes), chunk_size):
        value = int.from_bytes(reversed_bytes[offset:offset+chunk_size], 'little')
        encrypted = pow(value, e, n)
        # Browser biToHex emits whole 16-bit digits, not modulus-length padding.
        width = max(4, ((encrypted.bit_length()+15)//16)*4)
        chunks.append(format(encrypted, 'x').zfill(width))
    return ' '.join(chunks)


class CampusAuthentication:
    def __init__(self, policy=None):
        super().__init__(policy)
        self.execution = ''
        self.authenticated = self.passport_authenticated = self.eta_authenticated = False
        self.modules = []
        self.term_warnings = []

    def close(self):
        super().close()
        self.execution = ''
        self.authenticated = self.passport_authenticated = self.eta_authenticated = False
        self.modules = []
        self.expected_identity = ''

    def prepare(self):
        status, _, raw = self.request(CAS + 'login')
        if status != 200:
            raise CampusError('UPSTREAM_ERROR', f'校园登录页暂不可用（HTTP {status}）')
        self.execution = _Inputs(raw.decode('utf-8', 'replace')).values.get('execution', '')
        if not self.execution:
            raise CampusError('PROTOCOL_CHANGED', '校园登录页缺少表单令牌，暂无法登录')

    def captcha(self, kind):
        url = CAS + 'kaptcha' if kind == 'cas' else BASE + 'kaptcha'
        status, headers, raw = self.request(url + '?' + urlencode({'time': int(time.time()*1000)}))
        mime = next((v for k, v in headers.items() if k.lower() == 'content-type'), '').split(';')[0].lower()
        if status in (301, 302, 303, 401, 403):
            raise CampusError('SESSION_EXPIRED', '校园会话已失效，请重新登录')
        if status != 200 or mime not in ('image/png', 'image/jpeg', 'image/gif') or len(raw) > 150_000:
            raise CampusError('CAPTCHA_UNAVAILABLE', '暂时无法获取验证码，请刷新登录流程')
        return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')

    def login(self, username, password, authcode=''):
        self.authenticate_passport(username, password, authcode)
        self.connect_zdbk()

    def authenticate_passport(self, username, password, authcode=''):
        if not self.execution:
            raise CampusError('FLOW_EXPIRED', '请先准备校园登录')
        status, _, raw = self.request(CAS + 'v2/getPubKey')
        try:
            key = json.loads(raw)
            if status != 200 or not isinstance(key, dict):
                raise ValueError()
            encrypted = encrypt_password(password, key['modulus'], key['exponent'])
        except (ValueError, KeyError, TypeError):
            raise CampusError('PROTOCOL_CHANGED', '校园认证公钥响应不符合约定') from None
        status, _, raw = self.request(CAS + 'login', {
            'username': username, 'password': encrypted, 'execution': self.execution,
            '_eventId': 'submit', 'authcode': authcode,
        })
        self.execution = ''
        # A redirect alone is not successful authentication.
        if status not in (200, 302, 303) or not any(
                c.name == 'iPlanetDirectoryPro' and c.value and not c.is_expired() for c in self.cookies_for('zjuam.zju.edu.cn')):
            diagnostic = {'stage':'cas_login', 'host':'zjuam.zju.edu.cn', 'path':'/cas/login',
                          'method':'POST', 'http_status':status, 'reason':'AUTH_NOT_CONFIRMED'}
            if status == 429:
                raise CampusError('RATE_LIMITED', '校方认证请求过于频繁，请稍后重试', diagnostic)
            if status in (403, 901, 922):
                raise CampusError('ACCESS_REJECTED', '校方认证服务拒绝本次访问，未认定为密码错误', diagnostic)
            if 500 <= status <= 599:
                raise CampusError('UPSTREAM_ERROR', '校方认证服务暂不可用，请稍后重试', diagnostic)
            if not 100 <= status <= 599:
                raise CampusError('UPSTREAM_STATUS_UNKNOWN', '认证服务返回未识别的自定义响应，未认定为密码错误或服务宕机',
                                  diagnostic|{'reason':'HTTP_CUSTOM_STATUS'})
            text = raw.decode('utf-8', 'replace')
            # The normal login page always contains a captcha field; only an
            # explicit validation hint is evidence that interaction is required.
            if re.search(r'验证码(?:错误|不正确|不能为空|失效)|(?:captcha|authcode)[^<\n]{0,40}(?:incorrect|invalid|required)', text, re.I):
                raise CampusError('CAPTCHA_REQUIRED', '校方提示验证码错误或失效，请重新填写新的验证码', diagnostic)
            # The school answered with its own credential hint. This is a real,
            # user-actionable outcome (unlike an unconfirmed redirect), so it is
            # reported distinctly instead of being folded into CAS_REJECTED.
            if re.search(r'用户名或密码错误|账号或密码错误|密码错误|用户名不存在|您输入的密码|incorrect (?:password|username)|invalid (?:password|username)|authentication failed', text, re.I):
                raise CampusError('CREDENTIAL_REJECTED', '校方提示账号或密码不正确，请核对后重新保存凭据', diagnostic)
            if re.search(r'csrf|跨站请求|请求校验失败', text, re.I):
                raise CampusError('AUTH_CONTEXT_INVALID', '认证请求上下文失效，请重新准备登录；未认定为密码错误', diagnostic)
            if re.search(r'二次认证|多因素|动态口令|短信验证码|MFA|two.factor', text, re.I):
                raise CampusError('INTERACTION_REQUIRED', '校方要求额外交互认证，请通过官方通行证页面完成', diagnostic)
            raise CampusError('CAS_REJECTED', '未能确认通行证认证成功，请重新准备登录或核对官方页面；此提示不等同于密码错误', diagnostic)
        self.passport_authenticated = True

    def service_callback(self, service_name):
        # Add future sites through reviewed adapters, never caller-supplied URLs.
        services = {'zdbk': SERVICE, 'eta': ETA_SERVICE}
        if service_name not in services:
            raise CampusError('UNSUPPORTED_SERVICE', '此校园网站尚未接入')
        if not self.passport_authenticated:
            raise CampusError('SESSION_EXPIRED', '通行证会话已失效，请重新登录')
        service = services[service_name]
        status, headers, _ = self.request(CAS + 'login?' + urlencode({'service': service}))
        if service_name == 'eta' and status in (401, 403):
            raise CampusError('SERVICE_ACCESS_REJECTED', '认证服务未允许本次 ETA 换票，保留已有教务会话')
        if status in (200, 401, 403):
            self.passport_authenticated = False
            raise CampusError('SESSION_EXPIRED', '通行证会话已失效，请重新登录；已缓存资料仍可查看')
        location = next((v for k, v in headers.items() if k.lower() == 'location'), '')
        callback = urljoin(CAS + 'login', location)
        parsed = urlsplit(callback)
        expected = urlsplit(service)
        # ETA registers an HTTP service identifier. Only the identical callback is
        # eligible for upgrade; the ticket is never transmitted over HTTP.
        if service_name == 'eta' and parsed.scheme == 'http' and parsed.netloc == expected.netloc and parsed.path == expected.path:
            callback = parsed._replace(scheme='https').geturl()
        parsed = urlsplit(check_url(callback))
        if (status not in (302, 303) or parsed.hostname != expected.hostname
                or parsed.path != expected.path or not parse_qs(parsed.query).get('ticket')):
            raise CampusError('SERVICE_LOGIN_FAILED', '校园认证未能换取对应网站的票据')
        return callback
