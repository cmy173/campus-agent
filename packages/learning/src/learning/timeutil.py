"""时间工具：ISO 8601（带偏移量）解析、按本地日期拆分、可用窗口展开。

约定（对齐《协作总纲》4.1）：
- 对外时间一律为带偏移量的 ISO 8601 字符串；内部使用 aware datetime，比较按绝对时刻。
- 学习时长为整数分钟；窗口为本地时区的 HH:MM；weekday 使用 0=周一 … 6=周日（Python 惯例）。
- end <= start 的窗口视为跨午夜到次日。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .models import Availability

TZ_DEFAULT = "Asia/Shanghai"
DAY = _dt.timedelta(days=1)


def resolve_tz(name: str) -> _dt.tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        if name == TZ_DEFAULT:
            # Windows 精简环境可能没有 tzdata 包；中国自 1991 年起无夏令时，固定 +08:00 与 ZoneInfo 等价。
            return _dt.timezone(_dt.timedelta(hours=8), name=TZ_DEFAULT)
        raise ValueError(f"未知或不可用的时区：{name!r}") from None


def parse_dt(value: str) -> _dt.datetime:
    dt = _dt.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须包含偏移量（如 +08:00），收到：{value!r}")
    return dt


def fmt_dt(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="minutes")


def minutes_between(start: _dt.datetime, end: _dt.datetime) -> int:
    return int((end - start).total_seconds() // 60)


def split_by_date(
    start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo
) -> list[tuple[_dt.date, int]]:
    """把区间按本地日期拆分为 [(日期, 分钟数)]，用于每日上限核算。"""
    out: list[tuple[_dt.date, int]] = []
    cur = start.astimezone(tz)
    stop = end.astimezone(tz)
    while cur < stop:
        next_midnight = _dt.datetime.combine(cur.date() + DAY, _dt.time.min, tzinfo=tz)
        seg_end = min(stop, next_midnight)
        mins = minutes_between(cur, seg_end)
        if mins > 0:
            out.append((cur.date(), mins))
        cur = seg_end
    return out


@dataclass(frozen=True)
class DayWindows:
    date: _dt.date
    gaps: tuple[tuple[_dt.datetime, _dt.datetime], ...]
    daily_limit_minutes: int | None


def _combine(date: _dt.date, hhmm: str, tz: _dt.tzinfo) -> _dt.datetime:
    parts = hhmm.split(":")
    return _dt.datetime(date.year, date.month, date.day, int(parts[0]), int(parts[1]), tzinfo=tz)


def expand_availability(
    availability: "Availability",
    horizon_start: _dt.datetime,
    horizon_end: _dt.datetime,
) -> list[DayWindows]:
    """把周窗口 + 日期例外展开为日级可用窗口（已处理跨午夜与计划范围裁剪）。"""
    tz = resolve_tz(availability.timezone)
    hs = horizon_start.astimezone(tz)
    he = horizon_end.astimezone(tz)
    exceptions = {e.date: e for e in availability.date_exceptions}
    days: list[DayWindows] = []
    day = hs.date()
    last = he.date()
    if he == _dt.datetime.combine(last, _dt.time.min, tzinfo=tz):
        last -= DAY  # 半开区间 [start, end)：end 恰为午夜时不含该日
    while day <= last:
        exc = exceptions.get(day.isoformat())
        raw: list[tuple[_dt.datetime, _dt.datetime]] = []
        if exc is None or not exc.unavailable:
            if exc is not None and exc.windows is not None:
                spans = [(w.start, w.end) for w in exc.windows]
            else:
                spans = [
                    (w.start, w.end)
                    for w in availability.weekly_windows
                    if w.weekday == day.weekday()
                ]
            for start_s, end_s in spans:
                s = _combine(day, start_s, tz)
                e = _combine(day, end_s, tz)
                if e <= s:
                    e += DAY
                s2, e2 = max(s, hs), min(e, he)
                if s2 < e2:
                    raw.append((s2, e2))
        limit = availability.daily_limit_minutes
        if exc is not None and exc.daily_limit_minutes is not None:
            limit = exc.daily_limit_minutes
        raw.sort()
        merged: list[tuple[_dt.datetime, _dt.datetime]] = []
        for s, e in raw:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        days.append(DayWindows(date=day, gaps=tuple(merged), daily_limit_minutes=limit))
        day += DAY
    return days
