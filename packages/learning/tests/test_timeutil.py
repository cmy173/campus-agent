"""时间工具单元测试：偏移量校验、按日拆分、窗口展开（跨午夜、日期例外）。"""

import datetime as dt

import pytest

from learning.models import Availability, DateException, WeeklyWindow
from learning.timeutil import expand_availability, fmt_dt, parse_dt, resolve_tz, split_by_date

TZ = resolve_tz("Asia/Shanghai")


def test_parse_requires_offset():
    with pytest.raises(ValueError):
        parse_dt("2026-10-12T19:00")
    assert parse_dt("2026-10-12T19:00+08:00") == dt.datetime(2026, 10, 12, 19, 0, tzinfo=TZ)


def test_fmt_minutes_precision():
    value = dt.datetime(2026, 10, 12, 19, 0, tzinfo=TZ)
    assert fmt_dt(value) == "2026-10-12T19:00+08:00"


def test_split_by_date_cross_midnight():
    start = dt.datetime(2026, 10, 12, 22, 0, tzinfo=TZ)
    end = dt.datetime(2026, 10, 13, 0, 30, tzinfo=TZ)
    assert split_by_date(start, end, TZ) == [
        (dt.date(2026, 10, 12), 120),
        (dt.date(2026, 10, 13), 30),
    ]


def test_expand_cross_midnight_with_exception():
    availability = Availability(
        weekly_windows=[WeeklyWindow(weekday=0, start="22:00", end="01:00")],
        date_exceptions=[DateException(date="2026-10-13", unavailable=True)],
        daily_limit_minutes=180,
    )
    days = expand_availability(
        availability,
        parse_dt("2026-10-12T00:00+08:00"),
        parse_dt("2026-10-14T00:00+08:00"),
    )
    assert len(days) == 2
    monday = days[0]
    assert monday.gaps[0][0] == parse_dt("2026-10-12T22:00+08:00")
    assert monday.gaps[0][1] == parse_dt("2026-10-13T01:00+08:00")
    assert monday.daily_limit_minutes == 180
    tuesday = days[1]
    assert tuesday.gaps == ()


def test_expand_clips_to_horizon_and_limit_override():
    availability = Availability(
        weekly_windows=[WeeklyWindow(weekday=0, start="19:00", end="22:30")],
        date_exceptions=[DateException(date="2026-10-12", daily_limit_minutes=60)],
        daily_limit_minutes=180,
    )
    days = expand_availability(
        availability,
        parse_dt("2026-10-12T20:00+08:00"),
        parse_dt("2026-10-12T21:00+08:00"),
    )
    assert len(days) == 1
    assert days[0].gaps == ((parse_dt("2026-10-12T20:00+08:00"), parse_dt("2026-10-12T21:00+08:00")),)
    assert days[0].daily_limit_minutes == 60
