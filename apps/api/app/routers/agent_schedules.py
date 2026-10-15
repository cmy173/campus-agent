"""Owned schedule API and turn-authorized model tools. No independent model loop."""
from __future__ import annotations

from typing import Any, Literal
from fastapi import APIRouter, Depends, Header
from pydantic import Field, ValidationError

from ..agent_bridge.runtime import get_runtime
from ..agent_bridge.schedules import (Strict, ScheduleCreate, ScheduleUpdate, ScheduleDelete,
                                     ScheduleView, ScheduleListView, ScheduleHistoryView, get_scheduler)
from ..errors import ApiError
from .agent import browser_owner, runtime_authority

router = APIRouter(prefix='/agent', tags=['campus-agent-schedules'])


def owned(cid, owner):
    runtime = get_runtime()
    runtime.store.conversation(cid, owner)
    return get_scheduler(runtime)


@router.get('/conversations/{cid}/schedules', response_model=ScheduleListView)
def list_schedules(cid: str, owner=Depends(browser_owner)):
    return {'items': owned(cid, owner).list(cid), 'execution_scope': 'application_running'}


@router.post('/conversations/{cid}/schedules', response_model=ScheduleView)
def create_schedule(cid: str, req: ScheduleCreate, owner=Depends(browser_owner),
                    request_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=128)):
    return owned(cid, owner).create(cid, req, request_key=cid + ':http:' + request_key)


@router.patch('/conversations/{cid}/schedules/{sid}', response_model=ScheduleView)
def update_schedule(cid: str, sid: str, req: ScheduleUpdate, owner=Depends(browser_owner),
                    request_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=128)):
    return owned(cid, owner).update(cid, sid, req, request_key=cid + ':http:' + request_key)


@router.delete('/conversations/{cid}/schedules/{sid}', response_model=ScheduleView)
def delete_schedule(cid: str, sid: str, req: ScheduleDelete, owner=Depends(browser_owner),
                    request_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=128)):
    return owned(cid, owner).delete(cid, sid, req.revision, request_key=cid + ':http:' + request_key)


@router.get('/conversations/{cid}/schedules/{sid}/history', response_model=ScheduleHistoryView)
def schedule_history(cid: str, sid: str, owner=Depends(browser_owner)):
    return {'items': owned(cid, owner).history(cid, sid)}


class ScheduleTool(Strict):
    conversation_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=256)
    operation: Literal['create', 'list', 'update', 'delete', 'history']
    id: str | None = Field(default=None, min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


def validated(schema, value):
    try:
        return schema.model_validate(value)
    except ValidationError:
        raise ApiError(400, 'INVALID_INPUT', '定时任务参数无效，请核对时间规则、时区和版本')


@router.post('/internal/schedules', response_model=ScheduleView | ScheduleListView | ScheduleHistoryView)
def schedule_tool(req: ScheduleTool, authority=Depends(runtime_authority)):
    runtime, turn, conversation = authority
    if req.conversation_id != conversation['id']:
        raise ApiError(403, 'RUNTIME_DENIED', '运行时授权不属于指定会话')
    with runtime.call_lock(turn['id']):
        if runtime.store.turn(turn['id'])['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮已停止')
        scheduler = get_scheduler(runtime)
        key = turn['id'] + ':' + req.call_id
        if req.operation == 'create':
            return scheduler.create(conversation['id'], validated(ScheduleCreate, req.arguments), request_key=key)
        if req.operation == 'list':
            if req.arguments or req.id:
                raise ApiError(400, 'INVALID_INPUT', '列出任务不接受额外参数')
            return {'items': scheduler.list(conversation['id']), 'execution_scope': 'application_running'}
        if not req.id:
            raise ApiError(400, 'INVALID_INPUT', '操作需要定时任务 ID')
        if req.operation == 'update':
            return scheduler.update(conversation['id'], req.id, validated(ScheduleUpdate, req.arguments), request_key=key)
        if req.operation == 'delete':
            spec = validated(ScheduleDelete, req.arguments)
            return scheduler.delete(conversation['id'], req.id, spec.revision, request_key=key)
        if req.arguments:
            raise ApiError(400, 'INVALID_INPUT', '读取历史不接受额外参数')
        return {'items': scheduler.history(conversation['id'], req.id)}
