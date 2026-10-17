"""M2 offline acceptance contract: three campus business chains through the real product surface.

What this script is
-------------------
A deterministic, no-model, offline driver for the three required campus chains of
``docs/agent-design/harness-campus-readonly-task.md`` section 4 (M2), measured against
``docs/audit/harness-campus-readonly-acceptance.md`` cases C11/C12/C13 and the tool
call/result correlation part of C15:

* chain A (C11) notices: ``campus.sync`` -> ``campus.notice.read`` on the real
  ``CampusService`` + ``ckc_public`` adapter + real campus cache DB;
* chain B (C12) course materials/assignments: ``connections.courses`` ->
  ``connections.courses.import`` (paged, cursor honored) -> ``connections.sync`` ->
  the real worker ``handle_sync`` -> ``jobs.read``/``materials.list``/``materials.read``
  /``assignments.list``;
* chain C (C13) timetable -> local task -> plan preview: ``campus.academics`` ->
  ``tasks.list`` -> ``plans.preview`` on the real plan engine.

Only the *external* boundary is synthetic, and it is replaced at the two seams the
product itself uses: the school HTTP transport
(``connectors.campus.campus_network.PublicPageTransport.get`` and the public-notices
opener) and the platform transport (``app.connectors_registry.protocol_adapter`` for
discovery plus the ``app.sync_engine`` adapter registry the worker resolves through).
Everything else - tools, service, adapters, repositories, worker, schedule engine,
SQLite storage, the real PDF parser - is the real product code.

No HTTP server is started: the replaced transport is a pure function stub that answers
the exact registered school URLs this chain asks for. Nothing to crawl, nothing bound
to a port, nothing that can silently fall back to the real network.

Honesty rules baked in: a synthetic page never becomes "real network"; a metadata
listing never becomes "read the body"; an unknown deadline never becomes a date; an
accepted job is never reported as a completed one; a preview never writes an active
plan. The output document states exactly which boundaries were replaced.

CLI::

    python -X utf8 tools/verify_campus_chains.py [--chains a,b,c] [--out PATH]
        [--public-live] [--campus-live] [--timeout SECONDS]

Exit code is non-zero iff any chain check failed. ``blocked`` (a genuine missing
external condition) and confirmed live probes are exit 0.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEBUG = os.environ.get("M2_DEBUG") == "1"

# ─────────────────────────── network audit (installed before app imports) ──────
# Any socket connect this process makes is recorded, so "offline" is measured on the
# real call rather than asserted from a comment. Loopback is exempt because the
# synthetic page server and SQLite are both local.
_REAL_CONNECT = socket.socket.connect
_CONNECTIONS: list[dict] = []
_CONNECT_LOCK = threading.Lock()


def _is_loopback(host) -> bool:
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _record_connect(host, port) -> None:
    with _CONNECT_LOCK:
        entry = {"family": "unix" if not isinstance(host, str) else "tcp",
                 "host": host if _is_loopback(host) else "<non-loopback host redacted>",
                 "port": port, "loopback": _is_loopback(host),
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _CONNECTIONS.append(entry)
        if not entry["loopback"]:
            print(f"[audit] non-loopback connect: {entry['family']}:{entry['port']}", flush=True)


def _audited_connect(self, address, *args, **kwargs):
    if isinstance(address, tuple) and len(address) >= 2:
        _record_connect(address[0], address[1])
    else:
        _record_connect(address, None)
    return _REAL_CONNECT(self, address, *args, **kwargs)


socket.socket.connect = _audited_connect


def non_loopback_connections() -> list[dict]:
    with _CONNECT_LOCK:
        return [dict(item) for item in _CONNECTIONS if not item["loopback"]]


# ─────────────────────────── isolation (mirrors tools/audit_regression.py) ─────
# The guard below refuses to run if any resolved path lands inside the personal areas
# this script must never touch. It is checked before anything is created.
FORBIDDEN = tuple((ROOT / part).resolve() for part in
                  ("var/personal.db", "var/files", "var/secrets", "var/agent", "var/workspace"))

RUN_ID = uuid.uuid4().hex
WORK = ROOT / "var" / ("m2-campus-chains-" + RUN_ID)
DEFAULT_OUT = ROOT / "var" / "harness-campus-readonly-2b0480ed" / "m2" / "chains.json"


def _guard(paths) -> None:
    for path in paths:
        resolved = Path(path).resolve()
        for forbidden in FORBIDDEN:
            if resolved == forbidden or forbidden in resolved.parents:
                raise SystemExit(f"REFUSING: isolation path {resolved} is inside {forbidden}")
        if resolved == ROOT.resolve():
            raise SystemExit(f"REFUSING: isolation path {resolved} is the repository root itself")


ISOLATION = {
    "STUDY_DATABASE_URL": "sqlite:///" + (WORK / "domain.db").as_posix(),
    "STUDY_FILES_DIR": str(WORK / "files"),
    "STUDY_SECRETS_DIR": str(WORK / "secrets"),
    "CAMPUS_AGENT_HOME": str(WORK / "agent"),
    "CAMPUS_AGENT_WORKSPACE": str(WORK / "workspace"),
    "CAMPUS_WORKSPACE_DIR": str(WORK / "workspace"),
    "CAMPUS_DATA_DIR": str(WORK / "campus"),
    "STUDY_WORKER_ENABLED": "0",
    "STUDY_WORKER_INPROC": "0",
    "TMP": str(WORK / "temp"),
    "TEMP": str(WORK / "temp"),
}
_guard([WORK, *(Path(value[8:]) if key == "STUDY_DATABASE_URL" else Path(value)
                for key, value in ISOLATION.items())])
for folder in ("files", "secrets", "agent", "workspace", "campus", "temp"):
    (WORK / folder).mkdir(parents=True, exist_ok=True)
os.environ.update(ISOLATION)
sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT)]

# ─────────────────────────── synthetic school boundary ────────────────────────
CKC_HOST = "webplus.zju.edu.cn"
CKC_LISTING = "https://webplus.zju.edu.cn/_s597/qbtz/list.psp"
NOTICE_A_ID = 987654
NOTICE_B_ID = 987655
NOTICE_A_TITLE = "关于2026年秋冬学期本科生学业确认的通知"
NOTICE_B_TITLE = "竺可桢学院学业指导讲座安排"
NOTICE_A_DATE = "2026-10-14"
NOTICE_B_DATE = "2026-10-12"
NOTICE_BODY_LINE = "请于2026年10月20日前在教务系统完成学业确认，逾期视为放弃本次调整。"
NOTICE_ATTACHMENT = "https://webplus.zju.edu.cn/_upload/article/files/ab/cd/ef12-3456-7890-abcdef123456.pdf"
ZDBK_BOARD = "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_cxLoginNews.html?mxb_id=newsnotice"
ZDBK_XWBH = "A" * 32
ZDBK_TITLE = "教务处关于选课系统维护的公开通知"
ZDBK_DATE = "2026-10-13 09:30:00"


def _article_path(article_id: int) -> str:
    return f"/_s597/2026/1014/c12345a{article_id}/page.psp"


def _article_url(article_id: int) -> str:
    return "https://" + CKC_HOST + _article_path(article_id)


def notice_list_html() -> str:
    rows = "".join(
        f'<li><span class="news_meta">{stamp}</span>'
        f'<span class="news_title"><a href="{_article_path(article_id)}" title="{title}">{title}</a></span></li>'
        for article_id, title, stamp in (
            (NOTICE_A_ID, NOTICE_A_TITLE, NOTICE_A_DATE),
            (NOTICE_B_ID, NOTICE_B_TITLE, NOTICE_B_DATE)))
    return f'<html><body><div class="news_list"><ul>{rows}</ul></div></body></html>'


def notice_article_html(title: str, published: str, body: str) -> str:
    paragraphs = "".join(f"<p>{line}</p>" for line in body.splitlines() if line.strip())
    attachment_path = NOTICE_ATTACHMENT.removeprefix("https://" + CKC_HOST)
    return (
        '<html><body><div class="article">'
        f'<h1 class="arti_title">{title}</h1>'
        f'<div class="arti_update">发布时间：{published}</div>'
        '<div class="wp_articlecontent">'
        f"{paragraphs}"
        f'<p><a href="{attachment_path}">学业确认表.pdf</a></p>'
        '<p><img src="/_upload/article/images/aa/bb/pic.png" alt="示意图"></p>'
        "</div></div></body></html>")


SYNTHETIC_PAGES = {
    CKC_LISTING: (notice_list_html, "text/html; charset=utf-8"),
    _article_url(NOTICE_A_ID): (lambda: notice_article_html(NOTICE_A_TITLE, NOTICE_A_DATE,
                                                            NOTICE_BODY_LINE + "\n第二段：培养方案以教务系统为准。"),
                                "text/html; charset=utf-8"),
    _article_url(NOTICE_B_ID): (lambda: notice_article_html(NOTICE_B_TITLE, NOTICE_B_DATE,
                                                            "讲座时间与地点以学院门户后续通知为准。"),
                                "text/html; charset=utf-8"),
    ZDBK_BOARD: (lambda: (
        '<html><body><ul class="log-list"><li>'
        f'<a href="/jwglxt/xtgl/xwck_ckLoginNews.html?xwbh={ZDBK_XWBH}&amp;doType=save">{ZDBK_TITLE}</a>'
        f'<span>[{ZDBK_DATE}]</span></li></ul></body></html>'), "text/html; charset=utf-8"),
}
PAGE_REQUESTS: list[str] = []
PAGE_LOCK = threading.Lock()

REPLACED_BOUNDARIES = [
    "connectors.campus.campus_network.PublicPageTransport.get (public college pages)",
    "connectors.campus.campus_network.build_opener (public notices opener)",
    "app.connectors_registry.protocol_adapter (xzzd/zykt platform transport)",
]
NOT_EXERCISED = [
    "real school HTTP/TLS/DNS (no zju.edu.cn request is made offline)",
    "CAS login, passwords, captcha/MFA, browser cookie adoption (no credential store is touched)",
    "WebVPN tunnel, the browser session service and the review/model providers",
]


class _SyntheticResponse:
    """Minimal urlopen-shaped response for the synthetic public page server."""

    def __init__(self, url: str, body: bytes, content_type: str):
        self.code = 200
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self._body = body
        self._url = url

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit is None or limit < 0 else self._body[:limit]

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _synthetic_get(url: str):
    with PAGE_LOCK:
        PAGE_REQUESTS.append(url)
    page = SYNTHETIC_PAGES.get(url)
    if page is None:
        raise AssertionError(f"synthetic public page server has no registered page for {url!r}")
    factory, content_type = page
    return factory().encode("utf-8"), {"Content-Type": content_type}


class _SyntheticOpener:
    def open(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        raw, headers = _synthetic_get(url)
        return _SyntheticResponse(url, raw, headers.get("Content-Type", "text/html"))


# ─────────────────────────── evidence document plumbing ───────────────────────
def _clip(value, limit: int = 4000):
    """Bound recorded evidence strings; assertions always run on the unclipped value."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…(+{len(value) - limit} chars)"
    if isinstance(value, dict):
        return {str(k): _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


def _public_arguments(name: str, arguments: dict) -> dict:
    """Parameter summary for the record: no secrets exist in these schemas, and only
    bounded identifiers/enums are echoed."""
    allowed = {"capability", "platform", "connection_id", "cursor", "all", "full", "period", "day", "term",
               "record_id", "course_id", "resource_id", "expected_version", "limit", "status",
               "start_date", "end_date", "wait_seconds", "job_id"}
    summary = {key: value for key, value in arguments.items() if key in allowed}
    if isinstance(arguments.get("query"), dict):
        summary["query"] = _clip(arguments["query"], 200)
    if arguments.get("external_ids"):
        summary["external_ids"] = _clip(list(arguments["external_ids"])[:5], 200)
    return summary


class Chain:
    """One business chain: ordered tool calls, checks, and the final answer payload."""

    def __init__(self, chain_id: str, title: str, acceptance_cases: list[str]):
        self.data = {"id": chain_id, "title": title, "acceptance_cases": acceptance_cases,
                     "status": "failed", "checks": [], "tool_calls": [], "answer": {}}

    def check(self, name: str, ok: bool, detail) -> bool:
        self.data["checks"].append({"name": name, "ok": bool(ok), "detail": _clip(detail)})
        return bool(ok)

    def call(self, tools, name: str, arguments: dict, db, ctx) -> dict:
        started = time.monotonic()
        error = None
        try:
            result = tools.execute(name, arguments, db, ctx)
        except Exception as exc:
            error = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                     "status_code": getattr(exc, "status_code", None),
                     "traceback": traceback.format_exc()[-1600:]}
            result = {"status": "raised", "data": None, "error": error}
        self.data["tool_calls"].append({
            "name": name, "arguments": _public_arguments(name, arguments),
            "status": result.get("status"), "job_id": result.get("job_id"),
            "error": _clip(result.get("error")) if result.get("error") else None,
            "evidence": _clip(result.get("evidence") or [], 600),
            "coverage": _clip(result.get("coverage")),
            "warnings": _clip(result.get("warnings") or [], 800),
            "next_cursor": result.get("next_cursor"),
            "data": _clip(result.get("data")),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        })
        return result

    def finalize(self) -> dict:
        failures = [check for check in self.data["checks"] if not check["ok"]]
        if not self.data["checks"]:
            self.data["status"] = "failed"
        elif failures:
            self.data["status"] = "failed"
        else:
            self.data["status"] = "passed"
        return self.data


