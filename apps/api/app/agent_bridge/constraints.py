"""Deterministic one-preview availability overrides; never write long-term preferences."""
from __future__ import annotations

from copy import deepcopy
import re
from datetime import date, datetime, time, timedelta

from .. import models as m
from ..errors import ApiError


def _invalid(message):
    raise ApiError(400, "INVALID_INPUT", message)


def _point(text):
    if not isinstance(text, str):
        _invalid("临时约束时间必须是带时区的 ISO 时间")
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _invalid("临时约束时间格式无效")
    if value.tzinfo is None or value.second or value.microsecond:
        _invalid("临时约束须给出明确时区，并精确到分钟")
    return value


def _minute(text):
    if not isinstance(text, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        _invalid("可用时间窗口仅接受 HH:MM")
    try:
        value = time.fromisoformat(text)
    except (ValueError, TypeError):
        _invalid("可用时间窗口格式无效")
    if value.second or value.microsecond or value.tzinfo is not None:
        _invalid("可用时间窗口仅接受 HH:MM")
    return value.hour * 60 + value.minute


def _hm(minutes):
    return f"{minutes // 60:02}:{minutes % 60:02}"


def temporary_availability(row: m.Availability, start: date, end: date, tz,
                           blocked: list[dict], windows: list[dict] | None, locked_blocks: list) -> m.Availability:
    if not isinstance(blocked, list) or len(blocked) > 64 or (end - start).days > 30:
        _invalid("临时安排最多覆盖 31 天、64 个不可用时间段")
    weekly = deepcopy(row.weekly_windows or [])
    if windows is not None:
        if not isinstance(windows, list) or len(windows) > 56:
            _invalid("临时每周可用时间最多 56 项")
        weekly = deepcopy(windows)
        for w in weekly:
            if not isinstance(w, dict) or set(w) != {"weekday", "start", "end"}:
                _invalid("临时可用时间窗口字段无效")
            if type(w["weekday"]) is not int or not 1 <= w["weekday"] <= 7 or _minute(w["start"]) >= _minute(w["end"]):
                _invalid("临时可用时间的星期或起止无效")
    exceptions = {e["date"]: deepcopy(e) for e in row.date_exceptions or []}
    lower, upper = datetime.combine(start, time.min, tz), datetime.combine(end + timedelta(days=1), time.min, tz)
    unavailable = []
    for item in blocked:
        if not isinstance(item, dict) or set(item) != {"starts_at", "ends_at"}:
            _invalid("临时不可用时间字段无效")
        first, last = _point(item["starts_at"]).astimezone(tz), _point(item["ends_at"]).astimezone(tz)
        if first >= last or first < lower or last > upper:
            _invalid("临时不可用时间须位于安排范围内，且终点晚于起点")
        for block in locked_blocks:
            locked_start, locked_end = _point(block.starts_at), _point(block.ends_at)
            if first < locked_end and last > locked_start:
                raise ApiError(409, "LOCKED_CONSTRAINT_CONFLICT", "本次不可用时间与锁定安排冲突，请先决定是否解锁该安排")
        unavailable.append((first, last))
    current = start
    while current <= end:
        midnight = datetime.combine(current, time.min, tz)
        tomorrow = midnight + timedelta(days=1)
        cuts = [(max(first, midnight), min(last, tomorrow)) for first, last in unavailable if first < tomorrow and last > midnight]
        if cuts:
            key = current.isoformat()
            original = exceptions[key]["windows"] if key in exceptions else [w for w in weekly if w["weekday"] == current.isoweekday()]
            spans = [(_minute(w["start"]), _minute(w["end"])) for w in original]
            for first, last in sorted(cuts):
                a = int((first - midnight).total_seconds() / 60)
                b = int((last - midnight).total_seconds() / 60)
                remaining = []
                for left, right in spans:
                    if b <= left or a >= right:
                        remaining.append((left, right))
                    else:
                        if left < a:
                            remaining.append((left, a))
                        if b < right:
                            remaining.append((b, right))
                spans = remaining
            exceptions[key] = {"date": key, "windows": [{"start": _hm(left), "end": _hm(right)} for left, right in spans], "note": "仅本次安排的不可用时间"}
        current += timedelta(days=1)
    return m.Availability(id=1, timezone=row.timezone, weekly_windows=weekly,
                          date_exceptions=[exceptions[key] for key in sorted(exceptions)], daily_limit_minutes=row.daily_limit_minutes)
