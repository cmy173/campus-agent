"""Synthetic cache + real schedule engine/apply; no campus traffic or real DB."""
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models as m
from app.campus_schedule_constraints import CAMPUS_TZ, CampusEvidence, read_campus_evidence, subtract_campus_blocks
from app.campus_workspace import CampusWorkspace
from app.db import Base
from app.errors import ApiError
from app.plan_engine import preview_candidate
from app.plan_input import compute_input_revision
from app.plan_mutations import apply_plan
from app.repo import normalize_dt
from connectors.campus.campus_endpoints import TIMETABLE


@pytest.fixture
def campus_workspace(tmp_path, monkeypatch):
    def forbid_client():
        raise AssertionError("planning must not create a campus connector session")
    campus = CampusWorkspace(tmp_path / "synthetic-campus.sqlite3", client_factory=forbid_client)
    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: campus)
    # The public query wrapper may re-adopt browser cookies on auth errors. Even
    # an authorized provider must not be consulted while planning/revising/applying.
    campus._browser_cookie_provider = lambda: pytest.fail("browser cookies accessed")
    monkeypatch.setattr(campus, "query", lambda *a, **k: pytest.fail("auth-retry wrapper accessed"))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        day = datetime.now(CAMPUS_TZ).date() + timedelta(days=1)
        monday = day - timedelta(days=day.weekday())
        stamp = m.utc_now().isoformat()
        row = {"name": "合成课", "code": "SYNTH", "source": TIMETABLE, "fetched_at": stamp,
               "stale": False, "weekday": str(day.isoweekday()), "periods": "1-2", "weeks": "1",
               "season": "秋", "year": "2026", "semester": "1|秋"}
        calendar = {"verified": True, "stale": False, "fetched_at": stamp,
                    "seasons": [{"name": "秋", "start": str(monday), "end": str(monday + timedelta(days=55))}],
                    "period_times": [["19:00", "19:45"], ["19:50", "20:35"]],
                    "holidays": [], "exchanges": {}, "uncertain_dates": [],
                    "valid_until": str(monday + timedelta(days=55)),
                    "sources": [{"url": "https://www.zju.edu.cn/synthetic-calendar", "sha256": "a" * 64}],
                    "scope": "主校区本科"}
        result = {"source": "courses", "platform": "zdbk", "records": [row],
                  "freshness": "fresh", "completeness": "complete", "fetched_at": stamp}
        fixture = {"campus": campus, "db": db, "day": day, "row": row, "calendar": calendar, "result": result}
        db.add(m.LearningTask(id="task", title="学习", type="reading", estimated_minutes=120,
                              due_precision="unknown", status="pending"))
        db.add(m.Availability(id=1, timezone="Asia/Shanghai", weekly_windows=[
            {"weekday": day.isoweekday(), "start": "18:00", "end": "22:00"}], date_exceptions=[], daily_limit_minutes=None))
        db.commit()
        yield fixture
    campus.close()
    engine.dispose()


def save(fixture):
    campus = fixture["campus"]
    campus.manager.repository.write("local", {"calendars": {"2026/1|秋": deepcopy(fixture["calendar"])}})
    import json
    scope = json.dumps({"year": "2026", "semester": "1|秋"}, sort_keys=True, ensure_ascii=False)
    campus.manager.capability_repository.write("local", "zdbk", "courses", scope, deepcopy(fixture["result"]))


def preview(fixture, **kwargs):
    return preview_candidate(fixture["db"], fixture["day"], fixture["day"], True, **kwargs)


def assert_excluded(plan, day, first=time(19), last=time(20, 35)):
    a, b = datetime.combine(day, first, CAMPUS_TZ), datetime.combine(day, last, CAMPUS_TZ)
    assert plan.blocks
    for block in plan.blocks:
        assert not (normalize_dt(block.starts_at) < b and normalize_dt(block.ends_at) > a)


