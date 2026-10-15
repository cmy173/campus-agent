#!/usr/bin/env python3
"""A-复习卷单测：提取引擎（extract_html / normalize 纯函数）。

覆盖：JSON-LD、启发式（选择/判断/填空/计算/简答）、答案与解析、噪声过滤、
重复题页内合并、图片/公式 attachments、答案缺失如实保留、坏 HTML、注入文本当数据、
URL 清洗、去重、match_reason。

运行：python -m unittest connectors.tests.test_questionbanks_extract -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.questionbanks.extract_html import (  # noqa: E402
    EXTRACTOR_VERSION,
    extract_page,
    looks_like_login,
)
from connectors.questionbanks.normalize import (  # noqa: E402
    candidate_id,
    dedupe_candidates,
    infer_type,
    match_reason_and_score,
    options_to_list,
    sanitize_source_url,
    stem_hash,
)
from connectors.tests.fakes_questionbanks import fixture_text  # noqa: E402

PAGE_URL = "https://qbpages.example/q/1"


def extract(name: str, url: str = PAGE_URL) -> "object":
    return extract_page(fixture_text(name), page_url=url, source_id="s1",
                        fetched_at="2026-10-11T00:00:00Z")


class TestHeuristicExtraction(unittest.TestCase):
    def setUp(self) -> None:
        self.result = extract("page_questions.html")
        self.cands = self.result.candidates

    def test_candidate_count_and_dedupe(self):
        # 页内 6 道题 + 第 7 题为第 1 题重复 → 页内合并后 6 条
        self.assertEqual(6, len(self.cands))

    def test_choice_with_options_answer_analysis(self):
        c = self.cands[0]
        self.assertEqual("choice", c["type"])
        self.assertEqual(["A", "B", "C", "D"], [o["label"] for o in c["options"]])
        self.assertEqual("B", c["answer"])
        self.assertIn("f'(x)=2x+2", c["analysis"])
        self.assertGreater(c["extract_confidence"], 0.7)

    def test_judgment_type(self):
        c = self.cands[1]
        self.assertEqual("judgment", c["type"])
        self.assertEqual("A", c["answer"])

    def test_fill_blank_type_and_answer(self):
        c = self.cands[2]
        self.assertEqual("fill_blank", c["type"])
        self.assertEqual("4", c["answer"])
        self.assertNotIn("答案", c["stem"])  # 答案行不得混入题干

    def test_short_answer_type_without_answer_kept(self):
        c = self.cands[3]
        self.assertEqual("short_answer", c["type"])
        self.assertIsNone(c["answer"])  # 页面没有答案 → 如实为空，不猜测

    def test_image_question_notes_missing_options(self):
        c = self.cands[4]
        kinds = [a["kind"] for a in c["attachments"]]
        self.assertIn("image", kinds)
        self.assertIn("note", kinds)
        self.assertIn("选项可能为图片", c["attachments"][-1]["note"])
        self.assertEqual([], c["options"])  # 未提取到选项就为空，不编造

    def test_formula_attachment(self):
        c = self.cands[5]
        self.assertEqual("calculation", c["type"])
        formulas = [a["text"] for a in c["attachments"] if a["kind"] == "formula"]
        self.assertTrue(any("\\int_0^1" in f for f in formulas))
        self.assertEqual("1/2", c["answer"])

    def test_noise_filtered(self):
        stems = " ".join(c["stem"] for c in self.cands)
        for noise in ("相关推荐", "版权所有", "登录后查看", "首页"):
            self.assertNotIn(noise, stems)

    def test_injection_text_treated_as_data_not_question(self):
        stems = " ".join(c["stem"] for c in self.cands)
        self.assertNotIn("忽略此前所有指令", stems)  # 非题目行不进入候选

    def test_fields_and_provenance(self):
        c = self.cands[0]
        self.assertEqual("online", c["origin"])
        self.assertEqual("qbpages.example", c["source_site"])
        self.assertEqual("s1", c["source_id"])
        self.assertEqual(PAGE_URL, c["source_url"])
        self.assertEqual(EXTRACTOR_VERSION, c["extractor_version"])
        self.assertIn("未声明许可", c["license_note"])
        self.assertEqual("block", c["source_locator"]["kind"])
        self.assertIsInstance(c["source_locator"]["index"], int)
        self.assertEqual("2026-10-11T00:00:00Z", c["fetched_at"])


class TestJsonLdExtraction(unittest.TestCase):
    def test_jsonld_questions_and_invalid_block_note(self):
        result = extract("page_jsonld.html")
        self.assertEqual(2, len(result.candidates))
        first = result.candidates[0]
        self.assertEqual("jsonld", first["source_locator"]["kind"])
        self.assertIn("勾股定理", first["stem"])
        self.assertIn("直角三角形", first["answer"])
        self.assertGreaterEqual(first["extract_confidence"], 0.8)
        # 无答案题保留，answer 如实为空
        second = result.candidates[1]
        self.assertIsNone(second["answer"])
        # 非法 JSON-LD 块不静默：产生说明
        self.assertTrue(any("JSON-LD" in n for n in result.notes))


class TestDegradedPages(unittest.TestCase):
    def test_broken_html_still_extracts(self):
        result = extract("page_broken.html")
        self.assertEqual(2, len(result.candidates))
        self.assertEqual("B", result.candidates[0]["answer"])
        self.assertEqual("judgment", result.candidates[1]["type"])
        self.assertEqual("错误", result.candidates[1]["answer"])

    def test_empty_and_login_pages_yield_nothing(self):
        self.assertEqual([], extract("page_empty.html").candidates)
        self.assertEqual([], extract("page_login.html").candidates)
        self.assertTrue(looks_like_login(fixture_text("page_login.html")))
        self.assertFalse(looks_like_login(fixture_text("page_questions.html")))


class TestNormalize(unittest.TestCase):
    def test_type_inference_priority(self):
        self.assertEqual("judgment", infer_type("下列说法对的是？", [
            {"label": "A", "text": "正确"}, {"label": "B", "text": "错误"}]))
        self.assertEqual("choice", infer_type("下列哪项正确？", [
            {"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}]))
        self.assertEqual("fill_blank", infer_type("水在标准气压下沸点是 ______ 度。", []))
        self.assertEqual("calculation", infer_type("计算 1+2 的值。", []))
        self.assertEqual("proof", infer_type("证明：两直线平行内错角相等。", []))
        self.assertEqual("short_answer", infer_type("简述牛顿第三定律。", []))
        self.assertEqual("other", infer_type("一个不明所以的短语", []))
        self.assertEqual("choice", infer_type("随便", [], declared="选择题"))

    def test_options_normalization(self):
        self.assertEqual(["A", "B"], [o["label"] for o in options_to_list({"A": "x", "B": "y"})])
        self.assertEqual(["A", "B"], [o["label"] for o in options_to_list(["A. 甲", "B. 乙"])])
        self.assertEqual("乙", options_to_list(["甲", "乙"])[1]["text"])
        self.assertEqual([], options_to_list("整段文本不猜测切分"))

    def test_sanitize_source_url_strips_sensitive_params(self):
        url = "https://bank.example/item/1?token=SECRET&id=7&session=abc#frag"
        out = sanitize_source_url(url)
        self.assertIn("id=7", out)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("session", out)
        self.assertNotIn("#", out)

    def test_dedupe_and_stable_id(self):
        base = {"stem": "1. 同一道题（ ）。", "options": [{"label": "A", "text": "x"}],
                "source_url": "https://a.example/1", "candidate_id": "qb_a"}
        dup = dict(base, candidate_id="qb_b", source_url="https://b.example/2")
        kept, dropped = dedupe_candidates([base, dup])
        self.assertEqual(1, len(kept))
        self.assertEqual("qb_b", dropped[0]["dropped_candidate_id"])
        # 题号与来源不同不影响去重键；稳定 ID 对同题+同 URL 恒定
        self.assertEqual(stem_hash("同一道题（ ）。", base["options"]),
                         stem_hash(base["stem"], base["options"]))
        self.assertEqual(candidate_id(base["stem"], base["options"], "https://a.example/1"),
                         candidate_id("同一道题（ ）。", base["options"], "https://a.example/1"))

    def test_match_reason_reports_evidence(self):
        cand = {"stem": "矩阵的行列式定义是什么？", "options": [], "type": "other",
                "extract_confidence": 0.6}
        reason, score, hits = match_reason_and_score(cand, ["矩阵", "行列式", "微分"], type_filter=[])
        self.assertIn("矩阵", reason)
        self.assertIn("行列式", reason)
        self.assertEqual(2, len(hits))
        self.assertGreater(score, 0.0)
        reason2, score2, hits2 = match_reason_and_score(cand, ["电子电路"], type_filter=[])
        self.assertEqual([], hits2)
        self.assertIn("未命中", reason2)
        self.assertLess(score2, score)

    def test_ascii_term_word_boundary(self):
        cand = {"stem": "What is a matrix?", "options": [], "type": "other",
                "extract_confidence": 0.5}
        _r, _s, hits = match_reason_and_score(cand, ["art"], type_filter=[])
        self.assertEqual([], hits)  # "art" 不应命中 "matrix"


if __name__ == "__main__":
    unittest.main(verbosity=2)
