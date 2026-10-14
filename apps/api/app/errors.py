"""统一错误信封（契约 contracts/schemas/error.schema.json）。

所有非 2xx 响应形如 {"error": {code, message, retryable, details, request_id}}；
request_id 由 main.py 的中间件写入 request.state，异常处理器负责拼装。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


def error_response(request: Request, status_code: int, code: str, message: str, retryable: bool, details: dict | None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "req_unknown")
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details,
                "request_id": request_id,
            }
        },
    )


def not_found(kind: str, object_id: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{kind}不存在：{object_id}", details={"kind": kind, "id": object_id})
