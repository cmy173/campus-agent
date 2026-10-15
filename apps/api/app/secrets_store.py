"""Provider 密钥本地存储（安全红线：密钥绝不落库、不进日志/响应/导出）。

每个 provider 配置一个独立文件（STUDY_SECRETS_DIR，默认 var/secrets/provider-<id>.key）：

- Windows（部署目标）：CryptProtectData/CryptUnprotectData（当前用户范围 DPAPI）加密落盘；
  密文与当前 Windows 用户绑定，拷走文件在他机/他用户下不可解。
- 其他平台（开发备用）：0o600 权限的受限明文文件；部署目标为 Windows，此分支仅为可移植性。

本模块只提供读写；调用方（routers/ai_providers、出卷 worker）不得把返回值写入任何
持久日志或 API 响应。任何异常信息不含密钥内容。
"""
from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path

from .config import SECRETS_DIR

_IS_WINDOWS = os.name == "nt"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")

if _IS_WINDOWS:  # pragma: no cover - 平台分支（本机为 Windows 时走真实现）
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _path_for(provider_id: str) -> Path:
    safe = _SAFE_ID.sub("", provider_id)
    if not safe:
        raise ValueError("provider_id 不合法")
    return Path(SECRETS_DIR) / f"provider-{safe}.key"


def _protect(data: bytes) -> bytes:  # pragma: no cover - Windows-only
    if not _IS_WINDOWS:
        raise RuntimeError("DPAPI 仅 Windows 可用")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p).value)
    blob_out = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:  # pragma: no cover - Windows-only
    if not _IS_WINDOWS:
        raise RuntimeError("DPAPI 仅 Windows 可用")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p).value)
    blob_out = _DataBlob()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def _encode(data: bytes) -> bytes:
    return _protect(data) if _IS_WINDOWS else data


def _decode(data: bytes) -> bytes:
    return _unprotect(data) if _IS_WINDOWS else data


def set_key(provider_id: str, key: str) -> None:
    payload = key.encode("utf-8")
    path = _path_for(provider_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_revision = key_revision(provider_id)
    path.write_bytes(_encode(payload))
    stamp = path.stat()
    if stamp.st_mtime_ns <= prior_revision:
        os.utime(path, ns=(stamp.st_atime_ns, prior_revision + 1_000_000))
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义有限，失败不致命
        pass


def key_revision(provider_id: str) -> int:
    """Non-secret persistent credential version; never open/decrypt the key.

    Missing files have revision zero. A replacement gets a new mtime even when
    the filesystem clock would otherwise reuse the previous write's timestamp.
    """
    try:
        return _path_for(provider_id).stat().st_mtime_ns
    except (FileNotFoundError, ValueError):
        return 0


def has_key(provider_id: str) -> bool:
    try:
        return _path_for(provider_id).is_file()
    except ValueError:
        return False


def get_key(provider_id: str) -> str | None:
    path = _path_for(provider_id)
    if not path.is_file():
        return None
    try:
        return _decode(path.read_bytes()).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        # 文件损坏或非当前用户 DPAPI 密文：视为无可用密钥（不向外抛细节）
        return None


def clear_key(provider_id: str) -> bool:
    """幂等清除；返回是否删除了文件。"""
    try:
        path = _path_for(provider_id)
    except ValueError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False
