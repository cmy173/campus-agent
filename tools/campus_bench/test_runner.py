import hashlib
import json
import time

from tools.campus_bench.runner import Api, CallBudget, ROOT_CAUSE_LABELS, api_preflight, api_source_gate, private_calls_may_have_returned_data, provider_preflight, tool_exposure_preflight, classify, diagnose, evaluate, retry_allowed, root_cause_attribution, source_grounding, summarize_run, target_page_extraction, run_live_tool_probe, privacy_event, execution_dimensions, confirmation_summary, campus_entry_grounding, campus_credentials_grounding, assignment_date_grounding, assignment_next_by_course_grounding, course_directory_grounding, apply_redacted_assignment_oracle, apply_redacted_course_directory_oracle, apply_redacted_public_source_oracle, apply_redacted_forbidden_scope_evidence, public_source_oracle_summary, safe, metrics, tool_selection_evidence, tool_error_evidence, load_catalog, mock_e2e_layer_summary, schema_snapshot_summary, schema_factor_comparison, execute_agent_turn, pending_user_actions, agent_runtime_source_sha256, event_latency_ms, tool_availability_checks, agent_api_source_sha256, api_agent_source_fingerprint_status, compare_model_schemas_to_local, initialize_layer_batch, select_run_batch_dir


def test_api_preflight_records_loaded_agent_code_match():
    class ApiResponse:
        def call(self, method, path):
            assert (method, path) == ("GET", "/api/v1/health")
            return 200, {"status": "ok", "agent_source_sha256": agent_api_source_sha256()}

    result = api_preflight(ApiResponse())
    assert result["status"] == "passed"
    assert result["agent_source_status"] == "matched"
    assert result["agent_source_sha256_loaded"] == result["agent_source_sha256_expected"]


def test_api_preflight_flags_stale_or_legacy_agent_code_without_hiding_api_health():
    class ApiResponse:
        def __init__(self, payload): self.payload = payload
        def call(self, method, path): return 200, self.payload

    mismatch = api_preflight(ApiResponse({"status": "ok", "agent_source_sha256": "0" * 64}))
    legacy = api_preflight(ApiResponse({"status": "ok"}))
    assert mismatch["status"] == legacy["status"] == "passed"
    assert mismatch["agent_source_status"] == "mismatch"
    assert legacy["agent_source_status"] == "not_reported"


def test_api_agent_source_fingerprint_tracks_executor_source_roots(tmp_path):
    roots = [tmp_path / "apps" / "api" / "app",
             tmp_path / "connectors" / "campus",
             tmp_path / "packages" / "learning" / "src" / "learning"]
    for root in roots:
        root.mkdir(parents=True)
        (root / "module.py").write_text("value = 1", encoding="utf-8")
    first = agent_api_source_sha256(tmp_path)
    assert first == agent_api_source_sha256(tmp_path)
    (roots[1] / "module.py").write_text("value = 2", encoding="utf-8")
    assert agent_api_source_sha256(tmp_path) != first


def test_api_agent_source_fingerprint_rejects_missing_invalid_and_mismatched_hashes():
    assert api_agent_source_fingerprint_status({}, "a" * 64)["status"] == "not_reported"
    assert api_agent_source_fingerprint_status({"agent_source_sha256": "unknown"}, "a" * 64)["status"] == "invalid"
    assert api_agent_source_fingerprint_status({"agent_source_sha256": "b" * 64}, "a" * 64)["status"] == "mismatch"
    assert api_agent_source_fingerprint_status({"agent_source_sha256": "a" * 64}, "a" * 64)["status"] == "matched"


def test_api_source_gate_blocks_unmatched_and_allows_explicit_diagnostic_override():
    matched = api_source_gate({"agent_source_status": "matched"})
    missing = api_source_gate({"agent_source_status": "not_reported"})
    stale = api_source_gate({"agent_source_status": "mismatch"}, allow_unmatched=True)
    assert matched["status"] == "passed" and not matched["override"]
    assert missing["status"] == "blocked" and not missing["override"]
    assert stale["status"] == "overridden" and stale["override"]


def test_tool_availability_checked_on_every_model_call_and_distinguishes_dynamic_load():
    scenario = {"required_tools": ["campus.credentials.status", "courses.list"]}
    events = [
        {"seq": 1, "kind": "model_call", "data": {"tool_schema_snapshot": {
            "truncated": False, "sha256": "a", "schemas": [{"name": "campus_courses_list"}]}}},
        {"seq": 2, "kind": "model_call", "data": {"tool_schema_snapshot": {
            "truncated": False, "sha256": "b", "schemas": [
                {"name": "campus_courses_list"}, {"name": "campus_campus_credentials_status"}]}}},
    ]
    result = tool_availability_checks(scenario, events)
    assert result["status"] == {"campus.credentials.status": "loaded_later", "courses.list": "available_first_call"}
    assert result["unavailable_count"] == 0
    assert len(result["model_calls"]) == 2


def test_tool_availability_does_not_call_absence_from_truncated_schema_unavailable():
    result = tool_availability_checks({"required_tools": ["courses.list"]}, [
        {"seq": 1, "kind": "model_call", "data": {"tool_schema_snapshot": {
            "truncated": True, "sha256": "a", "schemas": []}}},
    ])
    assert result["status"]["courses.list"] == "not_observed"
    assert result["unavailable_count"] == 0


def test_tool_availability_marks_exact_missing_tool_only_with_complete_snapshots():
    result = tool_availability_checks({"required_tools": ["courses.list"]}, [
        {"seq": 1, "kind": "model_call", "data": {"tool_schema_snapshot": {
            "truncated": False, "sha256": "a", "schemas": [{"name": "web_search"}]}}},
    ])
    assert result["status"]["courses.list"] == "unavailable"
    assert result["unavailable_count"] == 1


def test_tool_availability_resolves_benchmark_alias_to_concrete_visible_candidates():
    scenario = {"required_tools": ["search"], "eval": {"tools_any": [
        "campus.query", "campus.sync", "web_search", "web_fetch"]}}
    result = tool_availability_checks(scenario, [{"seq": 1, "kind": "model_call", "data": {
        "tool_schema_snapshot": {"truncated": False, "sha256": "a", "schemas": [
            {"name": "campus_campus_query"}, {"name": "web_search"}]}}}])
    assert result["required_alias_groups"][0]["alias"] == "search"
    assert result["required_alias_groups"][0]["status"] == "available_first_call"
    assert result["required_alias_groups"][0]["visible_candidates_by_call"] == [["campus.query", "web.search"]]
    assert result["unavailable_count"] == 0


def test_campus_notice_detail_gap_is_explicitly_not_runnable_or_counted_as_supported():
    scenario = next(row for row in load_catalog()["scenarios"] if row["id"] == "BAS-06")
    assert scenario["capability_state"] == "expected_missing"
    assert scenario["run_status"] == "blocked_precondition"
    assert scenario["run_block_reason"]
    assert "detail_redirected_to_login" in scenario["evidence"]


def test_layers_initialization_creates_manifest_and_preserves_existing_batch(tmp_path):
    batch = tmp_path / "new-layer-batch"
    manifest_path = initialize_layer_batch(batch, "new-layer-batch", "all")
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert first["environment"] == "offline_contract_layers"
    assert first["layer_mode"] == "all"
    first["operator_note"] = "preserve"
    manifest_path.write_text(json.dumps(first), encoding="utf-8")
    initialize_layer_batch(batch, "new-layer-batch", "harness")
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["operator_note"] == "preserve"


def test_run_batch_dir_allows_new_named_batch_and_requires_existing_on_resume(tmp_path):
    new = select_run_batch_dir(tmp_path, "named-batch", False)
    assert new == tmp_path / "named-batch"
    assert not new.exists()
    new.mkdir()
    assert select_run_batch_dir(tmp_path, "named-batch", True) == new
    try:
        select_run_batch_dir(tmp_path, "../outside", False)
    except ValueError:
        pass
    else:
        raise AssertionError("batch-id path traversal must be rejected")


def test_tool_error_evidence_deduplicates_harness_and_domain_views_of_same_call():
    events = [
        {"kind": "tool_result", "data": {"name": "campus.sync", "call_id": "c1",
            "result": {"status": "error", "error": {"code": "INVALID_INPUT"}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 11, "type": "tool/call",
            "data": {"name": "campus_campus_sync", "callId": "c1"}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 12, "type": "tool/result",
            "sourceEventSeqs": [11], "data": {"message": {"isError": True,
                "content": [{"text": "INVALID_INPUT"}]}}}}},
    ]
    errors = tool_error_evidence(events)
    assert len(errors) == 1
    assert errors[0]["channel"] == "domain"
    assert errors[0]["error_code"] == "INVALID_INPUT"


def test_current_checkout_schema_comparison_detects_drift_without_retaining_schema_text():
    current = [{"model_name": "campus_campus_sync", "description": "No wait_seconds",
                "input_schema": {"type": "object", "properties": {"query": {"type": "object"}}}}]
    events = [{"seq": 4, "kind": "model_call", "data": {"tool_schema_snapshot": {
        "truncated": False, "schemas": [{"name": "campus.sync", "description": "old text",
            "parameters": {"type": "object", "properties": {"query": {"type": "object"}}}}]}}}]
    result = compare_model_schemas_to_local(events, current)
    assert result["status"] == "drifted"
    assert result["mismatched_tools"] == ["campus.sync"]
    assert result["per_tool"]["campus.sync"]["description_mismatches"] == 1
    assert result["per_tool"]["campus.sync"]["parameter_mismatches"] == 0
    assert "old text" not in json.dumps(result)


def test_current_checkout_schema_comparison_reports_matched_tool():
    row = {"description": "same", "type": "object", "properties": {"query": {"type": "object"}}}
    result = compare_model_schemas_to_local([{"kind": "model_call", "data": {
        "tool_schema_snapshot": {"truncated": False, "schemas": [{"name": "campus_campus_query",
            "description": "same", "parameters": row}]}}}],
        [{"model_name": "campus.query", "description": "same", "input_schema": row}])
    assert result["status"] == "matched"
    assert result["mismatched_tools"] == []


