"""计划端点测试：preview（引擎接入与边界换算）、apply（PLAN_STALE/版本保护）、revoke（恢复）与列表排序。

注意：本文件按 pytest 默认的文件名字母序先于 test_read/test_write 执行；
末尾通过“预览 + 应用”把生效计划留在最新的候选上（input_revision 与实时一致），
后续文件对 /plans/current 的断言（如 fp 相等、块存在）依赖于此。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

TZ = ZoneInfo("Asia/Shanghai")
HORIZON_DAYS = 14


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _preview(client: TestClient, **body) -> dict:
    resp = client.post("/api/v1/plans/preview", json=body or None)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _current(client: TestClient) -> dict:
    return client.get("/api/v1/plans/current").json()


def _all_plans(client: TestClient) -> list[dict]:
    return client.get("/api/v1/plans").json()["items"]


def test_preview_default_horizon_and_engine_inputs(client: TestClient) -> None:
    active_before = _current(client)["plan"]
    max_version = max(p["version"] for p in _all_plans(client))

    resp = client.post("/api/v1/plans/preview")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    plan = body["plan"]

    assert plan["id"].startswith("pln_")
    assert plan["status"] == "candidate"
    assert plan["version"] == max_version + 1
    assert plan["based_on_plan_id"] == active_before["id"]
    assert plan["applied_at"] is None and plan["revoked_at"] is None and plan["superseded_by"] is None

    today = datetime.now(TZ).date()
    assert plan["horizon_start"] == today.isoformat()
    assert plan["horizon_end"] == (today + timedelta(days=HORIZON_DAYS - 1)).isoformat()

    # 输入指纹：响应内一致、与实时一致
    live = _current(client)["current_input_revision"]
    assert body["current_input_revision"] == live
    assert plan["input_revision"] == live

    # 摘要字段与契约一致（plan.schema.json PlanSummary）
    assert set(plan["summary"]) == {
        "capacity_minutes", "scheduled_minutes", "locked_minutes",
        "scheduled_task_count", "unassigned_task_count",
    }
    assert all(isinstance(v, int) for v in plan["summary"].values())
    assert plan["summary"]["capacity_minutes"] > 0
    assert isinstance(plan["warnings"], list)

    # 块约束：范围含首尾、互不重叠、不含已归档/已取消任务
    h_start = datetime.combine(date.fromisoformat(plan["horizon_start"]), datetime.min.time(), TZ)
    h_end = datetime.combine(date.fromisoformat(plan["horizon_end"]) + timedelta(days=1), datetime.min.time(), TZ)
    blocks = sorted(plan["blocks"], key=lambda b: _parse(b["starts_at"]))
    assert blocks, "默认范围内应能安排若干待办任务"
    for b in blocks:
        assert h_start <= _parse(b["starts_at"]) < h_end
        assert _parse(b["starts_at"]) < _parse(b["ends_at"])
    for prev, nxt in zip(blocks, blocks[1:]):
        assert _parse(nxt["starts_at"]) >= _parse(prev["ends_at"])
    assert "tsk_5a2b63" not in {b["task_id"] for b in blocks}  # 已归档+已取消
    assert len({b["id"] for b in blocks}) == len(blocks)

    # 边界换算：windows=[] 的日期例外（2026-10-16）不得出现时间块
    exception_day = date(2026, 10, 16)
    if plan["horizon_start"] <= exception_day.isoformat() <= plan["horizon_end"]:
        assert all(_parse(b["starts_at"]).astimezone(TZ).date() != exception_day for b in blocks)

    # 截止已过的任务进入未安排并给出开放枚举原因码
    overdue = [u for u in plan["unassigned"] if u["task_id"] == "tsk_2c8d13"]
    assert overdue and overdue[0]["reason_code"] == "DEADLINE_PASSED"
    assert overdue[0]["shortage_minutes"] is None
    allowed = {
        "DEADLINE_PASSED", "INSUFFICIENT_WINDOW_BEFORE_DUE", "INSUFFICIENT_TOTAL_TIME",
        "TASK_TOO_LONG_INDIVISIBLE", "FRAGMENTED_WINDOW", "DEPENDENCY_CONFLICT", "INVALID_TASK",
    }
    assert all(u["reason_code"] in allowed for u in plan["unassigned"])


def test_preview_explicit_horizon_and_respect_locked_flag(client: TestClient) -> None:
    today = datetime.now(TZ).date()
    start, end = today + timedelta(days=1), today + timedelta(days=7)
    plan = _preview(client, horizon_start=start.isoformat(), horizon_end=end.isoformat())["plan"]
    assert plan["horizon_start"] == start.isoformat()
    assert plan["horizon_end"] == end.isoformat()

    no_locked = _preview(client, horizon_start=start.isoformat(), horizon_end=end.isoformat(), respect_locked=False)["plan"]
    assert all(not b["locked"] for b in no_locked["blocks"])


def test_repeated_preview_may_share_engine_block_ids(client: TestClient) -> None:
    """引擎块 ID（blk:{task}:{i}）仅计划内唯一；两次预览允许出现相同块 ID（plan_blocks 复合主键）。"""
    p1 = _preview(client)["plan"]
    p2 = _preview(client)["plan"]
    ids1 = {b["id"] for b in p1["blocks"]}
    ids2 = {b["id"] for b in p2["blocks"]}
    assert ids1 and ids2
    assert ids1 & ids2, "相同输入下两次预览应产生部分相同的引擎块 ID"


def test_locked_block_retained_by_next_preview(client: TestClient) -> None:
    candidate = _preview(client)["plan"]
    applied = client.post(f"/api/v1/plans/{candidate['id']}/apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["plan"]["status"] == "active"

    target_task = candidate["blocks"][0]["task_id"]
    before = [(b["starts_at"], b["ends_at"]) for b in candidate["blocks"] if b["task_id"] == target_task]

    lock = client.post(f"/api/v1/tasks/{target_task}/lock", json={"locked": True})
    assert lock.status_code == 200, lock.text
    locked_blocks = [b for b in lock.json()["plan"]["blocks"] if b["task_id"] == target_task]
    assert locked_blocks and all(b["locked"] for b in locked_blocks)

    kept = _preview(client)["plan"]
    kept_blocks = [(b["starts_at"], b["ends_at"]) for b in kept["blocks"] if b["task_id"] == target_task]
    assert kept_blocks == before, "锁定块应原样保留"
    assert all(b["locked"] for b in kept["blocks"] if b["task_id"] == target_task)

    dropped = _preview(client, respect_locked=False)["plan"]
    assert all(not b["locked"] for b in dropped["blocks"])


def test_apply_stale_after_input_change(client: TestClient) -> None:
    stale = _preview(client)["plan"]

    created = client.post("/api/v1/tasks", json={"title": "触发过期的任务", "estimated_minutes": 20})
    assert created.status_code == 201

    resp = client.post(f"/api/v1/plans/{stale['id']}/apply")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "PLAN_STALE"
    assert error["details"]["plan_id"] == stale["id"]
    assert error["details"]["input_revision"] == stale["input_revision"]
    assert error["details"]["current_input_revision"] != stale["input_revision"]

    fresh = _preview(client)["plan"]
    assert fresh["input_revision"] == error["details"]["current_input_revision"]
    ok = client.post(f"/api/v1/plans/{fresh['id']}/apply")
    assert ok.status_code == 200
    assert ok.json()["plan"]["status"] == "active"
    assert ok.json()["previous_plan_id"] is not None

    assert client.post("/api/v1/plans/pln_nope/apply").status_code == 404


def test_apply_supersedes_previous_and_rejects_older_candidate(client: TestClient) -> None:
    older = _preview(client)["plan"]
    newer = _preview(client)["plan"]
    active_before = _current(client)["plan"]

    ok = client.post(f"/api/v1/plans/{newer['id']}/apply")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["previous_plan_id"] == active_before["id"]
    assert body["plan"]["applied_at"] is not None

    previous = client.get(f"/api/v1/plans/{active_before['id']}").json()
    assert previous["status"] == "superseded"
    assert previous["superseded_by"] == newer["id"]

    rejected = client.post(f"/api/v1/plans/{older['id']}/apply")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "CONFLICT"

    again = client.post(f"/api/v1/plans/{newer['id']}/apply")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


def test_revoke_restores_previous_plan(client: TestClient) -> None:
    previous = _current(client)["plan"]
    candidate = _preview(client)["plan"]
    assert client.post(f"/api/v1/plans/{candidate['id']}/apply").status_code == 200

    resp = client.post(f"/api/v1/plans/{candidate['id']}/revoke")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"]["status"] == "revoked"
    assert body["plan"]["revoked_at"] is not None
    assert body["restored_plan_id"] == previous["id"]

    restored = client.get(f"/api/v1/plans/{previous['id']}").json()
    assert restored["status"] == "active"
    assert restored["superseded_by"] is None
    assert _current(client)["plan"]["id"] == previous["id"]

    twice = client.post(f"/api/v1/plans/{candidate['id']}/revoke")
    assert twice.status_code == 409
    assert twice.json()["error"]["code"] == "CONFLICT"

    still_candidate = client.get(f"/api/v1/plans/{candidate['id']}")
    assert still_candidate.status_code == 200
    assert still_candidate.json()["status"] == "revoked"


def test_revoke_candidate_is_conflict(client: TestClient) -> None:
    candidate = _preview(client)["plan"]
    resp = client.post(f"/api/v1/plans/{candidate['id']}/revoke")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"

    assert client.post("/api/v1/plans/pln_nope/revoke").status_code == 404


def test_plans_list_version_desc_with_cursor(client: TestClient) -> None:
    versions = [p["version"] for p in _all_plans(client)]
    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)

    page1 = client.get("/api/v1/plans", params={"limit": 3}).json()
    assert len(page1["items"]) == 3
    assert [p["version"] for p in page1["items"]] == versions[:3]
    assert page1["next_cursor"]
    page2 = client.get("/api/v1/plans", params={"limit": 3, "cursor": page1["next_cursor"]}).json()
    assert [p["version"] for p in page2["items"]] == versions[3:6]

    active_only = client.get("/api/v1/plans", params={"status": "active"}).json()["items"]
    assert [p["id"] for p in active_only] == [_current(client)["plan"]["id"]]

    bad_cursor = client.get("/api/v1/plans", params={"cursor": "!!not-a-cursor!!"})
    assert bad_cursor.status_code == 400


def test_preview_request_validation(client: TestClient) -> None:
    resp = client.post("/api/v1/plans/preview", json={"horizon_start": "2026-10-20", "horizon_end": "2026-10-10"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    bad_date = client.post("/api/v1/plans/preview", json={"horizon_start": "2026-13-01"})
    assert bad_date.status_code == 400

    extra = client.post("/api/v1/plans/preview", json={"unknown_field": 1})
    assert extra.status_code == 400


def test_final_apply_keeps_plan_fresh_for_following_suites(client: TestClient) -> None:
    """末位用例：应用最新预览，使 /plans/current 的 input_revision 与实时一致。"""
    candidate = _preview(client)["plan"]
    assert client.post(f"/api/v1/plans/{candidate['id']}/apply").status_code == 200
    body = _current(client)
    assert body["plan"]["id"] == candidate["id"]
    assert body["current_input_revision"] == body["plan"]["input_revision"]
