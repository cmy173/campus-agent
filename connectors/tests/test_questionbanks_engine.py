#!/usr/bin/env python3
"""A-复习卷单测：检索编排引擎（多来源/部分失败/重试/取消/缓存/去重/限额）。

覆盖 01-A §5 清单：成功检索、空结果、重复题去重、答案缺失保留、错误 HTML、
登录页、429（Retry-After）、超时重试、取消、超大正文截断、重定向、来源配置错误、
fetch_candidate 刷新（未变/移位/消失）、capabilities。

运行：python -m unittest connectors.tests.test_questionbanks_engine -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.questionbanks import (  # noqa: E402
    PageCache,
    QuestionBankCancelled,
    QuestionBankError,
    SearchRequest,
    fetch_candidate,
    get_questionbank_capabilities,
    search_candidates,
)
from connectors.questionbanks.types import NETWORK_ERROR  # noqa: E402
from connectors.tests.fakes_questionbanks import (  # noqa: E402
    FakeClock,
    ScriptedPageTransport,
    fixture_text,
    page,
    redirect,
)

SEARX = "https://searx.example/search"
Q1 = "https://qbpages.example/q/1"
Q2 = "https://qbpages.example/q/2"
Q3 = "https://qbpages.example/q/3"
BANK = "https://bank.example/api/search"


def searx_request(**overrides) -> SearchRequest:
    fields = dict(
        query="高等数学 导数 计算",
        keywords=["高等数学", "导数", "计算"],
        sources=[{"source_id": "searx-1", "kind": "search_service",
                  "provider": "searxng", "base_url": "https://searx.example"}],
    )
    fields.update(overrides)
    return SearchRequest(**fields)


def search_routes(urls: list[str]) -> dict:
    payload = {"results": [{"url": u, "title": f"p{i}", "content": "摘要不得当题干"}
                           for i, u in enumerate(urls)]}
    return {SEARX: page(200, json.dumps(payload))}


def codes(warnings: list[dict]) -> list[str]:
    return [w.get("code") for w in warnings]


class TestSearchSuccess(unittest.TestCase):
    def test_full_success_pipeline(self):
        transport = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(200, fixture_text("page_questions.html")),
        })
        outcome = search_candidates(searx_request(), transport,
                                    sleep=FakeClock(), allow_private_addresses=False)
        self.assertEqual("ok", outcome.status)
        self.assertEqual(1, len(outcome.sources))
        self.assertEqual("ok", outcome.sources[0].status)
        self.assertEqual(1, outcome.sources[0].pages_fetched)
        self.assertEqual(6, len(outcome.candidates))
        top = outcome.candidates[0]
        for key in ("candidate_id", "stem", "type", "options", "answer", "analysis",
                    "knowledge_point", "difficulty", "source_url", "source_site",
                    "source_locator", "fetched_at", "origin", "license_note",
                    "match_reason", "match_score", "extract_confidence", "source_id"):
            self.assertIn(key, top)
        self.assertEqual("online", top["origin"])
        self.assertTrue(top["candidate_id"].startswith("qb_"))
        # 排序：命中关键词者优先（至少分数单调不降）
        scores = [c["match_score"] for c in outcome.candidates]
        self.assertEqual(sorted(scores, reverse=True), scores)
        # 搜索参数：format=json 且查询词拼接，摘要字段不参与候选生成
        call = transport.calls_for(SEARX)[0]
        self.assertEqual("json", call["params"]["format"])
        self.assertEqual("高等数学 导数 计算", call["params"]["q"])
        for c in outcome.candidates:
            self.assertNotIn("摘要不得当题干", c["stem"])

    def test_empty_results_warns_no_results(self):
        transport = ScriptedPageTransport({SEARX: page(200, fixture_text("search_searxng_empty.json"))})
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("ok", outcome.status)
        self.assertEqual([], outcome.candidates)
        self.assertIn("NO_RESULTS", codes(outcome.warnings))

    def test_search_html_instead_of_json_is_upstream_changed(self):
        transport = ScriptedPageTransport({
            SEARX: page(200, fixture_text("search_searxng_html.html"), headers={"Content-Type": "text/html"})})
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)
        self.assertEqual("failed", outcome.sources[0].status)
        self.assertEqual("UPSTREAM_CHANGED", outcome.sources[0].error["code"])
        self.assertFalse(outcome.sources[0].error["retryable"])

    def test_partial_failure_other_pages_still_delivered(self):
        transport = ScriptedPageTransport({
            **search_routes([Q1, Q2, Q3]),
            Q1: page(200, fixture_text("page_questions.html")),
            Q2: page(200, fixture_text("page_login.html")),
            Q3: page(200, fixture_text("page_empty.html")),
        })
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("partial", outcome.status)
        self.assertEqual("partial", outcome.sources[0].status)
        self.assertEqual(6, len(outcome.candidates))
        self.assertIn("AUTH_REQUIRED", codes(outcome.warnings))
        self.assertIn("UNPARSABLE", codes(outcome.warnings))

    def test_all_pages_fail_source_failed_no_raise(self):
        transport = ScriptedPageTransport({
            **search_routes([Q2]),
            Q2: page(200, fixture_text("page_login.html")),
        })
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)  # 全失败返回失败而非抛异常
        self.assertEqual("failed", outcome.sources[0].status)
        self.assertEqual("AUTH_REQUIRED", outcome.sources[0].error["code"])
        self.assertEqual([], outcome.candidates)

    def test_page_404_reported_as_network_error(self):
        transport = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(404, "not found"),
        })
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)
        self.assertEqual(404, outcome.sources[0].error["details"]["status"])

    def test_cross_source_dedupe(self):
        transport = ScriptedPageTransport({
            Q1: page(200, fixture_text("page_questions.html")),
            "https://qbpages.example/q/1-copy": page(200, fixture_text("page_questions.html")),
        })
        request = SearchRequest(
            query="导数", keywords=["导数"],
            sources=[{"source_id": "u1", "kind": "url_import", "urls": [Q1]},
                     {"source_id": "u2", "kind": "url_import",
                      "urls": ["https://qbpages.example/q/1-copy"]}])
        outcome = search_candidates(request, transport, sleep=FakeClock())
        self.assertEqual(6, len(outcome.candidates))  # 同题干跨来源只保留一条
        dup = [w for w in outcome.warnings if w["code"] == "DUPLICATE_DROPPED"]
        self.assertEqual(1, len(dup))
        self.assertEqual(6, dup[0]["details"]["dropped_total"])

    def test_max_candidates_cap_and_type_filter(self):
        transport = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(200, fixture_text("page_questions.html")),
        })
        outcome = search_candidates(searx_request(max_candidates=2), transport, sleep=FakeClock())
        self.assertEqual(2, len(outcome.candidates))
        self.assertIn("CANDIDATES_CAPPED", codes(outcome.warnings))

        transport2 = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(200, fixture_text("page_questions.html")),
        })
        outcome2 = search_candidates(searx_request(question_types=["choice"]), transport2, sleep=FakeClock())
        self.assertTrue(outcome2.candidates)
        self.assertTrue(all(c["type"] == "choice" for c in outcome2.candidates))


class TestRetryAndThrottle(unittest.TestCase):
    def test_429_with_retry_after_is_honored(self):
        clock = FakeClock()
        transport = ScriptedPageTransport({
            SEARX: [page(429, "slow down", headers={"Retry-After": "2"}),
                    page(200, json.dumps({"results": [{"url": Q1}]}))],
            Q1: page(200, fixture_text("page_questions.html")),
        })
        outcome = search_candidates(searx_request(), transport, sleep=clock)
        self.assertEqual("ok", outcome.status)
        self.assertEqual(6, len(outcome.candidates))
        self.assertEqual([2.0], clock.sleeps)  # Retry-After=2 被遵守

    def test_429_exhausted_returns_rate_limited(self):
        clock = FakeClock()
        transport = ScriptedPageTransport({
            SEARX: [page(429, "slow down", headers={"Retry-After": "5"}),
                    page(429, "slow down", headers={"Retry-After": "5"})],
        })
        outcome = search_candidates(searx_request(max_retries=1), transport, sleep=clock)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("RATE_LIMITED", outcome.sources[0].error["code"])
        self.assertTrue(outcome.sources[0].error["retryable"])
        self.assertEqual([5.0], clock.sleeps)

    def test_network_timeout_retried_then_failed(self):
        clock = FakeClock()
        timeout = QuestionBankError(NETWORK_ERROR, "请求超时", retryable=True,
                                    details={"reason": "timeout"})
        transport = ScriptedPageTransport({SEARX: [timeout, timeout]})
        outcome = search_candidates(searx_request(max_retries=1), transport, sleep=clock)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("NETWORK_ERROR", outcome.sources[0].error["code"])
        self.assertEqual([1.0], clock.sleeps)  # 指数退避首个等待 = 1s

    def test_401_not_retried(self):
        clock = FakeClock()
        transport = ScriptedPageTransport({SEARX: page(401, "unauthorized")})
        outcome = search_candidates(searx_request(), transport, sleep=clock)
        self.assertEqual("failed", outcome.status)
        self.assertEqual("AUTH_REQUIRED", outcome.sources[0].error["code"])
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, len(transport.calls))  # 401 不重复请求


class TestRedirectsAndSsrF(unittest.TestCase):
    def test_same_host_redirect_followed_with_headers(self):
        transport = ScriptedPageTransport({
            "https://searx.example/search": redirect("/search2"),
            "https://searx.example/search2": page(200, json.dumps({"results": [{"url": Q1}]})),
            Q1: page(200, fixture_text("page_questions.html")),
        })
        request = searx_request()
        request.sources[0]["headers"] = {"X-Test-Api-Key": "user-key"}
        outcome = search_candidates(request, transport, sleep=FakeClock())
        self.assertEqual("ok", outcome.status)
        second = transport.calls_for("https://searx.example/search2")[0]
        self.assertEqual("user-key", second["headers"].get("X-Test-Api-Key"))

    def test_cross_host_redirect_drops_custom_headers(self):
        transport = ScriptedPageTransport({
            "https://searx.example/search": redirect("https://mirror.example/search"),
            "https://mirror.example/search": page(200, json.dumps({"results": []})),
        })
        request = searx_request()
        request.sources[0]["headers"] = {"Authorization": "Bearer user-token"}
        outcome = search_candidates(request, transport, sleep=FakeClock())
        self.assertEqual("ok", outcome.status)  # 跨主机可跟，但不得携带自定义头
        first = transport.calls_for("https://searx.example/search")[0]
        second = transport.calls_for("https://mirror.example/search")[0]
        self.assertEqual("Bearer user-token", first["headers"].get("Authorization"))
        self.assertNotIn("Authorization", second["headers"])  # 凭据不跨主机扩散

    def test_https_downgrade_blocked(self):
        transport = ScriptedPageTransport({
            SEARX: redirect("http://searx.example/insecure"),
        })
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)
        self.assertEqual("https_downgrade_blocked",
                         outcome.sources[0].error["details"]["reason"])

    def test_too_many_redirects_blocked(self):
        routes = {SEARX: redirect("/r1")}
        routes.update({f"https://searx.example/r{i}": redirect(f"/r{i+1}") for i in range(1, 5)})
        transport = ScriptedPageTransport(routes)
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)
        self.assertEqual("too_many_redirects", outcome.sources[0].error["details"]["reason"])

    def test_ssrf_blocked_private_targets(self):
        request = SearchRequest(query="q", keywords=["q"], sources=[
            {"source_id": "u1", "kind": "url_import",
             "urls": ["http://127.0.0.1:8080/admin", "http://10.0.0.8/x"]}])
        transport = ScriptedPageTransport({})
        outcome = search_candidates(request, transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)
        self.assertEqual([], transport.calls)  # 请求在传输层之前被阻断
        self.assertTrue(all("阻断" in w["message"] for w in outcome.warnings
                            if w["code"] == NETWORK_ERROR))


class TestCancellationAndCache(unittest.TestCase):
    def test_cancellation_stops_run(self):
        class CancelAfter:
            def __init__(self, n):
                self.n = n
                self.count = 0

            def is_cancelled(self):
                self.count += 1
                return self.count > self.n

        transport = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(200, fixture_text("page_questions.html")),
        })
        with self.assertRaises(QuestionBankCancelled):
            search_candidates(searx_request(), transport, sleep=FakeClock(),
                              cancellation=CancelAfter(1))

    def test_cache_prevents_second_fetch(self):
        transport = ScriptedPageTransport({
            SEARX: [page(200, json.dumps({"results": [{"url": Q1}]})),
                    page(200, json.dumps({"results": [{"url": Q1}]}))],
            Q1: page(200, fixture_text("page_questions.html")),
        })
        cache = PageCache(ttl_s=600)
        first = search_candidates(searx_request(), transport, sleep=FakeClock(), cache=cache)
        second = search_candidates(searx_request(), transport, sleep=FakeClock(), cache=cache)
        self.assertEqual(6, len(first.candidates))
        self.assertEqual(6, len(second.candidates))
        self.assertEqual(1, len(transport.calls_for(Q1)))  # 第二次命中缓存
        self.assertIn("CACHE_HIT", codes(second.warnings))

    def test_page_truncation_warns(self):
        transport = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(200, fixture_text("page_questions.html"), truncated=True),
        })
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        self.assertIn("PAGE_TRUNCATED", codes(outcome.warnings))
        self.assertEqual(6, len(outcome.candidates))


class TestRequestValidation(unittest.TestCase):
    def test_not_configured_raises(self):
        with self.assertRaises(QuestionBankError) as ctx:
            search_candidates(SearchRequest(query="x"), ScriptedPageTransport({}), sleep=FakeClock())
        self.assertEqual("NOT_CONFIGURED", ctx.exception.code)

    def test_invalid_request_raises(self):
        with self.assertRaises(QuestionBankError) as ctx:
            search_candidates(SearchRequest(query="  ", keywords=[], sources=[{"kind": "url_import", "urls": [Q1]}]),
                              ScriptedPageTransport({}), sleep=FakeClock())
        self.assertEqual("INVALID_REQUEST", ctx.exception.code)
        with self.assertRaises(QuestionBankError) as ctx2:
            search_candidates(SearchRequest(query="x", keywords=["x"], sources=[{"kind": "nope"}]),
                              ScriptedPageTransport({}), sleep=FakeClock())
        self.assertEqual("INVALID_REQUEST", ctx2.exception.code)
        with self.assertRaises(QuestionBankError) as ctx3:
            search_candidates("not-a-request", ScriptedPageTransport({}), sleep=FakeClock())
        self.assertEqual("INVALID_REQUEST", ctx3.exception.code)

    def test_dict_request_accepted(self):
        transport = ScriptedPageTransport({Q1: page(200, fixture_text("page_questions.html"))})
        outcome = search_candidates(
            {"query": "导数", "sources": [{"source_id": "u", "kind": "url_import", "urls": [Q1]}]},
            transport, sleep=FakeClock())
        self.assertEqual(6, len(outcome.candidates))

    def test_mixed_invalid_and_valid_sources(self):
        transport = ScriptedPageTransport({Q1: page(200, fixture_text("page_questions.html"))})
        request = SearchRequest(query="x", keywords=["x"], sources=[
            {"source_id": "bad", "kind": "search_service", "provider": "google"},
            {"source_id": "good", "kind": "url_import", "urls": [Q1]}])
        outcome = search_candidates(request, transport, sleep=FakeClock())
        self.assertEqual("partial", outcome.status)
        bad = [s for s in outcome.sources if s.source_id == "bad"][0]
        self.assertEqual("failed", bad.status)
        self.assertIn("INVALID_REQUEST", bad.error["code"])
        self.assertEqual(6, len(outcome.candidates))


class TestBankApiSource(unittest.TestCase):
    def bank_request(self, **cfg_overrides):
        cfg = {"source_id": "bank-1", "kind": "bank_api",
               "base_url": "https://bank.example", "search_path": "/api/search"}
        cfg.update(cfg_overrides)
        return SearchRequest(query="数列", keywords=["数列"], sources=[cfg])

    def test_items_normalized_with_sanitized_url_and_missing_answer_kept(self):
        transport = ScriptedPageTransport({
            BANK: page(200, fixture_text("bank_api_items.json"))})
        outcome = search_candidates(self.bank_request(), transport, sleep=FakeClock())
        self.assertEqual("partial", outcome.status)  # 缺字段条目有警告
        self.assertEqual(2, len(outcome.candidates))  # 缺题干条目×2 被跳过并告警
        by_stem = {c["stem"][:8]: c for c in outcome.candidates}
        first = [c for c in outcome.candidates if c["stem"].startswith("求数列")][0]
        self.assertEqual("calculation", first["type"])
        self.assertEqual("2 - 2^{1-n}", first["answer"])
        self.assertIn("等比数列", first["knowledge_point"])
        self.assertEqual("中等", first["difficulty"])
        self.assertNotIn("SECRET", first["source_url"])  # 敏感查询参数被剔除
        self.assertIn("debug=1", first["source_url"])
        self.assertEqual("示例题库；仅测试用途", first["license_note"])
        judgment = [c for c in outcome.candidates if c["stem"].startswith("判断")][0]
        self.assertEqual("judgment", judgment["type"])
        self.assertIsNone(judgment["answer"])  # 缺答案如实为空
        self.assertTrue(any("缺少题干" in w["message"] for w in outcome.warnings))

    def test_field_map_remaps_stem_key(self):
        transport = ScriptedPageTransport({
            BANK: page(200, fixture_text("bank_api_items.json"))})
        outcome = search_candidates(
            self.bank_request(field_map={"stem": "name"}), transport, sleep=FakeClock())
        self.assertTrue(any(c["stem"].startswith("字段名不同") for c in outcome.candidates))

    def test_weird_shape_upstream_changed(self):
        transport = ScriptedPageTransport({
            BANK: page(200, fixture_text("bank_api_weird.json"))})
        outcome = search_candidates(self.bank_request(), transport, sleep=FakeClock())
        self.assertEqual("failed", outcome.status)
        self.assertEqual("UPSTREAM_CHANGED", outcome.sources[0].error["code"])

    def test_empty_items_no_results(self):
        transport = ScriptedPageTransport({BANK: page(200, json.dumps({"items": []}))})
        outcome = search_candidates(self.bank_request(), transport, sleep=FakeClock())
        self.assertEqual("ok", outcome.status)
        self.assertEqual([], outcome.candidates)
        self.assertIn("NO_RESULTS", codes(outcome.warnings))


class TestFetchCandidate(unittest.TestCase):
    def _first_candidate(self) -> dict:
        transport = ScriptedPageTransport({
            **search_routes([Q1]),
            Q1: page(200, fixture_text("page_questions.html")),
        })
        outcome = search_candidates(searx_request(), transport, sleep=FakeClock())
        return [c for c in outcome.candidates if c["stem"].startswith("设函数")][0]

    def test_refresh_unchanged(self):
        handle = self._first_candidate()
        transport = ScriptedPageTransport({Q1: page(200, fixture_text("page_questions.html"))})
        refreshed = fetch_candidate(handle, transport, sleep=FakeClock())
        self.assertEqual("unchanged", refreshed["refresh"]["status"])
        self.assertEqual(handle["candidate_id"], refreshed["candidate_id"])
        # 刷新保留句柄匹配元数据，不得以题干自我匹配虚高
        self.assertEqual(handle["match_score"], refreshed["match_score"])
        self.assertEqual(handle["match_keywords"], refreshed["match_keywords"])

    def test_refresh_relocated(self):
        handle = self._first_candidate()
        modified = fixture_text("page_questions.html").replace(
            "<div class=\"question\">\n      <p>1. 设函数 f(x)=x^2+2x",
            "<div class=\"question\">\n      <p>0. 新增前置题：2+2 等于几？</p>\n      <p>A. 3</p>\n      <p>B. 4</p>\n      <p>答案：B</p>\n    </div>\n\n    <div class=\"question\">\n      <p>1. 设函数 f(x)=x^2+2x")
        transport = ScriptedPageTransport({Q1: page(200, modified)})
        refreshed = fetch_candidate(handle, transport, sleep=FakeClock())
        self.assertEqual("relocated", refreshed["refresh"]["status"])
        self.assertIn("设函数", refreshed["stem"])
        self.assertLess(refreshed["extract_confidence"], handle["extract_confidence"])

    def test_refresh_gone_raises_unparsable(self):
        handle = self._first_candidate()
        other_page = ("<html><body><div class=\"question\"><p>1. 完全不同的另一道题（选 B）。</p>"
                      "<p>A. 甲</p><p>B. 乙</p><p>答案：B</p></div></body></html>")
        transport = ScriptedPageTransport({Q1: page(200, other_page)})
        with self.assertRaises(QuestionBankError) as ctx:
            fetch_candidate(handle, transport, sleep=FakeClock())
        self.assertEqual("UNPARSABLE", ctx.exception.code)
        self.assertEqual("candidate_not_found_after_refetch", ctx.exception.details["reason"])

    def test_invalid_handle_raises(self):
        with self.assertRaises(QuestionBankError) as ctx:
            fetch_candidate({"stem": "x"}, ScriptedPageTransport({}), sleep=FakeClock())
        self.assertEqual("INVALID_REQUEST", ctx.exception.code)


class TestCapabilities(unittest.TestCase):
    def test_unconfigured(self):
        caps = get_questionbank_capabilities()
        self.assertFalse(caps["configured"])
        self.assertEqual([], caps["sources"])
        self.assertIn("searxng", caps["source_kinds"]["search_service"]["providers"])
        self.assertIn("max_pages", caps["limits_defaults"])

    def test_configured_with_problem_entry(self):
        caps = get_questionbank_capabilities({
            "sources": [
                {"source_id": "s1", "kind": "search_service", "base_url": "https://searx.example"},
                {"source_id": "s2", "kind": "url_import", "urls": [Q1]},
                {"source_id": "s3", "kind": "bank_api"},
                {"source_id": "s4", "kind": "unknown-kind"},
            ]})
        by_id = {e["source_id"]: e for e in caps["sources"]}
        self.assertTrue(caps["configured"])
        self.assertTrue(by_id["s1"]["configured"])
        self.assertTrue(by_id["s2"]["configured"])
        self.assertFalse(by_id["s3"]["configured"])  # 缺 base_url
        self.assertFalse(by_id["s4"]["configured"])  # 未知 kind
        self.assertTrue(by_id["s3"]["problem"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
