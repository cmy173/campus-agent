"""FastAPI 入口：request-id 中间件 + 统一错误信封 + 路由装配。

读写端点均已落库（SQLite/PostgreSQL，见 app/models.py），契约 v0.1 端点全部实现；
未知路径与不支持的方法由 Starlette 404/405 经统一错误信封返回。
后台任务由 app/worker.py 执行：独立进程（python -m app.worker）或
STUDY_WORKER_INPROC=1 时在 API 进程内启动守护线程。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import worker
from . import connectors_registry
from .config import APP_VERSION, REQUEST_ID_HEADER
from .errors import ApiError, error_response
from .log_redact import install_logging_redaction
from .routers import (
    agent,
    ai_providers,
    assignments,
    connections,
    courses,
    exports,
    files,
    health,
    imports,
    jobs,
    plans,
    preferences,
    resources,
    recordings,
    review_papers,
    search,
    tasks,
)

API = "/api/v1"

_STATUS_CODES = {
    400: "VALIDATION_FAILED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_logging_redaction()
    # 平台连接器默认注册（A20 桥接）：使 /connections/{id}/check 与 /sync 开箱可用；
    # 未注册时才表现为 DEPENDENCY_UNAVAILABLE（如实报错，不假装可用）。
    connectors_registry.register_default_adapters()
    worker_stop = worker.start_inproc_if_enabled()
    from .agent_bridge.runtime import get_runtime
    agent_runtime = get_runtime()
    agent_runtime.store.recover()
    try:
        yield
    finally:
        agent_runtime.close()
        if worker_stop is not None:
            worker_stop.set()


app = FastAPI(
    title="浙大学习工作台 API",
    description="契约驱动的学习工作台后端（核心对象已落库；契约见仓库 contracts/）。",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex[:12]}"
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request.state.request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return error_response(request, exc.status_code, exc.code, exc.message, exc.retryable, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    fields = []
    for err in exc.errors():
        parts = [str(p) for p in err.get("loc", ())]
        if parts and parts[0] == "body":
            parts = parts[1:]
        fields.append({"field": ".".join(parts), "reason": err.get("msg", "")})
    return error_response(request, 400, "VALIDATION_FAILED", "请求参数校验失败", False, {"fields": fields})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    code = _STATUS_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
    return error_response(request, exc.status_code, code, str(exc.detail), False, None)


for r in (
    agent,
    health,
    courses,
    resources,
    recordings,
    assignments,
    tasks,
    plans,
    preferences,
    connections,
    imports,
    exports,
    jobs,
    files,
    search,
    ai_providers,
    review_papers,
):
    app.include_router(r.router, prefix=API)