def test_exact_cached_period_times_exclude_overlap_for_preview_and_apply(campus_workspace):
    f = campus_workspace
    save(f)
    original = f["db"].get(m.Availability, 1).to_contract()
    plan = preview(f)
    assert_excluded(plan, f["day"])
    assert plan.summary["capacity_minutes"] == 145  # 240 - 95; teaching breaks are occupied too.
    assert not plan.warnings
    assert plan.input_revision == compute_input_revision(f["db"])
    assert f["db"].get(m.Availability, 1).to_contract() == original
    receipt = apply_plan(f["db"], plan.id)
    assert receipt["plan"]["status"] == "active"
    assert_excluded(plan, f["day"])
    assert plan.input_revision == compute_input_revision(f["db"])


def test_duplicate_cache_rows_do_not_double_block_or_change_revision(campus_workspace):
    f = campus_workspace
    save(f)
    before = compute_input_revision(f["db"])
    f["result"]["records"].append(deepcopy(f["row"]))
    save(f)
    assert compute_input_revision(f["db"]) == before
    plan = preview(f)
    assert plan.summary["capacity_minutes"] == 145
    assert_excluded(plan, f["day"])


def test_no_data_degrades_with_warning_and_does_not_prevent_apply(campus_workspace):
    f = campus_workspace
    plan = preview(f)
    assert plan.summary["capacity_minutes"] == 240
    assert plan.blocks
    assert {w.code for w in plan.warnings} == {"CAMPUS_TIMETABLE_UNCERTAIN"}
    assert plan.warnings[0].details["reasons"]["no_timetable"] == 1
    assert apply_plan(f["db"], plan.id)["plan"]["status"] == "active"


def test_empty_scoped_cache_evidence_stays_in_revision(campus_workspace):
    f = campus_workspace
    f["result"]["records"] = []
    save(f)
    before = compute_input_revision(f["db"])
    assert "2026/1|秋" in read_campus_evidence().terms
    f["result"]["completeness"] = "partial"
    save(f)
    assert compute_input_revision(f["db"]) != before
    assert preview(f).summary["capacity_minutes"] == 240


def test_unreadable_cache_degrades_and_never_calls_browser_or_connector(campus_workspace, monkeypatch):
    f = campus_workspace
    def fail_read(owner):
        raise OSError("synthetic cache unavailable")
    monkeypatch.setattr(f["campus"].manager.repository, "read", fail_read)
    plan = preview(f)
    assert plan.summary["capacity_minutes"] == 240
    assert plan.warnings[0].details["reasons"]["cache_unavailable"] == 1
    assert apply_plan(f["db"], plan.id)["plan"]["status"] == "active"


@pytest.mark.parametrize("change", ["stale", "partial", "auth_error", "old_stamp", "date_only_stamp",
                                   "naive_stamp", "missing_weeks", "unknown_weekday", "unknown_periods",
                                   "period_conflict", "unknown_season", "missing_scope_season", "haining",
                                   "fallback_calendar", "old_calendar", "calendar_date_only",
                                   "uncertain_day", "expired_calendar", "bad_calendar_time"])
def test_unreliable_data_warns_without_fabricated_hard_blocks(campus_workspace, change):
    f = campus_workspace
    if change == "stale":
        f["result"]["freshness"] = "stale"
    elif change == "partial":
        f["result"]["completeness"] = "partial"
    elif change == "auth_error":
        f["result"]["error"] = {"code": "AUTH_EXPIRED", "message": "synthetic"}
    elif change == "old_stamp":
        f["result"]["fetched_at"] = (m.utc_now() - timedelta(hours=7)).isoformat()
    elif change == "date_only_stamp":
        f["result"]["fetched_at"] = str(f["day"])
    elif change == "naive_stamp":
        f["row"]["fetched_at"] = datetime.now().isoformat()
    elif change == "missing_weeks":
        f["row"]["weeks"] = ""
        f["row"]["odd_even"] = "2"  # parity alone must not invent an all-weeks range
    elif change == "unknown_weekday":
        f["row"]["weekday"] = "周四"
    elif change == "unknown_periods":
        f["row"]["periods"] = "晚上"
    elif change == "period_conflict":
        f["row"].update(start_period="2", duration="1")
    elif change == "unknown_season":
        f["row"]["season"] = "特殊教学周"
    elif change == "missing_scope_season":
        f["row"]["season"] = ""
        f["row"]["semester"] = "3"
        # Keep queried scope unchanged; no row-to-scope mismatch should be trusted.
    elif change == "haining":
        f["row"]["location"] = "海宁国际校区"
    elif change == "fallback_calendar":
        f["calendar"]["verified"] = False
        f["calendar"]["usable"] = True
    elif change == "old_calendar":
        f["calendar"]["fetched_at"] = (m.utc_now() - timedelta(hours=7)).isoformat()
    elif change == "calendar_date_only":
        f["calendar"]["fetched_at"] = str(f["day"])
    elif change == "uncertain_day":
        f["calendar"]["uncertain_dates"] = [str(f["day"])]
    elif change == "expired_calendar":
        f["calendar"]["valid_until"] = str(f["day"] - timedelta(days=1))
    else:
        f["calendar"]["period_times"][0][0] = str(f["day"])
    save(f)
    plan = preview(f)
    assert plan.summary["capacity_minutes"] == 240
    assert "CAMPUS_TIMETABLE_UNCERTAIN" in {w.code for w in plan.warnings}
    assert apply_plan(f["db"], plan.id)["plan"]["status"] == "active"