def test_root_cause_marks_required_tool_absence_as_confirmed_capability_gap():
    scenario = {"required_tools": ["courses.list"], "eval": {}}
    events = [{"seq": 1, "kind": "model_call", "data": {"tool_schema_snapshot": {
        "truncated": False, "sha256": "a", "schemas": [{"name": "web_search"}]}}}]
    result = root_cause_attribution(scenario, {"status": "succeeded"}, events,
                                   {"success": "失败", "observed_tools": []})
    candidates = [result["primary"], *result["alternatives"]]
    assert any(row["code"] == "A" and row["confidence"] == "confirmed" for row in candidates)


def test_event_latency_uses_journal_timestamps_without_event_payloads():
    events = [
        {"kind": "model_call", "created_at": "2026-10-17T00:00:02+00:00", "data": {"private": "ignored"}},
        {"kind": "runtime", "created_at": "2026-10-17T00:00:01+00:00", "data": {"private": "ignored"}},
    ]
    origin = 1792195200000

    assert event_latency_ms(events, origin) == 1000
    assert event_latency_ms(events, origin, "model_call") == 2000
    assert event_latency_ms(events, None) is None


def test_mock_e2e_comparison_summary_keeps_scripted_runs_separate(tmp_path):
    layer_dir = tmp_path / "layers"
    layer_dir.mkdir()
    (layer_dir / "mock-agent-e2e.json").write_text(json.dumps({
        "scenario_id": "TOOL-REV-01", "mode": "mock_scenario_e2e_not_real_model_or_campus_service",
        "repetitions": [
            {"status": "passed", "verified_success": True, "elapsed_ms": 100,
             "evidence": {"model_call_count": 3, "tool_result_count": 1,
                          "model_steps": [{"available_tool_count": 50}],
                          "review_tool_schema_hidden_initially": True,
                          "provider_received_tool_result_before_final": True,
                          "harness_tool_calls": ["load_tools", "campus_reviews_preflight"]}},
            {"status": "failed", "verified_success": False, "elapsed_ms": 200,
             "evidence": {"model_call_count": 6, "tool_result_count": 0,
                          "model_steps": [], "review_tool_schema_hidden_initially": True,
                          "provider_received_tool_result_before_final": False,
                          "harness_tool_calls": []}},
        ],
    }), encoding="utf-8")
    result = mock_e2e_layer_summary(tmp_path)
    assert result["repeat_count"] == 2
    assert result["passed_runs"] == 1 and result["failed_runs"] == 1
    assert result["model_calls"] == {"mean": 4.5, "range": [3, 6]}
    assert result["initial_visible_tool_schemas"] == {"mean": 50, "range": [50, 50]}
    assert result["review_schema_hidden_runs"] == 2


def test_agent_runtime_fingerprint_covers_runtime_source_not_fixtures_or_dependencies(tmp_path):
    source = tmp_path / "browser-host.mjs"
    source.write_text("export const value = 1", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.mjs").write_text("fixture one", encoding="utf-8")
    (tmp_path / "example.test.mjs").write_text("test one", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.mjs").write_text("dependency one", encoding="utf-8")
    first = agent_runtime_source_sha256(tmp_path)
    assert first == agent_runtime_source_sha256(tmp_path)
    (tmp_path / "tests" / "fixture.mjs").write_text("fixture two", encoding="utf-8")
    (tmp_path / "example.test.mjs").write_text("test two", encoding="utf-8")
    (tmp_path / "node_modules" / "dep.mjs").write_text("dependency two", encoding="utf-8")
    assert agent_runtime_source_sha256(tmp_path) == first
    source.write_text("export const value = 2", encoding="utf-8")
    assert agent_runtime_source_sha256(tmp_path) != first


def test_job_scenario_is_mock_runnable_but_not_a_real_agent_scenario():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "JOB-01")
    assert scenario["run_status"] == "runnable"
    assert scenario["modes"] == ["mock"]
    assert scenario["mock_fixture"] == "isolated-sync-job-transition"
    assert scenario["mock_status"] == "implemented_scripted_model"


def test_materials_scenario_has_real_and_isolated_mock_paths():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "BUS-03")
    assert scenario["run_status"] == "runnable"
    assert scenario["modes"] == ["real", "mock"]
    assert scenario["mock_fixture"] == "synthetic-course-material-search-read"
    assert scenario["mock_status"] == "implemented_scripted_model"


def test_multi_step_plan_scenario_has_isolated_preview_only_fixture():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "MULTI-02")
    assert scenario["modes"] == ["real", "mock"]
    assert scenario["mock_fixture"] == "isolated-materials-assignment-plan-preview"
    assert scenario["mock_status"] == "implemented_scripted_model"
    assert "不得应用" in scenario["intervention"] or "不得应用" in " ".join(scenario["success_conditions"])


def test_empty_materials_scenario_has_synthetic_mock_path():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "ERR-02")
    assert scenario["modes"] == ["real", "mock"]
    assert scenario["mock_fixture"] == "synthetic-empty-material-search"
    assert scenario["mock_status"] == "implemented_scripted_model"


def test_invalid_tool_parameter_recovery_is_a_mock_only_scenario():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "ERR-09")
    assert scenario["modes"] == ["mock"]
    assert scenario["mock_fixture"] == "scripted-invalid-material-read-recovery"
    assert scenario["mock_status"] == "implemented_scripted_model"
    assert "INVALID_INPUT" in scenario["success_conditions"][0]


def test_expired_session_stop_fixture_is_mock_only_and_bounded():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "ERR-10")
    assert scenario["modes"] == ["mock"]
    assert scenario["mock_fixture"] == "isolated-auth-required-stop"
    assert scenario["mock_status"] == "implemented_scripted_model"
    assert "不重复同步" in " ".join(scenario["success_conditions"])


def test_empty_assignment_cache_fixture_requires_stale_and_source_boundary():
    scenario = next(x for x in load_catalog()["scenarios"] if x["id"] == "ERR-11")
    assert scenario["mock_fixture"] == "synthetic-empty-assignment-cache"
    assert scenario["modes"] == ["mock"]
    assert "不代表学校端没有作业" in scenario["eval"]["terms"]
    assert scenario["mock_status"] == "implemented_scripted_model"


def test_assignment_pagination_fixture_requires_cursor_round_trip_and_unique_rows():
    scenario = next(x for x in load_catalog()["scenarios"] if x["id"] == "BUS-12")
    assert scenario["mock_fixture"] == "synthetic-assignment-cursor-continuation"
    assert scenario["modes"] == ["mock"]
    assert any("完全一致" in condition for condition in scenario["success_conditions"])


class FakeApi:
    def __init__(self):
        self.messages = []
        self.turns = 0
        self.conversation_payloads = []

    def call(self, method, path, payload=None, headers=None):
        if method == "GET" and path == "/api/v1/agent/settings":
            return 200, {}
        if method == "POST" and path == "/api/v1/agent/conversations":
            self.conversation_payloads.append(payload)
            return 201, {"id": "conversation-1"}
        if method == "POST" and path.endswith("/turns"):
            self.turns += 1
            self.messages.append(payload["text"])
            return 202, {"id": f"turn-{self.turns}"}
        if method == "GET" and path.startswith("/api/v1/agent/turns/"):
            turn_id = path.split("/")[5].split("?")[0]
            return 200, {"id": turn_id, "status": "succeeded", "response": "请问是哪门课程以及考试日期？",
                         "error": None, "events": {"items": [], "next_cursor": 0}}
        raise AssertionError(f"unexpected API call: {method} {path}")


def test_api_preflight_requires_the_local_health_contract():
    class HealthApi:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error
        def call(self, method, path):
            assert (method, path) == ("GET", "/api/v1/health")
            if self.error:
                raise self.error
            return self.response

    assert api_preflight(HealthApi((200, {"status": "ok"})))['status'] == "passed"
    assert api_preflight(HealthApi((503, {"status": "starting"})))['status'] == "failed"
    offline = api_preflight(HealthApi(error=ConnectionError("refused")))
    assert offline['status'] == "failed"
    assert offline['http_status'] is None
    assert "ConnectionError" in offline['error']


def test_tool_exposure_preflight_requires_explicit_api_enablement():
    class ExposureApi:
        def __init__(self, body): self.body = body
        def call(self, method, path):
            assert (method, path) == ('GET', '/api/v1/agent/benchmark/tool-exposure')
            return 200, self.body

    assert tool_exposure_preflight(ExposureApi({'enabled': False, 'groups': []}), [])['status'] == 'passed'
    assert tool_exposure_preflight(ExposureApi({'enabled': False, 'groups': []}), ['campus_reviews'],
                                   'campus-model')['status'] == 'failed'
    assert tool_exposure_preflight(ExposureApi({'enabled': True, 'groups': ['campus_reviews']}),
                                   ['campus_reviews'], 'campus-model')['status'] == 'passed'

    class MustNotBeCalled:
        def call(self, *_args):
            raise AssertionError("API should not be contacted for an incompatible provider")

    incompatible = tool_exposure_preflight(MustNotBeCalled(), ['campus_reviews'], 'deepseek-official')
    assert incompatible['status'] == 'failed'
    assert 'no conversation was created' in incompatible['error']