def _notices_of(result: dict) -> list[dict]:
    data = result.get("data") or {}
    return list(data.get("records") or [])


# ─────────────────────────── chain A / C11: notices ───────────────────────────
def chain_notices(chain: Chain, tools, db, ctx) -> None:
    """C11: discovery -> body -> answer, with summary/body/attachment honesty."""
    from app.agent_bridge.tools import ToolContext  # noqa: F401  (documents the real surface)
    from app.campus_workspace import get_campus_workspace, OWNER

    workspace = get_campus_workspace()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # The account's own stored college is what selects the source: seed it through the
    # real snapshot repository, exactly as a real profile sync would.
    workspace.manager.repository.write(OWNER, {
        "profile": {"fetched_at": stamp, "stale": False, "data": {"college": "竺可桢学院"},
                    "field_sources": {"college": {"url": "https://eta.zju.edu.cn/", "fetched_at": stamp,
                                                  "stale": False, "source": "ETA"}}},
        "calendars": {}})

    sync = chain.call(tools, "campus.sync",
                      {"capability": "college_notices", "query": {"limit": 3}, "platform": None}, db, ctx)
    records = _notices_of(sync)
    chain.check("campus.sync returns the fetched synthetic notice list", len(records) >= 2,
                {"records": [{k: r.get(k) for k in ("id", "title", "published_at")} for r in records]})
    chain.check("C11 discovery is honest about its limited scope (list summary, not full text)",
                sync.get("status") == "partial" and sync.get("coverage", {}).get("complete") is False,
                {"status": sync.get("status"), "coverage": sync.get("coverage")})
    listed = next((r for r in records if f":{NOTICE_A_ID}" in str(r.get("id"))), None)
    chain.check("C11 the listed record carries the synthetic title/date/link, not invented ones",
                bool(listed) and listed.get("title") == NOTICE_A_TITLE
                and listed.get("published_at") == NOTICE_A_DATE
                and str(listed.get("source", "")).endswith(_article_path(NOTICE_A_ID)),
                {"listed": listed})
    chain.check("C11 the list page itself was served by the replaced transport (source actually read)",
                CKC_LISTING in PAGE_REQUESTS and _article_url(NOTICE_A_ID) in PAGE_REQUESTS,
                {"pages_requested": list(PAGE_REQUESTS)})

    detail = chain.call(tools, "campus.notice.read", {"record_id": listed["id"] if listed else ""}, db, ctx)
    notice = ((detail.get("data") or {}).get("notice")) or {}
    body = notice.get("body") or ""
    chain.check("C11 the body really came from the fetched detail page",
                NOTICE_BODY_LINE in body and detail.get("status") in {"ok", "partial"},
                {"body_head": body[:160], "status": detail.get("status")})
    chain.check("C11 title/date in the detail match the synthetic source page",
                notice.get("title") == NOTICE_A_TITLE and notice.get("published_at") == NOTICE_A_DATE,
                {"title": notice.get("title"), "published_at": notice.get("published_at")})
    chain.check("C11 summary and body stay separate (list summary is not the full text)",
                notice.get("body") != (listed or {}).get("title")
                and len(body) > len(NOTICE_A_TITLE)
                and all(line not in (listed or {}).get("title", "") for line in body.splitlines() if line),
                {"list_title": (listed or {}).get("title"), "body_chars": len(body)})
    chain.check("C11 the attachment stays UNREAD and is reported as such",
                bool(notice.get("attachments")) and notice.get("has_unread_content") is True,
                {"attachments": notice.get("attachments"), "has_unread_content": notice.get("has_unread_content")})
    file_link = next((item for item in notice.get("attachments") or [] if item.get("name")), {})
    linked_name = file_link.get("name") or ""
    body_without_link_label = body.replace(linked_name, "")
    chain.check("C11 an attachment-only requirement is not claimed as parsed",
                bool(linked_name) and linked_name not in body_without_link_label
                and "附件" not in body
                and "未解析" in json.dumps((sync.get("data") or {}).get("warnings") or [], ensure_ascii=False),
                {"body": body, "attachment_name": linked_name,
                 "body_without_link_label": body_without_link_label,
                 "adapter_warning": (sync.get("data") or {}).get("warnings")})

    # A second source (the校级 public board) is a summary with no body: having it can
    # never be reported as whole-school coverage.
    board = chain.call(tools, "campus.sync",
                       {"capability": "notices", "query": {"limit": 6}, "platform": "zdbk_public"}, db, ctx)
    board_records = _notices_of(board)
    chain.check("C11 second source is a bounded public summary, not a body read",
                len(board_records) >= 1 and (board["data"].get("completeness") == "partial"
                                             if isinstance(board.get("data"), dict) else False),
                {"records": [{k: r.get(k) for k in ("id", "title", "published_at")} for r in board_records],
                 "completeness": (board.get("data") or {}).get("completeness")})
    warnings_text = " ".join(str(w) for w in ((sync.get("warnings") or []) + (board.get("warnings") or [])))
    chain.check("C11 source scope is not widened into whole-school coverage",
                "竺可桢学院" in json.dumps(sync.get("data"), ensure_ascii=False)
                and sync.get("coverage", {}).get("complete") is False
                and "全" not in (notice.get("college") or ""),
                {"college": notice.get("college"), "coverage": sync.get("coverage"),
                 "warning_codes": [w.get("code") for w in (sync.get("data") or {}).get("warnings", [])],
                 "board_warnings": board.get("warnings")})
    chain.check("C11 a source-limited warning is actually surfaced to the answer",
                "full" not in notice.get("college", "") and bool(warnings_text.strip()),
                {"warnings_head": warnings_text[:200]})

    chain.data["answer"] = {
        "question": "最新一条竺可桢学院通知的标题、日期和正文要点是什么？附件读了吗？",
        "record_id": notice.get("id"), "title": notice.get("title"), "published_at": notice.get("published_at"),
        "source": notice.get("source"), "college": notice.get("college"),
        "body_excerpt": _clip(body, 400), "body_sha256": notice.get("body_sha256"),
        "attachments": notice.get("attachments"), "has_unread_content": notice.get("has_unread_content"),
        "coverage": {"college_scope_only": True, "whole_school_coverage_claimed": False,
                     "reason": "只有竺可桢学院来源；通知列表是摘要且附件未解析"},
        "other_source": {"platform": "zdbk_public", "records": len(board_records),
                         "scope": "公开登录页近期/置顶通知摘要，未读取正文"},
    }


