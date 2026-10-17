import json
from pathlib import Path

import pytest

from app.math_format import normalize_math
from app.review_export import render_markdown, render_answers_html, render_student_html

CASES = json.loads((Path(__file__).resolve().parents[3] / 'contracts/examples/math-format-cases.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('case', CASES)
def test_math_export_normalization_matches_page(case):
    assert normalize_math(case['input']) == case['expected']


def paper():
    return {'title': '数学公式测试卷', 'course_id': 'test', 'questions': [{
        'ordinal': 1, 'question_type': 'calculation', 'points': 10, 'difficulty': 'medium',
        'stem': '已知 e^z，计算积分。\n\\[\\int_0^1 x^2\\,dx\\]',
        'options': [{'key': 'A', 'text': 'sum_{n=0}^{∞} z^n/n!'}],
        'answer': '```latex\n\\frac13\n```', 'explanation': r'由 \(F(x)=\frac{x^3}{3}\) 得到结果。',
        'grading_points': ['正确写出 x^3/3。'], 'source_refs': [],
    }]}


def test_markdown_math_is_not_in_heading_or_code_block_and_original_is_unchanged():
    data = paper()
    before = json.dumps(data)
    text = render_markdown(data, '数理方法')
    assert '$e^z$' in text and r'$\sum_{n=0}^{\infty } z^n/n!$' in text
    assert '$$\n\\frac13\n$$' in text
    assert '```latex' not in text
    assert '## 第 1 题\n\n' in text
    assert json.dumps(data) == before


def test_html_is_self_contained_and_student_copy_has_no_answers():
    html = render_answers_html(paper(), '数理方法')
    assert 'renderMathInElement(document.body' in html
    assert 'data:font/woff2;base64,' in html
    assert '<script src=' not in html and 'url(fonts/' not in html
    assert 'trust:false' in html and 'maxExpand:1000' in html
    assert r'$F(x)=\frac{x^3}{3}$' in html
    student = render_student_html(paper(), '数理方法')
    assert '答案：' not in student and '解析：' not in student
