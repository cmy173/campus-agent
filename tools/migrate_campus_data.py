"""Inventory, import or rollback one explicitly chosen legacy account.

Examples are in docs/agent-design/campus-migration-v2.md. Source is always opened
read-only; no automatic import into a real workspace occurs at application start.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "apps/api"), str(Path(__file__).resolve().parents[1])]
from app.campus_migration import inventory, import_legacy, rollback_import
from app.campus_workspace import CacheStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["inventory", "import", "rollback"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--owner")
    parser.add_argument("--batch-id")
    parser.add_argument("--school-cache", action="store_true")
    args = parser.parse_args()
    if args.operation == "inventory":
        if not args.source:
            parser.error("inventory requires --source")
        result = inventory(args.source)
    elif args.operation == "import":
        if not args.source or not args.target or not args.owner:
            parser.error("import requires --source, --target and --owner")
        if args.source.resolve() == args.target.resolve():
            parser.error("source and target must differ")
        result = import_legacy(args.source, CacheStore(args.target), args.owner, include_school_cache=args.school_cache)
    else:
        if not args.target or not args.batch_id:
            parser.error("rollback requires --target and --batch-id")
        if not args.target.exists():
            parser.error("rollback target does not exist")
        result = rollback_import(CacheStore(args.target), args.batch_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
