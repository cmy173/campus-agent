"""Fixed public login-board metadata, without cookies, credentials or arbitrary URLs."""
from datetime import datetime
from html.parser import HTMLParser
import http.client
import re
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode
import urllib.request
import urllib.error

from ..campus_common import CampusError, plain, decode_page
from ..campus_network import build_opener, transport_failure, READ_BUDGET
from ..connector_contracts import ConnectorSpec, FetchResult

BOARD = 'https://zdbk.zju.edu.cn/jwglxt/xtgl/login_cxLoginNews.html?mxb_id=newsnotice'
DETAIL_PATH = '/jwglxt/xtgl/xwck_ckLoginNews.html'


class BoardParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows, self.row, self.anchor, self.board = [], None, False, False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'ul' and 'log-list' in attrs.get('class', '').split():
            self.board = True
        if self.board and tag == 'li':
            self.row = {'href': '', 'title': '', 'date': ''}
        if self.row is not None and tag == 'a':
            self.anchor = True
            self.row['href'] = attrs.get('href', '')

    def handle_data(self, text):
        if self.row is not None:
            self.row['title' if self.anchor else 'date'] += text

    def handle_endtag(self, tag):
        if tag == 'a':
            self.anchor = False
        if tag == 'li' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if tag == 'ul':
            self.board = False


def parse_board(raw, headers):
    html, _ = decode_page(raw, headers)
    parser = BoardParser(html)
    if not re.search(r'class=["\'][^"\']*\blog-list\b', html):
        raise CampusError('PROTOCOL_CHANGED', '公开通知列表结构无法确认')
    records = []
    for row in parser.rows:
        url = urlsplit(urljoin(BOARD, row['href']))
        query = parse_qs(url.query)
        ids = query.get('xwbh', [])
        if (url.scheme != 'https' or url.netloc != 'zdbk.zju.edu.cn' or url.path != DETAIL_PATH or
            url.fragment or set(query) != {'xwbh', 'doType'} or query.get('doType') != ['save'] or
            len(ids) != 1 or not re.fullmatch(r'[0-9A-Fa-f]{32}', ids[0])):
            raise CampusError('PROTOCOL_CHANGED', '公开通知来源地址无法确认')
        title = re.sub(r'\s+', ' ', plain(row['title'])).strip()
        date_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', row['date'])
        if not title or not date_match:
            raise CampusError('PROTOCOL_CHANGED', '公开通知字段无法确认')
        try:
            datetime.strptime(date_match[1], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            raise CampusError('PROTOCOL_CHANGED', '公开通知日期无法确认') from None
        records.append({'id': ids[0], 'title': title, 'published_at': date_match[1],
            'source': 'https://zdbk.zju.edu.cn'+DETAIL_PATH+'?'+urlencode({'xwbh':ids[0],'doType':'save'})})
    if len(records) > 20:
        raise CampusError('PROTOCOL_CHANGED', '通知列表规模变化')
    return records


class PublicNoticesAdapter:
    spec = ConnectorSpec('zdbk_public', ('notices',), 'public')

    def __init__(self, policy=None):
        self.opener = build_opener(policy)  # No CookieJar, including after CAS login.

    def fetch(self, capability, query, session=None):
        request = urllib.request.Request(BOARD, data=b'', headers={'User-Agent': 'CampusAgent/0.3'})
        READ_BUDGET.take('zdbk.zju.edu.cn')
        try:
            with self.opener.open(request, timeout=12) as response:
                raw = response.read(2_000_001)
                if response.code != 200 or len(raw) > 2_000_000:
                    raise CampusError('PROTOCOL_CHANGED', '公开通知响应无法确认')
                rows = parse_board(raw, dict(response.headers))
        except urllib.error.HTTPError as exc:
            raise CampusError('RATE_LIMITED' if exc.code == 429 else 'FORBIDDEN' if exc.code in (401,403) else 'PROTOCOL_CHANGED',
                '公开通知请求失败') from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, EOFError) as exc:
            reason, _ = transport_failure(exc)
            raise CampusError('TIMEOUT' if reason == 'TIMEOUT' else 'NETWORK_UNREACHABLE', '公开通知网络请求失败') from None
        return FetchResult(rows[:query.limit], 'partial',
            [{'code': 'LIMITED_SCOPE', 'message': '仅公开登录页置顶/近期通知摘要，标题可能截断；未读取通知正文或历史列表'}])
