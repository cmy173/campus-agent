"""Browser cookies → isolated in-memory API session; no credential persistence."""
from __future__ import annotations

import math
import os
import time
from http.cookiejar import Cookie
from typing import Any


_API_HOSTS = frozenset(("courses.zju.edu.cn", "classroom.zju.edu.cn"))


def _requests_session(cookies: list[dict[str, Any]], host: str) -> Any:
    """Copy applicable cookies only, binding parent cookies to this API host.

    CAS master cookies stay in the browser. API requests never need to send them;
    the browser handles SSO refresh. Preserve expiry, path and Secure semantics.
    """
    if host not in _API_HOSTS:
        raise ValueError("Unsupported platform host")
    import requests

    session = requests.Session()
    session.trust_env = False  # no implicit proxy/.netrc; TLS verification unchanged
    proxy = os.environ.get("STUDY_PLATFORM_PROXY")
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    now = time.time()
    for item in cookies:
        try:
            domain = str(item.get("domain", "")).lower().lstrip(".")
            name = item.get("name")
            value = item.get("value")
            if (domain not in (host, "zju.edu.cn") or not isinstance(name, str) or not name
                    or name.lower() == "iplanetdirectorypro" or not isinstance(value, str)):
                continue
            expires = item.get("expires")
            if expires is not None and expires != -1:
                expires = float(expires)
                if not math.isfinite(expires) or expires <= now:
                    continue
                expires = int(expires)
            else:
                expires = None
            path = item.get("path") or "/"
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            cookie = Cookie(0, name, value, None, False, host, False, False,
                            path, True, bool(item.get("secure", False)), expires,
                            expires is None, None, None,
                            {"HttpOnly": bool(item.get("httpOnly", False))}, False)
            session.cookies.set_cookie(cookie)
        except (TypeError, ValueError, OverflowError):
            continue
    if not session.cookies:
        session.close()
        return None
    return session
