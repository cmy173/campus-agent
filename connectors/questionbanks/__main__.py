#!/usr/bin/env python3
"""questionbanks 最小 CLI（离线可跑；不落任何凭据，输出经脱敏）。

用法::

    python -m connectors.questionbanks capabilities [--config FILE]
    python -m connectors.questionbanks search --query "线性代数 矩阵" \
        [--searxng https://searx.example] [--config FILE] \
        [--max-candidates N] [--max-pages N] [--types choice,judgment] \
        [--allow-private] [--out FILE]
    python -m connectors.questionbanks import-url URL [URL ...] \
        [--query "关键词串"] [--allow-private] [--out FILE]

``--config`` 为 JSON 文件：{"sources": [ {...}, ... ]}（来源形状见 types.SearchRequest）。
``--allow-private`` 仅用于本地自测（放开 SSRF 防护），默认关闭并会在 stderr 提示。
输出：stdout 打印 JSON（ensure_ascii=False）；``--out`` 时同时写文件（UTF-8）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # python connectors/questionbanks/__main__.py 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors.questionbanks import (  # noqa: E402
    QuestionBankCancelled,
    QuestionBankError,
    SearchRequest,
    fetch_candidate,
    get_questionbank_capabilities,
    search_candidates,
)


def _load_config(path: str | None) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读取 --config 失败：{exc}")
    if not isinstance(data, dict):
        raise SystemExit("--config 文件必须是 JSON 对象")
    return data


def _emit(payload: dict, out_path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    print(text)


def _build_sources(args: argparse.Namespace, config: dict) -> list[dict]:
    sources: list[dict] = list(config.get("sources") or [])
    if getattr(args, "searxng", None):
        sources.append({"source_id": "cli-searxng", "kind": "search_service",
                        "provider": "searxng", "base_url": args.searxng})
    if getattr(args, "url", None):
        sources.append({"source_id": "cli-url-import", "kind": "url_import", "urls": args.url})
    return sources


def cmd_capabilities(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    _emit(get_questionbank_capabilities(config), args.out)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    sources = _build_sources(args, config)
    if not sources:
        print("未配置来源：请用 --searxng/--config 指定（或先跑 capabilities 查看支持能力）",
              file=sys.stderr)
        return 2
    if args.allow_private:
        print("[警告] --allow-private 已开启：SSRF 防护放开，仅限本机自测使用", file=sys.stderr)
    request = SearchRequest(
        query=args.query, keywords=[k for k in args.keywords.split(",") if k] if args.keywords else [],
        question_types=[t for t in (args.types or "").split(",") if t],
        max_candidates=args.max_candidates, max_pages=args.max_pages,
        sources=sources)
    try:
        outcome = search_candidates(request, cancellation=None,
                                    allow_private_addresses=args.allow_private)
    except QuestionBankCancelled:
        print("已取消", file=sys.stderr)
        return 130
    except QuestionBankError as exc:
        _emit({"error": exc.to_info()}, args.out)
        return 3
    payload = outcome.to_dict()
    _emit(payload, args.out)
    return 0 if outcome.status != "failed" else 4


def cmd_import_url(args: argparse.Namespace) -> int:
    if args.allow_private:
        print("[警告] --allow-private 已开启：SSRF 防护放开，仅限本机自测使用", file=sys.stderr)
    request = SearchRequest(
        query=args.query or "从指定 URL 提取题目",
        keywords=(args.keywords.split(",") if args.keywords else []),
        sources=[{"source_id": "cli-url-import", "kind": "url_import", "urls": args.urls}])
    try:
        outcome = search_candidates(request, cancellation=None,
                                    allow_private_addresses=args.allow_private)
    except QuestionBankError as exc:
        _emit({"error": exc.to_info()}, args.out)
        return 3
    _emit(outcome.to_dict(), args.out)
    return 0 if outcome.status != "failed" else 4


def cmd_refresh(args: argparse.Namespace) -> int:
    """按候选 JSON（文件或 stdin）刷新单题。"""
    try:
        raw = sys.stdin.read() if args.handle == "-" else Path(args.handle).read_text(encoding="utf-8")
        handle = json.loads(raw)
    except (OSError, ValueError) as exc:
        print(f"读取候选句柄失败：{exc}", file=sys.stderr)
        return 2
    if args.allow_private:
        print("[警告] --allow-private 已开启：SSRF 防护放开，仅限本机自测使用", file=sys.stderr)
    try:
        refreshed = fetch_candidate(handle, allow_private_addresses=args.allow_private)
    except QuestionBankError as exc:
        _emit({"error": exc.to_info()}, args.out)
        return 3
    _emit(refreshed, args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m connectors.questionbanks",
        description="网络题库检索与题目来源采集（只返回真实网络内容；不登录、不绕限制）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capabilities", help="查看已配置来源与支持能力")
    p_cap.add_argument("--config", help="来源配置 JSON 文件")
    p_cap.add_argument("--out", help="同时写 JSON 到文件")
    p_cap.set_defaults(func=cmd_capabilities)

    p_search = sub.add_parser("search", help="多来源检索候选题")
    p_search.add_argument("--query", required=True, help="检索词（自然语言或关键词串）")
    p_search.add_argument("--keywords", help="逗号分隔关键词（缺省由 query 拆分）")
    p_search.add_argument("--searxng", help="SearXNG 实例根地址（如 https://searx.example）")
    p_search.add_argument("--config", help="来源配置 JSON 文件")
    p_search.add_argument("--types", help="题型过滤（逗号分隔：choice,judgment,fill_blank,...）")
    p_search.add_argument("--max-candidates", type=int, default=30)
    p_search.add_argument("--max-pages", type=int, default=5)
    p_search.add_argument("--allow-private", action="store_true",
                          help="放开 SSRF 防护（仅本机自测）")
    p_search.add_argument("--out", help="同时写 JSON 到文件")
    p_search.set_defaults(func=cmd_search)

    p_import = sub.add_parser("import-url", help="从用户提供的页面 URL 提取题目")
    p_import.add_argument("urls", nargs="+", help="页面 URL（http/https）")
    p_import.add_argument("--query", help="检索词（用于匹配理由计算，可选）")
    p_import.add_argument("--keywords", help="逗号分隔关键词（可选）")
    p_import.add_argument("--allow-private", action="store_true")
    p_import.add_argument("--out", help="同时写 JSON 到文件")
    p_import.set_defaults(func=cmd_import_url)

    p_refresh = sub.add_parser("refresh", help="按候选 JSON 刷新单题（句柄来自 search 输出）")
    p_refresh.add_argument("handle", help="候选 JSON 文件路径（或 - 从 stdin 读）")
    p_refresh.add_argument("--allow-private", action="store_true")
    p_refresh.add_argument("--out", help="同时写 JSON 到文件")
    p_refresh.set_defaults(func=cmd_refresh)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
