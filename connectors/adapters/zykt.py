#!/usr/bin/env python3
"""智云课堂（zykt）目录、场次详情与字幕适配器（基于历史观测形态；当前账号联调待确认）。

本骨架交付可离线验证的转换层，严格建立于 A1 §3.4 实测形态：
  ① epoch 秒 → ISO 8601（契约时间，UTC Z）；
  ② 转写 `list.all_content[]` {BeginSec, EndSec, Text, TransText} → transcript-sample.json 结构；
  ③ Yii2 REST 风格 XML（<response><success>…<result>…）解析；
  ④ `video_list` 按键对象 → 按键升序数组（归一）；
  ⑤ 回放 2 年有效期到期消失（目录中缺失）不判为故障、不产生删除；
     仅显式删除信号（sub_delete 置位）产生 deletion（A4 §4 严格语义）。

受控会话服务启用 detail_sync：目录之后逐场查询详情，按需读取字幕。
账号课程发现复用官网月课表、最近学习与收藏入口；PPT 笔记、字幕定时调度与批量视频下载尚未实现。
真实平台的当前登录状态、接口权限与限速仍需用户账号验证。

凭据红线：不自建登录、不接触凭据；会话由调用方注入的 transport 持有（见 transport.py）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
import hashlib
from typing import Any, Callable, Optional
from urllib.parse import urlencode
import time
import math
import re
import json
from xml.etree import ElementTree

from .base import AdapterError
from .transport import HttpTransport, is_true
from ..redact import RedactingFilter

PLATFORM = "zykt"
CONNECTOR_VERSION = "0.2.0-zykt-skeleton"
LOGIN_ENTRY_URL = "https://classroom.zju.edu.cn/"

_CATALOGUE_PATH = "/courseapi/v2/course/catalogue"
_CHECK_PATH = "/courseapi/v3/o-course/recent-learning"
_TRANSCRIPT_PATH = "/courseapi/v3/web-socket/search-trans-result"
_MONTH_PATH = "/courseapi/v2/course-live/get-my-course-month"
_RECENT_PATH = "/courseapi/v3/o-course/recent-learning"
_COLLECT_PATH = "/courseapi/v3/course-collect/lists"
_COURSE_PAGE_SIZE = 50

_logger = logging.getLogger("connectors.adapters.zykt")
if not any(isinstance(f, RedactingFilter) for f in _logger.filters):
    _logger.addFilter(RedactingFilter())


def epoch_to_iso(value: Any) -> Optional[str]:
    """epoch 秒（int 或数字字符串，实测 10 位）→ 契约时间（UTC Z）；缺失/不可解析 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat().replace("+00:00", "Z")


def normalize_video_list(video_list: Any) -> list[dict[str, Any]]:
    """`video_list` 实测为按键对象（非数组，A1 §3.4-3）→ 按键数值升序的数组。

    每项保留原始键（`key`）与字段；键不可数值化时按字符串排序置于数值键之后。
    """
    if video_list is None:
        return []
    if isinstance(video_list, list):
        return [dict(v) if isinstance(v, dict) else {"value": v} for v in video_list]
    if not isinstance(video_list, dict):
        return []

    def sort_key(kv: tuple[str, Any]) -> tuple[int, int, str]:
        try:
            return (0, int(kv[0]), "")
        except (TypeError, ValueError):
            return (1, 0, str(kv[0]))

    result = []
    for key, value in sorted(video_list.items(), key=sort_key):
        if isinstance(value, dict):
            result.append({"key": key, **value})
        else:
            result.append({"key": key, "value": value})
    return result


def parse_yii_xml(text: str) -> ElementTree.Element:
    """Yii2 REST 风格 XML（<response><success>…<result>…）解析；非法 XML 上抛 UPSTREAM_CHANGED。"""
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise AdapterError("UPSTREAM_CHANGED", f"XML 解析失败：{exc}", retryable=False) from exc