# ─────────────────────────── chain B / C12: courses, sync, materials ──────────
PAGE1 = [
    {"id": "zc-2001", "title": "高等数学（甲）", "term": "2026秋", "teacher": "张老师"},
    {"id": "zc-2002", "title": "数据结构基础", "term": "2026秋", "teacher": "李老师"},
]
PAGE2 = [
    {"id": "zc-2003", "title": "概率论与数理统计", "term": "2026秋", "teacher": "王老师"},
]
PAGE2_CURSOR = "cursor-page-2"
MATERIAL_TITLE = "第三章-极限与连续.pdf"
MATERIAL_BODY = "Chapter 3 limits: the derivative of x squared is 2x."
DUE_KNOWN = "2026-10-25T23:59:00+08:00"


def _synthetic_pdf(text: str) -> bytes:
    """A real text-layer PDF rendered by the product's own parser dependency."""
    import io

    import pypdf
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class SyntheticSchool:
    """Stands in for the 学在浙大 platform transport only (list/sync/bytes).

    Registered at the same two seams the real bridge uses: the discovery adapter
    (``connectors_registry.protocol_adapter``) for ``list_courses`` and the sync
    adapter registry (``sync_engine``) for ``sync_course``/``fetch_resource_bytes``.

    The platform's own course key and the local course id are different namespaces
    (the real adapter resolves one to the other), so the mapping learned from the
    real import path is used to resolve sync requests, exactly like the connector
    service does for its session.
    """

    instances: list["SyntheticSchool"] = []

    def __init__(self):
        self.list_calls: list[dict] = []
        self.sync_calls: list[dict] = []
        self.fetch_calls: list[str] = []
        self.local_to_external: dict[str, str] = {}
        self.pdf = _synthetic_pdf(MATERIAL_BODY)
        SyntheticSchool.instances.append(self)

    def bind_course(self, local_course_id: str, external_id: str) -> None:
        self.local_to_external[local_course_id] = external_id

    def list_courses(self, cursor=None, term=None):
        self.list_calls.append({"cursor": cursor, "term": term})
        if cursor is None:
            return {"items": [dict(item) for item in PAGE1], "next_cursor": PAGE2_CURSOR}
        if cursor == PAGE2_CURSOR:
            return {"items": [dict(item) for item in PAGE2], "next_cursor": None}
        raise AssertionError(f"synthetic platform: unknown cursor {cursor!r}")

    def sync_course(self, course_id, since_cursor=None, kinds=None):
        self.sync_calls.append({"course_id": course_id, "since_cursor": since_cursor, "kinds": kinds})
        external = self.local_to_external.get(course_id, course_id)
        if external != PAGE1[0]["id"]:
            # A course without material: a real platform returns an empty-but-complete
            # envelope, and the answer must not invent content for it.
            return {"platform": "xzzd", "connection_id": "conn_synthetic_xzzd", "course_id": course_id,
                    "mode": "incremental", "cursor": {"previous": since_cursor, "next": None},
                    "upserts": {"resources": [], "assignments": [], "recordings": []},
                    "deletions": [], "errors": [],
                    "stats": {"scanned": 0, "upserted": 0, "deleted": 0, "failed": 0},
                    "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "platform": "xzzd", "connection_id": "conn_synthetic_xzzd", "course_id": course_id,
            "mode": "incremental", "cursor": {"previous": since_cursor, "next": "course-cursor-2"},
            "upserts": {
                "resources": [{
                    "kind": "handout", "title": MATERIAL_TITLE, "version": 1,
                    "content_hash": "sha256:" + "c" * 64, "size_bytes": len(self.pdf),
                    "mime_type": "application/pdf", "access_level": "full",
                    "source_updated_at": stamp,
                    "source_ref": {"platform": "xzzd", "external_id": "zc-file-2001",
                                   "observed_at": stamp},
                }],
                "assignments": [{
                    "title": "第三章习题", "instructions": "完成教材第三章课后习题 1-12。",
                    "due_at": DUE_KNOWN, "due_precision": "minute", "submission_status": "pending",
                    "source_ref": {"platform": "xzzd", "external_id": "zc-asg-known", "observed_at": stamp},
                }, {
                    "title": "期中项目选题", "instructions": "选题要求见平台公告；截止时间平台未给出。",
                    "due_precision": "unknown", "submission_status": "unknown",
                    "source_ref": {"platform": "xzzd", "external_id": "zc-asg-unknown", "observed_at": stamp},
                }],
                "recordings": [],
            },
            "deletions": [], "errors": [],
            "stats": {"scanned": 3, "upserted": 3, "deleted": 0, "failed": 0},
            "observed_at": stamp,
        }

    def fetch_resource_bytes(self, external_id):
        self.fetch_calls.append(external_id)
        return {"status": "ok", "filename": MATERIAL_TITLE, "mime_type": "application/pdf",
                "size": len(self.pdf), "sha256": "sha256:" + __import__("hashlib").sha256(self.pdf).hexdigest(),
                "data_base64": base64.b64encode(self.pdf).decode("ascii")}


