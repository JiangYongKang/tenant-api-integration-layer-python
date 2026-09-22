"""FastAPI 装配：请求标识、请求体上限、统一错误契约、版本化路由。"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import AuthContext, CredentialStore, require_scope
from .errors import ApiError, ErrorCode, HTTP_STATUS, error_body
from .feature_flags import FlagRule, FlagSnapshot, FlagStore
from .idempotency import IdempotencyStore, content_fingerprint
from .rate_limit import RateLimiter, RateLimitRule
from .resources import ResourceStore
from .versioning import VersionRegistry, VersionSpec

logger = logging.getLogger("integration_layer")

DEFAULT_MAX_BODY_BYTES = 64 * 1024


@dataclass
class LayerConfig:
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    default_rate_rule: RateLimitRule = field(
        default_factory=lambda: RateLimitRule(capacity=20, refill_per_second=10.0)
    )
    flags: dict[str, FlagRule] = field(default_factory=dict)


def build_version_registry() -> VersionRegistry:
    """注册对外版本：v1 冻结，v2 新增可选字段 labels 与 extended_status 能力。"""
    reg = VersionRegistry()
    reg.register(
        VersionSpec(
            version="v1",
            known_fields=frozenset({"name"}),
            required_fields=frozenset({"name"}),
            capabilities=frozenset(),
        )
    )
    reg.register(
        VersionSpec(
            version="v2",
            known_fields=frozenset({"name", "labels"}),
            required_fields=frozenset({"name"}),
            capabilities=frozenset({"extended_status"}),
        )
    )
    return reg


def create_app(config: LayerConfig | None = None) -> FastAPI:
    config = config or LayerConfig()
    app = FastAPI(title="tenant-api-integration-layer")

    # 本地状态：全部只在进程内存
    app.state.credentials = CredentialStore()
    app.state.idempotency = IdempotencyStore()
    app.state.rate_limiter = RateLimiter(config.default_rate_rule)
    app.state.flags = FlagStore(config.flags)
    app.state.resources = ResourceStore()
    app.state.versions = build_version_registry()
    app.state.config = config

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id

        # 请求体上限：先信 Content-Length；再分块读取，超过上限即停，
        # 内存占用有界（至多 max_body_bytes + 一个分块）
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared < 0 or declared > config.max_body_bytes:
                err = ApiError(
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    "request body exceeds configured limit",
                    details={"max_body_bytes": config.max_body_bytes},
                )
                return _error_response(err, request_id)
        chunks: list[bytes] = []
        received = 0
        too_large = False
        async for chunk in request.stream():
            received += len(chunk)
            if received > config.max_body_bytes:
                too_large = True
                break  # 超限即停止读取，不再占用内存
            chunks.append(chunk)
        if too_large:
            err = ApiError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "request body exceeds configured limit",
                details={"max_body_bytes": config.max_body_bytes},
            )
            return _error_response(err, request_id)
        request.state.raw_body = b"".join(chunks)

        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        _log(request_id, exc.code.value, str(exc))
        return _error_response(exc, request_id)

    @app.exception_handler(Exception)
    async def unknown_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        # 内部异常只记日志（含类型），对外不回显原文 / 堆栈 / 路径
        logger.error("internal error request_id=%s type=%s", request_id, type(exc).__name__)
        err = ApiError(ErrorCode.INTERNAL_ERROR, "internal error")
        return _error_response(err, request_id)

    def _authenticate(request: Request) -> AuthContext:
        return request.app.state.credentials.authenticate(
            request.headers.get("x-api-key-id"),
            request.headers.get("x-api-secret"),
        )

    def _parse_body(request: Request) -> dict[str, Any]:
        raw = getattr(request.state, "raw_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(ErrorCode.VALIDATION_FAILED, "request body is not valid JSON")
        if not isinstance(data, dict):
            raise ApiError(ErrorCode.VALIDATION_FAILED, "request body must be a JSON object")
        return data

    @app.post("/api/resources", status_code=201)
    async def create_resource(request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        ctx = _authenticate(request)
        require_scope(ctx, "resources:write")
        request.app.state.rate_limiter.check(ctx.tenant_id, "POST /api/resources")
        flags: FlagSnapshot = request.app.state.flags.snapshot()  # 请求内一致

        payload = _parse_body(request)
        if "labels" in payload and "labels" not in spec.known_fields:
            # labels 是 v2 新增字段；v1 请求携带即不兼容变更，明确拒绝
            raise ApiError(
                ErrorCode.INCOMPATIBLE_CHANGE,
                f"field 'labels' is not part of version {spec.version}",
                details={"version": spec.version, "field": "labels"},
            )
        VersionRegistry.validate_payload(spec, payload)

        extended = flags.is_enabled("extended-status", ctx.tenant_id)
        if extended:
            # 能力校验必须先于副作用，避免不支持能力的版本产生写入
            VersionRegistry.require_capability(spec, "extended_status")

        idem_key = request.headers.get("idempotency-key")
        if not idem_key:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "Idempotency-Key header is required"
            )

        fingerprint = content_fingerprint(payload)

        def side_effect() -> dict[str, Any]:
            res = request.app.state.resources.create(ctx.tenant_id, payload["name"])
            out = res.as_dict()
            if "labels" in spec.known_fields:
                out["labels"] = payload.get("labels", {})
            return out

        result, replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect
        )

        if extended:
            result = {**result, "status_detail": f"{result['status']}:ok"}

        _log(
            request.state.request_id,
            "resource_created" if not replayed else "resource_replayed",
            f"tenant={ctx.tenant_id} cred={ctx.credential_fingerprint} key={idem_key}",
        )
        body = VersionRegistry.project_response(spec, result)
        body["request_id"] = request.state.request_id
        return JSONResponse(
            status_code=200 if replayed else 201,
            content=body,
            headers={"x-request-id": request.state.request_id},
        )

    @app.get("/api/resources/{resource_id}")
    async def get_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        ctx = _authenticate(request)
        require_scope(ctx, "resources:read")
        request.app.state.rate_limiter.check(ctx.tenant_id, "GET /api/resources")

        res = request.app.state.resources.get(ctx.tenant_id, resource_id)
        if res is None:
            raise ApiError(ErrorCode.NOT_FOUND, "resource not found")
        body = VersionRegistry.project_response(spec, res.as_dict())
        body["request_id"] = request.state.request_id
        return JSONResponse(content=body, headers={"x-request-id": request.state.request_id})

    return app


def _log(request_id: str, event: str, detail: str) -> None:
    logger.info("request_id=%s event=%s %s", request_id, event, detail)


def _error_response(err: ApiError, request_id: str) -> JSONResponse:
    headers = {"x-request-id": request_id}
    if err.retry_after is not None:
        headers["retry-after"] = str(err.retry_after)
    return JSONResponse(
        status_code=HTTP_STATUS[err.code], content=error_body(err, request_id), headers=headers
    )