@pytest.mark.parametrize("change", ["periods", "calendar_times", "holiday", "withdrawn", "freshness", "partial"])
def test_timetable_evidence_changes_make_candidate_stale(campus_workspace, change):
    f = campus_workspace
    save(f)
    plan = preview(f)
    if change == "periods":
        f["row"]["periods"] = "2"
    elif change == "calendar_times":
        f["calendar"]["period_times"][1][1] = "20:55"
    elif change == "holiday":
        f["calendar"]["holidays"] = [str(f["day"])]
    elif change == "withdrawn":
        f["result"]["records"] = []
    elif change == "freshness":
        f["result"]["freshness"] = "stale"
    else:
        f["result"]["completeness"] = "partial"
    save(f)
    assert plan.input_revision != compute_input_revision(f["db"])
    with pytest.raises(ApiError) as error:
        apply_plan(f["db"], plan.id)
    assert error.value.code == "PLAN_STALE"
    assert plan.status == "candidate"


def test_cache_aging_alone_changes_revision(campus_workspace):
    f = campus_workspace
    save(f)
    evidence = read_campus_evidence()
    before = compute_input_revision(f["db"], campus_evidence=evidence)
    aged = CampusEvidence(evidence.terms, evidence.calendars, now=evidence.now + timedelta(hours=7))
    assert compute_input_revision(f["db"], campus_evidence=aged) != before


def test_fixed_calendar_block_change_without_task_revision_makes_plan_stale(campus_workspace):
    f = campus_workspace
    db, day = f["db"], f["day"]
    active = m.Plan(id="active", version=1, status="active", input_revision=compute_input_revision(db),
                    horizon_start=day, horizon_end=day, summary={})
    db.add(active)
    db.flush()
    block = m.PlanBlock(plan_id=active.id, id="fixed", task_id="task", locked=True,
                        starts_at=normalize_dt(datetime.combine(day, time(18), CAMPUS_TZ)),
                        ends_at=normalize_dt(datetime.combine(day, time(19), CAMPUS_TZ)))
    db.add(block)
    db.commit()
    plan = preview(f)
    block.ends_at += timedelta(minutes=10)
    db.commit()
    with pytest.raises(ApiError) as error:
        apply_plan(db, plan.id)
    assert error.value.code == "PLAN_STALE"


def test_preserving_same_fixed_instants_across_apply_does_not_self_invalidate(campus_workspace):
    f = campus_workspace
    db, day = f["db"], f["day"]
    active = m.Plan(id="active", version=1, status="active", input_revision=compute_input_revision(db),
                    horizon_start=day, horizon_end=day, summary={})
    db.add(active)
    db.flush()
    db.add(m.PlanBlock(plan_id=active.id, id="fixed", task_id="task", locked=True,
                       starts_at=normalize_dt(datetime.combine(day, time(18), CAMPUS_TZ)),
                       ends_at=normalize_dt(datetime.combine(day, time(19), CAMPUS_TZ))))
    db.commit()
    plan = preview(f)
    assert any(b.locked for b in plan.blocks)
    apply_plan(db, plan.id)
    assert plan.input_revision == compute_input_revision(db)


