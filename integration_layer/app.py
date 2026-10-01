"""FastAPI 装配：请求标识、请求体上限、统一错误契约、版本化路由。"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .admin import register_admin_routes
from .auth import AuthContext, CredentialStore, require_scope
from .config_manager import ConfigManager, ConfigSnapshot
from .errors import ApiError, ErrorCode, HTTP_STATUS, error_body
from .feature_flags import FlagRule, FlagSnapshot, FlagStore
from .idempotency import (
    IdempotencyStore,
    content_fingerprint,
    OP_CREATE,
    OP_UPDATE,
    OP_DELETE,
)
from .rate_limit import RateLimiter, RateLimitRule
from .resources import ResourceStore
from .versioning import VersionRegistry, VersionSpec

logger = logging.getLogger("integration_layer")

DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_CONFIG_HISTORY_LIMIT = 10


@dataclass
class LayerConfig:
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    default_rate_rule: RateLimitRule = field(
        default_factory=lambda: RateLimitRule(capacity=20, refill_per_second=10.0)
    )
    flags: dict[str, FlagRule] = field(default_factory=dict)
    config_history_limit: int = DEFAULT_CONFIG_HISTORY_LIMIT


def build_version_registry() -> VersionRegistry:
    """注册对外版本：v1 冻结；v2 新增 labels、extended_status 与资源生命周期能力。"""
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
            known_fields=frozenset({"name", "labels", "expected_version"}),
            required_fields=frozenset({"name"}),
            capabilities=frozenset({"extended_status", "resource_lifecycle"}),
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

    # 配置版本管理：凭证/限流/灰度在同一快照内原子整体生效
    initial_flags = app.state.flags.snapshot().flags if not config.flags else dict(config.flags)
    app.state.config_manager = ConfigManager(
        ConfigSnapshot(flags=dict(initial_flags)),
        history_limit=config.config_history_limit,
        default_rate_rule=config.default_rate_rule,
    )
    app.state.credentials.bind(app.state.config_manager)
    app.state.rate_limiter.bind(app.state.config_manager)
    app.state.flags.bind(app.state.config_manager)

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

    def _authenticate(request: Request, snap: ConfigSnapshot) -> AuthContext:
        return request.app.state.credentials.authenticate(
            request.headers.get("x-api-key-id"),
            request.headers.get("x-api-secret"),
            credentials=snap.credentials,
        )

    def _request_config(request: Request) -> tuple[ConfigSnapshot, FlagSnapshot]:
        """请求开始取一次整体配置快照，整个请求内一致，不受并发运维变更影响。"""
        snap = request.app.state.config_manager.snapshot()
        return snap, FlagSnapshot(flags=dict(snap.flags))

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
        snap, flags = _request_config(request)
        ctx = _authenticate(request, snap)
        require_scope(ctx, "resources:write")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "POST /api/resources", rules=snap.rate_rules
        )

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

        def side_effect() -> tuple[int, dict[str, Any]]:
            res = request.app.state.resources.create(
                ctx.tenant_id,
                payload["name"],
                labels=payload.get("labels"),
            )
            # 创建响应保持迭代前对外契约：回显创建时提交的 name（v1/v2 均含）。
            # 响应在首次提交时定型并被幂等记录原样保存，同键重放逐字一致。
            # 生命周期字段（version/updated_at/deleted）属于后续读取/更新/删除
            # 投影，不在创建响应出现。
            out: dict[str, Any] = {
                "id": res.id,
                "name": res.name,
                "status": res.status,
                "created_at": res.created_at,
            }
            if "labels" in spec.known_fields:
                out["labels"] = res.labels
            return 201, out

        (first_status, result), replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect, op=OP_CREATE
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
            status_code=200 if replayed else first_status,
            content=body,
            headers={"x-request-id": request.state.request_id},
        )

    @app.get("/api/resources/{resource_id}")
    async def get_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        snap, _flags = _request_config(request)
        ctx = _authenticate(request, snap)
        require_scope(ctx, "resources:read")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "GET /api/resources", rules=snap.rate_rules
        )

        res = request.app.state.resources.get(ctx.tenant_id, resource_id)
        if res is None:
            raise ApiError(ErrorCode.NOT_FOUND, "resource not found")
        body = VersionRegistry.project_response(spec, res.as_dict())
        if "labels" in spec.known_fields:
            body["labels"] = res.labels
        body["request_id"] = request.state.request_id
        return JSONResponse(content=body, headers={"x-request-id": request.state.request_id})

    def _require_idem_key(request: Request) -> str:
        idem_key = request.headers.get("idempotency-key")
        if not idem_key:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "Idempotency-Key header is required"
            )
        return idem_key

    def _parse_if_match(request: Request) -> int | None:
        """解析 If-Match（资源 version）；头缺失表示不带版本前提；非法明确拒绝。"""
        raw = request.headers.get("if-match")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "If-Match must be an integer resource version",
            )
        if value < 1:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "If-Match version must be >= 1"
            )
        return value

    @app.patch("/api/resources/{resource_id}")
    async def update_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        # 更新/删除属于资源生命周期能力：旧版本明确拒绝，不悄悄降级
        VersionRegistry.require_capability(spec, "resource_lifecycle")
        snap, _flags = _request_config(request)
        ctx = _authenticate(request, snap)
        require_scope(ctx, "resources:write")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "PATCH /api/resources", rules=snap.rate_rules
        )

        payload = _parse_body(request)
        unknown = set(payload) - spec.known_fields
        if unknown:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"unknown fields for version {spec.version}: {sorted(unknown)}",
                details={"unknown": sorted(unknown), "version": spec.version},
            )
        if "expected_version" not in payload:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "missing required field: expected_version",
                details={"missing": ["expected_version"], "version": spec.version},
            )
        expected_version = payload["expected_version"]
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "expected_version must be an integer",
            )
        changes: dict[str, Any] = {}
        if "name" in payload:
            if not isinstance(payload["name"], str) or not payload["name"]:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED, "name must be a non-empty string"
                )
            changes["name"] = payload["name"]
        if "labels" in payload:
            if not isinstance(payload["labels"], dict):
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED, "labels must be an object"
                )
            changes["labels"] = payload["labels"]
        if not changes:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "update payload must contain at least one mutable field",
            )

        idem_key = _require_idem_key(request)
        # 指纹含期望版本：同键提交不同的更新前提也算不同请求
        fingerprint = content_fingerprint({"resource_id": resource_id, **payload})

        def side_effect() -> tuple[int, dict[str, Any]]:
            res = request.app.state.resources.update(
                ctx.tenant_id, resource_id, expected_version, changes
            )
            out = res.as_dict()
            # updated_at 在首次提交时固定，保证同键重放响应逐字一致
            out["updated_at"] = res.updated_at if res.updated_at is not None else out["created_at"]
            return 200, out

        (status_code, result), replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect, op=OP_UPDATE
        )

        _log(
            request.state.request_id,
            "resource_updated" if not replayed else "resource_update_replayed",
            f"tenant={ctx.tenant_id} cred={ctx.credential_fingerprint} key={idem_key}",
        )
        body = VersionRegistry.project_response(spec, result)
        if "labels" in spec.known_fields:
            body["labels"] = result.get("labels", {})
        body["request_id"] = request.state.request_id
        return JSONResponse(
            status_code=status_code,
            content=body,
            headers={"x-request-id": request.state.request_id},
        )

    @app.delete("/api/resources/{resource_id}")
    async def delete_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        VersionRegistry.require_capability(spec, "resource_lifecycle")
        snap, _flags = _request_config(request)
        ctx = _authenticate(request, snap)
        require_scope(ctx, "resources:write")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "DELETE /api/resources", rules=snap.rate_rules
        )

        expected_version = _parse_if_match(request)
        idem_key = _require_idem_key(request)
        fingerprint = content_fingerprint(
            {"resource_id": resource_id, "expected_version": expected_version}
        )

        def side_effect() -> tuple[int, dict[str, Any]]:
            res = request.app.state.resources.delete(
                ctx.tenant_id, resource_id, expected_version
            )
            out = res.as_dict()
            # 首次删除时刻固定，保证同键重放逐字一致；合成墓碑无真实删除时刻
            if out["created_at"] == 0.0:
                out["updated_at"] = None
            elif out.get("updated_at") is None:
                out["updated_at"] = out["created_at"]
            return 200, out

        (status_code, result), replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect, op=OP_DELETE
        )

        _log(
            request.state.request_id,
            "resource_deleted" if not replayed else "resource_delete_replayed",
            f"tenant={ctx.tenant_id} cred={ctx.credential_fingerprint} key={idem_key}",
        )
        body = VersionRegistry.project_response(spec, result)
        body["request_id"] = request.state.request_id
        return JSONResponse(
            status_code=status_code,
            content=body,
            headers={"x-request-id": request.state.request_id},
        )

    # 本机运维入口（凭证/限流/灰度/配置版本与回退），最后注册
    register_admin_routes(app)

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
