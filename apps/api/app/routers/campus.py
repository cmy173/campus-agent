"""New campus workspace routes; mount with prefix /api/v1/agent/campus.

Reuse the gateway's owner/Origin dependency through install_campus_router; no
standalone anonymous credential or cookie endpoint is exposed here.
"""
from __future__ import annotations

from typing import Any, Literal
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError

from ..campus_workspace import get_campus_workspace, OWNER
from ..errors import ApiError
from connectors.campus.connector_contracts import UserContext
from connectors.campus.campus_common import CampusError


class CampusQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: Literal["courses", "grades", "profile", "notices", "college_notices"]
    query: dict[str, Any] = Field(default_factory=dict)
    platform: Literal["zdbk", "eta", "zdbk_public", "ckc_public"] | None = None


class CampusDisconnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clear_cache: bool = False


def install_campus_router(app, owner_dependency, *, prefix="/api/v1/agent/campus"):
    router = APIRouter(prefix=prefix, tags=["campus-workspace"], dependencies=[Depends(owner_dependency)])

    @router.get("/status")
    def status():
        return get_campus_workspace().status()

    @router.post("/query")
    def query(payload: CampusQueryRequest):
        return run_query(payload, False)

    @router.post("/sync")
    def sync(payload: CampusQueryRequest):
        return run_query(payload, True)

    def run_query(payload, refresh):
        try:
            return get_campus_workspace().query(payload.capability, payload.query, payload.platform, refresh=refresh)
        except (ValidationError, ValueError, TypeError):
            raise ApiError(400, "INVALID_INPUT", "校园查询参数无效") from None

    @router.post("/disconnect")
    def disconnect(payload: CampusDisconnectRequest):
        workspace = get_campus_workspace()
        workspace.manager.disconnect(OWNER, delete=payload.clear_cache)
        return workspace.status()

    @router.get("/academics")
    def academics(day: str | None = None, term: str | None = None):
        from datetime import date
        try:
            if day:
                # Validate here, but hand the connector the ISO string it parses itself:
                # `academic_view`/`agenda` call `date.fromisoformat` on the raw value.
                date.fromisoformat(day)
            return get_campus_workspace().manager.academic_view(OWNER, day, term)
        except ValueError:
            raise ApiError(400, "INVALID_INPUT", "日期应为 YYYY-MM-DD") from None

    @router.get("/imports")
    def imports():
        from ..campus_migration import list_imports
        return list_imports(get_campus_workspace().store)

    @router.get("/imports/{batch_id}/documents/{document_id}")
    def document(batch_id: str, document_id: str):
        from ..campus_migration import read_imported_document
        return read_imported_document(get_campus_workspace().store, batch_id, document_id)

    @router.get("/imports/{batch_id}/documents/{document_id}/download")
    def download(batch_id: str, document_id: str):
        from ..campus_migration import imported_content
        content, name = imported_content(get_campus_workspace().store, batch_id, document_id)
        from urllib.parse import quote
        return Response(content, media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})

    app.include_router(router)
    return router
