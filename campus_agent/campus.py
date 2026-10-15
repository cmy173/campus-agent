"""Local, opt-in undergraduate CAS connector. No passwords or cookies on disk.

Protocol references and verification boundaries: docs/campus-login.md.
Only the fixed CAS, ZDBK and ETA services below can be contacted.
"""
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlencode, urljoin, urlsplit, parse_qs
import base64
import hashlib
import http.client
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from .core import TZ
from .academic_defaults import academic_year, semester_order, SEMESTER_PATTERN, fallback_terms, resolve_calendar, calendar_family, merge_term_catalog
from .campus_profiles import ETA_SERVICE, PROFILE_KEYS, basic_fields, eta_profile, zdbk_profile

CAS = 'https://zjuam.zju.edu.cn/cas/'
BASE = 'https://zdbk.zju.edu.cn/jwglxt/'
SERVICE = BASE + 'xtgl/login_ssologin.html'
MENU = BASE + 'xtgl/index_cxLeafGnmkdm.html'
TIMETABLE = BASE + 'kbcx/xskbcx_cxXsKb.html'
TIMETABLE_INDEX = BASE + 'kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N253508&layout=default'
GRADES = BASE + 'cxdy/xscjcx_cxXscjIndex.html?doType=query&gnmkdm=N5083'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36 CampusAgent/0.3'
CONNECTOR_REVISION = '2026-10-10.timetable-cache'

_REQUEST_STAGES = {
    ('zjuam.zju.edu.cn', '/cas/login'): ('cas_login', '校园认证'),
    ('zjuam.zju.edu.cn', '/cas/v2/getPubKey'): ('cas_key', '认证公钥'),
    ('zjuam.zju.edu.cn', '/cas/kaptcha'): ('cas_captcha', '登录验证码'),
    ('zdbk.zju.edu.cn', urlsplit(SERVICE).path): ('service_login', '教务登录'),
    ('zdbk.zju.edu.cn', urlsplit(MENU).path): ('menu', '教务菜单'),
    ('zdbk.zju.edu.cn', urlsplit(TIMETABLE_INDEX).path): ('terms', '学期列表'),
    ('zdbk.zju.edu.cn', urlsplit(TIMETABLE).path): ('timetable', '课表'),
    ('zdbk.zju.edu.cn', urlsplit(GRADES).path): ('grades', '成绩'),
    ('zdbk.zju.edu.cn', '/jwglxt/kaptcha'): ('timetable_captcha', '教务验证码'),
}
_JSON_PATHS = {urlsplit(url).path for url in (MENU, TIMETABLE, GRADES)}


def _transport_failure(exc):
    """Classify by type/code only: exception text may contain proxy secrets or tickets."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'TLS_CERTIFICATE', 'TLS 证书校验失败；请检查系统时间、证书信任及代理配置'
    if isinstance(reason, ssl.SSLError):
        return 'TLS_HANDSHAKE', 'TLS 连接失败或被中断；请核对同一网络下官方教务网页是否可用'
    if isinstance(reason, TimeoutError):
        return 'TIMEOUT', '请求超时；请核对官方教务网页是否可用，再手动重试'
    if isinstance(reason, socket.gaierror):
        return 'DNS', '域名解析失败；请检查本机 DNS 和网络连接'
    if isinstance(reason, (http.client.HTTPException, EOFError)):
        return 'HTTP_INTERRUPTED', 'HTTP 响应中断或格式异常；本次未确认查询结果'
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
        return 'CONNECTION_RESET', '连接被中断；请核对学校服务、网络及代理状态'
    if isinstance(reason, ConnectionRefusedError):
        return 'CONNECTION_REFUSED', '连接被拒绝；请核对学校服务、网络及代理状态'
    return 'NETWORK', '连接失败；请检查本机网络及代理，教务网可能需要校园网或学校 VPN'


class CampusError(Exception):
    def __init__(self, code, message, diagnostic=None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic

    def public(self):
        result = {'code': self.code, 'message': str(self)}
        if self.diagnostic:
            result['diagnostic'] = self.diagnostic
        return result


class _Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.values = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.values[attrs['name']] = attrs.get('value', '')


class _Text(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.parts = []
        self.feed(text)

    def handle_data(self, data):
        self.parts.append(data)


class _TermOptions(HTMLParser):
    """Read the authenticated page's actual options, never guess enrollment years."""
    def __init__(self, text):
        super().__init__()
        self.options = {'xnm': [], 'xqm': []}
        self.select = None
        self.option = None
        self.feed(text)
        self.finish_option()

    def finish_option(self):
        if self.select and self.option is not None:
            self.options[self.select].append(self.option)
        self.option = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.finish_option()
            self.select = next((attrs.get(key) for key in ('id', 'name')
                                if attrs.get(key) in self.options), None)
        elif tag == 'option' and self.select:
            # HTML permits an omitted </option>; the next option implicitly closes it.
            self.finish_option()
            self.option = {'value': attrs.get('value', ''), 'label': '', 'selected': 'selected' in attrs}

    def handle_data(self, data):
        if self.option is not None:
            self.option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'option' and self.select and self.option is not None:
            self.finish_option()
        elif tag == 'select':
            self.finish_option()
            self.select = self.option = None


def plain(value):
    if value is None:
        return ''
    return unescape(''.join(_Text(str(value)).parts)).strip()[:500]


def decode_page(raw, headers):
    """Preserve school option characters; never replace undecodable query values."""
    content_type=next((str(v) for k,v in headers.items() if k.lower()=='content-type'),'')
    meta=raw[:4096].decode('ascii','ignore')
    declared=re.search(r'charset\s*=\s*[\"\']?([A-Za-z0-9_-]+)',content_type,re.I)
    if not declared:declared=re.search(r'charset\s*=\s*[\"\']?([A-Za-z0-9_-]+)',meta,re.I)
    aliases={'utf-8':'utf-8-sig','utf8':'utf-8-sig','gbk':'gb18030','gb2312':'gb18030','gb18030':'gb18030'}
    first=aliases.get(declared[1].lower()) if declared else None
    for encoding in dict.fromkeys([first,'utf-8-sig','gb18030']):
        if not encoding:continue
        try:return raw.decode(encoding).strip(),encoding
        except UnicodeError:pass
    raise CampusError('PROTOCOL_CHANGED','校园页面编码无法识别，未用损坏字符查询',{'reason':'TEXT_ENCODING'})


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


