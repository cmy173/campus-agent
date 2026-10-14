#!/usr/bin/env python3
"""X5 环境完整性自检（B 补）：连接器套件的模块级第三方依赖缺失必须显式失败。

背景（X5）：tests/test_transport.py 模块级 `import requests`，缺依赖时 pytest 在
收集期整体中断，表象像“业务代码坏了”（协调者环境曾遇）。本自检让其以明确信息失败。

运行：
  python connectors/tests/test_env_integrity.py
  python -m pytest connectors/tests -q          # 随全套件
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HINT = "从仓库根执行 `python -m pip install -r connectors/requirements.txt`"


def _require(module: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - 依赖齐全时不可达
        raise AssertionError(
            f"缺少必需依赖 {module!r}：{_HINT}。测试环境不完整，显式失败（fail-fast），不得静默跳过。"
        ) from exc


class EnvIntegrityTests(unittest.TestCase):
    def test_requests_for_transport_suite(self) -> None:
        """test_transport.py 模块级依赖：缺失会让套件在收集期整体中断（X5 背景场景）。"""
        requests = _require("requests")
        self.assertTrue(requests.Session)

    def test_jsonschema_stack_for_sample_validation(self) -> None:
        """tests/validate_samples.py 依赖：samples 按 contracts/schemas 全量校验所需。"""
        jsonschema = _require("jsonschema")
        referencing = _require("referencing")
        self.assertTrue(jsonschema.Draft202012Validator)
        self.assertTrue(referencing.Registry)


if __name__ == "__main__":
    unittest.main()
