#!/usr/bin/env python3
"""connectors.questionbanks —— 网络题库检索与题目来源采集（复习卷功能 A 侧模块）。

对外三接口（01-A §3/§4，B 调用、D 消费候选）：

    from connectors.questionbanks import (
        search_candidates, fetch_candidate, get_questionbank_capabilities,
        SearchRequest, PageCache, QuestionBankError, QuestionBankCancelled,
    )

    request = SearchRequest(query="线性代数 矩阵", keywords=["线性代数", "矩阵"],
                            sources=[{"source_id": "s1", "kind": "url_import",
                                      "urls": ["https://example.org/questions"]}])
    outcome = search_candidates(request, transport=None)   # transport=None 用生产取页传输
    outcome.to_dict()["candidates"]                          # 真实候选题（来源可追溯）

红线（全文见 types.py 与各模块 docstring）：
  - 只返回网络真实内容；搜索摘要/AI 生成内容绝不冒充原题；
  - 专用取页传输与学校会话隔离，永不携带学校 cookie/凭据；
  - 不登录、不清验证码、不绕付费墙；单页大小/页数/重试均有上限；
  - 候选保留来源 URL（已剔除敏感参数）、抓取时间、定位与提取置信度；
  - 使用限制字段（license_note）如实标注"未声明许可"。

CLI 示例（最小运行方式）::

    python -m connectors.questionbanks capabilities
    python -m connectors.questionbanks search --query "高等数学 极限" \
        --searxng https://searx.example --max-candidates 10
    python -m connectors.questionbanks import-url https://example.org/q1 https://example.org/q2
"""
from __future__ import annotations

from .engine import (
    PageCache,
    fetch_candidate,
    get_questionbank_capabilities,
    search_candidates,
)
from .extract_html import EXTRACTOR_VERSION, extract_page, looks_like_login
from .page_transport import RequestsPageTransport, strip_sensitive_headers
from .sources import BaseSource, build_source
from .types import (
    ALL_ERROR_CODES,
    CANONICAL_TYPES,
    KIND_BANK_API,
    KIND_SEARCH_SERVICE,
    KIND_URL_IMPORT,
    PageResponse,
    PageTransport,
    QuestionBankCancelled,
    QuestionBankError,
    SearchOutcome,
    SearchRequest,
    SourceOutcome,
)

__all__ = [
    "search_candidates", "fetch_candidate", "get_questionbank_capabilities",
    "SearchRequest", "SearchOutcome", "SourceOutcome", "PageResponse", "PageTransport",
    "QuestionBankError", "QuestionBankCancelled", "PageCache",
    "RequestsPageTransport", "strip_sensitive_headers",
    "build_source", "BaseSource", "extract_page", "looks_like_login",
    "EXTRACTOR_VERSION", "ALL_ERROR_CODES", "CANONICAL_TYPES",
    "KIND_SEARCH_SERVICE", "KIND_BANK_API", "KIND_URL_IMPORT",
]
