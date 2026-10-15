"""日志脱敏：记录前剔除 URL 查询参数。

背景：A1 实测（§3.4-9）智云部分端点把 JWT 放进 URL 查询参数；uvicorn access
日志默认记录完整路径含查询串。本模块提供统一剔除，登录回跳等含票据参数
的端点必须经此处理后再记录（B12/CR001 安全红线第 5 条）。

- strip_query()：保留路径，查询串替换为 ?<redacted>；
- QueryStringRedactingFilter：改写 uvicorn.access 记录的 full_path（args 第 3 位）；
- install_logging_redaction()：幂等安装（API 启动时调用）。
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

REDACTED = "<redacted>"


def strip_query(value: str) -> str:
    """剔除 URL/路径中的查询串（无查询串则原样返回）。"""
    if "?" not in value:
        return value
    parts = urlsplit(value)
    if not parts.query:
        return value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, REDACTED, parts.fragment))


class QueryStringRedactingFilter(logging.Filter):
    """剔除 uvicorn.access 记录中的查询串。

    uvicorn 0.54 的访问日志 args 为 (client_addr, method, full_path, http_version, status_code)，
    且 AccessFormatter 直接从 record.args 取 full_path 组装 request_line。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) == 5
            and isinstance(args[2], str)
            and "?" in args[2]
        ):
            record.args = (args[0], args[1], strip_query(args[2]), args[3], args[4])
        return True


_installed = False


def install_logging_redaction() -> None:
    """幂等安装（uvicorn --reload 每进程调用一次；重复调用不重复挂 filter）。"""
    global _installed
    if _installed:
        return
    logging.getLogger("uvicorn.access").addFilter(QueryStringRedactingFilter())
    _installed = True
