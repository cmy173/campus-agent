"""ICS（iCalendar RFC 5545）导出：计划时间块 + 任务截止 → 日历文本（纯函数、确定性输出）。

设计要点（D17，配合 B19 的 POST /exports ICS 扩展）：

- 时间与时区（绝不猜测时区规则）：
  * 所有时刻按绝对瞬时渲染。导出时区下全部出现时刻偏移一致时，采用 TZID + 固定偏移
    VTIMEZONE（本地时间可读，覆盖 Asia/Shanghai 等无夏令时场景）；
  * 出现时刻跨夏令时切换（偏移不唯一）时，整体回退为 UTC（Z 结尾）渲染，不伪造
    VTIMEZONE 规则——客户端按本地时区换算后仍为准确瞬时。
- 全天与精确时刻区分：due_precision="date" 的截止渲染为全天事件（VALUE=DATE，DTEND 为
  次日，日期取 due_at 在导出时区下的日期）；"datetime" 渲染为精确时刻事件；unknown/缺失
  不生成事件（不猜测）。
- 约束一致性：时间块逐块原样输出（跨午夜块 DTEND 落在次日），与 generate_plan /
  validate_plan 的账本一致（每日本地分钟数 = timeutil.split_by_date 口径，不超过每日上限，
  见 tests/test_ics.py 的约束测试）。
- 输出规范：CRLF 行尾；75 字节折行（UTF-8 安全，不拆多字节字符）；TEXT 转义（反斜杠/
  分号/逗号/换行）。
- 确定性：同输入逐字节一致；DTSTAMP 缺省取 plan.horizon_start（B 侧可传真实 UTC 时刻覆盖）。
- UID 稳定（重复导出不重复建日程）：块 `{block_id}@{uid_domain}`、截止 `deadline:{task_id}@{uid_domain}`。

供 B19 的调用方式（契约形状 → 领域对象 → 文本）::

    from learning import plan_to_ics
    from learning.models import LearningTask, Plan

    text = plan_to_ics(
        Plan.from_dict(plan_contract),                    # B 的 plan 契约形状
        [LearningTask.from_dict(t) for t in task_rows],   # B 的 task 契约形状
        timezone=availability_timezone,                   # 用户时区（渲染口径）
        dtstamp=iso_utc(utc_now()),                       # 可选；缺省取 horizon_start
    )
"""

from __future__ import annotations

import datetime as _dt

from ..models import LearningTask, Plan
from ..timeutil import TZ_DEFAULT, parse_dt, resolve_tz

PRODID = "-//ZJU Study Workbench//learning//CN"
DEFAULT_UID_DOMAIN = "zju-study-workbench"
DEFAULT_CALENDAR_NAME = "浙大学习工作台"

_TYPE_LABELS = {
    "reading": "阅读",
    "watching": "观看",
    "practice": "练习",
    "assignment": "作业",
    "review": "复习",
}

# 不生成截止事件的任务状态：已完成/已取消（本地进度）与平台已提交（PD-3，不再有待办语义）
_INACTIVE_STATUSES = ("completed", "cancelled")


