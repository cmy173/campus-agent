"""读端点测试：契约形状、过滤、游标分页、错误信封（数据来自 seed 契约样例）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import REQUEST_ID_HEADER


def test_health(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert len(body["agent_source_sha256"]) == 64
    assert all(char in "0123456789abcdef" for char in body["agent_source_sha256"])
    assert body["time"].endswith("Z")


def test_request_id_header_echo(client: TestClient) -> None:
    resp = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "req_test_0001"})
    assert resp.headers[REQUEST_ID_HEADER] == "req_test_0001"
    resp2 = client.get("/api/v1/health")
    assert resp2.headers[REQUEST_ID_HEADER].startswith("req_")


def test_list_courses(client: TestClient) -> None:
    resp = client.get("/api/v1/courses")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert first["id"].startswith("crs_")
    assert first["created_at"].endswith("Z")
    assert first["source_refs"][0]["platform"]


def test_get_course_detail(client: TestClient) -> None:
    resp = client.get("/api/v1/courses/crs_3f8a21")
    assert resp.status_code == 200
    assert resp.json()["title"] == "数据结构基础"


def test_course_not_found_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/courses/crs_nope")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["retryable"] is False
    assert error["details"]["id"] == "crs_nope"
    assert error["request_id"]


def test_course_resources_filtered(client: TestClient) -> None:
    resp = client.get("/api/v1/courses/crs_3f8a21/resources")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(r["course_id"] == "crs_3f8a21" for r in items)
    res_01b2 = next(r for r in items if r["id"] == "res_01b2")
    assert len(res_01b2["source_refs"]) == 2


def test_course_assignments_and_recordings(client: TestClient) -> None:
    assignments = client.get("/api/v1/courses/crs_3f8a21/assignments").json()["items"]
    assert all(a["course_id"] == "crs_3f8a21" for a in assignments)
    assert assignments[0]["source_ref"]["platform"] == "xzzd"
    recordings = client.get("/api/v1/courses/crs_9b6c40/recordings").json()["items"]
    assert recordings and all(r["course_id"] == "crs_9b6c40" for r in recordings)


def test_assignments_all_and_detail(client: TestClient) -> None:
    items = client.get("/api/v1/assignments").json()["items"]
    assert len(items) == 4
    one = client.get("/api/v1/assignments/asg_a101").json()
    assert one["submission_status"] == "not_submitted"


def test_resource_and_versions(client: TestClient) -> None:
    resp = client.get("/api/v1/resources/res_01a7")
    assert resp.status_code == 200
    assert resp.json()["extent_unit"] == "slide"
    versions = client.get("/api/v1/resources/res_01a7/versions").json()["items"]
    assert [v["version"] for v in versions] == [2, 1]
    assert all(v["resource_id"] == "res_01a7" for v in versions)


def test_versions_of_unknown_resource_404(client: TestClient) -> None:
    resp = client.get("/api/v1/resources/res_nope/versions")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_tasks_filter_by_course_and_status(client: TestClient) -> None:
    items = client.get("/api/v1/tasks", params={"course_id": "crs_3f8a21"}).json()["items"]
    assert items and all(t["course_id"] == "crs_3f8a21" for t in items)
    pending = client.get("/api/v1/tasks", params={"status": "pending"}).json()["items"]
    assert pending and all(t["status"] == "pending" for t in pending)


def test_tasks_due_before_and_archived(client: TestClient) -> None:
    due = client.get("/api/v1/tasks", params={"due_before": "2027-01-01T00:00:00Z"}).json()["items"]
    assert due and all(t["due_at"] is not None for t in due)
    default = client.get("/api/v1/tasks").json()["items"]
    with_archived = client.get("/api/v1/tasks", params={"include_archived": True}).json()["items"]
    assert len(with_archived) >= len(default)


def test_tasks_invalid_status_400(client: TestClient) -> None:
    resp = client.get("/api/v1/tasks", params={"status": "nope"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_tasks_unknown_course_404(client: TestClient) -> None:
    resp = client.get("/api/v1/tasks", params={"course_id": "crs_nope"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_pagination_limit_and_cursor(client: TestClient) -> None:
    first = client.get("/api/v1/courses", params={"limit": 1}).json()
    assert len(first["items"]) == 1
    assert first["next_cursor"]
    second = client.get("/api/v1/courses", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1
    assert second["items"][0]["id"] != first["items"][0]["id"]


def test_pagination_invalid_params_400(client: TestClient) -> None:
    resp = client.get("/api/v1/courses", params={"limit": 0})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
    resp2 = client.get("/api/v1/courses", params={"cursor": "!!not-a-cursor!!"})
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "VALIDATION_FAILED"


def test_availability(client: TestClient) -> None:
    body = client.get("/api/v1/preferences/availability").json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["daily_limit_minutes"] == 180
    assert len(body["weekly_windows"]) == 9


def test_plans_current_and_list(client: TestClient) -> None:
    body = client.get("/api/v1/plans/current").json()
    assert body["plan"]["status"] == "active"
    assert body["current_input_revision"] == body["plan"]["input_revision"]
    active_only = client.get("/api/v1/plans", params={"status": "active"}).json()["items"]
    assert [p["id"] for p in active_only] == [body["plan"]["id"]]
    candidate = client.get("/api/v1/plans/pln_7a1b").json()
    assert candidate["summary"]["scheduled_minutes"] == 212
    assert candidate["unassigned"][0]["reason_code"] == "DEADLINE_PASSED"


def test_capabilities(client: TestClient) -> None:
    resp = client.get("/api/v1/connections/conn_2a9f/capabilities")
    assert resp.status_code == 200
    assert resp.json()["platform"] == "xzzd"
    resp2 = client.get("/api/v1/connections/conn_8c1de/capabilities")
    assert resp2.status_code == 404
    assert resp2.json()["error"]["code"] == "NOT_FOUND"


def test_connections_list(client: TestClient) -> None:
    body = client.get("/api/v1/connections").json()
    assert len(body["items"]) == 2
    auth = next(c for c in body["items"] if c["id"] == "conn_8c1de")
    assert auth["status"] == "auth_required"
    assert auth["recovery_action"]["type"] == "user_login"
    assert auth["entry_url"] == "https://classroom.zju.edu.cn/"


def test_job_detail(client: TestClient) -> None:
    body = client.get("/api/v1/jobs/job_7g8h9i").json()
    assert body["status"] == "partial_failed"
    assert body["result_refs"][0]["type"] == "export_file"


def test_jobs_list_order_filter_pagination(client: TestClient) -> None:
    body = client.get("/api/v1/jobs").json()
    times = [j["created_at"] for j in body["items"]]
    assert times == sorted(times, reverse=True)
    assert {j["id"] for j in body["items"]} == {
        "job_1a2b3c",
        "job_4d5e6f",
        "job_7g8h9i",
        "job_8d2c1f",
        "job_9h1k2m",
        "job_5j6k7l",
        "job_6l7m8n",
    }
    parse_only = client.get("/api/v1/jobs", params={"type": "parse"}).json()["items"]
    assert [j["id"] for j in parse_only] == ["job_8d2c1f"]
    build_only = client.get("/api/v1/jobs", params={"type": "build_tasks"}).json()["items"]
    assert [j["id"] for j in build_only] == ["job_9h1k2m"]
    assert build_only[0]["warnings"][0]["code"] == "TASK_VERSION_UPDATED"
    review_only = client.get("/api/v1/jobs", params={"type": "review_generate"}).json()["items"]
    assert [j["id"] for j in review_only] == ["job_6l7m8n", "job_5j6k7l"]
    assert review_only[0]["phase"] == "generation"
    assert review_only[1]["phase"] is None
    partial = client.get("/api/v1/jobs", params={"status": "partial_failed"}).json()["items"]
    assert [j["id"] for j in partial] == ["job_7g8h9i"]
    first = client.get("/api/v1/jobs", params={"limit": 2}).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get("/api/v1/jobs", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 2 and second["next_cursor"]
    third = client.get("/api/v1/jobs", params={"limit": 2, "cursor": second["next_cursor"]}).json()
    assert len(third["items"]) == 2 and third["next_cursor"]
    fourth = client.get("/api/v1/jobs", params={"limit": 2, "cursor": third["next_cursor"]}).json()
    assert len(fourth["items"]) == 1 and fourth["next_cursor"] is None
    ids = [j["id"] for j in first["items"] + second["items"] + third["items"] + fourth["items"]]
    assert len(set(ids)) == 7
    resp = client.get("/api/v1/jobs", params={"status": "nope"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_connection_detail(client: TestClient) -> None:
    body = client.get("/api/v1/connections/conn_2a9f").json()
    assert body["platform"] == "xzzd"
    assert body["status"] == "ok"
    assert body["entry_url"] == "https://course.zju.edu.cn/ua/login"
    resp = client.get("/api/v1/connections/conn_nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_assignments_filters(client: TestClient) -> None:
    before = client.get("/api/v1/assignments", params={"due_before": "2026-10-10T00:00:00+08:00"}).json()["items"]
    assert [a["id"] for a in before] == ["asg_a101"]
    after = client.get("/api/v1/assignments", params={"due_after": "2026-10-15T00:00:00+08:00"}).json()["items"]
    assert {a["id"] for a in after} == {"asg_a103", "asg_a104"}
    default = client.get("/api/v1/assignments").json()["items"]
    with_archived = client.get("/api/v1/assignments", params={"include_archived": True}).json()["items"]
    assert len(with_archived) >= len(default)
    resp = client.get("/api/v1/assignments", params={"due_before": "not-a-date"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_resource_chunks(client: TestClient) -> None:
    assert client.get("/api/v1/resources/res_01a7").json()["chunk_count"] == 2
    assert client.get("/api/v1/resources/res_01c0").json()["chunk_count"] is None
    body = client.get("/api/v1/resources/res_01a7/chunks").json()
    assert [c["ordinal"] for c in body["items"]] == [0, 1]
    assert all(c["resource_version"] == 2 for c in body["items"])
    assert body["items"][0]["locator"]["kind"] == "slide"
    assert body["items"][0]["provenance"]["job_id"] == "job_8d2c1f"
    assert body["next_cursor"] is None
    first = client.get("/api/v1/resources/res_01a7/chunks", params={"limit": 1}).json()
    assert first["next_cursor"]
    second = client.get("/api/v1/resources/res_01a7/chunks", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [c["ordinal"] for c in second["items"]] == [1]
    assert second["next_cursor"] is None
    pending = client.get("/api/v1/resources/res_01c0/chunks").json()
    assert pending["items"] == [] and pending["next_cursor"] is None
    resp = client.get("/api/v1/resources/res_nope/chunks")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_write_route_envelope(client: TestClient) -> None:
    resp = client.post("/api/v1/does-not-exist", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_search_and_files_validation(client: TestClient) -> None:
    missing_q = client.get("/api/v1/search")
    assert missing_q.status_code == 400
    assert missing_q.json()["error"]["code"] == "VALIDATION_FAILED"
    unknown = client.get("/api/v1/files/fil_nope1")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_route_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