def chain_courses(chain: Chain, tools, db, ctx, school: SyntheticSchool) -> None:
    """C12: discovery -> paged attach -> sync job -> real worker -> materials/assignments."""
    from app.models import Connection, Course, Job, SourceRef, utc_now

    db.add(Connection(id="conn_synthetic_xzzd", platform="xzzd", display_name="学在浙大（合成边界）",
                      status="ok"))
    db.commit()

    discovered = chain.call(tools, "connections.courses", {"platform": "xzzd"}, db, ctx)
    items = (discovered.get("data") or {}).get("courses") or []
    chain.check("C12 discovery reads the platform's real course list for page 1",
                [item.get("external_id") or item.get("id") for item in items] == [row["id"] for row in PAGE1],
                {"ids": [item.get("external_id") or item.get("id") for item in items]})
    chain.check("C12 a next_cursor is surfaced instead of the first page being called complete",
                discovered.get("next_cursor") == PAGE2_CURSOR
                and any("分页" in str(w) for w in discovered.get("warnings") or []),
                {"next_cursor": discovered.get("next_cursor"), "warnings": discovered.get("warnings")})

    page1 = chain.call(tools, "connections.courses.import", {"platform": "xzzd", "all": True}, db, ctx)
    for item in (page1.get("data") or {}).get("courses") or []:
        external = next((ref.get("external_id") for ref in item.get("source_refs") or []
                         if ref.get("platform") == "xzzd"), None)
        if external:
            school.bind_course(item["id"], external)
    bound_after_page1 = {ref.external_id for ref in db.query(SourceRef).filter(SourceRef.platform == "xzzd").all()}
    chain.check("C12 importing page 1 binds page-1 courses",
                {item["title"] for item in (page1.get("data") or {}).get("courses") or []}
                == {row["title"] for row in PAGE1},
                {"bound": sorted(bound_after_page1)})
    chain.check("C12 the un-read page 2 course is NOT silently imported as complete",
                PAGE2[0]["id"] not in bound_after_page1,
                {"bound": sorted(bound_after_page1), "page2_id": PAGE2[0]["id"]})

    cursor = page1.get("next_cursor") or PAGE2_CURSOR
    page2 = chain.call(tools, "connections.courses.import",
                       {"platform": "xzzd", "cursor": cursor, "all": True}, db, ctx)
    bound_after_page2 = {ref.external_id for ref in db.query(SourceRef).filter(SourceRef.platform == "xzzd").all()}
    chain.check("C12 the cursor is really honored: page 2 was requested from the platform",
                any(call["cursor"] == PAGE2_CURSOR for call in school.list_calls),
                {"list_calls": school.list_calls})
    chain.check("C12 following the cursor binds the page-2 course",
                PAGE2[0]["id"] in bound_after_page2
                and {item["title"] for item in (page2.get("data") or {}).get("courses") or []}
                == {row["title"] for row in PAGE2},
                {"bound": sorted(bound_after_page2)})

    sync = chain.call(tools, "connections.sync", {"platform": "xzzd"}, db, ctx)
    job_id = sync.get("job_id")
    job_row = db.get(Job, job_id) if job_id else None
    chain.check("C12 sync is accepted as a durable Job row (not an inline fake result)",
                job_row is not None and job_row.type == "sync"
                and job_row.payload.get("connection_id") == "conn_synthetic_xzzd"
                and bool(job_row.payload.get("course_ids")),
                {"job_id": job_id, "type": getattr(job_row, "type", None),
                 "payload": _clip(getattr(job_row, "payload", None))})
    chain.check("C12 acceptance is reported as accepted, never as completed",
                sync.get("status") in {"queued", "running"} and (sync.get("data") or {}).get("job", {}).get("status")
                not in {"succeeded", "partial_failed"},
                {"status": sync.get("status"), "job": (sync.get("data") or {}).get("job")})

    processed = _drain_worker_until(job_id, db)
    # The worker ran in its own sessions: drop this session's identity map so the
    # terminal row below is read from the database rather than from stale memory.
    db.expire_all()
    job_row = db.get(Job, job_id)
    from app.agent_bridge import tools as bridge_tools
    read = chain.call(tools, "jobs.read", {"job_id": job_id}, db, ctx)
    chain.check("C12 the real worker drove the job into a terminal state",
                job_row.status in {"succeeded", "failed", "cancelled", "partial_failed"}
                and processed["steps"] >= 1,
                {"job_status": job_row.status, "worker_steps": processed["steps"],
                 "drained_types": processed["types"]})
    chain.check("C12 the terminal state is the worker's, not the caller's claim",
                (read.get("data") or {}).get("status") == job_row.status
                and job_row.status == "succeeded"
                and job_row.errors == [] and job_row.finished_at is not None
                and any(ref.get("type") == "resource" for ref in (read.get("data") or {}).get("result_refs") or []),
                # `finished_at`/`result_refs` are written by the worker's own `_finish`; the
                # acceptance response can never produce them. `started_at` is deliberately not
                # asserted: on SQLite it stays NULL here (the same reason
                # apps/api/tests/test_worker_api.py drops it for the shared test DB).
                {"read_status": (read.get("data") or {}).get("status"), "job_status": job_row.status,
                 "errors": job_row.errors, "result_refs": (read.get("data") or {}).get("result_refs"),
                 "finished_at": str(job_row.finished_at)})
    _ = bridge_tools  # the tool module is the real execution surface that was called

    # Resolve the local course through the platform source reference bound by the real
    # import path, not by guessing a title.
    course_id = next((ref.owner_id for ref in db.query(SourceRef).filter(
        SourceRef.owner_type == "course", SourceRef.platform == "xzzd",
        SourceRef.external_id == PAGE1[0]["id"]).all()), None)
    materials = chain.call(tools, "materials.list", {"course_id": course_id}, db, ctx)
    listed = (materials.get("data") or {}).get("items") or []
    chain.check("C12 material metadata lists the synced handout and admits the local scope",
                any(item.get("title") == MATERIAL_TITLE for item in listed)
                and materials.get("coverage", {}).get("complete") is False,
                {"items": [{k: item.get(k) for k in ("id", "title", "version", "parse_status")} for item in listed],
                 "coverage": materials.get("coverage")})
    resource = next((item for item in listed if item.get("title") == MATERIAL_TITLE), None)
    chain.check("C12 metadata alone is not treated as body text",
                bool(resource) and "text" not in resource
                and not isinstance(resource.get("text"), str),
                {"resource_keys": sorted(resource.keys()) if resource else None})

    body = chain.call(tools, "materials.read",
                      {"resource_id": resource["id"], "expected_version": resource["version"]}, db, ctx)
    chunks = (body.get("data") or {}).get("items") or []
    text = "\n".join(chunk.get("text", "") for chunk in chunks)
    chain.check("C12 the worker really fetched the attachment bytes from the platform",
                "zc-file-2001" in school.fetch_calls,
                {"fetch_calls": school.fetch_calls, "sync_calls": school.sync_calls})
    chain.check("C12 the material body is the parsed attachment text, not the metadata",
                body.get("status") == "ok" and "limits" in text and "2x" in text,
                {"parse_status": (body.get("data") or {}).get("resource", {}).get("parse_status"),
                 "chunk_count": len(chunks), "text_head": text[:200]})
    chain.check("C12 the read envelope keeps evidence/coverage for the answer",
                bool(body.get("evidence")) and (body.get("coverage") or {}).get("scope", "").startswith("material:"),
                {"evidence": body.get("evidence"), "coverage": body.get("coverage")})

    assignments = chain.call(tools, "assignments.list", {"course_id": course_id}, db, ctx)
    rows = (assignments.get("data") or {}).get("items") or []
    unknown = next((row for row in rows if row.get("title") == "期中项目选题"), None)
    known = next((row for row in rows if row.get("title") == "第三章习题"), None)
    chain.check("C12 an unknown deadline stays unknown with no invented date",
                bool(unknown) and unknown.get("due_at") is None
                and unknown.get("due_precision") == "unknown"
                and any("未知" in str(w) for w in assignments.get("warnings") or []),
                {"unknown": unknown, "warnings": assignments.get("warnings")})
    chain.check("C12 a known deadline keeps its platform precision",
                bool(known) and known.get("due_at", "").startswith("2026-10-25")
                and known.get("due_precision") == "minute",
                {"known": known})

    chain.data["answer"] = {
        "question": "高等数学（甲）有哪些资料和作业？截止时间是什么？",
        "course_id": course_id, "job_id": job_id, "job_terminal_status": job_row.status,
        "material": {"resource_id": resource["id"] if resource else None, "title": MATERIAL_TITLE,
                     "version": resource["version"] if resource else None,
                     "body_excerpt": _clip(text, 200),
                     "body_source": "worker-synced attachment parsed by learning.parse (pypdf)"},
        "assignments": [{"title": row.get("title"), "due_at": row.get("due_at"),
                         "due_precision": row.get("due_precision"),
                         "submission_status": row.get("submission_status")} for row in rows],
        "coverage": assignments.get("coverage"),
        "unknown_deadline_reported_as_unknown": bool(unknown) and unknown.get("due_at") is None,
    }