def check_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise CampusError('UNSAFE_REDIRECT', '校园服务返回了无效的跳转地址') from None
    if (parsed.scheme != 'https' or parsed.hostname not in ('zjuam.zju.edu.cn', 'zdbk.zju.edu.cn', 'eta.zju.edu.cn')
            or port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        raise CampusError('UNSAFE_REDIRECT', '校园服务返回了不受支持的跳转地址')
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _CampusCookiePolicy(DefaultCookiePolicy):
    def return_ok(self, cookie, request):
        if cookie.name.lower() == 'iplanetdirectorypro' and urlsplit(request.full_url).hostname != 'zjuam.zju.edu.cn':
            return False
        return super().return_ok(cookie, request)


class CampusClient:
    def __init__(self):
        self.jar = CookieJar(policy=_CampusCookiePolicy())
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())
        self.execution = ''
        self.authenticated = False
        self.passport_authenticated = False
        self.eta_authenticated = False
        self.deadline = float('inf')
        self.last_request = 0
        self.term_warnings = []
        self.modules = []

    def start_operation(self):
        self.deadline = time.monotonic() + 45

    def close(self):
        self.jar.clear()
        self.execution = ''
        self.authenticated = False
        self.passport_authenticated = False
        self.eta_authenticated = False
        self.modules = []

    def request_navigation(self, url):
        """Consume status/cookies/Location; an SSO landing body is not data."""
        return self.request(url, read_body=False)

    def request(self, url, form=None, *, read_body=True):
        check_url(url)
        parsed = urlsplit(url)
        stage, label = _REQUEST_STAGES.get((parsed.hostname, parsed.path), ('campus', '校园服务'))
        if parsed.hostname == 'eta.zju.edu.cn':
            stage, label = 'profile_eta', 'ETA 基本信息'
        elif parsed.path.startswith(('/jwglxt/xsxxxggl/', '/jwglxt/xjgl/')):
            stage, label = 'profile_zdbk', '教务基本信息'
        # Never retain query strings, form contents, request/response headers, or bodies.
        diagnostic = {'stage': stage, 'method': 'GET' if form is None else 'POST', 'host': parsed.hostname}
        if stage != 'campus':
            diagnostic['path'] = parsed.path
        wait = max(0, self.last_request + .5 - time.monotonic())
        if wait:
            time.sleep(wait)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CampusError('OPERATION_TIMEOUT', label + '：本次操作已超过时限，请稍后手动重试',
                              diagnostic | {'reason': 'OPERATION_DEADLINE'})
        self.last_request = time.monotonic()
        started = self.last_request
        headers = {'User-Agent': USER_AGENT, 'Accept': '*/*', 'Referer': BASE + 'xtgl/index_initMenu.html'}
        if parsed.hostname == 'eta.zju.edu.cn':
            # The ETA frontend uses Axios without X-Requested-With. A CAS
            # callback is a navigation, not an AJAX/data request.
            is_api = parsed.path.startswith('/zftal-xgxt-web/') and parsed.path != urlsplit(ETA_SERVICE).path
            headers.update({'Referer': 'https://eta.zju.edu.cn/' if is_api else 'https://zjuam.zju.edu.cn/',
                            'Accept': 'application/json, text/plain, */*' if is_api else
                                      'text/html, application/xhtml+xml, */*; q=0.8'})
        data = None
        if form is not None:
            data = urlencode(form).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
            if parsed.hostname == 'eta.zju.edu.cn':
                headers['Origin'] = 'https://eta.zju.edu.cn'
            if parsed.hostname == 'zdbk.zju.edu.cn' and parsed.path in _JSON_PATHS:
                headers['X-Requested-With'] = 'XMLHttpRequest'
                headers['Accept'] = 'application/json, text/javascript, */*; q=0.01'
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            try:
                request_timeout=20 if stage in ('timetable','profile_eta') else 12
                response = self.opener.open(req, timeout=min(request_timeout, remaining))
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if parsed.hostname == 'eta.zju.edu.cn' and response.code in (901, 922, 429):
                    reason = 'HTTP_RATE_LIMIT' if response.code == 429 else 'HTTP_ACCESS_REJECTED'
                    raise CampusError('ETA_ACCESS_REJECTED',
                        f'ETA 返回访问拒绝响应（HTTP {response.code}），已停止该来源请求；保留缓存并继续教务查询。可在 ETA 官网核对访问情况',
                        diagnostic | {'reason': reason, 'http_status': response.code,
                                      'elapsed_ms': round((time.monotonic()-started)*1000)})
                raw = b''
                if read_body:
                    # ETA forms include option dictionaries and an encoded envelope.
                    # The larger cap applies only to its data APIs, not other sites.
                    limit = 8_000_000 if parsed.hostname == 'eta.zju.edu.cn' and is_api else 2_000_000
                    raw = response.read(limit+1)
                    if len(raw) > limit:
                        mime = next((v for k,v in response.headers.items() if k.lower() == 'content-type'), '').split(';')[0].lower()
                        kind = 'json' if mime == 'application/json' else 'html' if mime in ('text/html','application/xhtml+xml') else 'text' if mime.startswith('text/') else 'other'
                        raise CampusError('RESPONSE_TOO_LARGE', label + f'：接口响应超过 {limit//1_000_000} MB 上限，已保留缓存',
                            diagnostic | {'reason': 'RESPONSE_LIMIT', 'http_status': response.code, 'limit_bytes': limit,
                                          'observed_bytes': len(raw), 'content_kind': kind,
                                          'elapsed_ms': round((time.monotonic()-started)*1000)})
                return response.code, dict(response.headers), raw
        except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError) as exc:
            reason, message = _transport_failure(exc)
            diagnostic |= {'reason': reason, 'elapsed_ms': round((time.monotonic()-started)*1000)}
            raise CampusError('NETWORK_ERROR', label + '：' + message, diagnostic) from None

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
                c.name == 'iPlanetDirectoryPro' and c.value and not c.is_expired() for c in self.jar):
            text = raw.decode('utf-8', 'replace')
            if '验证码' in text or 'captcha' in text.lower():
                raise CampusError('CAS_REJECTED', '校园认证未通过，请核对账号、密码及验证码后重新准备登录')
            raise CampusError('CAS_REJECTED', '校园认证未通过，请在官方登录页核对账号状态后重试')
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

    def connect_zdbk(self):
        self.authenticated = False
        callback = self.service_callback('zdbk')
        for _ in range(8):
            status, headers, raw = self.request(callback)
            if status in (301, 302, 303, 307, 308):
                location = next((v for k, v in headers.items() if k.lower() == 'location'), '')
                callback = check_url(urljoin(callback, location))
                if not location or urlsplit(callback).hostname != 'zdbk.zju.edu.cn':
                    raise CampusError('SERVICE_LOGIN_FAILED', '教务网未建立有效会话')
                continue
            if status != 200:
                raise CampusError('SERVICE_LOGIN_FAILED', '教务网登录暂不可用')
            break
        else:
            raise CampusError('SERVICE_LOGIN_FAILED', '教务网登录跳转次数超限')
        # Probe an authenticated read endpoint; JS patch/landing pages are insufficient.
        menu = self.json_request(MENU, {'doType': ''})
        if not isinstance(menu, dict) or not isinstance(menu.get('result'), list) or not menu['result']:
            raise CampusError('UNSUPPORTED_ACCOUNT', '未能验证本科教务网权限；当前连接仅支持本科生')
        # Keep the CAS master session in memory; only service tickets are single-use.
        # The cookie policy prevents sending the master cookie to service hosts.
        self.authenticated = True
        self.modules = menu['result']

    def profiles(self, expected_id=''):
        """Independent, bounded sources. An ETA failure does not expire ZDBK."""
        records, warnings = [], []
        deadline = self.deadline
        self.deadline = min(deadline, time.monotonic()+35)
        try:
            for adapter, source, stage in ((zdbk_profile, '本科教务网 · 个人信息', 'profile_zdbk'),
                                            (eta_profile, 'ETA · 个人基本信息', 'profile_eta')):
                if adapter == eta_profile and not (self.passport_authenticated or getattr(self,'eta_authenticated',False)):
                    continue
                try:
                    result = adapter(self, expected_id) if adapter == eta_profile else adapter(self)
                    if result:
                        fields, url = result
                        identity = fields.get('student_id')
                        if expected_id and identity and expected_id != identity:
                            raise CampusError('PROFILE_IDENTITY_MISMATCH', '个人信息来源的学号不一致，未合并资料')
                        expected_id = expected_id or identity or ''
                        records.append({'data': fields, 'source': source, 'url': url} |
                            ({'invalidated_fields':getattr(self,'eta_invalidated_fields',[]),
                              'field_urls':getattr(self,'eta_field_urls',{})} if adapter==eta_profile else {}))
                        if adapter == eta_profile:
                            warnings.extend(getattr(self,'eta_followup_warnings',[]))
                            missing = [k for k in PROFILE_KEYS if not fields.get(k)]
                            if missing:
                                missing_form=not getattr(self,'eta_form_available',True)
                                warnings.append({'code':'PROFILE_FORM_UNAVAILABLE' if missing_form else 'PROFILE_PARTIAL','stage':stage,
                                    'message':'ETA 身份已验证，但未取得可解析的个人基本信息表单；保留已验证信息和缓存' if missing_form else
                                              'ETA 本人信息已读取，但部分基本字段仍未取得；详见同步诊断',
                                    'diagnostic':getattr(self,'eta_field_diagnostic',{})})
                except CampusError as exc:
                    # Business sessions are independent: failure of this adapter
                    # must not discard grades or stop the timetable query.
                    warning = exc.public() | {'stage': stage}
                    if exc.code == 'SESSION_EXPIRED':
                        warning.update(code='PROFILE_SESSION_EXPIRED', message='基本信息来源会话失效，保留缓存；其他教务查询继续')
                    warnings.append(warning)
        finally:
            self.deadline = deadline
        return records, warnings

    def page_request(self, url, form=None):
        status, headers, raw = self.request(url, form)
        parsed=urlsplit(url)
        stage=_REQUEST_STAGES.get((parsed.hostname,parsed.path),('campus',''))[0]
        diagnostic={'stage':stage,'method':'GET' if form is None else 'POST','host':parsed.hostname,'http_status':status}
        if stage!='campus':diagnostic['path']=parsed.path
        if stage=='timetable' and isinstance(form,dict):
            if re.fullmatch(SEMESTER_PATTERN,str(form.get('xqm',''))):diagnostic['query_semester']=form['xqm']
            if academic_year(form.get('xnm','')):diagnostic['query_year']=str(form['xnm'])
        mime=next((str(v) for k,v in headers.items() if k.lower()=='content-type'),'').split(';')[0].lower()
        diagnostic['content_kind']='json' if mime=='application/json' else 'html' if mime in ('text/html','application/xhtml+xml') else 'other'
        if status in (301,302,303,307,308,401):
            raise CampusError('SESSION_EXPIRED','校园会话已失效，请手动重新登录',diagnostic|{'reason':'HTTP_AUTH'})
        if status in (403,429,901,922):
            raise CampusError('ACCESS_REJECTED',f'校园服务拒绝本次请求（HTTP {status}），请使用官方页面核对',diagnostic|{'reason':'HTTP_ACCESS'})
        if status!=200:
            raise CampusError('UPSTREAM_ERROR',f'校园查询暂不可用（HTTP {status}）',diagnostic|{'reason':'HTTP_STATUS'})
        try:text,encoding=decode_page(raw,headers)
        except CampusError as exc:
            raise CampusError(exc.code,str(exc),diagnostic|{'reason':'TEXT_ENCODING'}) from None
        self.page_encoding=encoding
        if re.search(r'name=[\"\']execution[\"\']|/cas/login|统一身份认证|浙大通行证', text):
            raise CampusError('SESSION_EXPIRED', '校园会话已失效，请手动重新登录',diagnostic|{'reason':'LOGIN_PAGE'})
        if 'captcha_error' in text:
            raise CampusError('CAPTCHA_REQUIRED', '教务网需要验证码，请填写后重新同步',diagnostic|{'reason':'CAPTCHA_HINT'})
        if '没有访问权限' in text or '无访问权限' in text:
            raise CampusError('ACCESS_REJECTED', '教务网返回“没有访问权限”，请核对官方页面权限与请求适配；未认定为会话失效')
        self.last_page_diagnostic=diagnostic|{'page_encoding':encoding}
        return text

    def json_request(self, url, form):
        text = self.page_request(url, form)
        try:
            value = json.loads(text)
        except ValueError:
            raise CampusError('PROTOCOL_CHANGED', '校园查询返回了非 JSON 内容，无法确认结果',
                              getattr(self,'last_page_diagnostic',{})|{'reason':'NON_JSON'}) from None
        if isinstance(value, dict) and (value.get('success') is False or
                value.get('error') not in (None,False,0,'','0','false','False')):
            parsed=urlsplit(url)
            stage=_REQUEST_STAGES.get((parsed.hostname,parsed.path),('campus',''))[0]
            error=value.get('error')
            diagnostic={'stage':stage,'method':'POST','host':parsed.hostname,'reason':'UPSTREAM_REJECTED',
                'http_status':200,'upstream_flags':{'success_false':value.get('success') is False,
                    'error_kind':'object' if isinstance(error,dict) else 'list' if isinstance(error,list)
                    else 'string' if isinstance(error,str) else 'boolean' if isinstance(error,bool)
                    else 'missing' if error is None else 'number' if isinstance(error,(int,float)) else 'other'},
                'response_shape':{k:'list' if isinstance(value.get(k),list) else 'missing' if k not in value else 'other'
                                  for k in ('kbList','items','xsxx')}}
            if stage!='campus':diagnostic['path']=parsed.path
            if stage=='timetable' and re.fullmatch(SEMESTER_PATTERN,str(form.get('xqm',''))):
                diagnostic['query_semester']=form['xqm']
                if academic_year(form.get('xnm','')):diagnostic['query_year']=str(form['xnm'])
            messages=' '.join(str(value.get(k,'')) for k in ('msg','message','error'))
            if re.search(r'验证码|captcha',messages,re.I):
                raise CampusError('CAPTCHA_REQUIRED','教务查询要求验证码，请填写后再同步',diagnostic|{'reason':'CAPTCHA_HINT'})
            if re.search(r'无访问权限|没有访问权限|权限不足',messages):
                raise CampusError('ACCESS_REJECTED','教务查询提示权限不足，已保留缓存',diagnostic|{'reason':'PERMISSION_HINT'})
            raise CampusError('UPSTREAM_ERROR', '校园查询返回错误，未更新本地数据',diagnostic)
        return value

    def terms(self):
        self.term_warnings = []
        options = _TermOptions(self.page_request(TIMETABLE_INDEX)).options
        years, semesters, unsupported, shapes = {}, {}, [], []
        for option in options['xnm']:
            value = option['value']
            year = academic_year(value)
            if year:
                years[year] = option
        for option in options['xqm']:
            value = option['value']
            label = re.sub(r'\s+', '', plain(option['label'])).strip(' -—')
            # Ignore explicit aggregate/placeholder options; never query them as one semester.
            if not value or value in ('0', '-1') or re.fullmatch(
                    r'(?:请选择(?:学期)?|选择学期|全部(?:学期|学年)?|所有(?:学期|学年)?|全学年|全年|全学期)', label):
                continue
            # The school's literal option is already the query contract. Numeric codes are
            # opaque identifiers, not a reason to guess/rewrite their calendar semantics.
            if re.fullmatch(SEMESTER_PATTERN, value):
                semesters[value] = option
            else:
                unsupported.append(value if re.fullmatch(r'-?\d{1,4}', value) else '<非数值编码>')
                # Describe an unfamiliar option without publishing a token or label.
                shapes.append({'length': min(len(value), 128), 'has_pipe': '|' in value,
                               'has_season': any(c in value for c in '秋冬春夏'), 'selected': option['selected'],
                               'components': [{'length': min(len(part),128),
                                   'kind': 'digits' if re.fullmatch(r'[0-9]+',part) else
                                           'letters' if re.fullmatch(r'[A-Za-z]+',part) else
                                           'empty' if not part else 'replacement' if '\ufffd' in part else
                                           'cjk' if re.fullmatch(r'[\u4e00-\u9fff]+',part) else 'other'} for part in value.split('|')[:4]],
                               'page_encoding':getattr(self,'page_encoding','unknown')})
        if unsupported:
            self.term_warnings = [{'code': 'UNSUPPORTED_TERM_OPTIONS', 'stage': 'terms',
                'message': '部分学期选项暂无法查询（' + '、'.join(sorted(set(unsupported))) +
                           '）；继续同步可查询学期，课表范围不标记为完整',
                'diagnostic': {'stage': 'terms', 'reason': 'UNSUPPORTED_TERM_OPTIONS',
                               'semester_codes': sorted(set(unsupported)), 'option_shapes': shapes[:20]}}]
        if not years or not semesters or len(years)*len(semesters) > 180:
            codes = sorted({o['value'] if re.fullmatch(r'-?\d{1,4}', o['value']) else '<非数值编码>'
                            for o in options['xqm'] if o['value']})
            raise CampusError('TERM_DISCOVERY_FAILED', '无法确认教务网可查询的学期范围，未推断全部课表',
                              {'stage': 'terms', 'reason': 'TERM_OPTIONS', 'semester_codes': codes, 'option_shapes': shapes[:20]})
        terms = [{'year': y, 'query_year': years[y]['value'], 'semester': s, 'range_source': 'page',
                  'term': (plain(years[y]['label']) or y) + ' · ' + (plain(semesters[s]['label']) or '学期编码 ' + s),
                  'current': years[y]['selected'] and semesters[s]['selected']}
                 for y in years for s in semesters]
        return sorted(terms, key=lambda t: (t['current'], t['year'], semester_order(t['semester'])), reverse=True)

    def timetable(self, year, semester, captcha=''):
        payload = self.json_request(TIMETABLE, {'xnm': year, 'xqm': semester, 'captcha_value': captcha})
        if not isinstance(payload, dict) or not isinstance(payload.get('kbList'), list):
            raise CampusError('PROTOCOL_CHANGED', '课表响应缺少 kbList，未更新本地数据')
        if len(payload['kbList']) > 1000:
            raise CampusError('PROTOCOL_CHANGED', '课表数据规模或格式不符合约定')
        profile = normalize_profile(payload.get('xsxx')) or normalize_profile(payload)
        rows, skipped = [], 0
        self.timetable_warnings = []
        for row in payload['kbList']:
            if isinstance(row, dict) and _field(row, 'sfyjskc') == '1':
                continue
            try:
                if not isinstance(row, dict):
                    raise CampusError('PROTOCOL_CHANGED', '无效课程条目')
                rows.append(normalize_course(row))
            except CampusError:
                skipped += 1
        if skipped:
            self.timetable_warnings = [{'code': 'PARTIAL_TIMETABLE',
                'message': f'{skipped} 条课程缺少可用字段，已保留其余课程及原缓存，未标记完整', 'skipped': skipped}]
            if not rows:
                raise CampusError('PROTOCOL_CHANGED', '本学期没有可解析课程，保留上次课表并继续其他学期')
        return profile, rows

    def grades(self):
        collected, seen, total = [], set(), None
        for page in range(1, 21):
            payload = self.json_request(GRADES, {'queryModel.currentPage': page,
                'queryModel.showCount': 100, 'queryModel.sortName': 'xkkh', 'queryModel.sortOrder': 'asc'})
            if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
                raise CampusError('PROTOCOL_CHANGED', '成绩响应缺少 items，未更新本地数据')
            rows = payload['items']
            if len(rows) > 2000 or any(not isinstance(row, dict) for row in rows):
                raise CampusError('PROTOCOL_CHANGED', '成绩数据规模或格式不符合约定')
            count = payload.get('totalCount', payload.get('records'))
            if count is not None:
                try:
                    total = int(count)
                    if total < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise CampusError('PROTOCOL_CHANGED', '成绩总数格式不符合约定') from None
            fresh = 0
            for row in rows:
                fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    collected.append(normalize_grade(row))
                    fresh += 1
            if total is not None and len(collected) >= total:
                return collected, True, total
            if not rows or (len(rows) < 100 and total is None):
                return collected, total is None or len(collected) >= total, total
            if not fresh or len(collected) >= 2000:
                break
        return collected, False, total


