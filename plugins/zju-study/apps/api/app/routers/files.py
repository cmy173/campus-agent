"""受控文件下载（GET /api/v1/files/{file_id}）。

blob 由服务端 file_id 派生（storage.blob_path 拒绝越界标识）；服务器本地路径与
临时签名 URL 不进入响应。所有权检查：M1 单用户阶段库内记录即当前用户资产，
多用户鉴权落地后在此按 owner 过滤（403 Forbidden 预留）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import storage
from ..db import get_db
from ..errors import ApiError, not_found
from ..models import FileRecord
from ..document_preview import create_preview, preview_path

router = APIRouter(tags=["files"])


@router.get("/files/{file_id}")
def download_file(file_id: str, db: Session = Depends(get_db)) -> FileResponse:
    record = db.get(FileRecord, file_id)
    if record is None:
        raise not_found("文件", file_id)
    blob = storage.blob_path(file_id)
    if not blob.is_file():
        raise ApiError(404, "NOT_FOUND", f"文件已不可用：{file_id}", details={"kind": "文件", "id": file_id})
    return FileResponse(path=blob, media_type=record.mime_type, filename=record.original_name)


@router.post("/files/{file_id}/preview")
def prepare_preview(file_id: str, db: Session = Depends(get_db)):
    record = db.get(FileRecord, file_id)
    if record is None:
        raise not_found("文件", file_id)
    create_preview(record)
    return {"url": f"/api/v1/files/{file_id}/preview", "format": "pdf"}


@router.get("/files/{file_id}/preview")
def view_preview(file_id: str, db: Session = Depends(get_db)):
    record = db.get(FileRecord, file_id)
    if record is None:
        raise not_found("文件", file_id)
    path = preview_path(record)
    if not path.is_file():
        raise ApiError(404, "NOT_FOUND", "尚未生成本地预览，请先创建预览")
    return FileResponse(path, media_type="application/pdf", filename="preview.pdf",
                        content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})
