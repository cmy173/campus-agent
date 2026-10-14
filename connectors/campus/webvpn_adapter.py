"""WebVPN adapter for ZJU.

Handles login, session management, and proxying requests through WebVPN tunnel.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Optional

from .campus_crypto import webvpn_encrypt_password


WEBVPN_LOGIN_URL = 'https://webvpn.zju.edu.cn/login'
WEBVPN_DO_LOGIN_URL = 'https://webvpn.zju.edu.cn/do-login'
WEBVPN_PREFIX = 'https://webvpn.zju.edu.cn/https/'

# Error codes observed in the login page callback switch; unknown codes fall back
# to a generic message that still surfaces the raw code for diagnosis.
WEBVPN_ERRORS = {
    '1': 'WebVPN 账号或密码错误',
    '2': 'WebVPN 账号或密码错误',
    '3': 'WebVPN 账号已锁定，请稍后重试或联系信息技术中心',
    '4': 'WebVPN 需要验证码，请在浏览器中手动登录一次',
    '5': 'WebVPN 服务暂时不可用',
}

# Host tokens observed on 2026-10-12 from a live authenticated session. The token is
# hex("wrdvpnisawesome!" + cipher(host)) produced by server-side-constant key material;
# it is deterministic per host but its cipher could not be reproduced offline, so known
# hosts are listed here and verified at request time. Add new hosts by reading the
# proxied URL from the portal search box (see docs/webvpn-analysis.md).
HOST_TOKENS = {
    'zjuam.zju.edu.cn': '77726476706e69737468656265737421eafd549d2a7e725a6b468ca88d1b203b',
    'zdbk.zju.edu.cn': '77726476706e69737468656265737421eaf34397692a6245300d8db9d6562d',
}


class WebVPNAdapter:
    """Manage WebVPN authentication and request proxying."""
    
    def __init__(self, opener=None):
        """Initialize WebVPN adapter.
        
        Args:
            opener: Optional urllib opener with custom handlers
        """
        self.cookies = CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.opener.cookiejar = self.cookies
        self.authenticated = False
        self.username: Optional[str] = None
        self.session_expiry: Optional[float] = None
    
    def login(self, username: str, password: str) -> bool:
        """Authenticate with WebVPN.

        Protocol reverse-engineered from the login page inline script (2026-10-12):
        jQuery $.post('/do-login', serialized form) with fields user_name and
        password (hex AES-CFB ciphertext); response JSON {success, url} or
        {success: false, error: <code>}.

        Args:
            username: WebVPN network account
            password: WebVPN password (will be encrypted client-side)

        Returns:
            True if login successful

        Raises:
            ValueError: If login fails or connection error
        """
        # Step 1: Get login page to ensure we have any required cookies
        req = urllib.request.Request(WEBVPN_LOGIN_URL)
        try:
            with self.opener.open(req, timeout=10) as resp:
                resp.read(4096)
        except Exception as e:
            raise ValueError(f'无法访问 WebVPN 登录页面: {e}') from None

        # Step 2: Encrypt password exactly like the page script
        encrypted_password = webvpn_encrypt_password(password)

        # Step 3: Submit as form-urlencoded, same fields as the login form
        form = urllib.parse.urlencode({
            'user_name': username,
            'password': encrypted_password,
        }).encode('ascii')

        req = urllib.request.Request(
            WEBVPN_DO_LOGIN_URL,
            data=form,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': 'https://webvpn.zju.edu.cn',
                'Referer': WEBVPN_LOGIN_URL,
            }
        )

        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read(65536)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ValueError('WebVPN 账号或密码错误') from None
            if e.code == 429:
                raise ValueError('WebVPN 登录过于频繁，请稍后重试') from None
            raise ValueError(f'WebVPN 请求失败 (HTTP {e.code})') from None
        except (urllib.error.URLError, OSError) as e:
            raise ValueError(f'WebVPN 连接失败: {e}') from None

        try:
            result = json.loads(raw.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('WebVPN 返回了非 JSON 响应，可能服务异常') from None

        if result.get('success'):
            self.authenticated = True
            self.username = username
            # Assume 24-hour session unless observed otherwise
            self.session_expiry = time.time() + 86400
            return True

        error_code = result.get('error') or result.get('message') or 'unknown'
        message = WEBVPN_ERRORS.get(str(error_code), f'WebVPN 登录失败（{error_code}）')
        raise ValueError(message)
    
    def proxy_url(self, original_url: str) -> str:
        """Convert a campus URL to its WebVPN proxied form.

        Proxied URLs look like https://webvpn.zju.edu.cn/https/<host-token>/<path>.
        Only hosts with a known token can be proxied; see HOST_TOKENS.

        Args:
            original_url: Original campus URL (e.g., https://zdbk.zju.edu.cn/jwglxt/...)

        Returns:
            WebVPN proxied URL

        Raises:
            ValueError: If not authenticated or the host has no known token
        """
        if not self.authenticated:
            raise ValueError('WebVPN 未认证，请先登录')

        parts = urllib.parse.urlsplit(original_url)
        host = parts.hostname or ''
        token = HOST_TOKENS.get(host)
        if not token:
            raise ValueError(f'WebVPN 未登记主机 {host} 的代理令牌，见 docs/webvpn-analysis.md')

        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        scheme = 'https' if parts.scheme != 'http' else 'http'
        return f'https://webvpn.zju.edu.cn/{scheme}/{token}{path}'

    def open(self, url: str, **kwargs) -> urllib.response.addinfourl:
        """Open a URL, proxying registered campus hosts through WebVPN.

        Args:
            url: Target URL (campus hosts are rewritten, others pass through)
            **kwargs: Passed to urllib.request.Request

        Returns:
            Response object

        Raises:
            ValueError: If session expired or the campus host is not registered
        """
        if self.authenticated and self.session_expiry and time.time() > self.session_expiry:
            self.authenticated = False
            raise ValueError('WebVPN 会话已过期，请重新登录')

        target = url
        host = urllib.parse.urlsplit(url).hostname or ''
        if self.authenticated and host in HOST_TOKENS:
            target = self.proxy_url(url)

        req = urllib.request.Request(target, **kwargs)
        return self.opener.open(req, timeout=30)
    
    def get_cookies_dict(self) -> list:
        """Export cookies for persistence.
        
        Returns:
            List of cookie dicts that can be serialized to JSON
        """
        return [
            {
                'name': cookie.name,
                'value': cookie.value,
                'domain': cookie.domain,
                'path': cookie.path,
                'secure': cookie.secure,
                'expires': cookie.expires,
                'domain_specified': cookie.domain_specified,
                'path_specified': cookie.path_specified
            }
            for cookie in self.cookies
        ]
    
    def restore_cookies(self, cookies_list: list) -> bool:
        """Restore cookies from persistence.
        
        Args:
            cookies_list: List of cookie dicts from get_cookies_dict()
            
        Returns:
            True if restoration successful
        """
        from http.cookiejar import Cookie
        
        try:
            for cookie_data in cookies_list:
                cookie = Cookie(
                    version=0,
                    name=cookie_data['name'],
                    value=cookie_data['value'],
                    port=None,
                    port_specified=False,
                    domain=cookie_data['domain'],
                    domain_specified=cookie_data.get('domain_specified', True),
                    domain_initial_dot=False,
                    path=cookie_data['path'],
                    path_specified=cookie_data.get('path_specified', True),
                    secure=cookie_data['secure'],
                    expires=cookie_data['expires'],
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False
                )
                self.cookies.set_cookie(cookie)
            
            self.authenticated = True
            return True
        except Exception:
            self.authenticated = False
            return False
    
    def is_session_valid(self) -> bool:
        """Check if current session is still valid.
        
        Returns:
            True if authenticated and not expired
        """
        if not self.authenticated:
            return False
        
        if self.session_expiry and time.time() > self.session_expiry:
            self.authenticated = False
            return False
        
        return True

    def probe(self) -> bool:
        """Ask the portal whether this session is still authenticated.

        Restored cookies can be stale server-side, so local expiry alone is not
        enough. /user/info returns JSON for a live session and a login redirect
        or error payload otherwise.
        
        Returns:
            True if the server accepts the current session
        """
        if not self.is_session_valid():
            return False
        try:
            req = urllib.request.Request(
                'https://webvpn.zju.edu.cn/user/info?_t=%d' % int(time.time() * 1000))
            with self.opener.open(req, timeout=10) as resp:
                body = json.loads(resp.read(65536).decode('utf-8'))
            return bool(body.get('success', True)) and 'login' not in str(body.get('url', ''))
        except Exception:
            self.authenticated = False
            return False
