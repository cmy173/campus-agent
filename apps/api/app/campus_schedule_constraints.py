"""Offline, conservative admission of cached ZDBK timetable evidence.

Course records contain teaching weeks/periods, NOT absolute timestamps. Only a
fresh, verified same-term calendar can turn these into occupied instants. Never
use academic_defaults/academic_view (which permit provisional calendar dates),
or CampusWorkspace.query's browser-session retry wrapper.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import re

from . import models as m
from .config import ensure_repo_root_on_path
from .errors import ApiError

CACHE_MAX_AGE = timedelta(hours=6)  # Same freshness policy as CampusService.
CAMPUS_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
ROW_FIELDS = ("source", "fetched_at", "stale", "code", "name", "location", "weekday",
              "periods", "start_period", "duration", "weeks", "season", "odd_even",
              "year", "semester", "term")
CALENDAR_FIELDS = ("verified", "stale", "fetched_at", "seasons", "period_times", "holidays",
                   "exchanges", "uncertain_dates", "valid_until", "sources", "scope", "error")


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows(records) -> list[dict]:
    # Duplicate rows/order changes are not new scheduling evidence.
    rows = ({key: row[key] for key in ROW_FIELDS if key in row} for row in records if isinstance(row, dict))
    return [json.loads(text) for text in sorted({_canonical(row) for row in rows})]


def _result(result: dict) -> dict:
    return {"records": _rows(result.get("records") or []),
            "source": result.get("source"), "platform": result.get("platform"),
            "fetched_at": result.get("fetched_at"), "freshness": result.get("freshness"),
            "completeness": result.get("completeness"),
            "error_code": (result.get("error") or {}).get("code"),
            "warning_codes": sorted({str(w.get("code", "UNKNOWN")) for w in result.get("warnings") or []})}


def _point(value) -> datetime:
    # fromisoformat accepts date-only and naive values; those are NOT instants.
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
        raise ValueError("missing timestamp")
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError("missing timezone")
    return point.astimezone(timezone.utc)


def _fresh(stamp, now: datetime) -> bool:
    try:
        age = now - _point(stamp)
        return timedelta(0) <= age <= CACHE_MAX_AGE
    except (TypeError, ValueError, OverflowError):
        return False


def _date(value) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid date")
    return date.fromisoformat(value)


def _minute(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("invalid minute")
    return int(value[:2]) * 60 + int(value[3:])


def _calendar_valid(calendar: dict, now: datetime) -> bool:
    try:
        if (calendar.get("verified") is not True or calendar.get("stale") is not False
                or calendar.get("error") or not _fresh(calendar.get("fetched_at"), now)):
            return False
        seasons = calendar["seasons"]
        if not isinstance(seasons, list) or not 1 <= len(seasons) <= 4:
            return False
        ranges = []
        for season in seasons:
            first, last = _date(season["start"]), _date(season["end"])
            if season["name"] not in "秋冬春夏" or len(season["name"]) != 1 or first > last or first.weekday() != 0:
                return False
            ranges.append((first, last))
        ranges.sort()
        if any(left[1] >= right[0] for left, right in zip(ranges, ranges[1:])):
            return False
        slots = calendar["period_times"]
        if not isinstance(slots, list) or not 1 <= len(slots) <= 20:
            return False
        previous = -1
        for slot in slots:
            if not isinstance(slot, list) or len(slot) != 2:
                return False
            first, last = map(_minute, slot)
            if first >= last or first < previous:
                return False
            previous = last
        _date(calendar["valid_until"])
        for key in ("holidays", "uncertain_dates"):
            if not isinstance(calendar[key], list):
                return False
            for day in calendar[key]:
                _date(day)
        if not isinstance(calendar["exchanges"], dict):
            return False
        for actual, logical in calendar["exchanges"].items():
            _date(actual)
            _date(logical)
        sources = calendar["sources"]
        return bool(isinstance(sources, list) and sources and all(
            isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("https://")
            and re.fullmatch(r"[0-9a-f]{64}", s.get("sha256", "")) for s in sources))
    except (KeyError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CampusEvidence:
    terms: dict[str, dict]
    calendars: dict[str, dict]
    unavailable: bool = False
    # Frozen freshness decisions are shared by preview's fingerprint and expansion.
    now: datetime | None = None

    def revision_payload(self) -> dict:
        now = self.now or datetime.now(timezone.utc)
        return {"policy": "campus-hard-blocks-v1", "unavailable": self.unavailable,
                "terms": self.terms, "calendars": self.calendars,
                "term_recent": {k: _fresh(v.get("fetched_at"), now) for k, v in self.terms.items()},
                "row_recent": {k: [_fresh(r.get("fetched_at") or v.get("fetched_at"), now)
                                   for r in v["records"]] for k, v in self.terms.items()},
                "calendar_reliable": {k: _calendar_valid(v, now) for k, v in self.calendars.items()}}


def read_campus_evidence() -> CampusEvidence:
    """Read the existing query cache and calendar snapshot under one owner lock.

    The service.query used by CampusWorkspace.query(refresh=False) is cache-only and,
    since the explicit-sync boundary change, the wrapper is equally offline; call the
    service directly because this reader needs the repository/scopes view too.
    No connector_session, sync, calendar(), status() or academic_view() call.
    """
    now = datetime.now(timezone.utc)
    try:
        ensure_repo_root_on_path()
        from .campus_workspace import get_campus_workspace, OWNER
        from connectors.campus.connector_contracts import CoursesQuery, UserContext
        workspace = get_campus_workspace()
        with workspace.manager.lock(OWNER):
            snapshot = workspace.manager.repository.read(OWNER)
            calendars = {key: {field: value[field] for field in CALENDAR_FIELDS if field in value}
                         for key, value in (snapshot.get("calendars") or {}).items() if isinstance(value, dict)}
            aggregate = workspace.manager.service.query("courses", CoursesQuery(), UserContext(OWNER), platform="zdbk").model_dump(mode="json")
            known = {(r.get("year"), r.get("semester")) for r in aggregate.get("records", [])}
            section = snapshot.get("courses") or {}
            known.update((r.get("year"), r.get("semester")) for r in section.get("terms", []))
            known.add((section.get("year"), section.get("semester")))
            # Empty/withdrawn term caches still carry freshness/completeness
            # evidence. The aggregate has no rows from which to recover them.
            for scope in workspace.manager.service.repository.scopes(OWNER, "zdbk", "courses"):
                cached_query = CoursesQuery.model_validate_json(scope)
                known.add((cached_query.year, cached_query.semester))
            terms = {}
            for year, semester in sorted(known, key=lambda pair: str(pair)):
                if not year or not semester:
                    continue
                query = CoursesQuery(year=year, semester=semester)
                result = workspace.manager.service.query("courses", query, UserContext(OWNER), platform="zdbk").model_dump(mode="json")
                terms[year + "/" + semester] = _result(result)
            # Keep unscoped evidence in the revision, never infer a term for it.
            if aggregate.get("records") and any(not r.get("year") or not r.get("semester") for r in aggregate["records"]):
                terms["unscoped"] = _result(aggregate)
            return CampusEvidence(terms, calendars, now=now)
    except Exception:
        # Cache unavailable/malformed is a warning, not a planning outage or a
        # fabricated empty timetable. No exception text can leak private data.
        return CampusEvidence({}, {}, unavailable=True, now=now)


def campus_blocks(evidence: CampusEvidence, start: datetime, end: datetime) -> tuple[list[tuple[datetime, datetime]], list[dict]]:
    ensure_repo_root_on_path()
    from connectors.campus.academics import course_periods, week_matches
    from connectors.campus.academic_defaults import semester_seasons
    from connectors.campus.connector_contracts import Course
    from connectors.campus.campus_endpoints import TIMETABLE
    now = evidence.now or datetime.now(timezone.utc)
    reasons: Counter = Counter()
    intervals = set()
    if evidence.unavailable:
        reasons["cache_unavailable"] += 1
    elif not evidence.terms:
        reasons["no_timetable"] += 1
    lower, upper = start.astimezone(CAMPUS_TZ), end.astimezone(CAMPUS_TZ)
    for key, result in evidence.terms.items():
        calendar = evidence.calendars.get(key) or {}
        if (result.get("source") != "courses" or result.get("platform") != "zdbk"
                or result.get("freshness") != "fresh" or result.get("completeness") != "complete"
                or result.get("error_code") or not _fresh(result.get("fetched_at"), now)):
            reasons["unreliable_timetable"] += 1
            continue
        if not _calendar_valid(calendar, now):
            reasons["unverified_or_stale_calendar"] += 1
            continue
        if not result["records"]:
            reasons["empty_timetable"] += 1
        for raw in result["records"]:
            try:
                row = Course.model_validate(raw).model_dump()
                if row["year"] + "/" + row["semester"] != key:
                    raise ValueError("course scope mismatch")
                if row["stale"] or not _fresh(row["fetched_at"] or result.get("fetched_at"), now) or row["source"] != TIMETABLE:
                    raise ValueError("unreliable row")
                if not re.fullmatch(r"[1-7]", row["weekday"]) or not row["weeks"].strip():
                    raise ValueError("missing teaching range")
                seasons = row["season"]
                if seasons:
                    if not re.fullmatch(r"[秋冬春夏季学期\s、,，/]+", seasons):
                        raise ValueError("unknown season")
                    seasons = "".join(s for s in "秋冬春夏" if s in seasons)
                else:
                    seasons = semester_seasons(key.split("/", 1)[-1])
                if not seasons or "海宁" in row["location"]:
                    raise ValueError("unknown calendar applicability")
                periods = course_periods(row)
                if not periods or periods[1] > len(calendar["period_times"]) or week_matches(row, 1) is None:
                    raise ValueError("unknown teaching time")
                # Conflicting precise period representations are not reliable.
                if row["periods"] and row["start_period"] and row["duration"]:
                    if course_periods(dict(row, start_period="", duration="")) != periods:
                        raise ValueError("conflicting periods")
                day = lower.date()
                while datetime.combine(day, time.min, CAMPUS_TZ) < upper:
                    iso = day.isoformat()
                    if iso > calendar["valid_until"] or iso in calendar["uncertain_dates"]:
                        reasons["uncertain_calendar_date"] += 1
                        day += timedelta(days=1)
                        continue
                    logical = calendar["exchanges"].get(iso, iso)
                    season = next((s for s in calendar["seasons"] if s["start"] <= logical <= s["end"]), None)
                    if season is None and iso not in calendar["holidays"]:
                        reasons["outside_calendar_coverage"] += 1
                    if iso not in calendar["holidays"] and season and season["name"] in seasons:
                        week = (_date(logical) - _date(season["start"])).days // 7 + 1
                        if _date(logical).isoweekday() == int(row["weekday"]) and week_matches(row, week):
                            first = calendar["period_times"][periods[0]-1][0]
                            last = calendar["period_times"][periods[1]-1][1]
                            a = datetime.combine(day, time.fromisoformat(first), CAMPUS_TZ)
                            b = datetime.combine(day, time.fromisoformat(last), CAMPUS_TZ)
                            if a < end and b > start:
                                intervals.add((max(a, start).astimezone(timezone.utc), min(b, end).astimezone(timezone.utc)))
                    day += timedelta(days=1)
            except (ValueError, TypeError, KeyError):
                reasons["incomplete_or_ambiguous_row"] += 1
    warnings = []
    if reasons:
        warnings.append({"code": "CAMPUS_TIMETABLE_UNCERTAIN",
                         "message": "校园课表或校历缓存缺失、过期或未完整确认；仅可靠且可精确定位的课程占用时间，未自动访问校园，请核对未确认时段。",
                         "task_ids": [], "details": {"source": "campus_cache", "reasons": dict(sorted(reasons.items())),
                                                       "admitted_interval_count": len(intervals)}})
    return sorted(intervals), warnings


def subtract_campus_blocks(row: m.Availability, start: date, end: date, tz,
                           intervals: list[tuple[datetime, datetime]], locked_blocks: list) -> m.Availability:
    """Subtract occupied instants once, after temporary windows; never persist it.

    This is deliberately independent of temporary_availability's 31-day/64-cut
    user-input cap. Cached timetable evidence may cover larger normal horizons.
    """
    for first, last in intervals:
        if any(first < _point(block.ends_at) and last > _point(block.starts_at) for block in locked_blocks):
            raise ApiError(409, "LOCKED_CONSTRAINT_CONFLICT", "可靠校园课表与锁定安排冲突，请先解锁或调整学习安排",
                           details={"source": "campus_cache"})
    exceptions = {e["date"]: deepcopy(e) for e in row.date_exceptions or []}
    weekly = deepcopy(row.weekly_windows or [])
    day = start
    while day <= end:
        midnight = datetime.combine(day, time.min, tz)
        tomorrow = datetime.combine(day + timedelta(days=1), time.min, tz)
        cuts = [(max(a, midnight).astimezone(tz), min(b, tomorrow).astimezone(tz))
                for a, b in intervals if a < tomorrow and b > midnight]
        if cuts:
            key = day.isoformat()
            original = exceptions.get(key, {}).get("windows")
            if original is None:
                original = [w for w in weekly if w["weekday"] == day.isoweekday()]
            spans = [(_minute(w["start"]), _minute(w["end"])) for w in original]
            for first, last in sorted(cuts):
                # Wall-clock minute conversion, not elapsed time since midnight
                # (elapsed time is incorrect on DST transition dates).
                a = first.hour * 60 + first.minute
                b = 1440 if last.date() > day else last.hour * 60 + last.minute
                if first.utcoffset() != last.utcoffset() or first.fold or last.fold:
                    # HH:MM availability cannot express folds. Occupy the union
                    # of wall minutes touched by these instants, conservatively
                    # enclosing it, rather than inverting/shrinking the cut.
                    probe = first.astimezone(timezone.utc)
                    stop = last.astimezone(timezone.utc)
                    minutes = []
                    while probe < stop:
                        local = probe.astimezone(tz)
                        if local.date() == day:
                            minutes.append(local.hour * 60 + local.minute)
                        probe += timedelta(minutes=1)
                    if minutes:
                        a, b = min(minutes), max(minutes) + 1
                if last.second or last.microsecond:
                    b += 1  # Never round an occupied end inward.
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
            exceptions[key] = {"date": key, "windows": [{"start": f"{a//60:02}:{a%60:02}",
                                                             "end": f"{b//60:02}:{b%60:02}"} for a, b in spans]}
        day += timedelta(days=1)
    return m.Availability(id=1, timezone=row.timezone, weekly_windows=weekly,
                          date_exceptions=[exceptions[key] for key in sorted(exceptions)],
                          daily_limit_minutes=row.daily_limit_minutes)
