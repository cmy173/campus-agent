"""应用配置（阶段 1：本地 SQLite 开发；生产 PostgreSQL 由 STUDY_DATABASE_URL 指定）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def ensure_repo_root_on_path() -> None:
    """把仓库根加入 `sys.path`，使 `connectors.campus.*` 等领域包按仓库根解析。

    与 `connectors_registry` / `review_deps` 同法；追加而非前插，避免仓库根下的
    同名目录遮蔽标准库或已安装包。可重复调用。
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.append(root)

APP_VERSION = "0.2.0-local"

DATABASE_URL = os.environ.get("STUDY_DATABASE_URL") or f"sqlite:///{(REPO_ROOT / 'var' / 'study.db').as_posix()}"

EXAMPLES_DIR = Path(os.environ.get("STUDY_EXAMPLES_DIR") or (REPO_ROOT / "contracts" / "examples"))

CONFIG_DEFAULTS_PATH = Path(os.environ.get("STUDY_CONFIG_DEFAULTS") or (REPO_ROOT / "contracts" / "config-defaults.json"))

FILES_DIR = Path(os.environ.get("STUDY_FILES_DIR") or (REPO_ROOT / "var" / "files"))
# Provider 密钥本地存储（Windows DPAPI 加密文件；绝不落库/日志/响应/导出）。
SECRETS_DIR = Path(os.environ.get("STUDY_SECRETS_DIR") or (REPO_ROOT / "var" / "secrets"))
MAX_UPLOAD_BYTES = int(os.environ.get("STUDY_MAX_UPLOAD_MB") or 100) * 1024 * 1024
# 单文件解析上限（PD-1）：64MB 十进制，与 learning.parse 默认值、D 侧校准口径一致（64_000_000）。
# 上传通道护栏（MAX_UPLOAD_BYTES，100MiB）是独立控制，恒 ≥ 解析上限：64MB~100MiB 的文件可上传存留，解析时如实失败。
MAX_PARSE_BYTES = int(os.environ.get("STUDY_MAX_PARSE_MB") or 64) * 1_000_000
MAX_IMPORT_FILES = int(os.environ.get("STUDY_MAX_IMPORT_FILES") or 20)
MAX_ARCHIVE_MEMBERS = int(os.environ.get("STUDY_MAX_ARCHIVE_MEMBERS") or 200)
MAX_ARCHIVE_BYTES = int(os.environ.get("STUDY_MAX_ARCHIVE_MB") or 200) * 1_000_000

WORKER_POLL_SECONDS = float(os.environ.get("STUDY_WORKER_POLL_SECONDS") or 1.0)


def _load_sync_adapters() -> dict[str, str]:
    """STUDY_SYNC_ADAPTERS：平台 → "module:attr"（attr 缺省 sync_course），JSON 对象。"""
    raw = os.environ.get("STUDY_SYNC_ADAPTERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


SYNC_ADAPTERS = _load_sync_adapters()


REQUEST_ID_HEADER = "X-Request-ID"
