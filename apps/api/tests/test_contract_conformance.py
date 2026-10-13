"""B16 契约-实现一致性回归：响应体校验 + 契约↔实现路由集合双向对账。

第一部分：把实际响应体对 contracts/schemas/ 逐实体校验。
覆盖 seed 契约样例对应的全部实体读端点：course / resource / resource-version /
content-chunk / assignment / recording / learning-task / plan / connection /
capabilities / availability / job（含 B13 新增 job 类型 build_tasks），
以及错误信封（404 / 405 / 400 校验失败 / 409 AUTH_EXPIRED，均对 error.schema.json）。

第二部分（C2 联调整改加补）：openapi.yaml 的 (method, path) 集合与 FastAPI 应用
逐项相等——正向（契约→实现）缺项即 405/404 风险（C2 曾暴露 4 个写端点缺失）；
反向（实现→契约）多出即契约-实现漂移（如 CR004 补录的 GET /tasks/{task_id}）。

全部用例只读，不改动共享测试库状态。契约 v0.1 已冻结（B6）：只允许兼容新增；
任何结构变更必须走 contracts/reviews/CHANGE-REQUEST.md 并同步 contracts/CHANGELOG.md。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from contract_support import validate_contract

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "openapi.yaml"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

COURSE_MAIN = "crs_3f8a21"  # 有资源与作业
COURSE_REC = "crs_9b6c40"  # 有回放
RESOURCE = "res_01a7"  # 有版本链与解析片段
ASSIGNMENT = "asg_a101"
CONNECTION = "conn_2a9f"  # xzzd，ok，含 capabilities
AUTH_REQUIRED_CONNECTION = "conn_8c1de"  # zykt，auth_required
BUILD_JOB = "job_9h1k2m"  # B13 新 job 类型（build_tasks，succeeded，带告警）


def _get_list(client: TestClient, url: str, schema_name: str, *, min_items: int = 1) -> list[dict]:
    resp = client.get(url)
    assert resp.status_code == 200, f"{url}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}, f"{url} 列表信封键：{sorted(body)}"
    assert body["next_cursor"] is None or isinstance(body["next_cursor"], str)
    items = body["items"]
    assert len(items) >= min_items, f"{url}: {len(items)} 项 < {min_items}"
    for item in items:
        validate_contract(item, schema_name)
    return items


def _get_object(client: TestClient, url: str, schema_name: str) -> dict:
    resp = client.get(url)
    assert resp.status_code == 200, f"{url}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    validate_contract(body, schema_name)
    return body


def test_courses_resources_versions_chunks_conform(client: TestClient) -> None:
    _get_list(client, "/api/v1/courses", "course.schema.json", min_items=3)
    _get_object(client, f"/api/v1/courses/{COURSE_MAIN}", "course.schema.json")
    resources = _get_list(client, f"/api/v1/courses/{COURSE_MAIN}/resources", "resource.schema.json")
    assert any(r["id"] == RESOURCE for r in resources)
    _get_object(client, f"/api/v1/resources/{RESOURCE}", "resource.schema.json")
    _get_list(client, f"/api/v1/resources/{RESOURCE}/versions", "resource-version.schema.json")
    chunks = _get_list(client, f"/api/v1/resources/{RESOURCE}/chunks", "content-chunk.schema.json")
    assert all(c["text"] for c in chunks)  # 片段正文非空——检索与计划引擎的实际输入


def test_assignments_recordings_conform(client: TestClient) -> None:
    _get_list(client, "/api/v1/assignments", "assignment.schema.json", min_items=4)
    _get_object(client, f"/api/v1/assignments/{ASSIGNMENT}", "assignment.schema.json")
    _get_list(client, f"/api/v1/courses/{COURSE_MAIN}/assignments", "assignment.schema.json")
    _get_list(client, f"/api/v1/courses/{COURSE_REC}/recordings", "recording.schema.json")


def test_tasks_conform(client: TestClient) -> None:
    tasks = _get_list(client, "/api/v1/tasks", "learning-task.schema.json", min_items=1)
    _get_object(client, f"/api/v1/tasks/{tasks[0]['id']}", "learning-task.schema.json")


def test_plans_conform(client: TestClient) -> None:
    plans = _get_list(client, "/api/v1/plans", "plan.schema.json", min_items=2)
    _get_object(client, f"/api/v1/plans/{plans[0]['id']}", "plan.schema.json")
    current = client.get("/api/v1/plans/current")
    assert current.status_code == 200, current.text[:300]
    body = current.json()
    assert set(body) == {"plan", "current_input_revision"}
    assert body["plan"] is not None  # 种子含 active 计划
    validate_contract(body["plan"], "plan.schema.json")


def test_connections_and_capabilities_conform(client: TestClient) -> None:
    conns = _get_list(client, "/api/v1/connections", "connection.schema.json", min_items=2)
    _get_object(client, f"/api/v1/connections/{CONNECTION}", "connection.schema.json")
    _get_object(client, f"/api/v1/connections/{CONNECTION}/capabilities", "capabilities.schema.json")
    xzzd = next(c for c in conns if c["id"] == CONNECTION)
    assert xzzd["entry_url"]  # B12/CR001：入口地址由服务端常量下发


def test_availability_conform(client: TestClient) -> None:
    _get_object(client, "/api/v1/preferences/availability", "availability.schema.json")


def test_jobs_conform_including_build_tasks(client: TestClient) -> None:
    jobs = _get_list(client, "/api/v1/jobs", "job.schema.json", min_items=5)
    build = next(j for j in jobs if j["id"] == BUILD_JOB)
    assert build["type"] == "build_tasks"  # B13 新 job 类型
    assert build["warnings"], "build_tasks 样例应携带告警（重复内容合并/估算回退）"
    detail = _get_object(client, f"/api/v1/jobs/{BUILD_JOB}", "job.schema.json")
    assert detail["type"] == "build_tasks"
    assert detail["warnings"] == build["warnings"]


def test_error_envelopes_conform(client: TestClient) -> None:
    not_found = client.get("/api/v1/courses/crs_nope")
    assert not_found.status_code == 404
    validate_contract(not_found.json(), "error.schema.json")
    assert not_found.json()["error"]["code"] == "NOT_FOUND"
    assert not_found.json()["error"]["request_id"]

    method_not_allowed = client.get("/api/v1/exports")  # 仅 POST
    assert method_not_allowed.status_code == 405
    validate_contract(method_not_allowed.json(), "error.schema.json")

    invalid = client.get("/api/v1/search")  # 缺必需查询参数 q
    assert invalid.status_code == 400
    validate_contract(invalid.json(), "error.schema.json")
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"

    auth_expired = client.post(f"/api/v1/connections/{AUTH_REQUIRED_CONNECTION}/sync")
    assert auth_expired.status_code == 409
    validate_contract(auth_expired.json(), "error.schema.json")
    error = auth_expired.json()["error"]
    assert error["code"] == "AUTH_EXPIRED"
    assert error["retryable"] is False
    assert error["details"]["action"]  # recovery_action（含 entry_url，不循环重试）


def _operations(paths: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in paths.items()
        for method in item
        if method in HTTP_METHODS
    }


def test_openapi_route_coverage_bidirectional() -> None:
    """契约 (openapi.yaml) 与实现 (FastAPI) 的 (METHOD, path) 集合必须完全一致。

    缺项（契约→实现）会让客户端在联调中撞 405/404——按实现补齐并锁定；
    多出项（实现→契约）是契约-实现漂移——经 CR（兼容新增）补录，不得私留。
    """
    import yaml

    from app.main import app

    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    contract_ops = _operations(spec["paths"])
    agent_spec = yaml.safe_load((OPENAPI_PATH.parent / "agent-openapi.yaml").read_text(encoding="utf-8"))
    agent_ops = _operations(agent_spec["paths"])
    assert not contract_ops & agent_ops, "新增 agent 契约不能重定义已有端点"
    contract_ops |= agent_ops
    impl_ops = _operations(app.openapi()["paths"])

    missing = sorted(contract_ops - impl_ops)
    extra = sorted(impl_ops - contract_ops)
    assert not missing, f"契约定义但实现缺失（联调将 405/404）：{missing}"
    assert not extra, f"实现多出但契约未定义（须按 CR 流程补录）：{extra}"
