"""日志脱敏测试：URL 查询参数在记录前剔除（uvicorn access 日志与工具函数）。"""
from __future__ import annotations

import logging

from app.log_redact import QueryStringRedactingFilter, install_logging_redaction, strip_query


def test_strip_query_removes_secret_params() -> None:
    assert strip_query("/api/v1/connections/callback?state=s&code=SECRET") == "/api/v1/connections/callback?<redacted>"
    assert strip_query("https://zjuam.zju.edu.cn/login?jwt=eyJhbGci") == "https://zjuam.zju.edu.cn/login?<redacted>"
    assert strip_query("/api/v1/health") == "/api/v1/health"
    assert strip_query("/x#frag") == "/x#frag"


def test_uvicorn_access_record_redacted_before_format() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',  # uvicorn access 的真实 msg 形态（httptools_impl）
        args=("127.0.0.1:5555", "GET", "/api/v1/connections/callback?state=s&code=SECRET", "1.1", 200),
        exc_info=None,
    )
    QueryStringRedactingFilter().filter(record)

    from uvicorn.logging import AccessFormatter

    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert "SECRET" not in line
    assert "<redacted>" in line
    assert "/api/v1/connections/callback" in line


def test_install_is_idempotent() -> None:
    install_logging_redaction()
    install_logging_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, QueryStringRedactingFilter)]
    assert len(filters) == 1


def test_record_without_query_untouched() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        pathname="",
        lineno=0,
        msg="x",
        args=("127.0.0.1:5555", "GET", "/api/v1/health", "1.1", 200),
        exc_info=None,
    )
    QueryStringRedactingFilter().filter(record)
    assert record.args[2] == "/api/v1/health"
