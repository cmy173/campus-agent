"""Network policy scoped exclusively to campus clients."""
from enum import Enum
import os
import http.client
import socket
import ssl
import time
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit, urlencode
from .campus_common import CampusError
from .campus_endpoints import request_options

class NetworkPolicy(str, Enum):
    SYSTEM = "system"
    DIRECT = "direct"

def network_policy():
    # Keep the established direct default; system is an explicit supported option.
    return NetworkPolicy(os.environ.get("CAMPUS_NETWORK_POLICY", "direct"))

def build_opener(policy=None, jar=None):
    policy = NetworkPolicy(policy) if policy is not None else network_policy()
    handlers = [urllib.request.ProxyHandler({}) if policy == NetworkPolicy.DIRECT else urllib.request.ProxyHandler(), _NoRedirect()]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)

def transport_failure(exc):
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


_transport_failure = transport_failure  # old test/import compatibility


class PlatformBudget:
    """Process-local read request ceiling shared by users on each campus host."""
    def __init__(self, limit=120):
        self.limit, self.attempts, self.lock = limit, {}, threading.Lock()

    def take(self, platform):
        with self.lock:
            now = time.monotonic()
            recent = [t for t in self.attempts.get(platform, []) if now-60 < t <= now]
            if len(recent) >= self.limit:
                raise CampusError('RATE_LIMITED', '校园平台请求频率受限')
            self.attempts[platform] = recent+[now]


READ_BUDGET = PlatformBudget()


class PublicPageTransport:
    """Bounded credential-free reads; a separate, explicit public-site network policy.

    New public college pages follow the system by default. There is no fallback
    between channels; CAS and other modules keep their existing configuration.
    """
    def __init__(self, allowed, policy=None):
        self.allowed = allowed
        self.opener = build_opener(policy or os.environ.get('CAMPUS_PUBLIC_NETWORK_POLICY', 'system'))

    def get(self, url):
        if not self.allowed(url):
            raise CampusError('FORBIDDEN', '公开来源未登记')
        READ_BUDGET.take(urlsplit(url).hostname)
        request = urllib.request.Request(url, headers={'User-Agent': 'CampusAgent/0.3'})
        try:
            with self.opener.open(request, timeout=10) as response:
                if response.code != 200 or response.geturl() != url:
                    raise CampusError('PROTOCOL_CHANGED', '公开页面响应无法确认')
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise CampusError('PROTOCOL_CHANGED', '公开页面超出读取上限')
                return raw, dict(response.headers)
        except urllib.error.HTTPError as exc:
            code = 'RATE_LIMITED' if exc.code == 429 else 'FORBIDDEN' if exc.code in (401, 403) else 'PROTOCOL_CHANGED'
            raise CampusError(code, '公开页面请求失败') from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, EOFError) as exc:
            reason, _ = transport_failure(exc)
            raise CampusError('TIMEOUT' if reason == 'TIMEOUT' else 'NETWORK_UNREACHABLE', '公开页面网络请求失败') from None


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
    def set_ok(self, cookie, request):
        if not super().set_ok(cookie, request):
            return False
        host = urlsplit(request.full_url).hostname
        if host not in ('zjuam.zju.edu.cn', 'zdbk.zju.edu.cn', 'eta.zju.edu.cn'):
            return False
        # CAS currently issues _csrf for .zju.edu.cn. Dropping it breaks the
        # login form; sharing it with business sites breaks session isolation.
        # Bind accepted cookies to their actual issuer, preserving name/path.
        cookie.domain = host
        cookie.domain_specified = False
        cookie.domain_initial_dot = False
        return True

    def return_ok(self, cookie, request):
        host = urlsplit(request.full_url).hostname
        # CAS may issue a parent-domain master cookie; it is only ever sent to CAS.
        if cookie.name.lower() == 'iplanetdirectorypro':
            if host != 'zjuam.zju.edu.cn':
                return False
        elif cookie.domain.lstrip('.').lower() != host:
            return False
        return super().return_ok(cookie, request)


class CampusTransport:
    def __init__(self, policy=None):
        self.jar = CookieJar(policy=_CampusCookiePolicy())
        self.policy = NetworkPolicy(policy) if policy is not None else network_policy()
        self.opener = build_opener(self.policy, self.jar)
        self.deadline = float('inf')
        self.last_request = 0

    def start_operation(self):
        self.deadline = time.monotonic() + 45

    def close(self):
        self.jar.clear()

    def request_navigation(self, url):
        """Consume status/cookies/Location; an SSO landing body is not data."""
        return self.request(url, read_body=False)

    def request(self, url, form=None, *, read_body=True):
        if getattr(self,'cancel_event',None) is not None and self.cancel_event.is_set():
            raise CampusError('FLOW_CANCELLED','登录已取消')
        check_url(url)
        parsed = urlsplit(url)
        options = request_options(url, form is not None)
        stage, label = options['stage'], options['label']
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
        READ_BUDGET.take(parsed.hostname)
        req = urllib.request.Request(url, data=urlencode(form).encode('utf-8') if form is not None else None,
                                     headers=options['headers'])
        try:
            try:
                response = self.opener.open(req, timeout=min(options['timeout'], remaining))
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if response.code in options['reject_statuses']:
                    reason = 'HTTP_RATE_LIMIT' if response.code == 429 else 'HTTP_ACCESS_REJECTED'
                    raise CampusError(options['rejection_code'], options['rejection_message'].format(status=response.code),
                        diagnostic | {'reason': reason, 'http_status': response.code,
                                      'elapsed_ms': round((time.monotonic()-started)*1000)})
                raw = b''
                if read_body:
                    # ETA forms include option dictionaries and an encoded envelope.
                    # The larger cap applies only to its data APIs, not other sites.
                    limit = options['limit']
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
