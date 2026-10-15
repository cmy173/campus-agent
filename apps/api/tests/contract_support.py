"""契约校验支持（B16）：把实际响应体对 contracts/schemas/ 做 JSON Schema 校验。

同一校验器供各测试文件复用（test_contract_conformance / test_build_tasks），
亦可作独立校验脚本，对捕获的响应 JSON 快速核对契约：

    python apps/api/tests/contract_support.py <schema 文件名> <响应体.json>

契约 v0.1 已冻结（B6）：只允许兼容新增；任何结构变更必须走
contracts/reviews/CHANGE-REQUEST.md 并同步 contracts/CHANGELOG.md。
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schemas"


@lru_cache(maxsize=None)
def load_schema(schema_name: str) -> dict:
    return json.loads((SCHEMAS_DIR / schema_name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _registry():
    from referencing import Registry, Resource

    return Registry().with_resources(
        [
            (path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))))
            for path in SCHEMAS_DIR.glob("*.schema.json")
        ]
    )


def schema_errors(instance: object, schema_name: str) -> list[str]:
    """返回校验错误消息列表（空列表 = 通过）。跨文件 $ref 由 referencing 注册表解析。"""
    import jsonschema

    validator = jsonschema.Draft202012Validator(load_schema(schema_name), registry=_registry())
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]


def validate_contract(instance: object, schema_name: str) -> None:
    errors = schema_errors(instance, schema_name)
    assert not errors, f"响应体不符合 {schema_name}：{errors}"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    schema_name, path = argv[1], argv[2]
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = schema_errors(body, schema_name)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {path} 符合 {schema_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
