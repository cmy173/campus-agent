#!/usr/bin/env python3
"""题库检索编排引擎与对外三接口（01-A §3/§4）：

  - ``search_candidates(search_request, transport, cancellation) -> SearchOutcome``
      多来源执行、单来源失败隔离（部分失败可见）、跨来源去重、限额裁剪、逐页缓存。
  - ``fetch_candidate(candidate_handle, transport, ...) -> dict``
      单题刷新：重新抓取来源页并核对定位（未变化/已移位/已消失，消失=明确失败）。
  - ``get_questionbank_capabilities(config=None) -> dict``
      已配置来源与支持能力（供 B 展示）。

安全与节流（实现要点）：
  - 每个网络请求前做目标校验（SSRF：拒绝字面内网/环回 IP 与 localhost/.local 等；不做 DNS
    复核——离线优先，见 handoff 说明），重定向手动跟随（≤3 跳，跨主机丢弃自定义头，
    阻断 https→http 降级）；
  - 重试仅用于可重试错误（超时/连接/429/5xx），遵循 Retry-After（上限 cap），
    401/403/格式错误不重试；
  - 全文抓取有单页大小上限（超限截断 + 警告），每来源抓取页数有上限；
  - 取消为协作式：在来源/页/重试等待之间检查，取消后立即抛 QuestionBankCancelled。
"""
from __future__ import annotations

import ipaddress
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

from .extract_html import EXTRACTOR_VERSION, extract_page, looks_like_login
from .normalize import (
    canonicalize_type_filter,
    candidate_id,
    collapse_ws,
    dedupe_candidates,
    infer_type,
    match_reason_and_score,
    resolve_and_sanitize,
    site_of,
)
from .sources import build_source
from .types import (
    AUTH_REQUIRED,
    INVALID_REQUEST,
    NETWORK_ERROR,
    NOT_CONFIGURED,
    PageResponse,
    QuestionBankCancelled,
    QuestionBankError,
    RATE_LIMITED,
    SearchOutcome,
    SearchRequest,
    SourceOutcome,
)

_FAILURE_WARNING_CODES = {AUTH_REQUIRED, RATE_LIMITED, NETWORK_ERROR, "UPSTREAM_CHANGED", "UNPARSABLE"}

_MAX_REDIRECTS = 3
_RETRY_BASE_WAIT_S = 1.0


# ---------------------------------------------------------------------------
# 协作式取消
# ---------------------------------------------------------------------------

def _is_cancelled(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    if callable(cancellation) and not hasattr(cancellation, "is_cancelled"):
        return bool(cancellation())
    checker = getattr(cancellation, "is_cancelled", None)
    if callable(checker):
        return bool(checker())
    if hasattr(cancellation, "cancelled"):
        return bool(cancellation.cancelled)
    return False


# ---------------------------------------------------------------------------
# 页缓存（内存 TTL）
# ---------------------------------------------------------------------------

class PageCache:
    """运行内页缓存：同一 URL 在 TTL 内不重复抓取（合规低频；不落盘、不跨进程）。"""

    def __init__(self, ttl_s: float = 600.0) -> None:
        self._ttl = ttl_s
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, url: str) -> Optional[dict[str, Any]]:
        if self._ttl <= 0:
            return None
        entry = self._store.get(url)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._ttl:
            self._store.pop(url, None)
            return None
        return value

    def put(self, url: str, value: dict[str, Any]) -> None:
        if self._ttl <= 0:
            return
        self._store[url] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_retry_after(value: str, *, cap: float, now_ts: float) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return max(0.0, min(seconds, cap))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, min(dt.timestamp() - now_ts, cap))
    except (TypeError, ValueError):
        return None


