"""ASGI/HTTP 中间件：请求 ID、请求体大小上限。

请求体大小限制在读取字节流阶段执行（Content-Length 预检 + 分块接收累计），
超限立即终止接收并返回 413，不再继续缓冲，避免大请求体无界占用内存。

注意：我们不包装 ``receive`` 让上游"读到半截才报错"，而是在中间件层
主动消费 body 完成校验，再通过状态封装把缓存的消息交给下游 ASGI 应用，
因此下游永远看到完整 body 或直接收到 413。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.errors import ErrorCode, make_error_body
from app.logging_setup import (
    config_version_var,
    configure_logging,
    log_event,
    new_request_id,
    request_id_var,
    tenant_id_var,
)
from app.stores import ConfigStore

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_SCOPE_KEY = "app_request_id"
MAX_REQUEST_ID_LEN = 64

MAX_HEADER_VALUE_BYTES = 8_192  # 单个请求头值的合理上限（本地约束）

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _resolve_limit(config_store: ConfigStore, path: str, default: int) -> int:
    """认证前无法识别租户，先使用全局硬上限；

    租户级更严格的上限在认证完成后由 :func:`app.services.enforce_tenant_body_limit`
    基于已读取字节数二次判定，因此不会出现"小上限租户的大请求被缓冲"以外的
    额外内存占用，且小上限仍被稳定拒绝。
    """
    return config_store.current().default_max_body_bytes or default


class RequestLimitMiddleware:
    """纯 ASGI 中间件：注入 request_id 上下文并限制请求体字节数。"""

    def __init__(self, app: Any, config_store: ConfigStore) -> None:
        self.app = app
        self.config_store = config_store
        self.logger = configure_logging()

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming_rid = ""
        for k, v in scope.get("headers", []):
            if k == REQUEST_ID_HEADER.encode("ascii"):
                incoming_rid = v.decode("latin-1", errors="replace")
                break
        if incoming_rid and 1 <= len(incoming_rid) <= MAX_REQUEST_ID_LEN and all(
            0x21 <= ord(c) <= 0x7E for c in incoming_rid
        ):
            rid = incoming_rid
        else:
            rid = new_request_id()

        rid_token = request_id_var.set(rid)
        scope[REQUEST_ID_SCOPE_KEY] = rid
        # BaseHTTPMiddleware 会浅拷贝 scope；state 作为可变对象内嵌后，
        # 内层路由经 request.state 取到的始终是同一个 request_id。
        from starlette.datastructures import State as ScopeState

        state = scope.get("state")
        if not isinstance(state, ScopeState):
            state = ScopeState()
            scope["state"] = state
        # State 对象内部用 _state 字典存储
        state._state["request_id"] = rid  # type: ignore[attr-defined]

        async def send_with_rid(message: dict[str, Any]) -> None:
            # 统一在最外层注入 X-Request-Id，保证响应头与错误体使用同一个 id；
            # 若内层处理器已设置同名头则不重复添加。
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(k == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode("ascii")))
            await send(message)

        try:
            method = scope.get("method", "").upper()
            if method in ("POST", "PUT", "PATCH"):
                limit = _resolve_limit(
                    self.config_store, scope.get("path", ""), 65_536
                )
                body, error_sent = await self._read_limited(
                    scope, receive, send_with_rid, limit, rid
                )
                if error_sent:
                    return

                async def cached_receive() -> dict[str, Any]:
                    # 下游通过 FastAPI 读取 body：先吐出已缓存的 http.request，
                    # 然后补一个空的收尾消息。
                    if not getattr(cached_receive, "_done", False):
                        cached_receive._done = True  # type: ignore[attr-defined]
                        return {"type": "http.request", "body": body, "more_body": False}
                    return {"type": "http.disconnect"}

                cached_receive._done = False  # type: ignore[attr-defined]
                await self.app(scope, cached_receive, send_with_rid)
            else:
                await self.app(scope, receive, send_with_rid)
        finally:
            request_id_var.reset(rid_token)
            tenant_id_var.set(None)
            config_version_var.set(None)

    async def _read_limited(
        self, scope: dict, receive: Receive, send: Send, limit: int, rid: str
    ) -> tuple[bytes, bool]:
        """分块读取 body 并累计字节；超限立即发 413，返回 (b'', True)。"""
        # 1) Content-Length 预检：明确超限就完全不读 body
        declared: int | None = None
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    declared = int(v.decode("ascii"))
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            await self._reject_oversize(send, rid, limit, declared, via="content-length")
            log_event(
                self.logger,
                "request_rejected",
                decision="reject",
                reason=ErrorCode.PAYLOAD_TOO_LARGE.value,
                path=scope.get("path"),
                input_declared_bytes=declared,
                decision_limit_bytes=limit,
            )
            return b"", True

        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    # 非预期消息（如 disconnect）：交回下游处理
                    break
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > limit:
                    await self._reject_oversize(send, rid, limit, total, via="streamed")
                    log_event(
                        self.logger,
                        "request_rejected",
                        decision="reject",
                        reason=ErrorCode.PAYLOAD_TOO_LARGE.value,
                        path=scope.get("path"),
                        input_received_bytes=total,
                        decision_limit_bytes=limit,
                    )
                    return b"", True
                if chunk:
                    chunks.append(chunk)
                if not message.get("more_body"):
                    break
        except Exception:
            # 读取阶段异常：不泄露原文，统一 500（由全局契约兜底）
            raise
        return b"".join(chunks), False

    async def _reject_oversize(
        self, send: Send, rid: str, limit: int, observed: int, via: str
    ) -> None:
        body = make_error_body(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"请求体超过允许上限 {limit} 字节，已在读取阶段拒绝",
            rid,
            details={
                "max_body_bytes": limit,
                "observed_bytes": observed,
                "detected_via": via,
            },
        )
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-request-id", rid.encode("ascii")),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
