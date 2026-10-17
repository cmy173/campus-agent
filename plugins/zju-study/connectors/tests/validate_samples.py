#!/usr/bin/env python3
"""校验 connectors/samples/ 对 contracts/schemas/ 的契约符合性（M0 自检；M1 起扩展为契约测试）。

用法：python connectors/tests/validate_samples.py
依赖：jsonschema（4.18+，含 referencing）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas"
SAMPLES = ROOT / "connectors" / "samples"

registry = Registry()
for f in SCHEMAS.glob("*.schema.json"):
    contents = json.loads(f.read_text(encoding="utf-8"))
    registry = registry.with_resource(
        uri=f"file:///{SCHEMAS.as_posix()}/{f.name}",
        resource=Resource(contents=contents, specification=DRAFT202012),
    )


def absolutize(node):
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if not ref.startswith(("#", "file://", "http")):
                node["$ref"] = f"file:///{SCHEMAS.as_posix()}/{ref}"
        for v in node.values():
            absolutize(v)
    elif isinstance(node, list):
        for v in node:
            absolutize(v)


def validator(schema_name: str, pointer: str = ""):
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    if pointer:
        for part in pointer.strip("/").split("/"):
            schema = schema[part]
    absolutize(schema)
    return Draft202012Validator(schema, registry=registry)


errors: list[str] = []


def check(path: str, items, schema_name: str, pointer: str = "", key: str | None = None):
    before = len(errors)
    v = validator(schema_name, pointer)
    for i, item in enumerate(items):
        errs = sorted(v.iter_errors(item), key=lambda e: e.path)
        for e in errs:
            loc = f"{path}[{i}]" + (f" ({key})" if key else "") + " → " + "/".join(map(str, e.path))
            errors.append(f"{loc}: {e.message}")
    delta = len(errors) - before
    status = "OK" if delta == 0 else f"{delta} 处问题"
    print(f"{path}: {len(items)} 项 → {schema_name}{pointer}  {status}")


def load(name: str):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def main() -> int:
    check("capabilities.json", load("capabilities.json")["platforms"], "capabilities.schema.json")
    check("courses.json", load("courses.json")["courses"], "course.schema.json")
    check("courses.json", load("courses.json")["mapping_candidates"], "mapping-candidate.schema.json")
    check("resources.json", load("resources.json")["resources"], "resource.schema.json")
    check("assignments.json", load("assignments.json")["assignments"], "assignment.schema.json")
    check("recordings.json", load("recordings.json")["recordings"], "recording.schema.json")
    check("check-connection.json", load("check-connection.json")["samples"], "connection-state.schema.json")
    check("errors.json", load("errors.json")["errors"], "error.schema.json", "/$defs/Error")
    check("sync-result.json", [load("sync-result.json")], "sync-result.schema.json")
    real = "real-2026-10-10"
    check(
        f"{real}/resources.json",
        load(f"{real}/resources.json")["resources"],
        "resource.schema.json",
    )
    check(
        f"{real}/assignments.json",
        load(f"{real}/assignments.json")["assignments"],
        "assignment.schema.json",
    )

    if errors:
        print("\n=== 校验失败 ===")
        for e in errors:
            print("-", e)
        return 1
    print("\n全部样例通过契约校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
