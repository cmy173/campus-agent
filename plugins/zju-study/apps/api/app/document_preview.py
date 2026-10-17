"""本地 Office → PDF 转换；不上传文档，不使用远程预览服务。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from . import config, storage
from .errors import ApiError
from .models import FileRecord

_convert_lock = threading.Lock()


def converter_path() -> str | None:
    configured = os.environ.get("STUDY_SOFFICE_PATH")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("soffice")
    if found:
        return found
    for path in (Path("C:/Program Files/LibreOffice/program/soffice.exe"),
                 Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe")):
        if path.is_file():
            return str(path)
    runtime = config.REPO_ROOT / "var" / "runtime" / "libreoffice"
    if runtime.is_dir():
        return next((str(p) for p in runtime.glob("**/program/soffice.exe")), None)
    return None


def preview_path(record: FileRecord) -> Path:
    # Validate even when looking up cached previews.
    original = storage.blob_path(record.id)
    if record.mime_type == "application/pdf":
        return original
    return config.FILES_DIR / "preview-cache" / f"{record.id}.pdf"


def create_preview(record: FileRecord) -> Path:
    original = storage.blob_path(record.id)
    if not original.is_file():
        raise ApiError(404, "NOT_FOUND", "原文件未保存在本地，请重新同步或导入")
    target = preview_path(record)
    if target.is_file():
        return target
    extension = {"application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}.get(record.mime_type)
    if extension is None:
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "此文件不能生成页面预览；可下载原文件")
    converter = converter_path()
    if not converter:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "PPT 原样预览需要本地 LibreOffice。请运行 python tools/setup_preview.py；已有 PDF 可直接查看。")
    if record.size_bytes > config.parse_limit_bytes():
        raise ApiError(413, "FILE_TOO_LARGE", f"预览文件超过当前解析上限 {config.parse_limit_bytes() // (1024 * 1024)} MiB，可在设置中调整或拆分文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    # One converter at a time; independent profile never attaches to user's office session.
    with _convert_lock:
        if target.is_file():
            return target
        with tempfile.TemporaryDirectory(prefix="preview-", dir=target.parent) as temp:
            directory = Path(temp)
            source = directory / f"source{extension}"
            shutil.copyfile(original, source)
            profile = (directory / "profile").as_uri()
            try:
                process = subprocess.run([
                    converter, f"-env:UserInstallation={profile}", "--headless", "--nologo",
                    "--nodefault", "--norestore", "--convert-to", "pdf", "--outdir", str(directory), str(source),
                ], capture_output=True, timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except subprocess.TimeoutExpired:
                raise ApiError(504, "PREVIEW_TIMEOUT", "文档转换超时，请拆分课件后重试", retryable=True) from None
            converted = directory / "source.pdf"
            if process.returncode or not converted.is_file() or not converted.read_bytes().startswith(b"%PDF-"):
                raise ApiError(422, "PREVIEW_FAILED", "无法转换此文档，请确认文件可在办公软件中打开，或导入 PDF 版本")
            os.replace(converted, target)
    return target
