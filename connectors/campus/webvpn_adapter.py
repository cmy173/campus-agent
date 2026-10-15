"""WebVPN adapter for ZJU.

Handles login, session management, and proxying requests through WebVPN tunnel.
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Optional

from .campus_common import CampusError
from .campus_crypto import webvpn_encrypt_password
from .campus_network import build_opener


WEBVPN_ORIGIN = 'https://webvpn.zju.edu.cn'
WEBVPN_LOGIN_URL = WEBVPN_ORIGIN + '/login'
WEBVPN_DO_LOGIN_URL = WEBVPN_ORIGIN + '/do-login'
WEBVPN_PREFIX = WEBVPN_ORIGIN + '/https/'


class WebVPNError(CampusError, ValueError):
    """Structured portal error that stays a ValueError for existing callers.

    The code is what the background channel reacts to (re-authenticate once on
    WEBVPN_SESSION_EXPIRED, back off on the rest); the message is user-facing and
    never contains a password, ticket or response body.
    """


# Error codes observed in the live /do-login switch (2026-10-15). Server messages are
# never echoed: each code maps to a reviewed, user-facing sentence.
WEBVPN_ERRORS = {
    'WEEK_PASSWORD_FORBID': 'WebVPN 因弱密码禁止登录，请先修改密码',
    'IP_FORBIDDEN': 'WebVPN 暂时拒绝本机地址登录，请稍后重试或手动登录',
    'TOO_MANY_ATTEMPTS': 'WebVPN 登录尝试过多，需要验证码；请手动登录一次',
    'CAPTCHA_FAILED': 'WebVPN 要求验证码，需要你手动登录一次',
    'INVALID_ACCOUNT': 'WebVPN 账号或密码不正确',
    'NEED_TWO_STEP': 'WebVPN 要求二次认证（短信或动态口令），需要你手动登录一次',
}

# Codes the background channel must never retry on its own.
WEBVPN_ERROR_CODES = {
    'WEEK_PASSWORD_FORBID': 'WEBVPN_PASSWORD_CHANGE_REQUIRED',
    'IP_FORBIDDEN': 'WEBVPN_ADDRESS_REFUSED',
    'TOO_MANY_ATTEMPTS': 'WEBVPN_CAPTCHA_REQUIRED',
    'CAPTCHA_FAILED': 'WEBVPN_CAPTCHA_REQUIRED',
    'INVALID_ACCOUNT': 'WEBVPN_CREDENTIAL_REJECTED',
    'NEED_TWO_STEP': 'WEBVPN_MFA_REQUIRED',
}

# The live login form's named fields, in page order. Only these are echoed back.
WEBVPN_FORM_FIELDS = ('_csrf', 'auth_type', 'username', 'sms_code', 'password',
                      'captcha', 'needCaptcha', 'captcha_id', 'rememberMe')

_MESSAGE_CODES = (
    ('密码', 'WEBVPN_CREDENTIAL_REJECTED'),
    ('锁定', 'WEBVPN_LOCKED'),
    ('冻结', 'WEBVPN_LOCKED'),
    ('验证码', 'WEBVPN_CAPTCHA_REQUIRED'),
    ('滑动', 'WEBVPN_CAPTCHA_REQUIRED'),
    ('拖动', 'WEBVPN_CAPTCHA_REQUIRED'),
    ('二次', 'WEBVPN_MFA_REQUIRED'),
    ('动态口令', 'WEBVPN_MFA_REQUIRED'),
    ('短信', 'WEBVPN_MFA_REQUIRED'),
    ('频繁', 'WEBVPN_RATE_LIMITED'),
    ('过多', 'WEBVPN_RATE_LIMITED'),
    ('弱密码', 'WEBVPN_PASSWORD_CHANGE_REQUIRED'),
)


def _classify(code: str, message: str):
    """Map a /do-login failure to a reviewed (code, message) pair; never echo server text."""
    if code in WEBVPN_ERROR_CODES:
        return WEBVPN_ERROR_CODES[code], WEBVPN_ERRORS[code]
    for token, mapped in _MESSAGE_CODES:
        if token in message:
            return mapped, WEBVPN_ERRORS.get(code, f'WebVPN 登录未完成（{mapped}）')
    return 'WEBVPN_LOGIN_FAILED', 'WebVPN 登录失败，请在官方页面核对后手动重试'


def _is_portal_login(location) -> bool:
    """True when a Location points back at the portal's own login page.

    A redirect to a *school* host is the normal SSO handoff and must be handed to the
    caller; only the portal's login page proves our session is gone.
    """
    if not isinstance(location, str) or not location.strip():
        return False
    try:
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(WEBVPN_ORIGIN, location))
    except ValueError:
        return True
    if parts.hostname != 'webvpn.zju.edu.cn':
        return False
    # A proxied campus page always lives under /https/<token>/...; only the portal's own
    # /login (with or without a query) proves the session is gone.
    return parts.path.rstrip('/') in ('/login', '')


def _form_fields(html: str) -> dict:
    """Extract only the first (login) form's named fields; nothing here is a secret.

    The page also carries second-step/binding forms; taking their inputs would post the
    wrong payload. ``_csrf``/``captcha_id`` are per-page tokens and the password is
    encrypted by the caller.
    """
    fields: dict = {}

    class _Form(HTMLParser):
        depth = 0

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == 'form':
                self.depth += 1
                return
            if self.depth != 1 or tag != 'input':
                return
            name = attributes.get('name')
            if name in WEBVPN_FORM_FIELDS:
                fields.setdefault(name, attributes.get('value', ''))

        def handle_endtag(self, tag):
            if tag == 'form' and self.depth:
                self.depth -= 1

    try:
        _Form().feed(html)
    except Exception:
        return {}
    return fields

# Host tokens observed on 2026-10-12 from a live authenticated session. The token is
# hex("wrdvpnisawesome!" + cipher(host)) produced by server-side-constant key material;
# it is deterministic per host but its cipher could not be reproduced offline, so known
# hosts are listed here and verified at request time. Add new hosts by reading the
# proxied URL from the portal search box (see docs/webvpn-analysis.md).
HOST_TOKENS = {
    'zjuam.zju.edu.cn': '77726476706e69737468656265737421eafd549d2a7e725a6b468ca88d1b203b',
    'zdbk.zju.edu.cn': '77726476706e69737468656265737421eaf34397692a6245300d8db9d6562d',
}


class _WebVPNCookiePolicy(DefaultCookiePolicy):
    """Portal cookies cannot spread to CAS/business hosts or other users' jars."""
    def set_ok(self, cookie, request):
        if (urllib.parse.urlsplit(request.full_url).hostname != 'webvpn.zju.edu.cn'
                or not super().set_ok(cookie, request)):
            return False
        cookie.domain = 'webvpn.zju.edu.cn'
        cookie.domain_specified = False
        cookie.domain_initial_dot = False
        return True

    def return_ok(self, cookie, request):
        return (urllib.parse.urlsplit(request.full_url).hostname == 'webvpn.zju.edu.cn'
                and cookie.domain.lstrip('.').lower() == 'webvpn.zju.edu.cn'
                and super().return_ok(cookie, request))


