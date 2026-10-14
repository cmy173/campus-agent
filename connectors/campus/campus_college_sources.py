"""Reviewed public sources. Matching uses server profile fields, never URL guessing."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CollegeSource:
    key: str
    college: str
    aliases: tuple[str, ...]
    host: str
    site_path: str
    listing: str

    def article_id(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != self.host or parsed.query or parsed.fragment):
            return None
        match = re.fullmatch(re.escape(self.site_path) + r'/\d{4}/\d{4}/c\d+a(\d{1,12})/page\.psp', parsed.path)
        return match[1] if match else None

    def attachment_allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.netloc == self.host and not parsed.query and
                not parsed.fragment and bool(re.fullmatch(
                    r'/_upload/article/files/[0-9a-fA-F/]+/[0-9a-fA-F-]+\.(?:pdf|docx?|xlsx?|pptx?|zip)',
                    parsed.path)))


CKC = CollegeSource('ckc', '竺可桢学院', ('竺可桢学院', '浙江大学竺可桢学院'),
    'webplus.zju.edu.cn', '/_s597', 'https://webplus.zju.edu.cn/_s597/qbtz/list.psp')
COLLEGE_SOURCES = (CKC,)


def normalize_college(value):
    return re.sub(r'\s+', '', value) if isinstance(value, str) else ''


def is_college_article(url):
    return any(source.article_id(url) for source in COLLEGE_SOURCES)


def is_college_attachment(url):
    return any(source.attachment_allowed(url) for source in COLLEGE_SOURCES)
