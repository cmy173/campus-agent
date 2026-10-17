"""Offline Harness/model bridge round-trip used by the campus benchmark layer.

This deliberately mocks only the provider response. The real FastAPI model/tool
endpoints, domain executor, journal events, argument parser, and provider message
projection remain under test. No external model or campus service is contacted.
"""
import json

import requests
from sqlalchemy.orm import Session

from app import models as m
from app.agent_bridge import model, tools
from test_agent_gateway import conversation, gateway, internal, turn


def test_webvpn_schema_limits_lookup_to_explicit_remote_access_intent():
    description = dict(tools.SPECS)["campus.webvpn.entry"][1]
    assert "只在用户明确询问校外访问或VPN入口" in description
    assert "普通询问校园平台用途/入口无需查VPN映射" in description
    assert "不等于已登录或数据连通" in description


def provider_reply(monkeypatch, message):
    captured = []

    def post(url, **kwargs):
        captured.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
        return response

    monkeypatch.setattr(model.requests, "post", post)
    return captured


def test_harness_tool_call_result_round_trip(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    conversation_id = conversation(client)
    turn_id = turn(client, conversation_id, text="查找测试课程并报告课程名")
    token = tokens[turn_id]
    schema = next(s for s in model.registry_schemas(None) if s["name"] == "campus_courses_list")
    user_message = {"role": "user", "content": [{"type": "text", "text": "查找测试课程并报告课程名"}]}

    first_provider = provider_reply(monkeypatch, {
        "content": None,
        "tool_calls": [{"id": "bench-call-1", "type": "function", "function": {
            "name": "campus_courses_list", "arguments": '{"query":"测试课程"}'}}],
    })
    first = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message], "tools": [schema]})
    assert first.status_code == 200, first.text
    call = first.json()["tool_calls"][0]
    assert call == {"id": "bench-call-1", "name": "campus_courses_list", "arguments": {"query": "测试课程"}}
    assert [item["function"]["name"] for item in first_provider[0][1]["json"]["tools"]] == ["campus_courses_list"]

    result = internal(client, token, "tool", {"conversation_id": conversation_id,
        "call_id": call["id"], "name": "courses.list", "arguments": call["arguments"]})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["items"][0]["title"] == "测试课程"

    second_provider = provider_reply(monkeypatch, {"content": "找到课程：测试课程。", "tool_calls": []})
    continued = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message,
            {"role": "assistant", "content": [{"type": "tool-call", "id": call["id"],
                "name": call["name"], "arguments": call["arguments"]}]},
            {"role": "tool", "toolCallId": call["id"], "content": [{"type": "text",
                "text": json.dumps(result.json(), ensure_ascii=False)}]}],
        "tools": [schema]})
    assert continued.status_code == 200, continued.text
    assert continued.json()["content"] == "找到课程：测试课程。"
    projected = second_provider[0][1]["json"]["messages"]
    assert projected[-1] == {"role": "tool", "tool_call_id": "bench-call-1",
        "content": json.dumps(result.json(), ensure_ascii=False)}

    journal = runtime.store.events(turn_id)["items"]
    model_events = [e for e in journal if e["kind"] == "model_call"]
    tool_events = [e for e in journal if e["kind"] == "tool_result"]
    assert len(model_events) == 2 and len(tool_events) == 1
    assert model_events[0]["data"]["selected_tools"] == ["campus_courses_list"]
    assert model_events[1]["data"]["available_tools"] == ["campus_courses_list"]
    snapshot = model_events[0]["data"]["tool_schema_snapshot"]
    assert snapshot["truncated"] is False
    assert snapshot["schemas"] == [schema]
    assert len(snapshot["sha256"]) == 64
    assert tool_events[0]["data"]["result"]["data"]["items"][0]["title"] == "测试课程"


