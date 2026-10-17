import json

from tools.campus_bench import build_tool_map


def test_real_trace_observations_separate_visible_selected_and_executed(tmp_path, monkeypatch):
    monkeypatch.setattr(build_tool_map, "ROOT", tmp_path)
    run_dir = tmp_path / "var" / "campus-bench" / "batch-a" / "runs"
    run_dir.mkdir(parents=True)
    record = {
        "environment": "real_api",
        "trace": [
            {"kind": "model_call", "data": {
                "tool_schema_snapshot": {"schemas": [{"name": "campus_test_model"}]},
                "selected_tools": ["campus_test_model"],
            }},
            {"kind": "tool_started", "data": {"name": "test.read"}},
            {"kind": "harness_event", "data": {"event": {"type": "tool/call",
                "data": {"name": "web_search"}}}},
        ],
    }
    (run_dir / "BAS-01-run01.json").write_text(json.dumps(record), encoding="utf-8")
    (run_dir / "mock-run.json").write_text(json.dumps({"environment": "mock", "trace": []}), encoding="utf-8")

    result = build_tool_map.real_trace_observations({"campus_test_model": "campus_test"})

    assert result["real_api_run_count_scanned"] == 1
    assert result["batches_with_schema_snapshots"] == 1
    assert result["tools"]["campus_test"] == {"visible_runs": 1, "selected_runs": 0,
        "executed_runs": 0, "selected_calls": 0, "model_selected_runs": 1,
        "model_selected_calls": 1, "executed_calls": 0}
    assert result["tools"]["test.read"]["executed_runs"] == 1
    assert result["tools"]["web_search"]["selected_runs"] == 1
    assert result["tools"]["web_search"]["selected_calls"] == 1
    assert result["tools"]["web_search"]["executed_runs"] == 1


def test_domain_tool_observation_uses_canonical_domain_name_after_alias_mapping():
    observations = {"courses.list": {"visible_runs": 9, "selected_runs": 3,
                                     "executed_runs": 3, "executed_calls": 4}}
    spec = {"name": "courses.list", "model_name": "campus_courses_list"}

    assert build_tool_map.domain_tool_observation(observations, spec) == observations["courses.list"]


def test_runtime_schema_drift_observations_compare_live_snapshots_to_source(tmp_path):
    runs = tmp_path / "batch-a" / "runs"
    runs.mkdir(parents=True)
    source = [{"name": "courses.list", "model_name": "campus_courses_list",
               "description": "Current description", "input_schema": {"type": "object"}}]
    trace = {"environment": "real_api", "trace": [{"kind": "model_call", "data": {
        "tool_schema_snapshot": {"schemas": [
            {"name": "campus_courses_list", "description": "Old description",
             "parameters": {"type": "object"}},
        ]},
    }}]}
    (runs / "BUS-11-run01.json").write_text(json.dumps(trace), encoding="utf-8")
    result = build_tool_map.runtime_schema_drift_observations(source, tmp_path)

    assert result == {"campus_courses_list": {
        "snapshot_observations": 1, "matching": 0, "drifting": 1,
        "differing_fields": {"description": 1},
        "observations_with_runtime_fingerprint": 0,
        "observations_without_runtime_fingerprint": 1,
        "comparison_scope": "historical_snapshot_vs_current_registry; not a live root-cause finding",
        "current_live_runtime_verified": False,
    }}


def test_schema_comparison_records_batch_fingerprint_provenance_without_claiming_live_match(tmp_path):
    batch = tmp_path / "batch-a"
    runs = batch / "runs"
    runs.mkdir(parents=True)
    (batch / "manifest.json").write_text(json.dumps({
        "agent_runtime_source_sha256": "a" * 64,
    }), encoding="utf-8")
    source = [{"name": "courses.list", "model_name": "campus_courses_list",
               "description": "same", "input_schema": {"type": "object"}}]
    trace = {"environment": "real_api", "trace": [{"kind": "model_call", "data": {
        "tool_schema_snapshot": {"schemas": [{"name": "campus_courses_list",
            "description": "same", "parameters": {"type": "object"}}]},
    }}]}
    (runs / "BUS-11-run01.json").write_text(json.dumps(trace), encoding="utf-8")

    row = build_tool_map.runtime_schema_drift_observations(source, tmp_path)["campus_courses_list"]

    assert row["observations_with_runtime_fingerprint"] == 1
    assert row["observations_without_runtime_fingerprint"] == 0
    assert row["current_live_runtime_verified"] is False


def test_material_dependency_does_not_claim_review_tool_coverage():
    scenarios = [{"id": "MULTI-02", "required_tools": ["materials", "assignments", "plans.preview"],
                  "run_status": "runnable", "capability_state": "implemented"}]

    assert build_tool_map.scenario_crosswalk("reviews.preflight", scenarios) == []
    assert build_tool_map.scenario_crosswalk("materials.list", scenarios)[0]["id"] == "MULTI-02"
