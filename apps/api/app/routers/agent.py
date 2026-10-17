"""Local-only conversation API and per-turn internal bridge, additive to v0.2."""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query, Request, Response, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..agent_bridge import model, tools
from ..agent_bridge import plan_actions
from ..agent_bridge.runtime import get_runtime
from ..db import get_db
from ..errors import ApiError
from ..providers import ProviderCallError
from ..agent_bridge.settings import RuntimeSettings, harness_server
from ..agent_bridge import diagnostics
from ..agent_bridge.files import Preview
from ..agent_bridge.browser import deliver_downloads, BrowserStatus, BrowserReceipt, BrowserOpenResult, BrowserStopResult
from .. import secrets_store

router = APIRouter(prefix="/agent", tags=["campus-agent"])
COOKIE = "campus_agent_owner"


class ConversationRequest(tools.Input):
    provider_id: str = Field(min_length=1, max_length=128)
    # Compatibility field only: saved, enabled provider configuration is consent.
    send_to_provider: bool | None = None
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Benchmark-only schema exposure treatment; never grants tool capability.
    benchmark_tool_groups: list[Literal['campus_reviews']] = Field(default_factory=list, max_length=4)


class TurnRequest(tools.Input):
    text: str = Field(min_length=1, max_length=8000)
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    clear_course_context: bool = False
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)


class TurnResponse(tools.Input):
    id: str
    conversation_id: str
    text: str
    status: Literal['queued', 'running', 'stopping', 'succeeded', 'failed', 'cancelled', 'interrupted']
    response: str
    error: dict[str, Any] | None
    created_at: str
    updated_at: str
    attachment_ids: list[str]


class TurnWithEvents(TurnResponse):
    events: dict[str, Any]


class AcceptActionRequest(tools.Input):
    operation: Literal["apply", "revoke"]


class ToolRequest(tools.Input):
    conversation_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]


class ModelRequest(tools.Input):
    conversation_id: str = Field(min_length=1, max_length=128)
    # Tool discovery adds small messages to long-lived sessions. Bound payload
    # content in internal_model, not the number of historical message records.
    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    max_tokens: int = Field(default=16384, ge=1, le=65536)
    attribution_headers: dict[str, str] = Field(default_factory=dict)


def _loaded_tool_groups(messages: list[dict[str, Any]]) -> set[str]:
    """Read loader receipts only when tied to the matching assistant tool-call ID."""
    loaded: set[str] = set()
    loader_calls: set[str] = set()

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool-call":
                continue
            call_id = block.get("id")
            name = block.get("name")
            arguments = block.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    continue
            if (isinstance(call_id, str) and name == "load_tools" and isinstance(arguments, dict)
                    and arguments.get("group") == "campus_reviews"):
                loader_calls.add(call_id)

    def inspect(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, str):
            if len(value) > 32_000:
                return
            try:
                inspect(json.loads(value), depth + 1)
            except (TypeError, ValueError):
                return
        elif isinstance(value, dict):
            groups = value.get("loaded_groups")
            if isinstance(groups, list):
                loaded.update(item for item in groups if isinstance(item, str) and len(item) <= 64)
            for item in value.values():
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:128]:
                inspect(item, depth + 1)

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = message.get("toolCallId") or message.get("tool_call_id")
        if isinstance(call_id, str) and call_id in loader_calls:
            inspect(message.get("content"))
    return loaded