def plan_to_ics(
    plan: Plan,
    tasks: list[LearningTask],
    *,
    timezone: str = TZ_DEFAULT,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    uid_domain: str = DEFAULT_UID_DOMAIN,
    dtstamp: str | None = None,
    include_deadline_events: bool = True,
) -> str:
    """把计划（时间块）与任务（截止时间）渲染为 ICS 文本。

    - timezone：导出/渲染时区（应为用户时区）；不改变任何瞬时，只影响本地时间与 TZID。
    - dtstamp：DTSTAMP 时刻（ISO 8601 带偏移量）；缺省取 plan.horizon_start。
    - include_deadline_events：是否包含任务截止事件（全天/精确时刻）。
    """
    tz = resolve_tz(timezone)
    stamp = _format_utc(_resolve_dtstamp(plan, dtstamp, tz))

    index: dict[str, LearningTask] = {}
    for task in tasks:
        index.setdefault(task.id, task)

    block_rows = []
    for block in plan.blocks:
        try:
            start, end = parse_dt(block.starts_at), parse_dt(block.ends_at)
        except ValueError as exc:
            raise ValueError(
                f"时间块 {block.id} 时间无法解析（{block.starts_at}~{block.ends_at}），"
                "导出 ICS 前应保证计划合法"
            ) from exc
        block_rows.append((start, end, block))
    block_rows.sort(key=lambda row: (row[0], row[2].id))

    deadline_rows: list[tuple[str, _dt.datetime, LearningTask]] = []
    if include_deadline_events:
        for task in tasks:
            if task.platform_submitted or task.status in _INACTIVE_STATUSES or not task.due_at:
                continue
            try:
                due = parse_dt(task.due_at)
            except ValueError:
                continue  # 截止时间不可解析：不生成截止事件（不猜测）
            if task.due_precision == "date":
                deadline_rows.append(("date", due, task))
            elif task.due_precision == "datetime":
                deadline_rows.append(("datetime", due, task))
        deadline_rows.sort(key=lambda row: (row[1], row[2].id))

    timed_instants = [row[0] for row in block_rows] + [row[1] for row in block_rows]
    timed_instants += [row[1] for row in deadline_rows if row[0] == "datetime"]
    # 偏移必须以“渲染到导出时区后”的口径取（输入可能带 UTC/其它偏移，如 B 侧契约的 Z 时间）；
    # 若直接用输入自身 tzinfo 的偏移，会生成与本地时间不符的 VTIMEZONE（客户端将整体错位）。
    offsets = {instant.astimezone(tz).utcoffset() for instant in timed_instants}
    use_tzid = len(offsets) == 1

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text(calendar_name)}",
    ]
    if use_tzid:
        lines.append(f"X-WR-TIMEZONE:{timezone}")
        offset_text = _format_offset(next(iter(offsets)))
        lines += [
            "BEGIN:VTIMEZONE",
            f"TZID:{timezone}",
            "BEGIN:STANDARD",
            "DTSTART:19700101T000000",
            f"TZOFFSETFROM:{offset_text}",
            f"TZOFFSETTO:{offset_text}",
            "END:STANDARD",
            "END:VTIMEZONE",
        ]

    def tz_props(start: _dt.datetime, end: _dt.datetime) -> tuple[str, str]:
        if use_tzid:
            return (
                f"DTSTART;TZID={timezone}:{_format_local(start, tz)}",
                f"DTEND;TZID={timezone}:{_format_local(end, tz)}",
            )
        return (f"DTSTART:{_format_utc(start)}", f"DTEND:{_format_utc(end)}")

    for start, end, block in block_rows:
        task = index.get(block.task_id)
        summary = task.title if task is not None else block.task_id
        if task is not None:
            description_parts = [
                f"类型：{_TYPE_LABELS.get(task.type, task.type)}",
                f"预计用时：{task.estimated_minutes} 分钟",
            ]
            if task.completion_criteria:
                description_parts.append(f"完成标准：{task.completion_criteria}")
        else:
            description_parts = [f"任务：{block.task_id}"]
        dtstart_prop, dtend_prop = tz_props(start, end)
        lines += _event_lines(
            uid=f"{block.id}@{uid_domain}",
            stamp=stamp,
            dtstart_prop=dtstart_prop,
            dtend_prop=dtend_prop,
            summary=summary,
            description="\n".join(description_parts),
        )

    for kind, due, task in deadline_rows:
        if kind == "date":
            local_date = due.astimezone(tz).date()
            dtstart_prop = f"DTSTART;VALUE=DATE:{local_date.strftime('%Y%m%d')}"
            dtend_prop = f"DTEND;VALUE=DATE:{(local_date + _dt.timedelta(days=1)).strftime('%Y%m%d')}"
            description = "平台截止日期（全天）"
        elif use_tzid:
            dtstart_prop = f"DTSTART;TZID={timezone}:{_format_local(due, tz)}"
            dtend_prop = None
            description = "平台截止时刻"
        else:
            dtstart_prop = f"DTSTART:{_format_utc(due)}"
            dtend_prop = None
            description = "平台截止时刻"
        lines += _event_lines(
            uid=f"deadline:{task.id}@{uid_domain}",
            stamp=stamp,
            dtstart_prop=dtstart_prop,
            dtend_prop=dtend_prop,
            summary=f"截止：{task.title}",
            description=description,
        )

    lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in lines:
        folded.extend(_fold_line(line))
    return "\r\n".join(folded) + "\r\n"


def _resolve_dtstamp(plan: Plan, dtstamp: str | None, tz: _dt.tzinfo) -> _dt.datetime:
    if dtstamp:
        return parse_dt(dtstamp)
    try:
        return parse_dt(plan.horizon_start)
    except ValueError:
        # B 侧计划契约的 horizon_* 为纯日期（iso_date）：取导出时区当日 00:00
        day = _dt.date.fromisoformat(plan.horizon_start)
        return _dt.datetime.combine(day, _dt.time.min, tzinfo=tz)


def _event_lines(
    *,
    uid: str,
    stamp: str,
    dtstart_prop: str,
    dtend_prop: str | None,
    summary: str,
    description: str,
) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_escape_text(uid)}",
        f"DTSTAMP:{stamp}",
        dtstart_prop,
    ]
    if dtend_prop is not None:
        lines.append(dtend_prop)
    lines.append(f"SUMMARY:{_escape_text(summary)}")
    lines.append(f"DESCRIPTION:{_escape_text(description)}")
    lines.append("END:VEVENT")
    return lines


def _format_utc(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _format_local(dt: _dt.datetime, tz: _dt.tzinfo) -> str:
    return dt.astimezone(tz).strftime("%Y%m%dT%H%M%S")


def _format_offset(offset: _dt.timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, rem = divmod(abs(total), 3600)
    minutes, seconds = divmod(rem, 60)
    base = f"{sign}{hours:02d}{minutes:02d}"
    return f"{base}{seconds:02d}" if seconds else base


def _escape_text(value: str) -> str:
    """RFC 5545 TEXT 转义：反斜杠、分号、逗号、（回车）换行。先转反斜杠再转其余。"""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold_line(line: str) -> list[str]:
    """按 RFC 5545 折行：首行 ≤75 字节，续行含前导空格 ≤75 字节，不拆多字节字符。"""
    if len(line.encode("utf-8")) <= 75:
        return [line]
    parts: list[str] = []
    current = ""
    used = 0
    for char in line:
        limit = 75 if not parts else 74
        size = len(char.encode("utf-8"))
        if used + size > limit:
            parts.append(current)
            current = ""
            used = 0
        current += char
        used += size
    parts.append(current)
    return [part if i == 0 else " " + part for i, part in enumerate(parts)]
