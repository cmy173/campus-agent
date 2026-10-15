#!/usr/bin/env python3
"""三种题目来源模式（01-A §3）：

  1. ``SearxngSource``      通用搜索服务（SearXNG JSON API）。**搜索结果只当页面 URL**，
     必须真实抓取页面并从中提取题目；搜索摘要/标题绝不作为题干或答案。
  2. ``GenericBankApiSource`` 明确题库 API。A 定义通用 JSON 协议（见类 docstring），
     由用户/B 配置端点与字段映射；数据以上游返回为准，缺答案如实为空。
  3. ``UrlImportSource``    用户提供页面 URL，逐页抓取提取（含 SSRF 防护）。

单来源失败不静默：``SourceSearchResult`` 携带逐页/逐条警告与 fatal（全失败原因）；
"搜索成功但无结果"为 NO_RESULTS 警告（非错误），"页面可达但提不出题"为 UNPARSABLE。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .normalize import collapse_ws, options_to_list, resolve_and_sanitize
from .types import (
    INVALID_REQUEST,
    KIND_BANK_API,
    KIND_SEARCH_SERVICE,
    KIND_URL_IMPORT,
    QuestionBankError,
)


@dataclass
class SourceSearchResult:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0
    pages_attempted: int = 0
    fatal: Optional[QuestionBankError] = None


class BaseSource:
    kind = ""

    def __init__(self, source_id: str, config: dict[str, Any]) -> None:
        self.source_id = source_id
        self.config = config

    def search(self, run: Any, request: Any) -> SourceSearchResult:  # pragma: no cover - 抽象
        raise NotImplementedError

    # -- 工具 --

    def _warn(self, code: str, message: str, **details: Any) -> dict[str, Any]:
        return {"code": code, "message": message, "source_id": self.source_id,
                "details": details or None}


def _join_base(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + (path or "").lstrip("/")


class SearxngSource(BaseSource):
    """SearXNG JSON API（``GET {base}{search_path}?q=&format=json``）。

    配置：``base_url``（必填）、``search_path``（默认 ``/search``）、
    ``extra_params``（可选，静态查询参数如 language）、``headers``（由 B 运行时注入）。
    """

    kind = KIND_SEARCH_SERVICE

    def search(self, run: Any, request: Any) -> SourceSearchResult:
        result = SourceSearchResult()
        endpoint = _join_base(self.config["base_url"], self.config.get("search_path", "/search"))
        params: dict[str, Any] = {"q": " ".join(request.keywords), "format": "json"}
        params.update(self.config.get("extra_params") or {})
        try:
            resp = run.request_checked("GET", endpoint, params=params,
                                       headers=self.config.get("headers"), what="搜索服务")
        except QuestionBankError as exc:
            if exc.code == "NETWORK_ERROR" and exc.details and exc.details.get("status") == 404:
                result.fatal = QuestionBankError(
                    "UPSTREAM_CHANGED", "搜索端点返回 404：请核对该实例的 search_path 配置",
                    retryable=False, details=exc.details)
                return result
            raise
        try:
            payload = resp.json()
        except (ValueError, TypeError) as exc:
            result.fatal = QuestionBankError(
                "UPSTREAM_CHANGED",
                "搜索服务响应不是 JSON（实例可能未启用 format=json 或返回了 HTML 页面）",
                retryable=False, details={"host": run.host_of(endpoint)})
            return result
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            result.fatal = QuestionBankError(
                "UPSTREAM_CHANGED", "搜索服务响应结构不符合预期（缺少 results 列表）",
                retryable=False, details={"keys": sorted(payload.keys())[:10] if isinstance(payload, dict) else None})
            return result

        urls: list[str] = []
        seen: set[str] = set()
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= request.max_pages:
                break
        if not urls:
            result.warnings.append(self._warn(
                "NO_RESULTS", "搜索成功但无可用结果 URL（0 条 http/https 结果）"))
            return result

        for url in urls:
            result.pages_attempted += 1
            try:
                cands = run.page_candidates(url, self.source_id)
            except QuestionBankError as exc:
                result.warnings.append(self._warn(
                    exc.code, f"页面抓取/提取失败：{exc.message}", url=resolve_and_sanitize(url, url)))
                result.fatal = exc
                continue
            result.pages_fetched += 1
            result.candidates.extend(cands)
        if result.candidates:
            result.fatal = None
        elif result.pages_attempted and result.fatal is None:
            result.fatal = QuestionBankError(
                "UNPARSABLE", "搜索结果页均未提取出题目", retryable=False,
                details={"pages_attempted": result.pages_attempted})
        return result


class GenericBankApiSource(BaseSource):
    """用户配置的题库 API（A 定义的通用 JSON 协议；字段映射可配）。

    请求（默认 ``POST {base}{search_path}``，body 为 JSON）::

        {"query": "关键词串", "keywords": [...], "limit": N, "types": [...]}

    期望响应（下列之一的 items 位置均兼容）::

        {"items": [ {...}, ... ]}          # 或 results / questions / data.items

    条目的规范字段（可用 ``field_map`` 重映射，键为规范名、值为上游实际键名）::

        stem 题干（必填；缺失的条目跳过并告警）
        type/options/answer/analysis/difficulty/knowledge_point
        source_url（可选；缺失时以 API 端点为来源标识）
        license_note（可选）

    配置还可含 ``method``（默认 POST）、``request_defaults``（并入请求 body）、
    ``headers``（由 B 运行时注入；A 不存储密钥）、``license_note``（来源级使用限制）。
    """

    kind = KIND_BANK_API

    DEFAULT_FIELD_MAP = {
        "stem": "stem",
        "type": "type",
        "options": "options",
        "answer": "answer",
        "analysis": "analysis",
        "difficulty": "difficulty",
        "knowledge_point": "knowledge_point",
        "source_url": "source_url",
        "license_note": "license_note",
    }
    _ITEM_KEYS = ("items", "results", "questions")

    def _pick_items(self, payload: Any) -> Optional[list[Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in self._ITEM_KEYS:
                if isinstance(payload.get(key), list):
                    return payload[key]
            data = payload.get("data")
            if isinstance(data, dict):
                for key in self._ITEM_KEYS:
                    if isinstance(data.get(key), list):
                        return data[key]
            if isinstance(data, list):
                return data
        return None

    def search(self, run: Any, request: Any) -> SourceSearchResult:
        result = SourceSearchResult()
        endpoint = _join_base(self.config["base_url"], self.config.get("search_path", "/search"))
        method = str(self.config.get("method", "POST")).upper()
        body: dict[str, Any] = {
            "query": " ".join(request.keywords),
            "keywords": list(request.keywords),
            "limit": request.max_candidates,
        }
        if request.question_types:
            body["types"] = list(request.question_types)
        body.update(self.config.get("request_defaults") or {})
        kwargs: dict[str, Any] = {"headers": self.config.get("headers"), "what": "题库 API"}
        if method in ("GET",):
            params: dict[str, Any] = {}
            for k, v in body.items():
                if isinstance(v, list):
                    params[k] = ",".join(str(x) for x in v)
                elif not isinstance(v, dict):
                    params[k] = v
            resp = run.request_checked("GET", endpoint, params=params, **kwargs)
        else:
            resp = run.request_checked(method, endpoint, json_body=body, **kwargs)

        try:
            payload = resp.json()
        except (ValueError, TypeError):
            result.fatal = QuestionBankError(
                "UPSTREAM_CHANGED", "题库 API 响应不是 JSON",
                retryable=False, details={"host": run.host_of(endpoint)})
            return result
        items = self._pick_items(payload)
        if items is None:
            result.fatal = QuestionBankError(
                "UPSTREAM_CHANGED",
                "题库 API 响应结构不符合通用协议（未找到 items/results/questions 列表）",
                retryable=False,
                details={"keys": sorted(payload.keys())[:10] if isinstance(payload, dict) else None})
            return result
        if not items:
            result.warnings.append(self._warn("NO_RESULTS", "题库 API 返回空结果"))
            return result

        field_map = dict(self.DEFAULT_FIELD_MAP)
        field_map.update(self.config.get("field_map") or {})
        license_note = self.config.get("license_note") or "由用户配置的题库 API 提供；使用限制以该服务条款为准"
        endpoint_safe = resolve_and_sanitize(endpoint, endpoint)
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                result.warnings.append(self._warn("UNPARSABLE", f"条目 #{idx} 不是对象，已跳过"))
                continue
            stem = collapse_ws(str(item.get(field_map["stem"]) or ""))
            if not stem:
                result.warnings.append(self._warn("UNPARSABLE", f"条目 #{idx} 缺少题干字段，已跳过"))
                continue
            raw_answer = item.get(field_map["answer"])
            answer: Optional[str] = None
            if isinstance(raw_answer, (list, tuple)):
                answer = "、".join(collapse_ws(str(a)) for a in raw_answer if str(a).strip()) or None
            elif raw_answer is not None and str(raw_answer).strip():
                answer = collapse_ws(str(raw_answer))
            source_url = item.get(field_map["source_url"])
            source_url = resolve_and_sanitize(endpoint_safe, str(source_url)) if source_url else endpoint_safe
            result.candidates.append({
                "stem": stem,
                "type": None,  # 由引擎按声明值+证据推断（infer_type）
                "declared_type": item.get(field_map["type"]),
                "options": options_to_list(item.get(field_map["options"])),
                "answer": answer,
                "analysis": (collapse_ws(str(item.get(field_map["analysis"]))) or None)
                            if item.get(field_map["analysis"]) else None,
                "knowledge_point": (collapse_ws(str(item.get(field_map["knowledge_point"]))) or None)
                                   if item.get(field_map["knowledge_point"]) else None,
                "difficulty": (collapse_ws(str(item.get(field_map["difficulty"]))) or None)
                              if item.get(field_map["difficulty"]) else None,
                "source_url": source_url,
                "source_site": run.host_of(endpoint),
                "source_locator": {"kind": "api_item", "index": idx, "page_title": None},
                "fetched_at": run.now(),
                "origin": "online",
                "license_note": (collapse_ws(str(item.get(field_map["license_note"]))) or license_note)
                                if item.get(field_map["license_note"]) else license_note,
                "source_id": self.source_id,
                "attachments": [],
                "extractor_version": None,
                "extract_confidence": 0.8,  # 结构化 API 数据；字段缺失不计入，如实体现
            })
        return result


class UrlImportSource(BaseSource):
    """用户显式提供的页面 URL 列表（配置：``urls`` 非空）。"""

    kind = KIND_URL_IMPORT

    def search(self, run: Any, request: Any) -> SourceSearchResult:
        result = SourceSearchResult()
        urls: list[str] = [str(u).strip() for u in (self.config.get("urls") or []) if str(u).strip()]
        if len(urls) > request.max_explicit_urls:
            result.warnings.append(self._warn(
                "CANDIDATES_CAPPED",
                f"显式 URL 数（{len(urls)}）超过上限 {request.max_explicit_urls}，仅抓取前 {request.max_explicit_urls} 条",
                total=len(urls)))
            urls = urls[: request.max_explicit_urls]
        if not urls:
            result.fatal = QuestionBankError(
                INVALID_REQUEST, "url_import 来源未提供 URL", retryable=False)
            return result
        for url in urls:
            result.pages_attempted += 1
            try:
                cands = run.page_candidates(url, self.source_id)
            except QuestionBankError as exc:
                result.warnings.append(self._warn(
                    exc.code, f"页面抓取/提取失败：{exc.message}", url=resolve_and_sanitize(url, url)))
                result.fatal = exc
                continue
            result.pages_fetched += 1
            result.candidates.extend(cands)
        if result.candidates:
            result.fatal = None
        elif result.pages_attempted and result.fatal is None:
            result.fatal = QuestionBankError(
                "UNPARSABLE", "提供的 URL 均未提取出题目", retryable=False,
                details={"pages_attempted": result.pages_attempted})
        return result


def build_source(config: dict[str, Any]) -> BaseSource:
    """按配置构建来源；配置非法时抛 INVALID_REQUEST（属调用方错误）。"""
    if not isinstance(config, dict):
        raise QuestionBankError(INVALID_REQUEST, "来源配置必须是对象", retryable=False)
    kind = str(config.get("kind") or "")
    source_id = str(config.get("source_id") or f"src-{kind or 'unknown'}")
    if kind == KIND_SEARCH_SERVICE:
        provider = str(config.get("provider") or "searxng").lower()
        if provider != "searxng":
            raise QuestionBankError(
                INVALID_REQUEST, f"暂不支持的搜索服务 provider：{provider}（当前仅 searxng）",
                retryable=False, details={"source_id": source_id})
        if not str(config.get("base_url") or "").startswith(("http://", "https://")):
            raise QuestionBankError(INVALID_REQUEST, "search_service 来源缺少合法 base_url",
                                    retryable=False, details={"source_id": source_id})
        return SearxngSource(source_id, config)
    if kind == KIND_BANK_API:
        base = str(config.get("base_url") or "")
        if not base.startswith(("http://", "https://")):
            raise QuestionBankError(INVALID_REQUEST, "bank_api 来源缺少合法 base_url",
                                    retryable=False, details={"source_id": source_id})
        return GenericBankApiSource(source_id, config)
    if kind == KIND_URL_IMPORT:
        return UrlImportSource(source_id, config)
    raise QuestionBankError(
        INVALID_REQUEST, f"未知来源种类 kind={kind!r}（支持：search_service/bank_api/url_import）",
        retryable=False, details={"source_id": source_id})
