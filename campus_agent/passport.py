"""校园通行证登录：用学号 + 统一身份认证密码走一遍浙大 CAS，确认身份。

## 这是什么

浙大的「校园通行证」就是统一身份认证（`zjuam.zju.edu.cn` 的 CAS）：
学号 + 统一认证密码。本模块**只负责认证**——向 CAS 证明「这个人确实是
这个学号的持有者」，成功后返回学号作为身份。

它**不**做这些事：

  · 不保存密码。密码只在下面这一次函数调用的内存里存在，用完即弃，
    绝不写库、绝不写盘、绝不写日志、绝不进模型提示。
  · 不跟随跳转进教务系统，不抓取任何教务/个人数据。CAS 一发票就算认证成功，
    到此为止。
  · 不重试。见下。

## 协议来源

照 [login-ZJU](https://github.com/5dbwat4/login-ZJU)（MIT）与
[Celechron](https://github.com/Celechron/Celechron) 的做法移植，
与本地参考实现 `E:\\zjuhelper` 的 `autologin.py` 同源，不是猜的。

流程：

    ① GET  /cas/login?service=...   → 拿 execution 令牌（顺带种 CAS 会话 cookie）
    ② GET  /cas/v2/getPubKey        → {modulus, exponent}
    ③ POST /cas/login?service=...   → 302 + Location（带 ticket）＝认证成功
                                      200 + <span id="msg">…</span> ＝被拒绝

关键一点：RSA **没有填充**，而且指数 `10001` 是**十六进制**——
所以 Python 里 `pow(value, e, n)` 一行就够，不需要任何加密库。

## 为什么失败绝不重试

连续几次密码错误就可能触发风控把账号锁掉，锁号比"登录失败"麻烦得多。
所以 ③ 一旦返回 200（被拒绝），就把 CAS 的原话抛给用户，让他自己决定，
**本模块不替他重试**。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

CAS_LOGIN = "https://zjuam.zju.edu.cn/cas/login"
PUBKEY_URL = "https://zjuam.zju.edu.cn/cas/v2/getPubKey"

# 只用于向 CAS 换一个一次性 ticket 来验证密码；不跟随、不抓数据。
# 这个 service 值来自参考实现里确认过的教务 SSO 入口，不是猜的。
DEFAULT_SERVICE = "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) campus-agent/0.2 "
    "(personal study tool; authenticates via ZJU CAS only)"
)

EXECUTION_RE = re.compile(r'name="execution"\s+value="([^"]+)"')
ERROR_RE = re.compile(r'<span\s+id="msg">([^<]+)</span>')
TICKET_RE = re.compile(r"[?&]ticket=(ST-[^&\s]+)")


class PassportError(Exception):
    """通行证认证失败。报错要说清是哪种失败、下一步怎么办。"""


@dataclass(frozen=True)
class PassportIdentity:
    """一次成功认证得到的身份。当前只含学号。"""

    student_id: str


# ---------------------------------------------------------------- RSA


def password_to_int(password: str) -> int:
    """把密码变成大整数——**这一步决定登录成不成功，所以跟参考实现逐位对齐**。

    JS 那边是 `for (const c of passwd) pwd = pwd * 256n + BigInt(c.charCodeAt(0))`：
    `charCodeAt(0)` 取的是**第一个 UTF-16 码元**。对 BMP 字符等于码点；
    对辅助平面字符（emoji）取到的是高代理项。这里照做——不照做的话，
    密码里但凡有个 emoji 就会静默对不上，然后被报成"密码错误"，极难排查。
    """
    value = 0
    for char in password:
        unit = ord(char)
        if unit > 0xFFFF:
            unit = 0xD800 + ((unit - 0x10000) >> 10)
        value = value * 256 + unit
    return value


def rsa_encrypt(password: str, modulus_hex: str, exponent_hex: str) -> str:
    """把密码加密成 CAS 要的那串十六进制。

    **没有填充**——教科书式 RSA。指数 `10001` 是**十六进制**的
    （参考实现的原作者在这上面卡了很久）。所以就是 `pow()` 一行，无需加密库。
    """
    value = password_to_int(password)
    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    if value >= modulus:
        raise PassportError("密码过长，超出统一身份认证公钥模长。")
    return format(pow(value, exponent, modulus), "x").zfill(len(modulus_hex))


# ---------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转——认证成功与否就是那个 302 的 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _request(opener, url: str, *, data: bytes | None = None, timeout: float):
    """发一个请求，返回 (状态码, 正文, 响应头)。不跟随跳转。"""
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, dict(exc.headers)
    except urllib.error.URLError as exc:
        raise PassportError(
            f"连不上统一身份认证（{getattr(exc, 'reason', exc)}）。"
            "多数情况是不在校园网 / RVPN。"
        ) from exc


def _fetch_pubkey(opener, *, timeout: float) -> tuple[str, str]:
    status, body, _ = _request(opener, PUBKEY_URL, timeout=timeout)
    if status != 200:
        raise PassportError(f"取统一身份认证公钥失败（HTTP {status}）。")
    try:
        data = json.loads(body)
        return str(data["modulus"]), str(data["exponent"])
    except (ValueError, KeyError) as exc:
        raise PassportError("公钥返回内容无法解析，认证接口可能已改版。") from exc


# ---------------------------------------------------------------- 登录


def login(
    student_id: str,
    password: str,
    *,
    service: str = "",
    timeout: float = 20.0,
) -> PassportIdentity:
    """用学号 + 统一认证密码走一遍 CAS，成功返回 :class:`PassportIdentity`。

    **只尝试一次，失败即抛 :class:`PassportError`，绝不重试**（防风控锁号）。

    `password` 仅在本次调用的内存中用于换取一次性 ticket：不写库、不写盘、
    不写日志，函数返回后即随栈帧丢弃。
    """
    if not student_id or not password:
        raise PassportError("学号和统一身份认证密码都不能为空。")

    service = service or os.environ.get("CAMPUS_CAS_SERVICE") or DEFAULT_SERVICE
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
    )
    login_url = f"{CAS_LOGIN}?{urllib.parse.urlencode({'service': service})}"

    # ---------- ① 登录页，拿 execution（顺带让 CAS 种下会话 cookie）
    status, page, _ = _request(opener, login_url, timeout=timeout)
    if status != 200:
        raise PassportError(
            f"打不开统一身份认证页面（HTTP {status}）。先确认能访问 zjuam.zju.edu.cn。"
        )
    match = EXECUTION_RE.search(page)
    if not match:
        raise PassportError(
            "统一身份认证登录页里没有 execution 令牌，认证系统可能已改版。"
        )
    execution = match.group(1)

    # ---------- ② 公钥 + RSA 加密密码
    modulus, exponent = _fetch_pubkey(opener, timeout=timeout)
    encrypted = rsa_encrypt(password, modulus, exponent)

    # ---------- ③ 提交（只提交一次）
    body = urllib.parse.urlencode(
        {
            "username": student_id,
            "password": encrypted,
            "execution": execution,
            "_eventId": "submit",
            "authcode": "",
        }
    ).encode("ascii")
    status, page, headers = _request(opener, login_url, data=body, timeout=timeout)

    if status in (301, 302, 303, 307, 308):
        location = headers.get("Location") or headers.get("location") or ""
        if TICKET_RE.search(location):
            # CAS 发了一次性 service ticket ＝ 学号密码被接受。到此为止，
            # 不跟随跳转、不进教务系统、不抓任何个人数据。
            return PassportIdentity(student_id=student_id)
        raise PassportError(
            "统一身份认证返回了跳转，但没有 service ticket，认证系统可能已改版。"
        )

    if status == 200:
        # 被拒绝时 CAS 返回 200 + <span id="msg">错误信息</span>。看到就停，不重试。
        message = ERROR_RE.search(page)
        detail = message.group(1).strip() if message else "（页面未给出原因）"
        raise PassportError(
            f"校园通行证认证被拒绝：{detail}\n"
            "常见原因：学号或统一身份认证密码不正确、密码已过期需先修改、"
            "或账号因多次输错被临时锁定。请到 https://zjuam.zju.edu.cn 用浏览器核对一次；"
            "本工具不会替你重试，以免延长锁定。"
        )

    raise PassportError(f"统一身份认证返回了意外的状态码 {status}。")
