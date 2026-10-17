"""导出（POST /api/v1/exports）。

受理即返回 202 + queued 的 export job；两种格式（B19）：

- format=json（缺省）：按 include 类别汇集（缺省全部）；产物为 JSON 受控文件；
- format=ics：生效计划时间块 + 任务截止的 iCalendar（include 不适用，带 include 即 400）。

产物由 worker 生成受控文件，任务 result_refs 含 export_file 供 GET /files/{file_id} 下载。
支持 Idempotency-Key（指纹含格式与类别）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import Job
from ..repo import (
    idempotency_lookup,
    idempotency_store,
    new_id,
    validate_idempotency_key,
)
from ..schemas import CreateExportRequest

router = APIRouter(tags=["exports"])

ENDPOINT = "POST /exports"


@router.post("/exports", status_code=202)
def create_export(
    db: Session = Depends(get_db),
    body: CreateExportRequest | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    validate_idempotency_key(idempotency_key)
    fmt = body.format if body is not None else "json"
    include = list(body.include) if body is not None and body.include is not None else None
    if fmt == "ics" and include is not None:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "ICS 导出为计划与任务截止整体，不支持 include（该参数仅对 format=json 有效）",
            details={"format": fmt},
        )
    fingerprint = {"include": include, "format": fmt}
    replay = idempotency_lookup(db, idempotency_key, ENDPOINT, fingerprint)
    if replay is not None:
        status_code, payload = replay
        return JSONResponse(status_code=status_code, content=payload)

    job = Job(
        id=new_id(db, Job, "job"),
        type="export",
        status="queued",
        progress=0.0,
        errors=[],
        result_refs=[],
        payload={"include": include, "format": fmt},
    )
    db.add(job)
    db.flush()
    payload: dict[str, Any] = {"job": job.to_contract()}
    idempotency_store(db, idempotency_key, ENDPOINT, fingerprint, 202, payload)
    db.commit()
    return payload