def _field(row, *names):
    for name in names:
        if name in row and row[name] is not None:
            return plain(row[name])
    return ''


def normalize_profile(row):
    if not isinstance(row, dict):
        return None
    result = basic_fields(row)
    return result if result['student_id'] and result['name'] else None


def stale_profile(snapshot):
    profile = snapshot.get('profile')
    if not profile:
        return
    profile['stale'] = True
    sources = profile.setdefault('field_sources', {})
    for key in PROFILE_KEYS:
        if profile.get('data', {}).get(key):
            sources.setdefault(key, {'source': '本科教务网 · 上次资料', 'url': TIMETABLE,
                                     'fetched_at': profile.get('fetched_at')})['stale'] = True


def merge_profile(snapshot, record, *, fill_only=False, protect_fresh=False):
    """Only six basic fields, with independent provenance for retained cache."""
    incoming = basic_fields(record.get('data'))
    if not any(incoming.values()):
        return
    old = snapshot.get('profile') or {}
    previous = old.get('data', {})
    if previous.get('student_id') and incoming.get('student_id') and previous['student_id'] != incoming['student_id']:
        raise CampusError('PROFILE_IDENTITY_MISMATCH', '个人信息来源的学号不一致，未合并资料')
    data = {key: previous.get(key, '') for key in PROFILE_KEYS}
    sources = dict(old.get('field_sources', {}))
    for key in record.get('invalidated_fields',[]):
        source=sources.get(key,{})
        if key=='college' and not incoming.get(key) and source.get('source','').startswith('ETA') and source.get('url')==record.get('url'):
            data[key]=''
            sources.pop(key,None)
    stamp = datetime.now(TZ).isoformat()
    for key, value in incoming.items():
        if not value or (data[key] and (fill_only or (protect_fresh and sources.get(key, {}).get('stale') is False))):
            continue
        data[key] = value
        sources[key] = {'source': record['source'], 'url': record.get('field_urls',{}).get(key,record['url']), 'fetched_at': stamp, 'stale': False}
    snapshot['profile'] = {'data': data, 'field_sources': sources, 'fetched_at': stamp,
                           'stale': all(m.get('stale', True) for m in sources.values()),
                           'complete': all(data.values()), 'missing_fields': [k for k,v in data.items() if not v]}