def test_provider_preflight_fails_before_run_when_config_is_missing_or_unusable():
    class ProviderApi:
        def __init__(self, code, body=None, error=None):
            self.code, self.body, self.error = code, body, error
            self.path = None
        def call(self, method, path):
            assert method == "GET"
            self.path = path
            if self.error:
                raise self.error
            return self.code, self.body

    ok = ProviderApi(200, {"id": "prv_test", "enabled": True, "has_api_key": True,
                           "api_key": "must-not-be-copied", "protocol": "openai_chat_completions",
                           "base_url": "https://proxy.example/v1"})
    result = provider_preflight(ok, "prv_test")
    assert result["status"] == "passed"
    assert result["runtime_provider"] == "campus-model"
    assert ok.path == "/api/v1/ai/providers/prv_test"
    assert "api_key" not in result and "must-not-be-copied" not in str(result)

    official = ProviderApi(200, {"id": "prv_test", "enabled": True, "has_api_key": True,
                                 "protocol": "openai_chat_completions",
                                 "base_url": "https://api.deepseek.com/v1"})
    assert provider_preflight(official, "prv_test")["runtime_provider"] == "deepseek-official"

    assert provider_preflight(ProviderApi(500, {"detail": "internal"}), "prv_test")["status"] == "failed"
    assert provider_preflight(ProviderApi(404, {}), "prv_test")["status"] == "failed"
    assert provider_preflight(ProviderApi(200, {"id": "prv_test", "enabled": False,
                                                "has_api_key": True}), "prv_test")["error"] == "provider is disabled"
    assert provider_preflight(ProviderApi(200, {"id": "prv_test", "enabled": True,
                                                "has_api_key": False}), "prv_test")["error"] == "provider key is not configured"


def test_course_directory_fact_oracle_is_deterministic_and_persists_aggregates_only():
    scenario = {"id": "BUS-11", "required_tools": ["courses.list"], "eval": {
        "kind": "course_directory_grounded", "exact_tools": ["courses.list"],
        "forbidden_tools": ["assignments.list", "connections.sync"],
        "required_tool_arguments": {"courses.list": {"limit": 2}}}}
    events = [
        {"kind": "harness_event", "data": {"event": {"seq": 10, "type": "tool/call", "data": {
            "name": "campus_courses_list", "callId": "private-call", "turn": 1, "step": 1}}}},
        {"kind": "tool_started", "data": {"name": "courses.list", "call_id": "private-call"}},
        {"kind": "tool_result", "data": {"name": "courses.list", "call_id": "private-call",
            "arguments": {"redacted": True, "safe_options": {"limit": 2}},
            "result": {"status": "partial", "data": {"items": [
                {"title": "数理逻辑", "term": "2026秋季"},
                {"title": "编译原理", "term": "2026秋季"}]}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 11, "sourceEventSeqs": [10],
            "type": "tool/result", "data": {"turn": 1, "step": 1,
            "message": {"content": [{"text": "[REDACTED]"}]}}}}},
    ]
    response = "本机缓存中：数理逻辑（2026秋季）；编译原理（2026秋季）。以上仅反映本机缓存。"
    result = evaluate(scenario, {"status": "succeeded", "response": response}, events)
    assert result["success"] == "成功"
    assert result["course_directory_evidence"]["status"] == "aligned"
    assert result["course_directory_evidence"]["matched_title_count"] == 2
    assert result["course_directory_evidence"]["matched_term_count"] == 2
    assert "数理逻辑" not in json.dumps(result, ensure_ascii=False)
    assert "private-call" not in json.dumps(result, ensure_ascii=False)

    redacted = apply_redacted_course_directory_oracle({"success": "不可验证"},
        result["course_directory_evidence"])
    assert redacted["success"] == "成功"
    assert "编译原理" not in json.dumps(redacted, ensure_ascii=False)

    redacted_redundant = apply_redacted_course_directory_oracle({
        "success": "不可验证",
        "tool_selection": {"redundant_tools_executed": ["connections.status"]},
    }, result["course_directory_evidence"])
    assert redacted_redundant["success"] == "部分成功"
    assert redacted_redundant["failure_category"] == "I_任务完成但存在工具冗余候选"
    assert redacted_redundant["tool_selection"]["redundant_tools_executed"] == ["connections.status"]

    mismatch = course_directory_grounding("本机缓存中：数理逻辑（2026秋季）。仅反映本机缓存。", events)
    assert mismatch["status"] == "content_mismatch"
    assert "2026秋季" not in json.dumps(mismatch, ensure_ascii=False)


def test_native_model_metrics_use_schema_snapshot_and_do_not_double_count_selection():
    events = [
        {"kind": "model_call", "data": {
            "duration_ms": 123.0, "available_tools": ["web_search"],
            "selected_tools": ["web_search"], "source": "native_provider_llm_stream",
            "tool_schema_snapshot": {"count": 1, "truncated": False,
                                     "schemas": [{"name": "web_search"}], "sha256": "a" * 64},
        }},
        {"kind": "harness_event", "data": {"event": {
            "type": "assistant/message", "time": 1000, "data": {"turn": 1, "step": 1},
        }}},
        {"kind": "harness_event", "data": {"event": {
            "type": "tool/call", "time": 1100, "data": {"turn": 1, "step": 1, "name": "web_search", "callId": "c1"},
        }}},
        {"kind": "harness_event", "data": {"event": {
            "type": "tool/result", "time": 1500, "data": {"turn": 1, "step": 1, "callId": "c1"},
        }}},
    ]
    measured = metrics(events)
    assert measured["model_calls"] == 1
    assert measured["model_time_ms"] == 123
    assert measured["model_time_source"].startswith("Harness llm/stream")
    assert measured["model_tools_seen"] == ["web_search"]
    assert measured["model_tools_seen_note"] == "本轮模型可见 schema 已记录完整快照与哈希"
    assert measured["model_tools_selected"] == ["web_search"]
    assert measured["tool_calls"] == 1
    assert measured["tool_time_estimated_ms"] == 400


def test_schema_snapshot_summary_counts_only_fingerprints_and_truncation(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run.json").write_text(json.dumps({"trace": [
        {"kind": "model_call", "data": {"tool_schema_snapshot": {
            "sha256": "a" * 64, "truncated": False, "schemas": [{"name": "private_tool",
                "description": "schema text", "input_schema": {"type": "object"}}]}}},
        {"kind": "model_call", "data": {"tool_schema_snapshot": {
            "sha256": "b" * 64, "truncated": True}}},
    ]}), encoding="utf-8")

    result = schema_snapshot_summary(tmp_path)

    assert result == {"model_calls_with_schema_snapshot": 2, "truncated_snapshots": 1,
                      "distinct_sha256": ["a" * 64, "b" * 64],
                      "fingerprint_call_counts": {"a" * 64: 1, "b" * 64: 1},
                      "per_tool": {"private_tool": {"count": 1, "fingerprints": {
                          hashlib.sha256(json.dumps({"name": "private_tool", "description": "schema text",
                              "input_schema": {"type": "object"}}, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")).hexdigest(): 1}}}}


def test_schema_factor_comparison_isolates_expected_target_schema_changes():
    before = {"per_tool": {
        "campus_campus_query": {"fingerprints": {"old-query": 3}},
        "campus_campus_sync": {"fingerprints": {"old-sync": 3}},
        "web_search": {"fingerprints": {"same-web": 4}}}}
    after = {"per_tool": {
        "campus_campus_query": {"fingerprints": {"new-query": 3}},
        "campus_campus_sync": {"fingerprints": {"new-sync": 3}},
        "web_search": {"fingerprints": {"same-web": 4}}}}
    evidence = schema_factor_comparison(before, after, ["campus.query", "campus.sync"])
    assert evidence["expected_one_factor_schema_change_verified"] is True
    assert evidence["non_target_tool_schema_differences"] == []
    drifted = {"per_tool": {**after["per_tool"], "web_search": {"fingerprints": {"different": 4}}}}
    assert schema_factor_comparison(before, drifted, ["campus.query", "campus.sync"])[
        "expected_one_factor_schema_change_verified"] is False


def test_runner_budget_cancellation_is_censored_not_an_agent_task_failure():
    budget_stop = {"kind": "model_call_budget", "per_run_limit": 3,
                   "total_limit": 3, "observed_total": 3}
    result = evaluate({"id": "BAS-03", "eval": {"kind": "tool_and_output"}},
                      {"status": "cancelled", "response": "", "error": None}, [], budget_stop)
    assert result["success"] == "不可验证"
    assert result["correctness"] == "不可验证"
    assert "样本被截尾" in result["reason"]
    assert result["failure_category"].startswith("J_")


def test_multi_turn_scenario_uses_one_conversation_and_scores_final_turn():
    api = FakeApi()
    scenario = {
        "id": "INT-02", "input": "帮我准备考试复习计划。",
        "messages": ["帮我准备考试复习计划。", "安排在这周末吧，尽量轻松一点。"],
        "category": "智能理解", "complexity": "中", "goal": "保留对话上下文并追问缺少信息",
        "eval": {"kind": "clarification", "terms_any": ["课程", "考试日期"]},
    }
    record = summarize_run(scenario, api, "provider", 20, 1, 42, CallBudget(10), "test-run")
    assert api.messages == scenario["messages"]
    assert record["turn_ids"] == ["turn-1", "turn-2"]
    assert [row["user_input"] for row in record["turn_results"]] == scenario["messages"]
    assert record["evaluation"]["success"] == "部分成功"
    assert record["evaluation"]["tool_quality"]["status"] == "no_tool_calls"
    assert record["evaluation"]["efficiency"]["end_to_end_ms"] == record["timings"]["end_to_end_ms"]
    assert record["evaluation"]["autonomy"]["manual_login_events"] is None
    assert record["timings"]["end_to_end_ms"] >= 0


def test_benchmark_tool_exposure_treatment_is_recorded_and_sent_at_conversation_creation():
    api = FakeApi()
    scenario = {"id": "BAS-01", "input": "你好。", "category": "基础能力",
                "complexity": "低", "goal": "普通对话", "eval": {"kind": "clarification"}}
    record = summarize_run(scenario, api, "provider", 20, 1, 42, CallBudget(10), "treatment-run",
                           benchmark_tool_groups=("campus_reviews",))

    assert api.conversation_payloads == [{"provider_id": "provider",
                                          "benchmark_tool_groups": ["campus_reviews"]}]
    assert record["benchmark_tool_groups"] == ["campus_reviews"]


def test_assistant_message_event_is_not_misclassified_as_tool_result_failure():
    event = {"kind": "harness_event", "data": {"event": {"type": "assistant/message", "data": {}}}}
    scenario = {"eval": {"kind": "nonempty"}}
    result = evaluate(scenario, {"status": "succeeded", "response": "你好", "error": None}, [event])
    assert result["success"] == "不可验证"
    assert result["failure_category"] == "J_评测环境或评分标准不足"
    assert classify({"status": "succeeded", "error": None}, [event]) == "C_工具选择或完成条件未满足"


def test_private_tool_arguments_are_redacted_in_sdk_and_domain_traces():
    secret_query = "我的离散数学课程"
    sdk_event = {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
        "name": "campus_courses_list", "arguments": json.dumps({"query": secret_query, "term": "2026秋"}),
        "callId": "call-private", "turn": 1, "step": 2}}}}
    domain_event = {"kind": "tool_result", "data": {"name": "courses.list",
        "arguments": {"query": secret_query, "term": "2026秋"}, "call_id": "call-private",
        "result": {"status": "ok", "data": {"items": []}}}}
    safe_sdk = privacy_event(sdk_event)
    safe_domain = privacy_event(domain_event)
    serialized = json.dumps([safe_sdk, safe_domain], ensure_ascii=False)
    assert secret_query not in serialized
    assert "2026秋" not in serialized
    assert safe_sdk["data"]["event"]["data"]["arguments"]["argument_keys"] == ["query", "term"]
    assert safe_domain["data"]["arguments"]["redacted"] is True
    assert safe_domain["data"]["result"]["redacted"] is True


def test_failed_private_call_without_data_does_not_require_final_answer_redaction():
    call = {"name": "campus_campus_notice_read", "callId": "private-fail", "turn": 1, "step": 2,
            "arguments": json.dumps({"record_id": "opaque-id"})}
    result = {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
        "callId": "private-fail", "turn": 1, "step": 2,
        "message": {"content": [{"text": json.dumps({"status": "error",
            "error": {"code": "UNSUPPORTED"}})}]},
    }}}}
    assert not private_calls_may_have_returned_data([result], [call])
    # The tool result itself is still redacted in persisted traces.
    assert privacy_event({"kind": "tool_result", "data": {"name": "campus.notice.read",
        "call_id": "private-fail", "result": {"status": "error", "error": {"code": "UNSUPPORTED"}}}})["data"]["result"]["redacted"]


