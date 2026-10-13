"""ICS 导出（B19）：生效计划时间块 + 任务截止 → RFC 5545 iCalendar。

口径（收官轮任务书 B19 / D17 约束）：

- 时间与时区：库内一律 UTC（SQLite 取回为 naive，按 UTC 解释）；导出按用户时区
  （Availability.timezone）渲染——定时事件 DTSTART/DTEND 带 TZID + 本地墙钟，
  并附最小 VTIMEZONE。无夏令时的时区（如 Asia/Shanghai）定义精确；有夏令时的
  时区仅保证各事件时刻换算正确，VTIMEZONE 定义为近似（见交付材料已知限制）。
- 全天与精确区分：计划块恒为定时事件；任务截止按 due_precision 区分——
  datetime → 定时（TZID）；date → 全天（VALUE=DATE，日期按用户时区判定）；
  unknown → 由调用方不导出（本模块不臆断精度）。
- 跨日不截断：块/截止按真实时刻换算，跨午夜即如实跨午夜，不做日期错位归并。
- 序列化：CRLF 行尾；TEXT 值按 RFC 5545 转义（\\ ; , 换行）；按 75 个八位组
  折行且不切断多字节字符；DTSTAMP/UID 稳定。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

CRLF = "\r\n"
_FOLD_OCTETS = 75

# PRODID 固定值：产物来源标识（客户端按此聚合日历）
PRODID = "-//ZJU Study Workbench//Study Plan//CN"
CALNAME = "学习计划"


@dataclass
class Event:
    """一个 VEVENT。

    - 定时事件：starts_at（+可选 ends_at）按用户在用户时区渲染，带 TZID；
    - 全天事件：all_day=True 时仅用 starts_at 在用户时区的“日期”部分
      （DTSTART;VALUE=DATE，DTEND 为次日，RFC 5545 全天区间为半开）。
    """

    uid: str
    summary: str
    starts_at: datetime
    ends_at: datetime | None = None
    all_day: bool = False


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """80 列折行：首行与续行均为最多 75 个八位组（续行前导空格计入宽度）。"""
    encoded = line.encode("utf-8")
    if len(encoded) <= _FOLD_OCTETS:
        return line
    chunks: list[bytes] = []
    current = b""
    for ch in line:
        piece = ch.encode("utf-8")
        if len(current) + len(piece) > _FOLD_OCTETS:
            chunks.append(current)
            current = b" " + piece
        else:
            current += piece
    chunks.append(current)
    return CRLF.join(chunk.decode("utf-8") for chunk in chunks)


def _fmt_utc(value: datetime) -> str:
    return _as_utc(value).strftime("%Y%m%dT%H%M%SZ")


def _fmt_local(value: datetime, tz: tzinfo) -> str:
    return _as_utc(value).astimezone(tz).strftime("%Y%m%dT%H%M%S")


def _fmt_date(value: datetime, tz: tzinfo) -> str:
    return _as_utc(value).astimezone(tz).strftime("%Y%m%d")


def _offset_token(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def _vtimezone(tz_name: str, offsets: set[timedelta]) -> list[str]:
    lines = ["BEGIN:VTIMEZONE", f"TZID:{tz_name}"]
    for offset in sorted(offsets, key=lambda o: o.total_seconds()):
        token = _offset_token(offset)
        lines.extend(
            [
                "BEGIN:STANDARD",
                "DTSTART:19700101T000000",
                f"TZOFFSETFROM:{token}",
                f"TZOFFSETTO:{token}",
                "END:STANDARD",
            ]
        )
    lines.append("END:VTIMEZONE")
    return lines


def _event_lines(event: Event, *, tz_name: str, tz: tzinfo, stamp: str) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{event.uid}", f"DTSTAMP:{stamp}"]
    if event.all_day:
        start_date = _fmt_date(event.starts_at, tz)
        next_day = datetime.strptime(start_date, "%Y%m%d") + timedelta(days=1)
        lines.append(f"DTSTART;VALUE=DATE:{start_date}")
        lines.append(f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART;TZID={tz_name}:{_fmt_local(event.starts_at, tz)}")
        if event.ends_at is not None:
            lines.append(f"DTEND;TZID={tz_name}:{_fmt_local(event.ends_at, tz)}")
    lines.append(f"SUMMARY:{_text(event.summary)}")
    lines.append("END:VEVENT")
    return lines


def build_ics(*, events: list[Event], tz_name: str, tz: tzinfo, exported_at: datetime) -> str:
    """事件序列 → 完整 iCalendar 文本（CRLF 行尾；无事件时也是合法空日历）。"""
    stamp = _fmt_utc(exported_at)
    offsets: set[timedelta] = set()
    for event in events:
        if event.all_day:
            continue
        offsets.add(_as_utc(event.starts_at).astimezone(tz).utcoffset() or timedelta(0))
        if event.ends_at is not None:
            offsets.add(_as_utc(event.ends_at).astimezone(tz).utcoffset() or timedelta(0))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-TIMEZONE:{tz_name}",
        f"X-WR-CALNAME:{CALNAME}",
    ]
    if offsets:
        lines.extend(_vtimezone(tz_name, offsets))
    for event in events:
        lines.extend(_event_lines(event, tz_name=tz_name, tz=tz, stamp=stamp))
    lines.append("END:VCALENDAR")
    return CRLF.join(_fold(line) for line in lines) + CRLF
