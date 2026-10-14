"""Local-only conversation API and per-turn internal bridge, additive to v0.2."""
from __future__ import annotations

import json
import os
import secrets
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
from .. import secrets_store

router = APIRouter(prefix="/agent", tags=["campus-agent"])
COOKIE = "campus_agent_owner"


class ConversationRequest(tools.Input):
    provider_id: str = Field(min_length=1, max_length=128)
    send_to_provider: bool = False
    course_id: str | None = Field(default=None, min_length=1, max_length=128)


class TurnRequest(tools.Input):
    text: str = Field(min_length=1, max_length=8000)
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    clear_course_context: bool = False
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)


class AcceptActionRequest(tools.Input):
    operation: Literal["apply", "revoke"]


class ToolRequest(tools.Input):
    conversation_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]


class ModelRequest(tools.Input):
    conversation_id: str = Field(min_length=1, max_length=128)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    max_tokens: int = Field(default=16384, ge=1, le=65536)
    attribution_headers: dict[str, str] = Field(default_factory=dict)


class ModelLeaseRequest(tools.Input):
    conversation_id: str = Field(min_length=1, max_length=128)


class NativeToolLeaseRequest(ModelLeaseRequest):
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)


class PublishFileRequest(ModelLeaseRequest):
    call_id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=4096)


class ControlAddressRequest(ModelLeaseRequest):
    url: str = Field(pattern=r'^http://127\.0\.0\.1:\d{1,5}$')


class ModelErrorRequest(ModelLeaseRequest):
    code: Literal['PROVIDER_AUTH_FAILED', 'PROVIDER_RATE_LIMITED', 'PROVIDER_TIMEOUT',
                  'PROVIDER_UNREACHABLE', 'PROVIDER_INVALID_RESPONSE', 'PROVIDER_HTTP_ERROR',
                  'PROVIDER_CHANGED', 'BUDGET_EXCEEDED', 'CONSENT_REQUIRED']
    message: str = Field(min_length=1, max_length=512)
    retryable: bool


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
    turn, conversation = runtime.store.runtime(authorization[7:])
    return runtime, turn, conversation


def public_conversation(item: dict) -> dict:
    settings = item["settings"]
    return {k: item[k] for k in ("id", "created_at", "updated_at")} | {
        "provider_id": settings["provider"]["id"], "course_ids": settings["course_ids"],
        "current_course_id": settings.get("current_course_id"), "send_to_provider": settings["send_to_provider"]}


@router.get('/settings')
def read_settings(owner: str = Depends(browser_owner)):
    return get_runtime().settings.public()


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
    if not req.send_to_provider:
        raise ApiError(403, "CONSENT_REQUIRED", "请明确同意向所选模型发送对话和相关工具返回的本地资料")
    cfg = db.get(m.ProviderConfig, req.provider_id)
    if cfg is None:
        raise ApiError(404, "NOT_FOUND", "所选模型配置不存在")
    if cfg.protocol != "openai_chat_completions":
        raise ApiError(400, "UNSUPPORTED", "当前对话仅支持 Chat Completions 兼容服务")
    course_ids = list(db.scalars(select(m.Course.id).where(m.Course.archived_at.is_(None))))
    if req.course_id and req.course_id not in course_ids:
        raise ApiError(403, "SCOPE_DENIED", "所选课程不在本机可用范围内")
    connection_ids = list(db.scalars(select(m.Connection.id)))
    item = get_runtime().store.create(owner, {"provider": model.snapshot(cfg), "send_to_provider": True,
        "course_ids": course_ids, "connection_ids": connection_ids, "job_ids": [], "current_course_id": req.course_id})
    return public_conversation(item)


@router.get("/conversations")
def list_conversations(owner: str = Depends(browser_owner)):
    return {"items": [public_conversation(c) for c in get_runtime().store.conversations(owner)], "next_cursor": None}


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


@router.get("/turns/{turn_id}")
def read_turn(turn_id: str, owner: str = Depends(browser_owner), after: int = Query(default=0, ge=0)):
    runtime, turn = owned_turn(turn_id, owner)
    return turn | {"events": runtime.store.events(turn_id, after)}


