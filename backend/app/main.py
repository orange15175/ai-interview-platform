"""新架构入口：uvicorn app.main:app --reload --port 8000

路由结构：
- /api/health        健康检查（脱敏）
- /api/interviews    面试 6 个接口（阶段 5）

旧原型 app/server.py 已在阶段 5 删除；历史接口契约见 git 记录。
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.interviews import router as interviews_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)


class PendingCookieMiddleware:
    """把依赖登记的访客 Cookie 盖到**最终响应**上（含异常处理器生成的错误响应）。

    为什么需要它：get_visitor 在依赖里 set_cookie 时，如果这条请求后来
    抛了 AppError，异常处理器新建 JSONResponse，依赖设置的头会静默丢失——
    首次访客的第一个请求恰好失败，就永远拿不到身份。纯 ASGI 实现不进
    BaseHTTPMiddleware，零开销。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # 到这一步依赖已执行完，request.state 里的登记才可见
                state = scope.get("state")
                cookie = state.get("pending_set_cookie") if isinstance(state, dict) else None
                if cookie:
                    headers = list(message.get("headers", []))
                    headers.append((b"set-cookie", cookie.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """领域错误 → JSON {code, message, retryable}，前端按 code 分支。

    http_status 由错误类自带（§10：状态码在 OpenAPI 里统一可查）。
    """
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """请求体校验失败 → 422 + 第一条人话（不把 pydantic 的原始 loc 甩给用户）。"""
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
    msg = first.get("msg", "请求参数不合法")
    return JSONResponse(
        status_code=422,
        content={"code": "INVALID_REQUEST", "message": f"{loc}: {msg}" if loc else msg,
                 "retryable": False},
    )


def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 500：不接的话 FastAPI 返回纯文本，前端 res.json() 解析失败。

    对外只给稳定 code + 人话，堆栈只进日志（绝不回传给客户端）。
    retryable=True：这类错误多数是瞬时故障，允许前端用同一个 request_id 重试
    （幂等由后端保证，不会重复计分）。
    """
    log.exception("未捕获异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务内部错误，请稍后重试",
            "retryable": True,
        },
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI 模拟面试系统",
        version="1.0.0",
        # 生产用同源代理（前端由 Vite/Nginx 反代），CORS 只是开发期方便
        docs_url="/docs",
    )

    settings = get_settings()
    app.add_middleware(PendingCookieMiddleware)
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
            allow_credentials=True,   # 匿名访客 Cookie 需要 credentials
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health_router, prefix="/api")
    app.include_router(interviews_router, prefix="/api")

    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    # 兜底：Starlette 对 Exception 的处理器只在 ServerErrorMiddleware 里生效，
    # 仍会记录日志并返回这里给的结构
    app.add_exception_handler(Exception, _unhandled_error_handler)
    return app


app = create_app()