def test_successful_private_call_and_missing_result_remain_conservatively_redacted():
    call = {"name": "campus_courses_list", "callId": "private-ok", "turn": 1, "step": 1}
    result = {"kind": "tool_result", "data": {"name": "courses.list", "call_id": "private-ok",
        "result": {"status": "ok", "data": {"items": [{"title": "PRIVATE_COURSE"}]}}}}
    assert private_calls_may_have_returned_data([result], [call])
    assert private_calls_may_have_returned_data([], [call])


def test_private_assignment_arguments_keep_only_allowlisted_operational_options():
    event = {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
        "name": "campus_assignments_list", "arguments": json.dumps({
            "order_by": "due_soon", "limit": 10, "include_instructions": False,
            "wait_seconds": 14, "course_id": "PRIVATE_COURSE", "due_after": "PRIVATE_DATE", "cursor": "PRIVATE_CURSOR"}),
        "callId": "call-safe-options", "turn": 1, "step": 1}}}}
    safe_event = privacy_event(event)
    serialized = json.dumps(safe_event, ensure_ascii=False)
    summary = safe_event["data"]["event"]["data"]["arguments"]
    assert summary["safe_options"] == {"limit": 10, "order_by": "due_soon",
        "include_instructions": False, "wait_seconds": 14, "cursor_present": True}
    assert all(secret not in serialized for secret in ("PRIVATE_COURSE", "PRIVATE_DATE", "PRIVATE_CURSOR"))
    rebuilt = privacy_event(safe_event)
    assert rebuilt["data"]["event"]["data"]["arguments"]["safe_options"] == summary["safe_options"]


def test_assignment_date_oracle_checks_before_redaction_and_persists_aggregates_only():
    events = [{"kind": "tool_result", "data": {"name": "assignments.list", "result": {
        "status": "partial", "coverage": {"complete": False},
        "data": {"items": [{"title": "PRIVATE_ASSIGNMENT", "course_id": "PRIVATE_COURSE",
                              "due_at": "2026-11-02T16:00:00Z", "instructions": "PRIVATE_BODY"}],
                 "next_cursor": None}}}}]
    grounded = assignment_date_grounding("作业截止日期：2026年11月2日", events)
    assert grounded["status"] == "dates_aligned_partial_source"
    assert grounded["matched_date_count"] == 1
    assert grounded["source_coverage_complete"] is False
    local_midnight = assignment_date_grounding("作业截止日期：2026年11月3日", events)
    assert local_midnight["status"] == "dates_aligned_partial_source"
    failed = assignment_date_grounding("作业截止日期：2026年11月4日", events)
    assert failed["status"] == "mismatch"
    categorized = assignment_date_grounding("错误作业截止日期：2026年11月4日；缓存数据截至2026年11月5日", events)
    assert categorized["unsupported_date_context_counts"] == {"deadline": 1, "cache": 1, "generic": 0}
    failed_with_unrelated_source_error = assignment_date_grounding(
        "作业截止日期：2026年11月4日", events + [{"kind": "tool_result", "data": {
            "name": "assignments.list", "result": {"status": "error", "error": {"code": "INVALID_INPUT"}}}}])
    assert failed_with_unrelated_source_error["status"] == "mismatch"
    serialized = json.dumps(grounded, ensure_ascii=False)
    assert all(secret not in serialized for secret in ("PRIVATE_ASSIGNMENT", "PRIVATE_COURSE", "PRIVATE_BODY", "2026-11-02"))
    scored = evaluate({"eval": {"kind": "assignment_dates_grounded"}},
        {"status": "succeeded", "response": "作业截止日期：2026年11月2日"}, events)
    assert scored["success"] == "部分成功"
    assert scored["assignment_date_evidence"]["matched_date_count"] == 1
    mismatch = evaluate({"id": "BUS-02", "eval": {"kind": "assignment_dates_grounded"}},
        {"status": "succeeded", "response": "作业截止日期：2026年11月4日"}, events)
    diagnosis = diagnose({"id": "BUS-02"}, {"status": "succeeded"}, events, mismatch)
    assert mismatch["failure_category"].startswith("F_")
    assert diagnosis["root_cause"]["code"] == "F"
    assert diagnosis["root_cause"]["confidence"] == "confirmed"


def test_assignment_next_by_course_oracle_requires_grounded_cached_read_without_sync():
    events = [{"kind": "tool_result", "data": {"name": "assignments.next_by_course", "result": {
        "status": "partial", "coverage": {"complete": False},
            "data": {"items": [{"course_id": "private-course-id", "course_title": "private course",
                                 "course_status": "next_due", "id": "private-assignment",
                                 "assignment_title": "private title", "due_at": "2026-11-02T16:00:00Z"}],
                 "next_cursor": None}}}}]
    result = assignment_next_by_course_grounding(
        "本机缓存覆盖未证明全量。private course下一项作业截止 2026-11-03。", events)
    assert result["status"] == "aligned"
    assert result["courses_returned"] == 1 and result["returned_rows"] == 1
    assert result["unsupported_date_context_counts"] == {"deadline": 0, "cache": 0, "generic": 0}
    serialized = json.dumps(result, ensure_ascii=False)
    assert all(secret not in serialized for secret in ("private course", "private-assignment", "private title", "2026-11-02"))

    events.append({"kind": "tool_result", "data": {"name": "connections.sync", "result": {"status": "ok"}}})
    assert assignment_next_by_course_grounding(
        "本机缓存覆盖未证明全量。private course下一项作业截止 2026-11-03。", events)["status"] == "forbidden_sync_observed"


def test_assignment_next_by_course_oracle_accepts_disclosed_no_deadline_states():
    events = [{"kind": "tool_result", "data": {"name": "assignments.next_by_course", "result": {
        "status": "partial", "coverage": {"complete": False}, "data": {"items": [
            {"course_id": "c1", "course_title": "私有课程甲", "course_status": "unknown_deadline_only",
             "cached_assignment_count": 1, "unknown_deadline_count": 1},
            {"course_id": "c2", "course_title": "私有课程乙", "course_status": "no_cached_assignments",
             "cached_assignment_count": 0, "unknown_deadline_count": 0}], "next_cursor": None}}}}]
    response = "本机缓存无法证明学校平台全量。私有课程甲：有1项缓存作业，但截止时间未知；私有课程乙：本机缓存没有作业记录。"
    result = assignment_next_by_course_grounding(response, events)
    assert result["status"] == "aligned_no_deadlines"
    assert result["course_status_counts"] == {"unknown_deadline_only": 1, "no_cached_assignments": 1}
    assert result["course_statuses_disclosed"] is True


def test_assignment_next_by_course_evaluator_marks_no_deadline_states_partial_not_unverified():
    event = {"kind": "tool_result", "data": {"name": "assignments.next_by_course", "result": {
        "status": "partial", "coverage": {"complete": False}, "data": {"items": [
            {"course_id": "c1", "course_title": "课程甲", "course_status": "unknown_deadline_only",
             "cached_assignment_count": 1, "unknown_deadline_count": 1},
            {"course_id": "c2", "course_title": "课程乙", "course_status": "no_cached_assignments",
             "cached_assignment_count": 0, "unknown_deadline_count": 0}], "next_cursor": None}}}}
    response = "本机缓存无法证明学校平台全量。课程甲：截止时间未知；课程乙：本机缓存没有作业记录。"
    scored = evaluate({"id": "BUS-09", "eval": {"kind": "assignment_next_by_course"}},
        {"status": "succeeded", "response": response, "error": None}, [event])
    assert scored["success"] == "部分成功"
    assert scored["assignment_date_evidence"]["status"] == "aligned_no_deadlines"