def parse_catalogue_xml(text: str) -> list[dict[str, str]]:
    """场次目录 XML → 条目字典列表（A1 §3.4-4）。success 为假时上抛。"""
    root = parse_yii_xml(text)
    success = root.find(".//success")
    if success is not None and not _xml_true(success.text):
        raise AdapterError("UPSTREAM_CHANGED", "catalogue 返回 success=false（待 A5b 采样确认形态）",
                           retryable=False)
    items: list[dict[str, str]] = []
    for item in root.iter("item"):
        items.append({child.tag: (child.text or "").strip() for child in item})
    return items


def _xml_true(text: Optional[str]) -> bool:
    return is_true(text) is True


def parse_catalogue_response(text: str) -> list[dict[str, Any]]:
    """Consume XML or JSON catalogue responses; unknown shapes are never an empty success."""
    cleaned = text.lstrip("\ufeff \t\r\n")
    if not cleaned.startswith(("{", "[")):
        return parse_catalogue_xml(cleaned)
    try:
        payload = json.loads(cleaned)
    except ValueError as exc:
        raise AdapterError("UPSTREAM_CHANGED", "智云目录 JSON 格式无效", retryable=False) from exc
    if isinstance(payload, dict):
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        codes = (payload.get("code"), payload.get("err"), result.get("code"), result.get("err"))
        if any(str(code) in ("401", "403") for code in codes):
            raise AdapterError("AUTH_EXPIRED", "智云目录要求登录，请检查平台会话", retryable=False,
                               details={"action": _login_action()})
        if is_true(payload.get("success", True)) is False or any(code is not None and str(code) not in ("0", "200") for code in codes):
            raise AdapterError("UPSTREAM_CHANGED", "智云目录返回失败，未读取到有效场次列表", retryable=False)
    node = payload
    container_keys = ("result", "data", "models", "list", "items", "catalogue")
    for _ in range(8):
        if isinstance(node, list):
            break
        if not isinstance(node, dict):
            raise AdapterError("UPSTREAM_CHANGED", "智云目录缺少场次列表", retryable=False)
        key = next((key for key in container_keys if key in node), None)
        if key is None:
            raise AdapterError("UPSTREAM_CHANGED", "智云目录缺少可识别的场次列表", retryable=False)
        node = node[key]
    if not isinstance(node, list):
        raise AdapterError("UPSTREAM_CHANGED", "智云目录列表结构异常", retryable=False)
    rows = []
    def visit(entries, depth=0):
        if depth > 8:
            raise AdapterError("UPSTREAM_CHANGED", "智云目录嵌套超过支持范围", retryable=False)
        for entry in entries:
            if not isinstance(entry, dict):
                raise AdapterError("UPSTREAM_CHANGED", "智云目录场次条目非对象", retryable=False)
            if entry.get("sub_id") is not None:
                row = dict(entry)
                row["sub_id"] = str(row["sub_id"])
                row["title"] = row.get("title") or row.get("sub_title")
                rows.append(row)
            else:
                child = next((entry[key] for key in ("children", "items", "list", "models", "data") if isinstance(entry.get(key), list)), None)
                if child is None:
                    # Preserve malformed items so normal item-level error reporting can explain them.
                    rows.append(dict(entry))
                else:
                    visit(child, depth + 1)
    visit(node)
    return rows