def refresh_profile(snapshot, client, warnings):
    if not callable(getattr(client, 'profiles', None)):
        return
    try:
        identity = (snapshot.get('profile') or {}).get('data', {}).get('student_id', '')
        records, failures = client.profiles(identity)
        warnings.extend(failures)
        snapshot['profile_checks'] = []
        snapshot['profile_checks'].extend({'source':w.get('stage','profile'),'status':'failed','code':w['code']}
                                         for w in failures if w['code'] != 'PROFILE_PARTIAL')
        for record in records:
            try:
                merge_profile(snapshot, record)
                snapshot['profile_checks'].append({'source':record['source'],'status':'read',
                    'field_status':{k:'available' if record.get('data',{}).get(k) else 'unavailable' for k in PROFILE_KEYS}})
            except CampusError as exc:
                warnings.append(exc.public() | {'stage': 'profile'})
                snapshot['profile_checks'].append({'source':record['source'],'status':'failed','code':exc.code})
    except CampusError as exc:
        failure = exc.public() | {'stage': 'profile'}
        if exc.code == 'SESSION_EXPIRED':
            failure['code'] = 'PROFILE_SESSION_EXPIRED'
        warnings.append(failure)
        snapshot['profile_checks'] = [{'source':'profile','status':'failed','code':failure['code']}]
    finally:
        client.start_operation()