def _drain_worker_until(job_id: str, db) -> dict:
    """Run the real in-process worker until the target job settles and the queue drains.

    Mirrors ``apps/api/tests/test_worker_api.py``: the worker is invoked in-process on
    the real handlers (``app.worker.process_job``), so the sync job *and* the follow-up
    parse job it enqueues are executed by product code, not by this script. Draining the
    whole queue (not just the target) is required: the material body only becomes
    readable once the worker's own parse job has run.
    """
    from sqlalchemy import func, select

    from app import models as m
    from app import worker
    from app.db import SessionLocal

    types: list[str] = []
    steps = 0
    target_settled = False
    for _ in range(40):
        with SessionLocal() as session:
            row = session.get(m.Job, job_id)
            status = row.status if row is not None else "missing"
            pending = session.execute(select(func.count()).select_from(m.Job)
                                      .where(m.Job.status.in_(("queued", "running")))).scalar() or 0
        target_settled = target_settled or status not in {"queued", "running"}
        if target_settled and not pending:
            break
        with SessionLocal() as session:
            queued = session.execute(
                select(m.Job).where(m.Job.status.in_(("queued", "running")))
                .order_by(m.Job.created_at)).scalars().first()
            if queued is None:
                break
            types.append(queued.type)
            worker.process_job(session, queued)
        steps += 1
    return {"steps": steps, "types": types}


# ─────────────────────────── chain C / C13: timetable -> task -> preview ──────
CALENDAR_SOURCE_URL = "https://www.zju.edu.cn/_s597/2026/0914/calendar-notice.html"
PERIOD_TIMES = [["08:00", "08:45"], ["08:50", "09:35"], ["10:00", "10:45"], ["10:50", "11:35"],
                ["11:40", "12:25"], ["13:25", "14:10"], ["14:15", "15:00"]]
CLASS_WEEKDAY = 5  # Friday
CLASS_PERIODS = "1-2"
CLASS_WINDOW = ("08:00", "09:35")
FREE_WINDOW = ("09:35", "12:00")


def _seed_campus_cache(term_key: str, day: date, stamp: str) -> None:
    from app.campus_workspace import OWNER, get_campus_workspace
    from connectors.campus.campus_endpoints import TIMETABLE

    year, semester = term_key.split("/")
    monday = day - timedelta(days=day.weekday())
    row = {"name": "合成课-数据结构", "code": "SYN-CS", "source": TIMETABLE, "fetched_at": stamp,
           "stale": False, "weekday": str(CLASS_WEEKDAY), "periods": CLASS_PERIODS, "weeks": "1-8",
           "season": "秋", "year": year, "semester": semester, "term": f"{year}-{int(year) + 1} · 秋"}
    calendar = {"verified": True, "stale": False, "fetched_at": stamp,
                "seasons": [{"name": "秋", "start": str(monday), "end": str(monday + timedelta(days=55))}],
                "period_times": PERIOD_TIMES, "holidays": [], "exchanges": {}, "uncertain_dates": [],
                "valid_until": str(monday + timedelta(days=55)),
                "sources": [{"url": CALENDAR_SOURCE_URL, "sha256": "b" * 64}],
                "scope": "主校区本科（合成替换）"}
    result = {"source": "courses", "platform": "zdbk", "records": [row], "freshness": "fresh",
              "completeness": "complete", "fetched_at": stamp, "warnings": []}
    workspace = get_campus_workspace()
    workspace.manager.repository.write(OWNER, {"calendars": {term_key: calendar}})
    scope = json.dumps({"year": year, "semester": semester}, sort_keys=True, ensure_ascii=False)
    workspace.manager.capability_repository.write(OWNER, "zdbk", "courses", scope, result)
    workspace.manager.capability_repository.write(OWNER, "zdbk", "courses", "{}", result)


