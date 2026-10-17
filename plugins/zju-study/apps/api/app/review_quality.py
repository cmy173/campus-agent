"""Targeted rejection of editor-format questions in unrelated subject papers."""
import re


def reject_format_questions(course_title, notes, questions):
    if re.search(r'计算机|网页|前端|编程|HTML|CSS|Web', course_title, re.I):
        return
    if re.search(r'(?:考察|考查|学习|复习)(?:HTML|CSS|网页标签)', notes, re.I):
        return
    bad = [q.get('ordinal') for q in questions if re.search(
        r'HTML|CSS|font-family|font-size|background-color|<\s*span\b|(?:span|div)\s*标签',
        str(q.get('stem') or ''), re.I)]
    if bad:
        from .worker import JobFailed
        raise JobFailed('SUBJECT_MISMATCH', '题目错误地考察网页排版或样式，不符合课程内容，已阻止保存为合格试卷',
                        retryable=False, details={'question_ordinals':bad})
