from html import unescape
from html.parser import HTMLParser
import re

class CampusError(Exception):
    def __init__(self, code, message, diagnostic=None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic

    def public(self):
        result = {'code': self.code, 'message': str(self)}
        if self.diagnostic:
            result['diagnostic'] = self.diagnostic
        return result


class _Text(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.parts = []
        self.feed(text)

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    if value is None:
        return ''
    return unescape(''.join(_Text(str(value)).parts)).strip()[:500]


def decode_page(raw, headers):
    """Preserve school option characters; never replace undecodable query values."""
    content_type=next((str(v) for k,v in headers.items() if k.lower()=='content-type'),'')
    meta=raw[:4096].decode('ascii','ignore')
    declared=re.search(r'charset\s*=\s*[\"\']?([A-Za-z0-9_-]+)',content_type,re.I)
    if not declared:declared=re.search(r'charset\s*=\s*[\"\']?([A-Za-z0-9_-]+)',meta,re.I)
    aliases={'utf-8':'utf-8-sig','utf8':'utf-8-sig','gbk':'gb18030','gb2312':'gb18030','gb18030':'gb18030'}
    first=aliases.get(declared[1].lower()) if declared else None
    for encoding in dict.fromkeys([first,'utf-8-sig','gb18030']):
        if not encoding:continue
        try:return raw.decode(encoding).strip(),encoding
        except UnicodeError:pass
    raise CampusError('PROTOCOL_CHANGED','校园页面编码无法识别，未用损坏字符查询',{'reason':'TEXT_ENCODING'})