def _clear_campus_cache() -> None:
    from app.campus_workspace import OWNER, get_campus_workspace

    workspace = get_campus_workspace()
    workspace.manager.repository.write(OWNER, {"calendars": {}})
    workspace.manager.capability_repository.write(OWNER, "zdbk", "courses", "{}", {})
    for scope in ("1|秋", "2|春"):
        workspace.manager.capability_repository.write(
            OWNER, "zdbk", "courses",
            json.dumps({"year": "2026", "semester": scope}, sort_keys=True, ensure_ascii=False), {})


def chain_plan(chain: Chain, tools, db, ctx_factory) -> None:
    """C13: cached timetable/calendar precision -> local task -> plan preview."""
    from sqlalchemy import select

    from app import models as m
    from app.agent_bridge.tools import ToolContext
    from app.campus_workspace import OWNER, get_campus_workspace

    today = datetime.now(timezone(timedelta(hours=8))).date()
    monday = today - timedelta(days=today.weekday())
    term_key = "2026/1|秋"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    course = m.Course(id="crs_synthetic_cs", title="数据结构基础", term="2026秋", teacher="李老师")
    db.merge(course)
    db.add(m.SourceRef(owner_type=m.OWNER_COURSE, owner_id=course.id, platform="xzzd",
                       external_id="zc-2002", observed_at=m.utc_now()))
    task = m.LearningTask(id="task_synthetic_limits", course_id=course.id, type="reading",
                          title="复习极限与连续", estimated_minutes=120, due_at=None,
                          due_precision="unknown", status="pending")
    db.merge(task)
    db.merge(m.Availability(id=1, timezone="Asia/Shanghai", weekly_windows=[
        {"weekday": CLASS_WEEKDAY, "start": "08:00", "end": "12:00"}], date_exceptions=[],
        daily_limit_minutes=None))
    db.commit()
    # Planning needs the conversation to hold the complete local workspace scope: the
    # scheduled subjects are every non-archived course, exactly as the real gateway
    # grants them for a fresh conversation.
    workspace_courses = frozenset(db.scalars(
        select(m.Course.id).where(m.Course.archived_at.is_(None))))
    ctx = ctx_factory(course_ids=workspace_courses, allow_uncoursed_tasks=True)
    _seed_campus_cache(term_key, today, stamp)

    academics = chain.call(tools, "campus.academics", {"day": str(monday + timedelta(days=CLASS_WEEKDAY - 1))},
                           db, ctx)
    agenda = (academics.get("data") or {}).get("agenda") or {}
    events = agenda.get("events") or []
    chain.check("C13 the real academic view dates the cached timetable rows",
                bool(events) and any(event.get("name") == "合成课-数据结构" for event in events),
                {"term_key": agenda.get("term_key"), "event_count": len(events),
                 "events": [{k: event.get(k) for k in ("name", "date", "start", "end", "weeks", "week")}
                            for event in events]})
    chain.check("C13 calendar provenance and precision survive into the tool answer",
                agenda.get("dated") is True
                and (agenda.get("calendar") or {}).get("sources", [{}])[0].get("sha256") == "b" * 64
                and (agenda.get("calendar") or {}).get("period_times") == PERIOD_TIMES,
                {"dated": agenda.get("dated"), "source_status": agenda.get("source_status"),
                 "calendar_sources": (agenda.get("calendar") or {}).get("sources"),
                 "period_times": (agenda.get("calendar") or {}).get("period_times")})
    class_day = agenda.get("day")

    tasks = chain.call(tools, "tasks.list", {"course_id": course.id}, db, ctx)
    task_rows = (tasks.get("data") or {}).get("items") or []
    chain.check("C13 the local learning task is real and read through the domain tool",
                any(row.get("id") == task.id for row in task_rows)
                and any("不代表学校作业提交状态" in str(w) for w in tasks.get("warnings") or []),
                {"tasks": [{k: row.get(k) for k in ("id", "title", "estimated_minutes", "status")}
                           for row in task_rows], "warnings": tasks.get("warnings")})

    plans_before = _plan_snapshot(db)
    preview = chain.call(tools, "plans.preview", {"period": "this_week"}, db, ctx)
    plans_after = _plan_snapshot(db)
    # `plans.preview` returns `data.action` = plan_actions.view(action): a pending
    # action whose `preview` payload is the candidate plan (never an applied one).
    payload = _preview_action(preview)
    plan = _preview_plan(payload)
    blocks = plan.get("blocks") or []
    occupied = _interval_utc(class_day, *CLASS_WINDOW)
    if DEBUG:  # pragma: no cover - diagnostic only
        print(f"[debug] action={payload.get('id')} plan={plan.get('id')} status={plan.get('status')} "
              f"blocks={len(blocks)} warnings={[w.get('code') for w in plan.get('warnings') or []]}",
              flush=True)
    chain.check("C13 the preview avoids the real class block",
                bool(blocks) and all(not (interval[0] < occupied[1] and interval[1] > occupied[0])
                                     for interval in (_block_interval(b) for b in blocks)),
                {"class_block": [value.isoformat() for value in occupied],
                 "blocks": [_clip(block) for block in blocks]})
    candidate = chain.call(tools, "plans.read", {"plan_id": plan.get("id")}, db, ctx)
    active = chain.call(tools, "plans.read", {}, db, ctx)
    chain.check("C13 the preview writes no formal plan (candidate only, no active arrangement)",
                plans_before == [row for row in plans_after if row["id"] in {p["id"] for p in plans_before}]
                and not any(row["status"] == "active" for row in plans_after)
                and len(plans_after) - len(plans_before) <= 1
                and (candidate.get("data") or {}).get("status") == "candidate"
                and (active.get("data") or {}).get("plan") is None,
                {"plans_before": plans_before, "plans_after": plans_after,
                 "candidate_status": (candidate.get("data") or {}).get("status"),
                 "active_plan": (active.get("data") or {}).get("plan")})
    chain.check("C13 the preview reports it is not yet effective",
                payload.get("status") == "pending"
                and any("预览" in str(w) or "接受" in str(w) for w in preview.get("warnings") or []),
                {"action_status": payload.get("status"), "warnings": preview.get("warnings")})
    free_used = [(_block_interval(block), block) for block in blocks]
    chain.check("C13 the task is placed in the verified free window, not in class time",
                bool(free_used) and all(start >= occupied[1] or end <= occupied[0] for (start, end), _ in free_used),
                {"free_window": FREE_WINDOW, "placed": [_clip(block) for _, block in free_used]})

    # Scenario 2: no timetable and no calendar evidence at all -> honest uncertainty.
    _clear_campus_cache()
    blind = chain.call(tools, "plans.preview", {"period": "this_week"}, db,
                       ctx_factory(course_ids=workspace_courses, allow_uncoursed_tasks=True,
                                   request_key="turn:m2-chain-c-blind"))
    blind_plan = _preview_plan(_preview_action(blind))
    reasons = {}
    for warning in blind_plan.get("warnings") or []:
        if warning.get("code") == "CAMPUS_TIMETABLE_UNCERTAIN":
            reasons = (warning.get("details") or {}).get("reasons") or {}
    chain.check("C13 without timetable/date evidence the preview reports honest uncertainty",
                bool(reasons) and any(key in reasons for key in
                                      ("no_timetable", "cache_unavailable", "unreliable_timetable",
                                       "unverified_or_stale_calendar")),
                {"reasons": reasons, "warnings": _clip(blind_plan.get("warnings"))})
    chain.check("C13 no free slot is presented as verified when the evidence is missing",
                "uncertain" in json.dumps(blind_plan.get("warnings") or [], ensure_ascii=False)
                or bool(reasons),
                {"warning_codes": [w.get("code") for w in blind_plan.get("warnings") or []],
                 "message": _clip([w.get("message") for w in blind_plan.get("warnings") or []])})
    final_plans = _plan_snapshot(db)
    chain.check("C13 the blind preview also writes no formal plan",
                not any(row["status"] == "active" for row in final_plans)
                and [row for row in final_plans if row["id"] in {p["id"] for p in plans_after}] == plans_after
                and len(final_plans) - len(plans_after) <= 1,
                {"plans_after_first_preview": plans_after, "final_plans": final_plans})

    chain.data["answer"] = {
        "question": "结合课表和校历，这周什么时候可以安排复习极限与连续？",
        "agenda_day": class_day, "class_block": {"date": class_day, "start": CLASS_WINDOW[0],
                                                 "end": CLASS_WINDOW[1], "source": "zdbk timetable + verified calendar"},
        "calendar_source_sha256": "b" * 64, "period_times": PERIOD_TIMES,
        "placed_blocks": [{"task_id": block.get("task_id"), "starts_at": block.get("starts_at"),
                           "ends_at": block.get("ends_at")} for block in blocks],
        "preview_effective": False, "formal_plans_written": 0,
        "candidate_plan_id": plan.get("id"),
        "active_plan_after_preview": (active.get("data") or {}).get("plan"),
        "blind_scenario": {"uncertainty_reasons": reasons,
                           "coverage_claim": "无课表/校历证据时只报告不确定性，不生成虚构空闲时段"},
    }