@router.post("/turns/{turn_id}/cancel")
def cancel_turn(turn_id: str, owner: str = Depends(browser_owner)):
    runtime, _ = owned_turn(turn_id, owner)
    runtime.cancel(turn_id)
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
    runtime.register_controls(turn['id'], req.url, authorization[7:])
    return {'ok': True}


@router.post("/internal/schemas")
def internal_schemas(req: tools.Empty, authority=Depends(runtime_authority)):
    return tools.schemas()


def error_envelope(exc: ApiError) -> dict:
    return {"status": "error", "data": {}, "evidence": [],
            "coverage": {"scope": "authorized_conversation", "complete": False, "missing": ["工具未成功完成"]},
            "freshness": {"fetched_at": None, "stale": True}, "warnings": [], "next_cursor": None, "job_id": None,
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}


@router.post("/internal/tool")
def internal_tool(req: ToolRequest, authority=Depends(runtime_authority), db: Session = Depends(get_db)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation["id"]:
        raise ApiError(403, "RUNTIME_DENIED", "运行时授权不属于指定会话")
    request = {"name": req.name, "arguments": req.arguments}
    with runtime.call_lock(turn["id"]):
        if runtime.store.turn(turn["id"])["status"] != "running":
            raise ApiError(409, "TURN_STOPPED", "本轮已停止")
        cached = runtime.store.cached_call(turn["id"], req.call_id, request)
        if cached is not None:
            return cached
        settings = conversation["settings"]
        ctx = tools.ToolContext("local", conversation["id"], frozenset(settings["course_ids"]),
                                frozenset(settings["connection_ids"]), frozenset(settings["job_ids"]), allow_uncoursed_tasks=True,
                                request_key=turn["id"] + ":" + req.call_id)
        try:
            result = tools.execute(req.name, req.arguments, db, ctx)
        except ApiError as exc:
            result = error_envelope(exc)
        runtime.store.cache_call(turn["id"], req.call_id, request, result)
        runtime.store.event(turn["id"], "tool_result", {"call_id": req.call_id, "name": req.name,
                            "arguments": req.arguments, "result": result})
        return result


@router.post("/internal/model")
def internal_model(req: ModelRequest, authority=Depends(runtime_authority), db: Session = Depends(get_db)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation["id"]:
        raise ApiError(403, "RUNTIME_DENIED", "运行时授权不属于指定会话")
    if len(json.dumps(req.messages)) > 250000:
        raise ApiError(400, "BUDGET_EXCEEDED", "会话内容超出本轮发送预算")
    runtime.store.reserve_model(turn["id"])
    try:
        # The authenticated Harness registry supplies native/skill/MCP tools.
        # Transport headers remain server-owned; domain schemas cannot be replaced.
        return model.call_model(db, conversation["settings"], req.messages, req.max_tokens, req.tools)
    except (ApiError, ProviderCallError) as exc:
        runtime.store.event(turn["id"], "model_error", {"code": exc.code, "message": exc.message, "retryable": exc.retryable})
        raise ApiError(getattr(exc, "status_code", 502), exc.code, exc.message, retryable=exc.retryable) from None


@router.post('/internal/model-lease')
def internal_model_lease(req: ModelLeaseRequest, authority=Depends(runtime_authority), db: Session = Depends(get_db)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    model.validated_config(db, conversation['settings'])
    runtime.store.reserve_model(turn['id'])
    return {'ok': True}


@router.post('/internal/tool-lease')
def internal_tool_lease(req: NativeToolLeaseRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id'] or req.name.startswith('campus_'):
        raise ApiError(403, 'RUNTIME_DENIED', '通用工具租约不属于指定会话或工具类别')
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
    source = Path(req.path).expanduser()
    if not source.is_absolute():
        source = workspace / source
    with runtime.call_lock(turn['id']):
        if runtime.store.turn(turn['id'])['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮已停止')
        item = runtime.files.publish(conversation['id'], source.resolve(), 'publish:' + turn['id'] + ':' + req.call_id)
        runtime.store.event(turn['id'], 'file.published', {'file': item})
    return item


@router.post('/internal/model-error')
def internal_model_error(req: ModelErrorRequest, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    runtime.store.event(turn['id'], 'model_error', req.model_dump(exclude={'conversation_id'}))
    return {'ok': True}
