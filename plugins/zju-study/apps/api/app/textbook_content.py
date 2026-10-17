"""从本地教材提取候选习题、解析作业题号，不调用模型猜测原题。"""
from __future__ import annotations

import re

_NUMBERS = {c: n for n, c in enumerate('零一二三四五六七八九')}


def chapter_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value == '十':
        return 10
    if '十' in value:
        left, right = value.split('十', 1)
        return (_NUMBERS.get(left, 1) * 10) + _NUMBERS.get(right, 0)
    return _NUMBERS.get(value, 0)


def assignment_references(text: str) -> list[tuple[int, str]]:
    """只识别明确的第 X 章题号列表；不将日期、分数当作题号。"""
    result = []
    matches = list(re.finditer(r'第?([\d零一二三四五六七八九十]+)章', text))
    for index, match in enumerate(matches):
        tail = text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        listing = re.search(r'(?:习题|练习题|作业题|题目)\s*(?:第)?\s*([\d\s、,，和及至到\-–—]+)', tail)
        if not listing:
            continue
        values = listing.group(1).strip()
        if not values or len(values) > 150:
            continue
        numbers = []
        for part in re.split(r'[、,，和及\s]+', values):
            if re.fullmatch(r'\d+', part):
                numbers.append(int(part))
            elif re.fullmatch(r'\d+[-–—至到]\d+', part):
                start, end = map(int, re.split(r'[-–—至到]', part))
                if 0 < start <= end < start + 100:
                    numbers.extend(range(start, end + 1))
        chapter = chapter_number(match.group(1))
        result.extend((chapter, str(number)) for number in numbers if 0 < number < 1000 and chapter)
    return list(dict.fromkeys(result))


def extract_candidates(chunks, chapter: int, start_page: int, end_page: int) -> list[dict]:
    """用户限定习题页面后，识别行首编号；跨页续行保留，候选须核对。"""
    lines = []
    for chunk in sorted(chunks, key=lambda item: (item.ordinal, item.id)):
        page = (chunk.locator or {}).get('page')
        if isinstance(page, int) and start_page <= page <= end_page:
            lines.extend((line, page) for line in chunk.text.splitlines())
    results = []
    active = None
    for line, page in lines:
        match = re.match(r'^\s*(?:[（(])?(?:(\d{1,3})[.\-](\d{1,3})[）).、．]?\s+|(\d{1,3})[）).、．]\s*)(\S.*)$', line)
        if match:
            if match.group(2) and int(match.group(1)) != chapter:
                active = None
                continue
            number = str(int(match.group(2) or match.group(3)))
            active = {'chapter': chapter, 'number': number, 'page': page, 'text': match.group(4)}
            results.append(active)
        elif active is not None:
            active['text'] += '\n' + line
    return [item for item in results if len(item['text'].strip()) >= 10]


def auto_candidates(chunks) -> tuple[list[dict], list[str]]:
    """寻找明确章节及习题区，无须填写页码。不会在普通正文段落编号上猜题。"""
    active_chapter = None
    in_exercises = False
    active = None
    found = []
    for chunk in sorted(chunks, key=lambda item: (item.ordinal, item.id)):
        page = (chunk.locator or {}).get('page')
        for line in chunk.text.splitlines():
            line = line.strip()
            heading = re.match(r'^(?:第([\d零一二三四五六七八九十]+)章|Chapter\s+(\d+))', line, re.I)
            if heading:
                active_chapter = chapter_number(heading.group(1) or heading.group(2))
                in_exercises = False
                active = None
            exercise_heading = re.match(r'^(?:(?:第?[\d零一二三四五六七八九十]+章)\s*)?(?:习题|练习题|思考题|Exercises|Problems)(?:\s*\d+)?\s*$', line, re.I)
            if exercise_heading and active_chapter:
                in_exercises = True
                active = None
                continue
            if re.match(r'^(?:习题答案|参考答案|习题解答|答案与提示|参考文献|Answers|References|附录)', line, re.I):
                in_exercises = False
                active = None
            if not in_exercises or not active_chapter:
                continue
            match = re.match(r'^(?:[（(])?(?:(\d{1,3})[.\-](\d{1,3})[）).、．]?\s+|(\d{1,3})[）).、．]\s*)(\S.*)$', line)
            if match:
                if match.group(2) and int(match.group(1)) != active_chapter:
                    active = None
                    continue
                number = str(int(match.group(2) or match.group(3)))
                active = {'chapter': active_chapter, 'number': number, 'page': page,
                          'text': match.group(4)}
                found.append(active)
            elif active is not None and len(active['text']) < 20000:
                active['text'] += '\n' + line
    groups = {}
    for item in found:
        groups.setdefault((item['chapter'], item['number']), []).append(item)
    ambiguous = [f'第{chapter}章第{number}题重复出现' for (chapter, number), items in groups.items() if len(items) > 1]
    unique = [items[0] for items in groups.values() if len(items) == 1 and 10 <= len(items[0]['text'].strip()) <= 20000]
    return unique, ambiguous


def index_book(db, book):
    from sqlalchemy import select
    from .models import ContentChunk, Resource, TextbookExercise
    resource = db.get(Resource, book.resource_id) if book.resource_id else None
    if not resource or resource.parse_status != 'ready':
        return {'indexed': 0, 'message': '教材正文尚未就绪，解析完成后会自动建立候选索引。', 'ambiguous': []}
    chunks = list(db.scalars(select(ContentChunk).where(ContentChunk.resource_id == resource.id,
                                                       ContentChunk.resource_version == resource.version)))
    candidates, ambiguous = auto_candidates(chunks)
    count = 0
    for item in candidates:
        row = db.get(TextbookExercise, (book.id, item['chapter'], item['number']))
        if row and row.confirmed and row.resource_version == resource.version:
            continue
        if row is None:
            row = TextbookExercise(textbook_id=book.id, chapter=item['chapter'], number=item['number'])
            db.add(row)
        row.text = item['text']
        row.page = item['page']
        row.resource_version = resource.version
        row.confirmed = False
        count += 1
    db.flush()
    return {'indexed': count, 'ambiguous': ambiguous,
            'message': f'已自动定位 {count} 道习题候选，请核对后保存。' if count else '未发现明确的章节与习题区；保留页码提取和手工补充入口。'}