def normalize_course(row):
    # Some ZDBK versions encode display columns in kcb instead of separate fields.
    block = [plain(x).replace('zwf', '').strip() for x in re.split(r'<br\s*/?>', str(row.get('kcb') or ''), flags=re.I)]
    name = _field(row, 'kcmc') or (block[0] if block else '')
    if not name:
        raise CampusError('PROTOCOL_CHANGED', '课表条目缺少课程名称，未更新本地数据')
    return {'name': name, 'code': _field(row, 'kch', 'kch_id'),
            'teacher': _field(row, 'xm', 'jsxm') or (block[2] if len(block) > 2 else ''),
            'location': _field(row, 'cdmc') or (block[3] if len(block) > 3 else ''),
            'weekday': _field(row, 'xqj'), 'periods': _field(row, 'jcs', 'jc'),
            'start_period': _field(row, 'djj'), 'duration': _field(row, 'skcd'),
            'weeks': _field(row, 'zcd'), 'season': _field(row, 'xxq'), 'odd_even': _field(row, 'dsz')}


def normalize_grade(row):
    name = _field(row, 'kcmc')
    if not name:
        raise CampusError('PROTOCOL_CHANGED', '成绩条目缺少课程名称，未更新本地数据')
    selection = _field(row, 'xkkh')
    match = re.match(r'^\(([^)]+)\)-([^-]+)-', selection)
    return {'name': name, 'code': _field(row, 'kch') or (match[2] if match else ''),
            'term': match[1] if match else _field(row, 'xnmmc', 'xnm'),
            'score': _field(row, 'cj'), 'credits': _field(row, 'xf'), 'gpa': _field(row, 'jd')}