def test_unsupported_public_notice_body_error_is_explained_and_returned_to_model(gateway, monkeypatch):
    """A public notice summary ID is not a college notice body ID; expose one actionable error."""
    from types import SimpleNamespace
    from connectors.campus.campus_common import CampusError

    client, runtime, tokens, _ = gateway
    conversation_id = conversation(client)
    turn_id = turn(client, conversation_id, text="读取这条校园公开通知正文")
    token = tokens[turn_id]
    notice_schema = next(s for s in model.registry_schemas(None)
                         if s["name"] == "campus_campus_notice_read")
    user_message = {"role": "user", "content": [{"type": "text", "text": "读取这条校园公开通知正文"}]}

    provider_reply(monkeypatch, {"content": None, "tool_calls": [{
        "id": "bench-public-notice-read", "type": "function", "function": {
            "name": "campus_campus_notice_read", "arguments": '{"record_id":"public:123"}'}}]})
    first = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message], "tools": [notice_schema]})
    assert first.status_code == 200, first.text
    call = first.json()["tool_calls"][0]

    class Service:
        def cached_college_notice(self, _user, _record_id):
            raise CampusError("UNSUPPORTED", "opaque backend detail")

    workspace = SimpleNamespace(manager=SimpleNamespace(service=Service()))
    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: workspace)
    result = internal(client, token, "tool", {"conversation_id": conversation_id,
        "call_id": call["id"], "name": "campus.notice.read", "arguments": call["arguments"]})
    assert result.status_code == 200, result.text
    envelope = result.json()
    assert envelope["status"] == "error"
    assert envelope["error"]["code"] == "UNSUPPORTED"
    assert "college_notices" in envelope["error"]["message"]
    assert "校级 notices" in envelope["error"]["message"]
    assert "opaque backend detail" not in json.dumps(envelope, ensure_ascii=False)

    followup = provider_reply(monkeypatch, {"content": "公开列表摘要不能读取正文，请使用列表中的标题、日期和来源。",
                                             "tool_calls": []})
    continued = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message,
            {"role": "assistant", "content": [{"type": "tool-call", "id": call["id"],
                "name": call["name"], "arguments": call["arguments"]}]},
            {"role": "tool", "toolCallId": call["id"],
             "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}]}],
        "tools": [notice_schema]})
    assert continued.status_code == 200, continued.text
    projected = followup[0][1]["json"]["messages"][-1]
    assert projected["role"] == "tool" and projected["tool_call_id"] == call["id"]
    assert "college_notices" in projected["content"]
    assert "opaque backend detail" not in projected["content"]


def test_jobs_read_omitted_wait_round_trips_through_harness(gateway, monkeypatch):
    """The model may omit wait_seconds; dispatch must use the bounded sync default and return it."""
    client, runtime, tokens, engine = gateway
    conversation_id = conversation(client)
    turn_id = turn(client, conversation_id, text="检查刚才的同步任务是否完成")
    token = tokens[turn_id]
    job_id = "bench-sync-job"
    with Session(engine) as db:
        db.add(m.Job(id=job_id, type="sync", status="running", payload=None))
        db.commit()
    with runtime.store.connection() as journal_db:
        row = journal_db.execute("SELECT settings FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        settings = json.loads(row["settings"])
        settings["job_ids"] = [job_id]
        journal_db.execute("UPDATE conversations SET settings=? WHERE id=?",
                           (json.dumps(settings, ensure_ascii=False), conversation_id))

    schema = next(s for s in model.registry_schemas(None) if s["name"] == "campus_jobs_read")
    user_message = {"role": "user", "content": [{"type": "text", "text": "检查刚才的同步任务是否完成"}]}
    first_provider = provider_reply(monkeypatch, {
        "content": None,
        "tool_calls": [{"id": "bench-job-read-1", "type": "function", "function": {
            "name": "campus_jobs_read", "arguments": json.dumps({"job_id": job_id})}}],
    })
    first = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message], "tools": [schema]})
    assert first.status_code == 200, first.text
    selected = first.json()["tool_calls"][0]
    assert selected["arguments"] == {"job_id": job_id}

    states = iter(("running", "succeeded"))
    monkeypatch.setattr("app.agent_bridge.tools.jobs.get_job", lambda jid, db:
                        {"id": jid, "type": "sync", "status": next(states),
                         "errors": [], "warnings": [], "result_refs": []})
    monkeypatch.setattr("app.agent_bridge.tools.time.sleep", lambda _seconds: None)
    result = internal(client, token, "tool", {"conversation_id": conversation_id,
        "call_id": selected["id"], "name": "jobs.read", "arguments": selected["arguments"]})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["status"] == "succeeded"

    second_provider = provider_reply(monkeypatch, {"content": "同步已完成。", "tool_calls": []})
    continued = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message,
            {"role": "assistant", "content": [{"type": "tool-call", "id": selected["id"],
                "name": selected["name"], "arguments": selected["arguments"]}]},
            {"role": "tool", "toolCallId": selected["id"], "content": [{"type": "text",
                "text": json.dumps(result.json(), ensure_ascii=False)}]}],
        "tools": [schema]})
    assert continued.status_code == 200, continued.text
    assert continued.json()["content"] == "同步已完成。"
    projected = second_provider[0][1]["json"]["messages"][-1]
    assert projected["role"] == "tool" and projected["tool_call_id"] == selected["id"]
    assert "succeeded" in projected["content"]
    assert len(first_provider[0][1]["json"]["tools"]) == 1


