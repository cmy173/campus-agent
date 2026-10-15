"""Install the chat-only data API with the gateway's local owner/Origin guard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ..agent_bridge.data_management import DataManagement
from ..agent_bridge.runtime import get_runtime


class DataSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    history_retention_days: int = Field(ge=0, le=36500)


class Empty(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def install_agent_data_router(app, owner_dependency, *, prefix='/api/v1/agent/data'):
    router = APIRouter(prefix=prefix, tags=['campus-agent-data'])

    def manager():
        return DataManagement(get_runtime())

    @router.get('/settings')
    def settings(owner: str = Depends(owner_dependency)):
        return manager().settings(owner)

    @router.put('/settings')
    def save_settings(payload: DataSettingsRequest, owner: str = Depends(owner_dependency)):
        return manager().save_settings(owner, payload.history_retention_days)

    @router.get('/history')
    def history(owner: str = Depends(owner_dependency), cursor: str | None = Query(default=None, max_length=1024),
                search: str = Query(default='', max_length=200), limit: int = Query(default=50, ge=1, le=100)):
        from .agent import public_conversation
        page = manager().history(owner, cursor=cursor, search=search, limit=limit)
        return page | {'items': [public_conversation(item) | {'title': item.get('title')} for item in page['items']]}

    @router.delete('/history/{conversation_id}')
    def delete_conversation(conversation_id: str, owner: str = Depends(owner_dependency)):
        return manager().delete_conversation(owner, conversation_id)

    @router.delete('/history')
    def delete_history(owner: str = Depends(owner_dependency)):
        return manager().delete_history(owner)

    @router.post('/history/purge-expired')
    def purge_expired(payload: Empty, owner: str = Depends(owner_dependency)):
        return manager().purge_expired(owner)

    @router.post('/browser-session/clear')
    def clear_browser_session(payload: Empty, owner: str = Depends(owner_dependency)):
        runtime = get_runtime()
        # The shared app browser is a single-person profile, not a user Edge profile.
        # Clear is never a model capability. Active turns must first drain.
        from ..errors import ApiError
        with runtime.store.connection() as db:
            if db.execute("SELECT 1 FROM turns WHERE status IN ('queued','running','stopping') LIMIT 1").fetchone():
                raise ApiError(409, 'BROWSER_BUSY', '有任务正在使用助手；请停止并等待收尾后再退出校园会话')
        from ..campus_workspace import get_campus_workspace
        get_campus_workspace().disconnect(clear_cache=True)
        return runtime.browser.clear_session_private()

    app.include_router(router)