class WebVPNAdapter:
    """Opt-in portal session; no automatic login/retry, no direct fallback.

    This standalone adapter is not wired into CampusTransport or BrowserHost.
    A custom opener is an explicit caller-owned transport (for offline tests).
    """
    def __init__(self, opener=None, *, policy=None):
        self.cookies = CookieJar(policy=_WebVPNCookiePolicy())
        self.opener = opener if opener is not None else build_opener(policy, self.cookies)
        self.opener.cookiejar = self.cookies
        self.authenticated = False
        self.username: Optional[str] = None
        self.session_expiry: Optional[float] = None
        # True when this login took the account over from another portal session.
        self.replaced_other_session = False

    def _invalidate(self):
        self.authenticated = False
        self.username = None
        self.session_expiry = None
        self.replaced_other_session = False

    def close(self):
        """Discard this adapter's in-memory session, not other browser profiles."""
        self._invalidate()
        self.cookies.clear()

    def login(self, username: str, password: str) -> bool:
        """One explicitly requested official login; never retain the password.

        The portal posts the *whole* serialized login form to ``/do-login``, with the
        password encrypted by the page's own AES-CFB helper and the per-page
        ``_csrf``/``captcha_id`` values echoed back inside the same cookie session
        (verified against the live login page on 2026-10-15). Sending only
        ``user_name``/``password`` is answered with HTTP 400.
        """
        # A failed account switch must not keep the previous user's session alive.
        self.close()
        fields = self._login_form()
        if fields.get('needCaptcha', '').lower() == 'true':
            raise WebVPNError('WEBVPN_CAPTCHA_REQUIRED', 'WebVPN 要求验证码，需要在官方页面手动登录一次')
        if not fields.get('_csrf'):
            raise WebVPNError('WEBVPN_PROTOCOL_CHANGED', 'WebVPN 登录页缺少 _csrf 令牌，登录协议可能已变化')
        payload = dict(fields)
        payload.update(username=username, password=webvpn_encrypt_password(password),
                       auth_type=fields.get('auth_type') or 'local')
        result = self._post_json('/do-login', urllib.parse.urlencode(payload).encode('ascii'))
        if result.get('success') is True:
            self.authenticated = True
            self.username = username
            return True
        code = str(result.get('error') or '')
        message = str(result.get('message') or '')
        if code == 'NEED_CONFIRM':
            # The school says this account is already signed in elsewhere. The page offers
            # the same one-click takeover; the caller has already asked for automatic
            # re-authentication on this machine, so take it over exactly once.
            confirm = self._post_json('/do-confirm-login', b'')
            if confirm.get('success') is True:
                self.authenticated = True
                self.username = username
                self.replaced_other_session = True
                return True
            raise WebVPNError('WEBVPN_TAKEOVER_REFUSED', '校方未允许接管已有 WebVPN 会话，请手动登录一次')
        raise WebVPNError(*_classify(code, message))

    def _login_form(self) -> dict:
        """Fetch the login page and return only its named form fields (never a password)."""
        request = urllib.request.Request(WEBVPN_LOGIN_URL, headers={'Accept': 'text/html'})
        try:
            with self.opener.open(request, timeout=15) as response:
                body = response.read(262_145)
            if response.code != 200 or len(body) > 262_144:
                raise WebVPNError('WEBVPN_UNAVAILABLE', 'WebVPN 登录页响应异常，请稍后重试')
        except WebVPNError:
            raise
        except Exception:
            raise WebVPNError('WEBVPN_UNAVAILABLE', '无法访问 WebVPN 登录页面，请检查网络后手动重试') from None
        return _form_fields(body.decode('utf-8', 'replace'))

    def _post_json(self, path: str, data: bytes) -> dict:
        request = urllib.request.Request(WEBVPN_ORIGIN + path, data=data, headers={
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://webvpn.zju.edu.cn', 'Referer': WEBVPN_LOGIN_URL,
        })
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(65537)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise WebVPNError('WEBVPN_CREDENTIAL_REJECTED', 'WebVPN 账号或密码错误') from None
            if exc.code == 429:
                raise WebVPNError('WEBVPN_RATE_LIMITED', 'WebVPN 登录过于频繁，请稍后重试') from None
            if exc.code in (400, 415, 422):
                # The portal understood the endpoint but refused the payload: the login
                # protocol changed. This is not a credential verdict and not a network fault.
                raise WebVPNError('WEBVPN_PROTOCOL_CHANGED',
                                  f'WebVPN 登录请求被门户拒绝（HTTP {exc.code}），登录协议可能已变化；请在官方页面手动登录一次') from None
            raise WebVPNError('WEBVPN_UNAVAILABLE', f'WebVPN 请求失败 (HTTP {exc.code})') from None
        except (urllib.error.URLError, OSError):
            raise WebVPNError('WEBVPN_UNAVAILABLE', 'WebVPN 连接失败，请检查网络后手动重试') from None
        try:
            result = json.loads(raw.decode('utf-8')) if len(raw) <= 65536 else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            result = None
        if not isinstance(result, dict):
            raise WebVPNError('WEBVPN_PROTOCOL_CHANGED', 'WebVPN 返回了无效 JSON 响应，可能服务异常')
        return result

    @staticmethod
    def _parts(url: str):
        """Reject ambiguous authority/ports rather than silently change targets."""
        try:
            if not isinstance(url, str) or any(ord(c) < 33 or c == chr(92) for c in url):
                raise ValueError
            parts = urllib.parse.urlsplit(url)
            port = parts.port
            if (parts.scheme not in ('https', 'http') or not parts.hostname
                    or parts.username is not None or parts.password is not None or parts.fragment
                    or port not in (None, 443 if parts.scheme == 'https' else 80)):
                raise ValueError
            return parts
        except (ValueError, TypeError):
            raise WebVPNError('WEBVPN_TARGET_UNSUPPORTED', 'WebVPN 目标地址不受支持') from None

    def proxy_url(self, original_url: str) -> str:
        """Only registered hosts; preserve path/query, never guess a host token."""
        if not self.is_session_valid():
            raise WebVPNError('WEBVPN_SESSION_EXPIRED', 'WebVPN 未认证或会话过期，请在官方页面登录')
        parts = self._parts(original_url)
        token = HOST_TOKENS.get(parts.hostname)
        if not token:
            raise WebVPNError('WEBVPN_HOST_UNREGISTERED', 'WebVPN 目标主机未登记，见 docs/webvpn-analysis.md')
        path = parts.path or '/'
        query = '?' + parts.query if parts.query else ''
        return f'https://webvpn.zju.edu.cn/{parts.scheme}/{token}{path}{query}'

    def unmap_url(self, url):
        """Translate a portal URL back to the campus URL it proxies.

        A tunnelled app answers with ``Location: https://webvpn.zju.edu.cn/https/<token>/...``.
        That is not a campus URL, so `check_url` would (correctly) reject it and the SSO
        handoff would break. Only the reverse of a registered token is rewritten; anything
        else is returned untouched.
        """
        if not isinstance(url, str) or 'webvpn.zju.edu.cn' not in url:
            return url
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return url
        if parts.hostname != 'webvpn.zju.edu.cn':
            return url
        segments = parts.path.lstrip('/').split('/', 2)
        if len(segments) < 3 or segments[0] not in ('http', 'https'):
            return url
        host = {token: name for name, token in HOST_TOKENS.items()}.get(segments[1])
        if not host:
            return url
        query = '?' + parts.query if parts.query else ''
        return f'{segments[0]}://{host}/{segments[2]}{query}'

    def _localize(self, response):
        """Rewrite a proxied redirect target back to its campus URL before the caller reads it."""
        headers = getattr(response, 'headers', None)
        if headers is None:
            return response
        location = headers.get('Location')
        mapped = self.unmap_url(location)
        if mapped != location:
            try:
                if hasattr(headers, 'replace_header'):
                    headers.replace_header('Location', mapped)
                else:
                    headers['Location'] = mapped
            except (KeyError, TypeError, ValueError):
                pass
        return response

    def open(self, url: str, *, timeout: float = 30, **kwargs):
        """Explicit VPN channel: not authenticated/unregistered never go direct."""
        if not self.is_session_valid():
            raise WebVPNError('WEBVPN_SESSION_EXPIRED', 'WebVPN 未认证或会话过期，请在官方页面登录')
        parts = self._parts(url)
        if parts.hostname == 'webvpn.zju.edu.cn':
            if parts.scheme != 'https':
                raise WebVPNError('WEBVPN_TARGET_UNSUPPORTED', 'WebVPN 门户仅支持 HTTPS')
            target = url
        else:
            target = self.proxy_url(url)
        req = urllib.request.Request(target, **kwargs)
        try:
            resp = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # The owned opener does not follow redirects, so a 3xx arrives as an
            # HTTPError. The campus SSO handoff *is* a redirect chain, so a redirect
            # must be returned to the caller like the direct channel does — unless it
            # points back at the portal's own login page, which means our session died.
            if exc.code in (301, 302, 303, 307, 308):
                if _is_portal_login(exc.headers.get('Location') if exc.headers else None):
                    self._invalidate()
                    raise WebVPNError('WEBVPN_SESSION_EXPIRED', 'WebVPN 会话已失效，需要重新认证') from None
                return self._localize(exc)
            if exc.code == 401:
                self._invalidate()
                raise WebVPNError('WEBVPN_SESSION_EXPIRED', 'WebVPN 会话需要重新确认，请在官方页面登录') from None
            raise
        # Custom openers may follow redirects; a landing login page is never data.
        if resp.code in (301, 302, 303, 307, 308):
            if _is_portal_login(resp.headers.get('Location') if resp.headers else None):
                resp.close()
                self._invalidate()
                raise WebVPNError('WEBVPN_SESSION_EXPIRED', 'WebVPN 会话已失效，需要重新认证')
            return self._localize(resp)
        if resp.geturl() != target or resp.code == 401:
            resp.close()
            self._invalidate()
            raise WebVPNError('WEBVPN_SESSION_EXPIRED', 'WebVPN 会话需要重新确认，请在官方页面登录')
        return resp

    def get_cookies_dict(self) -> list:
        """Compatibility in-memory transfer only; caller MUST NOT log/persist it.

        No production caller currently persists these values in this repository.
        """
        return [dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                     secure=c.secure, expires=c.expires,
                     domain_specified=c.domain_specified, path_specified=c.path_specified)
                for c in self.cookies if not c.is_expired()]

    def restore_cookies(self, cookies_list: list) -> bool:
        """Replace candidate cookies atomically; live portal probe is mandatory."""
        self.close()
        try:
            if not isinstance(cookies_list, list) or not cookies_list:
                return False
            candidates = []
            now = time.time()
            for data in cookies_list:
                if (not isinstance(data, dict) or str(data.get('domain', '')).lstrip('.').lower() != 'webvpn.zju.edu.cn'
                        or not isinstance(data.get('name'), str) or not data['name']
                        or not isinstance(data.get('value'), str)
                        or not isinstance(data.get('path'), str) or not data['path'].startswith('/')):
                    raise ValueError
                expires = data.get('expires')
                if expires is not None:
                    if isinstance(expires, bool) or not isinstance(expires, (float, int)):
                        raise ValueError
                    if not math.isfinite(expires):
                        raise ValueError
                    if expires <= now:
                        continue
                candidates.append(Cookie(0, data['name'], data['value'], None, False,
                    'webvpn.zju.edu.cn', False, False, data['path'], True,
                    bool(data.get('secure', False)), expires, expires is None,
                    None, None, {}, False))
            if not candidates:
                return False
            for cookie in candidates:
                self.cookies.set_cookie(cookie)
            if self.probe():
                return True
            self.close()
            return False
        except (KeyError, TypeError, ValueError, OverflowError):
            self.close()
            return False

    def is_session_valid(self) -> bool:
        """Local candidate check only; probe() confirms server-side validity."""
        if self.session_expiry is not None and time.time() >= self.session_expiry:
            self._invalidate()
        return self.authenticated

    def probe(self) -> bool:
        """Fail closed on empty/malformed/negative JSON or login redirects.

        The full /user/info schema still needs credentialed confirmation; only
        explicit JSON success=true is accepted, not a guessed default success.
        """
        if not self.authenticated and not any(not c.is_expired() for c in self.cookies):
            return False
        if self.session_expiry is not None and time.time() >= self.session_expiry:
            self._invalidate()
            return False
        url = 'https://webvpn.zju.edu.cn/user/info?_t=%d' % int(time.time() * 1000)
        valid = False
        try:
            req = urllib.request.Request(url)
            with self.opener.open(req, timeout=10) as resp:
                raw = resp.read(65537)
                if resp.code == 200 and resp.geturl() == url and len(raw) <= 65536:
                    body = json.loads(raw.decode('utf-8'))
                    valid = (isinstance(body, dict) and body.get('success') is True
                             and 'login' not in str(body.get('url', '')).lower())
        except Exception:
            # Error strings may contain tickets, proxy credentials or response data.
            valid = False
        if not valid:
            self._invalidate()
        else:
            self.authenticated = True
        return valid