class CampusManager:
    """Serialize all operations per local owner, including disconnect and deletion."""
    def __init__(self, store, client_factory=CampusClient):
        from .calendar_sources import CalendarClient
        self.store, self.factory = store, client_factory
        self.calendar_client = CalendarClient()
        self.states, self.locks = {}, {}
        self.guard = threading.Lock()

    def lock(self, owner):
        with self.guard:
            return self.locks.setdefault(owner, threading.RLock())

    def _state(self, owner):
        state = self.states.get(owner)
        if state and state['expires'] <= time.monotonic():
            state['client'].close()
            del self.states[owner]
            return None
        return state

    def _snapshot(self, owner):
        with self.store.connect() as db:
            row = db.execute('SELECT snapshot FROM campus_snapshots WHERE owner=?', (owner,)).fetchone()
        return json.loads(row['snapshot']) if row else {'profile': None, 'courses': None, 'grades': None}

    def _save(self, owner, snapshot):
        with self.store.connect() as db:
            db.execute('INSERT INTO campus_snapshots(owner,snapshot) VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET snapshot=excluded.snapshot',
                       (owner, json.dumps(snapshot, ensure_ascii=False)))

    def status(self, owner):
        with self.lock(owner):
            state = self._state(owner)
            snapshot = self._snapshot(owner)
            # Saved snapshots are always labelled as previously fetched data.
            return {'connected': bool(state and state['connected']), 'snapshot': snapshot,
                    'connector_revision': CONNECTOR_REVISION,
                    'passport_connected': bool(state and getattr(state['client'], 'passport_authenticated', state.get('passport', False))),
                    'source': '本科教务网', 'cached': True,
                    'can_resume': bool(state and state['connected'] and state.get('sync_job') and state['sync_job']['pending'])}

    def academic_view(self, owner, day=None, term=None):
        from .academics import agenda, grade_summary
        with self.lock(owner):
            snapshot = self._snapshot(owner)
            calendars = {k: v | {'stale': v.get('stale', False) or bool(v.get('fetched_at') and
                (datetime.now(TZ)-datetime.fromisoformat(v['fetched_at'])).total_seconds() > 6*3600)}
                for k, v in snapshot.get('calendars', {}).items()}
            for record in (snapshot.get('courses') or {}).get('terms', []):
                resolved = resolve_calendar(record, calendars)
                if resolved:
                    calendars[record['year']+'/'+record['semester']] = resolved
            return {'agenda': agenda(snapshot.get('courses'), calendars, day, term),
                    'today_agenda': agenda(snapshot.get('courses'), calendars),
                    'grades': grade_summary(snapshot.get('grades'))}

    def calendar(self, owner, year, semester, refresh=False):
        with self.lock(owner):
            snapshot = self._snapshot(owner)
            terms = (snapshot.get('courses') or {}).get('terms', [])
            term = next((t for t in terms if t['year'] == year and t['semester'] == semester), None)
            if not term:
                raise CampusError('TERM_UNAVAILABLE', '请先同步该学期课表')
            key = year+'/'+semester
            calendars = snapshot.setdefault('calendars', {})
            try:
                calendars[key] = self.calendar_client.fetch(year, calendar_family(term) or semester, force=refresh)
            except CampusError as exc:
                old = calendars.get(key) or {}
                resolved = resolve_calendar(term, calendars) or old
                calendars[key] = resolved | {'stale': bool(old.get('verified')), 'error': exc.public(),
                    'verified': bool(resolved.get('verified')), 'usable': bool(resolved.get('usable') or resolved.get('verified')),
                    'source_status': 'cache' if old.get('verified') else 'fallback' if resolved.get('seasons') else 'unavailable',
                    'attempted_at': datetime.now(TZ).isoformat()}
            self._save(owner, snapshot)
            record = calendars[key]
            return self.status(owner) | {'calendar_result': {'verified': record.get('verified', False),
                'term': key, 'error': record.get('error')}}

    def prepare(self, owner):
        with self.lock(owner):
            old = self.states.pop(owner, None)
            if old:
                old['client'].close()
            client = self.factory()
            try:
                client.start_operation()
                client.prepare()
                try:
                    captcha = client.captcha('cas')
                except CampusError as exc:
                    if exc.code != 'CAPTCHA_UNAVAILABLE':
                        raise
                    captcha = None
            except Exception:
                client.close()
                raise
            flow = secrets.token_hex(32)
            self.states[owner] = {'client': client, 'flow': flow, 'expires': time.monotonic()+300, 'connected': False}
            return {'flow_id': flow, 'captcha': captcha}

    def login(self, owner, flow, username, password, authcode):
        with self.lock(owner):
            state = self._state(owner)
            if not state or state['connected'] or state['flow'] != flow:
                raise CampusError('FLOW_EXPIRED', '登录流程已过期，请重新准备登录')
            try:
                state['client'].start_operation()
                state['client'].login(username, password, authcode)
            except Exception:
                state['client'].close()
                self.states.pop(owner, None)
                raise
            state.update(connected=True, expires=time.monotonic()+3600, flow='')
            # A new passport may represent another student: never mix old snapshots.
            with self.store.connect() as db:
                db.execute('DELETE FROM campus_snapshots WHERE owner=?', (owner,))
            return self.status(owner)

    def adopt_passport(self, owner, client):
        with self.lock(owner):
            self.disconnect(owner)
            self.states[owner] = {'client': client, 'passport': True, 'connected': False,
                                  'flow': '', 'expires': time.monotonic()+3600}

    def connect_and_sync(self, owner):
        with self.lock(owner):
            state = self._state(owner)
            if not state or not state.get('passport'):
                raise CampusError('SESSION_EXPIRED', '请重新登录通行证；已缓存资料仍可查看')
            state['connected'] = False
            try:
                state['client'].start_operation()
                state['client'].connect_zdbk()
                state['connected'] = True
            except CampusError as exc:
                snapshot = self._snapshot(owner)
                snapshot['warnings'] = [exc.public() | {'stage': 'service_login'}]
                for key in ('profile', 'courses', 'grades'):
                    if snapshot.get(key):
                        snapshot[key]['stale'] = True
                stale_profile(snapshot)
                if snapshot.get('courses'):
                    for term in snapshot['courses'].get('terms', []):
                        term['stale'] = True
                    for row in snapshot['courses']['data']:
                        row['stale'] = True
                snapshot['attempted_at'] = datetime.now(TZ).isoformat()
                snapshot['sync_revision'] = CONNECTOR_REVISION
                # ETA can remain available even when undergraduate services fail.
                if getattr(state['client'], 'passport_authenticated', False):
                    state['client'].start_operation()
                    refresh_profile(snapshot, state['client'], snapshot['warnings'])
                self._save(owner, snapshot)
                if exc.code == 'SESSION_EXPIRED':
                    self.disconnect(owner)
                return self.status(owner) | {'auto_continue': False}
            return self.sync(owner)

    def captcha(self, owner):
        with self.lock(owner):
            state = self._state(owner)
            if not state or not state['connected']:
                raise CampusError('SESSION_EXPIRED', '请先登录校园通行证')
            state['client'].start_operation()
            return {'captcha': state['client'].captcha('timetable')}

    def sync(self, owner, captcha='', resume=False):
        with self.lock(owner):
            state = self._state(owner)
            if not state or not state['connected']:
                raise CampusError('SESSION_EXPIRED', '校园会话已失效，请重新登录；已同步数据仍可查看')
            client = state['client']
            client.start_operation()
            snapshot = self._snapshot(owner)
            previous_grades = snapshot.get('grades')
            warnings = []
            fetched = datetime.now(TZ).isoformat()
            if resume and not state.get('sync_job'):
                raise CampusError('SYNC_NOT_RESUMABLE', '本次连接没有待续同步，请重新开始同步')
            if not resume:
                state.pop('sync_job', None)
                for key in ('profile', 'courses', 'grades'):
                    if snapshot.get(key):
                        snapshot[key]['stale'] = True
                stale_profile(snapshot)
                if snapshot.get('courses'):
                    for term in snapshot['courses'].get('terms', []):
                        term['stale'] = True
                    for row in snapshot['courses']['data']:
                        row['stale'] = True
                # Personal information has dedicated sources; timetable xsxx is
                # a fallback, rather than the sole source of college/major.
                refresh_profile(snapshot, client, warnings)
                # Grades have no academic-year filter and are independent of discovery.
                try:
                    grades, complete, total = client.grades()
                    snapshot['grades'] = {'data': grades, 'fetched_at': datetime.now(TZ).isoformat(), 'stale': False,
                                          'complete': complete, 'total': total}
                    if not complete:
                        warnings.append({'code': 'INCOMPLETE_GRADES', 'message': '成绩未获取完整，请以官方成绩单为准'})
                except CampusError as exc:
                    warnings.append(exc.public() | {'stage': 'grades'})
                try:
                    if any(w['code'] == 'SESSION_EXPIRED' for w in warnings):
                        raise CampusError('SESSION_EXPIRED', '教务会话已失效，保留缓存')
                    try:
                        terms = client.terms()
                    except CampusError as exc:
                        if exc.code not in ('TERM_DISCOVERY_FAILED', 'PROTOCOL_CHANGED', 'NETWORK_ERROR', 'UPSTREAM_ERROR'):
                            raise
                        terms = fallback_terms(snapshot, datetime.now(TZ).date())
                        client.term_warnings = [exc.public() | {'stage': 'terms'},
                            {'code': 'TERM_RANGE_FALLBACK', 'stage': 'terms',
                             'message': '学期列表不可用，已用缓存、已有成绩与默认学年范围继续查询真实课表；范围暂不标记完整',
                             'source_status': 'fallback'}]
                    catalog_warnings = list(getattr(client, 'term_warnings', []))
                    warnings.extend(catalog_warnings)
                    # Migrate a previous single-term snapshot without discarding its cache.
                    old = snapshot.get('courses') or {}
                    saved = old.get('terms', [])
                    if not saved and old.get('year'):
                        saved = [{'year': old['year'], 'semester': old['semester'],
                                  'term': old['year'] + ' · ' + old['semester'],
                                  'data': old['data'], 'fetched_at': old['fetched_at']}]
                    catalog=merge_term_catalog(terms,old.get('catalog',[]),saved)
                    remembered={(t['year'],t['semester']):t for t in catalog}
                    cached_only=[t for t in catalog if not t['available']]
                    if cached_only:
                        warnings.append({'code':'CACHED_TERM_RANGE','stage':'terms',
                            'message':f'本次学期列表未包含 {len(cached_only)} 个已知学期，已保留学期记录和已有课表缓存',
                            'source_status':'cache'})
                    for term in saved:
                        term.update(remembered[(term['year'],term['semester'])])
                        term['stale'] = True
                        term['source_status']='cache'
                    snapshot['courses'] = {'data': old.get('data', []), 'terms': saved,
                        'catalog':catalog,
                        'fetched_at': old.get('fetched_at', fetched), 'stale': True, 'complete': False,
                        'scope': '按降级范围查询的真实课表' if any(t.get('range_source') == 'fallback' for t in terms)
                                 else '教务课表页面可查询的学期', 'discovered_at': fetched}
                    state['sync_job'] = {'pending': [dict(t) for t in terms], 'total': len(terms), 'completed': 0, 'failed': [],
                                         'profile_seen': False, 'catalog_complete': not catalog_warnings and not cached_only}
                except CampusError as exc:
                    warnings.append(exc.public() | {'stage': 'terms'})
                    if snapshot.get('courses'):
                        snapshot['courses']['complete'] = False
            job = state.get('sync_job')
            if job:
                if resume:
                    warnings = list(job.get('warnings', []))
                else:
                    job['warnings'] = list(warnings)
            halted = any(w['code'] == 'SESSION_EXPIRED' for w in warnings)
            server_failures=0
            timeout_failures=0
            # Bounded batches preserve the request deadline. The UI resumes only after success.
            for _ in range(6):
                if not job or not job['pending'] or halted:
                    break
                term = job['pending'][0]
                try:
                    profile, courses = client.timetable(term.get('query_year', term['year']), term['semester'], captcha)
                    server_failures=0
                    timeout_failures=0
                    row_warnings = list(getattr(client, 'timetable_warnings', []))
                    warnings.extend(w | {'stage': 'timetable', 'term': term['term']} for w in row_warnings)
                    job['warnings'].extend(w | {'stage': 'timetable', 'term': term['term']} for w in row_warnings)
                    if profile:
                        previous = (snapshot.get('profile') or {}).get('data') or {}
                        if previous.get('student_id') and previous['student_id'] != profile['student_id']:
                            raise CampusError('SESSION_EXPIRED', '教务响应的学生身份发生变化，请重新登录')
                        # Current/default term has priority; historical responses only fill gaps.
                        merge_profile(snapshot, {'data': profile, 'source': '本科教务网 · 课表基本信息', 'url': TIMETABLE},
                                      fill_only=job['profile_seen'], protect_fresh=True)
                        job['profile_seen'] = True
                    section = snapshot['courses']
                    key = (term['year'], term['semester'])
                    old_term = next((t for t in section['terms'] if (t['year'], t['semester']) == key), {})
                    empty_cache=not courses and bool(old_term.get('data'))
                    if empty_cache:
                        warning={'code':'EMPTY_TIMETABLE_CACHE','stage':'timetable','term':term['term'],
                                 'message':'本次返回空课表，继续显示上次成功获取的课表及原获取时间',
                                 'source_status':'cache'}
                        warnings.append(warning);job['warnings'].append(warning)
                        job['failed'].append(term)
                        old_term.update(term,stale=True,source_status='cache',attempted_at=datetime.now(TZ).isoformat())
                        # Empty responses may be temporary; they cannot withdraw known courses.
                        job['pending'].pop(0)
                        continue
                    if row_warnings:
                        # Keep older courses not present in this partial response as stale rows.
                        fingerprints = {(r.get('code'), r.get('name'), r.get('weekday'), r.get('periods'), r.get('start_period')) for r in courses}
                        courses = courses + [r | {'stale': True,'fetched_at':r.get('fetched_at',old_term.get('fetched_at'))} for r in old_term.get('data', []) if
                            (r.get('code'), r.get('name'), r.get('weekday'), r.get('periods'), r.get('start_period')) not in fingerprints]
                        job['failed'].append(term)
                    section['terms'] = [t for t in section['terms'] if (t['year'], t['semester']) != key]
                    section['terms'].append(term | {'data': courses, 'fetched_at': datetime.now(TZ).isoformat(),
                                                   'stale': False, 'partial': bool(row_warnings),
                                                   'source_status':'mixed' if row_warnings else 'live'})
                    job['pending'].pop(0)
                    job['completed'] += 1
                except CampusError as exc:
                    warnings.append(exc.public() | {'stage': 'timetable', 'term': term['term']})
                    if exc.code in ('PROTOCOL_CHANGED', 'UPSTREAM_ERROR'):
                        job['warnings'].append(exc.public() | {'stage': 'timetable', 'term': term['term']})
                        job['failed'].append(job['pending'].pop(0))
                        status=(exc.diagnostic or {}).get('http_status',0)
                        server_failures=server_failures+1 if isinstance(status,int) and status>=500 else 0
                        if server_failures>=3:
                            warnings.append({'code':'SERVER_ERRORS_PAUSED','stage':'timetable',
                                'message':'教务服务连续三次返回服务器错误，已暂停自动续传；已有资料和待查询学期保留，可稍后继续'})
                            halted=True
                    elif exc.code=='NETWORK_ERROR' and (exc.diagnostic or {}).get('reason')=='TIMEOUT':
                        # One slow historical semester must not indefinitely block all later terms.
                        job['warnings'].append(exc.public()|{'stage':'timetable','term':term['term']})
                        job['failed'].append(job['pending'].pop(0))
                        timeout_failures+=1
                        if timeout_failures>=3:
                            warnings.append({'code':'TIMETABLE_TIMEOUTS_PAUSED','stage':'timetable',
                                'message':'课表连续三次请求超时，已暂停自动续传；保留缓存和待查询学期，可稍后继续'})
                            halted=True
                    else:
                        halted = True
            if job:
                section = snapshot['courses']
                section['terms'].sort(key=lambda t: (t['year'], semester_order(t['semester'])), reverse=True)
                section['data'] = [row | {k: term[k] for k in ('year', 'semester', 'term')} |
                                   {'stale': term['stale'] or row.get('stale', False), 'fetched_at': row.get('fetched_at', term['fetched_at'])}
                                   for term in section['terms'] for row in term['data']]
                section['complete'] = not job['pending'] and not job.get('failed') and job.get('catalog_complete', True)
                section['stale'] = any(t['stale'] for t in section['terms']) or bool(job['pending'])
                stamps=[t['fetched_at'] for t in section['terms'] if t.get('fetched_at')]
                section['fetched_at'] = max(stamps) if stamps else section.get('fetched_at')
                section['attempted_at'] = datetime.now(TZ).isoformat()
                snapshot['sync_progress'] = {'completed': job['completed'], 'total': job['total'],
                                             'pending': job['pending'], 'failed': job.get('failed', [])}
                if not job['pending'] and not snapshot.get('profile'):
                    warnings.append({'code': 'PROFILE_UNAVAILABLE', 'message': '课表响应未提供完整学生基本信息，未推断缺失字段'})
            else:
                snapshot.pop('sync_progress', None)
            if any(w['code'] == 'SESSION_EXPIRED' for w in warnings):
                # Discard grades fetched in this request if the later identity/session probe failed.
                snapshot['grades'] = previous_grades
                if previous_grades:
                    previous_grades['stale'] = True
                client.close()
                self.states.pop(owner, None)
            snapshot['attempted_at'] = fetched
            snapshot['sync_revision'] = CONNECTOR_REVISION
            snapshot['warnings'] = warnings
            self._save(owner, snapshot)
            return self.status(owner) | {'cached': False, 'auto_continue': bool(job and job['pending'] and not halted)}

    def disconnect(self, owner, delete=False):
        with self.lock(owner):
            state = self.states.pop(owner, None)
            if state:
                state['client'].close()
            if delete:
                with self.store.connect() as db:
                    db.execute('DELETE FROM campus_snapshots WHERE owner=?', (owner,))

    def close(self):
        for owner in list(self.states):
            self.disconnect(owner)