def _check_url_allowed(url: str, *, allow_private: bool) -> Optional[str]:
    """SSRF 目标校验：返回阻断原因；None 表示允许。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "URL 无法解析"
    if parts.scheme not in ("http", "https"):
        return f"仅支持 http/https（实际 {parts.scheme or '空'}）"
    host = (parts.hostname or "").strip().lower()
    if not host:
        return "缺少主机名"
    if allow_private:
        return None
    if host == "localhost" or host.endswith((".local", ".internal", ".lan", ".localhost", ".home.arpa")):
        return f"阻断本地/内网主机名：{host}"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # 域名不做 DNS 复核（见模块 docstring）
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return f"阻断非公网 IP 目标：{host}"
    return None


class _Run:
    """一次检索运行的共享上下文（transport/请求参数/缓存/告警收集）。"""

    def __init__(self, request: SearchRequest, transport: Any, *,
                 sleep: Callable[[float], None], cancellation: Any,
                 cache: Optional[PageCache], allow_private_addresses: bool) -> None:
        self.request = request
        self.transport = transport
        self.sleep = sleep
        self.cancellation = cancellation
        self.cache = cache if cache is not None else PageCache(request.cache_ttl_s)
        self.allow_private = allow_private_addresses
        self.notes: list[dict[str, Any]] = []          # 页面级告警（截断等）
        self.non_candidate_warnings: list[dict[str, Any]] = []  # 缓存命中数等

    # -- 基础 --

    def now(self) -> str:
        return _utc_now_iso()

    def host_of(self, url: str) -> str:
        return site_of(url)

    def check_cancel(self) -> None:
        if _is_cancelled(self.cancellation):
            raise QuestionBankCancelled()

    def add_note(self, code: str, message: str, *, source_id: Optional[str] = None,
                 **details: Any) -> None:
        entry: dict[str, Any] = {"code": code, "message": message, "details": details or None}
        if source_id:
            entry["source_id"] = source_id
        self.notes.append(entry)

    # -- 网络 --

    def _assert_allowed(self, url: str, *, code: str) -> None:
        reason = _check_url_allowed(url, allow_private=self.allow_private)
        if reason:
            raise QuestionBankError(code, f"目标地址被安全策略阻断：{reason}", retryable=False,
                                    details={"reason": "blocked_address", "detail": reason,
                                             "url": resolve_and_sanitize(url, url)})

    def _classify_status(self, resp: PageResponse, *, what: str, url: str) -> Optional[QuestionBankError]:
        if 200 <= resp.status_code < 300:
            return None
        safe = resolve_and_sanitize(url, url)
        if resp.status_code in (401, 403):
            return QuestionBankError(
                AUTH_REQUIRED,
                f"{what}返回 {resp.status_code}：需要登录或权限（也可能是反爬拦截）；当前不支持登录/绕过",
                retryable=False, details={"status": resp.status_code, "url": safe})
        if resp.status_code == 429:
            retry_after = _parse_retry_after(
                resp.header("retry-after"), cap=self.request.retry_max_wait_s,
                now_ts=time.time())
            return QuestionBankError(
                RATE_LIMITED, f"{what}限流（429）", retryable=True,
                details={"status": 429, "retry_after_s": retry_after, "url": safe})
        if resp.status_code >= 500:
            retry_after = _parse_retry_after(
                resp.header("retry-after"), cap=self.request.retry_max_wait_s,
                now_ts=time.time())
            return QuestionBankError(
                NETWORK_ERROR, f"{what}服务错误（{resp.status_code}）", retryable=True,
                details={"status": resp.status_code, "retry_after_s": retry_after, "url": safe})
        return QuestionBankError(
            NETWORK_ERROR, f"{what}返回 {resp.status_code}", retryable=False,
            details={"status": resp.status_code, "url": safe})

    def _request_once(self, method: str, url: str, *,
                      params: Optional[dict[str, Any]], json_body: Optional[dict[str, Any]],
                      headers: Optional[dict[str, str]], max_bytes: Optional[int]) -> PageResponse:
        self.check_cancel()
        try:
            resp = self.transport.request(method, url, params=params, json_body=json_body,
                                          headers=headers, max_bytes=max_bytes)
        except (QuestionBankError, AssertionError):
            raise
        except Exception as exc:  # noqa: BLE001 - 传输实现异常统一映射，避免单来源炸整批
            raise QuestionBankError(
                NETWORK_ERROR, f"传输层异常：{type(exc).__name__}", retryable=False,
                details={"reason": f"transport_exception:{type(exc).__name__}",
                         "host": self.host_of(url)}) from exc
        if not isinstance(resp, PageResponse):
            raise QuestionBankError(
                NETWORK_ERROR, "传输实现返回了非 PageResponse 对象", retryable=False,
                details={"reason": "contract_violation", "host": self.host_of(url)})
        return resp

    def _fetch_following_redirects(self, method: str, url: str, *,
                                   params: Optional[dict[str, Any]],
                                   json_body: Optional[dict[str, Any]],
                                   headers: Optional[dict[str, str]],
                                   max_bytes: Optional[int],
                                   what: str) -> tuple[PageResponse, str]:
        original_parts = urlsplit(url)
        current_url, current_method = url, method
        current_params, current_body = params, json_body
        hops = 0
        while True:
            self._assert_allowed(current_url, code=NETWORK_ERROR)
            # 跨主机重定向时丢弃调用方自定义头（含潜在 Authorization），阻断凭据扩散
            parts = urlsplit(current_url)
            same_origin = (parts.scheme, parts.hostname, parts.port) == (
                original_parts.scheme, original_parts.hostname, original_parts.port)
            hop_headers = headers if same_origin else None
            resp = self._request_once(current_method, current_url, params=current_params,
                                      json_body=current_body, headers=hop_headers,
                                      max_bytes=max_bytes)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.header("location")
                if not location:
                    raise QuestionBankError(
                        NETWORK_ERROR, f"{what}返回 {resp.status_code} 但缺少 Location 头",
                        retryable=False, details={"status": resp.status_code,
                                                  "url": resolve_and_sanitize(current_url, current_url)})
                if hops >= _MAX_REDIRECTS:
                    raise QuestionBankError(
                        NETWORK_ERROR, f"{what}重定向超过 {_MAX_REDIRECTS} 跳",
                        retryable=False, details={"reason": "too_many_redirects",
                                                  "url": resolve_and_sanitize(url, url)})
                next_url = urljoin(current_url, location)
                if urlsplit(current_url).scheme == "https" and urlsplit(next_url).scheme == "http":
                    raise QuestionBankError(
                        NETWORK_ERROR, "阻断 https→http 降级重定向", retryable=False,
                        details={"reason": "https_downgrade_blocked",
                                 "url": resolve_and_sanitize(current_url, current_url)})
                if resp.status_code in (301, 302, 303):
                    current_method, current_params, current_body = "GET", None, None
                hops += 1
                current_url = next_url
                continue
            resp.final_url = current_url
            return resp, current_url

    def http(self, method: str, url: str, *, params: Optional[dict[str, Any]] = None,
             json_body: Optional[dict[str, Any]] = None,
             headers: Optional[dict[str, str]] = None,
             max_bytes: Optional[int] = None, what: str = "页面") -> tuple[PageResponse, str]:
        """带重定向跟随 + 有限重试的请求（重试仅针对可重试错误，遵循 Retry-After）。"""
        attempt = 0
        while True:
            self.check_cancel()
            try:
                return self._fetch_following_redirects(
                    method, url, params=params, json_body=json_body,
                    headers=headers, max_bytes=max_bytes, what=what)
            except QuestionBankError as exc:
                if not exc.retryable or attempt >= self.request.max_retries:
                    raise
                retry_after = None
                if exc.details:
                    retry_after = exc.details.get("retry_after_s")
                wait = retry_after if retry_after is not None else min(
                    _RETRY_BASE_WAIT_S * (2 ** attempt), self.request.retry_max_wait_s)
                attempt += 1
                self.check_cancel()
                self.sleep(wait)

    def request_checked(self, method: str, url: str, *, params: Optional[dict[str, Any]] = None,
                        json_body: Optional[dict[str, Any]] = None,
                        headers: Optional[dict[str, str]] = None,
                        max_bytes: Optional[int] = None, what: str = "接口") -> PageResponse:
        """请求并校验 2xx：401/403/429/4xx/5xx 映射为域错误；可重试错误按退避/Retry-After 重试。"""
        attempt = 0
        while True:
            resp, _final = self.http(method, url, params=params, json_body=json_body,
                                     headers=headers, max_bytes=max_bytes, what=what)
            err = self._classify_status(resp, what=what, url=url)
            if err is None:
                return resp
            if not err.retryable or attempt >= self.request.max_retries:
                raise err
            wait = None
            if err.details:
                wait = err.details.get("retry_after_s")
            if wait is None:
                wait = min(_RETRY_BASE_WAIT_S * (2 ** attempt), self.request.retry_max_wait_s)
            attempt += 1
            self.check_cancel()
            self.sleep(wait)

    # -- 页面 → 候选 --

    def page_candidates(self, url: str, source_id: str) -> list[dict[str, Any]]:
        """抓取一页并提取候选：缓存命中不重复抓取；登录页→AUTH_REQUIRED；空页→UNPARSABLE。"""
        self.check_cancel()
        cached = self.cache.get(url)
        if cached is not None:
            html, final_url, truncated, fetched_at = (
                cached["html"], cached["final_url"], cached["truncated"], cached["fetched_at"])
            self.non_candidate_warnings.append({
                "code": "CACHE_HIT", "message": "页面命中运行内缓存（未重复抓取）",
                "source_id": source_id, "details": {"url": resolve_and_sanitize(url, url)}})
        else:
            resp = self.request_checked("GET", url, max_bytes=self.request.max_page_bytes,
                                        what="页面")
            final_url = resp.final_url or url
            html, truncated, fetched_at = resp.text, resp.truncated, self.now()
            self.cache.put(url, {"html": html, "final_url": final_url,
                                 "truncated": truncated, "fetched_at": fetched_at})
            if truncated:
                self.add_note(
                    "PAGE_TRUNCATED",
                    f"页面正文超过 {self.request.max_page_bytes} 字节已截断（仅提取截断前内容）",
                    source_id=source_id, url=resolve_and_sanitize(final_url, final_url),
                    max_bytes=self.request.max_page_bytes)
        extraction = extract_page(html, page_url=final_url, source_id=source_id,
                                  fetched_at=fetched_at,
                                  type_filter=canonicalize_type_filter(self.request.question_types) or None)
        for note in extraction.notes:
            self.add_note("EXTRACTION_NOTE", note, source_id=source_id,
                          url=resolve_and_sanitize(final_url, final_url))
        if not extraction.candidates:
            safe = resolve_and_sanitize(final_url, final_url)
            if looks_like_login(html):
                raise QuestionBankError(
                    AUTH_REQUIRED, "页面为登录/认证页（不支持登录、不绕过）", retryable=False,
                    details={"url": safe})
            raise QuestionBankError(
                "UNPARSABLE", "页面可达但未提取出题目", retryable=False,
                details={"url": safe})
        return extraction.candidates


# ---------------------------------------------------------------------------
# 请求规整
# ---------------------------------------------------------------------------

_SEARCH_REQUEST_FIELDS = {
    "query", "keywords", "course_hint", "question_types", "max_candidates", "max_pages",
    "max_explicit_urls", "timeout_s", "max_page_bytes", "max_retries", "retry_max_wait_s",
    "cache_ttl_s", "sources",
}


def _request_from_input(raw: Any) -> SearchRequest:
    if isinstance(raw, SearchRequest):
        request = raw
    elif isinstance(raw, dict):
        unknown = set(raw.keys()) - _SEARCH_REQUEST_FIELDS
        if unknown:
            # 未知字段忽略而非报错（B 侧演进友好），但明确不静默：写入请求详情供诊断
            pass
        try:
            request = SearchRequest(**{k: v for k, v in raw.items() if k in _SEARCH_REQUEST_FIELDS})
        except TypeError as exc:
            raise QuestionBankError(INVALID_REQUEST, f"检索请求字段类型不合法：{exc}",
                                    retryable=False) from exc
    else:
        raise QuestionBankError(INVALID_REQUEST, "search_request 必须是 SearchRequest 或 dict",
                                retryable=False)
    query = collapse_ws(str(request.query or ""))
    keywords = [collapse_ws(str(k)) for k in (request.keywords or []) if collapse_ws(str(k))]
    if not query and not keywords:
        raise QuestionBankError(INVALID_REQUEST, "检索请求缺少 query/keywords", retryable=False)
    if not keywords:
        keywords = [part for part in re.split(r"[\s,，、;；]+", query) if part][:8]
    request.query = query or " ".join(keywords)
    request.keywords = keywords[:8]
    if not isinstance(request.sources, list) or not request.sources:
        raise QuestionBankError(
            NOT_CONFIGURED,
            "未配置任何题库来源（需在来源配置中提供 search_service/bank_api/url_import）",
            retryable=False)
    if request.max_pages < 1:
        request.max_pages = 1
    if request.max_candidates < 1:
        request.max_candidates = 1
    request.question_types = canonicalize_type_filter(request.question_types or [])
    return request


def _default_transport(request: SearchRequest) -> Any:
    from .page_transport import RequestsPageTransport
    return RequestsPageTransport(timeout=request.timeout_s)


# ---------------------------------------------------------------------------
# 对外接口 1：search_candidates
# ---------------------------------------------------------------------------

def search_candidates(search_request: Any, transport: Any = None,
                      cancellation: Any = None, *,
                      sleep: Callable[[float], None] = time.sleep,
                      cache: Optional[PageCache] = None,
                      allow_private_addresses: bool = False) -> SearchOutcome:
    """多来源检索历史题目候选。

    - ``transport=None`` 时使用生产 ``RequestsPageTransport``（全新无凭据会话）；
      测试注入实现 PageTransport 协议的替身。
    - ``cancellation``：可调用对象（返回 True=已取消）或带 ``is_cancelled()`` 的对象。
    - 请求级错误（NOT_CONFIGURED/INVALID_REQUEST）上抛；来源级失败进 outcome（部分失败可见），
      全部来源失败时返回 ``status="failed"`` 而**不抛异常**（调用方可按需展示）。
    """
    request = _request_from_input(search_request)
    if transport is None:
        transport = _default_transport(request)
    run = _Run(request, transport, sleep=sleep, cancellation=cancellation,
               cache=cache, allow_private_addresses=allow_private_addresses)

    warnings: list[dict[str, Any]] = []
    source_outcomes: list[SourceOutcome] = []
    collected: list[dict[str, Any]] = []
    order = 0

    # 构建来源（构建失败不中断其他来源；全部构建失败→INVALID_REQUEST）
    built: list[Any] = []
    for cfg in request.sources:
        run.check_cancel()
        try:
            built.append(build_source(cfg))
        except QuestionBankError as exc:
            source_id = str((cfg or {}).get("source_id") or "unknown") if isinstance(cfg, dict) else "unknown"
            kind = str((cfg or {}).get("kind") or "") if isinstance(cfg, dict) else ""
            source_outcomes.append(SourceOutcome(
                source_id=source_id, kind=kind, status="failed", error=exc.to_info()))
            warnings.append({"code": exc.code, "message": f"来源配置不可用：{exc.message}",
                             "source_id": source_id, "details": exc.details})
    if not built:
        detail_sources = [s.source_id for s in source_outcomes]
        raise QuestionBankError(
            INVALID_REQUEST, "所有来源配置均不可构建", retryable=False,
            details={"sources": detail_sources})

    source_seen: set[str] = set()
    for source in built:
        run.check_cancel()
        if source.source_id in source_seen:
            warnings.append({"code": "INVALID_REQUEST",
                             "message": f"来源 ID 重复：{source.source_id}（仅执行首个）",
                             "source_id": source.source_id, "details": None})
            continue
        source_seen.add(source.source_id)
        try:
            result = source.search(run, request)
        except (QuestionBankCancelled, AssertionError):
            raise
        except QuestionBankError as exc:
            source_outcomes.append(SourceOutcome(
                source_id=source.source_id, kind=source.kind, status="failed",
                error=exc.to_info()))
            warnings.append({"code": exc.code, "message": f"来源失败：{exc.message}",
                             "source_id": source.source_id, "details": exc.details})
            continue
        except Exception as exc:  # noqa: BLE001 - 非预期异常按来源隔离并如实上报
            info = QuestionBankError("UNEXPECTED", f"来源内部异常：{type(exc).__name__}: {exc}",
                                     retryable=False).to_info()
            source_outcomes.append(SourceOutcome(
                source_id=source.source_id, kind=source.kind, status="failed", error=info))
            warnings.append({"code": "UNEXPECTED", "message": info["message"],
                             "source_id": source.source_id, "details": None})
            continue

        for w in result.warnings:
            warnings.append(w)
        failure_warnings = [w for w in result.warnings if w.get("code") in _FAILURE_WARNING_CODES]
        if result.fatal is not None and not result.candidates:
            status = "failed"
            error = result.fatal.to_info()
            warnings.append({"code": result.fatal.code, "message": f"来源失败：{result.fatal.message}",
                             "source_id": source.source_id, "details": result.fatal.details})
        else:
            if result.fatal is not None:
                warnings.append({"code": result.fatal.code,
                                 "message": f"来源部分失败：{result.fatal.message}",
                                 "source_id": source.source_id, "details": result.fatal.details})
            status = "partial" if (failure_warnings or result.fatal is not None) else "ok"
            error = None
        for cand in result.candidates:
            cand["source_order"] = order
            order += 1
        source_outcomes.append(SourceOutcome(
            source_id=source.source_id, kind=source.kind, status=status,
            candidate_count=len(result.candidates), pages_fetched=result.pages_fetched,
            error=error))
        collected.extend(result.candidates)

    # 汇总页级告警（截断/提取说明/缓存）
    warnings.extend(run.notes)
    warnings.extend(w for w in run.non_candidate_warnings if w.get("code") != "CACHE_HIT"
                    or True)  # 缓存命中保留为说明级告警

    # 终化候选：题型兜底、匹配理由、稳定 ID、类型过滤
    type_filter = request.question_types
    finalized: list[dict[str, Any]] = []
    for cand in collected:
        if cand.get("type") is None:
            cand["type"] = infer_type(cand.get("stem", ""), cand.get("options", []),
                                      declared=cand.pop("declared_type", None))
        else:
            cand.pop("declared_type", None)
        if type_filter and cand["type"] not in type_filter:
            continue
        if cand["type"] == "other" and not (
                cand.get("answer") or cand.get("options") or cand.get("attachments")
                or str(cand.get("stem", "")).rstrip().endswith(("？", "?"))):
            continue
        reason, score, hits = match_reason_and_score(cand, request.keywords, type_filter=type_filter)
        cand["match_reason"] = reason
        cand["match_score"] = score
        cand["match_keywords"] = hits
        cand["candidate_id"] = candidate_id(cand["stem"], cand.get("options", []),
                                            cand.get("source_url", ""))
        finalized.append(cand)

    deduped, dropped = dedupe_candidates(finalized)
    if dropped:
        warnings.append({
            "code": "DUPLICATE_DROPPED",
            "message": f"跨来源/页内去重丢弃 {len(dropped)} 条重复候选",
            "details": {"dropped": dropped[:10],
                        "dropped_total": len(dropped)},
        })

    deduped.sort(key=lambda c: (-float(c.get("match_score") or 0.0),
                                -float(c.get("extract_confidence") or 0.0),
                                int(c.get("source_order") or 0)))
    if len(deduped) > request.max_candidates:
        warnings.append({"code": "CANDIDATES_CAPPED",
                         "message": f"候选数超过上限 {request.max_candidates}，已裁剪",
                         "details": {"total": len(deduped)}})
        deduped = deduped[: request.max_candidates]
    for cand in deduped:
        cand.pop("source_order", None)

    if source_outcomes and all(s.status == "failed" for s in source_outcomes):
        overall = "failed"
    elif any(s.status != "ok" for s in source_outcomes):
        overall = "partial"
    else:
        overall = "ok"
    if overall == "ok" and not deduped:
        warnings.append({"code": "NO_RESULTS", "message": "检索完成但未获得任何候选题目",
                         "details": None})

    return SearchOutcome(status=overall, candidates=deduped, warnings=warnings,
                         sources=source_outcomes, query=request.query,
                         keywords=request.keywords, generated_at=_utc_now_iso())


# ---------------------------------------------------------------------------
# 对外接口 2：fetch_candidate
# ---------------------------------------------------------------------------

def fetch_candidate(candidate_handle: Any, transport: Any = None, cancellation: Any = None, *,
                    sleep: Callable[[float], None] = time.sleep,
                    cache: Optional[PageCache] = None,
                    allow_private_addresses: bool = False,
                    request_defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """按候选句柄刷新单题（重新抓取来源页并核对定位）。

    句柄即 search_candidates 返回的候选 dict（至少含 ``source_url``）。
    - 页面未变 → 返回新候选（``refresh.status="unchanged"``）；
    - 题干移位/题号变化 → 按题干在页内重新定位（``refresh.status="relocated"``）；
    - 题目已不在页面 → 明确失败（UNPARSABLE，details 含原因），不返回旧内容冒充成功。
    """
    if not isinstance(candidate_handle, dict):
        raise QuestionBankError(INVALID_REQUEST, "candidate_handle 必须是候选项 dict",
                                retryable=False)
    source_url = str(candidate_handle.get("source_url") or "")
    if not source_url.startswith(("http://", "https://")):
        raise QuestionBankError(INVALID_REQUEST, "candidate_handle 缺少可抓取的 source_url",
                                retryable=False)
    fields = {"max_retries": 1, "cache_ttl_s": 0.0}
    if request_defaults:
        fields.update({k: v for k, v in request_defaults.items() if k in _SEARCH_REQUEST_FIELDS})
    request = _request_from_input({"query": "刷新单题", "keywords": ["刷新"],
                                   "sources": [{"kind": "url_import", "urls": [source_url]}],
                                   **fields})
    if transport is None:
        transport = _default_transport(request)
    run = _Run(request, transport, sleep=sleep, cancellation=cancellation,
               cache=cache, allow_private_addresses=allow_private_addresses)

    try:
        candidates = run.page_candidates(source_url, str(candidate_handle.get("source_id") or "url_import"))
    except QuestionBankError as exc:
        if exc.code == "UNPARSABLE":
            raise QuestionBankError(
                "UNPARSABLE", "刷新失败：页面当前未提取出任何题目（可能已改版或题目被移除）",
                retryable=False, details={"reason": "candidate_not_found_after_refetch",
                                          "url": exc.details.get("url") if exc.details else None}) from exc
        raise

    target_stem = collapse_ws(str(candidate_handle.get("stem") or ""))
    target_index = None
    locator = candidate_handle.get("source_locator")
    if isinstance(locator, dict):
        target_index = locator.get("index") if isinstance(locator.get("index"), int) else None

    matched: Optional[dict[str, Any]] = None
    match_status = "unchanged"
    if isinstance(target_index, int) and 0 <= target_index < len(candidates):
        cand_at_index = candidates[target_index]
        if collapse_ws(cand_at_index.get("stem", "")) == target_stem or not target_stem:
            matched = cand_at_index
    if matched is None and target_stem:
        for cand in candidates:
            if collapse_ws(cand.get("stem", "")) == target_stem:
                matched = cand
                match_status = "relocated"
                break
    if matched is None:
        raise QuestionBankError(
            "UNPARSABLE", "刷新失败：题干在页面当前内容中不存在（可能已改动或移除）",
            retryable=False, details={"reason": "candidate_not_found_after_refetch",
                                      "url": resolve_and_sanitize(source_url, source_url)})
    refreshed = dict(matched)
    refreshed["fetched_at"] = _utc_now_iso()
    refreshed["candidate_id"] = candidate_id(refreshed.get("stem", ""),
                                             refreshed.get("options", []),
                                             refreshed.get("source_url", ""))
    if match_status == "relocated":
        refreshed["extract_confidence"] = round(
            float(refreshed.get("extract_confidence") or 0.5) * 0.9, 3)
    # 刷新不含原始检索词：保留句柄中的匹配元数据，避免以题干自我匹配虚高得分
    if "match_score" in candidate_handle:
        refreshed["match_reason"] = candidate_handle.get("match_reason")
        refreshed["match_score"] = candidate_handle.get("match_score")
        refreshed["match_keywords"] = list(candidate_handle.get("match_keywords") or [])
    refreshed["refresh"] = {
        "status": match_status,
        "previous_locator": locator if isinstance(locator, dict) else None,
        "previous_fetched_at": candidate_handle.get("fetched_at"),
    }
    return refreshed


# ---------------------------------------------------------------------------
# 对外接口 3：capabilities
# ---------------------------------------------------------------------------

_SUPPORTED_KINDS_DOC: dict[str, dict[str, Any]] = {
    "search_service": {
        "providers": ["searxng"],
        "features": ["search", "fetch_result_pages", "extract_html", "extract_jsonld"],
        "note": "搜索结果只作页面 URL；题干/答案一律来自真实抓取页面",
    },
    "bank_api": {
        "protocol": "generic-json-v1（items/results/questions 列表；字段可 field_map 重映射）",
        "features": ["structured_items", "field_map", "license_note"],
        "note": "缺答案/解析如实为空，不猜测",
    },
    "url_import": {
        "features": ["fetch_pages", "extract_html", "extract_jsonld"],
        "note": "SSRF 防护对初始 URL 与重定向目标逐跳生效",
    },
}


def get_questionbank_capabilities(config: Any = None) -> dict[str, Any]:
    """已配置来源与支持能力。``config`` 为 {"sources": [...]} 或来源列表（B 传入）。

    - 未配置：``configured=false``，sources 为空列表（B/UI 据此提示配置）；
    - 配置条目不可构建：该条目 ``configured=false`` 且附 ``problem``（不抛错）。
    """
    sources_cfg: list[Any] = []
    if isinstance(config, dict):
        sources_cfg = list(config.get("sources") or [])
    elif isinstance(config, list):
        sources_cfg = list(config)
    entries: list[dict[str, Any]] = []
    for cfg in sources_cfg:
        if not isinstance(cfg, dict):
            entries.append({"source_id": "unknown", "kind": "", "configured": False,
                            "problem": "来源配置必须是对象"})
            continue
        source_id = str(cfg.get("source_id") or "unknown")
        kind = str(cfg.get("kind") or "")
        try:
            source = build_source(cfg)
            entries.append({"source_id": source_id, "kind": source.kind, "configured": True,
                            "problem": None})
        except QuestionBankError as exc:
            entries.append({"source_id": source_id, "kind": kind, "configured": False,
                            "problem": exc.message})
    return {
        "configured": any(e["configured"] for e in entries),
        "source_kinds": _SUPPORTED_KINDS_DOC,
        "extractor_version": EXTRACTOR_VERSION,
        "limits_defaults": {
            "max_candidates": 30, "max_pages": 5, "max_explicit_urls": 20,
            "timeout_s": 15.0, "max_page_bytes": 2 * 1024 * 1024,
            "max_retries": 1, "retry_max_wait_s": 30.0, "cache_ttl_s": 600.0,
            "max_redirects": _MAX_REDIRECTS,
        },
        "sources": entries,
    }
