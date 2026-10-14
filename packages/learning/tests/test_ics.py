"""D17 · ICS 导出测试：时间与时区准确、全天/精确区分、跨日与每日上限不矛盾、格式规范。

覆盖（约束测试优先）：
1. 日历结构 / CRLF / PRODID / 确定性；
2. 单一时区偏移 → TZID + 固定偏移 VTIMEZONE 本地时间渲染（含 DST 时区单侧场景）；
3. 偏移不唯一（跨夏令时切换）→ 整体 UTC 回退，瞬时逐秒准确（含跨越切换点的块）；
4. 全天截止（VALUE=DATE，DTEND 次日，日期取导出时区）与精确时刻截止区分；unknown 不生成；
5. 已完成/已取消/平台已提交任务不生成截止事件；
6. 跨午夜块 DTEND 落在次日；ICS 账本 = split_by_date 口径 ≤ 每日上限（约束一致性）；
7. TEXT 转义 / 75 字节折行（UTF-8 安全）/ 确定性输出 / UID 稳定。
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from learning.models import LearningTask, Plan, PlanBlock
from learning.samples import run_sample
from learning.schedule.ics import DEFAULT_UID_DOMAIN, plan_to_ics
from learning.schedule.validate import validate_plan
from learning.timeutil import resolve_tz, split_by_date

# ── 迷你 ICS 解析器（解折行 + 解属性/参数） ─────────────────────────────────


def parse_ics(text: str) -> tuple[list[str], list[str], list[dict]]:
    """返回 (物理行, 逻辑行, 事件属性表)；事件属性形如 name -> (params, value)。"""
    assert text.endswith("\r\n"), "ICS 必须以 CRLF 结尾"
    assert not re.search(r"(?<!\r)\n", text), "不允许裸 LF"
    physical = text.split("\r\n")[:-1]
    logical: list[str] = []
    for line in physical:
        assert "\r" not in line, "物理行不得含裸 CR"
        if line.startswith(" "):
            assert logical, "续行不得出现在首行"
            logical[-1] += line[1:]
        else:
            logical.append(line)
    events: list[dict] = []
    current: dict | None = None
    for line in logical:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None:
            head, _, value = line.partition(":")
            name, _, params = head.partition(";")
            current[name] = (params, value)
    return physical, logical, events


def block_event_map(events: list[dict], plan: Plan) -> dict[str, dict]:
    """块 UID → 事件；断言计划中每块恰有一个事件。"""
    by_uid = {event["UID"][1]: event for event in events}
    mapping = {}
    for block in plan.blocks:
        uid = f"{block.id}@{DEFAULT_UID_DOMAIN}"
        assert uid in by_uid, f"时间块 {block.id} 缺少 ICS 事件"
        mapping[block.id] = by_uid[uid]
    return mapping


def dt_of(event: dict, name: str, tz) -> _dt.datetime:
    """解析 DTSTART/DTEND 为 aware datetime（TZID 参数 / Z 后缀 / VALUE=DATE）。"""
    params, value = event[name]
    assert "VALUE=DATE" not in params
    if value.endswith("Z"):
        return _dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)
    tzid = None
    for part in params.split(";"):
        if part.startswith("TZID="):
            tzid = part.split("=", 1)[1]
    assert tzid, f"{name} 既无 Z 后缀也无 TZID 参数：{params!r}"
    return _dt.datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=resolve_tz(tzid))


def unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            nxt = value[index]
            out.append("\n" if nxt in ("n", "N") else nxt)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def make_task(task_id: str, title: str, **over) -> LearningTask:
    base = dict(
        id=task_id,
        course_id="crs_demo",
        type="reading",
        title=title,
        estimated_minutes=60,
    )
    base.update(over)
    return LearningTask(**base)


def make_block(block_id: str, task_id: str, start: str, end: str, locked: bool = False) -> PlanBlock:
    return PlanBlock(id=block_id, task_id=task_id, starts_at=start, ends_at=end, locked=locked)


def make_plan(blocks: list[PlanBlock], horizon_start="2026-10-12T00:00+08:00") -> Plan:
    return Plan(
        horizon_start=horizon_start,
        horizon_end="2026-10-26T00:00+08:00",
        input_revision=1,
        blocks=blocks,
    )


# ── 1. 结构与规范 ────────────────────────────────────────────────────────────


def test_calendar_structure_and_header():
    plan = make_plan([make_block("blk_a", "t1", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")])
    text = plan_to_ics(plan, [make_task("t1", "任务一")])
    physical, logical, events = parse_ics(text)
    assert [line for line in logical if line in ("BEGIN:VCALENDAR", "END:VCALENDAR")]
    assert logical[0] == "BEGIN:VCALENDAR" and logical[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in logical
    assert "CALSCALE:GREGORIAN" in logical and "METHOD:PUBLISH" in logical
    assert any(line.startswith("PRODID:-//ZJU Study Workbench") for line in logical)
    assert any(line.startswith("X-WR-CALNAME:") for line in logical)
    assert len(events) == 1
    assert all(len(line.encode("utf-8")) <= 75 for line in physical)


def test_block_event_content_and_tzid_rendering():
    plan = make_plan([make_block("blk_a", "t1", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")])
    tasks = [make_task("t1", "高等数学 第一章", completion_criteria="读完并做笔记")]
    logical_text = plan_to_ics(plan, tasks)
    _, logical, events = parse_ics(logical_text)

    assert "X-WR-TIMEZONE:Asia/Shanghai" in logical
    assert "BEGIN:VTIMEZONE" in logical and "TZID:Asia/Shanghai" in logical
    assert "DTSTART:19700101T000000" in logical
    assert "TZOFFSETFROM:+0800" in logical and "TZOFFSETTO:+0800" in logical

    event = events[0]
    assert event["UID"][1] == f"blk_a@{DEFAULT_UID_DOMAIN}"
    assert event["DTSTAMP"][1].endswith("Z")
    params, value = event["DTSTART"]
    assert params == "TZID=Asia/Shanghai" and value == "20261012T190000"
    params, value = event["DTEND"]
    assert params == "TZID=Asia/Shanghai" and value == "20261012T200000"
    assert event["SUMMARY"][1] == "高等数学 第一章"
    description = unescape(event["DESCRIPTION"][1])
    assert "类型：阅读" in description
    assert "预计用时：60 分钟" in description
    assert "完成标准：读完并做笔记" in description


# ── 2/3. 时区渲染与夏令时回退 ────────────────────────────────────────────────


def test_utc_fallback_when_offsets_differ_across_dst():
    """America/New_York 跨 2026-11-01 切换（EDT→EST）：偏移不唯一 → 整体 UTC，瞬时准确。"""
    tasks = [make_task("t1", "夏令时两侧")]
    plan = make_plan(
        [
            make_block("blk_edt", "t1", "2026-10-31T19:00-04:00", "2026-10-31T20:00-04:00"),
            make_block("blk_est", "t1", "2026-11-02T19:00-05:00", "2026-11-02T20:00-05:00"),
        ]
    )
    _, logical, events = parse_ics(plan_to_ics(plan, tasks, timezone="America/New_York"))
    assert "BEGIN:VTIMEZONE" not in logical
    assert not any("TZID=" in line and not line.startswith("X-WR-CALNAME") for line in logical)
    by_uid = {event["UID"][1].split("@")[0]: event for event in events}
    assert by_uid["blk_edt"]["DTSTART"][1] == "20261031T230000Z"  # 19:00 EDT = 23:00Z
    assert by_uid["blk_edt"]["DTEND"][1] == "20261101T000000Z"
    assert by_uid["blk_est"]["DTSTART"][1] == "20261103T000000Z"  # 19:00 EST = 次日 00:00Z
    assert by_uid["blk_est"]["DTEND"][1] == "20261103T010000Z"


def test_single_offset_dst_zone_uses_tzid():
    """同一 DST 时区内事件全在切换单侧（偏移唯一）→ 仍用 TZID 本地时间渲染。"""
    plan = make_plan([make_block("blk_a", "t1", "2026-11-02T19:00-05:00", "2026-11-02T20:00-05:00")])
    _, logical, events = parse_ics(plan_to_ics(plan, [make_task("t1", "冬令时单侧")], timezone="America/New_York"))
    assert "TZID:America/New_York" in logical
    assert "TZOFFSETTO:-0500" in logical
    assert events[0]["DTSTART"] == ("TZID=America/New_York", "20261102T190000")


def test_utc_shaped_inputs_render_with_matching_vtimezone_offset():
    """B 侧契约形状的时间为 UTC（Z）：偏移集合须按渲染时区口径取，
    VTIMEZONE 必须与本地时间一致（回归：曾按输入自身 tzinfo 取值生成 +0000 错位）。"""
    plan = make_plan([make_block("blk_a", "t1", "2026-10-12T11:00:00Z", "2026-10-12T12:30:00Z")])
    text = plan_to_ics(plan, [make_task("t1", "UTC 输入")], timezone="Asia/Shanghai")
    _, logical, events = parse_ics(text)
    assert "TZOFFSETFROM:+0800" in logical and "TZOFFSETTO:+0800" in logical
    params, value = events[0]["DTSTART"]
    assert params == "TZID=Asia/Shanghai" and value == "20261012T190000"  # 11:00Z = 19:00+08:00
    # 一致性硬校验：本地时间按 VTIMEZONE 声明偏移换算 → 与输入瞬时一致
    rendered = dt_of(events[0], "DTSTART", resolve_tz("Asia/Shanghai"))
    assert rendered == _dt.datetime(2026, 10, 12, 11, 0, tzinfo=_dt.timezone.utc)

    # UTC 输入且目标时区跨 DST 两侧 → 回退 UTC（偏移不唯一同样以渲染口径判定）
    plan_dst = make_plan(
        [
            make_block("blk_edt", "t1", "2026-10-31T23:00:00Z", "2026-11-01T00:00:00Z"),
            make_block("blk_est", "t1", "2026-11-03T00:00:00Z", "2026-11-03T01:00:00Z"),
        ]
    )
    _, logical_dst, events_dst = parse_ics(plan_to_ics(plan_dst, [make_task("t1", "UTC 跨 DST")], timezone="America/New_York"))
    assert "BEGIN:VTIMEZONE" not in logical_dst
    assert events_dst[0]["DTSTART"][1] == "20261031T230000Z"
    assert events_dst[1]["DTSTART"][1] == "20261103T000000Z"


def test_block_crossing_dst_transition_instant_exact():
    """块恰好跨越切换点（墙钟回拨 01:30→01:30）：回退 UTC，起止瞬时逐秒准确。"""
    plan = make_plan([make_block("blk_x", "t1", "2026-11-01T01:30-04:00", "2026-11-01T01:30-05:00")])
    _, _, events = parse_ics(plan_to_ics(plan, [make_task("t1", "跨越切换")], timezone="America/New_York"))
    assert events[0]["DTSTART"][1] == "20261101T053000Z"
    assert events[0]["DTEND"][1] == "20261101T063000Z"
    # 回读换算：UTC 瞬时在纽约分别是 01:30(EDT) / 01:30(EST)，同一墙钟、跨切换
    ny = resolve_tz("America/New_York")
    start = dt_of(events[0], "DTSTART", ny)
    end = dt_of(events[0], "DTEND", ny)
    assert (start.astimezone(ny).strftime("%H:%M"), end.astimezone(ny).strftime("%H:%M")) == ("01:30", "01:30")
    assert (end - start) == _dt.timedelta(minutes=60)


# ── 4. 全天与精确时刻截止 ────────────────────────────────────────────────────


def test_all_day_vs_timed_deadline_distinction():
    tasks = [
        make_task("t_date", "全天截止", due_at="2026-10-16T00:00+08:00", due_precision="date"),
        make_task("t_time", "精确截止", due_at="2026-10-12T15:59+08:00", due_precision="datetime"),
        make_task("t_unknown", "未知精度", due_at="2026-10-20T00:00+08:00", due_precision="unknown"),
        make_task("t_none", "无截止"),
    ]
    plan = make_plan([])
    _, _, events = parse_ics(plan_to_ics(plan, tasks))
    by_uid = {event["UID"][1]: event for event in events}

    assert set(by_uid) == {
        f"deadline:t_date@{DEFAULT_UID_DOMAIN}",
        f"deadline:t_time@{DEFAULT_UID_DOMAIN}",
    }
    all_day = by_uid[f"deadline:t_date@{DEFAULT_UID_DOMAIN}"]
    assert all_day["DTSTART"] == ("VALUE=DATE", "20261016")
    assert all_day["DTEND"] == ("VALUE=DATE", "20261017")  # RFC：DTEND 为次日（开区间）
    assert all_day["SUMMARY"][1] == "截止：全天截止"

    timed = by_uid[f"deadline:t_time@{DEFAULT_UID_DOMAIN}"]
    params, value = timed["DTSTART"]
    assert params == "TZID=Asia/Shanghai" and value == "20261012T155900"
    assert "DTEND" not in timed  # 精确时刻事件为瞬时事件

    # include_deadline_events=False：不生成任何截止事件
    _, _, without = parse_ics(plan_to_ics(plan, tasks, include_deadline_events=False))
    assert without == []


def test_all_day_deadline_date_uses_export_timezone():
    """全天日期取 due_at 在导出时区下的日期：北京 10-16 00:00 = 纽约 10-15 12:00。"""
    tasks = [make_task("t_date", "跨时区全天截止", due_at="2026-10-16T00:00+08:00", due_precision="date")]
    plan = make_plan([])
    _, _, events = parse_ics(plan_to_ics(plan, tasks, timezone="America/New_York"))
    assert events[0]["DTSTART"] == ("VALUE=DATE", "20261015")
    assert events[0]["DTEND"] == ("VALUE=DATE", "20261016")

    _, _, events_cn = parse_ics(plan_to_ics(plan, tasks))
    assert events_cn[0]["DTSTART"] == ("VALUE=DATE", "20261016")


def test_deadline_events_skip_inactive_and_submitted_tasks():
    tasks = [
        make_task("t_done", "已完成", due_at="2026-10-15T00:00+08:00", due_precision="date", status="completed"),
        make_task("t_cancel", "已取消", due_at="2026-10-15T00:00+08:00", due_precision="date", status="cancelled"),
        make_task(
            "t_submitted",
            "平台已提交",
            due_at="2026-10-15T00:00+08:00",
            due_precision="date",
            platform_submitted=True,
        ),
        make_task("t_active", "进行中", due_at="2026-10-15T23:59+08:00", due_precision="datetime"),
    ]
    _, _, events = parse_ics(plan_to_ics(make_plan([]), tasks))
    assert {event["UID"][1] for event in events} == {f"deadline:t_active@{DEFAULT_UID_DOMAIN}"}


# ── 5. 跨午夜与每日上限的一致性 ──────────────────────────────────────────────


def test_cross_midnight_blocks_render_with_next_day_dtend():
    result = run_sample("scenarios/cross_midnight")
    text = plan_to_ics(result["plan"], result["tasks"], timezone=result["inputs"]["availability"].timezone)
    _, logical, events = parse_ics(text)
    assert "BEGIN:VTIMEZONE" in logical  # 单一 +08:00 偏移
    by_uid = block_event_map(events, result["plan"])
    assert by_uid["blk:t_midnight:0"]["DTEND"][1] == "20261012T225000"
    assert by_uid["blk:t_midnight:1"]["DTEND"][1] == "20261012T234000"
    # 第三块跨午夜：DTEND 落在次日
    assert by_uid["blk:t_midnight:2"]["DTSTART"][1] == "20261012T234000"
    assert by_uid["blk:t_midnight:2"]["DTEND"][1] == "20261013T003000"


@pytest.mark.parametrize("name", ["scenarios/cross_midnight", "scenarios/tight_daily_limit"])
def test_ics_accounting_matches_engine_and_daily_limit(name):
    """约束一致性：ICS 中每块事件换算回账准时区后，逐日分钟数 = split_by_date 口径 ≤ 每日上限。"""
    result = run_sample(name)
    plan, tasks = result["plan"], result["tasks"]
    availability = result["inputs"]["availability"]
    tz = resolve_tz(availability.timezone)

    # 前提：计划本身满足全部硬约束（否则本测试的"不矛盾"没有意义）
    assert validate_plan(plan, tasks, availability, result["inputs"]["schedule_options"]) == []

    _, _, events = parse_ics(plan_to_ics(plan, tasks, timezone=availability.timezone))
    by_uid = block_event_map(events, plan)
    assert len(events) == len(plan.blocks)  # 样例任务无截止，仅块事件

    engine_daily: dict[_dt.date, int] = {}
    for block in plan.blocks:
        start, end = dt_of(by_uid[block.id], "DTSTART", tz), dt_of(by_uid[block.id], "DTEND", tz)
        for day, minutes in split_by_date(start, end, tz):
            engine_daily[day] = engine_daily.get(day, 0) + minutes

    # 与引擎块直接核算的口径一致
    expected_daily: dict[_dt.date, int] = {}
    for block in plan.blocks:
        for day, minutes in split_by_date(
            _dt.datetime.fromisoformat(block.starts_at), _dt.datetime.fromisoformat(block.ends_at), tz
        ):
            expected_daily[day] = expected_daily.get(day, 0) + minutes
    assert engine_daily == expected_daily

    if availability.daily_limit_minutes is not None:
        assert engine_daily and all(minutes <= availability.daily_limit_minutes for minutes in engine_daily.values())

    # 每任务分钟总数守恒（跨午夜拆分不丢失）
    ics_totals: dict[str, int] = {}
    for block in plan.blocks:
        event = by_uid[block.id]
        minutes = int((dt_of(event, "DTEND", tz) - dt_of(event, "DTSTART", tz)).total_seconds() // 60)
        ics_totals[block.task_id] = ics_totals.get(block.task_id, 0) + minutes
    by_task: dict[str, int] = {}
    for block in plan.blocks:
        by_task[block.task_id] = by_task.get(block.task_id, 0) + int(
            (
                _dt.datetime.fromisoformat(block.ends_at) - _dt.datetime.fromisoformat(block.starts_at)
            ).total_seconds()
            // 60
        )
    assert ics_totals == by_task
    for task in tasks:
        if task.id in by_task:
            assert by_task[task.id] <= task.estimated_minutes


# ── 6. 确定性 / UID / 转义与折行 ─────────────────────────────────────────────


def test_deterministic_output_and_stable_uids():
    plan = make_plan([make_block("blk_a", "t1", "2026-10-12T19:00+08:00", "2026-10-12T19:45+08:00")])
    tasks = [make_task("t1", "任务一", due_at="2026-10-14T23:59+08:00", due_precision="datetime")]
    first = plan_to_ics(plan, tasks)
    assert first == plan_to_ics(plan, tasks)  # 逐字节确定性

    # DTSTAMP 缺省 = horizon_start（ISO 带偏移，UTC 化）
    _, logical, _ = parse_ics(first)
    assert "DTSTAMP:20261011T160000Z" in logical

    # B 侧契约风格：horizon_start 为纯日期 → 导出时区当日 00:00
    b_style = plan_to_ics(make_plan(plan.blocks, horizon_start="2026-10-12"), tasks)
    _, logical_b, _ = parse_ics(b_style)
    assert "DTSTAMP:20261011T160000Z" in logical_b

    # 显式 dtstamp 只影响 DTSTAMP 行；UID 集合稳定
    stamped = plan_to_ics(plan, tasks, dtstamp="2026-10-10T12:00:00+08:00")
    assert stamped == first.replace("20261011T160000Z", "20261010T040000Z")
    _, _, events_a = parse_ics(first)
    _, _, events_b = parse_ics(stamped)
    assert {e["UID"][1] for e in events_a} == {e["UID"][1] for e in events_b}
    assert {e["UID"][1] for e in events_a} == {
        f"blk_a@{DEFAULT_UID_DOMAIN}",
        f"deadline:t1@{DEFAULT_UID_DOMAIN}",
    }


def test_text_escaping_and_utf8_safe_folding():
    title = "第1讲, 导论; 含\\反斜杠 " + "很长的标题内容" * 12 + "\n第二行"
    plan = make_plan([make_block("blk_a", "t1", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00")])
    text = plan_to_ics(plan, [make_task("t1", title)])
    physical, logical, events = parse_ics(text)

    assert all(len(line.encode("utf-8")) <= 75 for line in physical)
    summary_line = next(line for line in logical if line.startswith("SUMMARY:"))
    escaped = summary_line[len("SUMMARY:") :]
    assert "\\," in escaped and "\\;" in escaped and "\\\\" in escaped and "\\n" in escaped
    assert unescape(escaped) == title  # 折行 + 转义可逆
    assert events[0]["SUMMARY"][1] == escaped


def test_empty_plan_yields_valid_empty_calendar():
    text = plan_to_ics(make_plan([]), [])
    _, logical, events = parse_ics(text)
    assert events == []
    assert logical[0] == "BEGIN:VCALENDAR" and logical[-1] == "END:VCALENDAR"
    assert "BEGIN:VTIMEZONE" not in logical
    assert text == plan_to_ics(make_plan([]), [])


def test_unparseable_block_raises_value_error():
    plan = make_plan([make_block("blk_bad", "t1", "not-a-time", "also-bad")])
    with pytest.raises(ValueError, match="blk_bad"):
        plan_to_ics(plan, [make_task("t1", "任务一")])
