"""X5 环境自检（收集前钩子）：connectors 套件的模块级第三方依赖缺失时给出明确指引。

背景：tests/test_transport.py 模块级 `import requests`，缺失时 pytest 默认在收集期
中断（"Interrupted: 1 error during collection"），表象像"业务代码坏了"。本钩子在
收集前 fail-fast，把缺失依赖与安装命令直接说清。unittest 运行不受本文件影响
（另有 tests/test_env_integrity.py 双保险）。
"""
from __future__ import annotations

import importlib.util

import pytest

# 与 tests/ 下各文件的模块级导入对齐：test_transport.py（requests）、
# validate_samples.py（jsonschema/referencing）。playwright 为延迟导入，不在此列。
_REQUIRED = ("requests", "jsonschema", "referencing")


def pytest_configure(config: pytest.Config) -> None:
    missing = [name for name in _REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        raise pytest.UsageError(
            f"连接器套件依赖缺失：{', '.join(missing)}；"
            "从仓库根执行 `python -m pip install -r connectors/requirements.txt`"
        )
