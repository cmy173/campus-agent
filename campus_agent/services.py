"""Deterministic extraction and retrieval. Retrieved text has no execution authority."""
from datetime import datetime
import re
import unicodedata
from .core import TZ

DATE = re.compile(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?(?:\s*(\d{1,2})[:：](\d{2}))?')

def extract_tasks(text):
    results = []
    # Treat each line/sentence separately; do not attach an unrelated date elsewhere on the page.
    for sentence in re.split(r'[\n。；;]+', text):
        if not re.search(r'截止|截至|提交|交作业|报名|需完成|须完成', sentence):
            continue
        sentence = sentence.strip()
        if not sentence:
            continue
        if re.match(r'^(提交|报名)(地点|方式|入口|平台|地址)\s*[：:]', sentence):
            continue
        marker = re.search(r'截止|截至', sentence)
        matches = list(DATE.finditer(sentence[marker.end():])) if marker else []
        due, raw = None, None
        if len(matches) == 1:
            date = matches[0]
            raw = date[0]
            # Date without a time remains unconfirmed; no invented 23:59 deadline.
            if date[4] is not None:
                try:
                    due = datetime(*map(int, date.groups()), tzinfo=TZ).isoformat()
                except ValueError:
                    pass
        results.append({'title': sentence[:300], 'due': due, 'raw_date': raw,
                        'reviewed': 0})
    return results

STOP = ('请问', '帮我', '告诉我', '一下', '什么', '哪些', '怎么', '时候', '资料', '内容', '关于', '查询', '查找', '一下', '需要', '多少', '我的', '可以', '如何')

def terms(value):
    value = unicodedata.normalize('NFKC', value).lower()
    for word in STOP:
        value = value.replace(word, ' ')
    result = set(re.findall(r'[a-z0-9]{2,}', value))
    for phrase in re.findall(r'[\u4e00-\u9fff]+', value):
        result.update(phrase[i:i+2] for i in range(len(phrase)-1))
    return result

def retrieve(query, rows):
    wanted = terms(query)
    if not wanted:
        return []
    scored = []
    for row in rows:
        found = terms(row['text'] + ' ' + row['name'])
        overlap = wanted & found
        score = len(overlap) / len(wanted)
        # Conservative lexical baseline, not semantic question answering.
        if score >= .4 and overlap:
            scored.append((score, dict(row)))
    scored.sort(key=lambda x: (-x[0], x[1]['document'], x[1]['position']))
    return [row for score, row in scored[:4]]
