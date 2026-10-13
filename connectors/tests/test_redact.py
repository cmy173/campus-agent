#!/usr/bin/env python3
"""A8 单测：记录前脱敏（redact.py）。

覆盖：URL 查询参数剔除（?token= 形态）、userinfo 剥离、Cookie/Authorization 头、
Bearer/Basic、JWT 片段、CAS 票据、dict/list 递归、logging 兜底过滤器。

运行：python connectors/tests/test_redact.py
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.redact import (  # noqa: E402
    REDACTED,
    RedactingFilter,
    redact,
    redact_mapping,
    redact_text,
    redact_url,
)

# 合成 JWT（非真实令牌，仅形态模拟）
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOiJ1MDAxIiwibmFtZSI6InN5bnRoZXRpYyJ9.c2lnbmF0dXJlLXN5bnRoZXRpYy0wMDE"


class TestRedactUrl(unittest.TestCase):
    def test_invalid_urls_are_masked_without_raising(self):
        for url in ("http://host:bad/p?token=secret", "http://host:99999/p", "http://[broken/p"):
            self.assertEqual(REDACTED, redact_url(url))
    def test_query_and_fragment_removed(self):
        out = redact_url(f"https://classroom.zju.edu.cn/courseapi/v2/x?token={FAKE_JWT}&a=1#frag")
        self.assertEqual("https://classroom.zju.edu.cn/courseapi/v2/x", out)
        self.assertNotIn("token", out)
        self.assertNotIn("?", out)
        self.assertNotIn("#", out)

    def test_oss_download_token_form(self):
        out = redact_url("https://course.zju.edu.cn/oss/download?token=abc123&filename=第03讲.pdf")
        self.assertEqual("https://course.zju.edu.cn/oss/download", out)
        self.assertNotIn("abc123", out)

    def test_userinfo_stripped(self):
        self.assertEqual("https://host/p", redact_url("https://user:pass@host/p?x=1"))

    def test_relative_url_keeps_path_only(self):
        self.assertEqual("/oss/download", redact_url("/oss/download?token=xyz&filename=a.pdf"))

    def test_clean_url_unchanged(self):
        url = "https://courses.zju.edu.cn/course/10001/content"
        self.assertEqual(url, redact_url(url))

    def test_ports_and_ipv6_kept(self):
        self.assertEqual("http://127.0.0.1:8123/callback", redact_url("http://127.0.0.1:8123/callback?state=x"))
        self.assertEqual("http://[::1]:80/p", redact_url("http://[::1]:80/p?q=1"))


class TestRedactText(unittest.TestCase):
    def test_embedded_url_redacted(self):
        text = f"GET https://classroom.zju.edu.cn/api/week?token={FAKE_JWT} 200"
        out = redact_text(text)
        self.assertEqual("GET https://classroom.zju.edu.cn/api/week 200", out)

    def test_trailing_punctuation_preserved(self):
        out = redact_text("见 https://example.zju.edu.cn/p?x=1。")
        self.assertEqual("见 https://example.zju.edu.cn/p。", out)

    def test_authorization_header_line(self):
        out = redact_text("Authorization: Bearer abcdef123456\nnext")
        self.assertEqual(f"Authorization: {REDACTED}\nnext", out)

    def test_cookie_and_set_cookie_lines(self):
        out = redact_text("Cookie: JSESSIONID=abc123; theme=dark\nSet-Cookie: sid=xyz")
        self.assertEqual(f"Cookie: {REDACTED}\nSet-Cookie: {REDACTED}", out)

    def test_bearer_inline(self):
        out = redact_text("used credential Bearer aaa.bbb.ccccccc1234 inline")
        self.assertEqual(f"used credential Bearer {REDACTED} inline", out)

    def test_jwt_fragment(self):
        out = redact_text(f"jwt={FAKE_JWT} 结束")
        self.assertNotIn("eyJ", out)
        self.assertIn(REDACTED, out)

    def test_query_token_form_in_plain_text(self):
        out = redact_text("重放 ?token=secret-value&page=2")
        self.assertEqual(f"重放 ?token={REDACTED}&page=2", out)

    def test_non_sensitive_query_kept(self):
        out = redact_text("分页 page=2&page_size=20&tenant_code=112")
        self.assertEqual("分页 page=2&page_size=20&tenant_code=112", out)

    def test_cas_ticket(self):
        out = redact_text("回跳 ticket=ST-1234567890-abcdef 完成")
        self.assertNotIn("ST-1234567890-abcdef", out)

    def test_plain_text_unchanged(self):
        text = "同步完成：扫描 14 项，新增 3 项，无删除"
        self.assertEqual(text, redact_text(text))


class TestRedactMapping(unittest.TestCase):
    def test_sensitive_keys_removed(self):
        payload = {
            "headers": {"Cookie": "a=b", "Authorization": "Bearer xyz", "Accept": "json"},
            "url": "https://h/p?token=abc",
            "page": 2,
            "items": [{"jwt": FAKE_JWT}, "https://h/q?code=1"],
        }
        out = redact_mapping(payload)
        self.assertEqual(REDACTED, out["headers"]["Cookie"])
        self.assertEqual(REDACTED, out["headers"]["Authorization"])
        self.assertEqual("json", out["headers"]["Accept"])
        self.assertEqual("https://h/p", out["url"])
        self.assertEqual(2, out["page"])
        self.assertEqual(REDACTED, out["items"][0]["jwt"])
        self.assertEqual("https://h/q", out["items"][1])

    def test_tuple_and_non_string_types_preserved(self):
        out = redact_mapping(("https://h/p?t=1", 5, None, True))
        self.assertEqual(("https://h/p", 5, None, True), out)

    def test_dispatcher(self):
        self.assertEqual({"password": REDACTED}, redact({"password": "p"}))
        self.assertEqual("https://h/p", redact("https://h/p?token=1"))
        self.assertEqual(7, redact(7))


class TestRedactingFilter(unittest.TestCase):
    def test_connector_ready_log_formats_port_and_preserves_chinese_suffix(self):
        logger, messages = self._capture()
        logger.info("连接器服务已就绪：http://%s:%d（受控浏览器窗口已打开；请在窗口内完成登录）",
                    "127.0.0.1", 8765)
        self.assertEqual("连接器服务已就绪：http://127.0.0.1:8765（受控浏览器窗口已打开；请在窗口内完成登录）", messages[0])

    def test_url_template_query_is_redacted_after_formatting(self):
        logger, messages = self._capture()
        logger.info("GET https://%s:%d/p?token=%s（完成）", "host", 443, "secret-value")
        self.assertEqual("GET https://host:443/p（完成）", messages[0])

    def test_redacted_percent_sign_is_not_formatted_twice(self):
        logger, messages = self._capture()
        logger.info("进度 %d%%，url=%s", 50, "https://h/p%20x?token=secret")
        self.assertEqual("进度 50%，url=https://h/p%20x", messages[0])

    def test_bad_log_format_does_not_crash_or_leak_args(self):
        logger, messages = self._capture()
        logger.info("port=%d", "secret-value")
        self.assertNotIn("secret-value", messages[0])
        self.assertIn(REDACTED, messages[0])

    def _capture(self):
        logger = logging.getLogger("connectors-test-redact")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers = []
        logger.filters = []
        messages: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        logger.addHandler(_Handler())
        logger.addFilter(RedactingFilter())
        return logger, messages

    def test_filter_redacts_msg_and_args(self):
        logger, messages = self._capture()
        logger.info("fetch url=%s", "https://classroom.zju.edu.cn/x?token=abc123")
        logger.info("headers=%s", {"Authorization": "Bearer zzz", "page": 1})
        self.assertEqual("fetch url=https://classroom.zju.edu.cn/x", messages[0])
        self.assertNotIn("zzz", messages[1])
        self.assertIn(REDACTED, messages[1])

    def test_filter_redacts_dict_args(self):
        logger, messages = self._capture()
        logger.info("url=%(url)s page=%(page)s",
                    {"url": "https://h/p?token=abc", "page": 3})
        self.assertEqual("url=https://h/p page=3", messages[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
