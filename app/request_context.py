"""每请求上下文：request_id、认证主体、开关快照、配置版本。"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from app.auth import Principal
from app.errors import APIError, ErrorCode
from app.feature_flags import FeatureSnapshot


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    principal: Principal
    flags: FeatureSnapshot
    config_version: int


_ctx: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "request_context", default=None
)


def bind(ctx: RequestContext) -> contextvars.Token[RequestContext | None]:
    return _ctx.set(ctx)


def reset(token: contextvars.Token[RequestContext | None]) -> None:
    _ctx.reset(token)


def current() -> RequestContext:
    """读取当前请求上下文；缺失说明中间件/路由装配错误 -> internal_error。"""
    ctx = _ctx.get()
    if ctx is None:
        raise APIError(
            ErrorCode.INTERNAL_ERROR,
            500,
            "请求上下文未初始化",
        )
    return ctx
