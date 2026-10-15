"""复习卷外部模块接缝（A 题库 / D 出卷引擎，CR008）。

与 `connectors_registry` 同法：把仓库根注入 `sys.path` 后按环境变量导入本地 Python 包，
**未安装/导入失败返回 None**——调用方据此返回 502 `DEPENDENCY_UNAVAILABLE`，
绝不伪造"可用"或空结果（红线：未实测保持 unknown，不把模拟写成真实）。

- A：`connectors.questionbanks`（`STUDY_QUESTIONBANK_MODULE` 覆盖），能力查询见
  `get_questionbank_capabilities`；来源配置经 `STUDY_QUESTIONBANK_SOURCES`（JSON）传入。
- D：`learning.review`（`STUDY_REVIEW_ENGINE_MODULE` 覆盖），`REVIEW_ENGINE_VERSION` 为
  fingerprint 公式的引擎版本项；生成流水线各步见 contracts/README.md 复习卷边界。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from types import ModuleType
from typing import Any

from .config import REPO_ROOT

LOG = logging.getLogger("app.review_deps")

DEFAULT_QUESTIONBANK_MODULE = "connectors.questionbanks"
DEFAULT_REVIEW_ENGINE_MODULE = "learning.review"


def _ensure_repo_root_on_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def load_module(spec: str) -> ModuleType | None:
    """按 "模块名" 导入；失败返回 None（错误留痕，不静默成功）。"""
    _ensure_repo_root_on_path()
    try:
        return importlib.import_module(spec)
    except Exception as exc:  # noqa: BLE001 —— 降级为未接入，错误留痕
        LOG.warning("复习卷模块导入失败：%s（%s: %s）", spec, type(exc).__name__, exc)
        return None


def questionbank_module() -> ModuleType | None:
    spec = os.environ.get("STUDY_QUESTIONBANK_MODULE") or DEFAULT_QUESTIONBANK_MODULE
    return load_module(spec)


def review_engine_module() -> ModuleType | None:
    spec = os.environ.get("STUDY_REVIEW_ENGINE_MODULE") or DEFAULT_REVIEW_ENGINE_MODULE
    return load_module(spec)


def questionbank_sources_config() -> dict[str, Any]:
    """`STUDY_QUESTIONBANK_SOURCES`：{"sources": [...]} JSON；非法/缺省返回空表（configured=false）。"""
    raw = os.environ.get("STUDY_QUESTIONBANK_SOURCES")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        LOG.warning("STUDY_QUESTIONBANK_SOURCES 不是合法 JSON，按未配置处理")
        return {}
    if isinstance(data, list):
        return {"sources": data}
    if isinstance(data, dict):
        return data
    return {}
