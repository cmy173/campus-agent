"""Readable assignment instructions, without editor markup or executable content."""
import re
from html import unescape
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if not self.hidden and tag in ('p','div','br','li','tr'):
            self.parts.append('\n')
        if not self.hidden and tag in ('td','th'):
            self.parts.append(' ')
        if not self.hidden and tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self.parts.append(alt)
    def handle_endtag(self, tag):
        if tag in ('script','style'):
            self.hidden = max(0,self.hidden-1)
        elif not self.hidden and tag in ('p','div','li','tr'):
            self.parts.append('\n')
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def assignment_text(value):
    text = value or ''
    if not re.search(r'</?(?:p|div|span|br|li|ul|ol|table|td|tr|script|style|img|strong|em|b|i|a|font|sup|sub|h[1-6])\b',text,re.I):
        return unescape(text)
    parser = _Text()
    parser.feed(text)
    return '\n'.join(line.strip() for line in ''.join(parser.parts).replace('\xa0',' ').splitlines() if line.strip())
