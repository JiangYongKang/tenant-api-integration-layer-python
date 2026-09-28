"""统一错误契约：稳定的机器可读错误码与对外响应结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """对外暴露的稳定错误码。任何内部异常都必须映射到其中之一。"""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    INCOMPATIBLE_CHANGE = "INCOMPATIBLE_CHANGE"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CREDENTIAL_INVALID = "CREDENTIAL_INVALID"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    CONCURRENT_CONFLICT = "CONCURRENT_CONFLICT"
    INVALID_CONFIG = "INVALID_CONFIG"
    CONFIG_VERSION_NOT_FOUND = "CONFIG_VERSION_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# 错误码 -> HTTP 状态码（对外契约的一部分，保持稳定）
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_VERSION: 400,
    ErrorCode.UNSUPPORTED_CAPABILITY: 400,
    ErrorCode.INCOMPATIBLE_CHANGE: 409,
    ErrorCode.CREDENTIAL_MISSING: 401,
    ErrorCode.CREDENTIAL_INVALID: 401,
    ErrorCode.CREDENTIAL_EXPIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.CONCURRENT_CONFLICT: 409,
    ErrorCode.INVALID_CONFIG: 400,
    ErrorCode.CONFIG_VERSION_NOT_FOUND: 404,
    ErrorCode.INTERNAL_ERROR: 500,
}


@dataclass
class ApiError(Exception):
    """接入层内部使用的异常类型；对外只暴露 code/message/request_id。"""

    code: ErrorCode
    message: str
    retry_after: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # 不含堆栈与内部路径
        return f"{self.code.value}: {self.message}"


def error_body(err: ApiError, request_id: str) -> dict[str, Any]:
    """构造对外错误响应体（不含堆栈、内部路径、凭证）。"""
    body: dict[str, Any] = {
        "error": {
            "code": err.code.value,
            "message": err.message,
            "request_id": request_id,
        }
    }
    if err.retry_after is not None:
        body["error"]["retry_after_seconds"] = err.retry_after
    if err.details:
        body["error"]["details"] = err.details
    return body
