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
    agent_schedules,
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
    from .agent_bridge.schedules import get_scheduler
    agent_scheduler = get_scheduler(agent_runtime)
    agent_scheduler.start()
    try:
        yield
    finally:
        from .campus_workspace import close_campus_workspace
        close_campus_workspace()
        agent_scheduler.close()
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
    if request.url.path.startswith('/api/v1'):
        import os
        # Uniform control-plane protection: legacy APIs must not be the anonymous
        # bypass for model-facing tool scopes or browser approvals.
        if request.url.hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'}:
            return error_response(request, 403, 'LOCAL_ONLY', '当前工作台仅供本机访问', False, None)
        origin = request.headers.get('origin')
        allowed = {str(request.base_url).rstrip('/')} | set(os.environ.get('CAMPUS_WEB_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173').split(','))
        if origin and origin.rstrip('/') not in allowed or request.headers.get('sec-fetch-site') == 'cross-site':
            return error_response(request, 403, 'ORIGIN_DENIED', '不接受外部网页访问本地工作台数据', False, None)
        if request.headers.get('authorization', '').startswith('Bearer ') and not request.url.path.startswith('/api/v1/agent/internal/'):
            return error_response(request, 403, 'RUNTIME_DENIED', '运行时凭据不能调用账户设置与人工审批接口', False, None)
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
    agent_schedules,
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

# 校园工作区路由在装配期挂载（不是 lifespan 内）：运行时、OpenAPI 与契约导出看到同一份
# 路由表，否则契约回归会把正常路由误判为实现漂移。复用网关 owner/Origin 依赖。
from .routers.campus import install_campus_router  # noqa: E402

install_campus_router(app, agent.browser_owner)

from .routers.agent_data import install_agent_data_router  # noqa: E402

install_agent_data_router(app, agent.browser_owner)
