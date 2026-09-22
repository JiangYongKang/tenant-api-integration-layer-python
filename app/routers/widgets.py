"""版本化 widget 路由（v1 / v2）。

每个写请求的处理流水线顺序固定、判定可解释：
  版本协商 -> 认证 -> 权限 -> 限流 -> 开关快照 -> 幂等裁决 -> 业务执行
任何一步失败都以统一错误契约返回，且日志记录输入与判定依据。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.auth import authenticate, require_scope
from app.dependencies import Deps
from app.errors import APIError, ErrorCode
from app.feature_flags import build_snapshot, request_scope
from app.idempotency import (
    IDEMPOTENCY_HEADER,
    enforce_tenant_body_limit,
    execute_idempotent,
)
from app.logging_setup import (
    config_version_var,
    log_event,
    tenant_id_var,
)
from app.ratelimit import resolve_rule, enforce
from app.request_context import RequestContext, bind, reset
from app.services import create_widget, get_insights, get_widget, validate_widget_input
from app.versioning import SUPPORTED_VERSIONS, check_accept_header

_WRITE_EP = {1: "POST /api/v1/widgets", 2: "POST /api/v2/widgets"}


def register(app: FastAPI, deps: Deps) -> None:
    for version in sorted(SUPPORTED_VERSIONS):
        _register_version(app, deps, version)


def _register_version(app: FastAPI, deps: Deps, version: int) -> None:
    prefix = f"/api/v{version}"

    async def _create(request: Request) -> Response:
        now_ms = deps.clock_ms()
        settings = deps.snapshot_settings()
        check_accept_header(request.headers.get("accept-version"), version)

        principal = authenticate(
            settings,
            request.headers.get("authorization"),
            now_ms // 1000,
            logger=deps.logger,
        )
        tenant_id_var.set(principal.tenant_id)
        config_version_var.set(settings.config_version)
        require_scope(principal, "widgets:write")

        endpoint_key = _WRITE_EP[version]
        rule = resolve_rule(
            settings.tenants[principal.tenant_id], endpoint_key, settings.default_rate_limit
        )
        enforce(deps.rate_limits, principal.tenant_id, endpoint_key, rule, now_ms)

        flags = build_snapshot(settings, principal.tenant_id)
        ctx = RequestContext(
            request_id=request.state.request_id,
            principal=principal,
            flags=flags,
            config_version=settings.config_version,
        )
        token = bind(ctx)
        raw = await request.body()
        # 全局硬上限已在 ASGI 层拦截；这里按认证后的租户上限做二次校验
        enforce_tenant_body_limit(
            raw,
            settings.tenants[principal.tenant_id].max_body_bytes,
            settings.default_max_body_bytes,
        )
        idem_key = request.headers.get(IDEMPOTENCY_HEADER.lower())

        def producer() -> tuple[int, dict[str, Any]]:
            # JSON 解析失败属于确定性的 422，同样纳入幂等重放：
            # 相同坏请求重复发送得到一致结果。
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise APIError(
                    ErrorCode.VALIDATION_FAILED,
                    422,
                    "请求体必须是合法的 UTF-8 JSON",
                    details={"fields": [{"field": "body", "rule": "invalid_json"}]},
                ) from None
            if parsed is None:
                raise APIError(
                    ErrorCode.VALIDATION_FAILED,
                    422,
                    "请求体不能为空",
                    details={"fields": [{"field": "body", "rule": "required"}]},
                )
            inp = validate_widget_input(version, parsed)
            body = create_widget(
                deps.resources, principal, flags, version, inp, now_ms
            )
            log_event(
                deps.logger,
                "widget_created",
                decision="side_effect",
                tenant_id=principal.tenant_id,
                api_version=version,
                input_idempotency_key=idem_key,
                input_name=inp.name,
                input_channel=inp.channel,
                endpoint=endpoint_key,
            )
            return 201, body

        try:
            with request_scope(flags):
                if not idem_key:
                    raise APIError(
                        ErrorCode.VALIDATION_FAILED,
                        422,
                        f"写请求必须提供 {IDEMPOTENCY_HEADER} 头以支持幂等",
                        details={"header": IDEMPOTENCY_HEADER, "rule": "required"},
                    )
                result = execute_idempotent(
                    deps.idempotency,
                    principal.tenant_id,
                    idem_key,
                    "POST",
                    f"{prefix}/widgets",
                    raw,
                    producer,
                )
        finally:
            reset(token)

        headers = {"X-Request-Id": request.state.request_id}
        if result.replayed:
            headers["Idempotent-Replay"] = "true"
            log_event(
                deps.logger,
                "idempotent_replay",
                decision="replay",
                tenant_id=principal.tenant_id,
                input_idempotency_key=idem_key,
            )
        return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)

    async def _get_one(request: Request, item_id: str) -> Response:
        now_ms = deps.clock_ms()
        settings = deps.snapshot_settings()
        check_accept_header(request.headers.get("accept-version"), version)
        principal = authenticate(
            settings,
            request.headers.get("authorization"),
            now_ms // 1000,
            logger=deps.logger,
        )
        tenant_id_var.set(principal.tenant_id)
        config_version_var.set(settings.config_version)
        require_scope(principal, "widgets:read")
        endpoint_key = f"GET {prefix}/widgets/{{item_id}}"
        rule = resolve_rule(
            settings.tenants[principal.tenant_id], endpoint_key, settings.default_rate_limit
        )
        enforce(deps.rate_limits, principal.tenant_id, endpoint_key, rule, now_ms)
        flags = build_snapshot(settings, principal.tenant_id)
        with request_scope(flags):
            body = get_widget(deps.resources, principal, flags, version, item_id)
        return JSONResponse(
            status_code=200,
            content=body,
            headers={"X-Request-Id": request.state.request_id},
        )

    app.add_api_route(f"{prefix}/widgets", _create, methods=["POST"])
    app.add_api_route(f"{prefix}/widgets/{{item_id}}", _get_one, methods=["GET"])

    if version == 2:
        async def _insights(request: Request, item_id: str) -> Response:
            now_ms = deps.clock_ms()
            settings = deps.snapshot_settings()
            check_accept_header(request.headers.get("accept-version"), version)
            principal = authenticate(
                settings,
                request.headers.get("authorization"),
                now_ms // 1000,
                logger=deps.logger,
            )
            tenant_id_var.set(principal.tenant_id)
            config_version_var.set(settings.config_version)
            require_scope(principal, "widgets:read")
            endpoint_key = "GET /api/v2/widgets/{item_id}/insights"
            rule = resolve_rule(
                settings.tenants[principal.tenant_id], endpoint_key, settings.default_rate_limit
            )
            enforce(deps.rate_limits, principal.tenant_id, endpoint_key, rule, now_ms)
            flags = build_snapshot(settings, principal.tenant_id)
            with request_scope(flags):
                body = get_insights(deps.resources, principal, item_id, flags)
            return JSONResponse(
                status_code=200,
                content=body,
                headers={"X-Request-Id": request.state.request_id},
            )

        app.add_api_route(
            f"{prefix}/widgets/{{item_id}}/insights", _insights, methods=["GET"]
        )
