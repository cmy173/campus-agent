"""Acquire and cross-check public ZJU calendars at runtime, without CAS cookies.

Only the known main-campus undergraduate layout and notice grammar are supported.
Only successful official checks are marked verified; callers use cache/defaults on failure.
"""
from datetime import date, datetime, timedelta
from html import unescape
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
from tempfile import TemporaryDirectory
import time
from urllib.parse import urljoin, urlsplit, parse_qs
import urllib.error
import urllib.request

from .campus_common import CampusError, _Text, plain
from .campus_network import build_opener, READ_BUDGET
from .core import TZ

CATALOG = 'https://ugrs.zju.edu.cn/28218/list1.htm'
ENGLISH = 'https://www.zju.edu.cn/english/19600/list.htm'
NEWS = 'https://zdbk.zju.edu.cn/jwglxt/xtgl/login_cxLoginNews.html?mxb_id=newsnotice'
MONTHS = {m: i+1 for i, m in enumerate(('January', 'February', 'March', 'April', 'May', 'June',
                                      'July', 'August', 'September', 'October', 'November', 'December'))}


def links(text):
    return [(unescape(href), plain(label)) for href, label in re.findall(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.S | re.I)]


def quarters(html, year, semester):
    expected = [('Autumn', '秋', year), ('Winter', '冬', year)] if semester == '3' else [
        ('Spring', '春', year+1), ('Summer', '夏', year+1)]
    found = []
    for block in re.split(r'<div\s+class="item"[^>]*>', html)[1:]:
        title = re.search(r'<div class="title">(.*?)</div>', block, re.S)
        if not title:
            continue
        match = next((item for item in expected if plain(title[1]) == f'{item[0]} Quarter {item[2]}'), None)
        if not match:
            continue
        rows = [(plain(a), plain(b)) for a, b in re.findall(r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', block, re.S)]
        def parse_day(text):
            value = re.search(r'('+'|'.join(MONTHS)+r')\s*(\d{1,2})', text)
            if not value:
                raise ValueError('missing date')
            month = MONTHS[value[1]]
            return date(match[2]+(match[0] == 'Winter' and month < 3), month, int(value[2]))
        start = next((parse_day(a) for a, b in rows if b == 'Classes begin'), None)
        exam = next((parse_day(a) for a, b in rows if b == 'Examinations'), None)
        if not start or not exam or start.weekday() != 0 or not 40 <= (exam-start).days <= 100:
            raise ValueError('unverified quarter range')
        found.append({'name': match[1], 'start': start.isoformat(), 'end': (exam-timedelta(days=1)).isoformat(),
                      'exam_start': exam.isoformat()})
    if len(found) != 2:
        raise ValueError('calendar year not available')
    return sorted(found, key=lambda item: item['start'])


def period_times(text):
    text = re.sub(r'\s', '', text).replace('：', ':').replace('．', '.').replace('一', '-').replace('—', '-')
    pairs = re.findall(r'(\d{1,2})[:.](\d{2})[-~](\d{1,2})[:.]?(\d{2})', text)
    result, previous = [], -1
    if len(pairs) != 13:
        raise ValueError('unverified period count')
    for sh, sm, eh, em in pairs:
        sh, sm, eh, em = map(int, (sh, sm, eh, em))
        start, end = sh*60+sm, eh*60+em
        if not (0 <= sh <= eh < 24 and sm < 60 and em < 60 and end-start == 45 and start > previous):
            raise ValueError('invalid period time')
        previous = end
        result.append([f'{sh:02}:{sm:02}', f'{eh:02}:{em:02}'])
    return result


def teaching_changes(html, year):
    # Parse specific dated clauses only. General policy paragraphs are not appointments.
    text = plain_blocks(html)
    start = text.find('一、中秋节')
    end = text.find('四、课程调补说明')
    if start < 0 or end < start or f'{year}年下半年' not in text:
        raise ValueError('unsupported teaching notice')
    text = text[start:end]
    holidays, exchanges = set(), {}
    dates = r'(?:20\d{2}年)?\d{1,2}月\d{1,2}日(?:（[^）]*）)?'
    def parse_day(value):
        m = re.search(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日', value)
        month = int(m[2])
        return date(int(m[1]) if m[1] else year+(month < 3), month, int(m[3])).isoformat()
    matched = 0
    for sentence in re.split(r'[。\n]', text):
        sentence = re.sub(r'\s', '', sentence)
        if not sentence:
            continue
        if '停课' in sentence:
            # Ranges like 10月1日至3日 and individual dates; omitted month is explicit shorthand.
            clause = sentence.split('停课')[0]
            def expand_month(match):
                months = re.findall(r'(\d{1,2})月', clause[:match.start()])
                if not months:
                    raise ValueError('missing cancellation month')
                return '、'+months[-1]+'月'+match[1]+'日'
            clause = re.sub(r'、(\d{1,2})日', expand_month, clause)
            tokens = re.findall(r'(?:20\d{2}年)?\d{1,2}月\d{1,2}日(?:（[^）]*）)?(?:至\d{1,2}日(?:（[^）]*）)?)?', clause)
            if not tokens:
                raise ValueError('unknown cancellation clause')
            for token in tokens:
                first = parse_day(token)
                tail = re.search(r'至(\d{1,2})日', token)
                last = date.fromisoformat(first).replace(day=int(tail[1])) if tail else date.fromisoformat(first)
                cursor = date.fromisoformat(first)
                if (last-cursor).days not in range(15):
                    raise ValueError('invalid cancellation range')
                while cursor <= last:
                    holidays.add(cursor.isoformat()); cursor += timedelta(days=1)
            matched += 1
        if '的课对调' in sentence:
            left, right = sentence.split('分别与') if '分别与' in sentence else sentence.split('与')
            a, b = re.findall(dates, left), re.findall(dates, right)
            if not a or len(a) != len(b):
                raise ValueError('unknown exchange clause')
            for x, y in zip(a, b):
                x, y = parse_day(x), parse_day(y)
                exchanges[x], exchanges[y] = y, x
            matched += 1
        elif '补' in sentence and '课' in sentence:
            values = re.findall(dates, sentence)
            pair = re.search('('+dates+')补('+dates+')的课', sentence)
            if pair:
                destination, original = parse_day(pair[1]), parse_day(pair[2])
            elif '停课' in sentence and len(values) == 2 and '补课' in sentence:
                original, destination = map(parse_day, values)
            else:
                raise ValueError('unknown makeup clause')
            exchanges[destination] = original
            matched += 1
        elif '调' in sentence and '课' in sentence:
            raise ValueError('unknown teaching rule')
    if matched < 5 or not exchanges or not holidays:
        raise ValueError('incomplete teaching notice')
    return sorted(holidays), exchanges


def plain_blocks(html):
    html = re.sub(r'</(?:p|div|h\d|li|tr)>|<br\s*/?>', '\n', html, flags=re.I)
    # plain() truncates individual fields; this is a bounded public document, not a field.
    return ''.join(_Text(html).parts).strip()


class CalendarClient:
    def __init__(self, policy=None):
        self.cache = {}
        self.lock = threading.Lock()
        self.opener = build_opener(policy)

    def read(self, url, form=None, limit=2_000_000):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise CampusError('CALENDAR_SOURCE_CHANGED', '校历来源地址无效') from None
        path, query = parsed.path, parse_qs(parsed.query)
        allowed_path = (
            parsed.hostname == 'www.zju.edu.cn' and path == '/english/19600/list.htm' and not query or
            parsed.hostname == 'ugrs.zju.edu.cn' and not query and (path == '/28218/list1.htm' or
                re.fullmatch(r'/20\d{2}/\d{4}/c28218a\d+/page.htm', path) or
                re.fullmatch(r'/_upload/article/files/[A-Za-z0-9/_.\-]+\.pdf', path)) or
            parsed.hostname == 'zdbk.zju.edu.cn' and (url == NEWS or path == '/jwglxt/xtgl/xwck_ckLoginNews.html'
                and set(query) <= {'xwbh', 'doType'} and len(query.get('xwbh', [])) == 1
                and re.fullmatch(r'[0-9A-Fa-f]{32}', query['xwbh'][0]) and query.get('doType', ['save']) == ['save']))
        if (parsed.scheme != 'https' or not allowed_path or port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment or '..' in path):
            raise CampusError('CALENDAR_SOURCE_CHANGED', '校历来源地址不符合固定学校来源范围')
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise CampusError('CALENDAR_TIMEOUT', '校历读取超时，暂保留星期课表')
        try:
            READ_BUDGET.take(parsed.hostname)
            with self.opener.open(urllib.request.Request(url, data=form, headers={'User-Agent': 'CampusAgent/0.3'}),
                                  timeout=min(12, remaining)) as response:
                raw = response.read(limit+1)
                if response.code != 200 or len(raw) > limit:
                    raise ValueError('invalid response')
                return raw
        except (OSError, ValueError, urllib.error.URLError):
            raise CampusError('CALENDAR_UNAVAILABLE', '无法读取学校公开校历，暂保留星期课表') from None

    def fetch(self, year, semester, force=False):
        with self.lock:
            return self._fetch(year, semester, force)

    def _fetch(self, year, semester, force):
        if semester != '3':
            raise CampusError('CALENDAR_UNSUPPORTED', '当前校方调课通知解析支持秋冬学期；其他学期保留星期课表，待接入对应规则')
        key = year+'/'+semester
        if not force and key in self.cache and self.cache[key][0] > time.monotonic():
            return self.cache[key][1]
        self.deadline = time.monotonic()+60
        try:
            label = f'{year}—{int(year)+1}'
            catalog = self.read(CATALOG).decode('utf-8')
            article = next((urljoin(CATALOG, href) for href, title in links(catalog)
                            if label in title.replace('-', '—') and '校历' in title), None)
            if not article:
                raise ValueError('calendar year not published')
            html = self.read(article).decode('utf-8')
            pdf_path = re.search(r'pdfsrc=["\']([^"\']+\.pdf)["\']', html)
            if not pdf_path:
                raise ValueError('calendar attachment not found')
            pdf_url = urljoin(article, pdf_path[1])
            if urlsplit(pdf_url).hostname != 'ugrs.zju.edu.cn' or not urlsplit(pdf_url).path.startswith('/_upload/article/files/'):
                raise ValueError('unexpected attachment')
            pdf = self.read(pdf_url, limit=10_000_000)
            if not pdf.startswith(b'%PDF-'):
                raise ValueError('invalid PDF')
            english_raw = self.read(ENGLISH)
            seasons = quarters(english_raw.decode('utf-8'), int(year), semester)
            with TemporaryDirectory(prefix='campus-calendar-') as temporary:
                path = Path(temporary)/'calendar.pdf'; path.write_bytes(pdf)
                worker = subprocess.run([sys.executable, '-m', 'campus_agent.calendar_extract', str(path)],
                    capture_output=True, timeout=max(1, min(25, self.deadline-time.monotonic())),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                pages = json.loads(worker.stdout.decode('utf-8'))
                if worker.returncode or not isinstance(pages, list) or not pages:
                    raise CampusError('CALENDAR_OCR_UNAVAILABLE', '学校校历需要本机中文识别；识别不可用时保留星期课表')
            times = period_times(pages[0]['times'])
            if any(period_times(p['times']) != times for p in pages):
                raise ValueError('conflicting period times')
            compact = re.sub(r'\s', '', pages[0]['notes'])
            if f'{year}年' not in compact or f'{int(year)+1}年' not in compact:
                raise ValueError('PDF academic year does not match')
            for season in seasons:
                for value in (season['start'], season['exam_start']):
                    d = date.fromisoformat(value)
                    if f'{d.month}月{d.day}日' not in compact and f'{d.month}月{d.day}一' not in compact:
                        raise ValueError('official sources disagree')
            news_html = self.read(NEWS, b'').decode('utf-8')
            notice_url = next((urljoin(NEWS, href) for href, title in links(news_html)
                               if year+'年下半年' in title and '节假日' in title), None)
            if not notice_url or urlsplit(notice_url).path != '/jwglxt/xtgl/xwck_ckLoginNews.html':
                raise ValueError('teaching notice missing')
            notice_raw = self.read(notice_url)
            holidays, exchanges = teaching_changes(notice_raw.decode('utf-8'), int(year))
            # Sports meetings and not-yet-published next-year adjustments remain unresolved.
            special = []
            for block in re.split(r'<div\s+class="item"[^>]*>', english_raw.decode('utf-8')):
                if f'Autumn Quarter {year}' not in block:
                    continue
                for left, right in re.findall(r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', block, re.S):
                    if plain(right) == 'Sports Meeting':
                        m = re.search(r'('+'|'.join(MONTHS)+r')\s*(\d+)-(\d+)', plain(left))
                        if not m:
                            raise ValueError('unknown school activity date')
                        special += [date(int(year), MONTHS[m[1]], d).isoformat() for d in range(int(m[2]), int(m[3])+1)]
            fetched = datetime.now(TZ).isoformat()
            result = {'verified': True, 'stale': False, 'fetched_at': fetched, 'seasons': seasons,
                      'period_times': times, 'holidays': holidays, 'exchanges': exchanges,
                      'uncertain_dates': special, 'valid_until': year+'-12-31',
                      'scope': '主校区本科秋冬学期；校运会与次年调休尚待核对，海宁院历不适用',
                      'sources': [{'url': u, 'sha256': hashlib.sha256(b).hexdigest()} for u, b in
                                  [(pdf_url, pdf), (ENGLISH, english_raw), (notice_url, notice_raw)]],
                      'method': '官方 PDF 本机识别；与官网开课/考试日期及教务停课调课通知交叉核对'}
            self.cache[key] = (time.monotonic()+6*3600, result)
            return result
        except CampusError:
            raise
        except (ValueError, KeyError, TypeError, StopIteration, subprocess.TimeoutExpired, OSError):
            raise CampusError('CALENDAR_NOT_VERIFIED', '校历日期、节次或调课规则未能通过程序核对，保留星期课表') from None