def test_redacted_assignment_report_uses_alignment_and_completeness_as_separate_signals():
    evaluation = {"success": "不可验证", "correctness": "不可验证"}
    oracle = {"status": "aligned", "date_evidence_status": "dates_aligned_partial_source",
              "unsupported_date_count": 0, "returned_rows": 17}
    result = apply_redacted_assignment_oracle(evaluation, oracle)
    assert result["success"] == "部分成功"
    assert result["assignment_date_evidence"]["status"] == "aligned"
    assert result["assignment_date_evidence"]["date_evidence_status"] == "dates_aligned_partial_source"

    bad = apply_redacted_assignment_oracle({"success": "成功"}, {**oracle, "unsupported_date_count": 1})
    assert bad["success"] == "失败"
    assert bad["assignment_date_evidence"]["date_evidence_status"] == "mismatch"


def test_next_by_course_result_and_course_name_are_redacted_in_persisted_trace():
    event = {"kind": "tool_result", "data": {"name": "assignments.next_by_course", "arguments": {},
        "result": {"status": "partial", "data": {"items": [{"course_title": "PRIVATE_COURSE_TITLE",
            "course_id": "PRIVATE_COURSE_ID", "title": "PRIVATE_ASSIGNMENT", "due_at": "2026-11-02",
            "due_at_local": "2026-11-03T00:00:00+08:00"}]}}}}
    redacted = privacy_event(event)
    serialized = json.dumps(redacted, ensure_ascii=False)
    assert "PRIVATE_COURSE_TITLE" not in serialized
    assert "PRIVATE_COURSE_ID" not in serialized
    assert "PRIVATE_ASSIGNMENT" not in serialized
    assert "2026-11-02" not in serialized and "2026-11-03" not in serialized


def test_course_directory_result_and_assistant_text_are_redacted_in_persisted_trace():
    result_event = {"kind": "tool_result", "data": {"name": "courses.list", "arguments": {"limit": 5},
        "result": {"status": "partial", "data": {"items": [{"id": "PRIVATE_COURSE_ID",
            "title": "PRIVATE_COURSE_TITLE", "term": "PRIVATE_TERM"}]}}}}
    answer_event = {"kind": "result", "data": {"response": "PRIVATE_COURSE_TITLE PRIVATE_TERM"}}
    serialized = json.dumps([privacy_event(result_event), privacy_event(answer_event)], ensure_ascii=False)
    for secret in ("PRIVATE_COURSE_ID", "PRIVATE_COURSE_TITLE", "PRIVATE_TERM"):
        assert secret not in serialized
    assert "PRIVATE_COURSE_TITLE" not in str(privacy_event(answer_event))


def test_execution_dimensions_report_observations_without_fabricating_scores():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_courses_list", "callId": "c1", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "callId": "c1", "turn": 1, "step": 1, "message": {"content": []}}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_tasks_list", "callId": "c2", "turn": 1, "step": 2}}}},
    ]
    scenario = {"eval": {"tools_any": ["course", "assignment"]}}
    tool_quality, efficiency, autonomy = execution_dimensions(
        scenario, events,
        {"tool_calls": 2, "tool_errors": 0, "model_calls": 3, "retry_signals": 1,
         "tokens_input_observed": 10, "tokens_output_observed": 5,
         "estimated_cost": None, "human_confirmations": 0},
        {"end_to_end_ms": 1200, "submission_to_completion_ms": 1100,
         "first_assistant_message_ms": 100, "first_token_ms": None},
        user_turn_count=2)
    assert tool_quality["tool_calls"] == 2
    assert tool_quality["calls_without_correlated_result"] == 1
    assert tool_quality["matched_tool_aliases"] == ["course"]
    assert efficiency["end_to_end_ms"] == 1200 and efficiency["threshold_status"] == "not_calibrated"
    assert autonomy["confirmation_events_observed"] == 0
    assert autonomy["manual_login_events"] is None
    assert autonomy["scripted_user_turns"] == 2


def test_tool_selection_evidence_separates_model_selection_from_execution_and_normalizes_aliases():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_assignments_list", "callId": "c1"}}}},
        {"kind": "tool_started", "data": {"name": "assignments.list", "argument_keys": ["limit", "order_by"]}},
        {"kind": "tool_result", "data": {"name": "assignments.list", "arguments": {
            "redacted": True, "safe_options": {"order_by": "due_soon", "limit": 10}},
            "result": {"status": "partial"}}},
        {"kind": "tool_started", "data": {"name": "courses.list", "argument_keys": ["limit"]}},
    ]
    result = tool_selection_evidence({"exact_tools": ["assignments.list"],
                                      "forbidden_tools": ["courses.list"],
                                      "required_tool_arguments": {"assignments.list": {
                                          "order_by": "due_soon", "limit": 10}}}, events)
    assert result["selected_tools"] == ["assignments.list"]
    assert result["executed_tools"] == ["assignments.list", "courses.list"]
    assert result["missing_required_tools"] == []
    assert result["forbidden_tools_executed"] == ["courses.list"]
    assert result["observed_safe_arguments"] == {"assignments.list": {
        "order_by": "due_soon", "limit": 10}}
    assert result["status"] == "constraint_violation"
    assert "title" not in str(result) and "course_id" not in str(result)


def test_tool_selection_evidence_finds_wrong_range_arguments_without_persisting_other_values():
    result = tool_selection_evidence({"required_tool_arguments": {
        "assignments.list": {"order_by": "due_soon", "limit": 10}}}, [
        {"kind": "tool_result", "data": {"name": "assignments.list", "arguments": {
            "redacted": True, "argument_keys": ["limit", "order_by", "course_id"],
            "safe_options": {"limit": 50, "order_by": "id", "course_id": "private-course"}}}},
    ])
    assert result["status"] == "constraint_violation"
    assert result["argument_mismatches"] == ["assignments.list"]
    assert "course_id" not in str(result)


def test_tool_selection_evidence_flags_model_selection_without_backend_execution():
    result = tool_selection_evidence({"exact_tools": ["assignments.list"]}, [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_assignments_list", "callId": "c1"}}}},
    ])
    assert result["selected_tools"] == ["assignments.list"]
    assert result["executed_tools"] == []
    assert result["missing_required_tools"] == ["assignments.list"]


def test_forbidden_argument_policy_flags_private_capability_without_persisting_value():
    evidence = tool_selection_evidence({"forbidden_tool_argument_values": {
        "campus.query": {"capability": ["profile", "courses", "grades"]}}}, [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_campus_query", "arguments": json.dumps({"capability": "profile", "query": {}})}}}}]
    )
    assert evidence["forbidden_argument_calls"] == ["campus.query"]
    assert "profile" not in json.dumps(evidence)


def test_redacted_forbidden_scope_evidence_retains_only_tool_name():
    score = apply_redacted_forbidden_scope_evidence({"success": "部分成功"}, ["campus.query"])
    assert score["failure_category"] == "C_模型选择了超出任务范围的数据能力"
    assert "profile" not in json.dumps(score)


def test_bus09_rubric_records_redundant_candidate_calls_as_efficiency_evidence():
    scenario = next(item for item in load_catalog()["scenarios"] if item["id"] == "BUS-09")
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_assignments_next_by_course", "callId": "c1"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "callId": "c1", "message": {"isError": False}}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_assignments_list", "callId": "c2"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "callId": "c2", "message": {"isError": False}}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_courses_list", "callId": "c3"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "callId": "c3", "message": {"isError": False}}}}},
        {"kind": "harness_event", "data": {"event": {"type": "assistant/message", "data": {
            "turn": 1, "step": 4}}}},
    ]
    evidence = tool_selection_evidence(scenario["eval"], events)
    assert evidence["missing_required_tools"] == []
    assert evidence["redundant_tools_executed"] == ["assignments.list", "courses.list"]
    cause = root_cause_attribution(scenario, {"status": "succeeded"}, events,
                                   {"success": "部分成功", "reason": "candidate calls"})
    assert cause["primary"]["code"] == "E"  # fixture omits per-call follow-up assistant steps
    assert any(item["code"] == "I" and item["confidence"] == "suspected"
               for item in [cause["primary"], *cause["alternatives"]])


def test_tool_selection_evidence_correlates_native_calls_with_results():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_search", "callId": "native-1"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_fetch", "callId": "native-2"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "message": {"toolCallId": "native-1", "content": [{"type": "text", "text": "result"}]}}}}},
    ]
    result = tool_selection_evidence({"exact_tools": ["web_search"]}, events)
    assert result["selected_tools"] == ["web.fetch", "web.search"]
    assert result["executed_tools"] == ["web.search"]
    assert result["missing_required_tools"] == []
    assert result["native_calls_without_result"] == ["web.fetch"]
    assert "native-1" not in str(result) and "native-2" not in str(result)


def test_tool_selection_correlates_call_id_omitted_result_by_source_event_sequence():
    events = [
        {"kind": "harness_event", "data": {"event": {"seq": 20, "type": "tool/call", "data": {
            "name": "campus_courses_list", "callId": "call-private", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 21, "sourceEventSeqs": [20],
            "type": "tool/result", "data": {"turn": 1, "step": 1,
            "message": {"content": [{"text": "[REDACTED]"}]}}}}},
    ]
    result = tool_selection_evidence({"exact_tools": ["courses.list"]}, events)
    assert result["executed_tools"] == ["courses.list"]
    assert result["missing_required_tools"] == []
    assert result["native_calls_without_result"] == []
    assert "call-private" not in str(result)


def test_exact_tool_policy_changes_evaluation_and_marks_redundant_directory_lookup():
    scenario = {"id": "BUS-10", "required_tools": ["assignments.list"], "eval": {
        "kind": "tool_and_output", "tools_any": ["assignment"], "keywords_any": ["作业"],
        "exact_tools": ["assignments.list"], "forbidden_tools": ["courses.list"]}}
    events = [
        {"kind": "tool_started", "data": {"name": "assignments.list"}},
        {"kind": "tool_result", "data": {"name": "assignments.list", "result": {"status": "partial"}}},
        {"kind": "tool_started", "data": {"name": "courses.list"}},
    ]
    result = evaluate(scenario, {"status": "succeeded", "response": "作业记录已读取"}, events)
    assert result["success"] == "部分成功"
    assert result["failure_category"] == "I_任务成功但执行冗余或违反工具范围"
    assert result["tool_selection"]["forbidden_tools_executed"] == ["courses.list"]


def test_native_tool_result_without_call_id_correlates_by_unambiguous_turn_step():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_fetch", "callId": "call-1", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "turn": 1, "step": 1, "message": {"content": [{"text": "page contents"}]}}}}},
    ]
    tool_quality, _, _ = execution_dimensions(
        {"eval": {}}, events, {"tool_calls": 1, "tool_errors": 0, "human_confirmations": 0}, {}, 1)
    assert tool_quality["calls_without_correlated_result"] == 0