def map_transcript(payload: Any, *, recording_id: Optional[str] = None) -> dict[str, Any]:
    """转写载荷（search-trans-result JSON）→ transcript-sample.json 结构。

    实测来源：`list.all_content[]` 分段 {BeginSec, EndSec, Text, TransText}（中英双语，A1 §3.4-5）。
    非法分段跳过；TransText 为空时不输出 translated_text（不输出空翻译）。
    """
    if not isinstance(payload, dict):
        raise AdapterError("UPSTREAM_CHANGED", "转写响应非对象", retryable=False)
    lst = payload.get("list")
    records = lst if isinstance(lst, list) else [lst] if isinstance(lst, dict) else []
    segments_raw = []
    for record in records:
        if not isinstance(record, dict) or "all_content" not in record:
            raise AdapterError("UPSTREAM_CHANGED", "字幕记录缺少 all_content", retryable=False)
        content = record["all_content"]
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                raise AdapterError("UPSTREAM_CHANGED", "字幕 all_content JSON 无效", retryable=False) from None
        if not isinstance(content, list):
            raise AdapterError("UPSTREAM_CHANGED", "字幕 all_content 非分段数组", retryable=False)
        segments_raw.extend(content)
    if not records and not isinstance(lst, list):
        segments_raw = None
    if not isinstance(segments_raw, list):
        raise AdapterError(
            "UPSTREAM_CHANGED",
            "转写载荷缺少 list.all_content（形态待 A5b 校正）",
            retryable=False,
        )
    segments: list[dict[str, Any]] = []
    for raw in segments_raw:
        if not isinstance(raw, dict):
            continue
        begin = _number(raw.get("BeginSec"))
        end = _number(raw.get("EndSec"))
        text = raw.get("Text")
        if begin is None or end is None or not math.isfinite(begin) or not math.isfinite(end) or begin < 0 or end < begin or not isinstance(text, str):
            continue
        segment: dict[str, Any] = {
            "start_seconds": begin,
            "end_seconds": end,
            "text": text,
        }
        trans = raw.get("TransText")
        if isinstance(trans, str) and trans.strip():
            segment["translated_text"] = trans
        segments.append(segment)
    result: dict[str, Any] = {
        "language": "zh-CN",
        "provenance": "platform",
        "segments": segments,
    }
    if recording_id is not None:
        result["recording_id"] = recording_id
    return result


