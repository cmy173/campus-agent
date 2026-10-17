"""手动导入通道（POST /api/v1/imports）。

multipart/form-data：payload（JSON 字符串）+ files（1..N 个 pdf/pptx/docx）。
受理即返回 202 + queued 的 import job；落库的实质处理（资源 upsert、解析派发）由 worker 执行。
支持 Idempotency-Key：同键同体重放首次响应；同键换体 409。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..errors import ApiError
from ..models import Course, FileRecord, Job, Textbook
from ..repo import (
    get_or_404,
    idempotency_lookup,
    idempotency_store,
    new_id,
    validate_idempotency_key,
)
from ..schemas import ImportPayload
from ..storage import StoredUpload, commit_upload, discard_upload, remove_blob, store_upload

router = APIRouter(tags=["imports"])

ENDPOINT = "POST /imports"


def _parse_payload(raw: str) -> ImportPayload:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "VALIDATION_FAILED", "payload 不是合法 JSON", details={"reason": exc.msg}) from None
    if not isinstance(data, dict):
        raise ApiError(400, "VALIDATION_FAILED", "payload 必须是 JSON 对象")
    try:
        return ImportPayload.model_validate(data)
    except ValidationError as exc:
        raise ApiError(
            400, "VALIDATION_FAILED", "payload 校验失败", details={"errors": jsonable_encoder(exc.errors())}
        ) from None


@router.post("/imports", status_code=202)
def create_import(
    db: Session = Depends(get_db),
    payload: str = Form(...),
    files: list[UploadFile] = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    validate_idempotency_key(idempotency_key)
    parsed = _parse_payload(payload)
    get_or_404(db, Course, parsed.course_id, "课程")
    if parsed.textbook_id:
        book = get_or_404(db, Textbook, parsed.textbook_id, '教材')
        if book.course_id != parsed.course_id or len(files) != 1:
            raise ApiError(400, 'VALIDATION_FAILED', '教材导入必须是该课程的单个教材文件')
        if parsed.kind != 'document':
            raise ApiError(400, 'VALIDATION_FAILED', '教材导入类型须为 document')
    if not files:
        raise ApiError(400, "VALIDATION_FAILED", "至少需要一个文件")
    if len(files) > config.MAX_IMPORT_FILES:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            f"单次最多导入 {config.MAX_IMPORT_FILES} 个文件",
            details={"count": len(files), "max_files": config.MAX_IMPORT_FILES},
        )

    stored: list[StoredUpload] = []
    committed = False
    try:
        for upload in files:
            stored.append(store_upload(upload, new_id(db, FileRecord, "fil")))

        fingerprint_payload = parsed.model_dump(mode="json")
        if parsed.textbook_id is None:
            fingerprint_payload.pop('textbook_id', None)  # Keep legacy import idempotency keys valid.
        fingerprint = {"payload": fingerprint_payload, "files": [st.audit() for st in stored]}
        replay = idempotency_lookup(db, idempotency_key, ENDPOINT, fingerprint)
        if replay is not None:
            status_code, body = replay
            return JSONResponse(status_code=status_code, content=body)

        try:
            for st in stored:
                commit_upload(st)
        except BaseException:
            for s in stored:
                remove_blob(s.file_id)
            raise
        committed = True
        for st in stored:
            db.add(
                FileRecord(
                    id=st.file_id,
                    original_name=st.original_name,
                    mime_type=st.mime_type,
                    size_bytes=st.size_bytes,
                    content_hash=st.content_hash,
                )
            )
        job = Job(
            id=new_id(db, Job, "job"),
            type="import",
            status="queued",
            progress=0.0,
            errors=[],
            result_refs=[],
            payload={
                "course_id": parsed.course_id,
                "textbook_id": parsed.textbook_id,
                "textbook_identity": ({key: getattr(book, key) for key in ('title', 'author', 'edition', 'isbn')} if parsed.textbook_id else None),
                "kind": parsed.kind,
                "title": parsed.title,
                "source_ref": parsed.model_dump(mode="json")["source_ref"],
                "files": [
                    {
                        "file_id": st.file_id,
                        "name": st.original_name,
                        "mime_type": st.mime_type,
                        "size_bytes": st.size_bytes,
                        "content_hash": st.content_hash,
                    }
                    for st in stored
                ],
            },
        )
        db.add(job)
        db.flush()
        body = {"job": job.to_contract()}
        idempotency_store(db, idempotency_key, ENDPOINT, fingerprint, 202, body)
        try:
            db.commit()
        except BaseException:
            db.rollback()
            for st in stored:
                remove_blob(st.file_id)
            raise
        return body
    finally:
        if not committed:
            for st in stored:
                discard_upload(st)