def test_native_tool_result_without_call_id_does_not_guess_parallel_call_pairing():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_fetch", "callId": "call-1", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_search", "callId": "call-2", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "turn": 1, "step": 1, "message": {"content": [{"text": "result"}]}}}}},
    ]
    tool_quality, _, _ = execution_dimensions(
        {"eval": {}}, events, {"tool_calls": 2, "tool_errors": 0, "human_confirmations": 0}, {}, 1)
    assert tool_quality["calls_without_correlated_result"] == 2


def test_native_parallel_tool_results_correlate_by_harness_source_event_sequence():
    events = [
        {"kind": "harness_event", "data": {"event": {"seq": 10, "type": "tool/call", "data": {
            "name": "campus_courses_list", "callId": "call-1", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 11, "type": "tool/call", "data": {
            "name": "campus_connections_status", "callId": "call-2", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 12, "sourceEventSeqs": [10], "type": "tool/result", "data": {
            "turn": 1, "step": 1, "message": {"content": []}}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 13, "sourceEventSeqs": [11], "type": "tool/result", "data": {
            "turn": 1, "step": 1, "message": {"content": []}}}}},
    ]
    tool_quality, _, _ = execution_dimensions(
        {"eval": {}}, events, {"tool_calls": 2, "tool_errors": 0, "human_confirmations": 0}, {}, 1)
    assert tool_quality["calls_without_correlated_result"] == 0


def test_confirmation_summary_does_not_report_unobservable_interventions_as_zero():
    result = confirmation_summary([
        {"metrics": {"human_confirmations": 0}},
        {"metrics": {"human_confirmations": 1}},
        {"metrics": {"human_confirmations": None}},
    ])
    assert result["mean_recorded_confirmation_events"] == 0.5
    assert result["confirmation_count_runs"] == 2
    assert result["mean_human_interventions"] is None
    assert "external logins" in result["confirmation_observation_note"]
    assert confirmation_summary([{"metrics": {"human_confirmations": None}}])["mean_recorded_confirmation_events"] is None


def test_campus_entry_oracle_uses_observed_connector_and_disconnected_boundary():
    status = json.dumps({"status": "partial", "data": {
        "connected": False,
        "login_url": "https://zjuam.zju.edu.cn/cas/login?service=zdbk.zju.edu.cn",
        "connectors": [{"platform_id": "zdbk", "connected": False}],
    }})
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_campus_status", "callId": "status-1", "turn": 1, "step": 1}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "turn": 1, "step": 1, "message": {"content": [{"text": status}]}}}}},
    ]
    response = "本科教务入口是 https://zdbk.zju.edu.cn；当前 connected: false，未读取个人课表。"
    scenario = {"eval": {"kind": "campus_entry_grounded"}}
    turn = {"status": "succeeded", "response": response}
    result = evaluate(scenario, turn, events)
    assert result["success"] == "部分成功"
    assert result["campus_entry_evidence"]["registered_zdbk_connector"] is True
    assert result["campus_entry_evidence"]["offline_boundary_disclosed"] is True
    compact = evaluate(scenario, {**turn, "response": "本科教务入口 https://zdbk.zju.edu.cn；当前连接状态connected:false。"}, events)
    assert compact["success"] == "部分成功"

    unsupported = evaluate(scenario, {**turn, "response": "本科教务入口是 https://zdbk.zju.edu.cn。"}, events)
    assert unsupported["success"] == "失败"


def test_campus_entry_oracle_accepts_structured_backend_status_result():
    events = [{"kind": "tool_result", "data": {"name": "campus.status", "result": {
        "status": "partial", "data": {"connected": False,
            "connectors": [{"platform_id": "zdbk", "connected": False}]}}}}]
    evidence = campus_entry_grounding(
        "本科教务入口是 https://zdbk.zju.edu.cn，目前未连接所以我没有读取个人课表。", events)
    assert evidence["status"] == "core_claim_aligned"
    assert evidence["registered_zdbk_connector"] is True


def test_campus_entry_oracle_does_not_score_unobserved_status_as_success():
    scenario = {"eval": {"kind": "campus_entry_grounded"}}
    result = evaluate(scenario, {"status": "succeeded", "response": "本科教务入口是 https://zdbk.zju.edu.cn。"}, [])
    assert result["success"] == "不可验证"


def test_campus_entry_oracle_accepts_independent_official_reference_without_personal_status():
    scenario = {"eval": {"kind": "campus_entry_grounded", "reference_sources": [{
        "url": "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_slogin.html",
        "domain": "zdbk.zju.edu.cn", "verified_at": "2026-10-16"}]}}
    result = evaluate(scenario, {"status": "succeeded",
        "response": "本科教务平台为 https://zdbk.zju.edu.cn。"}, [])
    assert result["success"] == "部分成功"
    assert result["campus_entry_evidence"]["status_result_observed"] is False
    assert result["campus_entry_evidence"]["independent_reference_sources"][0]["domain"] == "zdbk.zju.edu.cn"


def test_campus_entry_oracle_rejects_missing_official_portal_claim():
    scenario = {"eval": {"kind": "campus_entry_grounded", "reference_sources": [{
        "url": "https://zdbk.zju.edu.cn/", "domain": "zdbk.zju.edu.cn"}]}}
    result = evaluate(scenario, {"status": "succeeded", "response": "请联系学院教务员。"}, [])
    assert result["success"] == "失败"


def test_campus_credentials_oracle_requires_safe_status_result_and_distinct_explanation():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "campus_campus_credentials_status", "turn": 1, "step": 1}}}},
        {"kind": "tool_result", "data": {"name": "campus.credentials.status", "result": {
            "status": "ok", "data": {"credentials": [
                {"scope": "passport", "configured": True, "auto_relogin": True}],
                "browser_session_authorized": False, "campus_workspace_connected": False}}}},
    ]
    response = "凭据已保存且自动重登开启；浏览器授权未开，当前教务连接未连接。但本机状态不能验证学校当前是否接受凭据，也不能证明此前登录失败的原因。"
    evidence = campus_credentials_grounding(response, events)
    assert evidence["status"] == "aligned"
    assert evaluate({"eval": {"kind": "campus_credentials_status"}},
        {"status": "succeeded", "response": response}, events)["success"] == "部分成功"
    leaked = campus_credentials_grounding(response + " 用户名提示 32***32", events)
    assert leaked["username_hint_in_answer"] is True
    assert leaked["status"] == "incomplete_or_unverified"


def test_safe_redacts_url_query_values_without_corrupting_embedded_json():
    original = json.dumps({"status": "partial", "data": {
        "login_url": "https://zjuam.zju.edu.cn/cas/login?ticket=secret-ticket",
        "connected": False,
    }})
    cleaned = safe(original)
    parsed = json.loads(cleaned)
    assert parsed["data"]["login_url"] == "https://zjuam.zju.edu.cn/cas/login?<redacted>"
    assert parsed["data"]["connected"] is False
    assert "secret-ticket" not in cleaned


def test_safe_preserves_only_registered_public_notice_identity_queries():
    notice_url = "https://zdbk.zju.edu.cn/jwglxt/xtgl/xwck_ckLoginNews.html?xwbh=" + "a" * 32 + "&doType=save"
    text = f"[通知标题]({notice_url}) and https://zjuam.zju.edu.cn/cas/login?ticket=secret-ticket"
    cleaned = safe(text)
    assert notice_url in cleaned
    assert f"]({notice_url})" in cleaned
    assert "secret-ticket" not in cleaned
    assert "login?<redacted>" in cleaned


def test_safe_rejects_noncanonical_public_notice_queries():
    unsafe = "https://zdbk.zju.edu.cn/jwglxt/xtgl/xwck_ckLoginNews.html?ticket=secret"
    assert "ticket=secret" not in safe(unsafe)


def test_safe_redacts_username_hints_from_json_and_final_text():
    assert safe({"username_hint": "32***32"})["username_hint"] == "[REDACTED]"
    assert "32***32" not in safe("教务用户名提示：32***32")


def test_source_grounding_matches_citations_and_dates_to_observed_tool_results():
    events = [
        {"kind": "tool_result", "data": {"name": "campus.sync", "result": {"data": {
            "records": [{"title": "校园公开通知", "published_at": "2026-10-16", "source": "https://campus.example/notices/1"}]}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {"name": "web_search", "arguments": "{}"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "message": {"content": [{"text": "校园公开通知 2026-10-16"}]},
            "meta": {"sources": [{"title": "校园公开通知", "url": "https://campus.example/notices/1"}]}}}}},
    ]
    aligned = source_grounding("[校园公开通知](https://campus.example/notices/1) 发布日期 2026-10-16", events)
    assert aligned["status"] == "aligned"
    assert aligned["dates_supported_by_tool_results"] == ["2026-10-16"]
    ungrounded = source_grounding("[校园公开通知](https://other.example/fake) 发布日期 2026-10-20", events)
    assert ungrounded["status"] == "incomplete_or_unverified"
    retrieval = source_grounding("[校园公开通知](https://campus.example/notices/1) 抓取时间 2026-10-16",
        events, require_dates=False, allowed_domains=["campus.example"])
    assert retrieval["status"] == "aligned"
    assert retrieval["date_check"] == "not_required"
    wrong_official_host = source_grounding("[伪造标题](https://other.example/notices/1)", events,
        require_dates=False, allowed_domains=["campus.example"])
    assert wrong_official_host["status"] == "incomplete_or_unverified"