def test_material_search_then_read_round_trips_evidence_through_harness(gateway, monkeypatch):
    """A multi-tool materials task must read only the exact versioned search hit."""
    from app import models as m
    from sqlalchemy.orm import Session

    client, runtime, tokens, engine = gateway
    conversation_id = conversation(client)
    turn_id = turn(client, conversation_id, text="查找课程目标并读取对应原文片段")
    token = tokens[turn_id]
    with Session(engine) as db:
        db.add(m.Resource(id="bench_material", course_id="course_a", kind="syllabus",
            title="测试课程大纲", version=2, parse_status="ready", access_level="full"))
        db.flush()
        db.add(m.ContentChunk(id="bench_chunk", resource_id="bench_material", resource_version=2,
            ordinal=0, text="课程目标：掌握线性代数基本概念与证明方法。",
            locator={"section": "课程目标"}, provenance={"kind": "synthetic"}))
        db.commit()

    search_schema = next(s for s in model.registry_schemas(None) if s["name"] == "campus_materials_search")
    read_schema = next(s for s in model.registry_schemas(None) if s["name"] == "campus_materials_read")
    user_message = {"role": "user", "content": [{"type": "text", "text": "查找课程目标并读取对应原文片段"}]}
    search_provider = provider_reply(monkeypatch, {
        "content": None,
        "tool_calls": [{"id": "bench-material-search", "type": "function", "function": {
            "name": "campus_materials_search", "arguments": json.dumps({
                "course_id": "course_a", "query": "课程目标"})}}],
    })
    first = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message], "tools": [search_schema, read_schema]})
    assert first.status_code == 200, first.text
    search_call = first.json()["tool_calls"][0]
    assert search_call["name"] == "campus_materials_search"
    search_result_response = internal(client, token, "tool", {"conversation_id": conversation_id,
        "call_id": search_call["id"], "name": "materials.search", "arguments": search_call["arguments"]})
    assert search_result_response.status_code == 200, search_result_response.text
    search_result = search_result_response.json()
    hit = search_result["data"]["items"][0]
    assert hit["resource_id"] == "bench_material" and hit["resource_version"] == 2

    read_arguments = {"resource_id": hit["resource_id"], "expected_version": hit["resource_version"],
                      "chunk_ids": [hit["chunk_id"]]}
    read_provider = provider_reply(monkeypatch, {
        "content": None,
        "tool_calls": [{"id": "bench-material-read", "type": "function", "function": {
            "name": "campus_materials_read", "arguments": json.dumps(read_arguments)}}],
    })
    second = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message,
            {"role": "assistant", "content": [{"type": "tool-call", "id": search_call["id"],
                "name": search_call["name"], "arguments": search_call["arguments"]}]},
            {"role": "tool", "toolCallId": search_call["id"], "content": [{"type": "text",
                "text": json.dumps(search_result, ensure_ascii=False)}]}],
        "tools": [search_schema, read_schema]})
    assert second.status_code == 200, second.text
    read_call = second.json()["tool_calls"][0]
    assert read_call["arguments"] == read_arguments
    read_result_response = internal(client, token, "tool", {"conversation_id": conversation_id,
        "call_id": read_call["id"], "name": "materials.read", "arguments": read_call["arguments"]})
    assert read_result_response.status_code == 200, read_result_response.text
    read_result = read_result_response.json()
    assert read_result["data"]["items"][0]["text"].startswith("课程目标：")

    final_provider = provider_reply(monkeypatch, {"content": "原文目标是掌握线性代数基本概念与证明方法。", "tool_calls": []})
    final = internal(client, token, "model", {"conversation_id": conversation_id,
        "messages": [user_message,
            {"role": "assistant", "content": [{"type": "tool-call", "id": read_call["id"],
                "name": read_call["name"], "arguments": read_call["arguments"]}]},
            {"role": "tool", "toolCallId": read_call["id"], "content": [{"type": "text",
                "text": json.dumps(read_result, ensure_ascii=False)}]}],
        "tools": [search_schema, read_schema]})
    assert final.status_code == 200, final.text
    assert "线性代数基本概念" in final.json()["content"]
    assert search_provider[0][1]["json"]["tools"]
    assert read_provider[0][1]["json"]["tools"]
    assert final_provider[0][1]["json"]["messages"][-1]["tool_call_id"] == read_call["id"]
    tool_results = [e for e in runtime.store.events(turn_id)["items"] if e["kind"] == "tool_result"]
    assert len(tool_results) == 2
    assert {e["data"]["name"] for e in tool_results} == {"materials.search", "materials.read"}
    assert all(e["data"]["result"].get("error") is None for e in tool_results)


