"""CKC public WebPlus list and article protocol; no cookies or business task extraction."""
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import urljoin

from ..campus_college_sources import CKC
from ..campus_common import CampusError, decode_page
from ..campus_network import PublicPageTransport
from ..connector_contracts import ConnectorSpec, FetchResult


class Element:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()

    def find(self, predicate):
        for child in self.children:
            if isinstance(child, Element):
                if predicate(child):
                    yield child
                yield from child.find(predicate)

    def text(self, blocks=False):
        if self.tag in ('script', 'style', 'noscript'):
            return ''
        text = ''.join(c.text(blocks) if isinstance(c, Element) else c for c in self.children)
        if blocks and self.tag in ('p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3'):
            text += '\n'
        elif blocks and self.tag in ('td', 'th'):
            text += ' | '
        return text


class PageParser(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Element()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 100:
            raise CampusError('PROTOCOL_CHANGED', '页面嵌套过深')
        node = Element(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(value):
    return re.sub(r'\s+', ' ', value).strip()


def one(node, class_name):
    matches = list(node.find(lambda n: n.has_class(class_name)))
    if len(matches) != 1:
        raise CampusError('PROTOCOL_CHANGED', '通知页面结构无法确认')
    return matches[0]


def page(raw, headers):
    html, _ = decode_page(raw, headers)
    return PageParser(html).root


def parse_list(raw, headers, source=CKC):
    root = page(raw, headers)
    listing = one(root, 'news_list')
    rows, seen = [], set()
    for li in listing.find(lambda n: n.tag == 'li'):
        title_node = one(li, 'news_title')
        anchors = list(title_node.find(lambda n: n.tag == 'a'))
        if len(anchors) != 1:
            raise CampusError('PROTOCOL_CHANGED', '通知链接无法确认')
        anchor = anchors[0]
        url = urljoin(source.listing, anchor.attrs.get('href', ''))
        article_id = source.article_id(url)
        if not article_id:
            raise CampusError('PROTOCOL_CHANGED', '通知跳转不在登记来源内')
        title = clean(anchor.attrs.get('title') or anchor.text())
        date = clean(one(li, 'news_meta').text())
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            raise CampusError('PROTOCOL_CHANGED', '通知发布日期无法确认') from None
        if not title:
            raise CampusError('PROTOCOL_CHANGED', '通知标题为空')
        if article_id not in seen:
            rows.append({'id': source.key+':'+article_id, 'title': title, 'source': url, 'published_at': date})
            seen.add(article_id)
    if not 1 <= len(rows) <= 100:
        raise CampusError('PROTOCOL_CHANGED', '通知列表为空或超出已知范围')
    return rows


def parse_article(raw, headers, row, source=CKC):
    if not source.article_id(row['source']):
        raise CampusError('FORBIDDEN', '通知来源未登记')
    root = page(raw, headers)
    titles = list(root.find(lambda n: n.tag == 'h1' and n.has_class('arti_title')))
    if len(titles) != 1 or not clean(titles[0].text()):
        raise CampusError('PROTOCOL_CHANGED', '通知正文标题无法确认')
    published = clean(one(root, 'arti_update').text())
    match = re.fullmatch(r'发布时间[：:]\s*(\d{4}-\d{2}-\d{2})', published)
    if not match:
        raise CampusError('PROTOCOL_CHANGED', '通知正文日期无法确认')
    try:
        datetime.strptime(match[1], '%Y-%m-%d')
    except ValueError:
        raise CampusError('PROTOCOL_CHANGED', '通知正文日期无效') from None
    content = one(root, 'wp_articlecontent')
    body = '\n'.join(line for line in (clean(x) for x in content.text(True).splitlines()) if line)
    if not body or len(body) > 100_000:
        raise CampusError('PROTOCOL_CHANGED', '通知正文为空或超出读取范围')
    attachments = {}
    unread = any(content.find(lambda n: n.tag in ('img', 'iframe', 'object', 'embed', 'video', 'audio')))
    for anchor in content.find(lambda n: n.tag == 'a'):
        url = urljoin(row['source'], anchor.attrs.get('href', ''))
        name = clean(anchor.text())
        if source.attachment_allowed(url) and name:
            attachments[url] = {'name': name, 'url': url}
        elif anchor.attrs.get('href'):
            unread = True
    return row | {'college':source.college, 'title':clean(titles[0].text()), 'published_at':match[1],
        'body':body, 'body_sha256':sha256(body.encode()).hexdigest(),
        'attachments':list(attachments.values()), 'has_unread_content':unread or bool(attachments)}


class CkcNoticesAdapter:
    spec = ConnectorSpec('ckc_public', ('college_notices',), 'public', college_names=CKC.aliases)

    def __init__(self, transport=None, policy=None):
        self.transport = transport or PublicPageTransport(
            lambda url: url == CKC.listing or bool(CKC.article_id(url)), policy)

    def fetch(self, capability, query, session=None):
        rows = parse_list(*self.transport.get(CKC.listing))
        records, error = [], None
        for row in rows[:query.limit]:
            try:
                records.append(parse_article(*self.transport.get(row['source']), row))
            except CampusError as exc:
                error = exc
                break  # No auto retry, no skipping an interrupted detail on the next sync.
        return FetchResult(records, 'partial', [{'code':'RECENT_COLLEGE_NOTICES'}], error=error)
