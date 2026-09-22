"""FastAPI 应用组装入口（应用工厂，便于测试构造可控实例）。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.dependencies import Deps
from app.errors import (
    APIError,
    ErrorCode,
    make_error_body,
    register_exception_handlers,
)
from app.logging_setup import log_event, request_id_var
from app.middleware import (
    REQUEST_ID_SCOPE_KEY,
    RequestLimitMiddleware,
)
from app.routers import admin, widgets
from app.versioning import SUPPORTED_VERSIONS, parse_path_prefix


def create_app(deps: Deps) -> FastAPI:
    """用给定依赖组装应用：错误处理、中间件、路由。"""
    app = FastAPI(
        title="Tenant API Integration Layer",
        version="1.0.0",
        # 关闭 FastAPI 默认异常对内部细节的透传，统一走我们的错误契约
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    register_exception_handlers(app)

    @app.exception_handler(404)
    async def _handle_not_found(request: Request, exc: Exception) -> JSONResponse:
        rid = request.scope.get(REQUEST_ID_SCOPE_KEY)
        path = request.url.path
        parts = path.strip("/").split("/")
        # /api/<prefix>/...：版本前缀可识别但未注册 -> 不支持的能力；
        # /api/<非法版本>/... -> 版本不存在；其他未知路径 -> 普通 not_found
        if len(parts) >= 2 and parts[0] == "api":
            from app.errors import APIError

            try:
                version = parse_path_prefix(parts[1])
            except APIError as ver_exc:
                body = make_error_body(ver_exc.code, ver_exc.message, rid, ver_exc.details)
                status = ver_exc.http_status
            else:
                body = make_error_body(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    f"v{version} 下不存在该接口能力: {path}",
                    rid,
                    details={"api_version": version, "path": path},
                )
                status = 404
        else:
            body = make_error_body(
                ErrorCode.VALIDATION_FAILED,
                f"路径不存在: {path}",
                rid,
            )
            status = 404
        return JSONResponse(
            status_code=status,
            content=body,
            headers={"X-Request-Id": rid},
        )

    # 纯 ASGI：请求 ID 注入 + 请求体大小硬上限
    app.add_middleware(RequestLimitMiddleware, config_store=deps.config_store)

    @app.middleware("http")
    async def _expose_request_id(request: Request, call_next):
        # request_id 由唯一的 ASGI 中间件生成并放入 scope/ContextVar；
        # 这里只同步到 request.state 供路由读取，响应头由 ASGI 层统一注入，
        # 保证错误体中的 request_id 与 X-Request-Id 头永远一致。
        request.state.request_id = request.scope.get(REQUEST_ID_SCOPE_KEY)
        try:
            return await call_next(request)
        except APIError as exc:
            # 经过 BaseHTTPMiddleware 重抛的业务异常：直接转译统一契约
            rid = request.scope.get(REQUEST_ID_SCOPE_KEY) or request_id_var.get()
            headers = {"X-Request-Id": rid}
            if exc.retry_after_ms is not None:
                headers["Retry-After-Ms"] = str(exc.retry_after_ms)
            log_event(
                logging.getLogger("tenant_api"),
                "request_rejected",
                decision="reject",
                reason=exc.code.value,
                http_status=exc.http_status,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=exc.http_status,
                content=make_error_body(exc.code, exc.message, rid, exc.details),
                headers=headers,
            )
        except Exception:  # noqa: BLE001 - 必须兜底，避免泄露原文
            rid = request.scope.get(REQUEST_ID_SCOPE_KEY) or request_id_var.get()
            logger = logging.getLogger("tenant_api")
            log_event(
                logger,
                "internal_error",
                decision="error",
                path=request.url.path,
            )
            logger.exception("unhandled exception in pipeline")
            return JSONResponse(
                status_code=500,
                content=make_error_body(
                    ErrorCode.INTERNAL_ERROR,
                    "服务内部错误，请携带 request_id 联系支持",
                    rid,
                ),
                headers={"X-Request-Id": rid},
            )

    widgets.register(app, deps)
    admin.register(app, deps)
    app.state.deps = deps
    return app
