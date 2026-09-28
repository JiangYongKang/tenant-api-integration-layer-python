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
from .config_manager import ConfigManager, mask_secret
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
    # 运维入口允许访问的对端地址；默认仅本机回环
    admin_allowed_hosts: frozenset[str] = frozenset({"127.0.0.1", "::1", "testclient"})
    config_history_size: int = 20


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

    # 运行时配置版本管理器：凭证/限流/灰度统一为不可变快照，原子提交与回退
    app.state.manager = ConfigManager(
        initial_flags=dict(config.flags),
        history_size=config.config_history_size,
    )
    app.state.credentials.bind(app.state.manager)
    app.state.rate_limiter.bind(app.state.manager)
    app.state.flags.bind(app.state.manager)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        # 请求开始即固定运行时配置快照：整个请求内凭证/限流/灰度一致，
        # 处理过程中发生配置切换或回退也不会把该请求带进中间状态
        request.state.runtime_config = request.app.state.manager.current()

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

    @app.middleware("http")
    async def admin_loopback_middleware(request: Request, call_next):
        # 运维入口只允许本机回环访问；非回环对端一律 PERMISSION_DENIED
        if request.url.path.startswith("/admin"):
            client_host = request.client.host if request.client else None
            if client_host not in config.admin_allowed_hosts:
                request_id = (
                    request.headers.get("x-request-id")
                    or getattr(request.state, "request_id", None)
                    or uuid.uuid4().hex
                )
                err = ApiError(
                    ErrorCode.PERMISSION_DENIED,
                    "admin endpoints are only available from localhost",
                    details={"path": "/admin"},
                )
                _log(request_id, "admin_denied", f"host={client_host}")
                return _error_response(err, request_id)
        return await call_next(request)

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
            snapshot=getattr(request.state, "runtime_config", None),
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
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "POST /api/resources",
            snapshot=request.state.runtime_config,
        )
        flags: FlagSnapshot = request.state.runtime_config.flag_snapshot()  # 请求内快照一致

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
            res = request.app.state.resources.create(
                ctx.tenant_id,
                payload["name"],
                labels=payload.get("labels") if "labels" in spec.known_fields else None,
            )
            out = res.as_dict()
            if "labels" in spec.known_fields:
                out["labels"] = payload.get("labels", {})
            return {"body": out, "version": res.version}

        descriptor, replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect
        )
        result = descriptor["body"]

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
            headers={
                "x-request-id": request.state.request_id,
                **_version_header(descriptor["version"]),
            },
        )

    def _require_idem_key(request: Request) -> str:
        idem_key = request.headers.get("idempotency-key")
        if not idem_key:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "Idempotency-Key header is required"
            )
        return idem_key

    def _version_header(version: int) -> dict[str, str]:
        return {"x-resource-version": str(version)}

    @app.patch("/api/resources/{resource_id}")
    async def update_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        ctx = _authenticate(request)
        require_scope(ctx, "resources:write")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "PATCH /api/resources/{id}",
            snapshot=request.state.runtime_config,
        )
        flags: FlagSnapshot = request.state.runtime_config.flag_snapshot()  # 请求内快照一致

        payload = _parse_body(request)
        if "labels" in payload and "labels" not in spec.known_fields:
            # labels 是 v2 新增字段；v1 更新携带即不兼容变更，明确拒绝
            raise ApiError(
                ErrorCode.INCOMPATIBLE_CHANGE,
                f"field 'labels' is not part of version {spec.version}",
                details={"version": spec.version, "field": "labels"},
            )
        allowed = {"expected_version", "name"} | (
            {"labels"} if "labels" in spec.known_fields else set()
        )
        unknown = set(payload) - allowed
        if unknown:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"unknown fields for version {spec.version}: {sorted(unknown)}",
                details={"unknown": sorted(unknown), "version": spec.version},
            )
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "'expected_version' is required and must be an integer",
                details={"field": "expected_version"},
            )
        changes: dict[str, Any] = {}
        if "name" in payload:
            if not isinstance(payload["name"], str) or not payload["name"]:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    "'name' must be a non-empty string when provided",
                )
            changes["name"] = payload["name"]
        if "labels" in payload:
            if not isinstance(payload["labels"], dict):
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED, "'labels' must be an object when provided"
                )
            changes["labels"] = payload["labels"]
        if not changes:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "update payload contains no mutable fields"
            )

        extended = flags.is_enabled("extended-status", ctx.tenant_id)
        if extended:
            # 能力校验必须先于副作用，避免不支持能力的版本产生写入
            VersionRegistry.require_capability(spec, "extended_status")

        idem_key = _require_idem_key(request)
        fingerprint = content_fingerprint(
            {"resource_id": resource_id, "payload": payload}
        )

        def side_effect() -> dict[str, Any]:
            outcome, res = request.app.state.resources.compare_and_update(
                ctx.tenant_id, resource_id, expected_version, changes
            )
            if outcome == "not_found":
                raise ApiError(ErrorCode.NOT_FOUND, "resource not found")
            if outcome == "stale":
                # 冲突是确定性结果，记录进幂等结果：同键同内容重放得到完全一致的冲突
                return {
                    "status": 409,
                    "conflict": {
                        "expected_version": expected_version,
                        "current_version": res.version,
                    },
                }
            out = res.lifecycle_dict()
            if extended:
                out["status_detail"] = f"{out['status']}:ok"
            projected = VersionRegistry.project_response(spec, out)
            return {"status": 200, "version": out["version"], "body": projected}

        descriptor, replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect, operation="update"
        )

        if descriptor.get("status") == 409:
            # 与参数错误 / 鉴权问题 / 幂等冲突明确区分，并告知当前版本
            conflict = descriptor["conflict"]
            raise ApiError(
                ErrorCode.CONCURRENT_CONFLICT,
                "resource version conflict: the resource was modified by "
                "another request; refetch and retry",
                details=conflict,
            )

        _log(
            request.state.request_id,
            "resource_updated" if not replayed else "resource_update_replayed",
            f"tenant={ctx.tenant_id} cred={ctx.credential_fingerprint} key={idem_key}",
        )
        body = dict(descriptor["body"])
        body["request_id"] = request.state.request_id
        return JSONResponse(
            status_code=descriptor["status"],
            content=body,
            headers={
                "x-request-id": request.state.request_id,
                **_version_header(descriptor["version"]),
            },
        )

    @app.delete("/api/resources/{resource_id}")
    async def delete_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        ctx = _authenticate(request)
        require_scope(ctx, "resources:write")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "DELETE /api/resources/{id}",
            snapshot=request.state.runtime_config,
        )
        idem_key = _require_idem_key(request)
        fingerprint = content_fingerprint({"resource_id": resource_id, "delete": True})

        def side_effect() -> dict[str, Any]:
            outcome, res = request.app.state.resources.delete(ctx.tenant_id, resource_id)
            if outcome == "not_found":
                raise ApiError(ErrorCode.NOT_FOUND, "resource not found")
            # 幂等删除：首次 deleted 与再次删除 already_deleted 都给出 200 与可解释视图
            body = {
                "id": res.id,
                "status": res.status,
                "version": res.version,
                "deleted": True,
                "result": outcome,
            }
            if res.deleted_at is not None:
                body["deleted_at"] = res.deleted_at
            return {"status": 200, "version": res.version, "body": body}

        descriptor, replayed = request.app.state.idempotency.execute(
            ctx.tenant_id, idem_key, fingerprint, side_effect, operation="delete"
        )

        _log(
            request.state.request_id,
            "resource_deleted" if not replayed else "resource_delete_replayed",
            f"tenant={ctx.tenant_id} cred={ctx.credential_fingerprint} key={idem_key}",
        )
        body = dict(descriptor["body"])
        body["request_id"] = request.state.request_id
        return JSONResponse(
            status_code=descriptor["status"],
            content=body,
            headers={
                "x-request-id": request.state.request_id,
                **_version_header(descriptor["version"]),
            },
        )

    @app.get("/api/resources/{resource_id}")
    async def get_resource(resource_id: str, request: Request) -> JSONResponse:
        spec = request.app.state.versions.get(request.headers.get("x-api-version"))
        ctx = _authenticate(request)
        require_scope(ctx, "resources:read")
        request.app.state.rate_limiter.check(
            ctx.tenant_id, "GET /api/resources",
            snapshot=request.state.runtime_config,
        )

        res = request.app.state.resources.get(ctx.tenant_id, resource_id)
        if res is None:
            raise ApiError(ErrorCode.NOT_FOUND, "resource not found")
        body = VersionRegistry.project_response(spec, res.as_dict())
        body["request_id"] = request.state.request_id
        return JSONResponse(
            content=body,
            headers={
                "x-request-id": request.state.request_id,
                **_version_header(res.version),
            },
        )

    # ===================== 本机运维入口（仅回环） =====================

    def _admin_ok(request: Request, payload: dict[str, Any], event: str) -> JSONResponse:
        version = request.app.state.manager.current().version
        _log(request.state.request_id, event, f"config_version={version}")
        payload = dict(payload)
        payload["request_id"] = request.state.request_id
        return JSONResponse(
            content=payload,
            headers={
                "x-request-id": request.state.request_id,
                "x-config-version": str(version),
            },
        )

    @app.post("/admin/credentials/issue")
    async def admin_issue_credential(request: Request) -> JSONResponse:
        data = _parse_body(request)
        tenant_id = data.get("tenant_id")
        scopes = data.get("scopes")
        expires_at = data.get("expires_at")
        # 结构问题 -> VALIDATION_FAILED；取值非法由 manager 明确拒绝为 INVALID_CONFIG
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'scopes' is required and must be a list of strings"
            )
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'expires_at' must be epoch seconds or null"
            )
        manager: ConfigManager = request.app.state.manager
        before = manager.current().version
        key_id, secret = manager.issue_credential(
            tenant_id, set(scopes), expires_at=expires_at
        )
        # HTTP 响应与日志绝不包含完整密钥，只给脱敏片段与指纹
        body = {
            "key_id": key_id,
            "tenant_id": tenant_id,
            "fingerprint": manager.current().credentials[key_id].fingerprint,
            "secret_masked": mask_secret(secret),
            "config_version": manager.current().version,
        }
        assert manager.current().version == before + 1
        return _admin_ok(request, body, "credential_issued")

    @app.post("/admin/credentials/rotate")
    async def admin_rotate_credential(request: Request) -> JSONResponse:
        data = _parse_body(request)
        key_id = data.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'key_id' is required and must be a string"
            )
        manager: ConfigManager = request.app.state.manager
        if key_id not in manager.current().credentials:
            raise ApiError(ErrorCode.NOT_FOUND, f"credential not found: {key_id}")
        old_fp = manager.current().credentials[key_id].fingerprint
        secret = manager.rotate_credential(key_id)
        new_fp = manager.current().credentials[key_id].fingerprint
        return _admin_ok(
            request,
            {
                "key_id": key_id,
                "previous_fingerprint": old_fp,
                "fingerprint": new_fp,
                "secret_masked": mask_secret(secret),
                "rotated": True,
                "config_version": manager.current().version,
            },
            "credential_rotated",
        )

    @app.post("/admin/credentials/revoke")
    async def admin_revoke_credential(request: Request) -> JSONResponse:
        data = _parse_body(request)
        key_id = data.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'key_id' is required and must be a string"
            )
        manager: ConfigManager = request.app.state.manager
        existed = key_id in manager.current().credentials
        revoked = manager.revoke_credential(key_id)  # 幂等：重复吊销不产生新版本
        return _admin_ok(
            request,
            {
                "key_id": key_id,
                "revoked": revoked,
                "existed": existed,
                "config_version": manager.current().version,
            },
            "credential_revoked",
        )

    @app.put("/admin/rate-limits")
    async def admin_set_rate_limit(request: Request) -> JSONResponse:
        data = _parse_body(request)
        tenant_id = data.get("tenant_id")
        endpoint = data.get("endpoint")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'tenant_id' is required and must be a string"
            )
        if not isinstance(endpoint, str) or not endpoint:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'endpoint' is required and must be a string"
            )
        capacity = data.get("capacity")
        refill = data.get("refill_per_second")
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "'capacity' is required and must be an integer"
            )
        if not isinstance(refill, (int, float)) or isinstance(refill, bool):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "'refill_per_second' is required and must be a number",
            )
        manager: ConfigManager = request.app.state.manager
        rule = RateLimitRule(capacity=capacity, refill_per_second=float(refill))
        manager.set_rate_rule(tenant_id, endpoint, rule)
        return _admin_ok(
            request,
            {
                "tenant_id": tenant_id,
                "endpoint": endpoint,
                "capacity": rule.capacity,
                "refill_per_second": rule.refill_per_second,
                "config_version": manager.current().version,
            },
            "rate_limit_updated",
        )

    @app.put("/admin/flags/{flag_name}")
    async def admin_set_flag(flag_name: str, request: Request) -> JSONResponse:
        data = _parse_body(request)
        rollout = data.get("rollout", 0)
        overrides = data.get("overrides", {})
        if not isinstance(overrides, dict) or not all(
            isinstance(k, str) and isinstance(v, bool) for k, v in overrides.items()
        ):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "'overrides' must be an object of string -> boolean",
            )
        manager: ConfigManager = request.app.state.manager
        rule = FlagRule(rollout=rollout, overrides=dict(overrides))  # 非法即 INVALID_CONFIG
        manager.set_flag(flag_name, rule)
        return _admin_ok(
            request,
            {
                "flag": flag_name,
                "rollout": rule.rollout,
                "overrides": dict(rule.overrides),
                "config_version": manager.current().version,
            },
            "flag_updated",
        )

    @app.get("/admin/config")
    async def admin_get_config(request: Request) -> JSONResponse:
        manager: ConfigManager = request.app.state.manager
        view = manager.current().public_view()
        view["request_id"] = request.state.request_id
        return JSONResponse(
            content=view,
            headers={
                "x-request-id": request.state.request_id,
                "x-config-version": str(view["version"]),
            },
        )

    @app.get("/admin/config/versions")
    async def admin_list_versions(request: Request) -> JSONResponse:
        manager: ConfigManager = request.app.state.manager
        versions = manager.history_versions()
        body = {
            "current_version": versions[-1] if versions else manager.current().version,
            "versions": versions,
            "request_id": request.state.request_id,
        }
        return JSONResponse(content=body, headers={"x-request-id": request.state.request_id})

    @app.post("/admin/config/rollback")
    async def admin_rollback(request: Request) -> JSONResponse:
        data = _parse_body(request)
        target = data.get("version")
        if not isinstance(target, int) or isinstance(target, bool):
            # 目标无法定位为某个版本：与“配置内容非法”区分
            raise ApiError(
                ErrorCode.CONFIG_VERSION_NOT_FOUND,
                "'version' is required and must be an integer",
            )
        manager: ConfigManager = request.app.state.manager
        cfg = manager.rollback(target)  # 目标不存在/不可用 -> CONFIG_VERSION_NOT_FOUND
        view = cfg.public_view()
        view["rolled_back_from"] = target
        view["request_id"] = request.state.request_id
        _log(
            request.state.request_id,
            "config_rolled_back",
            f"from={target} now={cfg.version}",
        )
        return JSONResponse(
            content=view,
            headers={
                "x-request-id": request.state.request_id,
                "x-config-version": str(cfg.version),
            },
        )

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