def _number(value: Any) -> Optional[float | int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def map_sub_detail(detail: Any) -> dict[str, Any]:
    """场次详情（get-sub-info result，A1 §3.4-3）→ 骨架归一结构（M1-c 扩展完整字段）。"""
    if not isinstance(detail, dict):
        raise AdapterError("UPSTREAM_CHANGED", "场次详情非对象", retryable=False)
    sub_id = detail.get("sub_id")
    if sub_id is None:
        raise AdapterError("UPSTREAM_CHANGED", "场次详情缺少 sub_id", retryable=False)
    duration = _number(detail.get("duration"))
    return {
        "sub_id": str(sub_id),
        "title": detail.get("sub_title"),
        "sub_type": detail.get("sub_type"),
        "sub_status": detail.get("sub_status"),
        "started_at": epoch_to_iso(detail.get("start_at")),
        "ended_at": epoch_to_iso(detail.get("end_at")),
        "duration_seconds": int(duration) if isinstance(duration, float) else duration,
        "allow_video_drag": is_true(detail.get("is_allow_video_drag")),
        "allow_speed_up": is_true(detail.get("is_allow_speed_up")),
        "video_parts": normalize_video_list(detail.get("video_list")),
    }


def _login_action() -> dict[str, Any]:
    return {
        "type": "user_login",
        "entry_url": LOGIN_ENTRY_URL,
        "message": "登录已失效，请在连接器窗口完成登录后重试",
    }


def _item_error(code: str, message: str, external_id: Optional[str] = None) -> dict[str, Any]:
    details = {"external_id": external_id} if external_id is not None else None
    return {
        "code": code,
        "message": message,
        "retryable": False,
        "details": details,
        "request_id": "req_zykt",
    }


class ZyktAdapter:
    """五操作面骨架；会话由 transport 持有，适配器只构造相对路径与查询参数。"""

    platform = PLATFORM
    connector_version = CONNECTOR_VERSION

    def __init__(
        self,
        connection_id: str,
        *,
        transport: HttpTransport,
        now: Optional[Callable[[], datetime]] = None,
        resolve_external_id: Optional[Callable[[str], Optional[str]]] = None,
        detail_sync: bool = False,
    ) -> None:
        self.connection_id = connection_id
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        # 内部课程 ID → 平台课程 ID 映射（A9 联调：B 提供映射函数；缺省视为同一值）
        self._resolve = resolve_external_id or (lambda course_id: course_id)
        self._sub_ids: set[str] = set()  # 最近一次目录同步观测到的场次（供 fetch 判定）
        self._detail_sync = detail_sync

    def _platform_course_id(self, course_id: str) -> str:
        external = self._resolve(course_id)
        if not external:
            raise AdapterError(
                "RESOURCE_NOT_FOUND",
                "课程未映射到 zykt 平台 ID（A9 联调：B 提供内部→平台映射）",
                retryable=False,
            )
        return external

    def _observed_at(self) -> str:
        dt = self._now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _request(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
        try:
            resp = self._transport.request(method, path, params=params)
        except Exception as exc:
            raise AdapterError(
                "NETWORK_ERROR", f"传输失败：{type(exc).__name__}", retryable=True,
                details={"path": path},
            ) from exc
        status = resp.status_code
        if status in (401, 403):
            raise AdapterError(
                "AUTH_EXPIRED", f"HTTP {status}（登录态失效）", retryable=False,
                details={"action": _login_action(), "http_status": status},
            )
        if status == 429:
            raise AdapterError("RATE_LIMITED", "HTTP 429（限流）", retryable=True,
                               details={"http_status": status})
        if status == 404:
            raise AdapterError("RESOURCE_NOT_FOUND", "HTTP 404", retryable=False,
                               details={"http_status": status})
        if status >= 500:
            raise AdapterError("NETWORK_ERROR", f"HTTP {status}", retryable=True,
                               details={"http_status": status})
        if not 200 <= status < 300:
            raise AdapterError("UPSTREAM_CHANGED", f"未登记状态码 HTTP {status}", retryable=False,
                               details={"http_status": status})
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(
                "AUTH_EXPIRED", "响应非 JSON（推断为登录态失效/SSO 页面）", retryable=False,
                details={"action": _login_action(), "path": path},
            ) from exc

    def _request_text(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None) -> str:
        """XML 端点：取原始文本（不做 JSON 解析）。"""
        try:
            resp = self._transport.request(method, path, params=params)
        except Exception as exc:
            raise AdapterError(
                "NETWORK_ERROR", f"传输失败：{type(exc).__name__}", retryable=True,
                details={"path": path},
            ) from exc
        status = resp.status_code
        if status in (401, 403):
            raise AdapterError(
                "AUTH_EXPIRED", f"HTTP {status}（登录态失效）", retryable=False,
                details={"action": _login_action(), "http_status": status},
            )
        if status >= 500:
            raise AdapterError("NETWORK_ERROR", f"HTTP {status}", retryable=True,
                               details={"http_status": status})
        if not 200 <= status < 300:
            raise AdapterError("UPSTREAM_CHANGED", f"未登记状态码 HTTP {status}", retryable=False,
                               details={"http_status": status})
        if resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise AdapterError("AUTH_EXPIRED", "平台返回登录页面，请在智云连接器窗口登录", retryable=False,
                               details={"action": _login_action()})
        return resp.text

    # ---------- 操作面 1：check_connection ----------

    def check_connection(self) -> dict[str, Any]:
        checked_at = self._observed_at()
        try:
            state = self._check_state()
        except AdapterError as err:
            if err.code == "AUTH_EXPIRED":
                return self._state("auth_required", checked_at, action=_login_action(), detail=err.error["message"])
            return self._state("network_error", checked_at, detail=err.error["message"])
        return self._state(state, checked_at)

    def _check_state(self) -> str:
        try:
            resp = self._transport.request("GET", _CHECK_PATH)
        except Exception as exc:
            raise AdapterError("NETWORK_ERROR", f"传输失败：{type(exc).__name__}", retryable=True) from exc
        status = resp.status_code
        if status == 200:
            try:
                payload = resp.json()
            except ValueError:
                raise AdapterError("AUTH_EXPIRED", "响应非 JSON（推断为登录态失效/SSO 页面）",
                                   retryable=False)
            if isinstance(payload, dict):
                if str(payload.get("code")) in ("401", "403"):
                    raise AdapterError("AUTH_EXPIRED", "个人学习记录接口要求登录", retryable=False)
                if is_true(payload.get("success", True)) is False:
                    raise AdapterError("UPSTREAM_CHANGED", "个人学习记录接口返回失败，不能据此确认登录失效", retryable=False)
            return "ok"
        if status in (401, 403):
            raise AdapterError("AUTH_EXPIRED", f"HTTP {status}（登录态失效）", retryable=False)
        if status == 429:
            return "blocked"
        if status >= 500:
            raise AdapterError("NETWORK_ERROR", f"HTTP {status}", retryable=True)
        return "blocked"

    def _state(
        self, state: str, checked_at: str, *, action: Optional[dict] = None, detail: Optional[str] = None
    ) -> dict[str, Any]:
        return {
            "platform": PLATFORM,
            "state": state,
            "checked_at": checked_at,
            "action": action,
            "detail": detail,
        }

    # ---------- 操作面 2：get_capabilities ----------

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "platform": PLATFORM,
            "connection_id": self.connection_id,
            "connector_version": CONNECTOR_VERSION,
            "auth": {
                "mode": "user_session",
                "sso": "zjuam-oauth2",
                "recovery_hint": "登录已失效，请在连接器窗口完成登录后重试",
            },
            "features": {
                "course_list": {"status": "supported", "note": "官网月课表、最近学习与收藏；当前账号权限需登录后确认"},
                "course_meta": {"status": "unknown", "note": "未验证"},
                "attachments_meta": {"status": "unknown", "note": "附件端点未观测"},
                "attachment_download": {"status": "unknown", "note": "未验证；首版不批量下载视频（红线 3）"},
                "assignments": {"status": "unsupported", "note": "无作业能力证据（A1 §5-6）"},
                "recordings": {"status": "supported", "note": "场次目录/详情已实测（A1 §3.4-2/3）"},
                "subtitles": {"status": "supported", "note": "转写接口与分段结构已实测（A1 §3.4-5）"},
                "playback_seek": {
                    "status": "preview_only",
                    "note": "仅跳转平台播放页；尚未实现按时间戳定位播放",
                },
                "deletion_signal": {
                    "status": "unknown",
                    "note": "目录条目含 sub_delete/sub_show 字段（A1 §3.4-4）；置位真实样本待 A5b 采集",
                },
            },
            "limits": {
                "page_size_max": None,
                "rate_limit_note": "M1 默认 0.5 rps 起（A4 §6）；实际阈值待 A5b 观察",
                "campus_network_required": "unknown",
            },
            "checked_at": self._observed_at(),
        }

    # ---------- 操作面 3：list_courses ----------

    def list_courses(self, cursor: Optional[str] = None, term: Optional[str] = None) -> dict[str, Any]:
        """读取本人的课表与学习记录，不读取课程广场、不要求用户提供课号。

        current 默认按 2 月/9 月划分时间窗口（不是学校权威学期元数据）。
        月课表只保留已经开课的条目；all 额外包含历史学习记录与收藏。
        learn_at 是观看时间，不能用于认定课程所属学期。
        """
        scope = term or "current"
        if scope not in ("current", "all"):
            raise AdapterError("VALIDATION_FAILED", "课程范围仅支持 current/all", retryable=False)
        now = self._now().astimezone(timezone(timedelta(hours=8)))
        start = now.replace(year=now.year if now.month >= 2 else now.year - 1,
                            month=9 if now.month >= 9 or now.month == 1 else 2,
                            day=1, hour=0, minute=0, second=0, microsecond=0)
        stage, value, previous = "month", start.strftime("%Y-%m"), ""
        if cursor is not None:
            try:
                parts = cursor.split(":")
                if len(parts) != 4 or parts[0] != scope:
                    raise ValueError
                _, stage, value, previous = parts
                if stage == "month":
                    dt = datetime.strptime(value, "%Y-%m").replace(tzinfo=now.tzinfo)
                    if dt < start or dt > now:
                        raise ValueError
                elif stage not in ("recent", "collect") or not value.isdigit() or not 1 <= int(value) <= 100:
                    raise ValueError
            except (ValueError, AttributeError):
                raise AdapterError("VALIDATION_FAILED", "课程分页游标无效，请重新读取", retryable=False) from None
        items, errors = [], []
        if stage == "month":
            dt = datetime.strptime(value, "%Y-%m").replace(tzinfo=now.tzinfo)
            following = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
            next_cursor = f"{scope}:month:{following:%Y-%m}:" if following <= now else f"{scope}:recent:1:"
            try:
                payload = self._request("GET", _MONTH_PATH, params={"month": f"{dt.year}-{dt.month}"})
                if isinstance(payload, dict) and str(payload.get("code")) in ("401", "403"):
                    raise AdapterError("AUTH_EXPIRED", "请在智云连接器窗口完成登录", retryable=False,
                                       details={"action": _login_action()})
                if not isinstance(payload, dict) or str(payload.get("code")) != "0" or not isinstance(payload.get("list"), list):
                    raise AdapterError("UPSTREAM_CHANGED", "智云月课表响应格式改变", retryable=False)
                raws = []
                for day in payload["list"]:
                    if not isinstance(day, dict) or not isinstance(day.get("course", []), list):
                        raise AdapterError("UPSTREAM_CHANGED", "智云月课表缺少课程数组", retryable=False)
                    for row in day.get("course", []):
                        if not isinstance(row, dict):
                            errors.append(_item_error("UPSTREAM_CHANGED", "课表课程项格式错误")); continue
                        started = self._course_start(row)
                        if started is None:
                            # 官网月课表按 day 分组，缺少场次时间时使用该日期。
                            try:
                                started = datetime.strptime(day["day"], "%Y-%m-%d").replace(tzinfo=now.tzinfo)
                            except (KeyError, ValueError, TypeError):
                                errors.append(_item_error("UPSTREAM_CHANGED", "课表课程缺少上课日期")); continue
                        if start <= started <= now:
                            raws.append(row)
            except AdapterError as exc:
                if exc.code not in ("UPSTREAM_CHANGED", "RESOURCE_NOT_FOUND"):
                    raise
                errors.append(exc.error)
                raws = []
        else:
            page = int(value)
            payload = self._request("GET", _RECENT_PATH if stage == "recent" else _COLLECT_PATH,
                                    params={"page": page, "per-page": _COURSE_PAGE_SIZE})
            if not isinstance(payload, dict):
                raise AdapterError("UPSTREAM_CHANGED", "智云课程列表读取失败", retryable=False)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AdapterError("UPSTREAM_CHANGED", "智云课程列表缺少 result", retryable=False)
            if str(result.get("err")) in ("401", "403"):
                raise AdapterError("AUTH_EXPIRED", "请在智云连接器窗口完成登录", retryable=False,
                                   details={"action": _login_action()})
            data = result.get("data")
            if is_true(payload.get("success", True)) is False or str(result.get("err")) != "200" or not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise AdapterError("UPSTREAM_CHANGED", "智云课程列表响应格式改变", retryable=False)
            raws = data["models"]
            digest = hashlib.sha256(repr(raws).encode("utf8")).hexdigest()[:16]
            meta = data.get("_meta") or data.get("pagination") or {}
            pages = meta.get("pageCount") if isinstance(meta, dict) else None
            if pages is None and isinstance(meta, dict):
                pages = meta.get("page_count")
            try:
                if pages is None and isinstance(meta, dict) and meta.get("totalCount") is not None:
                    pages = math.ceil(int(meta["totalCount"]) / _COURSE_PAGE_SIZE)
                more = int(pages) > page if pages is not None else len(raws) >= _COURSE_PAGE_SIZE
            except (ValueError, TypeError, OverflowError):
                raise AdapterError("UPSTREAM_CHANGED", "智云课程分页字段格式改变", retryable=False) from None
            if digest == previous or page >= 100:
                if more:
                    errors.append(_item_error("UPSTREAM_CHANGED", "课程列表分页重复或超过上限，已保留读取结果"))
                more = False
            next_cursor = (f"{scope}:{stage}:{page + 1}:{digest}" if more else
                           f"{scope}:collect:1:" if stage == "recent" and scope == "all" else None)
            if scope == "current":
                raws = [r for r in raws if isinstance(r, dict) and
                        (self._course_start(r) is not None and start <= self._course_start(r) <= now)]
        for raw in raws:
            if not isinstance(raw, dict):
                errors.append(_item_error("UPSTREAM_CHANGED", "课程项格式错误")); continue
            if raw.get("course_deleted_at") not in (None, "", 0, "0") or is_true(raw.get("course_publish", True)) is False:
                continue
            external = raw.get("course_id") or raw.get("course_subject_id") or (raw.get("id") if stage == "collect" else None)
            title = raw.get("course_title") or raw.get("course_name") or raw.get("title")
            if isinstance(external, bool) or external is None or not isinstance(title, str) or not title.strip():
                errors.append(_item_error("UPSTREAM_CHANGED", "课程项缺少课程标识或名称")); continue
            semester = raw.get("semester") or raw.get("term_name") or raw.get("term")
            if isinstance(semester, dict):
                semester = semester.get("name")
            observed = self._observed_at()
            teacher = raw.get("lecturer_name") or raw.get("course_lecturer_name")
            items.append({"id": str(external), "title": title.strip(),
                          "term": semester if isinstance(semester, str) else None,
                          "teacher": teacher if isinstance(teacher, str) else None,
                          "source_refs": [{"platform": PLATFORM, "external_id": str(external),
                                           "canonical_url": None, "observed_at": observed}],
                          "archived_at": None, "created_at": observed, "updated_at": observed})
        merged = {}
        for course in items:
            previous_course = merged.get(course["id"])
            if previous_course:
                for field in ("teacher", "term"):
                    if previous_course.get(field) and not course.get(field):
                        course[field] = previous_course[field]
                if previous_course.get("teacher") and course.get("teacher"):
                    names = re.split(r"[、,，;；]+", previous_course["teacher"] + "、" + course["teacher"])
                    course["teacher"] = "、".join(dict.fromkeys(name.strip() for name in names if name.strip()))
            merged[course["id"]] = course
        return {"items": list(merged.values()), "next_cursor": next_cursor, "errors": errors}

    @staticmethod
    def _course_start(raw: dict[str, Any]) -> Optional[datetime]:
        for key in ("course_begin", "start_at", "sub_start_at"):
            iso = epoch_to_iso(raw.get(key))
            if iso is not None:
                return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return None

    # ---------- 操作面 4：sync_course（目录级骨架） ----------

    def sync_course(
        self,
        course_id: str,
        since_cursor: Optional[str] = None,
        kinds: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """目录同步：每次全量对照（mode=resync），消费场次目录 XML/JSON。

        到期消失（目录中缺失）不判故障、不产生删除；仅 sub_delete 置位产生 deletion（A4 §4）。
        """
        allowed = None if kinds is None else set(kinds)
        observed_at = self._observed_at()
        upserts: dict[str, list[dict[str, Any]]] = {"resources": [], "assignments": [], "recordings": []}
        deletions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        scanned = 0

        if allowed is None or "recordings" in allowed:
            platform_id = self._platform_course_id(course_id)
            text = self._request_text("GET", _CATALOGUE_PATH, params={"course_id": platform_id, "format": "json"})
            items = parse_catalogue_response(text)
            for raw in items:
                scanned += 1
                sub_id = raw.get("sub_id")
                if not sub_id:
                    errors.append(_item_error("UPSTREAM_CHANGED", "目录条目缺少 sub_id"))
                    continue
                if is_true(raw.get("sub_delete")) is True:
                    deletions.append({
                        "kind": "recording",
                        "external_id": sub_id,
                        "reason": "explicit_delete_signal",
                        "observed_at": observed_at,
                    })
                    continue
                title = raw.get("title")
                if not title:
                    errors.append(_item_error("UPSTREAM_CHANGED", "目录条目缺少标题", sub_id))
                    continue
                self._sub_ids.add(sub_id)
                detail = {}
                if self._detail_sync:
                    try:
                        time.sleep(2.0)
                        payload = self._request("GET", "/courseapi/v3/portal-home-setting/get-sub-info",
                                                params={"course_id": platform_id, "sub_id": sub_id})
                        result = payload.get("result", payload) if isinstance(payload, dict) else None
                        if isinstance(result, dict) and isinstance(result.get("data"), dict):
                            result = result["data"]
                        detail = map_sub_detail(result)
                    except AdapterError as exc:
                        if exc.code == "AUTH_EXPIRED":
                            raise
                        errors.append(_item_error(exc.code, exc.error["message"], sub_id))
                url = f"https://classroom.zju.edu.cn/livingroom?{urlencode({'course_id': platform_id, 'sub_id': sub_id, 'tenant_code': '112'})}"
                upserts["recordings"].append({
                    "title": title,
                    "started_at": epoch_to_iso(raw.get("start_at")),
                    "duration_seconds": detail.get("duration_seconds"),
                    "canonical_url": url,
                    "transcript_status": "unknown",
                    "subtitle_languages": None,
                    "transcript_next_check_at": None,
                    "source_ref": {
                        "platform": PLATFORM,
                        "external_id": sub_id,
                        "canonical_url": url,
                        "observed_at": observed_at,
                    },
                })

        upserted = sum(len(v) for v in upserts.values())
        token = f"zykt-full:{course_id}:{observed_at}"
        return {
            "platform": PLATFORM,
            "connection_id": self.connection_id,
            "course_id": course_id,
            "mode": "resync",
            "cursor": {"previous": since_cursor, "next": token},
            "upserts": upserts,
            "deletions": deletions,
            "errors": errors,
            "stats": {
                "scanned": scanned,
                "upserted": upserted,
                "deleted": len(deletions),
                "failed": len(errors),
            },
            "observed_at": observed_at,
        }

    # ---------- 操作面 5：fetch_resource（骨架：视频不下载） ----------

    def fetch_resource(self, resource_id: str) -> dict[str, Any]:
        """骨架口径：视频首版不批量下载（红线 3）；字幕经 get_transcript 交付。

        目录未观测到的 ID → gone；已知场次 → 上抛 UPSTREAM_CHANGED（能力 unknown，不伪装可用）。
        """
        if resource_id not in self._sub_ids:
            return {"status": "gone", "handle": None, "denial": None}
        raise AdapterError(
            "UPSTREAM_CHANGED",
            "zykt 视频下载 M1 不开放（红线 3：首版不批量下载视频）；字幕用 get_transcript",
            retryable=False,
        )

    # ---------- 扩展：转写（字幕）获取，A10/D 侧输入 ----------

    def get_transcript(self, sub_id: str, *, recording_id: Optional[str] = None) -> dict[str, Any]:
        """取转写并归一为 transcript-sample.json 结构（离线单测：合成载荷）。"""
        payload = self._request(
            "GET", _TRANSCRIPT_PATH, params={"sub_id": sub_id, "format": "json"}
        )
        return map_transcript(payload, recording_id=recording_id)