def _preview_action(result: dict) -> dict:
    """The pending plan action a preview returned (``data.action`` of plans.preview)."""
    data = result.get("data") or {}
    action = data.get("action") if isinstance(data, dict) else None
    return action if isinstance(action, dict) else {}


def _preview_plan(action: dict) -> dict:
    """The candidate plan contract inside a preview action.

    ``plan_actions.view`` wraps the action as ``{"preview": {"plan": {...}, "diff": ...}}``,
    so the plan lives one level below ``preview``.
    """
    view = action.get("preview")
    if not isinstance(view, dict):
        return {}
    plan = view.get("plan")
    return plan if isinstance(plan, dict) else {}


def _block_interval(block: dict):
    return (datetime.fromisoformat(block["starts_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(block["ends_at"].replace("Z", "+00:00")))


def _interval_utc(day: str, start: str, end: str):
    tz = timezone(timedelta(hours=8))
    return (datetime.fromisoformat(f"{day}T{start}:00").replace(tzinfo=tz).astimezone(timezone.utc),
            datetime.fromisoformat(f"{day}T{end}:00").replace(tzinfo=tz).astimezone(timezone.utc))


def _plan_snapshot(db) -> list[dict]:
    from sqlalchemy import select

    from app import models as m
    rows = list(db.execute(select(m.Plan).order_by(m.Plan.id)).scalars())
    return [{"id": row.id, "version": row.version, "status": row.status} for row in rows]


# ─────────────────────────── live probes (off by default) ─────────────────────
PUBLIC_LIVE_URL = "https://example.org/"


def public_live_check(timeout: float) -> dict:
    """One credentialed-free fetch of a fixed public page; low-level detail only."""
    import ssl
    import urllib.request
    from urllib.parse import urlsplit

    item = {"url": PUBLIC_LIVE_URL, "credentials": False, "ok": False}
    host = urlsplit(PUBLIC_LIVE_URL).hostname
    started = time.monotonic()
    try:
        item["dns"] = sorted({row[4][0] for row in socket.getaddrinfo(host, 443)})
    except OSError as exc:
        item.update(error=type(exc).__name__, stage="dns")
        return item
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            item["tcp_connect"] = "ok"
            with context.wrap_socket(raw, server_hostname=host) as tls:
                item["tls"] = {"version": tls.version(), "cipher": (tls.cipher() or [None])[0],
                               "verified": True}
                request = ("GET / HTTP/1.1\r\nHost: " + host +
                           "\r\nUser-Agent: CampusAgent-ReadonlyAudit/1.0\r\nAccept: */*\r\n"
                           "Connection: close\r\n\r\n")
                tls.sendall(request.encode("ascii"))
                raw_bytes = b""
                while len(raw_bytes) < 200_000:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    raw_bytes += chunk
    except (OSError, ssl.SSLError) as exc:
        item.update(error=type(exc).__name__, stage="connect")
        return item
    head, _, body = raw_bytes.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").splitlines()
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    import hashlib
    item.update(status=status, content_type=headers.get("content-type", "").split(";")[0],
                redirect=None, content_length=len(body),
                body_sha256=hashlib.sha256(body).hexdigest(),
                body_excerpt=body[:400].decode("utf-8", "replace"),
                tls_verified=True, ok=status == 200 and bool(body),
                elapsed_ms=round((time.monotonic() - started) * 1000))
    return item


def _probe_credentials_exist() -> bool:
    """Whether a real stored campus credential exists, without creating or writing anything."""
    from app import config
    from app.secrets_store import _path_for  # type: ignore[attr-defined]  # real store layout

    try:
        return Path(_path_for("campus-passport")).exists()
    except Exception:
        return config.SECRETS_DIR.exists() and any(config.SECRETS_DIR.glob("*passport*"))


def campus_live_check(timeout: float) -> dict:
    """One minimal read-only campus query, only with a stored credential or live session.

    Never prints, logs or stores cookies, tickets, passwords or signed URLs; the
    isolated secret directory is used, so the real credential vault is not written.
    """
    from app.campus_workspace import get_campus_workspace, OWNER
    from app import campus_credentials

    workspace = get_campus_workspace()
    item = {"probe": "CampusWorkspace.query('courses', refresh=True)", "credentials_printed": False,
            "vault_written": False, "status": "blocked"}
    try:
        status = workspace.status()
    except Exception as exc:
        item.update(blocker=f"workspace status unavailable: {type(exc).__name__}")
        return item
    stored = campus_credentials.status("passport")
    has_stored_credential = stored.get("configured") or _probe_credentials_exist()
    connector = next((entry for entry in status.get("connectors", [])
                      if entry.get("platform_id") == "zdbk"), {})
    authorized = bool(status.get("browser_session_authorized")) or bool(connector.get("connected"))
    if not (has_stored_credential or authorized):
        item["blocker"] = ("no stored campus credential and no authorized live session available to this "
                           "offline process; add the credential in the trusted settings UI, then rerun")
        item["credential_status"] = stored
        item["browser_session_authorized"] = bool(status.get("browser_session_authorized"))
        return item
    result = workspace.query("courses", {}, None, refresh=True)
    error = result.get("error") or {}
    item.update(status="ok" if not error else "blocked",
                result={"platform": result.get("platform"), "freshness": result.get("freshness"),
                        "completeness": result.get("completeness"), "record_count": len(result.get("records") or []),
                        "error_code": error.get("code")},
                blocker=None if not error else f"campus read-only query returned {error.get('code')}")
    return item


# ─────────────────────────── repo facts ───────────────────────────────────────
def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_document(chains: list[dict], live_flags: dict, started_at: str, duration: float,
                   live: dict, notes_extra: list[str], platform_adapter_calls: list[dict]) -> dict:
    summary = {}
    for chain in chains:
        checks = chain["checks"]
        summary[chain["id"]] = {"passed": sum(1 for c in checks if c["ok"]),
                                "failed": sum(1 for c in checks if not c["ok"]),
                                "blocked": 0, "status": chain["status"]}
    return {
        "run_id": RUN_ID,
        "script": "tools/verify_campus_chains.py",
        "started_at": started_at,
        "duration_seconds": round(duration, 3),
        "python": sys.version.split()[0],
        "repository": str(ROOT),
        "repo_head": _git("rev-parse", "HEAD"),
        "dirty_files": [line for line in _git("status", "--porcelain").splitlines() if line.strip()],
        "live_flags": live_flags,
        "non_loopback_connections": non_loopback_connections(),
        "isolation": {"workspace": str(WORK), **ISOLATION,
                      "protected_paths_never_touched": [str(path) for path in FORBIDDEN]},
        "chains": chains,
        "summary": summary,
        "live": live,
        "notes": {
            "external_boundaries_replaced": REPLACED_BOUNDARIES,
            "external_boundaries_not_exercised": NOT_EXERCISED,
            "platform_adapter_calls": _clip(platform_adapter_calls, 200),
            "synthetic_page_requests": list(PAGE_REQUESTS),
            "tool_level_correlation": (
                "Each chain records ordered tool calls with parameter summary, returned status, "
                "evidence/coverage/warnings and the final answer payload; the tool names, argument "
                "objects and results are the real ones returned by app.agent_bridge.tools.execute."),
            "determinism": (
                "All fixtures are derived from the run date and fixed identifiers; no clock-dependent "
                "branch changes a verdict. Verdicts are stable for repeated runs on the same day."),
            **({"extra": notes_extra} if notes_extra else {}),
        },
    }


# ─────────────────────────── main ─────────────────────────────────────────────
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chains", default="a,b,c",
                        help="comma separated chain ids to run (default a,b,c = all three)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="evidence document path")
    parser.add_argument("--public-live", action="store_true",
                        help="also fetch ONE real public page (no campus credentials); default off")
    parser.add_argument("--campus-live", action="store_true",
                        help="also perform ONE minimal real read-only campus query if authorized; default off")
    parser.add_argument("--timeout", type=float, default=20.0, help="per live probe timeout in seconds")
    args = parser.parse_args(argv)

    selected = [item.strip().lower() for item in args.chains.split(",") if item.strip()]
    unknown = [item for item in selected if item not in {"a", "b", "c"}]
    if unknown or not selected:
        parser.error(f"unknown chain ids: {unknown or args.chains!r}")

    started = datetime.now(timezone.utc)
    started_at = started.isoformat(timespec="seconds")
    begin = time.monotonic()

    from sqlalchemy import select

    from app import models as m
    from app.agent_bridge import tools
    from app.agent_bridge.tools import ToolContext
    from app.campus_workspace import get_campus_workspace
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(engine)

    # Replace ONLY the external boundaries, before any chain runs.
    from app import connectors_registry, sync_engine
    from connectors.campus import campus_network
    from connectors.campus.connectors import notices as notices_adapter

    school = SyntheticSchool()
    campus_network.PublicPageTransport.get = lambda self, url: _synthetic_get(url)
    campus_network.build_opener = lambda *a, **k: _SyntheticOpener()
    calls: list[dict] = []

    def protocol_adapter(platform, connection_id):
        calls.append({"platform": platform, "connection_id": connection_id})
        return school

    connectors_registry.protocol_adapter = protocol_adapter
    # The worker resolves sync adapters through sync_engine, so the same synthetic
    # school must be registered there too; discovery and sync then share one fake
    # transport while every other layer stays real.
    school_factory = lambda platform, connection_id: school  # noqa: E731
    school_factory.protocol_factory = school_factory
    sync_engine.register_sync_adapter("xzzd", sync_engine._bridge_protocol_adapter(school_factory))
    notices_adapter.build_opener = lambda *a, **k: _SyntheticOpener()

    def ctx_factory(**overrides):
        values = {"principal": "local", "conversation_id": "conv_m2_chains",
                  "course_ids": frozenset(), "connection_ids": frozenset(), "job_ids": frozenset(),
                  "allow_uncoursed_tasks": False, "request_key": "turn:m2-chain-c",
                  "files": None}
        values.update(overrides)
        return ToolContext(**values)

    chain_results: list[dict] = []
    notes_extra: list[str] = []
    db = SessionLocal()
    try:
        if "a" in selected:
            chain = Chain("a", "校园通知：发现 → 正文 → 回答（C11）", ["C11"])
            try:
                chain_notices(chain, tools, db, ctx_factory(course_ids=None,
                                                            connection_ids=frozenset(),
                                                            request_key="turn:m2-chain-a"))
            except Exception:
                chain.check("chain A completed without an internal error", False, traceback.format_exc()[-1200:])
            chain_results.append(chain.finalize())
        if "b" in selected:
            chain = Chain("b", "课程资料/作业：平台发现 → 分页挂接 → 同步 job → 读取（C12）", ["C12"])
            try:
                chain_courses(chain, tools, db, ctx_factory(
                    course_ids=None, connection_ids=frozenset({"conn_synthetic_xzzd"}),
                    request_key="turn:m2-chain-b"), school)
            except Exception:
                chain.check("chain B completed without an internal error", False, traceback.format_exc()[-1200:])
            chain_results.append(chain.finalize())
        if "c" in selected:
            chain = Chain("c", "课表/校历 → 学习任务 → 计划预览（C13）", ["C13"])
            try:
                chain_plan(chain, tools, db, ctx_factory)
            except Exception:
                chain.check("chain C completed without an internal error", False, traceback.format_exc()[-1200:])
            chain_results.append(chain.finalize())
    finally:
        db.close()
        try:
            get_campus_workspace().close()
        except Exception:
            pass

    live = {}
    if args.public_live:
        live["public_live"] = public_live_check(args.timeout)
    else:
        live["public_live"] = {"status": "not_run",
                               "reason": "--public-live not passed (default fully offline)"}
    if args.campus_live:
        live["campus_live"] = campus_live_check(args.timeout)
    else:
        live["campus_live"] = {"status": "not_run",
                               "reason": "--campus-live not passed (default fully offline)"}
    if live.get("public_live", {}).get("status") == "blocked":
        live["public_live"]["status"] = "blocked"

    live_flags = {"public_live": bool(args.public_live), "campus_live": bool(args.campus_live)}
    connections = non_loopback_connections()
    if connections and not (args.public_live or args.campus_live):
        message = ("offline mode opened non-loopback socket connections: "
                   + json.dumps(connections, ensure_ascii=False))
        notes_extra.append(message)
        for chain in chain_results:
            chain["checks"].append({"name": "zero non-loopback sockets without a live flag",
                                    "ok": False, "detail": connections})
            chain["status"] = "failed"

    document = build_document(chain_results, live_flags, started_at, time.monotonic() - begin,
                              live, notes_extra, calls)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = sum(item["failed"] for item in document["summary"].values())
    for chain in document["chains"]:
        print(f"chain {chain['id']}: {chain['status']} "
              f"({document['summary'][chain['id']]['passed']} passed / "
              f"{document['summary'][chain['id']]['failed']} failed)")
        for check in chain["checks"]:
            print(f"  [{'ok' if check['ok'] else 'FAIL'}] {check['name']}")
    print(f"non_loopback_connections={len(document['non_loopback_connections'])} "
          f"live_flags={json.dumps(live_flags)}")
    print("evidence: " + str(out_path))
    cleaned = _cleanup()
    print("isolated workspace removed: " + str(cleaned))
    if failed:
        print(f"RESULT: FAILED ({failed} failing checks)", flush=True)
        return 1
    print("RESULT: PASSED (all chain checks ok)", flush=True)
    return 0


def _cleanup() -> bool:
    """Remove only this run's isolated workspace; the evidence document is kept."""
    if WORK.parent != ROOT / "var" or not WORK.name.startswith("m2-campus-chains-"):
        return False
    try:
        from app.db import engine
        engine.dispose()  # release the SQLite handle so Windows can delete the tree
    except Exception:
        pass
    shutil.rmtree(WORK, ignore_errors=True)
    return not WORK.exists()


if __name__ == "__main__":
    raise SystemExit(main())
