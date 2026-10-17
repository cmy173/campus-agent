import pytest
from app.content_text import assignment_text
from app.review_quality import reject_format_questions
from app.worker import JobFailed


def test_assignment_html_keeps_scope_and_removes_editor_noise():
    raw='<style>.x{color:red}</style><p><span style="font-family:inherit;color:rgb(51,51,51)">第四章习题第1、2和3题</span></p><script>alert(1)</script>'
    assert assignment_text(raw)=='第四章习题第1、2和3题'


def test_physics_comparisons_and_escaped_code_are_preserved():
    assert assignment_text('E < 0，x > 1')=='E < 0，x > 1'
    assert assignment_text('<p>讨论 &lt;span&gt; 标签</p>')=='讨论 <span> 标签'


def test_format_questions_are_rejected_for_physics_but_not_html_course():
    questions=[{'ordinal':1,'stem':'span 标签的 font-family 值是什么？'}]
    with pytest.raises(JobFailed) as exc:
        reject_format_questions('原子物理','参考教材杨福家',questions)
    assert exc.value.code=='SUBJECT_MISMATCH'
    reject_format_questions('Web前端开发','',questions)
    reject_format_questions('原子物理','', [{'stem':'计算电子的自旋磁矩'}])