def test_source_grounding_uses_observed_campus_browser_page_links_and_dates():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "mcp__campus_browser__browser_navigate", "callId": "browser-1",
            "arguments": {"url": "https://www.intl.zju.edu.cn/zh-hans/events"}}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "message": {"source": {"callId": "browser-1"}, "content": [{"text":
                "Page URL: https://www.intl.zju.edu.cn/zh-hans/events\n"
                "Page Title: 活动 | International Campus Zhejiang University\n"
                '- link "The CAAI International Conference on Artificial Intelligence" [e1]\n'
                "  - /url: /zh-hans/events/18674\n"
                "  - text: 2026年10月18日 - 08:30"}]}, "meta": {}}}}},
    ]
    result = source_grounding("[events/18674](https://www.intl.zju.edu.cn/zh-hans/events/18674) 日期 2026-10-18", events)
    assert result["status"] == "aligned"
    assert result["citations"][0]["matched_tool_source"] is True
    assert result["dates_supported_by_tool_results"] == ["2026-10-18"]


def test_redacted_public_source_oracle_persists_aggregates_and_caps_success():
    source = {"status": "aligned", "citation_count": 1,
        "citations": [{"matched_tool_source": True, "label_matches_source": True,
                       "allowed_domain": True, "url": "https://public.example/a"}],
        "date_claims": ["2026-10-16"], "date_check": "verified"}
    summary = public_source_oracle_summary(source)
    assert summary["label_aligned_citation_count"] == 1
    assert "url" not in json.dumps(summary)
    score = apply_redacted_public_source_oracle({"success": "不可验证",
        "tool_selection": {"redundant_tools_executed": ["campus.status"]}}, summary)
    assert score["success"] == "部分成功"
    assert score["failure_category"] == "I_任务完成但存在工具冗余候选"
    assert "public.example" not in json.dumps(score)


def test_redacted_public_source_oracle_can_retain_partial_aligned_evidence():
    score = apply_redacted_public_source_oracle({"success": "不可验证"}, {
        "status": "incomplete_or_unverified", "citation_count": 10,
        "matched_citation_count": 10, "label_aligned_citation_count": 9,
        "date_claim_count": 8, "date_check": "verified"})
    assert score["success"] == "部分成功"
    assert "9/10" in score["reason"]


def test_source_grounding_accepts_generic_source_link_label_when_url_matches():
    events = [{"kind": "tool_result", "data": {"name": "campus.query", "result": {"data": {
        "records": [{"title": "校园活动通知", "published_at": "2026-10-17",
                     "source": "https://public.example/notices/1"}]}}}}]
    result = source_grounding("[原链接](https://public.example/notices/1)", events, require_dates=False)
    assert result["status"] == "aligned"


def test_source_grounded_evaluator_confirms_source_alignment_only_as_partial():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_search", "turn": 1, "step": 1, "callId": "search-1"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "message": {"content": [{"text": "Official manual: describes widgets and API behavior."}]},
            "meta": {"sources": [{"title": "Official manual", "url": "https://docs.example/guide"}]}}}}},
    ]
    scenario = {"eval": {"kind": "source_grounded", "tools_any": ["web_search"],
        "minimum_citations": 1, "require_dates": False, "allowed_domains": ["docs.example"]}}
    aligned = evaluate(scenario, {"status": "succeeded", "response":
        "[Official manual](https://docs.example/guide) The page describes widgets.", "error": None}, events)
    assert aligned["success"] == "部分成功"
    assert aligned["source_grounding"]["status"] == "aligned"
    unaligned = evaluate(scenario, {"status": "succeeded", "response":
        "[Official manual](https://other.example/fake) The page describes widgets.", "error": None}, events)
    assert unaligned["success"] == "不可验证"


def test_target_page_rubric_can_confirm_success_only_with_correlated_fetch_and_content():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_fetch", "callId": "c1", "arguments": "{\"url\":\"https://www.deepseek.com/\"}"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "callId": "c1", "message": {"isError": False, "content": [{"text":
                "Fetched https://www.deepseek.com/ (HTTP 200)\nDeepSeek builds artificial intelligence models and research systems."}]}}}}},
    ]
    scenario = {"eval": {"kind": "target_page_extraction", "target_url": "https://www.deepseek.com/",
                         "minimum_shared_terms": 3, "minimum_answer_coverage": 0.2}}
    result = evaluate(scenario, {"status": "succeeded", "response":
        "DeepSeek builds artificial intelligence models for research.", "error": None}, events)
    assert result["success"] == "成功"
    assert result["target_page_evidence"]["status"] == "aligned"


def test_target_page_rubric_rejects_generic_answer_and_unmatched_fetch():
    event = {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
        "name": "web_fetch", "callId": "c1", "arguments": {"url": "https://www.deepseek.com/"}}}}}
    result = target_page_extraction("DeepSeek has a website.", [event], "https://www.deepseek.com/")
    assert result["status"] == "incomplete_or_unverified"
    assert result["evidence"] == []


def test_target_page_result_correlates_with_turn_and_step_when_call_id_is_omitted():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_fetch", "turn": 1, "step": 1,
            "arguments": "{\"url\":\"https://www.deepseek.com/\"}"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "turn": 1, "step": 1, "meta": {"statusCode": 200},
            "message": {"content": [{"text": "DeepSeek 深度求索人工智能模型研究"}]}}}}},
    ]
    result = target_page_extraction("DeepSeek 深度求索人工智能模型研究", events, "https://www.deepseek.com/")
    assert result["status"] == "aligned"


def test_target_page_rubric_correlates_mcp_browser_result_by_harness_event_lineage():
    target = "https://www.deepseek.com/"
    page = """### Ran Playwright code
await page.goto('https://www.deepseek.com/')
### Page
- Page URL: https://www.deepseek.com/
- Page Title: DeepSeek | 深度求索
### Page content
- navigation: DeepSeek 网页版 API 开放平台 API 文档
- main: 探索未至之境 深度思考 智能搜索
- footer: DeepSeek 应用下载 服务状态 研究"""
    events = [
        {"kind": "harness_event", "data": {"event": {"seq": 24, "type": "tool/call", "data": {
            "name": "mcp__campus_browser__browser_navigate", "callId": "browser-call", "turn": 1,
            "step": 2, "arguments": json.dumps({"url": target})}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 25, "sourceEventSeqs": [24],
            "type": "tool/result", "data": {"turn": 1, "step": 2,
                "message": {"isError": False, "content": [{"text": page}]}}}}},
    ]
    answer = "DeepSeek 深度求索首页，页面主标语探索未至之境，包含 API 开放平台、API 文档和服务状态导航。"
    result = target_page_extraction(answer, events, target, minimum_shared_terms=3,
                                   minimum_answer_coverage=0.10)
    assert result["status"] == "aligned"
    assert result["evidence"][0]["fetch_success"] is True
    assert result["evidence"][0]["tool"] == "mcp__campus_browser__browser_navigate"


def test_target_page_rubric_does_not_accept_browser_redirect_to_another_page():
    target = "https://www.deepseek.com/"
    events = [
        {"kind": "harness_event", "data": {"event": {"seq": 4, "type": "tool/call", "data": {
            "name": "mcp__campus_browser__browser_navigate", "callId": "browser-call", "turn": 1,
            "step": 1, "arguments": json.dumps({"url": target})}}}},
        {"kind": "harness_event", "data": {"event": {"seq": 5, "sourceEventSeqs": [4],
            "type": "tool/result", "data": {"turn": 1, "step": 1,
                "message": {"isError": False, "content": [{"text":
                    "- Page URL: https://login.example.test/\n- Page Title: Sign in\nDeepSeek login"}]}}}}},
    ]
    result = target_page_extraction("DeepSeek 登录页", events, target)
    assert result["status"] == "incomplete_or_unverified"
    assert result["evidence"][0]["fetch_success"] is False


def test_nested_native_call_ids_correlate_parallel_tool_results():
    target = "https://api-docs.deepseek.com/zh-cn/"
    events = []
    for call_id, url, content in (
        ("fetch-target", target,
         "DeepSeek API documentation explains chat completion requests and tool calls."),
        ("fetch-other", "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
         "Pricing page lists model prices."),
    ):
        events.append({"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "web_fetch", "callId": call_id, "turn": 1, "step": 2,
            "arguments": json.dumps({"url": url})}}}})
        events.append({"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "turn": 1, "step": 2, "meta": {"statusCode": 200}, "message": {
                "source": {"callId": call_id, "kind": "tool"}, "isError": False,
                "content": [{"text": content}]}}}}})

    answer = "DeepSeek API documentation explains chat completion requests and tool calls."
    scored = evaluate({"eval": {"kind": "target_page_extraction", "target_url": target}},
                      {"status": "succeeded", "response": answer, "error": None}, events)
    assert scored["success"] == "成功"
    assert scored["target_page_evidence"]["status"] == "aligned"
    quality, _, _ = execution_dimensions({"eval": {"tools_any": ["web_fetch"]}}, events,
                                          {"tool_errors": 0}, {"end_to_end_ms": 1})
    assert quality["calls_without_correlated_result"] == 0


class ProbeApi:
    def call(self, method, path, payload=None):
        if path == "/api/v1/health": return 200, {"ok": True}
        if path == "/api/v1/agent/campus/status":
            return 200, {"connected": False, "browser_session_authorized": False}
        if path == "/api/v1/connections":
            return 200, {"items": [{"id": "conn_zykt", "status": "service_unavailable"}]}
        raise AssertionError(path)


def test_live_probe_distinguishes_transport_from_missing_authorization(tmp_path):
    result = run_live_tool_probe(ProbeApi(), tmp_path)
    assert result["status"] == "completed"
    assert result["transport_status"] == "passed"
    assert result["capability_status"] == "not_assessed"
    campus = next(item for item in result["probes"] if item["name"] == "campus_status")
    assert campus["workspace_state"] == "disconnected_without_browser_grant"


