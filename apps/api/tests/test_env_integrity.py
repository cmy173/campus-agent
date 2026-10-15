"""B15 环境完整性自检：必需依赖缺失必须显式失败，而非 pytest.importorskip 静默跳过。

背景（P1 整改）：pypdf 只在 packages/learning 的 optional extra `[parse]` 中；
缺依赖时 D 的 4 项 PDF 测试经 `pytest.importorskip` 静默 skip，套件仍显"绿色"，
掩盖未测试。本自检让任何一次 apps/api 测试运行都能第一时间暴露不完整环境。

另含 learning editable 指向校验：陈旧的非 editable site-packages 副本（缺 D12
`build_tasks_json`）曾致 5 个测试文件在收集期 ImportError，表象像"业务代码坏了"。
"""
from __future__ import annotations

import importlib
from pathlib import Path

_INSTALL_HINT = (
    "从 apps/api 目录执行 `python -m pip install -r requirements.txt`"
    "（其中含 `-e ../../packages/learning[parse]`）"
)


def _require(module: str, hint: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - 依赖齐全时不可达
        raise AssertionError(
            f"缺少必需依赖 {module!r}：{hint}。测试环境不完整，显式失败（fail-fast），"
            "不得以 skip 掩盖。"
        ) from exc


def test_required_parse_dependency_pypdf() -> None:
    """解析依赖：pypdf 用于文本型 PDF 解析（[parse] extra），缺则 D 的 PDF 测试会静默 skip。"""
    pypdf = _require("pypdf", f"[parse] extra 未安装；{_INSTALL_HINT}")
    assert pypdf.__version__


def test_learning_package_parse_module_importable() -> None:
    """D2 解析模块可导入（editable 安装的 zju-learning 完好）。"""
    parse = _require("learning.parse", f"zju-learning 未安装或损坏；{_INSTALL_HINT}")
    assert callable(parse.parse_document)


def test_learning_package_loaded_from_workspace_source() -> None:
    """editable 指向校验：learning 必须加载自本仓库 packages/learning/src。

    非 editable 的陈旧副本会缺失新入口（如 D12 build_tasks_json），让多个测试文件
    在收集期 ImportError；此处断言实际加载路径，fail-fast 并给出重装指引。
    """
    from app.config import REPO_ROOT

    learning = _require("learning", f"zju-learning 未安装；{_INSTALL_HINT}")
    actual = Path(learning.__file__).resolve()
    expected_root = (REPO_ROOT / "packages" / "learning" / "src").resolve()
    assert actual.is_relative_to(expected_root), (
        f"learning 实际加载自 {actual}，不在工作区源码 {expected_root} 下"
        f"（疑似陈旧的非 editable site-packages 副本）；{_INSTALL_HINT}"
    )


def test_contract_validation_dependencies() -> None:
    """契约校验依赖（测试用）：jsonschema + referencing 按 contracts/schemas 校验响应体。"""
    jsonschema = _require("jsonschema", f"测试依赖缺失；{_INSTALL_HINT}")
    referencing = _require("referencing", f"测试依赖缺失；{_INSTALL_HINT}")
    assert jsonschema.Draft202012Validator
    assert referencing.Registry


def test_contract_route_coverage_dependency_yaml() -> None:
    """路由对账依赖（测试用）：PyYAML 解析 openapi.yaml（契约↔实现候选集对账）。"""
    yaml = _require("yaml", f"测试依赖缺失；{_INSTALL_HINT}")
    assert yaml.safe_load


def test_cross_module_connectors_dependency_requests() -> None:
    """跨模块依赖（X5）：connectors 套件（tests/test_transport.py）模块级导入 requests，
    缺则连接器套件在收集期整体中断，表象像“代码坏了”。本项让任一 api 套件运行也 fail-fast
    并给出安装指引（requirements.txt 已含；declaration 源头见 connectors/requirements.txt）。"""
    requests = _require(
        "requests",
        "跨模块测试依赖缺失（connectors 套件需要）；"
        f"{_INSTALL_HINT}，或 `python -m pip install -r ../../connectors/requirements.txt`",
    )
    assert requests.Session