def _model_visible_tool_schemas(
    schemas: list[dict[str, Any]], deferred_groups: tuple[str, ...], messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the benchmark exposure treatment at the trusted model request boundary.

    Registry entries and execution authorization remain unchanged. Only schemas sent to
    the provider are deferred, and a tool-role loader receipt opens the group later.
    """
    if "campus_reviews" not in deferred_groups or "campus_reviews" in _loaded_tool_groups(messages):
        return schemas
    visible = []
    for item in schemas:
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name.startswith("campus_reviews_"):
            continue
        visible.append(item)
    return visible


class ModelLeaseRequest(tools.Input):
    conversation_id: str = Field(min_length=1, max_length=128)


class ModelObservationRequest(ModelLeaseRequest):
    duration_ms: float = Field(ge=0, le=600_000)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    selected_tools: list[str] = Field(default_factory=list, max_length=64)


class NativeToolLeaseRequest(ModelLeaseRequest):
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)


class PublishFileRequest(ModelLeaseRequest):
    call_id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=4096)


class ControlAddressRequest(ModelLeaseRequest):
    url: str = Field(pattern=r'^http://127\.0\.0\.1:\d{1,5}$')


class GoalPauseRequest(tools.Input):
    id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)


class GoalResumeRequest(GoalPauseRequest):
    maxGoalRounds: int | None = Field(default=None, ge=1, le=10000)


class BrowserToolRequest(ModelLeaseRequest):
    method: Literal['tools', 'call']
    name: str = Field(default='', max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class BrowserOpenRequest(tools.Input):
    url: str | None = Field(default=None, max_length=8192)

    @classmethod
    def checked_url(cls, value):
        if value and urlsplit(value).scheme not in ('http', 'https'):
            raise ApiError(400, 'INVALID_INPUT', '浏览器地址需要 HTTP/HTTPS URL')
        return value


class ModelErrorRequest(ModelLeaseRequest):
    code: Literal['PROVIDER_AUTH_FAILED', 'PROVIDER_RATE_LIMITED', 'PROVIDER_TIMEOUT',
                  'PROVIDER_UNREACHABLE', 'PROVIDER_INVALID_RESPONSE', 'PROVIDER_HTTP_ERROR',
                  'PROVIDER_CHANGED', 'PROVIDER_DISABLED', 'BUDGET_EXCEEDED', 'CONSENT_REQUIRED']
    message: str = Field(min_length=1, max_length=512)
    retryable: bool
    duration_ms: float = Field(default=0, ge=0, le=600_000)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=256)


class SearchTestRequest(tools.Input):
    provider_id: str | None = Field(default=None, min_length=1, max_length=128)


def browser_owner(request: Request, response: Response) -> str:
    # Local single-person workspace, not authentication for a hosted multiuser DB.
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ApiError(403, "LOCAL_ONLY", "对话 API 当前仅供本机使用")
    origin = request.headers.get("origin")
    allowed = {str(request.base_url).rstrip("/")} | set(os.environ.get("CAMPUS_WEB_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(","))
    if origin and origin.rstrip("/") not in allowed:
        raise ApiError(403, "ORIGIN_DENIED", "请求来源不在本机网页允许列表中")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError(403, "ORIGIN_DENIED", "不接受跨站点会话请求")
    owner = request.cookies.get(COOKIE)
    if not owner or len(owner) != 43:
        owner = secrets.token_urlsafe(32)
        response.set_cookie(COOKIE, owner, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30,
                            secure=request.url.scheme == "https", path="/api/v1/agent")
    return owner


def runtime_authority(authorization: str | None = Header(default=None)) -> tuple:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(403, "RUNTIME_DENIED", "内部网关需要本轮运行时授权")
    runtime = get_runtime()
    token = authorization[7:]
    try:
        turn, conversation = runtime.store.runtime(token)
    except ApiError:
        # A prewarmed runtime boots before its conversation's first turn exists, so its
        # conversation-scoped grant may only be used for conversation-level bridge calls;
        # no turn-scoped effect can come from it.
        conversation = runtime.store.granted(token)
        if conversation is None:
            raise
        turn = {"id": "prewarm:" + conversation["id"], "conversation_id": conversation["id"],
                "status": "queued", "text": "", "attachment_ids": []}
    return runtime, turn, conversation


def public_conversation(item: dict) -> dict:
    settings = item["settings"]
    return {k: item[k] for k in ("id", "created_at", "updated_at")} | {
        "provider_id": settings["provider"]["id"], "course_ids": settings["course_ids"],
        "current_course_id": settings.get("current_course_id"), "send_to_provider": settings["send_to_provider"]}


@router.get('/settings')
def read_settings(owner: str = Depends(browser_owner)):
    return get_runtime().settings.public()


@router.get('/benchmark/tool-exposure')
def benchmark_tool_exposure(owner: str = Depends(browser_owner)):
    enabled = os.environ.get('CAMPUS_BENCH_ALLOW_TOOL_GROUPS') == '1'
    return {"enabled": enabled, "groups": ["campus_reviews"] if enabled else []}


@router.get('/conversations/{conversation_id}/browser', response_model=BrowserStatus)
def browser_status(conversation_id: str, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    return deliver_downloads(runtime, conversation_id, runtime.browser.peek(conversation_id))


@router.post('/conversations/{conversation_id}/browser/open', response_model=BrowserOpenResult)
def browser_open(conversation_id: str, req: BrowserOpenRequest, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    return runtime.browser.rpc(conversation_id, 'open', url=req.checked_url(req.url))


@router.post('/conversations/{conversation_id}/browser/stop', response_model=BrowserStopResult)
def browser_stop(conversation_id: str, req: tools.Empty, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    return runtime.browser.rpc(conversation_id, 'stop')


@router.post('/conversations/{conversation_id}/browser/actions/{action_id}/{operation}', response_model=BrowserReceipt)
def browser_action(conversation_id: str, action_id: str, operation: Literal['confirm', 'reject'],
                   req: tools.Empty, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    result = runtime.browser.rpc(conversation_id, operation, action_id=action_id)
    deliver_downloads(runtime, conversation_id, runtime.browser.rpc(conversation_id, 'status'))
    return result


@router.post('/internal/browser')
def internal_browser(req: BrowserToolRequest, auth=Depends(runtime_authority)):
    runtime, turn, conversation = auth
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'SCOPE_DENIED', '浏览器调用不属于本轮会话')
    if not str(turn['id']).startswith('prewarm:') and req.method == 'call' and runtime.auth_resume_context(turn):
        raise ApiError(403, 'RECOVERY_READ_ONLY', '自动认证续任务不执行浏览器操作；已取得的查询结果会继续使用')
    prewarming = str(turn['id']).startswith('prewarm:')
    if prewarming and req.method != 'tools':
        # A prewarmed runtime may discover the browser tools, but nothing may act on a
        # turn that does not exist yet.
        raise ApiError(409, 'TURN_NOT_RUNNING', '预热中的运行时只能发现浏览器工具')
    # Browser IO is serialized independently of short tool leases/local tools.
    # Holding the whole-turn lock during a slow navigation starved sibling reads.
    lock_key = 'browser-call:' + turn['id'] if not prewarming else 'browser:' + conversation['id']
    with runtime.call_lock(lock_key):
        if prewarming:
            return runtime.browser.rpc(conversation['id'], req.method, name=req.name,
                                       arguments=req.arguments, turn_id=turn['id'])
        if runtime.store.turn(turn['id'])['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮正在停止，不再启动浏览器操作')
        result = runtime.browser.rpc(conversation['id'], req.method, name=req.name, arguments=req.arguments, turn_id=turn['id'])
        if runtime.store.turn(turn['id'])['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮正在停止，浏览器操作已结束')
        if req.method == 'call':
            status = runtime.browser.rpc(conversation['id'], 'status')
            deliver_downloads(runtime, conversation['id'], status, turn['id'])
        return result


@router.put('/settings')
def save_settings(req: RuntimeSettings, owner: str = Depends(browser_owner)):
    return get_runtime().settings.save(req)


@router.post('/settings/mcp/{server_name}/test')
def test_mcp(server_name: str, req: tools.Empty, owner: str = Depends(browser_owner)):
    server = next((s for s in get_runtime().settings.resolved()['mcp_servers'] if s['serverName'] == server_name), None)
    if server is None:
        raise ApiError(404, 'NOT_FOUND', 'MCP 服务配置不存在')
    return diagnostics.check({'kind': 'mcp', 'server': harness_server(server)})


@router.post('/settings/search/test')
def test_search(req: SearchTestRequest, owner: str = Depends(browser_owner), db: Session = Depends(get_db)):
    shared = None
    if req.provider_id:
        if db.get(m.ProviderConfig, req.provider_id) is None:
            raise ApiError(404, 'NOT_FOUND', '共享密钥的模型配置不存在')
        shared = secrets_store.get_key(req.provider_id)
    search = get_runtime().settings.resolved(shared)['search']
    return diagnostics.check({'kind': 'search', 'key': search['key'], 'base_url': search['base_url'], 'model': search['model']})


@router.post("/conversations", status_code=201)
def create_conversation(req: ConversationRequest, owner: str = Depends(browser_owner), db: Session = Depends(get_db)):
    if req.benchmark_tool_groups and os.environ.get('CAMPUS_BENCH_ALLOW_TOOL_GROUPS') != '1':
        raise ApiError(403, 'BENCHMARK_EXPERIMENT_DISABLED', '此 API 实例未启用校园工具暴露实验')
    if len(set(req.benchmark_tool_groups)) != len(req.benchmark_tool_groups):
        raise ApiError(422, 'VALIDATION_FAILED', 'benchmark_tool_groups 不允许重复')
    cfg = db.get(m.ProviderConfig, req.provider_id)
    if cfg is None:
        raise ApiError(404, "NOT_FOUND", "所选模型配置不存在")
    if not cfg.enabled:
        raise ApiError(403, "PROVIDER_DISABLED", "该模型服务已停用，可在设置中启用或选择其他模型")
    if cfg.protocol != "openai_chat_completions":
        raise ApiError(400, "UNSUPPORTED", "当前对话仅支持 Chat Completions 兼容服务")
    if req.benchmark_tool_groups:
        provider_runtime = model.runtime_configuration(db, {"provider": model.snapshot(cfg)})
        if provider_runtime["provider"] != "campus-model":
            raise ApiError(400, "BENCHMARK_EXPERIMENT_UNSUPPORTED",
                           "工具暴露实验要求通过校园模型网关发送工具 schema")
    course_ids = list(db.scalars(select(m.Course.id).where(m.Course.archived_at.is_(None))))
    if req.course_id and req.course_id not in course_ids:
        raise ApiError(403, "SCOPE_DENIED", "所选课程不在本机可用范围内")
    connection_ids = list(db.scalars(select(m.Connection.id)))
    item = get_runtime().store.create(owner, {"provider": model.snapshot(cfg), "send_to_provider": True,
        "course_ids": course_ids, "connection_ids": connection_ids, "job_ids": [], "current_course_id": req.course_id,
        "benchmark_tool_groups": req.benchmark_tool_groups})
    # Start the runtime while the user is still typing: composing the upstream profile is
    # what makes a cold first turn wait. Best effort and never on the request path.
    get_runtime().prewarm_async(item["id"])
    return public_conversation(item)


@router.get("/conversations")
def list_conversations(owner: str = Depends(browser_owner), cursor: str | None = Query(default=None, max_length=1024),
                       search: str = Query(default="", max_length=200), limit: int = Query(default=50, ge=1, le=100)):
    runtime = get_runtime()
    page = runtime.store.conversation_page(owner, cursor=cursor, search=search, limit=limit)
    return {"items": [public_conversation(c) | {"title": c["title"]} for c in page["items"]], "next_cursor": page["next_cursor"]}


@router.get("/conversations/{conversation_id}")
def read_conversation(conversation_id: str, owner: str = Depends(browser_owner)):
    store = get_runtime().store
    return public_conversation(store.conversation(conversation_id, owner)) | {"turns": store.turns(conversation_id)}


@router.get('/conversations/{conversation_id}/files')
def list_conversation_files(conversation_id: str, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    return {'items': runtime.files.list(conversation_id)}


@router.post('/conversations/{conversation_id}/files', status_code=201)
def upload_conversation_file(conversation_id: str, file: UploadFile = File(), owner: str = Depends(browser_owner),
                             idempotency_key: str = Header(min_length=1, max_length=128)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    try:
        return runtime.files.save(conversation_id, file.file, file.filename or '', 'input', 'upload:' + idempotency_key)
    finally:
        file.file.close()


@router.get('/conversations/{conversation_id}/files/{file_id}/preview', response_model=Preview)
def preview_conversation_file(conversation_id: str, file_id: str, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    return runtime.files.preview(conversation_id, file_id)


@router.get('/conversations/{conversation_id}/files/{file_id}/content')
def content_conversation_file(conversation_id: str, file_id: str, owner: str = Depends(browser_owner), inline: bool = False):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    item, path = runtime.files.get(conversation_id, file_id)
    can_inline = inline and item['mime_type'] in {'application/pdf', 'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
    return FileResponse(path, filename=item['name'], media_type=item['mime_type'],
                        content_disposition_type='inline' if can_inline else 'attachment',
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'})


@router.post("/conversations/{conversation_id}/turns", status_code=202)
def send_turn(conversation_id: str, req: TurnRequest, owner: str = Depends(browser_owner),
              idempotency_key: str = Header(min_length=1, max_length=128), db: Session = Depends(get_db)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    if not req.text.strip():
        raise ApiError(400, "INVALID_INPUT", "消息不能为空")
    runtime.check_available()
    if req.clear_course_context and req.course_id is not None:
        raise ApiError(400, "INVALID_INPUT", "不能同时选择课程与清除课程上下文")
    accepted = None
    if req.action_id is not None:
        action = plan_actions._owned(db, conversation_id, req.action_id)
        if action.status not in {"approved", "completed"}:
            raise ApiError(403, "APPROVAL_REQUIRED", "请先接受具体方案，再请求执行")
        accepted = {"action_id": action.id, "tool": "plans." + action.kind}
    turn, token = runtime.store.enqueue(conversation_id, idempotency_key, req.text, req.course_id, req.clear_course_context, accepted, req.attachment_ids)
    if token:
        runtime.start(turn, token)
    return turn


@router.get('/conversations/{conversation_id}/recovery')
def conversation_recovery(conversation_id: str, owner: str = Depends(browser_owner)):
    runtime = get_runtime()
    runtime.store.conversation(conversation_id, owner)
    return runtime.recovery_status(conversation_id)


@router.get("/conversations/{conversation_id}/actions")
def list_actions(conversation_id: str, owner: str = Depends(browser_owner), db: Session = Depends(get_db)):
    get_runtime().store.conversation(conversation_id, owner)
    rows = db.scalars(select(m.AgentPlanAction).where(m.AgentPlanAction.conversation_id == conversation_id)
                      .order_by(m.AgentPlanAction.created_at.desc(), m.AgentPlanAction.id).limit(100))
    return {"items": [plan_actions.view(row) for row in rows]}


@router.post("/conversations/{conversation_id}/actions/{action_id}/accept")
def accept_action(conversation_id: str, action_id: str, req: AcceptActionRequest,
                  owner: str = Depends(browser_owner), db: Session = Depends(get_db),
                  idempotency_key: str = Header(min_length=1, max_length=128)):
    conversation = get_runtime().store.conversation(conversation_id, owner)
    settings = conversation["settings"]
    tools._whole_workspace(db, tools.ToolContext("local", conversation_id, frozenset(settings["course_ids"]),
                                               allow_uncoursed_tasks=True))
    action = plan_actions._owned(db, conversation_id, action_id)
    if action.kind != "apply":
        raise ApiError(400, "INVALID_INPUT", "请指定原预览或已应用方案的动作")
    if req.operation == "revoke":
        action_id = plan_actions.prepare_revoke(db, conversation_id, action_id, "browser:" + idempotency_key)["id"]
    return plan_actions.approve(db, conversation_id, action_id)


def owned_turn(turn_id: str, owner: str):
    runtime = get_runtime()
    turn = runtime.store.turn(turn_id)
    runtime.store.conversation(turn["conversation_id"], owner)
    return runtime, turn


@router.get("/turns/{turn_id}", response_model=TurnWithEvents)
def read_turn(turn_id: str, owner: str = Depends(browser_owner), after: int = Query(default=0, ge=0)):
    runtime, turn = owned_turn(turn_id, owner)
    return turn | {"events": runtime.store.events(turn_id, after)}


@router.post("/turns/{turn_id}/cancel", response_model=TurnResponse)
def cancel_turn(turn_id: str, owner: str = Depends(browser_owner)):
    runtime, _ = owned_turn(turn_id, owner)
    runtime.cancel(turn_id)
    with runtime.store.connection() as db:
        db.execute("UPDATE auth_recovery SET status='cancelled',probe_requested=0,claim_token=NULL,lease_until=0 WHERE source_turn_id=? AND status IN ('waiting_auth','ready')", (turn_id,))
    return runtime.store.turn(turn_id)


@router.post('/turns/{turn_id}/subagents/{child_id}/interrupt')
def interrupt_child(turn_id: str, child_id: str, req: tools.Empty, owner: str = Depends(browser_owner)):
    runtime, _ = owned_turn(turn_id, owner)
    return runtime.interrupt_child(turn_id, child_id)


@router.post('/internal/control-address')
def internal_control_address(req: ControlAddressRequest, authority=Depends(runtime_authority), authorization: str = Header()):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    runtime.register_controls(turn['id'], req.url, authorization[7:], conversation['id'])
    return {'ok': True}


@router.post('/turns/{turn_id}/goal/pause')
def pause_goal(turn_id: str, req: GoalPauseRequest, owner: str = Depends(browser_owner)):
    runtime, _ = owned_turn(turn_id, owner)
    state = runtime.goal_control(turn_id, {'operation': 'pause', **req.model_dump()})
    runtime.store.event(turn_id, 'goal.state', state)
    return state


@router.post('/conversations/{conversation_id}/goal/resume', response_model=TurnResponse, status_code=202)
def resume_goal(conversation_id: str, req: GoalResumeRequest, owner: str = Depends(browser_owner),
                idempotency_key: str = Header(min_length=1, max_length=128), db: Session = Depends(get_db)):
    runtime = get_runtime()
    conversation = runtime.store.conversation(conversation_id, owner)
    model.validated_config(db, conversation['settings'])
    runtime.check_available()
    turn, token = runtime.store.enqueue(conversation_id, 'goal-resume:' + idempotency_key,
        '继续已保存的持续任务，按原目标完成并核对结果。', None, goal_resume=req.model_dump(exclude_none=True))
    if token:
        runtime.start(turn, token)
    return turn


@router.post('/internal/goal-state')
def internal_goal_state(req: ModelLeaseRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    if runtime.auth_resume_context(turn):
        return {'goal': None, 'agent_status': 'running'}
    return runtime.goal_control(turn['id'], {'operation': 'read'})


@router.post("/internal/schemas")
def internal_schemas(req: tools.Empty, authority=Depends(runtime_authority)):
    return tools.schemas()


def error_envelope(exc: ApiError, files=None) -> dict:
    error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
    # Only the domain validator's sanitized field/type repair hints are model
    # feedback. Other exception details may carry internal paths or private data.
    if exc.code == "INVALID_INPUT" and isinstance(exc.details, dict):
        issues = exc.details.get("validation_errors")
        if isinstance(issues, list):
            error["details"] = {"validation_errors": [
                {"field": item["field"], "type": item["type"]} for item in issues[:16]
                if isinstance(item, dict) and isinstance(item.get("field"), str) and isinstance(item.get("type"), str)
            ]}
    else:
        # A failed batch publish that could not be rolled back is the one case where the
        # error must carry its own partial result: the model has to know which files really
        # exist instead of being told "nothing was delivered". Server-owned file rows only.
        # The key is added only when there is something to say, so an ordinary failure keeps
        # the same envelope shape as before.
        delivery = sanitized_delivery_details(exc, files)
        if delivery is not None:
            error["details"] = delivery
    return {"status": "error", "data": {}, "evidence": [],
            "coverage": {"scope": "authorized_conversation", "complete": False, "missing": ["工具未成功完成"]},
            "freshness": {"fetched_at": None, "stale": True}, "warnings": [], "next_cursor": None, "job_id": None,
            "error": error}


DELIVERED_FILE_FIELDS = ("id", "conversation_id", "kind", "name", "size", "sha256", "mime_type", "created_at")


def _retryable_error(result: dict) -> bool:
    """True for a cached envelope whose own error says the caller may retry it.

    A retryable failure must not be replayed as a permanent answer: that is what made the
    "retry the same call" advice in a publish-failure message unusable. The stored request
    fingerprint still decides idempotency — a genuinely charged business operation
    (`reviews.generate`) keeps its own receipt/identity guard, so relaxing the cache here
    cannot start a second billed job.
    """
    if not isinstance(result, dict) or result.get("status") != "error":
        return False
    error = result.get("error")
    return bool(isinstance(error, dict) and error.get("retryable") is True)


def sanitized_delivery_details(exc: ApiError, files=None) -> dict | None:
    """`error.details` for a failed batch publish, projected onto server-owned fields only.

    Only ids the conversation download entry can really serve (`files.servable`) are forwarded as
    read receipts; ids that cannot be served are forwarded as pending cleanup. `delivered` is
    empty in exactly that case, so the cleanup/rollback state is returned even with no delivered
    file: otherwise the model would be told nothing about a batch that may still hold a usable
    file. Without a store handle nothing can be verified, so nothing is advertised.
    """
    details = exc.details if isinstance(exc.details, dict) else {}
    delivered = details.get("delivered")
    if not isinstance(delivered, list):
        return None
    # The conversation to verify against: the detailed rows carry it, and the publisher also names
    # it explicitly because a batch that delivered nothing has no row to read it from.
    cid = details.get("conversation_id")
    if not isinstance(cid, str) or cid == "":
        cid = next((item.get("conversation_id") for item in delivered
                    if isinstance(item, dict) and isinstance(item.get("conversation_id"), str)), None)

    def servable_ids(key: str) -> list[str]:
        """Ids the download entry confirmed it can serve."""
        out: list[str] = []
        for item in details.get(key) or []:
            if not isinstance(item, str) or not isinstance(cid, str) or files is None:
                continue
            if files.servable(cid, item) is True:
                out.append(item)
        return out

    def unusable_ids(key: str) -> list[str]:
        """Ids the download entry confirmed it *cannot* serve (never merely "unverified")."""
        out: list[str] = []
        for item in details.get(key) or []:
            if not isinstance(item, str) or not isinstance(cid, str) or files is None:
                continue
            if files.servable(cid, item) is False:
                out.append(item)
        return out

    servable = servable_ids("retained_file_ids")
    cleanup = unusable_ids("cleanup_pending_file_ids")
    files_payload = [{key: item.get(key) for key in DELIVERED_FILE_FIELDS}
                     for item in delivered[:8]
                     if isinstance(item, dict) and isinstance(item.get("id"), str)
                     and item.get("id") in set(servable)]
    rollback = sanitized_rollback(details.get("rollback"))
    if not files_payload and not servable and not cleanup and rollback is None:
        return None
    result: dict = {"delivered": files_payload, "complete": False}
    if servable:
        result["retained_file_ids"] = servable[:8]
    if cleanup:
        result["cleanup_pending_file_ids"] = cleanup[:8]
    if isinstance(details.get("failed_format"), str):
        result["failed_format"] = details["failed_format"]
    if rollback is not None:
        result["rollback"] = rollback
    return result


def sanitized_rollback(rollback) -> dict | None:
    """Bounded per-state rollback ids; server-owned ids only, never file contents.

    Every state key is emitted, empty included, so "this batch had nothing in that state" is
    readable from the payload instead of being inferred from a missing key.
    """
    if not isinstance(rollback, dict):
        return None
    return {key: [item for item in rollback.get(key) or [] if isinstance(item, str)][:8]
            for key in ("removed", "retained", "cleanup_pending", "uncertain")}


@router.post("/internal/tool")
def internal_tool(req: ToolRequest, authority=Depends(runtime_authority), db: Session = Depends(get_db)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation["id"]:
        raise ApiError(403, "RUNTIME_DENIED", "运行时授权不属于指定会话")
    request = {"name": req.name, "arguments": req.arguments}
    # The cache identity is the *business* request: per-tool observation parameters (e.g.
    # `wait_seconds`, which only widens how long one call observes an already-accepted job) are
    # removed by the single canonical helper. Read and write use this same value — normalising on
    # one side only made a first call that already carried a wait window unresolvable on replay
    # (409 for byte-identical arguments). The raw `req.arguments` stays the journal record and the
    # value actually executed.
    cached_request = tools.cache_arguments(req.name, req.arguments)
    with runtime.call_lock(turn["id"]):
        if runtime.store.turn(turn["id"])["status"] != "running":
            raise ApiError(409, "TURN_STOPPED", "本轮已停止")
        cached = runtime.store.cached_call(turn["id"], req.call_id, cached_request)
        if cached is not None and not _retryable_error(cached):
            return cached
        if hasattr(runtime, 'recovery_tool_allowed') and not runtime.recovery_tool_allowed(turn, req.name):
            raise ApiError(403, 'RECOVERY_READ_ONLY', '认证恢复中的自动续任务仅允许查询与分析，不重放写入')
        # Another tool call may just have imported a course after this request's
        # dependency snapshot. Read grants inside the existing per-turn call lock.
        settings = runtime.store.conversation(conversation["id"])["settings"]
        ctx = tools.ToolContext("local", conversation["id"], frozenset(settings["course_ids"]),
                                frozenset(settings["connection_ids"]), frozenset(settings["job_ids"]), allow_uncoursed_tasks=True,
                                request_key=turn["id"] + ":" + req.call_id)
        recovered_ctx = runtime.auth_recovery_tool_context(turn, db)
        if recovered_ctx is not None:
            ctx = recovered_ctx
        started = time.perf_counter()
        runtime.store.event(turn["id"], "tool_started", {"call_id": req.call_id, "name": req.name,
                            "argument_keys": sorted(req.arguments.keys())[:80]})
        try:
            result = tools.execute(req.name, req.arguments, db, ctx)
        except ApiError as exc:
            # Verify any advertised file against the same store the tool just used, so a read
            # receipt cannot name a file the conversation download entry would refuse.
            result = error_envelope(exc, tools._file_store(ctx))
        if req.name == "connections.courses.import" and result["status"] in {"ok", "partial"}:
            receipts = result["data"]["courses"]
            verified = []
            for item in receipts:
                connection = db.get(m.Connection, item["connection_id"])
                course = db.get(m.Course, item["id"])
                ref = db.scalar(select(m.SourceRef).where(m.SourceRef.owner_type == "course",
                                m.SourceRef.owner_id == item["id"], m.SourceRef.platform == item["platform"]))
                if (connection is None or connection.id not in settings["connection_ids"]
                        or connection.platform != item["platform"] or course is None
                        or course.archived_at is not None or ref is None):
                    raise ApiError(403, "SCOPE_DENIED", "无法确认导入课程的授权来源")
                verified.append({"id": course.id, "connection_id": connection.id})
            runtime.store.grant_imported_courses(conversation["id"], verified)
        runtime.store.cache_call(turn["id"], req.call_id, cached_request, result)
        runtime.store.event(turn["id"], "tool_result", {"call_id": req.call_id, "name": req.name,
                            "arguments": req.arguments, "result": result,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
        if hasattr(runtime, 'defer_authentication'):
            runtime.defer_authentication(turn, req.name, req.arguments, result)
        return result


@router.post("/internal/model")
def internal_model(req: ModelRequest, authority=Depends(runtime_authority), db: Session = Depends(get_db)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation["id"]:
        raise ApiError(403, "RUNTIME_DENIED", "运行时授权不属于指定会话")
    if len(json.dumps(req.messages)) > 250000:
        raise ApiError(400, "BUDGET_EXCEEDED", "会话内容超出本轮发送预算")
    runtime.store.reserve_model(turn["id"])
    started = time.perf_counter()
    visible_tools = _model_visible_tool_schemas(
        req.tools or [], runtime._benchmark_tool_groups(conversation["settings"]), req.messages,
    )
    tool_names = []
    for item in visible_tools:
        if isinstance(item, dict):
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str):
                tool_names.append(name[:128])
    try:
        # The authenticated Harness registry supplies native/skill/MCP tools.
        # Transport headers remain server-owned; domain schemas cannot be replaced.
        response = model.call_model(db, conversation["settings"], req.messages, req.max_tokens, visible_tools)
        usage = response.get("usage") if isinstance(response, dict) else None
        choices = response.get("choices") if isinstance(response, dict) else None
        selected = []
        # call_model returns the normalized bridge shape (tool_calls with name),
        # not the provider's raw OpenAI choices envelope. Read that primary shape
        # first so tool selection telemetry is present on this path as well.
        normalized_calls = response.get("tool_calls") if isinstance(response, dict) else None
        if isinstance(normalized_calls, list):
            selected.extend(call.get("name", "")[:128] for call in normalized_calls
                            if isinstance(call, dict) and isinstance(call.get("name"), str))
        elif isinstance(choices, list):
            for choice in choices:
                msg = choice.get("message", {}) if isinstance(choice, dict) else {}
                calls = msg.get("tool_calls", []) if isinstance(msg, dict) else []
                for call in calls if isinstance(calls, list) else []:
                    fn = call.get("function", {}) if isinstance(call, dict) else {}
                    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                        selected.append(fn["name"][:128])
        runtime.store.event(turn["id"], "model_call", {
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "available_tools": tool_names[:256], "selected_tools": selected[:64],
            "tool_schema_snapshot": model.tool_schema_snapshot(visible_tools),
            "message_count": len(req.messages), "request_chars": len(json.dumps(req.messages, ensure_ascii=False)),
            "usage": usage if isinstance(usage, dict) else None,
        })
        return response
    except (ApiError, ProviderCallError) as exc:
        runtime.store.event(turn["id"], "model_error", {"code": exc.code, "message": exc.message,
                            "retryable": exc.retryable, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                            "available_tools": tool_names[:256],
                            "tool_schema_snapshot": model.tool_schema_snapshot(visible_tools)})
        raise ApiError(getattr(exc, "status_code", 502), exc.code, exc.message, retryable=exc.retryable) from None


@router.post('/internal/model-lease')
def internal_model_lease(req: ModelLeaseRequest, authority=Depends(runtime_authority), db: Session = Depends(get_db)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    model.runtime_configuration(db, conversation['settings'])
    runtime.store.reserve_model(turn['id'])
    return {'ok': True}


@router.post('/internal/model-observation')
def internal_model_observation(req: ModelObservationRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    names = []
    for item in req.tools:
        fn = item.get('function') if isinstance(item.get('function'), dict) else item
        name = fn.get('name') if isinstance(fn, dict) else None
        if isinstance(name, str):
            names.append(name[:128])
    runtime.store.event(turn['id'], 'model_call', {
        'duration_ms': round(req.duration_ms, 2),
        'available_tools': names[:256],
        'selected_tools': [name[:128] for name in req.selected_tools if isinstance(name, str)][:64],
        'tool_schema_snapshot': model.tool_schema_snapshot(req.tools),
        'source': 'native_provider_llm_stream',
    })
    return {'ok': True}


@router.post('/internal/tool-lease')
def internal_tool_lease(req: NativeToolLeaseRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id'] or req.name.startswith('campus_'):
        raise ApiError(403, 'RUNTIME_DENIED', '通用工具租约不属于指定会话或工具类别')
    if runtime.auth_resume_context(turn):
        raise ApiError(403, 'RECOVERY_READ_ONLY', '自动认证续任务仅使用领域只读工具，不执行通用代码、网页或文件修改')
    with runtime.call_lock(turn['id']):
        runtime.store.reserve_native_tool(turn['id'], req.call_id, req.name)
    return {'ok': True}


@router.post('/internal/files/publish')
def internal_publish_file(req: PublishFileRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    workspace = runtime.workspaces.get(turn['id'])
    if workspace is None:
        raise ApiError(409, 'RUNTIME_NOT_READY', '任务目录尚未就绪')
    with runtime.call_lock(turn['id']):
        current_turn = runtime.store.turn(turn['id'])
        if current_turn['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮已停止')
        # Authority comes only from the actual Harness root and persisted user
        # attachment selection. Preserve the raw path for link/reparse checks.
        item = runtime.files.publish(conversation['id'], Path(req.path), 'publish:' + turn['id'] + ':' + req.call_id,
                                     workspace=workspace, attachment_ids=current_turn.get('attachment_ids', []))
        runtime.store.event(turn['id'], 'file.published', {'file': item})
    return item


@router.post('/internal/model-error')
def internal_model_error(req: ModelErrorRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    data = req.model_dump(exclude={'conversation_id', 'tools'})
    data['available_tools'] = [
        (item.get('function') if isinstance(item.get('function'), dict) else item).get('name', '')[:128]
        for item in req.tools
        if isinstance(item, dict) and isinstance((item.get('function') if isinstance(item.get('function'), dict) else item).get('name'), str)
    ][:256]
    data['tool_schema_snapshot'] = model.tool_schema_snapshot(req.tools)
    data['source'] = 'native_provider_llm_stream'
    runtime.store.event(turn['id'], 'model_error', data)
    return {'ok': True}
