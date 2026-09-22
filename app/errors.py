"""统一机器可读错误契约。

任何对外错误都必须通过 :class:`APIError` 或全局异常处理器序列化为
:class:`ErrorBody`，禁止把堆栈、内部路径或底层异常原文返回给调用方。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logging_setup import log_event, new_request_id, request_id_var


class ErrorCode(str, Enum):
    """稳定的机器可读错误码（对外契约，不得随意改名）。"""

    VERSION_NOT_FOUND = "version_not_found"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INCOMPATIBLE_REQUEST = "incompatible_request"
    VALIDATION_FAILED = "validation_failed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNAUTHENTICATED = "unauthenticated"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_INVALID = "credential_invalid"
    CREDENTIAL_EXPIRED = "credential_expired"
    PERMISSION_DENIED = "permission_denied"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RATE_LIMITED = "rate_limited"
    CONCURRENT_CONFLICT = "concurrent_conflict"
    CONFIG_INVALID = "config_invalid"
    INTERNAL_ERROR = "internal_error"


class ErrorBody(dict):
    """标准错误响应体::

        {"error": {"code": ..., "message": ..., "request_id": ..., "details": ...}}
    """


def make_error_body(
    code: ErrorCode,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code.value,
            "message": message,
            "request_id": request_id,
        }
    }
    if details:
        body["error"]["details"] = details
    return body


class APIError(Exception):
    """业务层抛出的、可安全对外展示的错误。"""

    def __init__(
        self,
        code: ErrorCode,
        http_status: int,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = details
        self.retry_after_ms = retry_after_ms


def _safe_request_id(request: Request | None) -> str:
    rid = request_id_var.get()
    if rid:
        return rid
    if request is not None:
        candidate = request.headers.get("x-request-id")
        if candidate and len(candidate) <= 64:
            return candidate
    return new_request_id()


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器（把异常翻译成统一错误契约）。"""
    logger = logging.getLogger("tenant_api")

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        rid = _safe_request_id(request)
        headers = {"X-Request-Id": rid}
        if exc.retry_after_ms is not None:
            headers["Retry-After-Ms"] = str(exc.retry_after_ms)
        log_event(
            logger,
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

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = _safe_request_id(request)
        # 仅保留字段名与校验类型，不回显 pydantic 内部 ctx/异常原文
        safe_errors = [
            {"field": ".".join(str(p) for p in e.get("loc", []) if p != "body"),
             "rule": e.get("type", "value_error")}
            for e in exc.errors()
        ]
        log_event(
            logger,
            "request_rejected",
            decision="reject",
            reason=ErrorCode.VALIDATION_FAILED.value,
            path=request.url.path,
            fields=safe_errors,
        )
        return JSONResponse(
            status_code=422,
            content=make_error_body(
                ErrorCode.VALIDATION_FAILED,
                "请求参数校验失败",
                rid,
                {"fields": safe_errors},
            ),
            headers={"X-Request-Id": rid},
        )

    @app.exception_handler(Exception)
    async def _handle_unknown(request: Request, exc: Exception) -> JSONResponse:
        rid = _safe_request_id(request)
        # 日志可记录异常类型用于排障；响应只给稳定文案，绝不泄露堆栈/路径/原文
        log_event(
            logger,
            "internal_error",
            decision="error",
            path=request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content=make_error_body(
                ErrorCode.INTERNAL_ERROR,
                "服务内部错误，请携带 request_id 联系支持",
                rid,
            ),
            headers={"X-Request-Id": rid},
        )
