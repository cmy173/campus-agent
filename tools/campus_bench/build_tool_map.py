"""Export the registered Campus Agent tools and their benchmark crosswalk.

The inventory is generated from the same SPECS registry used by the live runtime.
Runtime-native tools are listed separately because they are supplied by the pinned
Harness/provider or user-configured MCP servers, not by the API domain registry.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "apps" / "api"
LEARNING_SRC = ROOT / "packages" / "learning" / "src"
# Importing the live registry also imports router adapters which use the API
# package root (for example ``learning``). Make the documented root-level
# command reproducible without requiring callers to hand-set PYTHONPATH.
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
if str(LEARNING_SRC) not in sys.path:
    sys.path.insert(0, str(LEARNING_SRC))
OUT = ROOT / "docs" / "campus-agent-tool-map.md"
JSON_OUT = ROOT / "docs" / "campus-agent-tool-map.json"
CATALOG = ROOT / "tools" / "campus_bench" / "scenarios.json"


def family(name: str) -> str:
    if name.startswith("campus."):
        return "校园门户 / 教务 / 认证"
    if name.startswith("connections."):
        return "学在浙大 / 智云课堂连接器"
    if name.startswith("courses."):
        return "课程"
    if name.startswith("assignments."):
        return "作业"
    if name.startswith("materials."):
        return "课程资料"
    if name.startswith("tasks."):
        return "学习任务"
    if name.startswith("plans.") or name == "availability.read":
        return "学习计划"
    if name.startswith("jobs."):
        return "后台任务"
    if name.startswith("reviews."):
        return "复习卷"
    if name == "artifacts.export":
        return "文件与导出"
    return "其他领域"


def operational_context(name: str) -> dict:
    local = "本地单用户工作区数据库；按会话课程/连接/job授权过滤。"
    result = {
        "permission_preconditions": "本地 Agent 会话与服务端范围授权；模型参数不能扩权。",
        "execution_path": "Agent API → Harness 工具注册 campus-plugin → /api/v1/agent/internal/tool → agent_bridge.tools.execute → 对应 router/action/store；结果按结构化 envelope 回传 Harness。",
        "error_timeout_retry_cancel": "严格 Pydantic 输入校验；错误以结构化 code/status 返回。Harness 域工具超时 20 秒；工具调用不自动重试，可信 call_id 可重放缓存结果；中止 turn 由运行时 signal 传播。后台任务用 jobs.read/jobs.cancel 有界观察/协作取消。",
        "observability": "API 路径记录 tool_started/tool_result、脱敏参数键、工具名、结果 envelope 摘要和 duration_ms；Token 与精确模型耗时取决于 provider 路径。",
        "tests": ["apps/api/tests/test_agent_tools.py", "apps/api/tests/test_agent_tool_budgets.py", "apps/api/tests/test_campus_bench_harness.py"],
        "real_validation": "本地工具/契约 mock 覆盖不等于真实校园端到端验证；见场景 crosswalk 与实测报告。",
    }
    if name in {"campus.status", "campus.query", "campus.sync", "campus.academics",
                "campus.notice.read", "campus.imported.read", "campus.webvpn.entry", "campus.notice.import"}:
        result["data_source_freshness"] = "CampusWorkspace / 迁入的 ZDBK 适配器；query 读缓存，sync 访问校园端并更新缓存，结果携带 fetched_at、freshness、completeness 与 warnings；失败保留旧缓存。"
        result["permission_preconditions"] = "教务与学院数据依赖已授权的平台会话及相应本地范围；campus.sync 是受限只读远端访问；不读取浏览器 Cookie、不向模型提供密码；验证码/MFA/拒绝/限流时停止自动恢复。"
        result["tests"] += ["apps/api/tests/test_campus_readonly_boundary.py", "apps/api/tests/test_campus_credentials.py", "apps/api/tests/test_campus_auto_login_cas.py", "apps/api/tests/test_campus_webvpn_channel.py"]
    elif name.startswith("connections."):
        result["data_source_freshness"] = "学在浙大 / 智云课堂连接器；status 读本地状态，courses 远端分页读取不落库，sync 建立只读同步 job 并写本地缓存；只把实际观察到的覆盖与时间标为新鲜。"
        result["permission_preconditions"] = "指定或已授权 connection；远端课程列表/同步需要当前有效连接器会话；失效返回 AUTH_REQUIRED，受控窗口登录后再继续。课程导入是本地绑定，不是学校提交。"
        result["tests"] += ["apps/api/tests/test_agent_connection_sync_tool.py", "apps/api/tests/test_campus_browser_bridge.py", "apps/api/tests/test_campus_persistent_grants.py"]
    elif name in {"courses.list", "assignments.list", "materials.list", "materials.search", "materials.read", "campus.notice.import", "campus.imported.read"}:
        result["data_source_freshness"] = local + "内容覆盖不证明学校源全量；资源版本、解析状态、来源引用及 next_cursor 用来表达实际范围。"
        result["permission_preconditions"] = "会话授权课程范围；资料 read/search 只读取已导入、未归档且版本匹配的本地数据。"
        result["tests"] += ["apps/api/tests/test_agent_tools.py", "apps/api/tests/test_campus_migration.py"]
    elif name.startswith(("tasks.", "plans.")) or name == "availability.read":
        result["data_source_freshness"] = local + "计划读写记录版本与动作回执；本地学习结果不代表学校平台提交。"
        result["permission_preconditions"] = "完整本地工作区/会话范围；plans.apply 与 plans.revoke 只能使用用户已在可信界面接受的 action_id；tasks.feedback 仅记录用户明确报告。"
        result["tests"] += ["apps/api/tests/test_agent_plan_actions.py", "apps/api/tests/test_plan_campus_constraints.py"]
    elif name.startswith("jobs."):
        result["data_source_freshness"] = local + "job 状态从持久作业记录读取；queued/running/succeeded/failed/cancelled/partial_failed 分别表达真实进度。"
        result["permission_preconditions"] = "仅可读取/取消本会话拥有或明确授权的 job。"
        result["tests"] += ["apps/api/tests/test_agent_job_actions.py"]
    elif name.startswith("reviews."):
        result["data_source_freshness"] = "本会话授权的课程资料与作业；生成时冻结材料版本快照，job 状态独立读取；内容版本与 snapshot fingerprint 一起验证。"
        result["permission_preconditions"] = "preflight 无副作用；generate 仅在用户明确要求时调用，使用服务端 provider 配置并受额度限制；重复请求有幂等约束。"
        result["tests"] += ["apps/api/tests/test_agent_tools.py", "apps/api/tests/test_agent_job_actions.py"]
    elif name == "artifacts.export":
        result["data_source_freshness"] = "已整理的结构化输入；服务端固定 renderer；返回实际生成文件、大小及磁盘 SHA-256。"
        result["permission_preconditions"] = "只允许本会话文件区；schema 不接受代码、命令、输出路径、模块或远端 URL。"
        result["tests"] += ["apps/api/tests/test_artifacts_export.py"]
    else:
        result["data_source_freshness"] = "本地状态/缓存；以 tool response 的 source、fetched_at、freshness、coverage、missing 和 next_cursor 为准。"
    result["side_effect_boundary"] = {
        "read": "无业务写入；可能读取缓存或外部只读页面。",
        "write": "按工具说明执行本地写入、排队、导出或已授权操作；不得据受理状态声称已完成。",
    }["write" if name in {"campus.notice.import", "connections.courses.import", "connections.sync", "plans.preview", "plans.apply", "plans.revoke", "tasks.build", "tasks.feedback", "jobs.cancel", "artifacts.export", "reviews.generate", "reviews.export"} else "read"]
    return result


def scenario_crosswalk(name: str, scenarios: list[dict]) -> list[dict]:
    aliases = {
        "campus.status": {"campus", "campus.status", "status"},
        "campus.query": {"campus", "campus.query", "academic", "schedule", "notice"},
        "campus.sync": {"campus", "sync", "academic", "schedule"},
        "campus.academics": {"academic", "schedule"},
        "campus.webvpn.entry": {"webvpn"},
        "campus.notice.read": {"notice"},
        "connections.status": {"connection", "campus", "browser"},
        "connections.courses": {"connection", "campus", "browser"},
        "connections.courses.import": {"connection", "campus"},
        "connections.sync": {"connection", "campus", "sync"},
        "courses.list": {"course", "courses", "campus", "academic"},
        "assignments.list": {"assignment", "assignments"},
        "materials.list": {"material", "materials"},
        "materials.search": {"material", "materials", "search"},
        "materials.read": {"material", "materials", "read"},
        "tasks.list": {"task", "tasks"},
        "tasks.build": {"task", "tasks"},
        "plans.preview": {"plan", "plans", "schedule"},
        "plans.read": {"plan", "plans"},
        "availability.read": {"plan", "plans", "schedule"},
        "jobs.read": {"job", "jobs"},
        "jobs.cancel": {"job", "jobs"},
        # Course material inputs are dependencies of review generation, but an
        # assignment/planning scenario does not test review-tool selection.
        "reviews.preflight": {"review", "exam", "study"},
        "reviews.generate": {"review", "exam", "study"},
        "reviews.read": {"review", "exam"},
        "reviews.export": {"review", "exam", "export"},
    }
    result = []
    for scenario in scenarios:
        required = set(scenario.get("required_tools") or [])
        terms = aliases.get(name, set())
        if name in required:
            result.append({"id": scenario["id"], "coverage_type": "exact tool requirement", "status": scenario["run_status"],
                           "capability_state": scenario["capability_state"]})
        elif required & terms:
            result.append({"id": scenario["id"], "coverage_type": "family alias only; not exact tool oracle", "status": scenario["run_status"],
                           "capability_state": scenario["capability_state"]})
    return result


def real_trace_observations(model_to_domain: dict[str, str] | None = None) -> dict:
    """Summarize only sanitized real-api benchmark traces, not code registration."""
    stats: dict[str, dict] = {}
    batches: set[str] = set()
    runs = 0

    def entry(name: str) -> dict:
        canonical = (model_to_domain or {}).get(name, name)
        return stats.setdefault(canonical, {"visible_runs": set(), "selected_runs": set(), "executed_runs": set(),
                                            "selected_calls": 0, "model_selected_runs": set(),
                                            "model_selected_calls": 0, "executed_call_ids": set()})

    def record_execution(name: str, run_key: str, call_id, seq):
        observed = entry(name)
        observed["executed_runs"].add(run_key)
        unique_call = str(call_id) if call_id is not None else f"{run_key}/seq-{seq}"
        observed["executed_call_ids"].add(unique_call)

    for path in sorted((ROOT / "var" / "campus-bench").glob("*/runs/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("environment") != "real_api":
            continue
        runs += 1
        batch_id = path.parents[1].name
        trace = record.get("trace") if isinstance(record.get("trace"), list) else []
        saw_native_model = False
        for event in trace:
            if not isinstance(event, dict):
                continue
            kind, data = event.get("kind"), event.get("data") or {}
            if kind == "model_call":
                snapshot = data.get("tool_schema_snapshot") or {}
                schemas = snapshot.get("schemas") if isinstance(snapshot, dict) else None
                if isinstance(schemas, list):
                    saw_native_model = True
                    for schema in schemas:
                        name = schema.get("name") if isinstance(schema, dict) else None
                        if isinstance(name, str):
                            entry(name)["visible_runs"].add(batch_id + "/" + path.stem)
                for name in data.get("selected_tools", []):
                    if isinstance(name, str):
                        observed = entry(name)
                        observed["model_selected_runs"].add(batch_id + "/" + path.stem)
                        observed["model_selected_calls"] += 1
            elif kind == "tool_started":
                name = data.get("name")
                if isinstance(name, str):
                    record_execution(name, batch_id + "/" + path.stem, data.get("call_id"), event.get("seq"))
            elif kind == "harness_event":
                inner = data.get("event") if isinstance(data, dict) else None
                inner_data = inner.get("data") if isinstance(inner, dict) else None
                if isinstance(inner_data, dict) and inner.get("type") == "tool/call":
                    name = inner_data.get("name")
                    if isinstance(name, str):
                        observed = entry(name)
                        observed["selected_runs"].add(batch_id + "/" + path.stem)
                        observed["selected_calls"] += 1
                        record_execution(name, batch_id + "/" + path.stem,
                                         inner_data.get("callId") or inner_data.get("call_id"), event.get("seq"))
        if saw_native_model:
            batches.add(batch_id)
    compact = {name: {"visible_runs": len(row["visible_runs"]), "selected_runs": len(row["selected_runs"]),
                      "selected_calls": row["selected_calls"],
                      "model_selected_runs": len(row["model_selected_runs"]),
                      "model_selected_calls": row["model_selected_calls"],
                      "executed_runs": len(row["executed_runs"]),
                      "executed_calls": len(row["executed_call_ids"])} for name, row in stats.items()}
    return {"real_api_run_count_scanned": runs, "batches_with_schema_snapshots": len(batches),
            "tools": compact}


def runtime_schema_drift_observations(specs: list[dict], runs_root: Path | None = None) -> dict:
    """Compare historical model-visible schemas with the current source registry.

    Only aggregate counts and differing fields are exported; prompts, arguments,
    and tool results are not read into the output. Historical differences do not
    prove a live-runtime defect: source fingerprints may be absent, and even a
    matching Agent runtime fingerprint does not fingerprint the API schema
    registry served by a separately running API process.
    """
    root = runs_root or (ROOT / "var" / "campus-bench")
    source = {spec["model_name"]: spec for spec in specs}
    stats = {name: {"snapshot_observations": 0, "matching": 0, "drifting": 0,
                    "differing_fields": {}, "observations_with_runtime_fingerprint": 0,
                    "observations_without_runtime_fingerprint": 0} for name in source}
    batch_fingerprints: dict[str, str | None] = {}
    for path in sorted(root.glob("*/runs/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("environment") != "real_api":
            continue
        batch_id = path.parent.parent.name
        if batch_id not in batch_fingerprints:
            try:
                manifest = json.loads((path.parent.parent / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            fingerprint = manifest.get("agent_runtime_source_sha256")
            batch_fingerprints[batch_id] = fingerprint if isinstance(fingerprint, str) and fingerprint else None
        has_fingerprint = batch_fingerprints[batch_id] is not None
        trace = record.get("trace") if isinstance(record.get("trace"), list) else []
        for event in trace:
            if not isinstance(event, dict) or event.get("kind") != "model_call":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            snapshot = data.get("tool_schema_snapshot")
            schemas = snapshot.get("schemas") if isinstance(snapshot, dict) else None
            if not isinstance(schemas, list):
                continue
            for observed in schemas:
                if not isinstance(observed, dict):
                    continue
                name = observed.get("name")
                spec = source.get(name)
                if spec is None:
                    continue
                row = stats[name]
                row["snapshot_observations"] += 1
                provenance_key = ("observations_with_runtime_fingerprint" if has_fingerprint
                                  else "observations_without_runtime_fingerprint")
                row[provenance_key] += 1
                differing = []
                if observed.get("description") != spec.get("description"):
                    differing.append("description")
                if observed.get("parameters") != spec.get("input_schema"):
                    differing.append("parameters")
                if differing:
                    row["drifting"] += 1
                    for field in differing:
                        row["differing_fields"][field] = row["differing_fields"].get(field, 0) + 1
                else:
                    row["matching"] += 1
    for row in stats.values():
        row["comparison_scope"] = "historical_snapshot_vs_current_registry; not a live root-cause finding"
        row["current_live_runtime_verified"] = False
    return {name: row for name, row in stats.items() if row["snapshot_observations"]}


def domain_tool_observation(trace_tools: dict, spec: dict) -> dict:
    """Return trace counters keyed by canonical domain name, not model schema name."""
    return trace_tools.get(spec["name"], {})


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from apps.api.app.agent_bridge.tools import schemas

    scenarios = json.loads(CATALOG.read_text(encoding="utf-8"))["scenarios"]
    specs = schemas()
    model_to_domain = {spec["model_name"]: spec["name"] for spec in specs}
    observations = real_trace_observations(model_to_domain)
    schema_drift = runtime_schema_drift_observations(specs)
    trace_tools = observations["tools"]
    inventory = []
    for spec in specs:
        params = spec["input_schema"].get("properties", {})
        real_validation = {
            "campus_campus_status": "真实 Agent trace 观察到；未校验其中每项私有连接状态",
            "campus_connections_status": "真实 Agent trace 观察到；连接器当前可用性仍随时间变化",
            "campus_campus_sync": "真实直接 API 只读探测成功（18 门课、fresh/complete）；不代表 Agent 自主选工具已验证",
            "campus_courses_list": "仅隔离脚本模型 E2E 的合成数据库验证；未真实验证",
        }.get(spec["model_name"], "未在真实 Agent trace 中验证")
        observed = domain_tool_observation(trace_tools, spec)
        selected_runs = observed.get("selected_runs", 0)
        model_selected_runs = observed.get("model_selected_runs", 0)
        executed_runs = observed.get("executed_runs", 0)
        visible_runs = observed.get("visible_runs", 0)
        if visible_runs or selected_runs or executed_runs:
            if real_validation == "未在真实 Agent trace 中验证":
                real_validation = ""
            prefix = (real_validation + "；") if real_validation else ""
            real_validation = (prefix + f"真实 Agent 轨迹：模型可见 {visible_runs} 次运行、Harness 收到 tool/call {selected_runs} 次运行"
                               f"（模型 selected_tools 元数据 {model_selected_runs} 次运行）、"
                               f"执行 {observed.get('executed_calls', 0)} 次调用 / {executed_runs} 次运行；"
                               "不代表每次返回内容或任务目标均已独立验证。")
        inventory.append({
            "tool_name": spec["name"], "model_name": spec["model_name"],
            "schema": spec["input_schema"], "description": spec["description"],
            "domain": family(spec["name"]), "effects_declared_by_registry": spec["effects"],
            "code_defined": True, "model_schema_registered_by_plugin": True,
            "model_visible_in_a_specific_call": visible_runs,
            # Trace observations are canonicalized from provider/model names to
            # domain tool names; read the same key used for visibility/execution.
            "real_agent_selected_run_count": selected_runs,
            "real_agent_model_selected_run_count": model_selected_runs,
            "real_agent_executed_run_count": executed_runs,
            "runtime_schema_drift": schema_drift.get(spec["model_name"], {
                "snapshot_observations": 0, "matching": 0, "drifting": 0,
                "differing_fields": {}}),
            "real_environment_validation": real_validation,
            "parameters_summary": {key: {"type": value.get("type"), "required": key in spec["input_schema"].get("required", []),
                "default": value.get("default"), "enum": value.get("enum"), "limits": {x: value.get(x) for x in ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "pattern") if x in value},
                "description": value.get("description")} for key, value in params.items()},
            "benchmark_crosswalk": scenario_crosswalk(spec["name"], scenarios),
            **operational_context(spec["name"]),
        })
    native = [
        {"tool_name": "web_search", "domain": "公网搜索", "status": "runtime-native; observed in BAS-02/BAS-03", "path": "Harness native provider web tool → tool/call/tool/result event; provider owns query execution; results returned into next model step.", "limits": "Provider supplies schema and executes queries; no credentialed access intended.", "real_trace_observation": trace_tools.get("web_search", {}), "scenarios": ["BAS-02", "BAS-03", "BAS-05", "BUS-04", "MULTI-03", "MULTI-04"]},
        {"tool_name": "web_fetch", "domain": "公网网页读取", "status": "runtime-native; observed in BAS-02/BAS-03/BUS-04", "path": "Harness native provider web tool; web-recovery.mjs preserves WEB_BLOCKED_URL and gives alternate-source guidance; same exact hostname is short-circuited after an address-policy denial for the rest of that Agent turn, then cleared at agent/turn-stopping; page text enters following model step.", "limits": "Provider owns hostname resolution and fetch behavior; retry suppression cannot fix DNS/address-policy denial or establish source freshness. It is per Agent turn and only exact hostnames; no domain allowlist or IP override is used.", "real_trace_observation": trace_tools.get("web_fetch", {}), "scenarios": ["BAS-02", "BAS-03", "BAS-04", "BAS-05", "ERR-01"]},
        {"tool_name": "publish_file", "domain": "会话文件发布", "status": "fixed runtime registration; live Agent API path", "path": "campus-plugin.mjs → /api/v1/agent/internal/files/publish → turn workspace and conversation file store.", "limits": "Path is restricted to current turn workspace or selected attachments; returns name/size/hash; no content is added to task journal beyond metadata.", "scenarios": ["artifact/export tasks; no explicit catalog scenario"]},
        {"tool_name": "load_tools", "domain": "动态工具发现", "status": "runtime registration and API schema deferral; TOOL-REV-01/MULTI-02 scripted Harness A/B passed 3/3 per condition", "path": "CampusModelAdapter → API internal model gateway filters `campus_reviews` schemas until a tool-role `loaded_groups` receipt; `load_tools` then updates schemas on later provider requests. API gate `CAMPUS_BENCH_ALLOW_TOOL_GROUPS=1` and runner option `--defer-tool-group campus_reviews` enable only the benchmark conversation; child allowlist carries the same group so loader enum can expose it.", "limits": "Schema filtering does not grant/revoke executor permissions. Scripted mock A/B only: TOOL-REV-01 adds one model round trip and about 0.58 s mean in this local scenario; MULTI-02 preserved calls with overlapping latency ranges. No real-model A/B yet. Browser, MCP and upstream tool lists vary by runtime configuration.", "scenarios": ["BAS-04", "BUS-06", "BUS-07", "BUS-08", "ERR-08", "TOOL-REV-01", "MULTI-02"]},
        {"tool_name": "browser MCP tools", "domain": "登录态浏览器", "status": "code policy and contract tests; no real benchmark trace verified", "path": "load_tools(browser) → mcp__campus_browser__* → browser bridge/host with capability policy.", "limits": "Actual tool names depend on browser MCP registry; unsafe submit/delete/payment operations blocked or require explicit policy. Real login/session/browser grant is separate from saved credentials.", "scenarios": ["BAS-04", "BUS-06", "BUS-07", "BUS-08", "ERR-01", "ERR-08"]},
        {"tool_name": "job_list/job_output/workflow", "domain": "通用后台任务", "status": "group is dynamically available; not verified as campus-job executor", "path": "load_tools(jobs) → upstream Harness tools; campus jobs must use campus_jobs_read/cancel.", "limits": "Do not mix process/workflow jobs with campus API job IDs.", "scenarios": ["ERR-06", "INT-05"]},
        {"tool_name": "read/write/edit/glob/grep/terminal", "domain": "本地文件与工作区", "status": "runtime-native tools; some names seen in smoke inventory, real-call exposure varies by task", "path": "Harness tool registry and per-turn workspace; publish_file delivers final artifact.", "limits": "Schema and per-turn policy depend on current upstream runtime; not enumerated by API SPECS registry.", "scenarios": ["document and artifact tasks; catalog currently has no focused file tool scenario"]},
        {"tool_name": "subagent/team tools", "domain": "子 Agent 与并行协作", "status": "group is dynamically available; no campus task trace verified", "path": "load_tools(team) → upstream team/subagent tools.", "limits": "Parallel work is runtime-specific; child tool permissions and completion need trace verification.", "scenarios": ["no focused scenario"]},
        {"tool_name": "user-configured MCP tools/resources", "domain": "外部 MCP", "status": "dynamic; exact inventory unavailable without runtime MCP registry", "path": "load_tools(mcp) → MCP tool/resource list and read operations.", "limits": "Each configured server changes names, schema, authentication and side-effect risk; current repo cannot claim a fixed complete list.", "scenarios": ["no focused scenario"]},
    ]
    payload = {"schema_version": 1, "generated_from": "apps.api.app.agent_bridge.tools.SPECS via tools.schemas()", "domain_tool_count": len(inventory),
               "scenario_count": len(scenarios), "experimental_tool_exposure": {"group": "campus_reviews", "default": "all-visible", "enablement": "API env CAMPUS_BENCH_ALLOW_TOOL_GROUPS=1 plus runner --defer-tool-group campus_reviews", "status": "TOOL-REV-01 and MULTI-02 scripted Harness control/treatment each passed 3/3; no real-model A/B"},
               "real_trace_observations": observations,
               "runtime_schema_drift_observations": {
                   "compared_with": "current_source_registry",
                   "tools_with_snapshots": len(schema_drift),
                   "drifting_observations": sum(row["drifting"] for row in schema_drift.values()),
                   "tools": schema_drift},
               "runtime_native_tools": native, "tools": inventory}
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = ["# Campus Agent 全工具地图", "", f"由 `python tools/campus_bench/build_tool_map.py` 从真实注册表导出。领域工具 {len(inventory)} 个；Benchmark 场景 {len(scenarios)} 个。真实 API 日志扫描 {observations['real_api_run_count_scanned']} 次运行，其中 {observations['batches_with_schema_snapshots']} 个批次已捕获调用级 schema。", "",
            "## 运行时边界", "", "领域工具由 `tools.py:SPECS` 注册，名字映射到 Harness 的 `campus_*` schema。Native/provider/MCP 工具由 SDK 和运行时配置注入，静态源码不能枚举所有用户 MCP 工具；清单将其单独列出，并区分代码注册、模型 schema 注册与真实轨迹观察。内部 provider 请求会在模型事件中记录脱敏、限长的可见 schema 快照；超限时保留摘要哈希，不将模型本轮可见范围外推为全局能力。", "",
            f"历史 schema 对照把真实 Agent 轨迹中的模型调用快照与当前工作树工具注册表逐字段比对：{sum(row['snapshot_observations'] for row in schema_drift.values())} 个工具级观测中，{sum(row['drifting'] for row in schema_drift.values())} 个有描述或参数差异。这是跨历史版本的差异统计，不是当前运行故障结论；工具级记录包含对应批次是否有 Agent runtime 源指纹，但该指纹不覆盖独立 API 进程中的 schema 注册表，因此当前服务匹配仍标记为未验证。字段级差异见 JSON `runtime_schema_drift_observations`。", "",
            "主路径：用户输入 → Agent API turn → Harness/DeepSeek SDK → provider/native `tool/call` → `campus-plugin.mjs` → `/api/v1/agent/internal/tool` → `tools.execute` → router/action/store 或 CampusWorkspace → 结构化工具结果 → 下一模型步。Native 搜索/网页读取绕过 API 域工具路由，凭 Harness 事件追踪。", "",
            "## 工具及 Benchmark 覆盖", "", "| 工具 | 模型名 | 领域 | 注册效果 | 参数边界 | 数据/ freshness | 权限及副作用 | 关联场景 | 真实运行可见/Harness请求/执行调用/执行运行数 | 真实验证备注 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for row in inventory:
        param_bits = []
        for key, value in row["parameters_summary"].items():
            limits = ",".join(f"{k}={v}" for k, v in value["limits"].items())
            enum = f" enum={','.join(map(str, value['enum']))}" if value["enum"] else ""
            default = f" default={value['default']}" if value["default"] is not None else ""
            param_bits.append(f"`{key}`{'*' if value['required'] else ''}{enum}{default}{(' '+limits) if limits else ''}")
        scenarios_text = ", ".join(f"{x['id']} ({x['coverage_type']}; {x['status']})" for x in row["benchmark_crosswalk"]) or "无直接匹配"
        trace_counts = f"{row['model_visible_in_a_specific_call']}/{row['real_agent_selected_run_count']}/{trace_tools.get(row['tool_name'], {}).get('executed_calls', 0)}/{row['real_agent_executed_run_count']}"
        rows.append("| `" + row["tool_name"] + "` | `" + row["model_name"] + "` | " + row["domain"] + " | " + row["effects_declared_by_registry"] + " | " + ("; ".join(param_bits) or "无参数") + " | " + row["data_source_freshness"] + " | " + row["side_effect_boundary"] + " | " + scenarios_text + " | " + trace_counts + " | " + row["real_environment_validation"] + " |")
    rows += ["", "`*` 表示必填。完整 schema、参数描述、前置条件、执行/结果回传路径、错误/超时/取消、观测指标和测试模块见 `docs/campus-agent-tool-map.json`。场景 crosswalk 会区分精确工具约束与家族别名；家族别名不是精确工具选择 oracle。轨迹数字依次为模型可见运行数、Harness 实际收到 `tool/call` 的运行数、实际执行调用数、发生实际执行的运行数；模型请求的 `selected_tools` 元数据另存于 JSON 的 `model_selected_*` 字段。", "", "## Native 与动态工具", "", "| 工具/工具组 | 领域 | 状态 | 路径和边界 | 可见/Harness请求/执行调用/执行运行数 | 覆盖场景 |", "|---|---|---|---|---|---|---|"]
    for row in native:
        observed = row.get("real_trace_observation", {})
        counts = f"{observed.get('visible_runs', 0)}/{observed.get('selected_runs', 0)}/{observed.get('executed_calls', 0)}/{observed.get('executed_runs', 0)}"
        rows.append(f"| `{row['tool_name']}` | {row['domain']} | {row['status']} | {row['path']} {row['limits']} | {counts} | {', '.join(row['scenarios'])} |")
    rows += ["", "## 运行追踪覆盖限制", "", "- 域工具 journal 可记录工具名、调用参数、返回 envelope、错误状态和 duration；单任务预算/超时/完成状态由 benchmark runner 记录。", "- Native provider 成功/错误事件记录模型本轮可见名称、选择结果，以及经敏感字段脱敏、最多 64,000 字符的 schema 快照和 SHA-256；超出上限时仅记录摘要、数量和哈希。", "- Harness `tool/call`、`tool/result` 事件提供 native 工具执行轨迹；独立 native 工具精确耗时和每个外部人工步骤仍不可稳定观测。", "- 官方 provider 的 Token 用量由 assistant/message usage 事件提供；模型请求墙钟耗时来自 `llm/stream` 包络，包含 SDK stream 处理。", "- API 域错误会保留结构化错误码；浏览器/MCP 工具服务端错误受运行时配置影响，需通过当次 trace 归因。", "- 真实校园证据：本批只读 `campus.sync` 已返回 18 门课程、fresh/complete（无课程内容存入 benchmark）；真实 Agent traces 已调用 `campus.status`、`connections.status`、`web_search`、`web_fetch`。这些不证明其余工具可在真实校园环境工作。", "", "## 已知覆盖缺口", "", f"现有 {len(scenarios)} 场景中 ERR-04/05/07/08 因缺少故障注入、VPN 授权目标/切换夹具、DOM 变化 fixture、人工认证暂停恢复而 blocked；剩余场景虽标 runnable，并非都已有确定性 oracle 或做过真实测试。作业工具已有直接单测覆盖日期排序、稳定游标续页和参数边界；ERR-11 隔离 Harness E2E n=3 验证空缓存的 stale/partial 语义与结果回传。BUS-12 隔离 Harness E2E n=3 验证游标结果回传、续页和唯一性；真实模型选择/表达仍未验证，其他工具家族的缓存、参数、结果回传和写入授权覆盖仍不完整。", ""]
    OUT.write_text("\n".join(rows), encoding="utf-8")
    print(json.dumps({"domain_tools": len(inventory), "native_groups": len(native), "scenarios": len(scenarios), "markdown": str(OUT), "json": str(JSON_OUT)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