def test_known_campus_conflict_is_not_silently_retained_as_locked_learning(campus_workspace):
    f = campus_workspace
    save(f)
    db, day = f["db"], f["day"]
    active = m.Plan(id="active", version=1, status="active", input_revision=compute_input_revision(db),
                    horizon_start=day, horizon_end=day, summary={})
    db.add(active)
    db.flush()
    db.add(m.PlanBlock(plan_id=active.id, id="fixed", task_id="task", locked=True,
                       starts_at=normalize_dt(datetime.combine(day, time(19), CAMPUS_TZ)),
                       ends_at=normalize_dt(datetime.combine(day, time(20), CAMPUS_TZ))))
    db.commit()
    with pytest.raises(ApiError) as error:
        preview(f)
    assert error.value.code == "LOCKED_CONSTRAINT_CONFLICT"
    # respect_locked=False unlocks learning, but cannot disable campus hard facts.
    plan = preview_candidate(db, day, day, False)
    assert_excluded(plan, day)


def test_temporary_windows_cannot_reintroduce_campus_occupied_time(campus_workspace):
    f = campus_workspace
    save(f)
    f["db"].get(m.LearningTask, "task").estimated_minutes = 60
    f["db"].commit()
    plan = preview(f, temporary_windows=[{"weekday": f["day"].isoweekday(), "start": "19:00", "end": "22:00"}])
    assert_excluded(plan, f["day"])
    assert plan.summary["capacity_minutes"] == 85


def test_campus_times_are_converted_to_user_timezone_not_relabelled(campus_workspace):
    f = campus_workspace
    save(f)
    row = f["db"].get(m.Availability, 1)
    row.timezone = "UTC"
    row.weekly_windows = [{"weekday": f["day"].isoweekday(), "start": "10:00", "end": "14:00"}]
    f["db"].commit()
    plan = preview(f)
    assert_excluded(plan, f["day"])
    assert plan.summary["capacity_minutes"] == 145


def test_holidays_and_verified_exchange_use_actual_not_teaching_date(campus_workspace):
    f = campus_workspace
    day = f["day"]
    f["calendar"]["exchanges"] = {str(day): str(day - timedelta(days=1))}
    f["row"]["weekday"] = str((day - timedelta(days=1)).isoweekday())
    if day.isoweekday() == 1:
        monday = day - timedelta(days=7)
        f["calendar"]["seasons"][0]["start"] = str(monday)
    save(f)
    assert_excluded(preview(f), day)
    f["calendar"]["holidays"] = [str(day)]
    save(f)
    assert preview(f).summary["capacity_minutes"] == 240


@pytest.mark.parametrize("transition", ["spring", "fall"])
def test_dst_cuts_do_not_treat_elapsed_minutes_as_wall_clock(transition):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from datetime import date
    try:
        tz = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA timezone database unavailable")
    if transition == "spring":
        day = date(2026, 3, 8)
        first = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)  # 03:30 EDT
        last = datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc)  # 04:30 EDT
        expected = [{"start": "00:00", "end": "03:30"}, {"start": "04:30", "end": "05:00"}]
    else:
        day = date(2026, 11, 1)
        first = datetime(2026, 11, 1, 5, 50, tzinfo=timezone.utc)  # 01:50 EDT
        last = datetime(2026, 11, 1, 6, 10, tzinfo=timezone.utc)  # 01:10 EST (fold)
        expected = [{"start": "00:00", "end": "01:00"}, {"start": "02:00", "end": "05:00"}]
    row = m.Availability(id=1, timezone="America/New_York", weekly_windows=[
        {"weekday": day.isoweekday(), "start": "00:00", "end": "05:00"}], date_exceptions=[], daily_limit_minutes=None)
    mapped = subtract_campus_blocks(row, day, day, tz, [(first, last)], [])
    assert mapped.date_exceptions[0]["windows"] == expected


def test_availability_contents_are_revision_inputs_without_timestamp_bump(campus_workspace):
    f = campus_workspace
    before = compute_input_revision(f["db"])
    f["db"].get(m.Availability, 1).daily_limit_minutes = 30
    f["db"].commit()
    assert compute_input_revision(f["db"]) != before
