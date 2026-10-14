#!/usr/bin/env python3
"""网络题库检索的类型、错误码与传输协议（A 定义，B 冻结后以契约为准）。

错误码（映射到 contracts/schemas/error.schema.json 的 ErrorInfo 形状
``{code, message, retryable, details}``；request_id 由 B 的 API 层补齐）：

  - NOT_CONFIGURED   未配置任何题库来源（属调用方错误，上抛异常）
  - INVALID_REQUEST  请求本身非法（如 query 为空、来源配置不可构建，上抛异常）
  - CANCELLED        协作式取消（上抛 QuestionBankCancelled）
  - AUTH_REQUIRED    目标要求登录/权限（来源级失败，不重试）
  - RATE_LIMITED     限流（429/Retry-After；来源级失败，按响应等待后重试）
  - NETWORK_ERROR    超时/连接/TLS/HTTP 状态异常（来源级失败）
  - UPSTREAM_CHANGED 上游格式改变（非 JSON、字段结构不再匹配，不重试）
  - UNPARSABLE       页面可达但无法提取出题目（登录页除外；登录页→AUTH_REQUIRED）
  - NO_RESULTS       检索成功但无结果（警告级，不算错误）
  - UNEXPECTED       未预期的内部异常（来源级失败兜底，不重试）

红线：本模块只把网络页面/题库 API 的**真实内容**作为候选；搜索摘要绝不冒充题干。
传输层协议 ``PageTransport`` 的契约（B 侧实现或注入时须保证）：

  1. **绝不自动跟随重定向**（引擎手动处理以阻断跨主机凭据与 https→http 降级）；
  2. **绝不携带任何环境凭据**（学校会话 cookie / Authorization），默认全新连接；
  3. 传输级失败抛 ``QuestionBankError``（NETWORK_ERROR 等），不抛裸异常。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

NOT_CONFIGURED = "NOT_CONFIGURED"
INVALID_REQUEST = "INVALID_REQUEST"
CANCELLED = "CANCELLED"
AUTH_REQUIRED = "AUTH_REQUIRED"
RATE_LIMITED = "RATE_LIMITED"
NETWORK_ERROR = "NETWORK_ERROR"
UPSTREAM_CHANGED = "UPSTREAM_CHANGED"
UNPARSABLE = "UNPARSABLE"
NO_RESULTS = "NO_RESULTS"
UNEXPECTED = "UNEXPECTED"

ALL_ERROR_CODES = (
    NOT_CONFIGURED, INVALID_REQUEST, CANCELLED,
    AUTH_REQUIRED, RATE_LIMITED, NETWORK_ERROR, UPSTREAM_CHANGED,
    UNPARSABLE, NO_RESULTS, UNEXPECTED,
)

# ---------------------------------------------------------------------------
# 题型（与 01-A §4 / D 侧蓝图的声明类型对齐；未知一律 other，不猜测）
# ---------------------------------------------------------------------------

TYPE_CHOICE = "choice"
TYPE_JUDGMENT = "judgment"
TYPE_FILL_BLANK = "fill_blank"
TYPE_CALCULATION = "calculation"
TYPE_SHORT_ANSWER = "short_answer"
TYPE_PROOF = "proof"
TYPE_OTHER = "other"

CANONICAL_TYPES = (
    TYPE_CHOICE, TYPE_JUDGMENT, TYPE_FILL_BLANK,
    TYPE_CALCULATION, TYPE_SHORT_ANSWER, TYPE_PROOF, TYPE_OTHER,
)

# 来源种类
KIND_SEARCH_SERVICE = "search_service"   # 通用搜索服务（如 SearXNG JSON API）
KIND_BANK_API = "bank_api"               # 明确题库 API（用户自配上游，A 定义通用 JSON 协议）
KIND_URL_IMPORT = "url_import"           # 用户提供页面 URL，逐页抓取

SOURCE_KINDS = (KIND_SEARCH_SERVICE, KIND_BANK_API, KIND_URL_IMPORT)


class QuestionBankError(Exception):
    """题库检索域错误（形状对齐契约 ErrorInfo：code/message/retryable/details）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details

    def to_info(self) -> dict[str, Any]:
        """对齐 contracts ErrorInfo（不含 request_id，由 B 的 API 层补齐）。"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class QuestionBankCancelled(QuestionBankError):
    """协作式取消（与 sync.SyncCancelled 同语义：取消后立即中断，不落结果）。"""

    def __init__(self, message: str = "题库检索被取消") -> None:
        super().__init__(CANCELLED, message, retryable=False)


# ---------------------------------------------------------------------------
# 传输
# ---------------------------------------------------------------------------

@dataclass
class PageResponse:
    """绝对 URL 请求的最小响应（比 TransportResponse 多 headers/truncated）。"""

    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    final_url: Optional[str] = None

    def json(self) -> Any:
        return json.loads(self.text)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


@runtime_checkable
class PageTransport(Protocol):
    """绝对 URL 取页传输。实现契约见模块 docstring（不跟重定向、无环境凭据）。"""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        max_bytes: Optional[int] = None,
    ) -> PageResponse: ...


# ---------------------------------------------------------------------------
# 请求 / 结果
# ---------------------------------------------------------------------------

@dataclass
class SearchRequest:
    """检索请求（B 构造；字段与 01-A §4 对齐）。

    ``sources`` 为来源配置列表，每项形如::

        {"source_id": "searx-1", "kind": "search_service", "provider": "searxng",
         "base_url": "https://searx.example", "search_path": "/search",
         "headers": {...}}                       # headers 由 B 运行时注入，A 不存
        {"source_id": "bank-1", "kind": "bank_api", "base_url": "...",
         "search_path": "/api/questions/search", "field_map": {...}}
        {"source_id": "url-1",  "kind": "url_import", "urls": ["https://..."]}
    """

    query: str
    keywords: list[str] = field(default_factory=list)
    course_hint: Optional[str] = None       # 只用于本地匹配与输出标注，不发出网
    question_types: list[str] = field(default_factory=list)
    max_candidates: int = 30
    max_pages: int = 5                      # 每个检索型来源最多抓取的页面数
    max_explicit_urls: int = 20             # url_import 模式最多抓取的显式 URL 数
    timeout_s: float = 15.0
    max_page_bytes: int = 2 * 1024 * 1024   # 单页正文上限（超限截断并告警）
    max_retries: int = 1                    # 可重试失败的额外尝试次数
    retry_max_wait_s: float = 30.0          # 遵循 Retry-After 的等待上限
    cache_ttl_s: float = 600.0              # 运行内页缓存 TTL（0 关闭）
    sources: list[dict[str, Any]] = field(default_factory=list)

    def plain_keywords(self) -> list[str]:
        return list(self.keywords)


@dataclass
class SourceOutcome:
    """单来源执行结果（部分失败可见）。"""

    source_id: str
    kind: str
    status: str                             # "ok" | "partial" | "failed"
    candidate_count: int = 0
    pages_fetched: int = 0
    error: Optional[dict[str, Any]] = None  # ErrorInfo 形状
    notes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "status": self.status,
            "candidate_count": self.candidate_count,
            "pages_fetched": self.pages_fetched,
            "error": self.error,
            "notes": self.notes,
        }


@dataclass
class SearchOutcome:
    """search_candidates 返回值：候选 + 警告 + 各来源分报（B/D 可见完整过程）。"""

    status: str                             # "ok" | "partial" | "failed"
    candidates: list[dict[str, Any]]
    warnings: list[dict[str, Any]]          # {"code","message","source_id"?,"details"?}
    sources: list[SourceOutcome]
    query: str = ""
    keywords: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "keywords": self.keywords,
            "generated_at": self.generated_at,
            "candidates": self.candidates,
            "warnings": self.warnings,
            "sources": [s.to_dict() for s in self.sources],
        }
