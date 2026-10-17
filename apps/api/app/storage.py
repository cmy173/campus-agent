"""文件存储：blob 落盘在 config.FILES_DIR（默认 var/files，gitignore），DB 只存元数据。

安全约束：
- 上传文件名从不参与路径拼接（防目录穿越）；blob 路径由服务端生成的 file_id 派生；
- file_id 形如 fil_<hex>，blob_path() 拒绝任何越界字符（下载端点同样经此校验）；
- 仅接受扩展名白名单 + 魔数校验；大小超限即中止读取（413），不落盘；
- 服务器本地路径不出现在任何 API 响应中（对外只有 file_id）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from . import config
from .errors import ApiError

CHUNK_SIZE = 1024 * 1024

EXTENSION_MIME = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # .xlsx（真实课件形态，如「excel练习.xlsx」）：同为 OOXML 压缩包，可解析
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # .zip 课件包（A21/A22 实测真实资源形态）：仅存档与下载，不解析（parse_status=skipped）
    ".zip": "application/zip",
}
EXTENSIONS_HINT = "pdf/pptx/docx/xlsx/zip"

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

_FILE_ID_RE = re.compile(r"^fil_[0-9a-f]{6,32}$")


@dataclass
class StoredUpload:
    file_id: str
    original_name: str
    mime_type: str
    size_bytes: int
    content_hash: str
    temp_path: Path

    def audit(self) -> dict[str, Any]:
        return {"name": self.original_name, "size_bytes": self.size_bytes, "content_hash": self.content_hash}


def safe_display_name(filename: str | None) -> str:
    """仅用于展示与扩展名判断；去掉任何目录成分，绝不用于路径拼接。"""
    base = (filename or "").replace("\\", "/").split("/")[-1].strip()
    return base[:512]


def blob_path(file_id: str) -> Path:
    if not _FILE_ID_RE.match(file_id):
        raise ApiError(404, "NOT_FOUND", f"文件不存在：{file_id}", details={"kind": "文件", "id": file_id})
    return config.FILES_DIR / file_id


def store_upload(upload: UploadFile, file_id: str) -> StoredUpload:
    name = safe_display_name(upload.filename)
    if not name:
        raise ApiError(400, "VALIDATION_FAILED", "上传文件缺少文件名")
    ext = Path(name).suffix.lower()
    mime_type = EXTENSION_MIME.get(ext)
    if mime_type is None:
        raise ApiError(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            f"不支持的文件类型：{ext or '（无扩展名）'}（支持 {EXTENSIONS_HINT}）",
            details={"file": name},
        )

    temp_dir = config.FILES_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{file_id}.part"
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = upload.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                if len(head) < 8:
                    head = (head + chunk[: 8 - len(head)])[:8]
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise ApiError(
                        413,
                        "FILE_TOO_LARGE",
                        f"文件超过大小限制（{config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB）",
                        details={"file": name, "max_bytes": config.MAX_UPLOAD_BYTES},
                    )
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "文件为空，不是受支持的文档", details={"file": name})
        if ext == ".pdf" and not head.startswith(_PDF_MAGIC):
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "文件内容与扩展名不符（不是 PDF）", details={"file": name})
        if ext in (".pptx", ".docx", ".zip") and not head.startswith(_ZIP_MAGIC):
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "文件内容与扩展名不符（不是 OOXML/ZIP 容器）", details={"file": name})
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return StoredUpload(
        file_id=file_id,
        original_name=name,
        mime_type=mime_type,
        size_bytes=size,
        content_hash=f"sha256:{digest.hexdigest()}",
        temp_path=temp_path,
    )


@dataclass
class StoredBytes:
    file_id: str
    original_name: str
    mime_type: str
    size_bytes: int
    content_hash: str


def store_bytes(file_id: str, data: bytes, *, display_name: str, mime_type: str) -> StoredBytes:
    """服务端生成内容的落盘（导出产物等）；blob 路径同样由服务端 file_id 派生。"""
    if not _FILE_ID_RE.match(file_id):
        raise ApiError(500, "INTERNAL_ERROR", "受控文件标识不合法")
    name = safe_display_name(display_name) or "download"
    path = config.FILES_DIR / file_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return StoredBytes(
        file_id=file_id,
        original_name=name,
        mime_type=mime_type,
        size_bytes=len(data),
        content_hash=f"sha256:{hashlib.sha256(data).hexdigest()}",
    )


def commit_upload(stored: StoredUpload) -> None:
    final = config.FILES_DIR / stored.file_id
    final.parent.mkdir(parents=True, exist_ok=True)
    stored.temp_path.replace(final)


def discard_upload(stored: StoredUpload) -> None:
    stored.temp_path.unlink(missing_ok=True)


def remove_blob(file_id: str) -> None:
    if _FILE_ID_RE.match(file_id):
        (config.FILES_DIR / file_id).unlink(missing_ok=True)