def test_private_course_probe_records_only_redacted_metadata(tmp_path):
    class PrivateProbeApi(ProbeApi):
        def call(self, method, path, payload=None):
            if path == "/api/v1/agent/campus/sync":
                assert method == "POST"
                assert payload == {"capability": "courses", "query": {"year": "2026", "semester": "1|秋"}, "platform": "zdbk"}
                return 200, {"records": [{"name": "PRIVATE_COURSE_NAME", "room": "PRIVATE_ROOM"}],
                    "fetched_at": "2026-10-16T00:00:00Z", "freshness": "fresh",
                    "completeness": "complete", "error": None}
            return super().call(method, path, payload)

    result = run_live_tool_probe(PrivateProbeApi(), tmp_path, private_courses=True,
                                 academic_year="2026", semester="1|秋")
    probe_file = (tmp_path / "layers" / "live-tool-probe.json").read_text(encoding="utf-8")
    private = next(item for item in result["probes"] if item["name"] == "private_courses")
    assert result["transport_status"] == "passed"
    assert result["capability_status"] == "ready"
    assert private["capability_status"] == "ready"
    assert private["evidence"]["record_count"] == 1
    assert private["evidence"]["term"] == {"year": "2026", "semester": "1|秋"}
    assert private["evidence"]["course_content_in_probe_artifact"] is False
    assert "PRIVATE_COURSE_NAME" not in probe_file
    assert "PRIVATE_ROOM" not in probe_file


def test_root_cause_attribution_separates_confirmed_auth_from_suspected_result_handoff():
    auth = root_cause_attribution({}, {"status": "failed", "error": {"code": "AUTH_REQUIRED"}}, [],
                                  {"success": "失败", "reason": "需要登录"})
    assert auth["primary"]["code"] == "H"
    assert auth["primary"]["confidence"] == "confirmed"

    call = {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
        "name": "web_fetch", "turn": 1, "step": 1, "callId": "c1"}}}}
    result = {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
        "turn": 1, "step": 1, "message": {"content": [{"text": "page result"}]}}}}}
    handoff = root_cause_attribution({}, {"status": "succeeded"}, [call, result],
                                    {"success": "部分成功", "reason": "结果未对齐"})
    assert handoff["primary"]["code"] == "E"
    assert handoff["primary"]["confidence"] == "suspected"
    assert "E_" in evaluate({"eval": {"kind": "target_page_extraction", "target_url": "https://example.test/"}},
        {"status": "succeeded", "response": "no evidence", "error": None}, [call, result])["failure_category"]


def test_root_cause_attribution_does_not_mislabel_invalid_arguments_as_external_failure():
    events = [{"kind": "tool_result", "data": {"name": "campus.query", "result": {
        "status": "error", "error": {"code": "INVALID_INPUT"}}}}]
    diagnosis = root_cause_attribution({}, {"status": "succeeded"}, events,
        {"success": "部分成功", "reason": "some result returned"})
    assert diagnosis["primary"]["code"] == "C"
    assert "INVALID_INPUT" in json.dumps(diagnosis["primary"]["evidence"])
    assert all("INVALID_INPUT" not in item for item in diagnosis["alternatives"]
               if item["code"] == "H")


def test_budget_cancellation_does_not_misdiagnose_inflight_tool_calls_as_harness_failure():
    call = {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
        "name": "web_search", "turn": 1, "step": 2, "callId": "inflight"}}}}
    diagnosis = root_cause_attribution({}, {"status": "cancelled"}, [call],
        {"success": "不可验证", "reason": "budget censored"},
        {"kind": "model_call_budget", "per_run_limit": 2, "observed_total": 2})
    assert diagnosis["primary"]["code"] == "J"
    assert diagnosis["primary"]["confidence"] == "confirmed"
    assert all(row["code"] not in {"D", "E"} for row in diagnosis["alternatives"])


def test_deadline_timeout_is_primary_over_observed_recoverable_fetch_error():
    events = [
        {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
            "name": "browser_click", "turn": 1, "step": 9, "callId": "pending"}}}},
        {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
            "turn": 1, "step": 3, "message": {"isError": True,
                "content": [{"text": "Error: URL blocked WEB_BLOCKED_URL"}]}}}}},
    ]
    result = root_cause_attribution({}, {"status": "timeout", "error": None}, events,
        {"success": "失败", "reason": "timeout"})
    assert result["primary"]["code"] == "J"
    assert any(item["code"] == "H" for item in result["alternatives"])


def test_root_cause_attribution_covers_combination_efficiency_and_environment_categories():
    assert root_cause_attribution({}, {"status": "succeeded"}, [],
        {"success": "不可验证", "reason": "no oracle"})["primary"]["code"] == "J"
    repeated_events = []
    for step in (1, 2, 3):
        repeated_events.extend([
            {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
                "name": "web_search", "turn": 1, "step": step, "callId": f"c{step}"}}}},
            {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
                "turn": 1, "step": step, "message": {"content": [{"text": "result"}]}}}}},
        ])
    repeated_events.append({"kind": "harness_event", "data": {"event": {"type": "assistant/message", "data": {
        "turn": 1, "step": 4}}}})
    diagnosis = diagnose({"id": "BAS-03"}, {"status": "succeeded"}, repeated_events,
                         {"success": "成功", "observed_tools": ["web_search"]})
    assert diagnosis["root_cause"]["code"] == "I"
    assert diagnosis["root_cause"]["confidence"] == "suspected"
    assert set(ROOT_CAUSE_LABELS) == set("ABCDEFGHIJK")

    multi_events = []
    for step in (1, 2):
        multi_events.extend([
            {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
                "name": f"tool_{step}", "turn": 1, "step": step, "callId": f"m{step}"}}}},
            {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
                "turn": 1, "step": step, "message": {"content": [{"text": "ok"}]}}}}},
        ])
    multi_events.append({"kind": "harness_event", "data": {"event": {"type": "assistant/message", "data": {
        "turn": 1, "step": 3}}}})
    combined = root_cause_attribution({"required_tools": ["tool_1", "tool_2"]},
        {"status": "succeeded"}, multi_events, {"success": "失败", "reason": "组合未完成"})
    assert combined["primary"]["code"] == "G"


def test_failed_task_retries_stop_at_configured_per_run_limit():
    assert retry_allowed({"attempt_count": 1}, 1)
    assert not retry_allowed({"attempt_count": 2}, 1)
    assert retry_allowed({"attempt_history": [{}, {}]}, 3)
    assert not retry_allowed({"attempt_count": 1}, 0)


def test_needs_user_handoff_is_counted_and_privacy_redacted():
    raw = {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
        "turn": 1, "step": 2, "callId": "call-123", "message": {"content": [{"text": json.dumps({
            "status": "needs_user", "action_id": "secret-id", "preview": {"page_url": "https://private.test"},
        })}]},
    }}}}
    assert pending_user_actions([raw]) == 1
    cleaned = privacy_event(raw)
    packed = json.dumps(cleaned, ensure_ascii=False)
    assert "needs_user" in packed
    assert "secret-id" not in packed and "call-123" in packed
    assert "private.test" not in packed
    evaluation = evaluate({"eval": {"kind": "nonempty"}}, {"status": "needs_user"}, [raw])
    assert evaluation["success"] == "部分成功"
    assert evaluation["failure_category"] == "K_安全边界要求人工确认"
    diagnosis = root_cause_attribution({}, {"status": "needs_user"}, [raw], evaluation)
    assert diagnosis["primary"]["code"] == "K"
    assert diagnosis["primary"]["confidence"] == "confirmed"
    aggregate = confirmation_summary([{"metrics": {"human_confirmations": 0,
        "user_action_requests": 1, "human_intervention_events": 1}}])
    assert aggregate["mean_recorded_confirmation_events"] == 0
    assert aggregate["mean_user_action_requests"] == 1
    assert aggregate["mean_human_interventions"] == 1


def test_web_blocked_provider_denials_and_local_repeat_shortcuts_are_separate_metrics():
    events = []
    for step, text, is_error in [
        (1, "Error: URL resolves to a non-public IP address WEB_BLOCKED_URL", True),
        (2, "WEB_BLOCKED_URL; no network request was repeated", True),
    ]:
        events.extend([
            {"kind": "harness_event", "data": {"event": {"type": "tool/call", "data": {
                "turn": 1, "step": step, "name": "web_fetch", "callId": f"c{step}"}}}},
            {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
                "turn": 1, "step": step, "callId": f"c{step}", "message": {
                    "isError": is_error, "content": [{"text": text}]}}}}},
        ])
    measured = metrics(events)
    assert measured["web_blocked_provider_results"] == 1
    assert measured["web_blocked_repeat_shortcuts"] == 1


def test_agent_turn_stops_promptly_after_explicit_human_action_request():
    result_event = {"kind": "harness_event", "data": {"event": {"type": "tool/result", "data": {
        "turn": 1, "step": 2, "callId": "c1", "message": {"content": [{"text": json.dumps({
            "status": "needs_user", "action_id": "ephemeral-id"})}]},
    }}}}

    class FakeApi:
        def __init__(self):
            self.cancelled = False
        def call(self, method, path, payload=None, headers=None):
            if method == "POST" and path.endswith("/turns"):
                return 202, {"id": "turn-1"}
            if method == "GET" and "turn-1" in path:
                return 200, {"status": "running", "events": {"items": [result_event], "next_cursor": 1}}
            if method == "POST" and path.endswith("/cancel"):
                self.cancelled = True
                return 200, {"status": "stopping"}
            raise AssertionError((method, path))

    api = FakeApi()
    outcome = execute_agent_turn(api, "conversation-1", "read the public page",
                                 time.monotonic() + 5, CallBudget(10), "run-1")
    assert api.cancelled
    assert outcome["user_action_required"] is True
    assert outcome["timed_out"] is False
    assert outcome["turn"]["status"] == "needs_user"
