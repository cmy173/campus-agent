"""契约自检：校验 schemas/ 元模式、解析全部 $ref、检查 OpenAPI 基本结构。

用法（仓库根或任意目录均可）：
    python contracts/tools/validate_contracts.py

退出码 0 = 通过；非 0 = 存在问题（输出问题清单）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema.validators import Draft202012Validator

CONTRACTS_DIR = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = CONTRACTS_DIR / "schemas"
OPENAPI_PATH = CONTRACTS_DIR / "openapi" / "openapi.yaml"

errors: list[str] = []


def load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_pointer(doc, pointer: str):
    if pointer in ("", "/"):
        return doc
    node = doc
    for part in pointer.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise KeyError(pointer)
    return node


def walk_refs(node, found: list[str]):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                walk_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            walk_refs(item, found)


def main() -> int:
    schema_docs: dict[Path, dict] = {}
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        try:
            doc = load_json(path)
        except json.JSONDecodeError as exc:
            errors.append(f"[JSON] {path.name}: {exc}")
            continue
        schema_docs[path] = doc
        try:
            Draft202012Validator.check_schema(doc)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[MetaSchema] {path.name}: {exc}")

    # schemas 内部及跨文件 $ref
    for path, doc in schema_docs.items():
        refs: list[str] = []
        walk_refs(doc, refs)
        for ref in refs:
            file_part, _, pointer = ref.partition("#")
            if not file_part:
                target_path, target_doc = path, doc
            else:
                target_path = (path.parent / file_part).resolve()
                target_doc = schema_docs.get(target_path)
                if target_doc is None:
                    errors.append(f"[RefFile] {path.name}: 引用了未加载的文件 {file_part}")
                    continue
            try:
                resolve_pointer(target_doc, pointer)
            except (KeyError, IndexError, ValueError):
                errors.append(f"[RefPointer] {path.name}: 无法解析 {ref}")

    # OpenAPI
    if not OPENAPI_PATH.exists():
        errors.append(f"[OpenAPI] 缺少 {OPENAPI_PATH}")
    else:
        try:
            spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"[OpenAPI] YAML 解析失败: {exc}")
            spec = None
        if spec is not None:
            for field in ("openapi", "info", "paths", "components"):
                if field not in spec:
                    errors.append(f"[OpenAPI] 缺少顶层字段 {field}")
            if not str(spec.get("openapi", "")).startswith("3.1"):
                errors.append(f"[OpenAPI] 版本应为 3.1.x，实际 {spec.get('openapi')}")
            refs: list[str] = []
            walk_refs(spec, refs)
            for ref in refs:
                file_part, _, pointer = ref.partition("#")
                if not file_part:
                    try:
                        resolve_pointer(spec, pointer)
                    except (KeyError, IndexError, ValueError):
                        errors.append(f"[OpenAPI] 无法解析内部引用 {ref}")
                    continue
                target_path = (OPENAPI_PATH.parent / file_part).resolve()
                target_doc = schema_docs.get(target_path)
                if target_doc is None:
                    errors.append(f"[OpenAPI] 引用了未加载的文件 {file_part}（ref: {ref}）")
                    continue
                try:
                    resolve_pointer(target_doc, pointer)
                except (KeyError, IndexError, ValueError):
                    errors.append(f"[OpenAPI] 无法解析外部引用 {ref}")
            # 每个 operation 必须有 operationId 与 responses
            for route, item in (spec.get("paths") or {}).items():
                for method, op in item.items():
                    if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                        continue
                    if "operationId" not in op:
                        errors.append(f"[OpenAPI] {method.upper()} {route} 缺少 operationId")
                    if not op.get("responses"):
                        errors.append(f"[OpenAPI] {method.upper()} {route} 缺少 responses")

    if errors:
        print(f"发现 {len(errors)} 个问题：")
        for item in errors:
            print(" -", item)
        return 1
    print(f"契约自检通过：{len(schema_docs)} 个 schema，OpenAPI 结构正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
