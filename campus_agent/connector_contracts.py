"""Small, validated contracts for implemented read capabilities only."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from .academic_defaults import SEMESTER_PATTERN
from .campus_common import CampusError
from .campus_college_sources import COLLEGE_SOURCES, is_college_article, is_college_attachment


class ErrorCode(str, Enum):
    AUTH_REQUIRED = 'AUTH_REQUIRED'
    AUTH_EXPIRED = 'AUTH_EXPIRED'
    INTERACTION_REQUIRED = 'INTERACTION_REQUIRED'
    FORBIDDEN = 'FORBIDDEN'
    RATE_LIMITED = 'RATE_LIMITED'
    NETWORK_UNREACHABLE = 'NETWORK_UNREACHABLE'
    TIMEOUT = 'TIMEOUT'
    PROTOCOL_CHANGED = 'PROTOCOL_CHANGED'
    UNSUPPORTED = 'UNSUPPORTED'


class ConnectorError(BaseModel):
    code: ErrorCode
    message: str


def standard_error(exc):
    """Never serialize upstream messages, diagnostics, headers or URLs."""
    diagnostic = getattr(exc, 'diagnostic', None) or {}
    code = getattr(exc, 'code', '')
    if code in ErrorCode._value2member_map_:
        mapped = code
    elif diagnostic.get('http_status') == 429:
        mapped = 'RATE_LIMITED'
    elif code in ('CAPTCHA_REQUIRED', 'CAS_REJECTED', 'CAPTCHA_UNAVAILABLE', 'AUTH_CONTEXT_INVALID'):
        mapped = 'INTERACTION_REQUIRED'
    elif code in ('SESSION_EXPIRED', 'PROFILE_SESSION_EXPIRED', 'FLOW_EXPIRED'):
        mapped = 'AUTH_EXPIRED'
    elif 'TIMEOUT' in code or diagnostic.get('reason') in ('TIMEOUT', 'OPERATION_DEADLINE'):
        mapped = 'TIMEOUT'
    elif code in ('ACCESS_REJECTED', 'ETA_ACCESS_REJECTED', 'SERVICE_ACCESS_REJECTED', 'UNSUPPORTED_ACCOUNT', 'PROFILE_IDENTITY_MISMATCH') or diagnostic.get('http_status') in (403, 901, 922):
        mapped = 'FORBIDDEN'
    elif 'UNSUPPORTED' in code:
        mapped = 'UNSUPPORTED'
    elif code == 'NETWORK_ERROR' or code.endswith('UNAVAILABLE'):
        mapped = 'NETWORK_UNREACHABLE'
    else:
        mapped = 'PROTOCOL_CHANGED'
    messages = {'AUTH_REQUIRED': '需要校园站点认证', 'AUTH_EXPIRED': '校园会话已过期',
        'INTERACTION_REQUIRED': '请在登录页面完成验证码或交互认证', 'FORBIDDEN': '平台未允许读取此能力',
        'RATE_LIMITED': '平台请求过于频繁，请稍后重试', 'NETWORK_UNREACHABLE': '校园网络不可达',
        'TIMEOUT': '校园请求超时', 'PROTOCOL_CHANGED': '平台响应无法确认，已保留缓存',
        'UNSUPPORTED': '平台或能力尚未支持'}
    return ConnectorError(code=mapped, message=messages[mapped])


@dataclass(frozen=True)
class UserContext:
    """Construct only from server authentication; never deserialize client/model input."""
    owner: str
    permissions: frozenset[str] = frozenset({'campus.read'})

    def __post_init__(self):
        if not isinstance(self.owner, str) or not self.owner or len(self.owner) > 128:
            raise ValueError('Invalid authenticated owner')


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class CoursesQuery(Query):
    year: str | None = None
    semester: str | None = None

    @model_validator(mode='after')
    def validate_scope(self):
        if (self.year is None) != (self.semester is None):
            raise ValueError('year and semester must be specified together')
        if self.year is not None and (not re.fullmatch(r'(19|20)\d{2}', self.year) or
                not re.fullmatch(SEMESTER_PATTERN, self.semester)):
            raise ValueError('Invalid academic term')
        return self


class GradesQuery(Query):
    pass


class ProfileQuery(Query):
    pass


class NoticesQuery(Query):
    limit: int = Field(default=6, ge=1, le=6)


class CollegeNoticesQuery(Query):
    # College and source are resolved from the authenticated account, not accepted here.
    limit: int = Field(default=3, ge=1, le=5)


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = ''
    fetched_at: str | None = None
    stale: bool = False

    @field_validator('source')
    @classmethod
    def safe_source(cls, value):
        if value:
            p = urlsplit(value)
            # Data sources never contain SSO tickets or arbitrary query strings.
            if p.scheme != 'https' or p.hostname not in ('zdbk.zju.edu.cn', 'eta.zju.edu.cn') or p.username or p.password or p.fragment or p.port not in (None,443):
                raise ValueError('Unsafe source')
            if p.query and not re.fullmatch(r'xwbh=[0-9A-Fa-f]{32}&doType=save', p.query):
                raise ValueError('Unsafe source query')
        return value


class Course(Record):
    name: str
    code: str = ''
    teacher: str = ''
    location: str = ''
    weekday: str = ''
    periods: str = ''
    start_period: str = ''
    duration: str = ''
    weeks: str = ''
    season: str = ''
    odd_even: str = ''
    year: str = ''
    semester: str = ''
    term: str = ''


class Grade(Record):
    name: str
    code: str = ''
    term: str = ''
    score: str = ''
    credits: str = ''
    gpa: str = ''
    selection_id: str = ''


class Profile(Record):
    student_id: str = ''
    name: str = ''
    college: str = ''
    major: str = ''
    class_name: str = ''
    year: str = ''
    field_sources: dict[Literal['student_id','name','college','major','class_name','year'], Record] = Field(default_factory=dict)


class Notice(Record):
    id: str = Field(pattern=r'^[0-9A-Fa-f]{32}$')
    title: str
    published_at: str | None = None


class NoticeAttachment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=500)
    url: str

    @field_validator('url')
    @classmethod
    def safe_url(cls, value):
        if not is_college_attachment(value):
            raise ValueError('Unregistered attachment URL')
        return value


class CollegeNotice(Record):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,31}:\d{1,12}$')
    college: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    published_at: str
    body: str = Field(min_length=1, max_length=100_000)
    body_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    attachments: list[NoticeAttachment] = Field(default_factory=list, max_length=30)
    # Image-only material and attachments are not silently counted as parsed text.
    has_unread_content: bool = False

    @field_validator('source')
    @classmethod
    def safe_source(cls, value):
        if not is_college_article(value):
            raise ValueError('Unregistered college article URL')
        return value

    @field_validator('published_at')
    @classmethod
    def published_date(cls, value):
        datetime.strptime(value, '%Y-%m-%d')
        return value

    @model_validator(mode='after')
    def consistent_origin(self):
        origin = next(source for source in COLLEGE_SOURCES if source.article_id(self.source))
        if self.id != origin.key+':'+origin.article_id(self.source) or self.college != origin.college:
            raise ValueError('College notice identity does not match its registered source')
        if any(not origin.attachment_allowed(link.url) for link in self.attachments):
            raise ValueError('Attachment does not belong to this notice source')
        return self


class CollegeSelection(BaseModel):
    college: str = ''
    status: Literal['matched', 'missing', 'unsupported', 'ambiguous'] = 'missing'
    profile_fetched_at: str | None = None
    profile_stale: bool = True


class QueryResult(BaseModel):
    records: list[dict] = Field(default_factory=list)
    source: str
    platform: str
    fetched_at: str | None = None
    last_attempt_at: str | None = None
    freshness: Literal['fresh', 'stale', 'unknown'] = 'unknown'
    completeness: Literal['complete', 'partial', 'unknown'] = 'unknown'
    next_cursor: str | None = None
    warnings: list[dict] = Field(default_factory=list)
    error: ConnectorError | None = None
    selection: CollegeSelection | None = None


@dataclass(frozen=True)
class ConnectorSpec:
    platform_id: str
    capabilities: tuple[str, ...]
    authentication: str
    permissions: tuple[str, ...] = ('campus.read',)
    read_only: bool = True
    pagination: bool = False
    incremental_sync: bool = False
    college_names: tuple[str, ...] = ()


@dataclass
class FetchResult:
    records: list[dict]
    completeness: str = 'complete'
    warnings: list[dict] | None = None
    invalidated_fields: tuple[str, ...] = ()
    error: CampusError | None = None


class Connector(Protocol):
    spec: ConnectorSpec
    def fetch(self, capability: str, query: Query, session) -> FetchResult: ...


class CacheRepository(Protocol):
    def read(self, owner: str, platform: str, capability: str, scope: str) -> dict | None: ...
    def write(self, owner: str, platform: str, capability: str, scope: str, result: dict) -> None: ...
    def delete(self, owner: str) -> None: ...
    def scopes(self, owner: str, platform: str, capability: str) -> list[str]: ...


class SessionProvider(Protocol):
    def lock(self, owner: str): ...
    def connector_session(self, platform: str, owner: str): ...
    def expire_platform(self, platform: str, owner: str) -> None: ...
    def platform_status(self, platform: str, owner: str) -> dict: ...
