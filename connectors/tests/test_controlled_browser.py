#!/usr/bin/env python3
"""单测：受控浏览器登录原型（A17；全部离线——不启动浏览器、不触网、无 playwright 依赖）。

覆盖（红线与纯逻辑为主）：
  - cookie 摘要只含键名/属性：值绝不进入输出（含 secret 注入反例）；
  - 会话标记识别、域分布统计；
  - 只读结果判定真值表（200+JSON=ok；重定向/200-HTML=登录失效形态；500-HTML=unexpected）；
  - 等待循环（假时钟）：启动即只读探针；计数变化/周期兜底触发再探针；探针 ok 判定会话可用；
  - 无凭据形态（A14 §0.2 硬性检查项）：公开接口无 password/credential 类入参；
  - 默认 profile 目录在用户数据目录下、仓库之外（独立 profile，不碰用户主浏览器）。

运行：
  python connectors/tests/test_controlled_browser.py
  python -m unittest discover -s connectors/tests
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.browser import controlled_login as cl  # noqa: E402

SECRET_VALUE = "SECRET-VALUE-9c1f3b"
JWT_VALUE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"


class TestCookieSummary(unittest.TestCase):
    def test_summary_never_contains_values(self):
        cookies = [
            {"name": "JWTUser", "value": SECRET_VALUE, "domain": ".zju.edu.cn",
             "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax", "expires": -1},
            {"name": "iPlanetDirectoryPro", "value": JWT_VALUE, "domain": "zjuam.zju.edu.cn",
             "path": "/", "httpOnly": True, "secure": True, "sameSite": "None", "expires": 1.7e9},
        ]
        summary = cl.cookie_key_summary(cookies)
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(SECRET_VALUE, blob)
        self.assertNotIn(JWT_VALUE, blob)
        self.assertNotIn("value", blob)
        self.assertEqual(2, len(summary))
        by_name = {item["name"]: item for item in summary}
        self.assertTrue(by_name["JWTUser"]["session"])
        self.assertFalse(by_name["iPlanetDirectoryPro"]["session"])
        self.assertTrue(by_name["iPlanetDirectoryPro"]["http_only"])
        self.assertEqual("zjuam.zju.edu.cn", by_name["iPlanetDirectoryPro"]["domain"])

    def test_summary_sorted_and_distribution(self):
        cookies = [
            {"name": "b", "value": "x", "domain": "d2", "path": "/"},
            {"name": "a", "value": "y", "domain": "d1", "path": "/"},
            {"name": "a", "value": "z", "domain": "d1", "path": "/p"},
        ]
        summary = cl.cookie_key_summary(cookies)
        self.assertEqual([("a", "/"), ("a", "/p"), ("b", "/")],
                         [(i["name"], i["path"]) for i in summary])
        self.assertEqual({"d1": 2, "d2": 1}, cl.domain_distribution(summary))

    def test_session_marker(self):
        self.assertTrue(cl.has_session_marker([{"name": "JWTUser", "value": "s"}]))
        self.assertFalse(cl.has_session_marker([{"name": "JWTUSER", "value": "s"}]))
        self.assertFalse(cl.has_session_marker([{"name": "route", "value": "s"}]))


class TestClassifyReadResult(unittest.TestCase):
    def test_truth_table(self):
        ok = cl.classify_read_result(200, "application/json; charset=utf-8", True, False)
        self.assertEqual("ok", ok)
        self.assertEqual("auth_required", cl.classify_read_result(200, "text/html", False, False))
        self.assertEqual("auth_required", cl.classify_read_result(200, "application/json", False, True))
        self.assertEqual("auth_required", cl.classify_read_result(302, "", False, True))
        self.assertEqual("unexpected", cl.classify_read_result(500, "text/html", False, False))
        self.assertEqual("unexpected", cl.classify_read_result(403, "application/json", True, False))
        self.assertEqual("unexpected", cl.classify_read_result(200, "application/json", False, False))


class TestNoCredentialInterface(unittest.TestCase):
    """A14 §0.2 硬性检查项：接口无凭据入参（反例性检查，仅测试允许出现关键字）。"""

    FORBIDDEN = ("password", "passwd", "pwd", "credential", "secret")

    def test_public_signatures_have_no_credential_params(self):
        for name, obj in vars(cl).items():
            if name.startswith("_"):
                continue
            if inspect.isfunction(obj) or inspect.isclass(obj):
                try:
                    sig = inspect.signature(obj)
                except (TypeError, ValueError):
                    continue
                for pname in sig.parameters:
                    low = pname.lower()
                    for bad in self.FORBIDDEN:
                        self.assertNotIn(bad, low, f"{name}({pname}) 命中禁用入参")

    def test_no_def_line_declares_credential_params(self):
        src = Path(cl.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                for bad in self.FORBIDDEN:
                    self.assertNotIn(bad, stripped.lower(),
                                     f"def 行出现禁用入参：{stripped}")


class TestProfileDir(unittest.TestCase):
    def test_default_profile_dir_outside_repo_and_independent(self):
        p = str(cl.default_profile_dir())
        self.assertNotIn(str(ROOT), p)          # 仓库之外（不随仓库提交）
        self.assertIn("zjulearn", p.lower())     # 独立目录族
        self.assertIn("controlled-browser", p.lower())
        lowered = p.lower()
        self.assertNotIn("microsoft\\edge", lowered)   # 不读写用户主浏览器 profile
        self.assertNotIn("google\\chrome", lowered)

    def test_logout_and_entry_constants(self):
        self.assertEqual("https://course.zju.edu.cn/ua/login", cl.LOGIN_ENTRY_URL)
        self.assertTrue(cl.LOGOUT_URL.startswith("https://courses.zju.edu.cn/ua/logout"))
        self.assertTrue(cl.READ_CHECK_PAGE.startswith("https://courses.zju.edu.cn/"))


class TestBrowserTempDir(unittest.TestCase):
    def test_default_temp_prefers_profile_sibling_and_checks_writable(self):
        profile = Path("C:/data/zjulearn/controlled-browser/profile")
        expected = profile.parent / "tmp"
        with mock.patch.dict(os.environ, {cl.BROWSER_TEMP_ENV: ""}, clear=False), \
                mock.patch.object(cl, "_ensure_writable_dir", side_effect=lambda path: path) as ensure:
            self.assertEqual(expected, cl.browser_temp_dir(profile))
        ensure.assert_called_once_with(expected)

    def test_explicit_env_override(self):
        target = Path("D:/writable/browser-tmp")
        profile = Path("C:/data/zjulearn/controlled-browser/profile")
        with mock.patch.dict(os.environ, {cl.BROWSER_TEMP_ENV: str(target)}, clear=False), \
                mock.patch.object(cl, "_ensure_writable_dir", side_effect=lambda path: path) as ensure:
            self.assertEqual(target, cl.browser_temp_dir(profile))
        ensure.assert_called_once_with(target)

    def test_falls_back_to_repo_var_when_profile_sibling_is_unwritable(self):
        profile = Path("C:/readonly/profile")
        expected = cl._ROOT / "var" / "tmp"

        def ensure(path):
            if path == profile.parent / "tmp":
                raise PermissionError(str(path))
            return path

        with mock.patch.dict(os.environ, {cl.BROWSER_TEMP_ENV: ""}, clear=False), \
                mock.patch.object(cl, "_ensure_writable_dir", side_effect=ensure):
            self.assertEqual(expected, cl.browser_temp_dir(profile))

    def test_configure_browser_temp_sets_process_and_node_env(self):
        target = Path("D:/writable/browser-tmp")
        profile = Path("C:/data/zjulearn/controlled-browser/profile")
        with mock.patch.object(cl, "browser_temp_dir", return_value=target), \
                mock.patch.dict(os.environ, {"TEMP": "old", "TMP": "old"}, clear=False), \
                mock.patch.object(cl.tempfile, "tempdir", "old"):
            self.assertEqual(target, cl.configure_browser_temp(profile))
            self.assertEqual(str(target), os.environ["TEMP"])
            self.assertEqual(str(target), os.environ["TMP"])
            self.assertEqual(str(target), cl.tempfile.tempdir)


class TestWritableProfileDir(unittest.TestCase):
    def test_prefers_requested_profile_dir(self):
        profile = Path("C:/data/zjulearn/controlled-browser/profile")
        with mock.patch.dict(os.environ, {cl.BROWSER_PROFILE_ENV: ""}, clear=False), \
                mock.patch.object(cl, "_ensure_writable_dir", side_effect=lambda path: path) as ensure:
            self.assertEqual(profile, cl.writable_profile_dir(profile))
        ensure.assert_called_once_with(profile)

    def test_falls_back_to_repo_var_when_requested_not_writable(self):
        profile = Path("C:/readonly/profile")
        expected = cl._ROOT / "var" / "controlled-browser" / "profile"

        def ensure(path):
            if path == profile:
                raise PermissionError(str(path))
            return path

        with mock.patch.dict(os.environ, {cl.BROWSER_PROFILE_ENV: ""}, clear=False), \
                mock.patch.object(cl, "_ensure_writable_dir", side_effect=ensure):
            self.assertEqual(expected, cl.writable_profile_dir(profile))

    def test_explicit_env_override_wins(self):
        target = Path("D:/writable/browser-profile")
        profile = Path("C:/readonly/profile")
        with mock.patch.dict(os.environ, {cl.BROWSER_PROFILE_ENV: str(target)}, clear=False), \
                mock.patch.object(cl, "_ensure_writable_dir", side_effect=lambda path: path) as ensure:
            self.assertEqual(target, cl.writable_profile_dir(profile))
        ensure.assert_called_once_with(target)


class TestRedactionIntegration(unittest.TestCase):
    def test_signed_url_never_kept_in_logs_or_results(self):
        url = "https://tcmedia.zju.edu.cn/download/file/123?timestamp=999&token=SECRET&name=x"
        self.assertNotIn("SECRET", cl.redact_url(url))
        self.assertNotIn("timestamp", cl.redact_url(url))

    def test_module_source_has_redacting_filter_attached(self):
        src = Path(cl.__file__).read_text(encoding="utf-8")
        self.assertIn("RedactingFilter", src)
        self.assertTrue(any(isinstance(f, cl.RedactingFilter) for f in cl.LOG.filters))


class _FakeClock:
    """假时钟：sleep 只推进虚拟时间（避免真实等待）。"""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeContext:
    """脚本化 cookie 序列；探针页只需要一个非空句柄。"""

    def __init__(self, cookie_seq):
        self._seq = cookie_seq
        self._i = 0

    def cookies(self):
        value = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return value

    def new_page(self):
        return object()


class TestWaitForSession(unittest.TestCase):
    """等待循环（假时钟，不启动浏览器）：探针触发条件与判定。"""

    def test_immediate_probe_then_count_change_detects(self):
        c1 = [{"name": "a", "value": "v"}]
        c2 = [{"name": "a", "value": "v"}, {"name": "b", "value": "w"}]
        ctx = _FakeContext([c1, c1, c1, c1, c1, c2, c2])
        clock = _FakeClock()
        with mock.patch.object(cl, "time", clock), \
                mock.patch.object(cl, "read_check_in_page",
                                  side_effect=[{"verdict": "auth_required"},
                                               {"verdict": "ok"}]) as probe:
            wait = cl._wait_for_session(ctx, timeout_s=60.0)
        self.assertTrue(wait["detected"])
        self.assertEqual(2, wait["probe_attempts"])
        self.assertEqual(2, probe.call_count)
        self.assertEqual([1, 2], [e["count"] for e in wait["cookie_count_history"]])

    def test_no_signal_times_out_with_history(self):
        c1 = [{"name": "a", "value": "v"}]
        ctx = _FakeContext([c1])
        clock = _FakeClock()
        with mock.patch.object(cl, "time", clock), \
                mock.patch.object(cl, "read_check_in_page",
                                  return_value={"verdict": "auth_required"}):
            wait = cl._wait_for_session(ctx, timeout_s=10.0)
        self.assertFalse(wait["detected"])
        self.assertEqual(1, wait["probe_attempts"])
        self.assertEqual([1], [e["count"] for e in wait["cookie_count_history"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