def test_tool_schema_snapshot_redacts_auth_fields_and_bounds_journal_payload():
    snapshot = model.tool_schema_snapshot([{
        "name": "mcp_example", "parameters": {"type": "object", "properties": {
            "api_token": {"type": "string"}, "query": {"type": "string"}}},
    }])
    assert snapshot["schemas"][0]["parameters"]["properties"]["api_token"] == "[REDACTED]"
    oversized = model.tool_schema_snapshot([{"name": "large", "description": "x" * 70_000}])
    assert oversized["truncated"] is True
    assert "schemas" not in oversized
    assert oversized["count"] == 1


def test_native_provider_observation_records_model_visible_schema(gateway):
    client, runtime, tokens, _ = gateway
    conversation_id = conversation(client)
    turn_id = turn(client, conversation_id, text="只读测试")
    schema = {"name": "web_search", "description": "Search public pages", "parameters": {
        "type": "object", "properties": {"query": {"type": "string"}}}}
    response = internal(client, tokens[turn_id], "model-observation", {
        "conversation_id": conversation_id, "duration_ms": 123.4,
        "tools": [schema], "selected_tools": ["web_search"],
    })
    assert response.status_code == 200, response.text
    event = next(e for e in runtime.store.events(turn_id)["items"] if e["kind"] == "model_call")
    assert event["data"]["source"] == "native_provider_llm_stream"
    assert event["data"]["available_tools"] == ["web_search"]
    assert event["data"]["selected_tools"] == ["web_search"]
    assert event["data"]["tool_schema_snapshot"]["schemas"] == [schema]


def test_native_provider_model_error_keeps_timing_and_visible_schema(gateway):
    client, runtime, tokens, _ = gateway
    conversation_id = conversation(client)
    turn_id = turn(client, conversation_id, text="只读测试")
    # This focuses on the journal payload and makes no provider request.
    token = tokens[turn_id]
    schema = {"name": "web_fetch", "description": "Fetch a public page", "parameters": {
        "type": "object", "properties": {"url": {"type": "string"}}}}
    response = internal(client, token, "model-error", {
        "conversation_id": conversation_id, "code": "PROVIDER_TIMEOUT",
        "message": "provider timed out", "retryable": True, "duration_ms": 987.6,
        "tools": [schema],
    })
    assert response.status_code == 200, response.text
    event = next(e for e in runtime.store.events(turn_id)["items"] if e["kind"] == "model_error")
    assert event["data"]["duration_ms"] == 987.6
    assert event["data"]["available_tools"] == ["web_fetch"]
    assert event["data"]["tool_schema_snapshot"]["schemas"] == [schema]
